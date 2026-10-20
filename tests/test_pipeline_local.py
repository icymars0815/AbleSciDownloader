"""Local OA work is independent of 科研通 waits; oa_review is non-blocking; session evidence; 已完结 workflow. All mocked."""
import json
import queue
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from accounts import Accounts
from app import make_server
from browser_agent import BASE
from core import Store
from pipeline import Pipeline, has_session, next_local_step, next_step, stage_of
from task_states import workflow_state

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)
PDF = b"%PDF-1.7\n" + b"x" * 100
URL = BASE + "/assist/detail?id=ABC"
MATCH = dict(status="identity_match", sha256="x", bibliography=dict(status="match"))


def local(name="l", **fields):
    fields.setdefault("request_confirmed", True)
    base = dict(id=name, title="Local " + name, doi="10.1234/" + name, source="import", owned=False, status="ready",
                created="2026-10-19T10:00:00", account_id="default", assist_url="", website_status="")
    base.update(fields)
    return base


def site(name="s", **fields):
    base = dict(id=name, title="Site " + name, doi="10.1234/" + name, source="website", owned=True, status="ready",
                created="2026-10-19T09:00:00", account_id="default", assist_url=URL + name, website_status="待确认")
    base.update(fields)
    return base


def awaiting(name="a", **fields):
    return site(name, status="downloaded", pdf="downloads/%s.pdf" % name, download_file_url=BASE + "/assist/download?id=F",
                verification=dict(MATCH), **fields)


def reviewing(name="r", **fields):
    return local(name, status="oa_review", pdf="oa/%s.pdf" % name, verification=dict(status="review", reason="x"),
                 oa_check=dict(status="downloaded"), **fields)


class FakeAgent:
    def __init__(self):
        self.busy, self.commands, self.calls = False, queue.Queue(), []

    def enqueue(self, action, item_id=None, credentials=None):
        self.calls.append((action, item_id))


class LocalStepTests(unittest.TestCase):
    def test_local_step_ignores_every_site_side_wait(self):
        pending = local("p")
        waits = [site("bad", status="attention"), site("dl", status="downloading"), awaiting(), site("g", website_status="待确认")]
        self.assertEqual("oa_fetch", next_local_step([pending], {})["action"])
        for wait in waits:
            items = [wait, pending]
            self.assertEqual(("oa_fetch", "p"), tuple(next_local_step(items, {})[k] for k in ("action", "item_id")), wait["id"])
            # the site-side planner (local=False) keeps its own wait and does not publish the item that is still awaiting OA
            planned = next_step(items, {}, {}, NOW, local=False)
            self.assertNotIn(planned["action"], ("prepare", "publish", "oa_fetch"))
            self.assertNotEqual("p", planned["item_id"])

    def test_old_planner_still_reports_oa_after_waits_only_when_unblocked(self):
        # compatibility: next_step(local=True) keeps its historical priority (site waits first)
        self.assertEqual("wait_user", next_step([awaiting(), local("p")], {}, {}, NOW)["action"])
        self.assertEqual("oa_fetch", next_step([local("p")], {}, {}, NOW)["action"])

    def test_no_local_step_when_not_applicable(self):
        self.assertIsNone(next_local_step([local(doi="")], {}))
        self.assertIsNone(next_local_step([local(oa_check=dict(status="failed"))], {}))
        self.assertIsNone(next_local_step([local()], dict(oa_first=False)))
        self.assertIsNone(next_local_step([local(source="demo")], {}))
        self.assertIsNone(next_local_step([reviewing()], {}))

    def test_item_awaiting_oa_is_not_published_in_the_same_tick(self):
        prefs = dict(pipeline_auto_publish=True, pipeline_rules_version="2026-10-19-p1")
        planned = next_step([local("p")], dict(points=900), prefs, NOW, local=False)
        self.assertEqual("idle", planned["action"])
        planned = next_step([local("p", oa_check=dict(status="failed"))], dict(points=900), prefs, NOW, local=False)
        self.assertEqual("publish", planned["action"])


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.agent = FakeAgent()
        self.pipeline = Pipeline(self.store, Accounts(self.temp.name), self.agent, start=False)

    def add(self, items):
        with self.store.lock:
            self.store.data["items"].extend(items)
            self.store.save()

    def run_tick(self):
        started = []
        with patch.object(Pipeline, "oa_start", lambda s, item_id, human=False, **o: started.append(item_id)):
            planned = self.pipeline.tick(NOW)
        return planned, started

    def test_oa_step_runs_while_an_item_awaits_decision(self):
        self.add([awaiting(), local("p")])
        self.store.update_preferences(pipeline_enabled=True)
        self.assertEqual("wait_user", self.pipeline.plan(NOW)[3]["action"])           # the 科研通 side is waiting for the user
        planned, started = self.run_tick()
        self.assertEqual(["p"], started)
        self.assertEqual("oa_fetch", planned["action"])
        self.assertEqual([], self.agent.calls)                                          # never through BrowserAgent

    def test_oa_step_runs_while_an_item_needs_attention_or_browser_busy(self):
        self.add([site("bad", status="attention"), local("p")])
        self.store.update_preferences(pipeline_enabled=True)
        self.agent.busy = True
        planned, started = self.run_tick()
        self.assertEqual((["p"], "oa_fetch"), (started, planned["action"]))

    def test_oa_is_single_concurrency(self):
        self.add([awaiting(), local("p"), local("q", created="2026-10-19T10:01:00")])
        self.store.update_preferences(pipeline_enabled=True)
        release = threading.Event()
        entered = []

        def slow(doi, dest, email="", **kwargs):
            entered.append(doi)
            release.wait(5)
            return dict(status="failed", attempts=[])
        with patch("oa_fetch.fetch_open_access", slow):
            self.pipeline.tick(NOW)
            self.pipeline.tick(NOW)
            self.pipeline.tick(NOW)
            self.assertEqual(["10.1234/p"], entered)
            self.assertEqual("p", self.pipeline.oa_running)
            release.set()
            self.pipeline.oa_thread.join(5)
        self.assertEqual("failed", self.store.get("p")["oa_check"]["status"])

    def test_site_steps_still_proceed_when_oa_running(self):
        self.add([site("g"), local("p")])
        self.store.update_preferences(pipeline_enabled=True)
        self.pipeline.oa_running = "p"
        planned = self.pipeline.tick(NOW)
        self.assertEqual("download", planned["action"])
        self.assertEqual([("download", "g")], self.agent.calls)

    def test_manual_step_endpoint_logic_prefers_local_when_site_waits(self):
        self.add([awaiting(), local("p")])
        self.assertEqual("p", self.pipeline.local_plan()["item_id"])


class OaReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.pipeline = Pipeline(self.store, Accounts(self.temp.name), FakeAgent(), start=False)
        self.item = self.store.add("A paper", "10.1234/oa.test", account_id="default", request_confirmed=True)

    def download_non_match(self, status="title_mismatch"):
        def run(doi, dest, email="", **kwargs):
            Path(dest).write_bytes(PDF)
            return dict(status="downloaded", oa_status="gold", source="OpenAlex", attempts=[])
        with patch("oa_fetch.fetch_open_access", run), patch("pipeline.verify_with_metadata", lambda *a, **k: dict(status=status, reason="mock", sha256="x")):
            self.pipeline.oa_run(self.item)

    def test_non_match_is_oa_review_not_attention(self):
        self.download_non_match()
        item = self.store.get(self.item)
        self.assertEqual("oa_review", stage_of(item))
        self.assertEqual("review", workflow_state(item))
        self.assertNotIn("done_via", item)
        self.assertTrue(item["pdf"])

    def test_oa_review_does_not_block_other_work(self):
        self.download_non_match()
        other = self.store.add("Other", "10.1234/other", account_id="default", request_confirmed=True)
        step = next_step(self.store.snapshot()["items"], dict(points=100), {}, NOW)
        self.assertEqual(("oa_fetch", other), (step["action"], step["item_id"]))
        self.store.update(other, oa_check=dict(status="failed"))
        step = next_step(self.store.snapshot()["items"], dict(points=100), {}, NOW)
        self.assertEqual(("prepare", other), (step["action"], step["item_id"]))              # not a wait_user on the review item
        self.assertEqual("idle", next_step([reviewing()], dict(points=100), {}, NOW)["action"])

    def test_accept_marks_done(self):
        self.download_non_match()
        self.pipeline.oa_accept(self.item)
        item = self.store.get(self.item)
        self.assertEqual(("done", "oa"), (stage_of(item), item["done_via"]))
        self.assertEqual("done", workflow_state(item))
        with self.assertRaises(ValueError):
            self.pipeline.oa_accept(self.item)

    def test_discard_keeps_file_and_proceeds_to_site(self):
        self.download_non_match()
        path = Path(self.temp.name) / self.store.get(self.item)["pdf"]
        self.pipeline.oa_discard(self.item)
        item = self.store.get(self.item)
        self.assertTrue(path.is_file())                                                     # file stays on disk
        self.assertEqual("", item["pdf"])
        self.assertIsNone(item["verification"])
        self.assertEqual("skipped", item["oa_check"]["status"])
        self.assertEqual(1, len(item["oa_rejected_files"]))
        self.assertEqual("oa/%s.pdf" % self.item, item["oa_rejected_files"][0]["pdf"].replace("\\", "/"))
        self.assertEqual("queued", stage_of(item))
        self.assertEqual("prepare", next_step(self.store.snapshot()["items"], dict(points=100), {}, NOW)["action"])
        self.assertIsNone(next_local_step(self.store.snapshot()["items"], {}))             # no OA retry after skipping
        with self.assertRaises(ValueError):
            self.pipeline.oa_discard(self.item)

    def test_only_oa_review_items_can_be_decided(self):
        with self.assertRaises(ValueError):
            self.pipeline.oa_accept(self.item)
        with self.assertRaises(ValueError):
            self.pipeline.oa_discard(self.item)

    def test_identity_match_is_still_done(self):
        self.download_non_match("identity_match")
        self.assertEqual("done", stage_of(self.store.get(self.item)))


class SessionTests(unittest.TestCase):
    def test_login_event_and_cookie_file_count_as_possible_session(self):
        self.assertFalse(has_session({}, [local()]))
        self.assertTrue(has_session({"last_login_time": "2026-10-19T10:00:00"}, [local()]))
        self.assertTrue(has_session({}, [local()], profile_cookie=True))
        self.assertTrue(has_session({"points": 0}, [local()]))
        self.assertFalse(has_session({}, [local()], profile_cookie=False))

    def test_cookie_file_detection_per_account(self):
        with tempfile.TemporaryDirectory() as temp:
            accounts = Accounts(temp)
            self.assertFalse(accounts.has_cookie_file())
            profile = Path(temp) / "browser-profile" / "Default" / "Network"
            profile.mkdir(parents=True)
            (profile / "Cookies").write_bytes(b"")
            self.assertFalse(accounts.has_cookie_file())                                    # empty file is no evidence
            (profile / "Cookies").write_bytes(b"sqlite")
            self.assertTrue(accounts.has_cookie_file())
            other = accounts.add("Other")
            self.assertFalse(accounts.has_cookie_file(other))

    def test_pipeline_uses_cookie_file_and_manual_only_otherwise(self):
        with tempfile.TemporaryDirectory() as temp:
            store, accounts = Store(temp), Accounts(temp)
            pipeline = Pipeline(store, accounts, FakeAgent(), start=False)
            store.add("Free", "10.1/x", account_id="default", request_confirmed=True)
            store.update(store.snapshot()["items"][0]["id"], oa_check=dict(status="not_oa"))
            step = pipeline.plan(NOW)[3]
            self.assertEqual(("wait_user", True), (step["action"], step["requires_user"]))   # 科研通 steps stay manual
            profile = Path(temp) / "browser-profile" / "Default"
            profile.mkdir(parents=True)
            (profile / "Cookies").write_bytes(b"sqlite")
            self.assertEqual("prepare", pipeline.plan(NOW)[3]["action"])

    def test_successful_login_records_session_evidence(self):
        from browser_agent import BrowserAgent
        from unittest.mock import MagicMock
        with tempfile.TemporaryDirectory() as temp:
            store = Store(temp)
            agent = BrowserAgent(store, Accounts(temp))
            self.addCleanup(agent.thread.join, 0.1)
            self.addCleanup(agent.commands.put, ("close", None, None))
            agent.ensure = MagicMock()
            agent.page = MagicMock()
            agent.page.url = BASE + "/"
            agent.login(None, {"email": "a@b.c", "password": "pw"})
            self.assertTrue(store.snapshot()["account_metrics"]["default"]["last_login_time"])


class WorkflowTests(unittest.TestCase):
    def test_done_workflow(self):
        self.assertEqual("done", workflow_state(site(website_status="已完结")))
        self.assertEqual("done", workflow_state(site(website_status="已完结", pdf="downloads/x.pdf", verification=dict(status="review"))))
        self.assertEqual("done", workflow_state(local(done_via="oa", pdf="oa/x.pdf", status="checked", verification=dict(MATCH))))
        self.assertEqual("done", workflow_state(site(website_status="已完结", adopt_action=dict(status="confirmed"))))

    def test_queued_means_not_started(self):
        self.assertEqual("queued", workflow_state(local()))
        self.assertEqual("queued", workflow_state({"status": "ready"}))
        self.assertNotEqual("done", workflow_state(site(website_status="待确认")))

    def test_other_states_unchanged(self):
        self.assertEqual("downloading", workflow_state(local(status="downloading")))
        self.assertEqual("review", workflow_state(local(status="attention")))
        self.assertEqual("downloaded", workflow_state(local(pdf="a.pdf", verification=dict(MATCH))))
        self.assertEqual("rejected", workflow_state(local(status="rejected")))


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.server = make_server(0, self.temp.name, auto_screen=False)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = "http://127.0.0.1:%d" % self.server.server_port
        html = urlopen(self.base).read().decode()
        self.token = html.split("const TOKEN=")[1].split(";let state=")[0].strip('"')
        self.addCleanup(self.stop)

    def stop(self):
        self.server.pipeline.close()
        self.server.shutdown()
        self.server.server_close()
        self.server.agent.commands.put(("close", None, None))
        self.server.agent.thread.join(timeout=2)

    def post(self, route, payload=None, expect=200):
        request = Request(self.base + "/api/" + route, json.dumps(payload or {}).encode(), method="POST",
                          headers={"Content-Type": "application/json", "Origin": self.base, "X-Demo-Token": self.token})
        try:
            return json.load(urlopen(request))
        except HTTPError as exc:
            self.assertEqual(expect, exc.code)

    def state(self):
        return json.load(urlopen(self.base + "/api/state"))

    def test_state_workflow_done_for_completed_site_item(self):
        store = self.server.store
        done = store.add("Finished", "10.1/f", source="website", account_id="default", owned=True, assist_url=URL + "1", website_status="已完结")
        waiting = store.add("Waiting", "10.1/w", source="website", account_id="default", owned=True, assist_url=URL + "2", website_status="求助中")
        items = {i["id"]: i for i in self.state()["items"]}
        self.assertEqual(("done", "done"), (items[done]["workflow"], items[done]["stage"]))
        self.assertEqual("queued", items[waiting]["workflow"])     # submitted, not yet started downloading (unchanged semantics)

    def test_oa_accept_and_discard_endpoints(self):
        store = self.server.store
        first = store.add("One", "10.1/one", account_id="default", request_confirmed=True)
        second = store.add("Two", "10.1/two", account_id="default", request_confirmed=True)
        for item_id in (first, second):
            store.update(item_id, status="oa_review", pdf="oa/%s.pdf" % item_id, verification=dict(status="review"), oa_check=dict(status="downloaded"))
        self.assertEqual("oa_review", {i["id"]: i for i in self.state()["items"]}[first]["stage"])
        self.post("oa-accept", {"id": first}, 400)                                           # needs explicit confirmation
        self.post("oa-accept", {"id": first, "confirmed": True})
        self.assertEqual("done", {i["id"]: i for i in self.state()["items"]}[first]["workflow"])
        self.post("oa-discard", {"id": second})
        item = store.get(second)
        self.assertEqual(("skipped", ""), (item["oa_check"]["status"], item["pdf"]))
        self.post("oa-discard", {"id": second}, 400)
        self.post("oa-accept", {"id": "missing", "confirmed": True}, 400)

    def test_session_flag_uses_login_time_and_cookie(self):
        self.assertFalse(self.state()["pipeline"]["session"])
        self.server.store.update_account("default", last_login_time="2026-10-19T10:00:00")
        self.assertTrue(self.state()["pipeline"]["session"])


if __name__ == "__main__":
    unittest.main()
