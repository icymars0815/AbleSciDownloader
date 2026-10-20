import json
import queue
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import autostart as autostart_module
import notifications
import oa_fetch
from app import make_server
from autostart import Autostart
from browser_agent import SessionExpired
from core import Store
from instance_lock import AlreadyRunning, InstanceLock
from monitor import Monitor, choose_interval, validate_monitor
from notifications import Notifier
from pipeline import FULFILLED, Pipeline, WAITING
from ratelimit import PublishLimiter, RateLimiter, limit_settings, validate_limits
from tray import Tray

T = 1_800_000_000.0   # fixed "now" (epoch seconds)


def iso(offset):
    return datetime.fromtimestamp(T + offset).astimezone().isoformat(timespec="seconds")


def waiting(name, age, **fields):
    base = dict(id=name, title="Title " + name, doi="10.1/" + name, source="website", owned=True, status="ready",
                created="2026-10-19T10:00:00", account_id="default", assist_url="https://www.ablesci.com/assist/detail?id=" + name,
                website_status=WAITING[0], first_seen=iso(-age))
    base.update(fields)
    return base


class FakeAccounts:
    active_id = "default"

    def has_cookie_file(self):
        return True


class FakeAgent:
    def __init__(self):
        self.busy = False
        self.commands = queue.Queue()
        self.calls = []
        self.context = None
        self.interactive = False
        self.now = 1000.0
        self.last_activity = 1000.0
        self.on_done = None

    def clock(self):
        return self.now

    def has_browser(self):
        return bool(self.context)

    def enqueue(self, action, item_id=None, credentials=None):
        if self.busy:
            raise ValueError("busy")
        self.calls.append(action)


class FakePipeline:
    def __init__(self, on=False):
        self.on, self.ticks = on, 0

    def enabled(self):
        return self.on

    def tick(self):
        self.ticks += 1


class Clock:
    def __init__(self, value=T):
        self.value = value

    def __call__(self):
        return self.value


class IntervalTests(unittest.TestCase):
    def pick(self, items, prefs=None, failures=0):
        return choose_interval(items, prefs or {}, T, failures)

    def test_each_band(self):
        self.assertEqual((30, "hot"), tuple(self.pick([waiting("a", 10)])[k] for k in ("seconds", "band")))
        self.assertEqual(30, self.pick([waiting("a", 299)])["seconds"])
        self.assertEqual(60, self.pick([waiting("a", 301)])["seconds"])
        self.assertEqual(60, self.pick([waiting("a", 14 * 60)])["seconds"])
        self.assertEqual((180, "warm"), tuple(self.pick([waiting("a", 16 * 60)])[k] for k in ("seconds", "band")))
        self.assertEqual((600, "normal"), tuple(self.pick([waiting("a", 2 * 3600)])[k] for k in ("seconds", "band")))
        self.assertEqual((1800, "idle"), tuple(self.pick([])[k] for k in ("seconds", "band")))

    def test_only_waiting_items_count(self):
        gone = waiting("done", 10, website_status=FULFILLED[0])
        self.assertEqual("idle", self.pick([gone])["band"])             # left WAITING: hot polling stops
        self.assertEqual("normal", self.pick([gone, waiting("old", 7200)])["band"])
        local = waiting("loc", 10, source="import", website_status="")
        self.assertEqual("idle", self.pick([local])["band"])

    def test_multiple_hot_items_single_request(self):
        items = [waiting("a", 5), waiting("b", 20), waiting("c", 100)]
        self.assertEqual(30, self.pick(items)["seconds"])
        agent = FakeAgent()
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(tmp)
            store.data["items"] = items
            store.update_preferences(monitor_enabled=True)
            monitor = Monitor(store, FakeAccounts(), agent, start=False, clock=Clock(T))
            monitor.next_due = T
            self.assertEqual("sync", monitor.tick(T))
            self.assertEqual(["sync"], agent.calls)
            self.assertIsNone(monitor.tick(T + 1))   # in flight: still exactly one request
            self.assertEqual(["sync"], agent.calls)

    def test_newest_item_decides_and_publish_time_wins(self):
        self.assertEqual(30, self.pick([waiting("old", 7200), waiting("new", 10)])["seconds"])
        published = waiting("p", 7200, publish_action=dict(status="confirmed", time=iso(-20)))
        self.assertEqual(30, self.pick([published])["seconds"])
        unconfirmed = waiting("u", 7200, publish_action=dict(status="attempted", time=iso(-20)))
        self.assertEqual("idle", self.pick([unconfirmed])["band"])   # an unconfirmed publish is "attention", not waiting

    def test_switches_and_values(self):
        items = [waiting("a", 10)]
        self.assertEqual("normal", self.pick(items, dict(monitor_hot_enabled=False))["band"])
        self.assertEqual(600, self.pick([waiting("a", 3000)], dict(monitor_hot_enabled=False))["seconds"])
        self.assertEqual(20, self.pick(items, dict(monitor_hot_fast_seconds=20))["seconds"])
        self.assertEqual(120, self.pick(items, dict(monitor_hot_fast_seconds=999))["seconds"])   # clamped
        self.assertEqual(60, self.pick([waiting("a", 70)], dict(monitor_hot_fast_min=1))["seconds"])
        self.assertEqual(1800, self.pick([waiting("a", 7200)], dict(monitor_normal_min=30))["seconds"])

    def test_backoff_and_hot_failure(self):
        old = [waiting("a", 7200)]
        self.assertEqual([600, 1200, 2400, 3600, 3600], [self.pick(old, failures=n)["seconds"] for n in range(5)])
        self.assertEqual([1800, 3600], [self.pick([], failures=n)["seconds"] for n in range(2)])
        self.assertEqual(180, self.pick([waiting("a", 10)], failures=1)["seconds"])   # no rapid retries in the hot window
        self.assertEqual(180, self.pick([waiting("a", 600)], failures=3)["seconds"])

    def test_validation(self):
        self.assertEqual({"monitor_hot_fast_seconds": 20, "monitor_enabled": True}, validate_monitor(dict(hot_fast_seconds=20, enabled=True)))
        for bad in ({}, {"enabled": 1}, {"hot_fast_seconds": 19}, {"hot_fast_seconds": 121}, {"hot_fast_min": 0}, {"hot_fast_min": 16},
                    {"normal_min": 9}, {"normal_min": 61}, {"normal_min": True}, {"normal_min": 10.5}, {"bogus": 1}, {"sync_now": "y"}):
            with self.assertRaises(ValueError, msg=bad):
                validate_monitor(bad)


class MonitorFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.agent = FakeAgent()
        self.pipeline = FakePipeline()
        self.clock = Clock()
        self.toasts = []
        self.wall = [datetime(2026, 10, 19, 12, 0)]
        self.notifier = Notifier(self.store, lambda t, m: self.toasts.append((t, m)), clock=lambda: self.wall[0])
        self.store.update_preferences(monitor_enabled=True)
        self.monitor = Monitor(self.store, FakeAccounts(), self.agent, self.pipeline, self.notifier, clock=self.clock, start=False)

    def put(self, *items):
        self.store.data["items"] = list(items)
        self.store.save()

    def finish_sync(self, error=None):
        self.agent.on_done("sync", None, error)

    def test_tick_waits_for_due_time_and_session(self):
        self.put(waiting("a", 10))
        self.assertIsNone(self.monitor.tick(T))                # START_DELAY not reached
        self.assertEqual("sync", self.monitor.tick(T + 31))

    def test_success_schedules_by_band_and_backoff_on_failure(self):
        self.put(waiting("a", 7200, first_seen=iso(-7200)))
        self.clock.value = T
        self.monitor.inflight = True
        self.finish_sync(RuntimeError("net"))
        self.assertEqual((1, T + 1200), (self.monitor.failures, self.monitor.next_due))
        self.finish_sync(RuntimeError("net"))
        self.assertEqual((2, T + 2400), (self.monitor.failures, self.monitor.next_due))
        self.finish_sync()
        self.assertEqual((0, T + 600), (self.monitor.failures, self.monitor.next_due))

    def test_failure_notice_only_after_three(self):
        self.store.update_preferences(notify_items={"sync_failed": True})
        for _ in range(2):
            self.finish_sync(RuntimeError("x"))
        self.assertEqual([], self.toasts)
        self.finish_sync(RuntimeError("x"))
        self.assertEqual(1, len(self.toasts))

    def test_session_expired_pauses_and_notifies_once(self):
        self.finish_sync(SessionExpired("login"))
        self.assertTrue(self.monitor.login_required)
        self.assertEqual("需登录", self.monitor.label())
        self.assertEqual(1, len(self.toasts))
        self.agent.on_done("sync", None, SessionExpired("login"))
        self.assertEqual(1, len(self.toasts))                  # deduplicated
        self.assertIsNone(self.monitor.tick(T + 10_000))       # no polling until the user logs in
        self.agent.on_done("login", None, None)
        self.assertFalse(self.monitor.login_required)

    def test_fulfilled_change_notifies_and_hands_to_pipeline(self):
        self.pipeline.on = True
        self.put(waiting("a", 100))
        self.finish_sync()                                      # baseline, silent
        self.assertEqual([], self.toasts)
        self.put(waiting("a", 100, website_status=FULFILLED[0]))
        self.finish_sync()
        self.assertEqual(1, len(self.toasts))
        self.assertEqual("有人应助", self.toasts[0][0])
        self.assertGreaterEqual(self.pipeline.ticks, 1)         # download is not left to the next poll
        self.finish_sync()
        self.assertEqual(1, len(self.toasts))                   # (item, kind) deduplicated

    def test_pipeline_off_only_notifies(self):
        self.put(waiting("a", 100))
        self.finish_sync()
        self.put(waiting("a", 100, website_status=FULFILLED[0]))
        self.finish_sync()
        self.assertEqual((1, 0), (len(self.toasts), self.pipeline.ticks))

    def test_pending_fulfilled_item_is_enqueued_immediately_when_pipeline_on(self):
        self.pipeline.on = True
        self.put(waiting("a", 100, website_status=FULFILLED[0]))
        self.finish_sync()
        self.assertGreaterEqual(self.pipeline.ticks, 1)

    def test_first_seen_stamped(self):
        item = waiting("a", 0)
        item.pop("first_seen")
        self.put(item)
        self.finish_sync()
        self.assertTrue(self.store.get("a")["first_seen"])

    def test_sleep_resume_delays_sync(self):
        self.put(waiting("a", 10))
        self.monitor.next_due = T
        self.monitor.tick(T - 10)                               # establishes last_poll (not yet due)
        self.assertEqual("sync", self.monitor.tick(T))
        self.monitor.inflight = False
        self.monitor.next_due = T
        self.assertIsNone(self.monitor.tick(T + 3600))          # woke from sleep: wait a minute
        self.assertIsNone(self.monitor.tick(T + 3630))
        self.assertEqual("sync", self.monitor.tick(T + 3661))

    def test_paused_does_nothing_and_resumes(self):
        self.store.update_preferences(monitor_enabled=False)
        self.put(waiting("a", 10))
        self.monitor.next_due = T
        self.assertIsNone(self.monitor.tick(T + 100))
        self.assertEqual("已暂停", self.monitor.label())
        self.monitor.set_enabled(True)
        self.assertEqual("监控中", self.monitor.label())

    def test_public_state(self):
        self.put(waiting("a", 10, first_seen=iso(-10)))
        info = self.monitor.public()
        self.assertEqual(("监控中", True), (info["label"], info["enabled"]))
        self.assertEqual(30, info["settings"]["hot_fast_seconds"])
        self.assertIn("next_sync_at", info)

    def test_idle_browser_close(self):
        agent = self.agent
        agent.context = object()
        agent.now = 1000 + 299
        self.assertFalse(self.monitor.close_idle_browser())
        agent.now = 1000 + 301
        agent.busy = True
        self.assertFalse(self.monitor.close_idle_browser())     # never while working
        agent.busy = False
        agent.interactive = True
        self.assertFalse(self.monitor.close_idle_browser())     # never a login window
        agent.interactive = False
        self.assertTrue(self.monitor.close_idle_browser())
        self.assertEqual(["close_idle"], agent.calls)
        self.assertFalse(self.monitor.close_idle_browser())

    def test_monitor_persists_but_pipeline_resets_on_restart(self):
        self.store.update_preferences(monitor_enabled=True, pipeline_enabled=True)
        reopened = Store(self.temp.name)
        pipeline = Pipeline(reopened, FakeAccounts(), FakeAgent(), start=False)
        monitor = Monitor(reopened, FakeAccounts(), FakeAgent(), pipeline, start=False)
        self.assertFalse(pipeline.enabled())
        self.assertTrue(monitor.enabled())
        self.assertTrue(Store(self.temp.name).snapshot()["preferences"]["monitor_enabled"])


class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.shown = []
        self.now = [datetime(2026, 10, 19, 12, 0)]
        self.n = Notifier(self.store, lambda t, m: self.shown.append(m), clock=lambda: self.now[0])

    def test_dedupe_and_reset(self):
        self.assertEqual("sent", self.n.notify("applied", "x", "t", "m"))
        self.assertEqual("duplicate", self.n.notify("applied", "x", "t", "m"))
        self.assertEqual("sent", self.n.notify("applied", "y", "t", "m"))
        self.assertEqual("sent", self.n.notify("needs_action", "x", "t", "m"))
        self.n.reset("x", "applied")
        self.assertEqual("sent", self.n.notify("applied", "x", "t", "m"))
        self.assertEqual(4, len(self.shown))
        again = Notifier(Store(self.temp.name), self.shown.append)    # survives a restart
        self.assertEqual("duplicate", again.notify("applied", "y", "t", "m"))

    def test_defaults_and_item_toggles(self):
        self.assertEqual("muted", self.n.notify("closed", "a", "t", "m"))      # default off
        self.assertEqual("muted", self.n.notify("oa_ok", "a", "t", "m"))
        self.assertEqual("muted", self.n.notify("sync_failed", "a", "t", "m"))
        self.store.update_preferences(notify_items={"closed": True, "applied": False})
        self.assertEqual("sent", self.n.notify("closed", "b", "t", "m"))
        self.assertEqual("muted", self.n.notify("applied", "b", "t", "m"))
        self.assertEqual(1, len(self.shown))

    def test_master_switch(self):
        self.store.update_preferences(notify_master=False)
        for kind in ("applied", "login_expired", "needs_human"):
            self.assertEqual("muted", self.n.notify(kind, "a", "t", "m"))
        self.assertEqual([], self.shown)
        self.assertTrue(any("[通知]" in e["message"] for e in self.store.snapshot()["events"]))   # still recorded in-app

    def test_quiet_hours_summary(self):
        self.store.update_preferences(notify_quiet_enabled=True, notify_quiet_start="22:00", notify_quiet_end="08:00")
        self.now[0] = datetime(2026, 10, 19, 23, 30)
        self.assertEqual("queued", self.n.notify("applied", "a", "t", "第一条"))
        self.assertEqual("queued", self.n.notify("needs_action", "b", "t", "第二条"))
        self.now[0] = datetime(2026, 10, 20, 3, 0)
        self.assertFalse(self.n.flush())                         # still quiet
        self.assertEqual([], self.shown)
        self.now[0] = datetime(2026, 10, 20, 8, 1)
        self.assertTrue(self.n.flush())
        self.assertEqual(1, len(self.shown))                     # a single summary
        self.assertIn("2 条", self.shown[0])
        self.assertFalse(self.n.flush())
        self.assertEqual("sent", self.n.notify("applied", "c", "t", "m"))

    def test_quiet_window_same_day_and_disabled(self):
        cfg = notifications.settings(dict(notify_quiet_enabled=True, notify_quiet_start="13:00", notify_quiet_end="14:00"))
        self.assertTrue(notifications.in_quiet(cfg, datetime(2026, 1, 1, 13, 30)))
        self.assertFalse(notifications.in_quiet(cfg, datetime(2026, 1, 1, 14, 0)))
        self.assertFalse(notifications.in_quiet(notifications.settings({}), datetime(2026, 1, 1, 3, 0)))

    def test_validation(self):
        cur = notifications.settings({})
        self.assertEqual({"notify_master": False}, notifications.validate({"master": False}, cur))
        ok = notifications.validate({"items": {"closed": True}}, cur)
        self.assertTrue(ok["notify_items"]["closed"] and ok["notify_items"]["applied"])
        for bad in ({}, {"master": "no"}, {"items": {"nope": True}}, {"items": {"closed": 1}}, {"items": {}}, {"quiet_start": "25:00"},
                    {"quiet_end": "8:00"}, {"quiet_enabled": 1}, {"x": 1}):
            with self.assertRaises(ValueError, msg=bad):
                notifications.validate(bad, cur)


class RateLimitTests(unittest.TestCase):
    def test_request_spacing_with_fake_clock(self):
        now = [100.0]
        slept = []

        def sleep(seconds):
            slept.append(seconds)
            now[0] += seconds
        limiter = RateLimiter(lambda: 3, lambda: now[0], sleep)
        with limiter.slot():
            pass
        with limiter.slot():
            pass
        now[0] += 1
        with limiter.slot():
            now[0] += 2                                           # the request itself takes 2 s
        with limiter.slot():
            pass
        self.assertEqual([3, 2, 3], [round(x, 6) for x in slept])
        self.assertEqual(3.0, limiter.delay())

    def test_gap_clamped_to_floor_and_ceiling(self):
        now = [0.0]
        limiter = RateLimiter(lambda: 0, lambda: now[0], lambda s: None)
        with limiter.slot():
            pass
        self.assertEqual(3.0, limiter.delay())
        limiter.gap = lambda: 999
        self.assertEqual(30.0, limiter.delay())

    def test_stop_interrupts_wait(self):
        now = [0.0]
        limiter = RateLimiter(lambda: 30, lambda: now[0], lambda s: None)
        with limiter.slot():
            pass
        stop = threading.Event()
        stop.set()
        with self.assertRaises(ValueError):
            with limiter.slot(stop):
                self.fail("must not run")

    def test_settings_and_validation(self):
        self.assertEqual(dict(request_gap_seconds=3, publish_per_hour=10, publish_gap_seconds=60), limit_settings({}))
        self.assertEqual(3, limit_settings(dict(request_gap_seconds=1))["request_gap_seconds"])
        self.assertEqual(60, limit_settings(dict(publish_gap_seconds=5))["publish_gap_seconds"])
        for bad in ({}, {"request_gap_seconds": 2}, {"request_gap_seconds": 31}, {"publish_per_hour": 0}, {"publish_per_hour": 31},
                    {"publish_gap_seconds": 59}, {"publish_per_hour": True}, {"publish_per_hour": 5.5}, {"x": 1}):
            with self.assertRaises(ValueError, msg=bad):
                validate_limits(bad)
        self.assertEqual({"publish_per_hour": 30}, validate_limits({"publish_per_hour": 30}))

    def test_publish_spacing_and_hourly_cap(self):
        now = [10_000.0]
        limiter = PublishLimiter(lambda: dict(publish_per_hour=3, publish_gap_seconds=60), lambda: now[0])
        self.assertEqual(0, limiter.wait_seconds())
        limiter.record()
        self.assertEqual(60, limiter.wait_seconds())
        now[0] += 59
        self.assertAlmostEqual(1, limiter.wait_seconds())
        now[0] += 1
        self.assertEqual(0, limiter.wait_seconds())
        limiter.record()
        now[0] += 60
        limiter.record()                                          # 3 in the last hour = cap
        now[0] += 60
        self.assertAlmostEqual(3600 - 180 - 0, limiter.wait_seconds() + 0, delta=1)   # until the oldest leaves the hour
        now[0] += 3600 - 180
        self.assertEqual(0, limiter.wait_seconds())

    def test_default_cap_is_ten_per_hour(self):
        now = [0.0]
        limiter = PublishLimiter(lambda: {}, lambda: now[0])
        for _ in range(10):
            self.assertEqual(0, limiter.wait_seconds())
            limiter.record()
            now[0] += 60
        self.assertGreater(limiter.wait_seconds(), 0)

    def test_pipeline_waits_instead_of_publishing(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(tmp)
            agent = FakeAgent()
            now = [5000.0]
            pipeline = Pipeline(store, FakeAccounts(), agent, start=False, publish_limiter=PublishLimiter.for_store(store, lambda: now[0]))
            store.add("T", "10.1/x", source="import", request_confirmed=True)
            step = dict(action="publish", item_id=store.snapshot()["items"][0]["id"], label="", requires_user=False, reason="")
            self.assertTrue(pipeline.execute(step))
            self.assertFalse(pipeline.execute(step))              # < 60 s later
            now[0] += 61
            self.assertTrue(pipeline.execute(step))
            self.assertEqual(["publish", "publish"], agent.calls)
            self.assertEqual(2, len(Store(tmp).snapshot()["publish_times"]))   # persisted


class OaPolitenessTests(unittest.TestCase):
    def test_same_host_gap_and_other_hosts_free(self):
        now = [50.0]
        slept = []
        with patch.object(oa_fetch, "_clock", lambda: now[0]), patch.object(oa_fetch, "_sleep", lambda s: (slept.append(s), now.__setitem__(0, now[0] + s))), \
                patch.dict(oa_fetch._host_next, clear=True):
            oa_fetch._throttle("https://a.example/x")
            oa_fetch._throttle("https://b.example/y")
            self.assertEqual([], slept)
            oa_fetch._throttle("https://a.example/z")
            self.assertEqual([1.0], slept)

    def test_429_skips_host_without_retry(self):
        calls = []

        def fake(url, *a, **k):
            calls.append(url)
            raise oa_fetch.OAError("HTTP 429", 429)
        with tempfile.TemporaryDirectory() as tmp, patch.object(oa_fetch, "_request", fake):
            result = oa_fetch.download_oa([dict(url="https://x.example/1.pdf", source="A"), dict(url="https://x.example/2.pdf", source="B"),
                                           dict(url="https://y.example/3.pdf", source="C")], Path(tmp) / "o.pdf")
        self.assertEqual(["https://x.example/1.pdf", "https://y.example/3.pdf"], calls)
        self.assertEqual("skipped (429)", result["attempts"][1]["status"])

    def test_429_not_retried_by_browser_fallback(self):
        info = dict(candidates=[dict(url="https://x.example/1.pdf", source="A")], is_oa=True, oa_status=None, license=None, attempts=[],
                    lookup_ok=True, manual_url=None)
        browser = MagicMock()
        with tempfile.TemporaryDirectory() as tmp, patch.object(oa_fetch, "find_oa_locations", return_value=info), \
                patch.object(oa_fetch, "_request", side_effect=oa_fetch.OAError("HTTP 429", 429)):
            result = oa_fetch.fetch_open_access("10.1/x", Path(tmp) / "o.pdf", browser=browser)
        browser.assert_not_called()
        self.assertEqual("failed", result["status"])


class LockAndAutostartTests(unittest.TestCase):
    def test_single_instance_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "data" / ".kytdl.lock"
            first = InstanceLock(path, pid=111, alive=lambda pid: True).acquire()
            self.assertEqual("111", path.read_text())
            with self.assertRaises(AlreadyRunning):
                InstanceLock(path, pid=222, alive=lambda pid: True).acquire()
            first.release()
            self.assertFalse(path.exists())

    def test_stale_lock_is_replaced(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".kytdl.lock"
            path.write_text("99999")
            lock = InstanceLock(path, pid=5, alive=lambda pid: False).acquire()
            self.assertEqual("5", path.read_text())
            path.write_text("garbage")
            InstanceLock(path, pid=6, alive=lambda pid: True).acquire()
            self.assertEqual("6", path.read_text())
            lock.release()                                          # not ours any more: left alone
            self.assertTrue(path.exists())

    def test_real_pid_probe(self):
        import os
        from instance_lock import pid_alive
        self.assertTrue(pid_alive(os.getpid()))
        self.assertFalse(pid_alive(0))

    def test_autostart_create_and_delete_shortcut(self):
        with tempfile.TemporaryDirectory() as tmp:
            startup = Path(tmp) / "Startup"
            auto = Autostart(Path(tmp) / "app", startup)
            self.assertFalse(auto.enabled())
            kind = auto.enable()
            self.assertTrue(auto.enabled())
            self.assertEqual("lnk", kind, auto.last_error)
            self.assertEqual([autostart_module.LINK_NAME], [p.name for p in startup.iterdir()])
            import pythoncom
            import win32com.client
            pythoncom.CoInitialize()
            try:
                shortcut = win32com.client.Dispatch("WScript.Shell").CreateShortCut(str(startup / autostart_module.LINK_NAME))
                target, arguments = shortcut.TargetPath, shortcut.Arguments
            finally:
                pythoncom.CoUninitialize()
            self.assertTrue(target.endswith("pythonw.exe"))
            self.assertEqual("launcher.py --background", arguments)
            auto.disable()
            self.assertFalse(auto.enabled())
            self.assertEqual([], list(startup.iterdir()))

    def test_autostart_cmd_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            auto = Autostart(Path(tmp) / "app", Path(tmp) / "S")
            with patch.object(Autostart, "_shortcut", side_effect=ImportError("no com")):
                self.assertEqual("cmd", auto.enable())
            self.assertTrue(auto.enabled())
            self.assertIn(b"launcher.py --background", (Path(tmp) / "S" / autostart_module.CMD_NAME).read_bytes())
            auto.disable()
            self.assertFalse(auto.enabled())

    def test_isolated_autostart_refuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            auto = Autostart(tmp, allowed=False)
            with self.assertRaises(ValueError):
                auto.enable()
            self.assertFalse(auto.enabled())


class TrayTests(unittest.TestCase):
    def fakes(self):
        pystray = MagicMock()
        pystray.MenuItem.side_effect = lambda text, action, **kw: (text, action)
        pystray.Menu.side_effect = lambda *items: items
        pystray.Menu.SEPARATOR = "---"
        icon = MagicMock()
        pystray.Icon.return_value = icon
        image = MagicMock()
        return pystray, icon, image

    def test_menu_actions_and_state(self):
        pystray, icon, image = self.fakes()
        calls = []
        state = {"label": "监控中", "paused": False}
        actions = dict(open_window=lambda: calls.append("open"), sync_now=lambda: calls.append("sync"),
                       toggle_pause=lambda: calls.append("toggle"), quit=lambda: calls.append("quit"))
        with tempfile.TemporaryDirectory() as tmp:
            tray = Tray(lambda: state["label"], lambda: state["paused"], actions, Path(tmp) / "logs", pystray, image, MagicMock())
            with patch("tray.make_image", return_value="img"), patch("threading.Thread"):
                tray.start()
            items = tray.menu_items()
            names = [i if isinstance(i, str) else (i[0] if isinstance(i[0], str) else i[0]()) for i in items]
            self.assertEqual(["打开窗口", "立即同步", "暂停监控", "打开日志文件夹", "---", "退出"], names)
            state["paused"] = True
            self.assertEqual("恢复监控", items[2][0]())
            for index in (0, 1, 2, 5):
                items[index][1]()
            self.assertEqual(["open", "sync", "toggle", "quit"], calls)
            with patch("os.startfile", create=True) as opener:
                items[3][1]()
            self.assertTrue((Path(tmp) / "logs").is_dir())          # created if missing
            opener.assert_called_once()
            state["label"] = "待处理 2 条"
            with patch("tray.make_image", return_value="img2"):
                tray.refresh()
            self.assertEqual("科研通下载器 · 待处理 2 条", icon.title)
            tray.notify("t", "m")
            icon.notify.assert_called_with("m", "t")


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.startup = tempfile.TemporaryDirectory()
        self.server = make_server(0, self.temp.name, startup_dir=self.startup.name, start_monitor=False)
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

    def bad(self, route, payload):
        with self.assertRaises(HTTPError) as caught:
            self.post(route, payload)
        self.assertEqual(400, caught.exception.code)

    def state(self):
        return json.load(urlopen(self.base + "/api/state"))

    def test_state_shape_and_internal_keys_hidden(self):
        state = self.state()
        self.assertFalse(state["monitor"]["enabled"])
        self.assertTrue(state["notify"]["settings"]["master"])
        self.assertEqual(9, len(state["notify"]["kinds"]))
        self.assertEqual(3, state["limits"]["settings"]["request_gap_seconds"])
        self.assertFalse(state["autostart"]["enabled"])
        for key in ("notifications", "publish_times"):
            self.assertNotIn(key, state)

    def test_monitor_endpoint(self):
        self.post("monitor", {"enabled": True})
        self.assertTrue(self.state()["monitor"]["enabled"])
        self.post("monitor", {"hot_fast_seconds": 45, "hot_fast_min": 3, "normal_min": 20, "hot_enabled": False})
        settings = self.state()["monitor"]["settings"]
        self.assertEqual((45, 3, 20, False), (settings["hot_fast_seconds"], settings["hot_fast_min"], settings["normal_min"], settings["hot_enabled"]))
        for payload in ({}, {"enabled": "x"}, {"hot_fast_seconds": 10}, {"hot_fast_min": 16}, {"normal_min": 5}, {"bogus": 1},
                        {"sync_now": True, "enabled": True}, {"sync_now": False}):
            self.bad("monitor", payload)

    def test_notifications_endpoint(self):
        self.post("notifications", {"master": False})
        self.post("notifications", {"items": {"closed": True}, "quiet_enabled": True, "quiet_start": "23:00", "quiet_end": "07:30"})
        cfg = self.state()["notify"]["settings"]
        self.assertEqual((False, True, True, "23:00", "07:30"), (cfg["master"], cfg["items"]["closed"], cfg["quiet_enabled"], cfg["quiet_start"], cfg["quiet_end"]))
        for payload in ({}, {"master": 1}, {"items": {"zzz": True}}, {"quiet_start": "7pm"}, {"extra": 1}):
            self.bad("notifications", payload)

    def test_limits_endpoint(self):
        self.post("limits", {"request_gap_seconds": 5, "publish_per_hour": 4})
        self.assertEqual((5, 4), tuple(self.state()["limits"]["settings"][k] for k in ("request_gap_seconds", "publish_per_hour")))
        for payload in ({}, {"request_gap_seconds": 2}, {"publish_per_hour": 31}, {"publish_gap_seconds": 30}, {"q": 1}):
            self.bad("limits", payload)
        self.assertEqual(5, self.server.agent.limiter._gap())

    def test_autostart_endpoint_uses_given_startup_dir_only(self):
        self.post("autostart", {"enabled": True})
        self.assertTrue(self.state()["autostart"]["enabled"])
        self.assertEqual(1, len(list(Path(self.startup.name).iterdir())))
        self.post("autostart", {"enabled": False})
        self.assertEqual([], list(Path(self.startup.name).iterdir()))
        self.bad("autostart", {"enabled": "yes"})
        self.bad("autostart", {})

    def test_isolated_server_without_startup_dir_cannot_touch_startup(self):
        with tempfile.TemporaryDirectory() as tmp:
            other = make_server(0, tmp, start_monitor=False)
            try:
                self.assertFalse(other.autostart.allowed)
                with self.assertRaises(ValueError):
                    other.autostart.enable()
                self.assertIsNone(getattr(other, "tray", None))     # make_server never starts a tray
            finally:
                other.pipeline.close()
                other.monitor.close()
                other.server_close()
                other.agent.commands.put(("close", None, None))
                other.agent.thread.join(timeout=2)

    def test_shutdown_path_is_shared(self):
        self.assertTrue(callable(self.server.shutdown_service))


if __name__ == "__main__":
    unittest.main()
