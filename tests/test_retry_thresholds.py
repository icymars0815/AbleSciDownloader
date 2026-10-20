"""Automatic download retry, backoff and human takeover thresholds. Everything mocked; no browser, no network."""
import json
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock
from urllib.request import Request, urlopen

import browser_agent
import download_policy as dp
from app import make_server
from browser_agent import BASE, RetryScheduled, SessionExpired
from core import Store
from download_policy import RULES_VERSION, validate_download_settings, download_settings
from monitor import Monitor
from notifications import Notifier
from pipeline import next_step, retry_waiting
from task_states import workflow_state
from tests import test_download_settings as tds
from tests import test_monitor as tm
from tests.test_pipeline import fulfilled, NOW


class Harness(tds.AgentFlowTests):
    """Reuses the mocked download harness; its inherited tests are disabled here (they run in their own module)."""

    def setUp(self):
        super().setUp()
        self.agent.ensure = MagicMock(side_effect=AssertionError("tests must never start a real browser"))
        self.agent.notifier = MagicMock()
        self.store.update_account("default", points=100)
        self.agent.read_points = MagicMock(return_value=100)

    def item_(self):
        return self.store.get(self.item)

    def fail_run(self, outcomes, expect=RetryScheduled):
        self.outcomes = list(outcomes)
        with self.assertRaises(expect) as caught:
            self.agent.download(self.item)
        return str(caught.exception)


for _name in dir(tds.AgentFlowTests):
    if _name.startswith("test"):
        setattr(Harness, _name, None)


class SettingsTests(unittest.TestCase):
    def test_defaults_and_ranges(self):
        s = download_settings({})
        self.assertEqual((5, 5, 10), (s["normal_max_failures"], s["highspeed_max_tries"], s["auto_retry_max"]))
        for key, low, high in (("normal_max_failures", 1, 20), ("highspeed_max_tries", 1, 20), ("auto_retry_max", 0, 50)):
            self.assertEqual({"download_" + key: low}, validate_download_settings({key: low}))
            self.assertEqual({"download_" + key: high}, validate_download_settings({key: high}))
            for bad in (low - 1, high + 1, "5", 5.5, True, None):
                with self.assertRaises(ValueError, msg=(key, bad)):
                    validate_download_settings({key: bad})
        self.assertEqual(3, download_settings({"download_normal_max_failures": 3})["normal_max_failures"])
        self.assertEqual(5, download_settings({"download_normal_max_failures": 99})["normal_max_failures"])

    def test_backoff_sequence(self):
        self.assertEqual([1, 3, 5, 10, 10, 10], [dp.backoff_minutes(n) for n in range(1, 7)])

    def test_takeover_reason_pure(self):
        s = download_settings({})
        self.assertEqual("", dp.takeover_reason(4, 0, 3, s, False, None))
        self.assertIn("普通线路累计失败 5 次（上限 5 次）", dp.takeover_reason(5, 0, 1, s, False))
        self.assertEqual("", dp.takeover_reason(9, 2, 1, s, True))
        self.assertIn("高速通道已失败 5 次", dp.takeover_reason(9, 5, 1, s, True))
        self.assertIn("上限 10 轮", dp.takeover_reason(1, 0, 10, s, True))
        self.assertIn("不自动重试", dp.takeover_reason(1, 0, 0, dict(s, auto_retry_max=0), True))
        self.assertIn("不足 2 小时", dp.takeover_reason(1, 0, 0, s, True, 3600))
        self.assertEqual("", dp.takeover_reason(1, 0, 0, s, True, 3 * 3600))


class CounterAndRetryTests(Harness):
    def test_normal_failures_counted_and_item_not_in_attention(self):
        message = self.fail_run(["stalled", "timeout", "eta"])
        item = self.item_()
        self.assertEqual(3, item["normal_failures"])
        self.assertEqual("retrying", item["status"])
        self.assertEqual(1, item["download_retry"]["count"])
        self.assertIn("自动重试中（第 1 次，预计", item["note"])
        self.assertIn("自动重试中", message)
        self.assertEqual("downloading", workflow_state(item))
        self.agent.notifier.notify.assert_not_called()

    def test_patient_wait_counts_one_failure(self):
        self.fail_run(["eta", "eta", "eta", "stalled"])      # 3 slow lines + a patient wait that did not complete
        self.assertEqual(4, self.item_()["normal_failures"])

    def test_backoff_schedule(self):
        self.store.update_preferences(download_normal_max_failures=20, download_auto_retry_max=50)
        for expected in (1, 3, 5, 10, 10):
            before = datetime.now().astimezone()
            self.fail_run(["stalled"] * 3)
            retry = self.item_()["download_retry"]
            minutes = (datetime.fromisoformat(retry["next_at"]) - before).total_seconds() / 60
            self.assertAlmostEqual(expected, minutes, delta=1.0)
        self.assertEqual(5, self.item_()["download_retry"]["count"])

    def test_takeover_on_normal_threshold_when_highspeed_off(self):
        self.fail_run(["stalled"] * 3)
        message = self.fail_run(["stalled"] * 3, ValueError)
        item = self.item_()
        self.assertEqual("attention", item["status"])
        self.assertIsNone(item["download_retry"])
        self.assertIn("普通线路累计失败 6 次（上限 5 次）", message)
        self.agent.notifier.notify.assert_called_once()
        self.assertEqual("needs_action", self.agent.notifier.notify.call_args[0][0])

    def test_normal_threshold_does_not_stop_when_highspeed_is_available(self):
        self.store.update_preferences(download_highspeed="smart", download_highspeed_chosen=True, rules_version=RULES_VERSION,
                                      download_normal_max_failures=2)
        self.fail_run(["stalled"] * 4)      # 3 lines + one high-speed try
        item = self.item_()
        self.assertEqual("retrying", item["status"])
        self.assertEqual((3, 1), (item["normal_failures"], item["highspeed_failures"]))

    def test_takeover_on_retry_rounds_limit(self):
        self.store.update_preferences(download_auto_retry_max=2, download_normal_max_failures=20)
        self.fail_run(["stalled"] * 3)
        self.fail_run(["stalled"] * 3)
        message = self.fail_run(["stalled"] * 3, ValueError)
        self.assertIn("自动重试已达 2 轮（上限 2 轮", message)
        self.assertEqual("attention", self.item_()["status"])

    def test_zero_retry_rounds_means_immediate_handover(self):
        self.store.update_preferences(download_auto_retry_max=0)
        message = self.fail_run(["stalled"] * 3, ValueError)
        self.assertIn("不自动重试", message)
        self.assertEqual("attention", self.item_()["status"])

    def test_takeover_when_deadline_is_near(self):
        fulfilled_at = (datetime.now().astimezone() - timedelta(hours=23)).isoformat(timespec="seconds")
        self.store.update(self.item, auto_adopt_hours=24, fulfilled_at=fulfilled_at)
        message = self.fail_run(["stalled"] * 3, ValueError)
        self.assertIn("不足 2 小时", message)
        self.assertEqual("attention", self.item_()["status"])
        self.store.update(self.item, auto_adopt_hours=48, status="ready", normal_failures=0)
        self.fail_run(["stalled"] * 3)
        self.assertEqual("retrying", self.item_()["status"])

    def test_non_transient_errors_are_immediate(self):
        self.agent.page.get_by_role.return_value.wait_for.side_effect = ValueError("下载入口格式变化，已停止")
        with self.assertRaises(ValueError) as caught:
            self.agent.download(self.item)
        self.assertNotIsInstance(caught.exception, RetryScheduled)
        self.assertEqual("attention", self.item_()["status"])
        self.assertIsNone(self.item_().get("download_retry"))
        self.agent.notifier.notify.assert_called_once()

    def test_login_expired_is_immediate(self):
        self.agent.page.get_by_role.return_value.wait_for.side_effect = SessionExpired("账户会话已失效，请登录")
        with self.assertRaises(SessionExpired):
            self.agent.download(self.item)
        self.assertEqual("attention", self.item_()["status"])

    def test_fee_text_mismatch_is_immediate(self):
        self.store.update_preferences(download_highspeed="smart", download_highspeed_chosen=True, rules_version=RULES_VERSION)
        self.body = "高速下载扣 5 积分"
        message = self.fail_run(["stalled"] * 3, ValueError)
        self.assertIn("不自动重试", message)
        self.assertEqual("attention", self.item_()["status"])
        self.assertEqual([], self.clicked)

    def test_transient_page_error_is_retried(self):
        self.agent.page.get_by_role.return_value.wait_for.side_effect = TimeoutError("progressbar")
        with self.assertRaises(RetryScheduled):
            self.agent.download(self.item)
        self.assertEqual("retrying", self.item_()["status"])


class HighspeedTries(Harness):
    def setUp(self):
        super().setUp()
        self.store.update_preferences(download_highspeed="smart", download_highspeed_chosen=True, rules_version=RULES_VERSION)

    def run_try(self, expect):
        self.outcomes = ["stalled"] * 4
        with self.assertRaises(expect):
            self.agent.download(self.item)

    def test_five_tries_charged_once_gate_each_time(self):
        gate_calls = []
        real = browser_agent.highspeed_allowed
        browser_agent.highspeed_allowed = lambda *a, **k: (gate_calls.append(k.get("mode")), real(*a, **k))[1]
        self.addCleanup(setattr, browser_agent, "highspeed_allowed", real)
        for n in range(1, 5):
            self.run_try(RetryScheduled)
            self.assertEqual(n, self.item_()["highspeed_failures"])
            self.assertEqual("retrying", self.item_()["status"])
        self.run_try(ValueError)                            # the fifth failed try hands the item over
        item = self.item_()
        self.assertEqual("attention", item["status"])
        self.assertEqual(5, item["highspeed_failures"])
        self.assertEqual(["highspeed"] * 5, self.clicked)
        self.assertEqual([True, False, False, False, False], [x["charged"] for x in item["highspeed_paid"]])
        self.assertEqual(["smart"] * 5, gate_calls)
        self.run_try(ValueError)                            # never a sixth click
        self.assertEqual(5, len(self.clicked))

    def test_setting_caps_the_clicks(self):
        self.store.update_preferences(download_highspeed_max_tries=2)
        self.run_try(RetryScheduled)
        self.run_try(ValueError)
        self.assertEqual(2, len(self.clicked))
        self.assertIn("2 次", self.item_()["note"])

    def test_fee_gate_failure_on_a_later_try_means_no_click(self):
        self.run_try(RetryScheduled)
        self.body = "高速下载扣 5 积分"
        self.run_try(ValueError)
        self.assertEqual(1, len(self.clicked))

    def test_force_mode_later_tries_are_not_charged(self):
        self.store.update_preferences(download_highspeed="force")
        self.store.update(self.item, highspeed_paid=[dict(file_id="FILE1", time="t", charged=True, mode="force")], highspeed_clicks=1)
        self.outcomes = ["stalled", "stalled", "stalled", "stalled"]
        with self.assertRaises(ValueError):
            self.agent.download(self.item)
        self.assertEqual(["highspeed"], self.clicked)
        self.assertEqual([True, False], [x["charged"] for x in self.item_()["highspeed_paid"]])


class PlannerTests(unittest.TestCase):
    def retrying(self, name, minutes):
        return fulfilled(name, status="retrying", download_retry=dict(count=1, next_at=(NOW + timedelta(minutes=minutes)).isoformat(), last_reason="x"))

    def plan(self, items):
        return next_step(items, {}, dict(oa_first=False), NOW)

    def test_retry_waiting_item_does_not_block_other_items(self):
        step = self.plan([self.retrying("r", 5), fulfilled("f2")])
        self.assertEqual(("download", "f2"), (step["action"], step["item_id"]))

    def test_due_retry_is_downloaded_again(self):
        step = self.plan([self.retrying("r", -1)])
        self.assertEqual(("download", "r"), (step["action"], step["item_id"]))
        self.assertFalse(retry_waiting(self.retrying("r", -1), NOW))
        self.assertTrue(retry_waiting(self.retrying("r", 1), NOW))

    def test_only_waiting_retry_means_no_download(self):
        self.assertNotEqual("download", self.plan([self.retrying("r", 5)])["action"])

    def test_real_downloading_item_still_makes_the_planner_wait(self):
        step = self.plan([fulfilled("d", status="downloading"), self.retrying("r", -1)])
        self.assertEqual(("wait", "d"), (step["action"], step["item_id"]))


class MonitorRetryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.agent = tm.FakeAgent()
        self.store.update_preferences(monitor_enabled=True)
        self.notifier = Notifier(self.store, lambda t, m: None)
        self.clock = tm.Clock()
        self.monitor = Monitor(self.store, tm.FakeAccounts(), self.agent, None, self.notifier, clock=self.clock, start=False)
        self.when = datetime.now().astimezone()

    def put(self, name, minutes):
        item = fulfilled(name, status="retrying", account_id="default",
                         download_retry=dict(count=1, next_at=(self.when + timedelta(minutes=minutes)).isoformat(), last_reason="x"))
        self.store.data["items"].append(item)

    def test_due_retry_is_enqueued_once_and_future_one_is_not(self):
        self.put("future", 30)
        self.assertFalse(self.monitor.retry_downloads(time.time()))
        self.put("due", -1)
        self.assertTrue(self.monitor.retry_downloads(time.time()))
        self.assertEqual(["download"], self.agent.calls)

    def test_never_two_downloads_at_once(self):
        self.put("due", -1)
        self.agent.busy = True
        self.assertFalse(self.monitor.retry_downloads(time.time()))
        self.assertEqual([], self.agent.calls)

    def test_monitor_off_schedules_nothing(self):
        self.put("due", -1)
        self.store.update_preferences(monitor_enabled=False)
        self.monitor.tick(time.time())
        self.assertNotIn("download", self.agent.calls)


class ManualRetryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.startup = tempfile.TemporaryDirectory()
        self.server = make_server(0, self.temp.name, startup_dir=self.startup.name, start_monitor=False)
        self.server.agent.ensure = MagicMock(side_effect=AssertionError("tests must never start a real browser"))
        self.server.agent.worker.shutdown()
        self.server.agent.worker = None      # this test exercises the inline path; the worker has its own tests
        self.server.agent.download = MagicMock()
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.token = urlopen(self.base).read().decode().split("const TOKEN=")[1].split(";let state=")[0].strip('"')

    def tearDown(self):
        self.server.pipeline.close()
        self.server.monitor.close()
        self.server.shutdown()
        self.server.server_close()
        self.server.agent.commands.put(("close", None, None))
        self.server.agent.thread.join(timeout=2)
        self.temp.cleanup()
        self.startup.cleanup()

    def post(self, route, payload):
        request = Request(self.base + "/api/" + route, json.dumps(payload).encode(), method="POST",
                          headers={"Content-Type": "application/json", "Origin": self.base, "X-Demo-Token": self.token})
        return json.load(urlopen(request))

    def test_manual_retry_resets_counters_and_schedule(self):
        store = self.server.agent.store
        iid = store.add("A", "", "website", account_id="default", assist_url=BASE + "/assist/detail?id=ABC", owned=True, website_status="待确认")
        store.update(iid, status="retrying", normal_failures=4, highspeed_failures=3, highspeed_clicks=3,
                     download_retry=dict(count=3, next_at="2099-01-01T00:00:00+00:00", last_reason="x"))
        self.post("browser", {"action": "download", "id": iid})
        item = store.get(iid)
        self.assertEqual((0, 0, 0, None), (item["normal_failures"], item["highspeed_failures"], item["highspeed_clicks"], item["download_retry"]))
        self.assertEqual("ready", item["status"])
        deadline = time.time() + 3
        while time.time() < deadline and not self.server.agent.download.called:
            time.sleep(0.05)
        self.assertTrue(self.server.agent.download.called)

    def test_settings_roundtrip_and_reset(self):
        keys = ("auto_retry_max", "normal_max_failures", "highspeed_max_tries")
        self.post("preferences", {"download_settings": {"auto_retry_max": 0, "normal_max_failures": 3, "highspeed_max_tries": 7}})
        state = json.load(urlopen(self.base + "/api/state"))
        self.assertEqual((0, 3, 7), tuple(state["download"]["settings"][k] for k in keys))
        self.assertEqual([0, 50], state["download"]["ranges"]["auto_retry_max"])
        self.post("preferences", {"download_reset": True})
        state = json.load(urlopen(self.base + "/api/state"))
        self.assertEqual((10, 5, 5), tuple(state["download"]["settings"][k] for k in keys))


if __name__ == "__main__":
    unittest.main()


class FulfilledAtStampTests(unittest.TestCase):
    def test_sync_stamps_answer_time_once_and_clears_after_rejection(self):
        import inspect, browser_agent
        src = inspect.getsource(browser_agent.BrowserAgent.sync)
        self.assertIn('if state in FULFILLED and not current.get("fulfilled_at")', src)
        self.assertIn('elif state in WAITING and current.get("fulfilled_at")', src)


class ClosedBrowserTests(unittest.TestCase):
    def test_target_closed_is_transient_and_close_is_idempotent(self):
        import browser_agent
        TargetClosedError = type("TargetClosedError", (Exception,), {})
        self.assertTrue(browser_agent.is_transient(TargetClosedError("Target page, context or browser has been closed")))
        agent = browser_agent.BrowserAgent.__new__(browser_agent.BrowserAgent)
        ctx = type("Ctx", (), {"close": lambda self: (_ for _ in ()).throw(TargetClosedError("closed"))})()
        agent.context, agent.page = ctx, object()
        agent.close_context()
        self.assertIsNone(agent.context)
