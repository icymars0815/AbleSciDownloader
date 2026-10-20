"""O2 size-aware limits, O5 lean probing, O6 high-speed history, O1 dedicated download worker. Mocked; no browser, no network."""
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import browser_agent
import download_lines as dl
from accounts import Accounts
from browser_agent import BASE, BrowserAgent, SessionExpired
from core import Store
from download_policy import (DOWNLOAD_DEFAULTS, TransferWatch, download_settings, dynamic_cap, dynamic_cap_for, longer_minutes,
                             make_watch, validate_download_settings)
from download_worker import DownloadWorker
from ratelimit import RateLimiter
from tests import test_download_settings as tds

MB = 1024 * 1024
KB = 1024


class DynamicCapTests(unittest.TestCase):
    def test_setting_validation(self):
        self.assertEqual(8, DOWNLOAD_DEFAULTS["min_ok_kbps"])
        self.assertEqual(8, download_settings({})["min_ok_kbps"])
        for good in (1, 8, 200):
            self.assertEqual({"download_min_ok_kbps": good}, validate_download_settings({"min_ok_kbps": good}))
        for bad in (0, 201, "8", 8.5, True, None):
            with self.assertRaises(ValueError):
                validate_download_settings({"min_ok_kbps": bad})
        self.assertEqual(8, download_settings({"download_min_ok_kbps": 999})["min_ok_kbps"])   # damaged value -> default

    def test_cap_math(self):
        s = dict(DOWNLOAD_DEFAULTS)                                   # 10 min, 8 KB/s
        self.assertEqual(600, dynamic_cap_for(s, 0))                  # size unknown
        self.assertEqual(600, dynamic_cap_for(s, 2 * MB))             # small file: 256 s < 600 s
        self.assertEqual(12 * MB / (8 * KB), dynamic_cap_for(s, 12 * MB))   # 1536 s
        self.assertEqual(3600, dynamic_cap_for(s, 500 * MB))          # never above 60 min
        self.assertAlmostEqual(14.9 * MB / (8 * KB), dynamic_cap_for(s, int(14.9 * MB)), places=1)
        self.assertEqual(3600, dynamic_cap_for(dict(s, line_max_minutes=60), 1 * MB))
        self.assertGreater(dynamic_cap_for(dict(s, min_ok_kbps=1), 5 * MB), dynamic_cap_for(dict(s, min_ok_kbps=50), 5 * MB))
        self.assertEqual(500, dynamic_cap(500, 10 * MB, 0))           # no min speed -> configured limit

    def test_patient_and_highspeed_caps_scale_the_same_way(self):
        s = dict(DOWNLOAD_DEFAULTS)
        self.assertEqual(30, longer_minutes(s))
        self.assertEqual(45, longer_minutes(dict(s, line_max_minutes=15)))
        self.assertEqual(60, longer_minutes(dict(s, line_max_minutes=40)))
        longer = dict(s, line_max_minutes=longer_minutes(s))
        self.assertEqual(1800, dynamic_cap_for(longer, 4 * MB))                  # >= 30 min as before
        self.assertEqual(3600, dynamic_cap_for(longer, 200 * MB))                # large file: scaled up, capped at 60 min

    def test_line_is_not_timed_out_while_a_big_file_is_still_within_its_size_cap(self):
        watch = make_watch(0, dict(DOWNLOAD_DEFAULTS), False)             # 10 min base, 12 MB -> 1536 s cap
        total = 12 * MB
        self.assertEqual("receiving", watch.observe(700, 700 * 20 * KB, "", total))          # 20 KB/s steady
        late = make_watch(0, dict(DOWNLOAD_DEFAULTS, stall_seconds=600), False)
        late.observe(1000, 5 * MB, "", total)
        self.assertEqual("timeout", late.observe(1537, 6 * MB, "", total))
        old = TransferWatch(0, 0)                                        # no min speed: the plain limit applies
        self.assertEqual("timeout", old.observe(old.max_seconds + 1, 100, "", total))

    def test_eta_compares_to_dynamic_cap_and_needs_ninety_seconds(self):
        total = 12 * MB                                                  # cap 1536 s
        watch = make_watch(0, dict(DOWNLOAD_DEFAULTS), True)
        watch.observe(30, 30 * 4 * KB, "", total)
        self.assertNotEqual("eta", watch.observe(89, 89 * 4 * KB, "", total))      # 4 KB/s: hopeless, but only 89 s observed
        self.assertEqual("eta", watch.observe(90, 90 * 4 * KB, "", total))
        # 10 KB/s: 12 MB takes ~1230 s < 1536 s cap -> kept (the old fixed 600 s cap would have dropped it)
        keep = make_watch(0, dict(DOWNLOAD_DEFAULTS), True)
        keep.observe(30, 30 * 10 * KB, "", total)
        self.assertNotEqual("eta", keep.observe(90, 90 * 10 * KB, "", total))
        self.assertNotEqual("eta", keep.observe(120, 120 * 10 * KB, "", total))
        self.assertEqual(90, TransferWatch(0, 0).eta_min_observe)

    def test_plan_text_uses_the_dynamic_cap_minutes(self):
        s = dict(DOWNLOAD_DEFAULTS)
        minutes = int(dynamic_cap_for(s, 12 * MB) // 60)
        self.assertNotIn("超过", dl.plan_text(12 * MB, 12 * KB, minutes))      # 1024 s < 1536 s cap
        self.assertIn("超过", dl.plan_text(12 * MB, 4 * KB, minutes))


class LeanProbingTests(unittest.TestCase):
    def probe_of(self, speeds, log):
        def probe(line, secs):
            log.append((line, secs))
            return speeds[line]
        return probe

    def test_history_probes_only_the_best_line_for_ten_seconds(self):
        log = []
        result = dl.select_line([1, 2, 3], self.probe_of({1: 1, 2: 5000, 3: 1}, log), {1: 3000, 2: 10000, 3: 2000}, 15)
        self.assertEqual(([(2, 10)], 2, "history"), (log, result["chosen"], result["mode"]))      # 5000 >= 50% of 10000

    def test_below_half_of_history_probes_all_three(self):
        log = []
        result = dl.select_line([1, 2, 3], self.probe_of({1: 6000, 2: 4999, 3: 100}, log), {1: 3000, 2: 10000}, 15)
        self.assertEqual([2, 1, 3], [l for l, _ in log])
        self.assertEqual((1, "history_then_full"), (result["chosen"], result["mode"]))

    def test_no_history_probes_all_three_in_full_mode(self):
        log = []
        result = dl.select_line([1, 2, 3], self.probe_of({1: 10, 2: 30, 3: 20}, log), {}, 15)
        self.assertEqual(([(1, 15), (2, 15), (3, 15)], "full", 2), (log, result["mode"], result["chosen"]))

    def test_history_must_be_same_hour_and_within_seven_days(self):
        with tempfile.TemporaryDirectory() as temp:
            now = 1_800_000_000
            history = dl.SpeedHistory(Store(temp), clock=lambda: now)
            history.record(1, 9000, now - 8 * 86400)          # too old
            history.record(2, 9000, now - 5 * 3600)           # other hour
            self.assertEqual({1: None, 2: None, 3: None}, history.all())
            history.record(3, 7000, now - 2 * 86400)          # same hour, 2 days ago
            self.assertEqual({1: None, 2: None, 3: 7000}, history.all())
            log = []
            result = dl.select_line([1, 2, 3], self.probe_of({1: 0, 2: 0, 3: 4000}, log), history.all(), 15)
            self.assertEqual(([(3, 10)], "history"), (log, result["mode"]))


class HighspeedHistoryTests(unittest.TestCase):
    def test_highspeed_speed_is_recorded_per_hour(self):
        with tempfile.TemporaryDirectory() as temp:
            now = 1_800_000_000
            history = dl.SpeedHistory(Store(temp), clock=lambda: now)
            history.record(dl.HIGHSPEED, 40000)
            history.record("bogus", 40000)
            self.assertEqual(40000, history.expected(dl.HIGHSPEED))
            self.assertEqual({1: None, 2: None, 3: None}, history.all())          # free-line views are unaffected
            self.assertEqual(40000, dl.SpeedHistory(Store(temp), clock=lambda: now).expected(dl.HIGHSPEED))

    def test_wait_transfer_records_highspeed_speed(self):
        with tempfile.TemporaryDirectory() as temp:
            store = Store(temp)
            iid = store.add("A", "", "website", account_id="default", owned=True)
            agent = BrowserAgent.__new__(BrowserAgent)
            agent.store, agent.stop = store, threading.Event()
            clock = [0.0]
            agent.page = MagicMock()
            agent.page.wait_for_timeout.side_effect = lambda ms: clock.__setitem__(0, clock[0] + 5)
            pending = []

            def body(*_a, **_k):
                got = int(clock[0] * 20 * KB)
                if clock[0] >= 20:
                    pending.append(1)
                return "已接收 %d KB / 1000 KB" % (got // KB)
            agent.page.locator.return_value.inner_text.side_effect = body
            history = dl.SpeedHistory(store)
            agent._record_ctx = dict(line=dl.HIGHSPEED, history=history)
            agent._line_ctx = None
            with patch.object(browser_agent.time, "monotonic", lambda: clock[0]):
                self.assertEqual("complete", agent.wait_transfer(iid, "高速通道", pending, download_settings({})))
            self.assertTrue(history.expected(dl.HIGHSPEED))
            self.assertEqual({1: None, 2: None, 3: None}, history.all())


class EarlyHighspeedTests(unittest.TestCase):
    """Smart mode: skip the free lines only when history says high speed is >= 2x faster; the fee gate still applies."""

    def setUp(self):
        tds.AgentFlowTests.setUp(self)
        self.addCleanup(lambda: [self.agent.worker and self.agent.worker.shutdown()])
        self.body = "已接收 100 KB / 100 MB\n" + tds.RULES
        self.store.update_account("default", points=100)
        self.store.update_preferences(download_highspeed="smart", download_highspeed_chosen=True, rules_version=tds.RULES_VERSION)
        self.store.update(self.item, line_probe=dict(measured={"1": 5000, "2": 8000, "3": 6000}))   # 100 MB at <= 8 KB/s > 60 min

    make_page = tds.AgentFlowTests.make_page
    run_download = tds.AgentFlowTests.run_download

    def set_hs(self, bps):
        dl.SpeedHistory(self.store).record(dl.HIGHSPEED, bps)

    def test_goes_to_highspeed_early_when_twice_as_fast(self):
        self.set_hs(16000)
        self.outcomes = ["complete"]
        spy, error = self.run_download()
        self.assertIsNone(error)
        self.assertEqual([("高速通道", False)], self.waits)
        self.assertEqual(["highspeed"], self.clicked)
        item = self.store.get(self.item)
        self.assertEqual("early_eta", item["download_attempts"][0]["reason"])
        self.assertEqual(0, int(item.get("normal_failures") or 0))
        self.assertEqual(1, len(item["highspeed_paid"]))                      # charged-once record written before the click
        self.assertEqual([1, 2, 3], list(spy.call_args.args[0]))               # still through highspeed_allowed

    def test_not_twice_as_fast_keeps_trying_free_lines(self):
        self.set_hs(15000)
        self.outcomes = ["complete"]
        _spy, error = self.run_download()
        self.assertIsNone(error)
        self.assertEqual("普通线路 1", self.waits[0][0])
        self.assertEqual([], self.clicked)

    def test_a_free_line_that_would_fit_the_cap_blocks_early_highspeed(self):
        self.set_hs(100000)
        self.store.update(self.item, line_probe=dict(measured={"1": 5000, "2": 8000, "3": 40000}))   # line 3: 100 MB / 40 KB/s = 2560 s
        self.outcomes = ["complete"]
        self.run_download()
        self.assertEqual("普通线路 1", self.waits[0][0])
        self.assertEqual([], self.clicked)

    def test_no_history_no_early(self):
        self.outcomes = ["complete"]
        self.run_download()
        self.assertEqual("普通线路 1", self.waits[0][0])

    def test_fee_gate_still_blocks(self):
        self.set_hs(50000)
        self.body = "已接收 100 KB / 100 MB\n高速下载扣 9 积分"
        self.outcomes = []
        _spy, error = self.run_download()
        self.assertIsNotNone(error)
        self.assertEqual(([], []), (self.clicked, self.waits))

    def test_max_tries_still_applies(self):
        self.set_hs(50000)
        self.store.update(self.item, highspeed_clicks=5)
        _spy, error = self.run_download()
        self.assertIn("已尝试", error)
        self.assertEqual([], self.clicked)

    def test_off_mode_never_goes_early(self):
        self.set_hs(50000)
        self.store.update_preferences(download_highspeed="off")
        self.outcomes = ["complete"]
        self.run_download()
        self.assertEqual("普通线路 1", self.waits[0][0])
        self.assertEqual([], self.clicked)

    def test_after_three_slow_lines_it_skips_the_patient_wait(self):
        self.set_hs(50000)
        self.store.update(self.item, line_probe=None)
        self.store.update_preferences(download_probe_enabled=True)
        # probing is mocked away; the three lines are dropped for being slow, then history decides
        self.agent.choose_line = MagicMock(return_value=([1, 2, 3], 1))
        self.store.update(self.item, line_probe=dict(measured={"1": 5000}))      # only partly measured: no early jump before the lines
        self.outcomes = ["eta", "eta", "eta", "complete"]
        _spy, error = self.run_download()
        self.assertIsNone(error)
        self.assertEqual(["普通线路 1", "普通线路 2", "普通线路 3", "高速通道"], [w[0] for w in self.waits][-4:])
        self.assertEqual(["highspeed"], self.clicked)


# ---------------------------------------------------------------- O1
class FakeClockStop:
    """Stand-in for an Event whose wait() advances a shared fake clock instead of sleeping."""

    def __init__(self, clock):
        self.clock = clock

    def wait(self, seconds=None):
        self.clock.advance(seconds or 0)
        return False

    def is_set(self):
        return False

    def set(self):
        pass

    def clear(self):
        pass


class FakeClock:
    def __init__(self):
        self.t = 1000.0
        self.lock = threading.Lock()

    def now(self):
        with self.lock:
            return self.t

    def advance(self, seconds):
        with self.lock:
            self.t += seconds


def wait_for(condition, seconds=5):
    end = time.time() + seconds
    while time.time() < end:
        if condition():
            return True
        time.sleep(0.01)
    return False


class WorkerBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.done = []
        self.main = BrowserAgent(self.store, Accounts(self.temp.name), use_worker=True, on_done=lambda *a: self.done.append(a))
        self.worker = self.main.worker
        self.addCleanup(self.close)
        self.main.ensure = MagicMock(side_effect=AssertionError("tests must never start a real browser"))
        self.worker.ensure = MagicMock(side_effect=AssertionError("tests must never start a real browser"))
        self.state = {"cookies": [{"name": "s", "value": "SECRET-TOKEN-123"}], "origins": []}
        self.main.context = MagicMock()
        self.main.context.storage_state.return_value = self.state
        self.main.ensure = MagicMock()
        self.iid = self.store.add("A", "", "website", account_id="default", assist_url=BASE + "/assist/detail?id=ABC", owned=True)

    def close(self):
        self.main.request_stop()
        self.main.commands.put(("close", None, None))
        self.main.thread.join(5)


class WorkerTests(WorkerBase):
    def test_main_agent_stays_free_while_a_download_runs(self):
        started, release, seen = threading.Event(), threading.Event(), {}

        def slow_download(agent, item_id):
            seen["state"] = agent._state
            seen["thread"] = threading.current_thread()
            started.set()
            release.wait(10)
        self.main.sync = MagicMock()
        with patch.object(BrowserAgent, "download", slow_download):
            self.main.enqueue("download", self.iid)
            self.assertTrue(started.wait(5))
            self.assertTrue(wait_for(lambda: not self.main.busy))                 # the main agent handed it over and is idle
            self.assertEqual(dict(enabled=True, status="downloading", item_id=self.iid), self.main.worker_status())
            self.assertIsNot(seen["thread"], self.main.thread)
            self.assertIs(self.state, seen["state"])                              # session handed over in memory
            self.main.enqueue("sync")                                              # sync runs while the download is in progress
            self.assertTrue(wait_for(lambda: self.main.sync.called))
            self.assertTrue(wait_for(lambda: any(d[0] == "sync" for d in self.done)))
            self.assertTrue(self.worker.busy)
            release.set()
            self.assertTrue(wait_for(lambda: not self.worker.busy))
        self.assertEqual("idle", self.main.worker_status()["status"])
        self.assertIsNone(self.worker._state)                                      # nothing lingers after the job
        self.assertIn(("download", self.iid, None), self.done)

    def test_only_one_download_at_a_time(self):
        started, release, calls = threading.Event(), threading.Event(), []
        other = self.store.add("B", "", "website", account_id="default", assist_url=BASE + "/assist/detail?id=XYZ", owned=True)

        def slow_download(agent, item_id):
            calls.append(item_id)
            started.set()
            release.wait(10)
        with patch.object(BrowserAgent, "download", slow_download):
            self.main.enqueue("download", self.iid)
            self.assertTrue(started.wait(5))
            self.assertTrue(wait_for(lambda: not self.main.busy))
            with self.assertRaises(ValueError):
                self.main.enqueue("download", other)
            with self.assertRaises(ValueError):
                self.main.enqueue("switch_account", "x")                           # no account change under a running download
            with self.assertRaises(ValueError):
                self.worker.submit(other, self.state)
            self.assertEqual(self.iid, self.worker.current_item)                  # the refused submit did not steal the slot
            release.set()
            self.assertTrue(wait_for(lambda: not self.worker.busy))
        self.assertEqual([self.iid], calls)

    def test_handover_keeps_the_precondition_checks(self):
        self.store.update(self.iid, pdf="downloads/x.pdf")
        with patch.object(BrowserAgent, "download") as inline:
            self.main.enqueue("download", self.iid)
            self.assertTrue(wait_for(lambda: not self.main.busy and self.main.commands.empty()))
            inline.assert_not_called()
        self.assertFalse(self.worker.busy)
        self.assertTrue(any("已有本地 PDF" in e["message"] for e in self.store.snapshot()["events"]))

    def test_shared_limiter_spaces_requests_across_both_threads(self):
        clock = FakeClock()
        limiter = RateLimiter(lambda: 3, clock=clock.now, sleep=clock.advance)
        self.main.limiter = self.worker.limiter = limiter
        self.main.stop, self.worker.stop = FakeClockStop(clock), FakeClockStop(clock)
        self.assertIs(self.main.limiter, self.worker.limiter)
        entries = []

        def hammer(agent):
            for _ in range(4):
                with agent.throttled():
                    entries.append(clock.now())
        threads = [threading.Thread(target=hammer, args=(a,)) for a in (self.main, self.worker)]
        [t.start() for t in threads]
        [t.join(10) for t in threads]
        self.assertEqual(8, len(entries))
        entries.sort()
        self.assertTrue(all(b - a >= 3 for a, b in zip(entries, entries[1:])), entries)

    def test_clean_shutdown_stops_a_running_download(self):
        started = threading.Event()

        def stoppable(agent, item_id):
            started.set()
            if agent.stop.wait(10):
                raise ValueError("下载已停止，未执行采纳或驳回")
        with patch.object(BrowserAgent, "download", stoppable):
            self.main.enqueue("download", self.iid)
            self.assertTrue(started.wait(5))
            self.main.request_stop()
            self.main.commands.put(("close", None, None))
            self.main.thread.join(10)
            self.assertFalse(self.main.thread.is_alive())
            self.assertFalse(self.worker.thread.is_alive())
        self.assertFalse(self.worker.busy)

    def test_shutdown_when_idle(self):
        self.worker.shutdown()
        self.assertFalse(self.worker.thread.is_alive())
        self.assertTrue(self.main.thread.is_alive())


class WorkerSessionTests(WorkerBase):
    def test_state_request_is_served_on_the_main_agents_thread(self):
        threads = []
        self.main.context.storage_state.side_effect = lambda: threads.append(threading.current_thread()) or {"cookies": [], "n": 2}
        result = []
        t = threading.Thread(target=lambda: result.append(self.main.fetch_state(threading.Event(), timeout=5)))
        t.start()
        t.join(6)
        self.assertEqual([{"cookies": [], "n": 2}], result)
        self.assertEqual([self.main.thread], threads)

    def test_expiry_refreshes_once_then_continues(self):
        fresh = {"cookies": [{"name": "s", "value": "NEW"}]}
        self.main.fetch_state = MagicMock(return_value=fresh)
        self.worker.close_context = MagicMock()
        with patch.object(BrowserAgent, "goto", side_effect=[SessionExpired("x"), None]) as goto:
            self.worker.goto("/assist/detail?id=ABC")
        self.assertEqual(2, goto.call_count)
        self.main.fetch_state.assert_called_once()
        self.assertIs(fresh, self.worker._state)

    def test_expiry_after_refresh_is_a_takeover_as_login_expired(self):
        self.main.fetch_state = MagicMock(return_value={"cookies": []})
        self.worker.close_context = MagicMock()
        self.store.update(self.iid, status="ready")
        with patch.object(BrowserAgent, "goto", side_effect=SessionExpired("账户会话已失效，请点击账户登录")) as goto:
            self.main.enqueue("download", self.iid)
            self.assertTrue(wait_for(lambda: self.store.get(self.iid)["status"] == "attention"))
            self.assertTrue(wait_for(lambda: not self.worker.busy))
        self.main.fetch_state.assert_called_once()                             # refreshed exactly once
        self.assertEqual(2, goto.call_count)
        item = self.store.get(self.iid)
        self.assertIn("会话已失效", item["note"])
        self.assertTrue(wait_for(lambda: any(d[0] == "download" and isinstance(d[2], SessionExpired) for d in self.done)))

    def test_no_fresh_state_is_a_takeover_without_retry(self):
        self.main.fetch_state = MagicMock(return_value=None)
        with patch.object(BrowserAgent, "goto", side_effect=SessionExpired("会话已失效")) as goto:
            with self.assertRaises(SessionExpired):
                self.worker.goto("/x")
        self.assertEqual(1, goto.call_count)


class FakePlaywright:
    """Only non-persistent launch is available: a persistent profile (or any file path) would raise AttributeError."""

    def __init__(self):
        self.contexts = []
        outer = self

        class Browser:
            def new_context(self, **kwargs):
                outer.contexts.append(kwargs)
                ctx = MagicMock()
                ctx.new_page.return_value = MagicMock()
                return ctx

            def close(self):
                pass

        class Chromium:
            launched = []

            def launch(self, **kwargs):
                Chromium.launched.append(kwargs)
                return Browser()
        self.chromium = Chromium()

    def start(self):
        return self

    def stop(self):
        pass


class WorkerContextTests(WorkerBase):
    def test_context_is_non_persistent_from_memory_state_and_never_on_disk(self):
        fake = FakePlaywright()
        module = types.ModuleType("playwright.sync_api")
        module.sync_playwright = lambda: fake
        parent = types.ModuleType("playwright")
        self.worker.__dict__.pop("ensure")          # use the real ensure (instance mock removed)
        self.worker._state = self.state
        with patch.dict(sys.modules, {"playwright": parent, "playwright.sync_api": module}):
            self.worker.ensure()
        self.assertEqual(1, len(fake.contexts))
        kwargs = fake.contexts[0]
        self.assertIs(self.state, kwargs["storage_state"])                    # a dict in memory, not a file path
        self.assertTrue(kwargs["accept_downloads"])
        self.assertTrue(fake.chromium.launched[0]["headless"])
        self.assertFalse(hasattr(fake.chromium, "launch_persistent_context"))
        self.store.update(self.iid, note="after")
        for path in Path(self.temp.name).rglob("*"):
            if path.is_file():
                self.assertNotIn(b"SECRET-TOKEN-123", path.read_bytes(), str(path))
        self.worker.close_context()
        self.assertIsNone(self.worker.context)


if __name__ == "__main__":
    unittest.main()
