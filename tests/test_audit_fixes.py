"""Regression tests for the 2026-10-20 audit fixes (H1-H6, M2-M15, L3, L6). Fake pages and servers only; nothing here
contacts ablesci.com."""
import json
import os
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import applog
import core
import intake
import oa_fetch
import publish_policy as pp
from accounts import Accounts
from ai_service import AIService
from app import make_server
from browser_agent import BrowserAgent, PreconditionError, TransientError, is_transient, OPEN_TIMEOUT_TEXT
from core import Store
from monitor import Monitor
from pipeline import Pipeline, stage_of
from tests import test_pipeline as tp
from tests.test_publish_flow import PublishBase, lookup_text, TITLE as PUB_TITLE, DOI as PUB_DOI

PDF_BYTES = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


def wait_until(check, seconds=5):
    end = time.time() + seconds
    while time.time() < end:
        if check():
            return True
        time.sleep(0.02)
    return check()


class AgentCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.accounts = Accounts(self.temp.name)
        self.agent = BrowserAgent(self.store, self.accounts)
        self.agent.ensure = MagicMock(side_effect=AssertionError("tests must never start a real browser"))
        self.addCleanup(self.close_agent)

    def close_agent(self):
        self.agent.stop.set()
        self.agent.commands.put(("close", None, None))
        self.agent.thread.join(timeout=3)

    def run_fake(self, func, item_id=None):
        self.agent.fake = func
        self.agent.enqueue("fake", item_id)
        self.assertTrue(wait_until(lambda: not self.agent.busy and self.agent.commands.empty()))
        time.sleep(0.05)


class H2PreconditionTests(AgentCase):
    def published(self):
        return self.store.add("Published paper", "10.1000/p", source="website", account_id="default", owned=True, status="published",
                              assist_url=tp.URL, website_status="求助中", publish_action=dict(status="confirmed"))

    def test_precondition_error_leaves_status_alone(self):
        item_id = self.published()

        def refuse(_):
            raise PreconditionError("没做任何事")
        self.run_fake(refuse, item_id)
        item = self.store.get(item_id)
        self.assertEqual("published", item["status"])
        self.assertEqual("submitted", stage_of(item))
        events = [e for e in self.store.snapshot()["events"] if e.get("item_id") == item_id]
        self.assertTrue(any(e["message"] == "没做任何事" and e.get("kind") == "error" for e in events))

    def test_ordinary_error_still_sets_attention(self):
        item_id = self.published()

        def fail(_):
            raise ValueError("真的出错了")
        self.run_fake(fail, item_id)
        self.assertEqual("attention", self.store.get(item_id)["status"])

    def test_real_precondition_checks_raise_the_subclass(self):
        item_id = self.published()
        has_pdf = self.store.add("Has a file", "10.1000/haspdf", source="website", account_id="default", owned=True, assist_url=tp.URL + "q",
                                 pdf="downloads/x/a.pdf", status="downloaded", website_status="待确认")
        loose = self.store.add("No link yet", "10.1000/nolink", source="import", account_id="default")
        for name, target in (("download", has_pdf), ("adopt", item_id), ("reject", item_id), ("publish", item_id), ("review", loose), ("prepare", "missing")):
            with self.subTest(name=name):
                with self.assertRaises(PreconditionError):
                    getattr(self.agent, name)(target)
        self.assertEqual("published", self.store.get(item_id)["status"])

    def test_precondition_error_is_a_value_error(self):
        self.assertTrue(issubclass(PreconditionError, ValueError))


class H3EnqueueTests(AgentCase):
    def test_marked_busy_at_enqueue_and_only_one_wins(self):
        gate = threading.Event()
        self.agent.slow = lambda _: gate.wait(5)
        results, start = [], threading.Barrier(12)

        def attempt():
            start.wait()
            try:
                self.agent.enqueue("slow")
                results.append("ok")
            except ValueError:
                results.append("busy")
        threads = [threading.Thread(target=attempt) for _ in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(1, results.count("ok"))
        self.assertEqual(11, results.count("busy"))
        self.assertTrue(self.agent.busy)
        gate.set()
        self.assertTrue(wait_until(lambda: not self.agent.busy))
        self.agent.enqueue("slow")      # free again after the command ended
        gate.set()

    def test_busy_before_the_worker_picks_the_command_up(self):
        agent = BrowserAgent.__new__(BrowserAgent)   # no worker thread at all
        import queue
        agent.commands, agent.busy, agent.stop = queue.Queue(), False, threading.Event()
        agent.enqueue("sync")
        self.assertTrue(agent.busy)
        with self.assertRaises(ValueError):
            agent.enqueue("sync")


class H4SurvivalTests(AgentCase):
    def test_store_failure_in_error_handling_does_not_kill_the_thread(self):
        item_id = self.store.add("Paper", "10.1000/x", source="website", account_id="default")

        def boom(_):
            raise ValueError("fail")
        with patch.object(Store, "update", side_effect=PermissionError("locked")), patch.object(Store, "event", side_effect=PermissionError("locked")):
            self.run_fake(boom, item_id)
        self.assertTrue(self.agent.thread.is_alive())
        self.assertFalse(self.agent.busy)
        done = []
        self.run_fake(lambda _: done.append(1), None)
        self.assertEqual([1], done)

    def test_watchdog_restarts_a_dead_thread(self):
        self.agent.commands.put(("close", None, None))
        self.agent.thread.join(timeout=3)
        self.assertFalse(self.agent.thread.is_alive())
        self.assertFalse(self.agent.revive())            # deliberate close: never revived
        self.agent._closed = False                       # as if the thread had died unexpectedly
        self.agent.busy = True
        self.assertTrue(self.agent.revive())
        self.assertTrue(self.agent.thread.is_alive())
        self.assertFalse(self.agent.busy)
        self.assertTrue(any("自动重启" in e["message"] for e in self.store.snapshot()["events"]))
        done = []
        self.run_fake(lambda _: done.append(1))
        self.assertEqual([1], done)

    def test_pipeline_and_monitor_ticks_run_the_watchdog(self):
        agent = MagicMock()
        agent.busy, agent.commands.empty.return_value = False, True
        pipeline = Pipeline(self.store, self.accounts, agent, start=False)
        pipeline.watchdog()
        agent.revive.assert_called_once()
        monitor = Monitor(self.store, self.accounts, agent, pipeline, start=False)
        agent.revive.reset_mock()
        monitor.tick()
        agent.revive.assert_called_once()

    def test_pipeline_watchdog_survives_a_failing_revive(self):
        agent = MagicMock()
        agent.revive.side_effect = RuntimeError("x")
        self.assertFalse(Pipeline(self.store, self.accounts, agent, start=False).watchdog())


class M5TransientTests(AgentCase):
    def test_transient_sync_failure_keeps_pipeline_on(self):
        self.store.update_preferences(pipeline_enabled=True)

        def timeout(_):
            raise TimeoutError("net")
        self.run_fake(timeout)
        self.assertTrue(self.store.snapshot()["preferences"]["pipeline_enabled"])

    def test_playwright_style_timeout_and_net_errors_are_transient(self):
        class TimeoutError(Exception):    # same name as playwright's class
            pass
        self.assertTrue(is_transient(TimeoutError("x")))
        self.assertTrue(is_transient(RuntimeError("Page.goto: net::ERR_CONNECTION_RESET at ...")))
        self.assertTrue(is_transient(TransientError("x")))
        self.assertFalse(is_transient(ValueError("账户会话已失效")))

    def test_non_transient_sync_failure_still_pauses(self):
        self.store.update_preferences(pipeline_enabled=True)

        def hard(_):
            raise ValueError("签到接口未正常返回")
        self.run_fake(hard)
        self.assertFalse(self.store.snapshot()["preferences"]["pipeline_enabled"])


class GotoTimeoutTests(unittest.TestCase):
    def test_goto_timeout_gets_a_plain_message(self):
        class TimeoutError(Exception):
            pass
        agent = BrowserAgent.__new__(BrowserAgent)
        agent.page = MagicMock()
        agent.page.goto.side_effect = TimeoutError("Timeout 45000ms exceeded")
        agent.ensure = lambda interactive=False: None
        agent.throttled = lambda: __import__("contextlib").nullcontext()
        with self.assertRaises(TransientError) as caught:
            agent.goto("/assist-my")
        self.assertEqual("科研通页面在 45 秒内未能打开，可能较为繁忙，请稍后重试", str(caught.exception))
        self.assertEqual(OPEN_TIMEOUT_TEXT, str(caught.exception))
        agent.page.goto.side_effect = RuntimeError("other")
        with self.assertRaises(RuntimeError):
            agent.goto("/x")


class ApiCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.start()

    def start(self):
        self.server = make_server(0, self.temp.name, start_monitor=False, auto_screen=False)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = "http://127.0.0.1:%d" % self.server.server_port
        self.token = urlopen(self.base).read().decode().split("const TOKEN=")[1].split(";let state=")[0].strip('"')
        self.store = self.server.store
        self.server.agent.enqueue = MagicMock()
        self.opened = []
        self.server.open_path = self.opened.append
        self.addCleanup(self.stop)

    def stop(self):
        self.server.pipeline.close()
        self.server.shutdown()
        self.server.server_close()
        self.server.agent.commands.put(("close", None, None))
        self.server.agent.thread.join(timeout=2)

    def post(self, route, body=None):
        request = Request(self.base + "/api/" + route, json.dumps(body or {}).encode(), method="POST",
                          headers={"Content-Type": "application/json", "Origin": self.base, "X-Demo-Token": self.token})
        return json.load(urlopen(request))

    def bad(self, route, body, code=400):
        with self.assertRaises(HTTPError) as caught:
            self.post(route, body)
        self.assertEqual(code, caught.exception.code)

    def state(self):
        return json.load(urlopen(self.base + "/api/state"))


class H1OpenTests(ApiCase):
    def test_open_refuses_non_pdf_but_allows_folder(self):
        folder = self.store.root / "downloads" / "x"
        folder.mkdir(parents=True)
        (folder / "evil.scr").write_bytes(b"MZ")
        (folder / "good.pdf").write_bytes(PDF_BYTES)
        bad = self.store.add("Bad", "10.1000/bad", source="website", account_id="default", pdf="downloads/x/evil.scr")
        good = self.store.add("Good", "10.1000/good", source="website", account_id="default", pdf="downloads/x/good.pdf")
        self.bad("open", {"id": bad, "target": "pdf"})
        self.assertEqual([], self.opened)
        self.post("open", {"id": bad, "target": "folder"})
        self.assertEqual([str(folder.resolve())], self.opened)
        self.post("open", {"id": good, "target": "pdf"})
        self.assertEqual(str((folder / "good.pdf").resolve()), self.opened[-1])


class H2H6ApiTests(ApiCase):
    def uncertain(self, status="uncertain"):
        return self.store.add("Maybe published", "10.1000/u", source="import", account_id="default", request_confirmed=True, status="attention",
                              publish_action=dict(status=status, point=10), publish_check=dict(kind="submitting"), publish_blocked=True)

    def test_ack_allows_confirmed_with_assist_url(self):
        item_id = self.store.add("Confirmed", "10.1000/c", source="website", account_id="default", owned=True, status="attention", assist_url=tp.URL + "z",
                                 website_status="求助中", publish_action=dict(status="confirmed"), publish_check=dict(kind="published", reason="x"))
        self.post("item/ack", {"id": item_id})
        item = self.store.get(item_id)
        self.assertEqual("published", item["status"])
        self.assertEqual("submitted", stage_of(item))

    def test_ack_still_refuses_uncertain_and_confirmed_without_url(self):
        self.bad("item/ack", {"id": self.uncertain()})
        other = self.store.add("C2", "10.1000/c2", source="import", account_id="default", status="attention",
                               publish_action=dict(status="confirmed"), publish_check=dict(kind="x"))
        self.bad("item/ack", {"id": other})

    def test_reset_publish_returns_item_to_queued(self):
        item_id = self.uncertain("attempted")
        self.assertEqual("attention", stage_of(self.store.get(item_id)))
        self.bad("item/reset-publish", {"id": item_id})                       # needs confirmed: true
        self.post("item/reset-publish", {"id": item_id, "confirmed": True})
        item = self.store.get(item_id)
        self.assertIsNone(item["publish_action"])
        self.assertEqual("queued", stage_of(item))
        self.bad("item/reset-publish", {"id": item_id, "confirmed": True})    # nothing uncertain any more

    def test_confirm_published_waits_for_sync_to_match(self):
        item_id = self.uncertain()
        self.bad("item/confirm-published", {"id": item_id})
        self.post("item/confirm-published", {"id": item_id, "confirmed": True})
        item = self.store.get(item_id)
        self.assertEqual("uncertain", item["publish_action"]["status"])
        self.assertTrue(item["publish_action"]["user_verified"])
        self.server.agent.enqueue.assert_called_with("sync")
        # the next sync matches it by title (BrowserAgent.match_published)
        agent = BrowserAgent.__new__(BrowserAgent)
        agent.store, agent.accounts = self.store, self.server.accounts
        self.assertEqual(item_id, agent.match_published("Maybe published", tp.URL))

    def test_state_buttons_present_in_ui(self):
        js = (Path(__file__).resolve().parent.parent / "app.js").read_text("utf-8")
        for text in ("核实：未发出，重置", "核实：已发出", "已在科研通处理", "重新尝试采纳 / 驳回", "urlHost"):
            self.assertIn(text, js)


class M14HandoverTests(ApiCase):
    def blocked(self):
        return self.store.add("Handover", "10.1000/h", source="website", account_id="default", owned=True, assist_url=tp.URL + "h", website_status="待确认",
                              pdf="downloads/h/a.pdf", download_file_url=tp.FILE, status="attention", handover=dict(kind="采纳", time="t"))

    def test_done_triggers_sync_and_marks_waiting(self):
        item_id = self.blocked()
        self.post("item/handover-done", {"id": item_id})
        self.server.agent.enqueue.assert_called_with("sync")
        item = self.store.get(item_id)
        self.assertTrue(item["handover"]["waiting_sync"])
        self.assertEqual("attention", item["status"])
        agent = BrowserAgent.__new__(BrowserAgent)
        agent.store = self.store
        agent.resolve_handover(item_id, "已完结")             # the next sync resolves it
        self.assertEqual("adopted", self.store.get(item_id)["status"])

    def test_retry_returns_to_awaiting_decision(self):
        item_id = self.blocked()
        self.post("item/handover-retry", {"id": item_id})
        item = self.store.get(item_id)
        self.assertEqual("awaiting_decision", stage_of(item))
        self.assertIsNone(item["handover"])
        self.bad("item/handover-retry", {"id": item_id})

    def test_rejects_items_without_handover(self):
        item_id = self.store.add("Plain", "10.1000/p", source="import", account_id="default", status="attention")
        self.bad("item/handover-done", {"id": item_id})


class M13UploadTests(ApiCase):
    def test_leftover_upload_is_replaced(self):
        import base64
        item_id = self.store.add("Upload me", "10.1000/up", source="import", account_id="default")
        folder = self.store.root / "uploads"
        folder.mkdir()
        (folder / (item_id + ".pdf")).write_bytes(b"%PDF-old leftover")
        data = base64.b64encode(PDF_BYTES).decode()
        with patch("app.verify_with_metadata", return_value=dict(status="review", reason="r", sha256="x")):
            self.post("upload", {"id": item_id, "base64": data})
        self.assertEqual(PDF_BYTES, (folder / (item_id + ".pdf")).read_bytes())
        self.assertTrue(self.store.get(item_id)["pdf"].endswith(item_id + ".pdf"))


class M7RestartNoticeTests(unittest.TestCase):
    def test_enabled_before_restart_leaves_a_pinned_notice(self):
        with tempfile.TemporaryDirectory() as temp:
            store = Store(temp)
            store.update_preferences(pipeline_enabled=True)
            Pipeline(store, Accounts(temp), MagicMock(), start=False)
            prefs = store.snapshot()["preferences"]
            self.assertFalse(prefs["pipeline_enabled"])
            self.assertTrue(prefs["pipeline_restart_notice"])

    def test_not_enabled_leaves_no_notice(self):
        with tempfile.TemporaryDirectory() as temp:
            store = Store(temp)
            Pipeline(store, Accounts(temp), MagicMock(), start=False)
            self.assertFalse(store.snapshot()["preferences"].get("pipeline_restart_notice"))


class M7ServerTests(ApiCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        Store(self.temp.name).update_preferences(pipeline_enabled=True)     # the previous run had the pipeline on
        self.start()

    def test_server_start_posts_event_and_state_flag_until_the_person_decides(self):
        state = self.state()
        self.assertTrue(state["pipeline"]["restart_notice"])
        self.assertFalse(state["pipeline"]["enabled"])
        events = [e for e in state["events"] if e.get("action") == "pipeline-enable"]
        self.assertEqual(1, len(events))
        self.assertIn("自动流程已在重启后关闭", events[0]["message"])
        self.assertTrue(any("自动流程已在重启后关闭" in e["message"] for e in state["events"] if e.get("kind") == "notify"))
        self.post("pipeline", {"enabled": True})
        self.assertFalse(self.state()["pipeline"]["restart_notice"])
        js = (Path(__file__).resolve().parent.parent / "app.js").read_text("utf-8")
        self.assertIn("自动流程已在重启后关闭", js)
        self.assertIn("重新开启", js)


class M2AiKeyTests(unittest.TestCase):
    def make(self):
        ai = AIService()
        ai.configure(True, "sk-test-key-12345678", "m1", True, provider="custom", base_url="https://api.one.example/v1", storage="memory")
        return ai

    def test_stored_key_not_sent_to_a_different_base_url(self):
        ai = self.make()
        with patch.object(AIService, "_send") as send:
            with self.assertRaises(ValueError) as caught:
                ai.test_connection("", "custom", "https://api.two.example/v1", "m1")
            self.assertIn("重新填写", str(caught.exception))
            send.assert_not_called()
            send.return_value = {"data": [{"id": "m1"}]}
            self.assertTrue(ai.test_connection("sk-another-key-87654321", "custom", "https://api.two.example/v1", "m1")["ok"])
            self.assertEqual("Bearer sk-another-key-87654321", send.call_args.args[0].get_header("Authorization"))

    def test_same_base_url_still_uses_stored_key(self):
        ai = self.make()
        with patch.object(AIService, "_send", return_value={"data": [{"id": "m1"}]}) as send:
            self.assertTrue(ai.test_connection("", "custom", "https://api.one.example/v1", "m1")["ok"])
            self.assertEqual("Bearer sk-test-key-12345678", send.call_args.args[0].get_header("Authorization"))

    def test_configure_with_new_base_requires_new_key(self):
        ai = self.make()
        with self.assertRaises(ValueError):
            ai.configure(True, "", "m1", True, provider="custom", base_url="https://api.two.example/v1", storage="memory")


class M3M4StoreTests(unittest.TestCase):
    def test_save_holds_the_store_lock(self):
        with tempfile.TemporaryDirectory() as temp:
            store = Store(temp)
            owned = []
            original = core.write_json_durable
            with patch.object(core, "write_json_durable", lambda *a, **k: (owned.append(store.lock._is_owned()), original(*a, **k))):
                store.save()
            self.assertEqual([True], owned)

    def test_monitor_state_changes_go_through_the_store(self):
        with tempfile.TemporaryDirectory() as temp:
            store = Store(temp)
            monitor = Monitor(store, Accounts(temp), MagicMock(), None, start=False)
            with patch.object(Store, "mutate", autospec=True, side_effect=Store.mutate) as mutate:
                monitor.persist()
                monitor.after_sync(False)
            self.assertGreaterEqual(mutate.call_count, 2)

    def test_durable_write_keeps_rolling_backup(self):
        with tempfile.TemporaryDirectory() as temp:
            store = Store(temp)
            store.add("One", "10.1000/1")
            store.add("Two", "10.1000/2")
            backup = Path(temp) / "queue.json.bak"
            self.assertTrue(backup.exists())
            self.assertEqual(1, len([i for i in json.loads(backup.read_text("utf-8"))["items"] if i["title"] == "One"]))
            self.assertEqual(2, len(json.loads((Path(temp) / "queue.json").read_text("utf-8"))["items"]))

    def test_corrupt_queue_falls_back_to_backup_with_event(self):
        with tempfile.TemporaryDirectory() as temp:
            store = Store(temp)
            store.add("One", "10.1000/1")
            store.add("Two", "10.1000/2")
            (Path(temp) / "queue.json").write_text("{ this is not json", "utf-8")
            again = Store(temp)
            self.assertEqual(["One"], [i["title"] for i in again.snapshot()["items"] if i["title"] == "One"])
            self.assertTrue(any("备份" in e["message"] for e in again.snapshot()["events"]))
            json.loads((Path(temp) / "queue.json").read_text("utf-8"))      # rewritten as valid JSON
            self.assertTrue(any("warn" in line for line in (next((Path(temp) / "logs").glob("kytdl-*.log"))).read_text("utf-8").splitlines()))

    def test_corrupt_without_backup_still_raises(self):
        with tempfile.TemporaryDirectory() as temp:
            (Path(temp) / "queue.json").write_text("nope", "utf-8")
            with self.assertRaises(ValueError):
                Store(temp)

    def test_damaged_file_never_overwrites_the_good_backup(self):
        with tempfile.TemporaryDirectory() as temp:
            store = Store(temp)
            store.add("One", "10.1000/1")
            store.add("Two", "10.1000/2")
            good = (Path(temp) / "queue.json.bak").read_text("utf-8")
            (Path(temp) / "queue.json").write_text("garbage", "utf-8")
            store.save()
            self.assertEqual(good, (Path(temp) / "queue.json.bak").read_text("utf-8"))

    def test_recovery_tolerates_missing_status_and_marks_in_flight_actions(self):
        with tempfile.TemporaryDirectory() as temp:
            data = {"items": [
                {"id": "nostatus", "title": "A", "doi": "", "source": "import"},
                {"id": "pub", "title": "B", "doi": "", "source": "import", "status": "publishing"},
                {"id": "att", "title": "C", "doi": "", "source": "import", "status": "queued", "publish_action": {"status": "attempted"}},
                {"id": "dl", "title": "D", "doi": "", "source": "website", "status": "downloading"},
            ], "events": []}
            (Path(temp) / "queue.json").write_text(json.dumps(data), "utf-8")
            store = Store(temp)
            self.assertEqual("ready", store.get("nostatus")["status"])
            for item_id in ("pub", "att", "dl"):
                self.assertEqual("attention", store.get(item_id)["status"], item_id)
            self.assertEqual("uncertain", store.get("att")["publish_action"]["status"])      # never retried automatically
            self.assertEqual("attention", stage_of(store.get("pub")))

    def test_accounts_json_gets_the_same_treatment(self):
        with tempfile.TemporaryDirectory() as temp:
            accounts = Accounts(temp)
            accounts.add("第二个账户")
            accounts.add("第三个账户")
            self.assertTrue((Path(temp) / "accounts.json.bak").exists())
            (Path(temp) / "accounts.json").write_text("{", "utf-8")
            again = Accounts(temp)
            self.assertTrue(again.recovered)
            self.assertEqual(2, len(again.data["accounts"]))     # the last good version (before the third account)

    def test_fsync_is_called(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(os, "fsync") as fsync:
                core.write_json_durable(Path(temp) / "x.json", {"a": 1})
            fsync.assert_called_once()


class M11DedupeTests(unittest.TestCase):
    def test_same_title_different_dois_is_not_a_duplicate(self):
        with tempfile.TemporaryDirectory() as temp:
            store = Store(temp)
            first = store.add("Editorial Note", "10.1000/aaa")
            second = store.add("Editorial note", "10.1000/bbb")
            self.assertNotEqual(first, second)

    def test_same_title_with_a_missing_doi_still_dedupes(self):
        with tempfile.TemporaryDirectory() as temp:
            store = Store(temp)
            first = store.add("Editorial Note", "10.1000/aaa")
            self.assertEqual(first, store.add("editorial note", ""))
            self.assertEqual(first, store.add("Another spelling", "10.1000/aaa"))
            plain = store.add("Only title", "")
            self.assertEqual(plain, store.add("Only Title", "10.1000/ccc"))


class M10BudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.ids = [self.store.add("Paper %d" % n, "10.1000/n%d" % n, account_id="default", oa_check=dict(status="not_oa")) for n in range(3)]
        intake.create_batch(self.store, "b1", "default", self.ids)
        intake.mark(self.store, "b1", "default", screening_done_at="2026-10-20T00:00:00+08:00")

    def confirm(self, rewards, points, **prefs):
        self.store.update_preferences(**prefs)
        return intake.confirm(self.store, "b1", self.ids, dict(zip(self.ids, rewards)), points, True, "default")

    def test_total_over_available_points_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            self.confirm([50, 50, 50], 120)
        self.assertIn("合计 150", str(caught.exception))
        self.assertFalse(any(i.get("request_confirmed") is True for i in self.store.snapshot()["items"]))

    def test_reserve_is_subtracted(self):
        with self.assertRaises(ValueError):
            self.confirm([50, 50], 120, publish_reserve_points=30)
        self.assertEqual(3, self.confirm([10, 10, 10], 120, publish_reserve_points=30))

    def test_unknown_points_do_not_apply_the_budget(self):
        self.assertEqual(3, self.confirm([100, 100, 100], None))


class M10PublishReserveTests(PublishBase):
    prefs = dict(publish_reserve_points=95)

    def test_base_reward_must_respect_reserve(self):
        item_id = self.item(request_reward=10)
        self.script(response=dict(code=0))
        message = None
        try:
            self.agent.publish(item_id)
        except ValueError as exc:
            message = str(exc)
        self.assertIn("保留", message)
        self.assertEqual(0, self.page.submit.clicks)
        self.assertEqual("reserve_points", self.store.get(item_id)["publish_check"]["kind"])


class M15OaFetchTests(unittest.TestCase):
    def test_private_urls_detected(self):
        for url in ("http://127.0.0.1/x.pdf", "http://localhost:8769/api", "http://192.168.1.2/a", "http://10.0.0.5/a", "http://[::1]/a",
                    "http://169.254.169.254/latest", "http://printer.local/a", "http://0.0.0.0/"):
            self.assertTrue(oa_fetch.is_private_url(url), url)
        for url in ("https://journals.example.org/a.pdf", "https://8.8.8.8/x", "https://doi.org/10.1000/x"):
            self.assertFalse(oa_fetch.is_private_url(url), url)

    def test_request_refuses_private_addresses_without_connecting(self):
        with patch("oa_fetch.build_opener") as opener:
            with self.assertRaises(oa_fetch.OAError):
                oa_fetch._request("http://127.0.0.1:8769/api/state")
            opener.assert_not_called()

    def test_redirect_to_private_address_refused(self):
        handler = oa_fetch._Redirects()
        with self.assertRaises(oa_fetch.OAError):
            handler.redirect_request(MagicMock(), None, 302, "Found", {}, "http://10.0.0.1/secret")

    def test_total_deadline_while_reading_the_body(self):
        class Slow:
            headers, status = {}, 200

            def read(self, n):
                time.sleep(0.01)
                return b"x"

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        class Opener:
            def open(self, request, timeout=None):
                return Slow()
        with patch("oa_fetch.build_opener", lambda *h: Opener()), patch.object(oa_fetch, "TOTAL_DEADLINE", 0.1):
            with self.assertRaises(oa_fetch.OAError) as caught:
                oa_fetch._request("https://slow.example.org/x.pdf")
        self.assertIn("deadline", str(caught.exception))


class L3RotationTests(unittest.TestCase):
    def test_server_logs_and_screenshots_are_rotated(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            logs, shots = root / "logs", root / "screenshots"
            logs.mkdir()
            shots.mkdir()
            old = time.time() - 20 * 86400
            for name in ("server-20260901.stdout.log", "server-20260901.stderr.log"):
                (logs / name).write_text("x")
                os.utime(logs / name, (old, old))
            (logs / "server-today.stdout.log").write_text("x")
            (logs / "notes.txt").write_text("keep")
            os.utime(logs / "notes.txt", (old, old))
            now = time.time()
            for n in range(205):
                path = shots / ("s%03d.png" % n)
                path.write_bytes(b"p")
                os.utime(path, (now - 1000 + n, now - 1000 + n))
            (shots / "browser-last.png").write_bytes(b"p")
            os.utime(shots / "browser-last.png", (old, old))
            removed = applog.rotate_server_logs(logs)
            self.assertEqual(["server-20260901.stderr.log", "server-20260901.stdout.log"], sorted(removed))
            self.assertTrue((logs / "server-today.stdout.log").exists())
            self.assertTrue((logs / "notes.txt").exists())
            applog.trim_screenshots(shots)
            names = sorted(p.name for p in shots.iterdir())
            self.assertEqual(201, len(names))
            self.assertIn("browser-last.png", names)
            self.assertNotIn("s000.png", names)
            self.assertIn("s204.png", names)

    def test_rotation_runs_from_the_applog_daily_hook(self):
        with tempfile.TemporaryDirectory() as temp:
            logs = Path(temp) / "logs"
            logs.mkdir()
            stale = logs / "server-old.stdout.log"
            stale.write_text("x")
            old = time.time() - 30 * 86400
            os.utime(stale, (old, old))
            applog.AppLog(logs).write("t", "a", "ok")
            self.assertFalse(stale.exists())


if __name__ == "__main__":
    unittest.main()
