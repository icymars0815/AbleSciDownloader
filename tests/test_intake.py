import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock
from urllib.request import Request, urlopen
from urllib.error import HTTPError

import deletion
import intake
from accounts import Accounts
from app import make_server
from core import Store
from notifications import Notifier
from pipeline import DONE, FULFILLED, WAITING, GATE_TEXT, Pipeline, next_step, stage_of, gate
from rejection_policy import REJECTION_RULES_VERSION  # noqa: F401  (import check only)

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)
AUTO = dict(pipeline_auto_publish=True, pipeline_rules_version="2026-10-19-p1")


def imp(name, **fields):
    base = dict(id=name, title="T " + name, doi="10.1234/" + name, source="import", owned=False, status="ready",
                created="2026-10-19T10:00:00", account_id="default", assist_url="", website_status="")
    base.update(fields)
    return base


def done_site(name, **fields):
    base = dict(id=name, title="S " + name, doi="10.1234/" + name, source="website", owned=True, status="ready", created="2026-10-19T09:00:00",
                account_id="default", assist_url="https://www.ablesci.com/assist/detail?id=" + name, website_status="已完结")
    base.update(fields)
    return base


class StatusConstants(unittest.TestCase):
    def test_constants(self):
        self.assertEqual(("求助中",), WAITING)
        self.assertEqual(("待确认",), FULFILLED)
        self.assertIn("已关闭", DONE)
        self.assertEqual("attention", stage_of(imp("x", source="website", owned=True, assist_url="u", website_status="某个新状态")))

    def test_gate_text_is_fixed_rule(self):
        blocked = gate([dict(id="d", title="Doc", source="website", owned=True, assist_url="u", website_status="待确认")], 100)
        self.assertTrue(blocked["blocked"])
        self.assertIn(GATE_TEXT, blocked["reason"])


class Gating(unittest.TestCase):
    def test_unconfirmed_never_published_even_with_auto_publish(self):
        items = [imp("a", oa_check=dict(status="not_oa"), batch_id="b1", request_confirmed=False)]
        step = next_step(items, {"points": 100}, AUTO, NOW)
        self.assertNotIn(step["action"], ("publish", "prepare"))
        self.assertEqual("wait_user", step["action"])
        step = next_step([dict(items[0], request_confirmed=True)], {"points": 100}, AUTO, NOW)
        self.assertEqual("publish", step["action"])
        no_login = next_step(items, {}, AUTO, NOW, session=False)
        self.assertNotIn(no_login["action"], ("publish", "prepare"))

    def test_execute_refuses_unconfirmed(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(tmp)
            agent = MagicMock()
            pipeline = Pipeline(store, MagicMock(), agent, start=False)
            iid = store.add("P", "10.1234/p", account_id="default", request_confirmed=False)
            step = dict(action="publish", item_id=iid, label="", requires_user=False, reason="")
            self.assertFalse(pipeline.execute(step))
            agent.enqueue.assert_not_called()
            store.update(iid, request_confirmed=True)
            self.assertTrue(pipeline.execute(step))


class Rewards(unittest.TestCase):
    def test_validation(self):
        self.assertEqual(10, intake.validate_reward(10, 500))
        self.assertEqual(150, intake.validate_reward(150, 500))
        self.assertEqual(200, intake.validate_reward(200, 500))
        for bad in (9, 15, 37, 75, 201, 500, True, 12.5, "20", None):
            with self.assertRaises(ValueError):
                intake.validate_reward(bad, 500)
        with self.assertRaises(ValueError):
            intake.validate_reward(10, 5)         # fewer than 10 points
        self.assertEqual(200, intake.validate_reward(200, None))   # unknown balance: cap 200
        with self.assertRaises(ValueError):
            intake.validate_reward(201, None)
        with self.assertRaises(ValueError):
            intake.validate_reward(100, 80)       # a tier above the current points is refused
        self.assertEqual(80, intake.validate_reward(80, 80))
        self.assertEqual([10, 20, 30, 50], intake.reward_tiers(50))

    def test_default_reward_clamped(self):
        self.assertEqual(10, intake.default_reward({}, 100))
        self.assertEqual(30, intake.default_reward({"default_reward": 80}, 30))
        self.assertEqual(50, intake.default_reward({"default_reward": 75}, 500))   # a stored off-tier value snaps down to a tier
        self.assertEqual((10, 20, 30, 50, 80, 100, 150, 200), intake.REWARD_STEPS)


class Flow(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.addCleanup(self.temp.cleanup)

    def make_batch(self):
        ids = [self.store.add("A", "10.1234/a", account_id="default", batch_id="B1", request_confirmed=False),
               self.store.add("B", "10.1234/b", account_id="default", batch_id="B1", request_confirmed=False),
               self.store.add("C", "", account_id="default", batch_id="B1", request_confirmed=False),
               self.store.add("D", "10.1234/d", account_id="default", batch_id="B1", request_confirmed=False)]
        intake.create_batch(self.store, "B1", "default", ids)
        return ids

    def test_screening_and_table(self):
        a, b, c, d = self.make_batch()
        seen = []
        for _ in range(10):
            nxt = intake.next_screening(self.store)
            if not nxt:
                break
            seen.append(nxt)
            self.store.update(nxt, oa_check=dict(status={a: "not_oa", b: "downloaded", d: "failed"}[nxt]))
            if nxt == b:
                self.store.update(b, pdf="oa/b.pdf", done_via="oa", status="checked")
        self.assertEqual(sorted([a, b, d]), sorted(seen))
        batch = intake.public_batches(self.store.snapshot(), "default", {}, 100)[0]
        self.assertEqual("awaiting_confirmation", batch["phase"])
        rows = {r["id"]: r for r in batch["items"]}
        self.assertTrue(rows[a]["checked"] and rows[d]["checked"])
        self.assertFalse(rows[b]["checked"] or rows[c]["checked"])
        self.assertEqual("no_doi", rows[c]["code"])
        self.assertFalse(rows[c]["selectable"])
        self.assertEqual("oa_done", rows[b]["code"])
        self.assertEqual(10, rows[a]["reward"])

    def test_interrupted_screening_is_retried_before_batch_finishes(self):
        a, b, c, d = self.make_batch()
        self.assertEqual(a, intake.next_screening(self.store))
        # Simulate a restart after the selection was persisted but before any OA result was saved.
        reopened = Store(self.temp.name)
        self.assertEqual(a, intake.next_screening(reopened))
        self.assertFalse(intake.find_batch(reopened, "B1").get("screening_done_at"))
        reopened.update(a, oa_check={"status": "failed"})
        self.assertEqual(b, intake.next_screening(reopened))

    def test_downloaded_oa_pending_review_is_not_marked_complete(self):
        iid = self.store.add("Open paper", "10.1234/open", account_id="default", batch_id="B2")
        intake.create_batch(self.store, "B2", "default", [iid])
        self.store.update(iid, status="oa_review", pdf="oa/open.pdf", oa_check={"status": "downloaded"},
                          verification={"status": "review"})
        intake.next_screening(self.store)
        batch = intake.public_batches(self.store.snapshot(), "default", {}, 100)[0]
        self.assertEqual("awaiting_confirmation", batch["phase"])
        self.assertEqual("免费全文待核对 1 条", batch["steps"][2]["label"])
        self.assertEqual(0, batch["counts"]["finished"])
        another = self.store.add("Site paper", "10.1234/site", account_id="default", batch_id="B2", source="website",
                                 owned=True, website_status="已完结", request_confirmed=True, request_batch_id="B2")
        self.store.mutate(lambda data: next(b for b in data["batches"] if b["id"] == "B2")["item_ids"].append(another))
        self.assertEqual("awaiting_confirmation", intake.public_batches(self.store.snapshot(), "default", {}, 100)[0]["phase"])
        self.store.update(iid, done_via="oa", status="checked")
        self.assertEqual("done", intake.public_batches(self.store.snapshot(), "default", {}, 100)[0]["phase"])

    def test_mcp_batch_keeps_status_without_requesting_user_popup(self):
        iid = self.store.add("No OA", "10.1234/nooa", account_id="default", batch_id="MCP")
        intake.create_batch(self.store, "MCP", "default", [iid], origin="mcp")
        self.store.update(iid, oa_check={"status": "not_oa"})
        notifier = MagicMock()
        intake.next_screening(self.store, notifier=notifier)
        notifier.notify.assert_not_called()
        self.assertEqual("awaiting_confirmation", intake.public_batches(self.store.snapshot(), "default", {}, 100)[0]["phase"])

    def test_confirm_rules(self):
        a, b, c, d = self.make_batch()
        for i in (a, b, d):
            self.store.update(i, oa_check=dict(status="not_oa"))
        with self.assertRaises(ValueError):
            intake.confirm(self.store, "B1", [a], {}, 100, True, "default")   # screening not finished yet
        intake.next_screening(self.store)   # every candidate already has a result: the batch finishes
        self.assertTrue(self.store.data["batches"][0]["screening_done_at"])
        with self.assertRaises(ValueError):
            intake.confirm(self.store, "B1", [a], {}, 100, False, "default")   # no login
        with self.assertRaises(ValueError):
            intake.confirm(self.store, "B1", [c], {}, 100, True, "default")    # no DOI
        with self.assertRaises(ValueError):
            intake.confirm(self.store, "B1", [a], {a: 101}, 100, True, "default")
        with self.assertRaises(ValueError):
            intake.confirm(self.store, "B1", [], {}, 100, True, "default")
        self.assertFalse(self.store.get(a).get("request_confirmed"))
        self.assertEqual(1, intake.confirm(self.store, "B1", [a], {a: 30}, 100, True, "default"))
        item = self.store.get(a)
        self.assertTrue(item["request_confirmed"])
        self.assertEqual(30, item["request_reward"])
        self.assertEqual("B1", item["request_batch_id"])
        self.assertFalse(self.store.get(b).get("request_confirmed"))
        self.assertEqual("requesting", intake.public_batches(self.store.snapshot(), "default", {}, 100)[0]["phase"])


class Deletion(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.sent = []
        self.notifier = Notifier(self.store, sink=lambda t, m: self.sent.append(m))

    def add(self, **fields):
        iid = self.store.add("Doc", "10.1234/x", source="website", account_id="default", owned=True,
                             assist_url="https://www.ablesci.com/assist/detail?id=Q1", website_status="已完结", **fields)
        return iid

    def test_estimated_then_deleted(self):
        iid = self.add(pdf="downloads/x.pdf")
        t0 = datetime(2026, 10, 19, 8, 0, tzinfo=timezone.utc)
        deletion.sweep(self.store, self.notifier, t0)
        item = self.store.get(iid)
        self.assertTrue(item["completed_seen_at"])
        view = deletion.view(item)
        self.assertEqual("scheduled", view["state"])
        self.assertTrue(view["estimated"])
        self.assertIn("估算", view["text"])
        self.assertIn("预计删除时间：", view["text"])
        deletion.sweep(self.store, self.notifier, t0 + timedelta(hours=23, minutes=59))
        self.assertFalse(self.store.get(iid).get("site_file_deleted"))
        deletion.sweep(self.store, self.notifier, t0 + timedelta(hours=24, seconds=1))
        done = self.store.get(iid)
        self.assertTrue(done["site_file_deleted"])
        self.assertIn("本地副本保留", deletion.view(done)["text"])

    def test_explicit_completion_time_not_estimate(self):
        iid = self.add(completed_at="2026-10-19T08:00:00+00:00")
        view = deletion.view(self.store.get(iid))
        self.assertFalse(view["estimated"])
        self.assertNotIn("估算", view["text"])
        deletion.sweep(self.store, self.notifier, datetime(2026, 10, 20, 8, 30, tzinfo=timezone.utc))
        self.assertTrue(self.store.get(iid)["site_file_deleted"])
        self.assertNotIn("本地副本保留", deletion.view(self.store.get(iid))["text"])

    def test_notification_two_hours_before_only_when_not_downloaded(self):
        iid = self.add(completed_at="2026-10-19T08:00:00+00:00")
        deletion.sweep(self.store, self.notifier, datetime(2026, 10, 20, 5, 59, tzinfo=timezone.utc))
        self.assertEqual([], self.sent)
        deletion.sweep(self.store, self.notifier, datetime(2026, 10, 20, 6, 1, tzinfo=timezone.utc))
        self.assertEqual(1, len(self.sent))
        deletion.sweep(self.store, self.notifier, datetime(2026, 10, 20, 6, 30, tzinfo=timezone.utc))
        self.assertEqual(1, len(self.sent))      # de-duplicated
        events = [e for e in self.store.snapshot()["events"] if e.get("kind") == "notify"]
        self.assertEqual(iid, events[-1]["item_id"])
        other = Store(tempfile.mkdtemp())
        notifier = Notifier(other, sink=lambda t, m: self.sent.append(m))
        other.add("Doc2", "10.1234/y", source="website", account_id="default", owned=True, website_status="已完结",
                  completed_at="2026-10-19T08:00:00+00:00", pdf="downloads/y.pdf")
        deletion.sweep(other, notifier, datetime(2026, 10, 20, 7, 0, tzinfo=timezone.utc))
        self.assertEqual(1, len(self.sent))      # already downloaded: no warning

    def test_pending_and_closed(self):
        pend = self.store.add("P", "10.1234/p", source="website", account_id="default", owned=True, assist_url="u", website_status="待确认")
        self.assertEqual("完结后 24 小时删除", deletion.view(self.store.get(pend))["text"])
        closed = self.store.add("C", "10.1234/c", source="website", account_id="default", owned=True, assist_url="u2", website_status="已关闭")
        deletion.sweep(self.store, self.notifier, NOW)
        self.assertEqual("none", deletion.view(self.store.get(closed))["state"])
        self.assertNotIn("site_file_deleted", self.store.get(closed))


class Api(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.server = make_server(0, self.temp.name, auto_screen=False, start_monitor=False)
        self.port = self.server.server_port
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.close)
        import re
        html = urlopen(Request("http://127.0.0.1:%d/" % self.port)).read().decode()
        self.token = json.loads(re.search(r"const TOKEN=(.*?);", html).group(1))

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.server.pipeline.close()
        self.server.monitor.close()
        self.temp.cleanup()

    def post(self, path, body):
        req = Request("http://127.0.0.1:%d/api/%s" % (self.port, path), json.dumps(body).encode(), method="POST",
                      headers={"Content-Type": "application/json", "X-Demo-Token": self.token, "Origin": "http://127.0.0.1:%d" % self.port})
        try:
            with urlopen(req) as r:
                return r.status, json.loads(r.read())
        except HTTPError as e:
            return e.code, json.loads(e.read())

    def state(self):
        return json.loads(urlopen("http://127.0.0.1:%d/api/state" % self.port).read())

    def test_import_creates_batch(self):
        self.post("import", {"text": "10.1234/one\tPaper one\n10.1234/two\tPaper two"})
        st = self.state()
        self.assertEqual(1, len(st["batches"]))
        self.assertEqual(2, st["batches"][0]["total"])
        self.assertEqual("screening", st["batches"][0]["phase"])
        self.assertTrue(all(i["request_confirmed"] is False for i in st["items"]))
        self.assertTrue(any(e.get("kind") == "intake" for e in st["events"]))
        self.post("import", {"text": "10.1234/one\tPaper one"})     # duplicate only: no new batch
        self.assertEqual(1, len(self.state()["batches"]))

    def test_confirm_endpoint_needs_login_and_validates(self):
        self.post("import", {"text": "10.1234/one\tPaper one"})
        st = self.state()
        bid = st["batches"][0]["id"]
        iid = st["items"][-1]["id"]
        self.server.pipeline.screen_step = lambda: None
        store = self.server.store
        store.update(iid, oa_check=dict(status="not_oa"))
        intake.next_screening(store)
        intake.next_screening(store)
        code, body = self.post("batch/confirm", {"batch_id": bid, "item_ids": [iid]})
        self.assertEqual(400, code)                      # no 科研通 login evidence
        self.assertIn("登录", body["error"])
        store.update_account("default", points=100)
        code, _ = self.post("batch/confirm", {"batch_id": bid, "item_ids": [iid], "rewards": {iid: 5}})
        self.assertEqual(400, code)
        code, _ = self.post("batch/confirm", {"batch_id": bid, "item_ids": [iid], "rewards": {iid: 30}})
        self.assertEqual(200, code)
        st = self.state()
        self.assertTrue(next(i for i in st["items"] if i["id"] == iid)["request_confirmed"])
        self.assertEqual("requesting", st["batches"][0]["phase"])
        self.assertEqual(30, next(i for i in st["items"] if i["id"] == iid)["request_reward"])

    def test_defer_and_seen(self):
        self.post("import", {"text": "10.1234/one\tPaper one"})
        bid = self.state()["batches"][0]["id"]
        self.assertEqual(200, self.post("batch/defer", {"batch_id": bid})[0])
        self.assertTrue(self.state()["batches"][0]["deferred"])
        self.assertEqual(400, self.post("batch/seen", {"batch_id": "nope"})[0])

    def test_preferences_reward_and_home_notice(self):
        self.server.store.update_account("default", points=120)
        self.assertEqual(400, self.post("preferences", {"default_reward": 9})[0])
        self.assertEqual(400, self.post("preferences", {"default_reward": 121})[0])
        self.assertEqual(400, self.post("preferences", {"default_reward": 75})[0])      # not an official tier
        self.assertEqual(400, self.post("preferences", {"default_reward": 150})[0])     # tier above the current points
        self.assertEqual(200, self.post("preferences", {"default_reward": 80})[0])
        st = self.state()
        self.assertEqual(80, st["preferences"]["default_reward"])
        self.assertEqual(80, st["reward"]["default"])
        self.assertEqual(120, st["reward"]["cap"])
        self.assertFalse(st["preferences"].get("home_notice_hidden", False))
        self.assertEqual(400, self.post("preferences", {"home_notice_hidden": "yes"})[0])
        self.assertEqual(200, self.post("preferences", {"home_notice_hidden": True})[0])
        self.assertTrue(self.state()["preferences"]["home_notice_hidden"])
        self.assertEqual(200, self.post("preferences", {"home_notice_hidden": False})[0])
        self.assertFalse(self.state()["preferences"]["home_notice_hidden"])
        self.assertIn("version", self.state()["about"])

    def test_deleted_download_refused_and_state_flag(self):
        store = self.server.store
        iid = store.add("Old", "10.1234/old", source="website", account_id="default", owned=True, assist_url="https://www.ablesci.com/assist/detail?id=Z9",
                        website_status="已完结", completed_at="2020-01-01T00:00:00+00:00")
        item = next(i for i in self.state()["items"] if i["id"] == iid)
        self.assertTrue(item["site_file_deleted"])
        self.assertEqual("deleted", item["deletion"]["state"])
        code, body = self.post("browser", {"action": "download", "id": iid})
        self.assertEqual(400, code)
        self.assertIn("已删除", body["error"])


if __name__ == "__main__":
    unittest.main()
