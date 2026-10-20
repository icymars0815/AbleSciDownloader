"""Configurable download judgement + high-speed modes (off / smart / force). Mocked; isolated temp dirs only."""
import json
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import browser_agent
from accounts import Accounts
from app import make_server
from browser_agent import BASE, BrowserAgent
from core import Store
from download_policy import (DOWNLOAD_DEFAULTS, DOWNLOAD_RANGES, RULES_VERSION, TransferWatch, download_settings,
                             highspeed_allowed, make_watch, migrate_preferences, resolve_highspeed,
                             validate_download_settings)

RULES = "用户积分＜500，高速下载扣 2 积分，同一文件不重复扣分；用户积分≥500，免积分。"
CONSENT = dict(rules_version=RULES_VERSION)


class SettingsValidationTests(unittest.TestCase):
    def test_defaults(self):
        self.assertEqual(10, DOWNLOAD_DEFAULTS["line_max_minutes"])
        settings = download_settings({})
        self.assertEqual(DOWNLOAD_DEFAULTS, settings)
        watch = make_watch(0, settings)
        self.assertEqual((600, 120, 300, 0.10), (watch.max_seconds, watch.stall_seconds, watch.committed_stall_seconds, watch.committed_fraction))
        self.assertEqual(20 * 1024, watch.slow_threshold_bps)

    def test_every_boundary(self):
        for key, (low, high) in DOWNLOAD_RANGES.items():
            self.assertEqual({"download_" + key: low}, validate_download_settings({key: low}))
            self.assertEqual({"download_" + key: high}, validate_download_settings({key: high}))
            for bad in (low - 1, high + 1, "5", 5.5, True, None, [low]):
                with self.assertRaises(ValueError, msg=(key, bad)):
                    validate_download_settings({key: bad})
        with self.assertRaises(ValueError):
            validate_download_settings({"smart_eta_check": "yes"})
        with self.assertRaises(ValueError):
            validate_download_settings({"unknown": 1})
        with self.assertRaises(ValueError):
            validate_download_settings({})

    def test_damaged_preference_falls_back_to_default(self):
        settings = download_settings({"download_line_max_minutes": 9999, "download_stall_seconds": "x", "download_slow_kbps": 50})
        self.assertEqual((10, 120, 50), (settings["line_max_minutes"], settings["stall_seconds"], settings["slow_kbps"]))

    def test_settings_feed_the_watch(self):
        settings = download_settings({"download_line_max_minutes": 3, "download_stall_seconds": 30, "download_committed_percent": 50})
        watch = make_watch(0, settings)
        self.assertEqual("timeout", watch.observe(181, 1000))
        stall = make_watch(0, settings)
        self.assertEqual("stalled", stall.observe(31, 0))


class EtaTests(unittest.TestCase):
    TOTAL = 100 * 1024 * 1024

    def watch(self, eta=True, minutes=10):
        return make_watch(0, dict(DOWNLOAD_DEFAULTS, line_max_minutes=minutes), eta)

    def test_slow_line_is_failed_after_observing_ninety_seconds(self):
        watch = self.watch()
        self.assertNotEqual("eta", watch.observe(30, 500_000, "", self.TOTAL))
        self.assertNotEqual("eta", watch.observe(89, 1_480_000, "", self.TOTAL))    # not observed for 90 s yet
        self.assertEqual("eta", watch.observe(90, 1_500_000, "", self.TOTAL))

    def test_fast_line_is_kept(self):
        watch = self.watch()
        watch.observe(30, 15_000_000, "", self.TOTAL)
        self.assertEqual("receiving", watch.observe(60, 30_000_000, "", self.TOTAL))   # ~500 KB/s, 140 s left

    def test_check_off_or_total_unknown_never_eta(self):
        off = self.watch(eta=False)
        off.observe(30, 500_000, "", self.TOTAL)
        self.assertNotEqual("eta", off.observe(60, 1_000_000, "", self.TOTAL))
        unknown = self.watch()
        unknown.observe(30, 500_000, "", 0)
        self.assertNotEqual("eta", unknown.observe(60, 1_000_000, "", 0))

    def test_preference_switch_disables_eta(self):
        watch = make_watch(0, dict(DOWNLOAD_DEFAULTS, smart_eta_check=False), True)
        self.assertFalse(watch.eta_check)

    def test_cap_is_the_configured_line_limit(self):
        # 11 MB left at 100 KB/s = 110 s: fine under a 3 min cap; a 1 GB file at that speed is not (even with the size-aware cap).
        watch = self.watch(minutes=3)
        watch.observe(60, 6_000_000, "", 20_000_000)
        self.assertNotEqual("eta", watch.observe(90, 9_000_000, "", 20_000_000))
        slow = self.watch(minutes=3)
        slow.observe(60, 6_000_000, "", 1 << 30)
        self.assertEqual("eta", slow.observe(90, 9_000_000, "", 1 << 30))


class HighspeedGateTests(unittest.TestCase):
    def test_fee_text_required_in_smart_and_force(self):
        changed = RULES.replace("2 积分", "3 积分")
        for mode, failed in (("smart", [1, 2, 3]), ("force", [])):
            self.assertTrue(highspeed_allowed(failed, CONSENT, RULES, mode=mode, points=100)[0], mode)
            self.assertFalse(highspeed_allowed(failed, CONSENT, changed, mode=mode, points=100)[0], mode)
            self.assertFalse(highspeed_allowed(failed, CONSENT, "无费用说明", mode=mode, points=100)[0], mode)
            # even a free account (>= 500) must see the tariff text
            self.assertFalse(highspeed_allowed(failed, {}, changed, mode=mode, points=900)[0], mode)
            self.assertFalse(highspeed_allowed(failed, {}, "", mode=mode, points=900)[0], mode)

    def test_consent_required_below_500_in_smart_and_force(self):
        for mode, failed in (("smart", [1, 2, 3]), ("force", [])):
            for points in (None, 0, 499):
                self.assertFalse(highspeed_allowed(failed, {}, RULES, mode=mode, points=points)[0], (mode, points))
                self.assertFalse(highspeed_allowed(failed, dict(rules_version="old"), RULES, mode=mode, points=points)[0])
            self.assertTrue(highspeed_allowed(failed, {}, RULES, mode=mode, points=500)[0], mode)    # free: tariff text suffices

    def test_one_click_rule_smart_needs_three_failures_force_does_not(self):
        self.assertFalse(highspeed_allowed([1, 2], CONSENT, RULES, mode="smart", points=100)[0])
        self.assertFalse(highspeed_allowed([1, 1, 2], CONSENT, RULES, mode="smart", points=100)[0])
        self.assertFalse(highspeed_allowed([1, 2, 3], CONSENT, RULES, mode="off", points=900)[0])
        self.assertTrue(highspeed_allowed([], CONSENT, RULES, mode="force", points=100)[0])

    def test_legacy_callers_derive_mode_from_preferences(self):
        self.assertFalse(highspeed_allowed([1, 2, 3], {}, RULES)[0])
        self.assertTrue(highspeed_allowed([1, 2, 3], dict(allow_paid_fallback=True, **CONSENT), RULES)[0])
        self.assertFalse(highspeed_allowed([], dict(allow_paid_fallback=True, **CONSENT), RULES)[0])   # smart, not force
        self.assertTrue(highspeed_allowed([], dict(download_highspeed="force", **CONSENT), RULES)[0])


class ModeResolutionTests(unittest.TestCase):
    def test_default_by_points(self):
        self.assertEqual(("force", "default"), resolve_highspeed({}, 500)[:2])
        self.assertEqual(("force", "default"), resolve_highspeed({}, 2000)[:2])
        for points in (499, 0, None, True, "800"):
            self.assertEqual(("off", "default"), resolve_highspeed({}, points)[:2], points)

    def test_user_choice_is_respected(self):
        for chosen in ("off", "smart", "force"):
            prefs = dict(download_highspeed=chosen, download_highspeed_chosen=True)
            for points in (10, 900):
                self.assertEqual((chosen, "user"), resolve_highspeed(prefs, points)[:2])

    def test_falls_back_to_off_when_points_drop(self):
        prefs = {}
        mode, source, changes, message = resolve_highspeed(prefs, 700)
        self.assertEqual(("force", {"download_default_force_applied": True}, ""), (mode, changes, message))
        prefs.update(changes)
        mode, source, changes, message = resolve_highspeed(prefs, 120)
        self.assertEqual(("off", "default", {"download_default_force_applied": False}), (mode, source, changes))
        self.assertIn("回落", message)
        prefs.update(changes)
        self.assertEqual("off", resolve_highspeed(prefs, 120)[0])
        self.assertEqual("", resolve_highspeed(prefs, 120)[3])         # reported once
        # a user's own force does not fall back
        self.assertEqual("force", resolve_highspeed(dict(download_highspeed="force", download_highspeed_chosen=True,
                                                         download_default_force_applied=True), 120)[0])

    def test_legacy_allow_paid_fallback_migration(self):
        for legacy, expected in ((True, "smart"), (False, "off")):
            with tempfile.TemporaryDirectory() as temp:
                store = Store(temp)
                store.update_preferences(allow_paid_fallback=legacy)
                migrate_preferences(store)
                prefs = store.snapshot()["preferences"]
                self.assertEqual((expected, True), (prefs["download_highspeed"], prefs["download_highspeed_chosen"]))
                self.assertEqual((expected, "user"), resolve_highspeed(prefs, 900)[:2])      # an explicit old choice beats the default
                migrate_preferences(store)
                self.assertEqual(expected, store.snapshot()["preferences"]["download_highspeed"])
        with tempfile.TemporaryDirectory() as temp:
            store = Store(temp)
            migrate_preferences(store)
            self.assertNotIn("download_highspeed", store.snapshot().get("preferences", {}))
        # without migration the pure resolver already understands the legacy flag
        self.assertEqual(("smart", "user"), resolve_highspeed(dict(allow_paid_fallback=True), 10)[:2])


class AgentFlowTests(unittest.TestCase):
    """download() with a mocked page: which calls reach highspeed_allowed, and whether the high-speed button is clicked."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.agent = BrowserAgent(self.store, Accounts(self.temp.name))
        self.addCleanup(self.agent.thread.join, 0.1)
        self.addCleanup(self.agent.commands.put, ("close", None, None))
        self.store.update_preferences(download_probe_enabled=False)    # line probing has its own tests (test_publish_download.py)
        self.item = self.store.add("A paper", "", "website", account_id="default", assist_url=BASE + "/assist/detail?id=ABC", owned=True,
                                   website_status="待确认")
        self.body = RULES
        self.clicked = []
        self.agent.goto = MagicMock()
        self.agent.context = MagicMock()
        self.agent.page = self.make_page()
        self.agent.line_control = MagicMock(return_value=MagicMock(**{"get_attribute.return_value": "false", "is_checked.return_value": False}))
        self.agent.save_download = MagicMock()
        self.waits = []

        def wait(item_id, label, pending, settings=None, eta=False):
            self.waits.append((label, eta))
            outcome = self.outcomes.pop(0)
            if outcome == "complete":
                pending.append(MagicMock())
            return outcome
        self.agent.wait_transfer = wait
        self.outcomes = []

    def make_page(self):
        page = MagicMock()
        page.url = BASE + "/assist/download?id=FILE1"

        def table_locator(selector, **_):
            m = MagicMock()
            if selector.startswith("a["):
                m.count.return_value = 1
                m.first.get_attribute.return_value = "/assist/download?id=FILE1"
            else:
                m.filter.return_value.count.return_value = 0
            return m

        def locator(selector, **_):
            m = MagicMock()
            if selector == "body":
                m.inner_text.side_effect = lambda *a, **k: self.body
            elif selector == "table":
                m.first.locator.side_effect = table_locator
                m.first.inner_text.return_value = "table"
            elif "file_highspeed" in selector:
                m.count.return_value = 1
                m.get_attribute.return_value = "0"
            elif selector == "#download-highspeed-direct":
                m.count.return_value = 1
                m.click.side_effect = lambda: self.clicked.append("highspeed")
            return m
        page.locator.side_effect = locator
        return page

    def run_download(self):
        with patch("browser_agent.highspeed_allowed", wraps=highspeed_allowed) as spy:
            try:
                self.agent.download(self.item)
                error = None
            except ValueError as exc:
                error = str(exc)
        return spy, error

    def prefs(self, **fields):
        self.store.update_preferences(**fields)

    def test_force_default_at_500_goes_straight_to_highspeed(self):
        self.store.update_account("default", points=800)
        self.agent.read_points = MagicMock(return_value=800)                   # live balance read for THIS download
        self.outcomes = ["complete"]
        spy, error = self.run_download()
        self.assertIsNone(error)
        self.assertEqual([("高速通道", False)], self.waits)                      # no normal line attempted
        args, kwargs = spy.call_args
        self.assertEqual(([], "force", 800), (args[0], kwargs["mode"], kwargs["points"]))
        self.assertEqual(["highspeed"], self.clicked)
        attempt = self.store.get(self.item)["download_attempts"][0]
        self.assertEqual(("highspeed", "force", "forced"), (attempt["line"], attempt["mode"], attempt["reason"]))
        self.assertTrue(self.store.snapshot()["preferences"]["download_default_force_applied"])

    def test_force_still_needs_fee_text(self):
        self.store.update_account("default", points=800)
        self.agent.read_points = MagicMock(return_value=800)
        self.body = "高速下载扣 5 积分"
        self.outcomes = ["stalled", "stalled", "stalled"]
        spy, error = self.run_download()
        self.assertIn("强制高速未通过费用核对", error)                         # falls back to normal lines, never clicks
        self.assertEqual([], self.clicked)
        self.assertEqual(["普通线路 1", "普通线路 2", "普通线路 3"], [w[0] for w in self.waits])
        spy.assert_called_once()

    def test_force_waiver_ignores_stale_stored_points(self):
        # Stored 800 but no live balance: the >=500 no-consent waiver must not apply (user may now be charged).
        self.store.update_account("default", points=800)
        self.agent.read_points = MagicMock(return_value=None)
        self.outcomes = ["complete"]
        spy, error = self.run_download()
        self.assertIsNone(error)
        self.assertEqual([], self.clicked)
        self.assertEqual("普通线路 1", self.waits[0][0])
        self.assertIsNone(spy.call_args.kwargs["points"])

    def test_force_chosen_below_500_without_consent_is_blocked(self):
        self.store.update_account("default", points=100)
        self.prefs(download_highspeed="force", download_highspeed_chosen=True)
        self.outcomes = ["complete"]
        spy, error = self.run_download()
        self.assertIsNone(error)                                                 # normal line 1 completed instead
        self.assertEqual([], self.clicked)
        self.assertIn("改走普通线路", " ".join(e["message"] for e in self.store.snapshot()["events"]))

    def test_force_chosen_below_500_with_consent_clicks_once(self):
        self.store.update_account("default", points=100)
        self.prefs(download_highspeed="force", download_highspeed_chosen=True, rules_version=RULES_VERSION)
        self.outcomes = ["complete"]
        spy, error = self.run_download()
        self.assertIsNone(error)
        self.assertEqual(["highspeed"], self.clicked)

    def test_forced_highspeed_failure_falls_back_to_free_lines_without_second_click(self):
        # Real 2026-10-20: forced high speed crawled at ~20 KB/s, restarted and timed out.
        self.store.update_account("default", points=100)
        self.prefs(download_highspeed="force", download_highspeed_chosen=True, rules_version=RULES_VERSION)
        self.outcomes = ["timeout", "complete"]
        spy, error = self.run_download()
        self.assertIsNone(error)
        self.assertEqual(["highspeed"], self.clicked)                 # never a second paid click
        self.assertEqual("高速通道", self.waits[0][0])
        self.assertTrue(self.waits[1][0].startswith("普通线路"))
        self.assertEqual(1, len(self.store.get(self.item)["highspeed_paid"]))

    def test_smart_after_three_failures_reaches_highspeed_allowed(self):
        self.store.update_account("default", points=100)
        self.prefs(download_highspeed="smart", download_highspeed_chosen=True, rules_version=RULES_VERSION)
        self.outcomes = ["eta", "stalled", "error", "complete"]
        spy, error = self.run_download()
        self.assertIsNone(error)
        self.assertEqual([("普通线路 1", True), ("普通线路 2", True), ("普通线路 3", True), ("高速通道", False)], self.waits)
        args, kwargs = spy.call_args
        self.assertEqual(([1, 2, 3], "smart"), (args[0], kwargs["mode"]))
        attempts = self.store.get(self.item)["download_attempts"]
        self.assertEqual(["eta", "stalled", "error", "error"], [a["reason"] for a in attempts])
        self.assertTrue(all(a["mode"] == "smart" for a in attempts))
        self.assertEqual(["highspeed"], self.clicked)

    def test_smart_fee_text_mismatch_does_not_click(self):
        self.store.update_account("default", points=100)
        self.prefs(download_highspeed="smart", download_highspeed_chosen=True, rules_version=RULES_VERSION)
        self.outcomes = ["timeout", "timeout", "timeout"]
        self.body = "高速下载扣 9 积分"
        spy, error = self.run_download()
        self.assertIn("3 条普通线路均未完成", error)
        self.assertEqual([], self.clicked)

    def test_smart_normal_line_success_never_evaluates_highspeed(self):
        self.store.update_account("default", points=100)
        self.prefs(download_highspeed="smart", download_highspeed_chosen=True, rules_version=RULES_VERSION)
        self.outcomes = ["eta", "complete"]
        spy, error = self.run_download()
        self.assertIsNone(error)
        spy.assert_not_called()
        self.assertEqual([], self.clicked)

    def test_off_never_uses_highspeed_and_no_eta(self):
        self.store.update_account("default", points=100)
        self.outcomes = ["stalled", "stalled", "timeout"]
        spy, error = self.run_download()
        self.assertIn("3 条普通线路均未完成", error)
        self.assertEqual([], self.clicked)
        self.assertTrue(all(eta is False for _, eta in self.waits))
        self.assertEqual("off", self.store.get(self.item)["download_attempts"][0]["mode"])

    def test_points_drop_falls_back_to_off_with_event(self):
        self.store.update_account("default", points=900)
        self.prefs(download_default_force_applied=True)
        self.body = RULES + "\n当前拥有 120 积分"                                     # live read on the settings page
        self.outcomes = ["stalled", "stalled", "stalled"]
        spy, error = self.run_download()
        self.assertIn("3 条普通线路均未完成", error)
        self.assertEqual([], self.clicked)
        self.assertEqual(120, self.store.snapshot()["account"]["points"])
        self.assertFalse(self.store.snapshot()["preferences"]["download_default_force_applied"])
        self.assertTrue(any("回落" in e["message"] for e in self.store.snapshot()["events"]))

    def test_assist_my_set_smart_highspeed_must_be_off(self):
        self.store.update_account("default", points=900)
        original = self.agent.page.locator.side_effect

        def locator(selector, **kw):
            m = original(selector, **kw)
            if "file_highspeed" in selector:
                m.get_attribute.return_value = "1"
            return m
        self.agent.page.locator.side_effect = locator
        spy, error = self.run_download()
        self.assertIn("智能高速下载”未确认关闭", error)
        self.assertEqual([], self.waits + self.clicked)

    def test_configured_settings_reach_wait_transfer(self):
        seen = {}

        def wait(item_id, label, pending, settings=None, eta=False):
            seen.update(settings)
            pending.append(MagicMock())
            return "complete"
        self.agent.wait_transfer = wait
        self.prefs(download_line_max_minutes=7, download_stall_seconds=45)
        self.run_download()
        self.assertEqual((7, 45), (seen["line_max_minutes"], seen["stall_seconds"]))


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.start()

    def start(self):
        self.server = make_server(0, self.temp.name)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = "http://127.0.0.1:%d" % self.server.server_port
        self.token = urlopen(self.base).read().decode().split("const TOKEN=")[1].split(";let state=")[0].strip('"')
        self.addCleanup(self.stop, self.server)

    def stop(self, server):
        server.pipeline.close()
        server.shutdown()
        server.server_close()
        server.agent.commands.put(("close", None, None))
        server.agent.thread.join(timeout=2)

    def post(self, route, payload=None, expect=200):
        request = Request(self.base + "/api/" + route, json.dumps(payload or {}).encode(), method="POST",
                          headers={"Content-Type": "application/json", "Origin": self.base, "X-Demo-Token": self.token})
        try:
            return json.load(urlopen(request))
        except HTTPError as exc:
            self.assertEqual(expect, exc.code, exc.read())

    def state(self):
        return json.load(urlopen(self.base + "/api/state"))

    def prefs(self):
        return self.server.store.snapshot().get("preferences", {})

    def test_state_exposes_settings_ranges_and_default_mode(self):
        download = self.state()["download"]
        self.assertEqual(DOWNLOAD_DEFAULTS, download["settings"])
        self.assertEqual([3, 60], download["ranges"]["line_max_minutes"])
        self.assertEqual(("off", "default", False), (download["highspeed"]["mode"], download["highspeed"]["source"], download["highspeed"]["chosen"]))

    def test_default_force_at_500_and_user_choice_wins(self):
        self.server.store.update_account("default", points=620)
        high = self.state()["download"]["highspeed"]
        self.assertEqual(("force", "default"), (high["mode"], high["source"]))
        self.post("preferences", {"download_highspeed": "off"})
        high = self.state()["download"]["highspeed"]
        self.assertEqual(("off", "user", True), (high["mode"], high["source"], high["chosen"]))

    def test_points_below_500_default_off(self):
        self.server.store.update_account("default", points=499)
        self.assertEqual("off", self.state()["download"]["highspeed"]["mode"])

    def test_settings_range_validation_and_persist(self):
        self.post("preferences", {"download_settings": {"line_max_minutes": 15, "smart_eta_check": False}})
        settings = self.state()["download"]["settings"]
        self.assertEqual((15, False), (settings["line_max_minutes"], settings["smart_eta_check"]))
        self.assertEqual(15, self.prefs()["download_line_max_minutes"])
        for bad in ({"line_max_minutes": 2}, {"line_max_minutes": 61}, {"stall_seconds": 29}, {"committed_percent": 91},
                    {"slow_kbps": 0}, {"line_max_minutes": "10"}, {"line_max_minutes": True}, {"smart_eta_check": 1}, {"bogus": 1}, {}):
            self.post("preferences", {"download_settings": bad}, 400)
        self.assertEqual(15, self.state()["download"]["settings"]["line_max_minutes"])     # rejected writes change nothing

    def test_reset_to_defaults(self):
        self.post("preferences", {"download_settings": {"line_max_minutes": 30, "stall_seconds": 400}})
        self.post("preferences", {"download_reset": True})
        self.assertEqual(DOWNLOAD_DEFAULTS, self.state()["download"]["settings"])
        self.post("preferences", {"download_reset": False}, 400)

    def test_smart_and_force_below_500_need_rules_version(self):
        for mode in ("smart", "force"):
            self.post("preferences", {"download_highspeed": mode}, 400)
            self.post("preferences", {"download_highspeed": mode, "rules_version": "old"}, 400)
            self.assertNotEqual(mode, self.state()["download"]["highspeed"]["mode"])
        self.server.store.update_account("default", points=499)
        self.post("preferences", {"download_highspeed": "force"}, 400)
        self.post("preferences", {"download_highspeed": "force", "rules_version": RULES_VERSION})
        self.assertEqual(("force", "user"), tuple(self.state()["download"]["highspeed"][k] for k in ("mode", "source")))
        self.post("preferences", {"download_highspeed": "off"})                         # turning off needs no confirmation
        self.assertEqual("", self.prefs()["rules_version"])
        self.post("preferences", {"download_highspeed": "turbo"}, 400)

    def test_force_at_500_needs_no_click_through_but_default_force_cannot_be_smuggled_below(self):
        self.server.store.update_account("default", points=900)
        self.post("preferences", {"download_highspeed": "force"})
        self.assertEqual("force", self.state()["download"]["highspeed"]["mode"])
        self.server.store.update_account("default", points=100)
        # chosen force at < 500 with no recorded consent still fails closed in the gate
        self.assertFalse(highspeed_allowed([], self.prefs(), RULES, mode="force", points=100)[0])

    def test_legacy_toggle_maps_to_modes(self):
        self.post("preferences", {"allow_paid_fallback": True, "rules_version": RULES_VERSION})
        self.assertEqual(("smart", "user"), tuple(self.state()["download"]["highspeed"][k] for k in ("mode", "source")))
        self.post("preferences", {"allow_paid_fallback": False})
        self.assertEqual("off", self.state()["download"]["highspeed"]["mode"])

    def test_startup_migrates_legacy_flag(self):
        self.server.store.update_preferences(allow_paid_fallback=True, rules_version=RULES_VERSION)
        self.stop(self.server)
        self.start()
        self.assertEqual("smart", self.prefs()["download_highspeed"])
        self.assertEqual("smart", self.state()["download"]["highspeed"]["mode"])


if __name__ == "__main__":
    unittest.main()


class LineContextTests(unittest.TestCase):
    def test_switch_context_cleared_before_patient_and_highspeed(self):
        # Real 2026-10-20: the free-line 'switch' rule leaked into patient waiting and high speed and cut both off.
        import re
        source = open(__import__("browser_agent").__file__, encoding="utf-8").read()
        self.assertRegex(source, r"self\._line_ctx = None\s+# patient mode")
        self.assertRegex(source, r"self\._line_ctx = None\s+# the free-line switch rule never applies to high speed\s+speed_button\.click\(\)")
