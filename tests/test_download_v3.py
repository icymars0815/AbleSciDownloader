"""Line selection, original filenames, no double highspeed, failure messages, title fill, CORE key handling. All mocked."""
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.request import Request, urlopen

import download_lines as dl
from app import make_server
from core import Store
from download_policy import RULES_VERSION, validate_download_settings, download_settings
from pipeline import Pipeline, stage_of
from accounts import Accounts
from tests import test_download_settings as tds
from tests import test_oa_fetch as toa


class FakeStore:
    def __init__(self):
        self.lock = threading.RLock()
        self.data = {}

    def save(self):
        pass


class SelectLineTests(unittest.TestCase):
    def probe_of(self, speeds, log):
        def probe(line, secs):
            log.append((line, secs))
            return speeds[line]
        return probe

    def test_full_probe_picks_fastest(self):
        log = []
        result = dl.select_line([2, 1, 3], self.probe_of({1: 4000, 2: 900, 3: 12000}, log), {}, 15)
        self.assertEqual((3, "full"), (result["chosen"], result["mode"]))
        self.assertEqual([(2, 15), (1, 15), (3, 15)], log)
        self.assertEqual([3, 1, 2], dl.order_after(3, result["measured"]))

    def test_history_starts_with_fastest_and_shortens_probe(self):
        log = []
        result = dl.select_line([1, 2, 3], self.probe_of({1: 100, 2: 9000, 3: 100}, log), {1: 500, 2: 10000, 3: 800}, 15)
        self.assertEqual(([(2, 10)], 2, "history"), (log, result["chosen"], result["mode"]))

    def test_history_favourite_gone_slow_falls_back_to_full(self):
        log = []
        result = dl.select_line([1, 2, 3], self.probe_of({1: 6000, 2: 100, 3: 300}, log), {1: 500, 2: 10000, 3: 800}, 30)
        self.assertEqual((1, "history_then_full"), (result["chosen"], result["mode"]))
        self.assertEqual({10}, {s for _, s in log})

    def test_finished_during_probe_stops_probing(self):
        log = []
        result = dl.select_line([1, 2, 3], lambda line, secs: log.append(line) or None, {}, 15)
        self.assertEqual(([1], True), (log, result["done"]))

    def test_switch_rule_needs_sixty_seconds_below_half(self):
        watch = dl.SwitchWatch(1, {1: 5000, 2: 20000, 3: 1000})
        self.assertIsNone(watch.observe(0, 11000))           # >= half of 20000 -> fine
        self.assertIsNone(watch.observe(10, 4000))
        self.assertIsNone(watch.observe(69, 4000))
        self.assertEqual(2, watch.observe(70, 4000))
        watch = dl.SwitchWatch(1, {1: 5000, 2: 20000})
        watch.observe(0, 1000)
        watch.observe(30, 15000)                              # recovered: timer resets
        self.assertIsNone(watch.observe(100, 1000))
        self.assertIsNone(dl.SwitchWatch(1, {1: 5000}).observe(0, 1))   # no other line with history

    def test_history_by_hour_is_persisted_and_recent_only(self):
        with tempfile.TemporaryDirectory() as temp:
            store = Store(temp)
            history = dl.SpeedHistory(store, clock=lambda: 1_800_000_000)
            history.record(2, 8000, 1_800_000_000 - 3600 * 24)       # same hour yesterday
            history.record(2, 4000, 1_800_000_000 - 3600 * 24 * 2)
            history.record(1, 9000, 1_800_000_000 - 3600 * 24 * 30)  # too old
            history.record(3, 7000, 1_800_000_000 - 3600 * 5)        # different hour
            again = dl.SpeedHistory(Store(temp), clock=lambda: 1_800_000_000)
            self.assertEqual(6000, again.expected(2))
            self.assertIsNone(again.expected(1))
            self.assertIsNone(again.expected(3))

    def test_probe_settings_validated(self):
        self.assertEqual(15, download_settings({})["probe_seconds"])
        self.assertTrue(download_settings({})["probe_enabled"])
        self.assertEqual({"download_probe_seconds": 5}, validate_download_settings({"probe_seconds": 5}))
        for bad in ({"probe_seconds": 4}, {"probe_seconds": 61}, {"probe_seconds": "15"}, {"probe_enabled": 1}):
            with self.assertRaises(ValueError):
                validate_download_settings(bad)

    def test_size_text_and_failure_texts(self):
        self.assertIn("14.9 MB", dl.plan_text(int(14.9 * 1024 * 1024), 10 * 1024, 10))
        self.assertIn("智能高速", dl.plan_text(15 * 1024 * 1024, 10 * 1024, 10))
        self.assertIn("暂无速度", dl.plan_text(1000, None, 10))
        cases = (("3 条普通线路均未完成", "site_slow"), ("页面提示失败", "line_error"), ("官网文件已删除", "file_deleted"),
                 ("高速通道未完成，已停止", "highspeed_failed"), ("下载已停止", "stopped"), ("收到文件但 PDF 解析失败", "bad_file"))
        for text, kind in cases:
            self.assertEqual(kind, dl.explain_failure(text)["kind"], text)
        self.assertEqual("login_expired", dl.explain_failure("账户会话已失效，请点击账户登录")["kind"])
        self.assertTrue(all(dl.explain_failure(t)["advice"] for t, _ in cases))

    def test_safe_filename_only_replaces_illegal_characters(self):
        self.assertEqual("Smith et al. 2020 - Is it good_ A study.pdf", dl.safe_filename("Smith et al. 2020 - Is it good? A study.pdf"))
        self.assertEqual("c.pdf", dl.safe_filename("a/b\\c.pdf"))
        self.assertEqual("_CON.pdf", dl.safe_filename("CON.pdf"))
        self.assertEqual("x.pdf", dl.safe_filename("x.pdf. "))
        self.assertEqual("", dl.safe_filename("..."))
        self.assertLessEqual(len(dl.safe_filename("a" * 400 + ".pdf")), 150)


class AgentTests(tds.AgentFlowTests):
    """Reuses the mocked download harness of test_download_settings."""

    def setUp(self):
        super().setUp()

    def test_original_filename_is_kept_under_item_folder(self):
        from browser_agent import BrowserAgent
        agent = BrowserAgent.__new__(BrowserAgent)
        agent.store, agent.stop = self.store, threading.Event()
        agent.reject = MagicMock()
        download = MagicMock()
        download.suggested_filename = 'Smith 2020: "Citrus" <v2>.pdf'
        download.save_as.side_effect = lambda path: Path(path).write_bytes(b"%PDF-1.4 x")
        with patch("browser_agent.verify_with_metadata", return_value=dict(status="review", reason="x", sha256="y")):
            agent.save_download(self.item, download, "Paper", "10.1234/ok")
        after = self.store.get(self.item)
        self.assertEqual('Smith 2020_ _Citrus_ _v2_.pdf', after["local_filename"])
        self.assertEqual('Smith 2020: "Citrus" <v2>.pdf', after["original_filename"])
        self.assertEqual(Path("downloads") / self.item / after["local_filename"], Path(after["pdf"]))
        self.assertTrue((self.store.root / after["pdf"]).is_file())
        # a second save of the same name never overwrites
        download.suggested_filename = after["original_filename"]
        self.store.update(self.item, status="downloading")
        with patch("browser_agent.verify_with_metadata", return_value=dict(status="review", reason="x", sha256="y")):
            with self.assertRaises(ValueError):
                agent.save_download(self.item, download, "Paper", "10.1234/ok")

    def test_old_style_item_is_untouched(self):
        old = self.store.add("Old", "10.1234/old", pdf="downloads/abc.pdf")
        self.assertEqual("downloads/abc.pdf", self.store.get(old)["pdf"])

    def test_highspeed_is_charged_once_per_file(self):
        self.store.update_account("default", points=100)
        self.prefs(download_highspeed="smart", download_highspeed_chosen=True, rules_version=RULES_VERSION)
        self.outcomes = ["stalled", "stalled", "stalled", "complete"]
        spy, error = self.run_download()
        self.assertIsNone(error)
        self.assertEqual(["highspeed"], self.clicked)
        paid = self.store.get(self.item)["highspeed_paid"]
        self.assertEqual(("FILE1", True), (paid[0]["file_id"], paid[0]["charged"]))
        # same file again: the site charges a file only once, so the next high-speed try is recorded as not charged
        self.store.update(self.item, status="ready", download_failure=None, pdf="")
        self.outcomes = ["stalled", "stalled", "stalled", "complete"]
        spy, error = self.run_download()
        self.assertIsNone(error)
        self.assertEqual(["highspeed", "highspeed"], self.clicked)
        self.assertEqual([True, False], [x["charged"] for x in self.store.get(self.item)["highspeed_paid"]])

    def test_force_mode_repeats_a_paid_file_without_charging(self):
        self.store.update_account("default", points=100)
        self.prefs(download_highspeed="force", download_highspeed_chosen=True, rules_version=RULES_VERSION)
        self.store.update(self.item, highspeed_paid=[dict(file_id="FILE1", time="t", charged=True, mode="force")], highspeed_clicks=1)
        self.outcomes = ["complete"]
        spy, error = self.run_download()
        self.assertIsNone(error)
        self.assertEqual(["highspeed"], self.clicked)
        self.assertEqual([True, False], [x["charged"] for x in self.store.get(self.item)["highspeed_paid"]])

    def test_item_with_local_pdf_is_not_downloaded_again(self):
        self.store.update(self.item, pdf="downloads/x/a.pdf")
        spy, error = self.run_download()
        self.assertIn("不会重复下载", error)
        self.assertEqual([], self.waits)

    def test_failure_is_explained_and_recorded(self):
        self.store.update_account("default", points=100)
        self.outcomes = ["stalled", "stalled", "stalled"]
        spy, error = self.run_download()
        self.assertIn("建议", error)
        failure = self.store.get(self.item)["download_failure"]
        self.assertEqual("site_slow", failure["kind"])
        self.assertTrue(failure["advice"])

    def test_choose_line_clicks_fastest_and_records_history(self):
        self.agent.line_control = MagicMock()
        speeds = {1: 2000, 2: 30000, 3: 500}
        order_seen = []
        self.agent.line_speed_sample = lambda pending, secs: speeds[order_seen[-1]]
        clicked = []
        self.agent.line_control = lambda n: MagicMock(click=lambda: (clicked.append(n), order_seen.append(n)))
        order_seen.append(1)
        history = dl.SpeedHistory(self.store)
        order, active = self.agent.choose_line(self.item, 1, download_settings({}), [], history)
        self.assertEqual(([2, 1, 3], 2), (order, active))
        self.assertEqual([2, 3, 2], clicked)
        self.assertEqual(30000, history.expected(2))
        probe = self.store.get(self.item)["line_probe"]
        self.assertEqual((2, "full"), (probe["chosen"], probe["mode"]))


class TitleAndCoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.pipeline = Pipeline(self.store, Accounts(self.temp.name), MagicMock(), start=False)

    RECORD = dict(status="available", doi="10.1234/oa.test", title="Dietary fibre and gut microbiota", authors=["A One", "B Two"],
                  year=2020, sources=["OpenAlex"], retraction=dict(retracted=False, updates=[]), fetched_at="2026-10-19T10:00:00+00:00")

    def test_doi_only_import_gets_title_and_authors(self):
        item_id = self.store.add("", "10.1234/oa.test", account_id="default")
        with patch("pipeline.fetch_metadata", return_value=self.RECORD) as fetched:
            self.pipeline.fill_from_metadata(item_id, "me@example.org")
        fetched.assert_called_once_with("10.1234/oa.test", self.store.root, "me@example.org")
        item = self.store.get(item_id)
        self.assertEqual(("Dietary fibre and gut microbiota", ["A One", "B Two"]), (item["title"], item["authors"]))
        self.assertEqual("Dietary fibre and gut microbiota", item["metadata_ref"]["title"])

    def test_existing_title_is_not_replaced_and_no_lookup(self):
        item_id = self.store.add("My own title", "10.1234/oa.test", account_id="default")
        with patch("pipeline.fetch_metadata") as fetched:
            self.assertIsNone(self.pipeline.fill_from_metadata(item_id))
        fetched.assert_not_called()

    def test_unavailable_metadata_leaves_item_alone(self):
        item_id = self.store.add("", "10.1234/oa.test", account_id="default")
        with patch("pipeline.fetch_metadata", return_value=dict(status="unavailable")):
            self.assertIsNone(self.pipeline.fill_from_metadata(item_id))
        self.assertEqual("", self.store.get(item_id)["title"])

    def test_oa_run_fills_title_first_and_passes_core_key_cache_and_email(self):
        self.store.update_preferences(core_api_key="CORE-SECRET-123", oa_contact_email="me@example.org")
        item_id = self.store.add("", "10.1234/oa.test", account_id="default", request_confirmed=True)
        seen = {}

        def fetch(doi, dest, email="", **kwargs):
            seen.update(email=email, **kwargs)
            Path(dest).write_bytes(b"%PDF-1.7\n" + b"x" * 100)
            return dict(status="downloaded", oa_status="green", source="CORE", attempts=[], version="acceptedVersion", cached=True,
                        fetched_at="2026-10-19T09:00:00+00:00")
        verified = {}

        def verify(path, title, doi, root, **kwargs):
            verified.update(title=title, **kwargs)
            return dict(status="identity_match", sha256="x")
        with patch("pipeline.fetch_metadata", return_value=self.RECORD), patch("oa_fetch.fetch_open_access", fetch), \
                patch("pipeline.verify_with_metadata", verify):
            self.pipeline.oa_run(item_id)
        self.assertEqual("CORE-SECRET-123", seen["core_api_key"])
        self.assertEqual(self.store.root, seen["cache_root"])
        self.assertEqual("me@example.org", seen["email"])
        self.assertEqual(("Dietary fibre and gut microbiota", "me@example.org"), (verified["title"], verified["email"]))
        check = self.store.get(item_id)["oa_check"]
        self.assertEqual(("acceptedVersion", True, "2026-10-19T09:00:00+00:00"), (check["version"], check["cached"], check["fetched_at"]))
        self.assertNotIn("CORE-SECRET-123", json.dumps(self.store.snapshot()["events"]))


class CoreKeyApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.server = make_server(0, self.temp.name, auto_screen=False)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = "http://127.0.0.1:%d" % self.server.server_port
        self.token = urlopen(self.base).read().decode().split("const TOKEN=")[1].split(";let state=")[0].strip('"')
        self.addCleanup(self.stop)

    def stop(self):
        self.server.pipeline.close()
        self.server.shutdown()
        self.server.server_close()
        self.server.agent.commands.put(("close", None, None))
        self.server.agent.thread.join(timeout=2)

    def post(self, route, body):
        request = Request(self.base + "/api/" + route, json.dumps(body).encode(), {"Content-Type": "application/json", "Origin": self.base, "X-Demo-Token": self.token})
        return json.load(urlopen(request))

    def state_text(self):
        return urlopen(self.base + "/api/state").read().decode()

    def test_core_key_never_returned_in_state_or_events(self):
        self.assertFalse(json.loads(self.state_text())["pipeline"]["core_key_configured"])
        self.post("pipeline", {"core_api_key": "CORE-SECRET-123"})
        text = self.state_text()
        self.assertNotIn("CORE-SECRET-123", text)
        self.assertTrue(json.loads(text)["pipeline"]["core_key_configured"])
        self.assertEqual("CORE-SECRET-123", self.server.store.snapshot()["preferences"]["core_api_key"])
        self.post("pipeline", {"core_api_key": ""})
        self.assertFalse(json.loads(self.state_text())["pipeline"]["core_key_configured"])

    def test_bad_core_key_rejected(self):
        from urllib.error import HTTPError
        for bad in ("has space", 5, "x" * 300):
            with self.assertRaises(HTTPError):
                self.post("pipeline", {"core_api_key": bad})

    def test_clear_oa_cache_endpoint(self):
        folder = self.server.store.root / "cache" / "oa"
        folder.mkdir(parents=True)
        (folder / "a.json").write_text("{}")
        self.post("oa-cache-clear", {})
        self.assertFalse((folder / "a.json").exists())

    def test_open_only_inside_store_root(self):
        opened = []
        self.server.open_path = opened.append
        store = self.server.store
        folder = store.root / "downloads" / "i1"
        folder.mkdir(parents=True)
        (folder / "My Paper.pdf").write_bytes(b"%PDF-1.4")
        good = store.add("P", "10.1234/p", "website", account_id="default", pdf="downloads/i1/My Paper.pdf")
        self.post("open", {"id": good, "target": "pdf"})
        self.post("open", {"id": good, "target": "folder"})
        self.assertEqual([str((folder / "My Paper.pdf").resolve()), str(folder.resolve())], opened)
        outside = Path(self.temp.name).parent / "outside.pdf"
        outside.write_bytes(b"%PDF-1.4")
        self.addCleanup(outside.unlink)
        bad = store.add("Q", "10.1234/q", "website", account_id="default", pdf="..\\outside.pdf")
        from urllib.error import HTTPError
        with self.assertRaises(HTTPError):
            self.post("open", {"id": bad, "target": "pdf"})
        self.assertEqual(2, len(opened))

    def test_state_has_download_probe_settings(self):
        state = json.loads(self.state_text())
        self.assertEqual((True, 15), (state["download"]["settings"]["probe_enabled"], state["download"]["settings"]["probe_seconds"]))
        self.assertEqual([5, 60], state["download"]["ranges"]["probe_seconds"])


if __name__ == "__main__":
    unittest.main()
