import hashlib
import json
import queue
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import MagicMock

from accounts import Accounts
from app import make_server
from browser_agent import BASE, BrowserAgent
from core import Store
from pipeline import (PIPELINE_RULES_VERSION, Pipeline, gate, next_step, stage_of, tier, DONE, WAITING, FULFILLED)
from rejection_policy import REJECTION_RULES_VERSION

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)
URL = BASE + "/assist/detail?id=ABC123"
FILE = BASE + "/assist/download?id=FILE1"
AUTO = dict(pipeline_auto_publish=True, pipeline_auto_adopt=True, pipeline_rules_version=PIPELINE_RULES_VERSION)


def item(name="a", **fields):
    base = dict(id=name, title="Title " + name, doi="10.1234/" + name, source="website", owned=True, status="ready",
                created="2026-10-19T10:00:0" + str(len(name) % 10), account_id="default", assist_url=URL + name, website_status="")
    base.update(fields)
    return base


def queued(name="q", **fields):
    fields.setdefault("request_confirmed", True)
    return item(name, source="import", owned=False, assist_url="", **fields)


def waiting(name="w", **fields):
    return item(name, website_status=WAITING[0], **fields)


def fulfilled(name="f", **fields):
    return item(name, website_status=FULFILLED[0], **fields)


MATCH = dict(status="identity_match", sha256="x", primary_dois=["10.1234/d"], title_score=100,
             bibliography=dict(status="match"))


def deciding(name="d", **fields):
    base = dict(website_status=FULFILLED[0], status="downloaded", pdf="downloads/" + name + ".pdf",
                download_file_url=FILE, verification=dict(MATCH))
    base.update(fields)
    return item(name, **base)


class StageTests(unittest.TestCase):
    def test_stages(self):
        self.assertEqual("queued", stage_of(queued()))
        self.assertEqual("submitted", stage_of(waiting()))
        self.assertEqual("fulfilled", stage_of(fulfilled()))
        self.assertEqual("awaiting_decision", stage_of(deciding()))
        self.assertEqual("downloading", stage_of(fulfilled(status="downloading")))
        self.assertEqual("done", stage_of(item(website_status=DONE[0])))
        self.assertEqual("done", stage_of(deciding(adopt_action=dict(status="confirmed"))))
        self.assertEqual("demo", stage_of(dict(source="demo", status="ready")))

    def test_unknown_status_is_attention(self):
        self.assertEqual("attention", stage_of(item(website_status="审核中")))
        self.assertEqual("attention", stage_of(waiting(status="attention")))
        self.assertEqual("attention", stage_of(item(website_status="")))   # synced request without readable status

    def test_unconfirmed_actions_are_attention(self):
        for key in ("publish_action", "adopt_action", "reject_action"):
            for status in ("attempted", "uncertain"):
                self.assertEqual("attention", stage_of(deciding(**{key: dict(status=status)})), key)

    def test_published_item_without_site_status_is_submitted(self):
        self.assertEqual("submitted", stage_of(item(publish_action=dict(status="confirmed"))))

    def test_rejection_returns_to_submitted_until_fresh_sync(self):
        rejected = fulfilled(rejected_at="2026-10-19T11:00:00+00:00", website_status_time="2026-10-19T10:00:00+00:00")
        self.assertEqual("submitted", stage_of(rejected))
        rejected["website_status_time"] = "2026-10-19T11:05:00+00:00"
        self.assertEqual("fulfilled", stage_of(rejected))
        rejected["website_status"] = WAITING[0]
        self.assertEqual("submitted", stage_of(rejected))


class TierGateTests(unittest.TestCase):
    def test_tier(self):
        self.assertEqual("single", tier(None))
        self.assertEqual("single", tier(499))
        self.assertEqual("batch", tier(500))

    def test_low_points_with_fulfilled_blocks(self):
        result = gate([fulfilled(), queued()], 499)
        self.assertTrue(result["blocked"])
        self.assertEqual("f", result["item_id"])
        self.assertTrue(gate([deciding()], None)["blocked"])
        self.assertTrue(gate([fulfilled(status="downloading")], 10)["blocked"])

    def test_waiting_alone_does_not_block(self):
        self.assertFalse(gate([waiting("w1"), waiting("w2")], 100)["blocked"])

    def test_high_points_never_block(self):
        self.assertFalse(gate([fulfilled(), deciding("d2")], 500)["blocked"])


class ProjectedPointsTests(unittest.TestCase):
    def test_crossing_below_500_applies_single_rules(self):
        self.assertEqual("single", tier(505, 10))
        self.assertTrue(gate([fulfilled()], 505, 10)["blocked"])

    def test_staying_above_500_is_open(self):
        self.assertEqual("batch", tier(600, 50))
        self.assertFalse(gate([fulfilled()], 600, 50)["blocked"])
        self.assertFalse(gate([fulfilled()], 510, 10)["blocked"])      # exactly 500 after publishing is still batch

    def test_next_step_uses_item_reward_and_text(self):
        prefs = dict(pipeline_auto_publish=True, pipeline_rules_version=PIPELINE_RULES_VERSION, oa_first=False)
        now = datetime(2026, 10, 20, 12, 0).astimezone()
        blocked = next_step([item("f2", status="attention", source="website", website_status="待确认", assist_url="u", owned=True, request_confirmed=True), queued(request_reward=10)],
                            dict(points=505), prefs, now)
        self.assertEqual("wait_user", blocked["action"])
        opened = next_step([queued(request_reward=50)], dict(points=600), prefs, now)
        self.assertEqual("publish", opened["action"])
        self.assertIn("发布后积分仍不低于 500", opened["reason"])
        crossing = next_step([queued(request_reward=10)], dict(points=505), prefs, now)
        self.assertIn("发布后积分将低于 500，按逐条求助规则处理", crossing["reason"])

    def test_site_gate_hold_waits_until_a_newer_sync(self):
        prefs = dict(pipeline_auto_publish=True, pipeline_rules_version=PIPELINE_RULES_VERSION, oa_first=False)
        now = datetime(2026, 10, 20, 12, 0).astimezone()
        hold = "2026-10-20T11:50:00+00:00"
        step_ = next_step([queued()], dict(site_gate=hold, last_sync_time=hold), prefs, now)
        self.assertEqual(("wait_user", True), (step_["action"], step_["requires_user"]))
        later = next_step([queued()], dict(site_gate=hold, last_sync_time="2026-10-20T11:55:00+00:00"), prefs, now)
        self.assertEqual("publish", later["action"])


class AttentionIsolationTests(unittest.TestCase):
    """H5: one attention item blocks only itself and the publish gate, never downloads/sync/decisions of the others."""
    def plan(self, items, account=None, prefs=None):
        return next_step(items, account or {}, dict(oa_first=False, **(prefs or {})), NOW)

    def bad(self):
        return waiting("bad", status="attention", note="出错了")

    def test_other_fulfilled_item_is_still_downloaded(self):
        step = self.plan([self.bad(), fulfilled("f2")])
        self.assertEqual(("download", "f2"), (step["action"], step["item_id"]))

    def test_sync_still_runs(self):
        old = dict(last_sync_time=(NOW - timedelta(minutes=30)).isoformat())
        self.assertEqual("sync", self.plan([self.bad(), waiting("w2")], old)["action"])

    def test_other_item_can_be_auto_adopted(self):
        self.assertEqual("adopt", self.plan([self.bad(), deciding("d2")], prefs=AUTO)["action"])

    def test_publish_is_gated_while_something_needs_attention(self):
        for prefs in (None, AUTO):
            step = self.plan([self.bad(), queued("q2")], {"points": 900}, prefs)
            self.assertEqual(("wait_user", "bad", True), (step["action"], step["item_id"], step["requires_user"]), prefs)

    def test_attention_alone_still_asks_the_person(self):
        step = self.plan([self.bad()])
        self.assertEqual(("wait_user", "bad"), (step["action"], step["item_id"]))
        self.assertIn("出错了", step["reason"])


class NextStepTests(unittest.TestCase):
    def plan(self, items, account=None, prefs=None):
        return next_step(items, account or {}, dict(oa_first=False, **(prefs or {})), NOW)   # OA step has its own tests

    def test_priority_ordering(self):
        # downloading > awaiting_decision > fulfilled > sync > publish > idle; an attention item only blocks itself and publishing (H5)
        layers = [waiting("bad", status="attention"), fulfilled("dl", status="downloading"), deciding(), fulfilled(), waiting(), queued()]
        expected = ["wait", "wait", "wait_user", "download", "sync", "prepare", "idle"]
        reasons = []
        for count in range(len(layers) + 1):
            reasons.append(self.plan(layers[count:])["action"])
        self.assertEqual(expected, reasons)
        self.assertEqual("dl", self.plan(layers)["item_id"])
        self.assertEqual("dl", self.plan(layers[1:])["item_id"])
        self.assertEqual("publish", self.plan(layers[5:], prefs=AUTO)["action"])

    def test_attention_reason_and_requires_user(self):
        result = self.plan([waiting(status="attention", note="出错了")])
        self.assertTrue(result["requires_user"])
        self.assertIn("出错了", result["reason"])

    def test_sync_respects_interval(self):
        recent = dict(last_sync_time=(NOW - timedelta(minutes=3)).isoformat())
        old = dict(last_sync_time=(NOW - timedelta(minutes=11)).isoformat())
        self.assertEqual("idle", self.plan([waiting()], recent)["action"])
        self.assertEqual("sync", self.plan([waiting()], old)["action"])
        self.assertEqual("idle", self.plan([waiting()], old, dict(pipeline_sync_minutes=30))["action"])

    def test_sync_does_not_run_without_submitted_items(self):
        self.assertEqual("idle", self.plan([item(website_status=DONE[0])])["action"])

    def test_auto_publish_off_prepares_for_user(self):
        result = self.plan([queued()])
        self.assertEqual(("prepare", True), (result["action"], result["requires_user"]))
        self.assertEqual("wait_user", self.plan([queued(status="prepared")])["action"])

    def test_auto_publish_requires_current_rules_version(self):
        self.assertEqual("prepare", self.plan([queued()], prefs=dict(pipeline_auto_publish=True))["action"])
        result = self.plan([queued()], prefs=AUTO)
        self.assertEqual(("publish", False, "q"), (result["action"], result["requires_user"], result["item_id"]))

    def test_publish_is_gated_below_500(self):
        # a fulfilled answer is handled first (download), never a new publish
        self.assertEqual("download", self.plan([fulfilled(), queued()], dict(points=100), AUTO)["action"])
        self.assertEqual("download", self.plan([fulfilled(), queued()], dict(points=900), AUTO)["action"])

    def test_waiting_items_do_not_stop_publish(self):
        result = self.plan([waiting(), queued()], dict(points=100, last_sync_time=NOW.isoformat()), AUTO)
        self.assertEqual("publish", result["action"])

    def test_manual_decision_waits_for_user(self):
        result = self.plan([deciding()], prefs=dict(pipeline_auto_adopt=False))
        self.assertEqual(("wait_user", True), (result["action"], result["requires_user"]))
        self.assertEqual("wait_user", self.plan([deciding()], prefs=dict(pipeline_auto_adopt=True))["action"])  # no rules version

    def test_auto_adopt_only_on_full_match(self):
        self.assertEqual("adopt", self.plan([deciding()], prefs=AUTO)["action"])
        weak = deciding(verification=dict(MATCH, bibliography=dict(status="unverified")))
        self.assertEqual("wait_user", self.plan([weak], prefs=AUTO)["action"])
        review = deciding(verification=dict(MATCH, status="review"))
        self.assertEqual("wait_user", self.plan([review], prefs=AUTO)["action"])
        nobib = deciding(verification=dict(status="identity_match", sha256="x"))
        self.assertEqual("wait_user", self.plan([nobib], prefs=AUTO)["action"])
        tried = deciding(adopt_action=dict(status="attempted"))
        self.assertEqual("wait_user", self.plan([tried], prefs=AUTO)["action"])
        demo = dict(deciding(), source="demo")
        self.assertEqual("idle", self.plan([demo], prefs=AUTO)["action"])

    def test_auto_reject_takes_precedence(self):
        bad = deciding(doi_origin="user", verification=dict(status="conflict", primary_dois=["10.9999/other"], title_score=20, sha256="x"))
        prefs = dict(rejection_mode="auto", rejection_rules_version=REJECTION_RULES_VERSION)
        self.assertEqual("reject_auto", self.plan([bad], prefs=prefs)["action"])
        self.assertEqual("wait_user", self.plan([bad], prefs={})["action"])

    def test_download_picks_earliest(self):
        a, b = fulfilled("b1", created="2026-10-19T09:00:00"), fulfilled("a1", created="2026-10-19T08:00:00")
        self.assertEqual("a1", self.plan([a, b])["item_id"])


class FakeAgent:
    def __init__(self):
        self.busy, self.commands, self.calls = False, queue.Queue(), []

    def enqueue(self, action, item_id=None, credentials=None):
        self.calls.append((action, item_id))


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.accounts = Accounts(self.temp.name)
        self.agent = FakeAgent()

    def make(self):
        return Pipeline(self.store, self.accounts, self.agent, start=False)

    def test_enabled_resets_on_restart(self):
        pipeline = self.make()
        self.store.update_preferences(pipeline_enabled=True)
        self.assertTrue(pipeline.enabled())
        again = Pipeline(Store(self.temp.name), Accounts(self.temp.name), self.agent, start=False)
        self.assertFalse(again.enabled())
        self.assertFalse(Store(self.temp.name).snapshot()["preferences"]["pipeline_enabled"])

    def test_tick_only_when_enabled_idle_and_not_user_step(self):
        pipeline = self.make()
        self.store.add("Paper", "10.1/x", account_id="default", source="website", owned=True, assist_url=URL, website_status=FULFILLED[0])
        self.assertIsNone(pipeline.tick(NOW))
        self.store.update_preferences(pipeline_enabled=True)
        self.agent.busy = True
        self.assertIsNone(pipeline.tick(NOW))
        self.agent.busy = False
        self.agent.commands.put("x")
        self.assertIsNone(pipeline.tick(NOW))
        self.agent.commands.get()
        self.assertEqual("download", pipeline.tick(NOW)["action"])
        self.assertEqual([("download", self.store.snapshot()["items"][0]["id"])], self.agent.calls)

    def test_user_steps_are_never_executed(self):
        pipeline = self.make()
        self.store.add("Local", "10.1/y", account_id="default")
        self.store.update_preferences(pipeline_enabled=True)
        self.assertIsNone(pipeline.tick(NOW))   # prepare requires the user
        self.assertEqual([], self.agent.calls)

    def test_other_account_items_are_ignored(self):
        pipeline = self.make()
        self.store.add("Other", "10.1/z", account_id="someone-else", source="website", owned=True, assist_url=URL, website_status=FULFILLED[0])
        self.store.update_preferences(pipeline_enabled=True)
        self.assertIsNone(pipeline.tick(NOW))


class ArchiveAndActionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.agent = BrowserAgent(self.store, Accounts(self.temp.name))
        self.addCleanup(self.agent.thread.join, 0.1)
        self.addCleanup(self.agent.commands.put, ("close", None, None))
        self.page = MagicMock()
        self.page.url = URL
        self.agent.context, self.agent.page = MagicMock(), self.page
        self.agent.goto = MagicMock()   # no navigation; the fake page is already "there"
        self.page.is_closed.return_value = False
        self.card = MagicMock()
        link = MagicMock()
        link.count.return_value = 1
        link.last = link
        self.page.get_by_role.return_value.count.return_value = 1
        link.locator.return_value = self.card
        self.card.count.return_value = 1
        self.card.inner_text.return_value = "应助文件 FILE1"
        self.controls = self.card.get_by_role.return_value.or_.return_value
        self.controls.count.return_value = 1
        dialog = MagicMock()
        dialog.count.return_value = 0
        body = MagicMock()
        body.inner_text.return_value = "当前拥有 640 积分"
        table = MagicMock()
        table.all.return_value = []
        def locator(selector, *a, **k):
            return (link if selector.startswith("a[href") else dialog if "dialog" in selector else table if selector == "table tr" else body)
        self.page.locator.side_effect = locator
        self.link = link
        self.body = body

    def downloaded(self, **fields):
        folder = self.store.root / "downloads"
        folder.mkdir(exist_ok=True)
        data = b"%PDF-1.4 fixture"
        (folder / "one.pdf").write_bytes(data)
        item_id = self.store.add("Paper", "10.1234/ok", source="website", account_id="default", owned=True, assist_url=URL,
                                 website_status=FULFILLED[0], pdf="downloads/one.pdf", download_file_url=FILE,
                                 verification=dict(status="conflict", primary_dois=["10.9/other"], title_score=10,
                                                   sha256=hashlib.sha256(data).hexdigest()), **fields)
        return item_id

    def test_confirmed_reject_archives_file_without_deleting(self):
        item_id = self.downloaded(manual_reject_reason="文件不一致：首页是另一篇", manual_reject_category="file_mismatch")
        self.agent.reject(item_id)
        after = self.store.get(item_id)
        self.assertEqual("", after["pdf"])
        self.assertIsNone(after["verification"])
        self.assertIsNone(after["reject_action"])
        self.assertEqual("rejected", after["status"])
        archived = after["rejected_files"][0]
        self.assertEqual("downloads/one.pdf", archived["pdf"])
        self.assertEqual(FILE, archived["download_file_url"])
        self.assertEqual("confirmed", archived["reject_action"]["status"])
        self.assertTrue((self.store.root / "downloads" / "one.pdf").is_file())
        self.assertEqual("submitted", stage_of(after))

    def test_next_download_does_not_overwrite_archived_file(self):
        item_id = self.downloaded(manual_reject_reason="文件不一致：首页是另一篇", manual_reject_category="file_mismatch")
        self.agent.reject(item_id)
        download = MagicMock()
        download.suggested_filename = "Original Name.pdf"
        download.save_as.side_effect = lambda path: Path(path).write_bytes(b"%PDF-1.4 second")
        self.agent.stop.clear()
        from unittest.mock import patch
        with patch("browser_agent.verify_with_metadata", return_value=dict(status="review", reason="x", sha256="y")):
            self.agent.save_download(item_id, download, "Paper", "10.1234/ok")
        self.assertEqual(Path("downloads/%s/answer-2/Original Name.pdf" % item_id), Path(self.store.get(item_id)["pdf"]))
        self.assertEqual(b"%PDF-1.4 fixture", (self.store.root / "downloads" / "one.pdf").read_bytes())

    def test_adopt_confirmed_and_uncertain_never_retry(self):
        item_id = self.downloaded()
        self.store.update(item_id, verification=dict(MATCH, sha256=hashlib.sha256(b"%PDF-1.4 fixture").hexdigest()))
        self.agent.adopt(item_id)
        self.assertEqual("confirmed", self.store.get(item_id)["adopt_action"]["status"])
        self.assertEqual("done", stage_of(self.store.get(item_id)))
        self.controls.click.assert_called_once()
        with self.assertRaises(ValueError):
            self.agent.adopt(item_id)
        self.controls.click.assert_called_once()

    def test_adopt_unconfirmed_is_uncertain(self):
        item_id = self.downloaded()
        self.store.update(item_id, verification=dict(MATCH, sha256=hashlib.sha256(b"%PDF-1.4 fixture").hexdigest()))
        self.card.get_by_text.return_value.first.wait_for.side_effect = Exception("timeout")
        with self.assertRaises(ValueError):
            self.agent.adopt(item_id)
        self.assertEqual("uncertain", self.store.get(item_id)["adopt_action"]["status"])
        self.assertEqual("attention", stage_of(self.store.get(item_id)))

    def test_adopt_requires_unchanged_file_and_unique_control(self):
        item_id = self.downloaded()
        self.store.update(item_id, verification=dict(MATCH, sha256="0" * 64))
        with self.assertRaises(ValueError):
            self.agent.adopt(item_id)
        self.store.update(item_id, verification=dict(MATCH, sha256=hashlib.sha256(b"%PDF-1.4 fixture").hexdigest()))
        self.controls.count.return_value = 2
        with self.assertRaises(ValueError):
            self.agent.adopt(item_id)
        self.controls.click.assert_not_called()
        self.assertIsNone(self.store.get(item_id).get("adopt_action"))

    def test_adopt_refused_for_wrong_stage(self):
        item_id = self.store.add("Waiting", "", source="website", account_id="default", owned=True, assist_url=URL, website_status=WAITING[0])
        with self.assertRaises(ValueError):
            self.agent.adopt(item_id)

    def test_publish_blocked_by_gate_does_not_touch_page(self):
        self.store.add("Answer", "", source="website", account_id="default", owned=True, assist_url=URL + "z", website_status=FULFILLED[0])
        item_id = self.store.add("New", "10.1234/new", account_id="default", metadata_ref=dict(title="New"))
        with self.assertRaises(ValueError):
            self.agent.publish(item_id)
        self.page.goto.assert_not_called()
        self.assertIsNone(self.store.get(item_id).get("publish_action"))

    def test_publish_rechecks_points_on_page(self):
        self.store.update_account(account_id="default", points=900)   # stale; page says 640 -> still batch
        self.store.add("Answer", "", source="website", account_id="default", owned=True, assist_url=URL + "z", website_status=FULFILLED[0])
        item_id = self.store.add("New", "10.1234/new", account_id="default", metadata_ref=dict(title="New"))
        self.body.inner_text.return_value = "当前拥有 120 积分"
        with self.assertRaises(ValueError):
            self.agent.publish(item_id)   # page shows <500 so the gate closes before any click
        self.assertEqual(120, self.store.snapshot()["account_metrics"]["default"]["points"])
        self.assertIsNone(self.store.get(item_id).get("publish_action"))

    def test_sync_reads_points_when_present(self):
        self.agent.sync(None)
        metrics = self.store.snapshot()["account_metrics"]["default"]
        self.assertEqual(640, metrics["points"])
        self.assertIn("last_sync_time", metrics)

    def test_sync_without_balance_keeps_points(self):
        self.store.update_account(account_id="default", points=77)
        self.body.inner_text.return_value = "没有余额信息"
        self.agent.sync(None)
        self.assertEqual(77, self.store.snapshot()["account_metrics"]["default"]["points"])

    def test_switch_account_disables_pipeline(self):
        self.store.update_preferences(pipeline_enabled=True)
        other = self.agent.accounts.add("Second")
        self.agent.switch_account(other)
        self.assertFalse(self.store.snapshot()["preferences"]["pipeline_enabled"])


class PipelineApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.server = make_server(0, self.temp.name, auto_screen=False)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        html = urlopen(self.base).read().decode()
        self.token = html.split("const TOKEN=")[1].split(";let state=")[0].strip('"')

    def tearDown(self):
        self.server.pipeline.close()
        self.server.shutdown()
        self.server.server_close()
        self.server.agent.commands.put(("close", None, None))
        self.server.agent.thread.join(timeout=2)
        self.thread.join()
        self.temp.cleanup()

    def post(self, route, payload=None):
        request = Request(self.base + "/api/" + route, json.dumps(payload or {}).encode(), method="POST",
                          headers={"Content-Type": "application/json", "Origin": self.base, "X-Demo-Token": self.token})
        return json.load(urlopen(request))

    def state(self):
        return json.load(urlopen(self.base + "/api/state"))

    def bad(self, route, payload):
        with self.assertRaises(HTTPError) as caught:
            self.post(route, payload)
        self.assertEqual(400, caught.exception.code)

    def test_state_has_stage_and_pipeline_shape(self):
        self.post("import", {"text": "10.1234/shape\tA local paper"})
        state = self.state()
        self.assertEqual("queued", state["items"][0]["stage"])
        pipe = state["pipeline"]
        self.assertEqual({"enabled", "auto_publish", "auto_adopt", "sync_minutes", "rules_version", "points", "tier", "gate",
                          "next_step", "last_tick", "oa_first", "oa_challenge_mode", "oa_running", "session", "core_key_configured", "batch_pending", "restart_notice"}, set(pipe))
        self.assertEqual((False, False, False, 10, PIPELINE_RULES_VERSION, None, "single"),
                         (pipe["enabled"], pipe["auto_publish"], pipe["auto_adopt"], pipe["sync_minutes"], pipe["rules_version"], pipe["points"], pipe["tier"]))
        self.assertEqual({"blocked", "item_id", "reason"}, set(pipe["gate"]))
        self.assertEqual({"action", "item_id", "label", "requires_user", "reason"}, set(pipe["next_step"]))
        self.assertEqual("oa_fetch", pipe["next_step"]["action"])
        self.assertIn("workflow", state["items"][0])

    def test_validation(self):
        for payload in ({}, {"enabled": "yes"}, {"enabled": 1}, {"auto_publish": None}, {"sync_minutes": 4}, {"sync_minutes": 121},
                        {"sync_minutes": "10"}, {"sync_minutes": True}, {"sync_minutes": 10.0}, {"bogus": True}, {"rules_version": PIPELINE_RULES_VERSION}):
            self.bad("pipeline", payload)
        self.assertFalse(self.state()["pipeline"]["enabled"])

    def test_auto_modes_need_matching_rules_version(self):
        self.bad("pipeline", {"auto_publish": True})
        self.bad("pipeline", {"auto_adopt": True, "rules_version": "old"})
        self.post("pipeline", {"auto_publish": True, "rules_version": PIPELINE_RULES_VERSION})
        pipe = self.state()["pipeline"]
        self.assertEqual((True, False), (pipe["auto_publish"], pipe["auto_adopt"]))
        self.post("pipeline", {"auto_publish": False})
        self.assertFalse(self.state()["pipeline"]["auto_publish"])
        self.assertEqual("", self.server.store.snapshot()["preferences"]["pipeline_rules_version"])

    def test_enable_and_sync_interval(self):
        self.post("pipeline", {"enabled": True, "sync_minutes": 30})
        pipe = self.state()["pipeline"]
        self.assertEqual((True, 30), (pipe["enabled"], pipe["sync_minutes"]))

    def test_stale_rules_version_disables_effective_auto_mode(self):
        self.server.store.update_preferences(pipeline_auto_publish=True, pipeline_rules_version="old")
        self.assertFalse(self.state()["pipeline"]["auto_publish"])

    def test_step_endpoint_refuses_user_steps_and_runs_others(self):
        self.post("pipeline", {"oa_first": False})
        self.post("import", {"text": "10.1234/step\tA local paper"})
        self.bad("pipeline/step", {})
        self.server.store.add("Answer", "", source="website", account_id="default", owned=True, assist_url=URL, website_status=FULFILLED[0])
        calls = []
        self.server.agent.enqueue = lambda *a, **k: calls.append(a)
        result = self.post("pipeline/step", {})
        self.assertTrue(result["executed"])
        self.assertEqual("download", calls[0][0])

    def test_browser_adopt_and_publish_guards(self):
        self.post("import", {"text": "10.1234/guard\tA local paper"})
        local = self.state()["items"][0]["id"]
        calls = []
        self.server.agent.enqueue = lambda *a, **k: calls.append(a)
        self.bad("browser", {"action": "adopt", "id": local, "confirmed": True})           # wrong stage
        waiting_id = self.server.store.add("W", "", source="website", account_id="default", owned=True, assist_url=URL, website_status=WAITING[0])
        self.bad("browser", {"action": "publish", "id": waiting_id})                       # already published
        deciding_id = self.server.store.add("D", "10.1/d", source="website", account_id="default", owned=True, assist_url=URL + "d",
                                            website_status=FULFILLED[0], pdf="downloads/d.pdf", download_file_url=FILE, verification=dict(MATCH))
        self.bad("browser", {"action": "adopt", "id": deciding_id})                        # not confirmed
        self.bad("browser", {"action": "adopt", "id": deciding_id, "confirmed": "true"})
        self.assertEqual([], calls)
        self.post("browser", {"action": "adopt", "id": deciding_id, "confirmed": True})
        self.bad("browser", {"action": "publish", "id": local})                              # publish spends points: confirmation required
        self.post("browser", {"action": "publish", "id": local, "confirmed": True})
        self.assertEqual([("adopt", deciding_id, None), ("publish", local, None)], calls)

    def test_mcp_permission_still_required_for_new_actions(self):
        self.post("import", {"text": "10.1234/mcp\tA local paper"})
        local = self.state()["items"][0]["id"]
        self.bad("browser", {"action": "publish", "id": local, "mcp_call": True})

    def test_account_switch_disables_pipeline(self):
        self.post("pipeline", {"enabled": True})
        other = self.server.accounts.add("Second test account")
        self.server.agent.switch_account(other)
        self.assertFalse(self.state()["pipeline"]["enabled"])

    def test_state_reads_do_not_start_browser_work(self):
        self.state()
        self.assertTrue(self.server.agent.commands.empty())


if __name__ == "__main__":
    unittest.main()
