"""Open-access first path. Everything is mocked: no network, no browser, never touches D:\\KYTDL\\data."""
import json
import queue
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import oa_fetch
from accounts import Accounts
from core import Store
from oa_fetch import OAChallenge, OAError
from pipeline import Pipeline, next_step, stage_of

PDF = b"%PDF-1.7\n" + b"x" * 100
NOW = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)
DOI = "10.1234/oa.test"


def j(data):
    return 200, json.dumps(data).encode()


class Router:
    """Fake network: maps a URL substring to (status, body) or an exception."""
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def __call__(self, url, accept="*/*", max_bytes=0, timeout=0):
        self.calls.append(url)
        for key, value in self.routes.items():
            if key in url:
                if isinstance(value, Exception):
                    raise value
                return value
        raise OAError("HTTP 404", 404)


OPENALEX = {"open_access": {"is_oa": True, "oa_status": "gold"}, "ids": {},
            "best_oa_location": {"pdf_url": "https://pub.example/best.pdf", "landing_page_url": "https://pub.example/landing", "license": "cc-by"},
            "locations": [{"pdf_url": "https://pub.example/best.pdf", "landing_page_url": None, "source": {"type": "journal"}},
                          {"pdf_url": "https://other.example/o.pdf", "landing_page_url": None, "source": {"type": "journal"}},
                          {"pdf_url": "https://repo.example/r.pdf", "landing_page_url": "https://www.ncbi.nlm.nih.gov/pmc/articles/7019488", "source": {"type": "repository"}}]}


class FindTests(unittest.TestCase):
    def find(self, routes, email=""):
        router = Router(routes)
        with patch("oa_fetch._request", router):
            return oa_fetch.find_oa_locations(DOI, email), router

    def test_ordering_and_dedupe(self):
        result, router = self.find({"api.openalex.org": j(OPENALEX)})
        urls = [c["url"] for c in result["candidates"]]
        self.assertEqual("https://pub.example/best.pdf", urls[0])
        self.assertEqual(["https://europepmc.org/articles/PMC7019488?pdf=render", "https://pmc.ncbi.nlm.nih.gov/articles/PMC7019488/pdf/"], urls[1:3])
        self.assertEqual(["https://repo.example/r.pdf", "https://other.example/o.pdf"], urls[3:])
        self.assertEqual(len(urls), len(set(urls)))
        self.assertEqual((True, "gold", "cc-by", "PMC7019488"), (result["is_oa"], result["oa_status"], result["license"], result["pmcid"]))
        self.assertTrue(all(u.startswith(("http://", "https://")) for u in urls))
        self.assertFalse(any("sci-hub" in u.lower() for u in urls))

    def test_unpaywall_only_with_email(self):
        up = j({"is_oa": True, "best_oa_location": {"url_for_pdf": "https://up.example/u.pdf", "host_type": "publisher"}})
        result, router = self.find({"api.openalex.org": j(OPENALEX), "api.unpaywall.org": up})
        self.assertFalse(any("unpaywall" in u for u in router.calls))
        result, router = self.find({"api.openalex.org": j(OPENALEX), "api.unpaywall.org": up}, email="me@example.org")
        self.assertTrue(any("unpaywall" in u and "email=me@example.org" in u for u in router.calls))
        self.assertIn("https://up.example/u.pdf", [c["url"] for c in result["candidates"]])

    def test_europepmc_pmcid_and_semantic_scholar(self):
        epmc = j({"resultList": {"result": [{"doi": DOI, "pmcid": "PMC123456", "isOpenAccess": "Y", "fullTextUrlList": {"fullTextUrl": [
            {"documentStyle": "pdf", "availabilityCode": "OA", "url": "https://epmc.example/a.pdf"},
            {"documentStyle": "pdf", "availabilityCode": "S", "url": "https://paywalled.example/a.pdf"}]}}]}})
        s2 = j({"openAccessPdf": {"url": "https://s2.example/p.pdf"}, "externalIds": {}})
        oa = j({"open_access": {"is_oa": False}, "locations": [], "ids": {}})
        result, _ = self.find({"api.openalex.org": oa, "europepmc/webservices": epmc, "semanticscholar": s2})
        urls = [c["url"] for c in result["candidates"]]
        self.assertIn("https://epmc.example/a.pdf", urls)
        self.assertNotIn("https://paywalled.example/a.pdf", urls)
        self.assertIn("https://europepmc.org/articles/PMC123456?pdf=render", urls)
        self.assertTrue(result["is_oa"])
        self.assertLess(urls.index("https://europepmc.org/articles/PMC123456?pdf=render"), urls.index("https://s2.example/p.pdf"))

    def test_pmc_cloud_uses_matching_metadata_and_published_version(self):
        xml = (200, b'<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><CommonPrefixes><Prefix>PMC7019488.1/</Prefix></CommonPrefixes><CommonPrefixes><Prefix>PMC7019488.2/</Prefix></CommonPrefixes></ListBucketResult>')
        base = "s3://pmc-oa-opendata/PMC7019488."
        published = j({"pmcid": "PMC7019488", "doi": DOI, "is_manuscript": False, "pdf_url": base + "2/paper.pdf?md5=abc"})
        manuscript = j({"pmcid": "PMC7019488", "doi": DOI, "is_manuscript": True, "pdf_url": base + "1/paper.pdf"})
        result, _ = self.find({"api.openalex.org": j(OPENALEX), "metadata/PMC7019488.1.json": manuscript,
                               "metadata/PMC7019488.2.json": published, "pmc-oa-opendata.s3.amazonaws.com/?": xml})
        urls = [c["url"] for c in result["candidates"]]
        self.assertLess(urls.index("https://pmc-oa-opendata.s3.amazonaws.com/PMC7019488.2/paper.pdf"),
                        urls.index("https://pmc-oa-opendata.s3.amazonaws.com/PMC7019488.1/paper.pdf"))

    def test_pmc_cloud_rejects_wrong_doi_and_external_pdf(self):
        xml = (200, b'<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><CommonPrefixes><Prefix>PMC7019488.1/</Prefix></CommonPrefixes></ListBucketResult>')
        for metadata in ({"pmcid": "PMC7019488", "doi": "10.1234/wrong", "pdf_url": "s3://pmc-oa-opendata/PMC7019488.1/a.pdf"},
                         {"pmcid": "PMC7019488", "doi": DOI, "pdf_url": "https://untrusted.example/a.pdf"}):
            result, _ = self.find({"api.openalex.org": j(OPENALEX), "metadata/PMC7019488.1.json": j(metadata),
                                   "pmc-oa-opendata.s3.amazonaws.com/?": xml})
            self.assertFalse(any(c["source"] == "PMC Cloud" for c in result["candidates"]))

    def test_preprints(self):
        with patch("oa_fetch._request", Router({})):
            arxiv = oa_fetch.find_oa_locations("10.48550/arXiv.1706.03762")
        self.assertEqual("https://arxiv.org/pdf/1706.03762", arxiv["candidates"][0]["url"])
        bio = j({"collection": [{"version": "1"}, {"version": "3"}]})
        router = Router({"api.biorxiv.org/details/biorxiv/": bio})
        with patch("oa_fetch._request", router):
            result = oa_fetch.find_oa_locations("10.1101/2020.01.01.000001")
        self.assertEqual("https://www.biorxiv.org/content/10.1101/2020.01.01.000001v3.full.pdf", result["candidates"][0]["url"])
        self.assertFalse(any("medrxiv" in u for u in router.calls))

    def test_crossref_needs_open_license(self):
        link = [{"URL": "https://pub.example/cr.pdf", "content-type": "application/pdf"}, {"URL": "https://pub.example/cr.xml", "content-type": "text/xml"}]
        closed = j({"message": {"link": link, "license": [{"URL": "https://publisher.example/tdm"}]}})
        opened = j({"message": {"link": link, "license": [{"URL": "https://creativecommons.org/licenses/by/4.0/"}]}})
        result, _ = self.find({"api.crossref.org": closed})
        self.assertEqual([], result["candidates"])
        result, _ = self.find({"api.crossref.org": opened})
        self.assertEqual(["https://pub.example/cr.pdf"], [c["url"] for c in result["candidates"]])

    def test_lookup_errors_recorded_not_raised_and_closed_is_not_oa(self):
        result, _ = self.find({"api.openalex.org": j({"open_access": {"is_oa": False, "oa_status": "closed"}, "locations": [], "ids": {}}),
                               "semanticscholar": OAError("HTTP 429", 429)})
        self.assertTrue(result["lookup_ok"])
        self.assertFalse(result["is_oa"])
        self.assertIn(429, [a["status"] for a in result["attempts"]])
        self.assertTrue(all(a["host"] and "url" not in a for a in result["attempts"]))


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dest = Path(self.temp.name) / "x.pdf"

    def test_rejects_non_pdf_403_oversize_then_succeeds(self):
        router = Router({"html": (200, b"<html>login</html>"), "forbidden": OAError("HTTP 403", 403), "big": OAError("file too large", 200),
                         "good": (200, PDF)})
        urls = ["https://a.example/html", "https://b.example/forbidden", "https://c.example/big", "https://d.example/good", "https://e.example/never"]
        with patch("oa_fetch._request", router):
            result = oa_fetch.download_oa(urls, self.dest)
        self.assertTrue(result["ok"])
        self.assertEqual(PDF, self.dest.read_bytes())
        self.assertEqual(4, len(router.calls))
        self.assertEqual([False, False, False, True], [a["ok"] for a in result["attempts"]])
        self.assertEqual(403, result["attempts"][1]["status"])
        self.assertIn("not a PDF", result["attempts"][0]["status"])
        self.assertEqual("d.example", result["attempts"][3]["host"])

    def test_all_fail_writes_nothing(self):
        with patch("oa_fetch._request", Router({"": (200, b"<html></html>")})):
            result = oa_fetch.download_oa(["https://a.example/1"], self.dest)
        self.assertFalse(result["ok"])
        self.assertFalse(self.dest.exists())

    def test_never_overwrites(self):
        self.dest.write_bytes(b"%PDF-mine")
        router = Router({"": (200, PDF)})
        with patch("oa_fetch._request", router):
            result = oa_fetch.download_oa(["https://a.example/1"], self.dest)
        self.assertFalse(result["ok"])
        self.assertEqual([], router.calls)
        self.assertEqual(b"%PDF-mine", self.dest.read_bytes())

    def test_exclusive_create_race(self):
        def racing(url, *a, **k):
            self.dest.write_bytes(b"%PDF-other")
            return 200, PDF
        with patch("oa_fetch._request", racing):
            result = oa_fetch.download_oa(["https://a.example/1"], self.dest)
        self.assertFalse(result["ok"])
        self.assertEqual(b"%PDF-other", self.dest.read_bytes())

    def test_challenge_is_detected_not_solved(self):
        router = Router({"cf": OAError("HTTP 403", 403, True)})
        with patch("oa_fetch._request", router):
            result = oa_fetch.download_oa(["https://pub.example/cf"], self.dest)
        self.assertEqual("https://pub.example/cf", result["challenge_url"])
        page = (200, b"<html><title>Just a moment...</title></html>")
        with patch("oa_fetch._request", Router({"": page})):
            self.assertTrue(oa_fetch.download_oa(["https://pub.example/x"], self.dest)["challenge_url"])
        self.assertTrue(oa_fetch.challenge_in("<div class='cf-turnstile'>"))
        self.assertFalse(oa_fetch.challenge_in("<p>Please enter a captcha</p>", 200))
        self.assertFalse(oa_fetch.challenge_in("<p>an ordinary article</p>"))


class TransportTests(unittest.TestCase):
    class Response:
        def __init__(self, length="10", body=b"%PDF-1234"):
            self.headers, self.status, self._body = {"Content-Length": length}, 200, body

        def read(self, n):
            chunk, self._body = self._body[:n], self._body[n:]
            return chunk

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def test_headers_cookies_and_size_cap(self):
        seen = []

        class Opener:
            def __init__(self, response):
                self.response = response

            def open(self, request, timeout=None):
                seen.append((request, timeout))
                return self.response
        with patch("oa_fetch.build_opener", lambda *h: Opener(self.Response())):
            oa_fetch._request("https://a.example/x.pdf")
        request, timeout = seen[0]
        self.assertEqual("KYTDL/1.0 (local literature helper)", request.get_header("User-agent"))
        self.assertIsNone(request.get_header("Cookie"))
        self.assertIsNone(request.get_header("Authorization"))
        self.assertEqual(45, timeout)
        with patch("oa_fetch.build_opener", lambda *h: Opener(self.Response(length=str(61 * 1024 * 1024)))):
            with self.assertRaises(OAError):
                oa_fetch._request("https://a.example/x.pdf")
        with patch("oa_fetch.build_opener", lambda *h: Opener(self.Response(length="", body=b"x" * 50))):
            with self.assertRaises(OAError):
                oa_fetch._request("https://a.example/x.pdf", max_bytes=10)
        with self.assertRaises(OAError):
            oa_fetch._request("file:///etc/passwd")
        with self.assertRaises(OAError):
            oa_fetch._request("ftp://a.example/x")

    def test_redirects_only_http(self):
        handler = oa_fetch._Redirects()
        with self.assertRaises(OAError):
            handler.redirect_request(None, None, 302, "Found", {}, "ftp://a.example/x")
        self.assertEqual(oa_fetch.MAX_BYTES, 60 * 1024 * 1024)


class FetchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dest = Path(self.temp.name) / "x.pdf"
        self.browser_calls = []

    def fake_browser(self, result="ok"):
        def run(url, dest, timeout, visible=False, profile_dir=None):
            self.browser_calls.append((url, visible))
            if result == "challenge":
                raise OAChallenge()
            if result == "fail":
                raise OAError("browser: no PDF captured")
            Path(dest).write_bytes(PDF)
            return len(PDF)
        return run

    def fetch(self, routes, mode="prompt", browser="ok"):
        routes = dict({"api.openalex.org": j(OPENALEX)}, **routes)
        with patch("oa_fetch._request", Router(routes)):
            return oa_fetch.fetch_open_access(DOI, self.dest, challenge_mode=mode, browser=self.fake_browser(browser))

    def test_direct_success(self):
        result = self.fetch({"pub.example/best.pdf": (200, PDF)})
        self.assertEqual(("downloaded", "gold"), (result["status"], result["oa_status"]))
        self.assertEqual([], self.browser_calls)
        self.assertEqual("OpenAlex", result["source"])

    def test_headless_fallback_for_plain_block(self):
        result = self.fetch({"pub.example": OAError("HTTP 403", 403)})
        self.assertEqual("downloaded", result["status"])
        self.assertEqual([("https://pub.example/best.pdf", False)], self.browser_calls)

    def test_failed_keeps_manual_url(self):
        result = self.fetch({"pub.example": OAError("HTTP 403", 403)}, browser="fail")
        self.assertEqual("failed", result["status"])
        self.assertEqual("https://pub.example/landing", result["manual_url"])
        self.assertFalse(self.dest.exists())

    def test_challenge_prompt_stops_without_window(self):
        result = self.fetch({"pub.example": OAError("HTTP 403", 403, True)})
        self.assertEqual("needs_human", result["status"])
        self.assertEqual([], self.browser_calls)
        self.assertTrue(result["manual_url"].startswith("https://"))

    def test_challenge_detected_by_headless_prompt(self):
        result = self.fetch({"pub.example": OAError("HTTP 403", 403)}, browser="challenge")
        self.assertEqual("needs_human", result["status"])
        self.assertEqual([False], [v for _, v in self.browser_calls])

    def test_challenge_skip_goes_straight_to_failed(self):
        result = self.fetch({"pub.example": OAError("HTTP 403", 403, True)}, mode="skip")
        self.assertEqual("failed", result["status"])
        self.assertEqual([], self.browser_calls)

    def test_challenge_popup_opens_visible_window(self):
        result = self.fetch({"pub.example": OAError("HTTP 403", 403, True)}, mode="popup")
        self.assertEqual("downloaded", result["status"])
        self.assertEqual([True], [v for _, v in self.browser_calls])
        result = self.fetch({"pub.example": OAError("HTTP 403", 403, True)}, mode="popup", browser="fail")
        self.assertEqual("failed", result["status"])

    def test_closed_article_is_not_oa_and_no_browser(self):
        closed = j({"open_access": {"is_oa": False, "oa_status": "closed"}, "locations": [], "ids": {}})
        result = self.fetch({"api.openalex.org": closed})
        self.assertEqual("not_oa", result["status"])
        self.assertEqual([], self.browser_calls)

    def test_all_lookups_down_is_failed_not_not_oa(self):
        with patch("oa_fetch._request", Router({"": OAError("URLError")})):
            result = oa_fetch.fetch_open_access(DOI, self.dest, browser=self.fake_browser())
        self.assertEqual("failed", result["status"])

    def test_human_fetch_uses_visible_window(self):
        result = oa_fetch.human_fetch("https://pub.example/landing", self.dest, self.temp.name, browser=self.fake_browser())
        self.assertEqual("downloaded", result["status"])
        self.assertEqual([("https://pub.example/landing", True)], self.browser_calls)
        self.dest.unlink()
        result = oa_fetch.human_fetch("https://pub.example/landing", self.dest, self.temp.name, browser=self.fake_browser("fail"))
        self.assertEqual("needs_human", result["status"])


def imp(name="q", **fields):
    fields.setdefault("request_confirmed", True)
    base = dict(id=name, title="Title " + name, doi="10.1234/" + name, source="import", owned=False, status="ready",
                created="2026-10-19T10:00:00", account_id="default", assist_url="", website_status="")
    base.update(fields)
    return base


def plan(items, prefs=None, points=None):
    return next_step(items, {"points": points}, prefs or {}, NOW)


class PlannerTests(unittest.TestCase):
    def test_oa_runs_even_when_points_gate_would_block_publishing(self):
        # points < 500 means single tier; the OA step is local and is chosen before any publish/prepare/gate step
        self.assertEqual("oa_fetch", plan([imp()], points=10)["action"])
        self.assertEqual("oa_fetch", plan([imp()], points=None)["action"])
        self.assertFalse(plan([imp()], points=10)["requires_user"])

    def test_not_for_items_without_conditions(self):
        self.assertEqual("prepare", plan([imp(doi="")])["action"])
        self.assertEqual("prepare", plan([imp(oa_check=dict(status="failed"))])["action"])
        self.assertEqual("prepare", plan([imp(oa_check=dict(status="not_oa"))])["action"])
        self.assertEqual("prepare", plan([imp()], prefs=dict(oa_first=False))["action"])
        website = imp("w", source="website", owned=True, assist_url="https://x/assist/detail?id=1", website_status="求助中")
        self.assertNotEqual("oa_fetch", plan([website])["action"])
        self.assertNotEqual("oa_fetch", plan([imp(source="demo")])["action"])

    def test_priority_after_download_before_sync(self):
        fulfilled = imp("f", source="website", owned=True, assist_url="https://x/a?id=2", website_status="待确认", created="2026-10-19T11:00:00")
        waiting = imp("w", source="website", owned=True, assist_url="https://x/a?id=3", website_status="求助中", created="2026-10-19T11:00:00")
        self.assertEqual("download", plan([fulfilled, imp()])["action"])
        self.assertEqual("oa_fetch", plan([waiting, imp()])["action"])       # before sync

    def test_oa_first_defaults_true(self):
        self.assertEqual("oa_fetch", plan([imp()], prefs={})["action"])

    def test_needs_human_item_does_not_block_others(self):
        held = imp("h", oa_check=dict(status="needs_human", manual_url="https://p.example/x"), created="2026-10-19T09:00:00")
        other = imp("o", oa_check=dict(status="failed"), created="2026-10-19T10:00:00")
        step = plan([held, other])
        self.assertEqual(("prepare", "o"), (step["action"], step["item_id"]))
        fresh = imp("n", created="2026-10-19T11:00:00")
        self.assertEqual(("oa_fetch", "n"), (plan([held, fresh])["action"], plan([held, fresh])["item_id"]))
        alone = plan([held])
        self.assertEqual(("wait_user", True), (alone["action"], alone["requires_user"]))
        skipped = imp("h", oa_check=dict(status="skipped"))
        self.assertEqual("prepare", plan([skipped])["action"])

    def test_done_via_oa_stage(self):
        self.assertEqual("done", stage_of(imp(done_via="oa", pdf="oa/q.pdf", status="checked")))
        self.assertEqual("attention", stage_of(imp(done_via="oa", pdf="oa/q.pdf", status="attention")))
        self.assertEqual("attention", stage_of(imp(pdf="oa/q.pdf", status="attention")))


class FakeAgent:
    def __init__(self):
        self.busy, self.commands, self.calls = False, queue.Queue(), []

    def enqueue(self, action, item_id=None, credentials=None):
        self.calls.append((action, item_id))


class PipelineOATests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.agent = FakeAgent()
        self.pipeline = Pipeline(self.store, Accounts(self.temp.name), self.agent, start=False)
        self.item = self.store.add("A paper", DOI, account_id="default", request_confirmed=True)

    def result(self, status="downloaded", **extra):
        def run(doi, dest, email="", **kwargs):
            if status == "downloaded":
                Path(dest).write_bytes(PDF)
            self.kwargs = dict(kwargs, email=email)
            return dict(dict(status=status, oa_status="gold", is_oa=status != "not_oa", license="cc-by", source="OpenAlex",
                             attempts=[dict(kind="download", source="OpenAlex", host="p.example", status=200, bytes=len(PDF), ok=True)],
                             manual_url=None), **extra)
        return run

    def verification(self, status):
        return patch("pipeline.verify_with_metadata", lambda *a, **k: dict(status=status, reason="mock", sha256="x"))

    def test_success_identity_match_is_done(self):
        with patch("oa_fetch.fetch_open_access", self.result()), self.verification("identity_match"):
            self.pipeline.oa_run(self.item)
        item = self.store.get(self.item)
        self.assertEqual("done", stage_of(item))
        self.assertEqual("oa", item["done_via"])
        self.assertEqual("oa/%s.pdf" % self.item, item["pdf"].replace("\\", "/"))
        self.assertTrue((Path(self.temp.name) / item["pdf"]).is_file())
        self.assertEqual("identity_match", item["verification"]["status"])
        check = item["oa_check"]
        self.assertEqual(("downloaded", "gold"), (check["status"], check["oa_status"]))
        self.assertTrue(check["attempts"] and check["time"])
        self.assertEqual(("idle", False), (plan_for(self.store)["action"], False))
        self.assertEqual("profile", Path(self.kwargs["profile_dir"]).name.split("-")[-1])

    def test_success_without_match_goes_to_oa_review(self):
        with patch("oa_fetch.fetch_open_access", self.result()), self.verification("title_mismatch"):
            self.pipeline.oa_run(self.item)
        item = self.store.get(self.item)
        self.assertEqual("oa_review", stage_of(item))
        self.assertNotIn("done_via", item)
        self.assertTrue(item["pdf"])

    def test_failure_and_not_oa_continue_to_publish_path(self):
        for status in ("failed", "not_oa"):
            store_item = self.store.add("Other " + status, "10.1234/" + status, account_id="default", request_confirmed=True)
            with patch("oa_fetch.fetch_open_access", self.result(status)):
                self.pipeline.oa_run(store_item)
            item = self.store.get(store_item)
            self.assertEqual(status, item["oa_check"]["status"])
            self.assertEqual("queued", stage_of(item))
            self.assertFalse(item.get("pdf"))
        self.store.update(self.item, oa_check=dict(status="failed"))
        self.assertEqual("prepare", plan_for(self.store)["action"])

    def test_exception_is_recorded_as_failed(self):
        def boom(*a, **k):
            raise RuntimeError("network")
        with patch("oa_fetch.fetch_open_access", boom):
            self.pipeline.oa_run(self.item)
        self.assertEqual("failed", self.store.get(self.item)["oa_check"]["status"])

    def test_preferences_are_passed_and_email_not_logged(self):
        self.store.update_preferences(oa_contact_email="me@example.org", oa_challenge_mode="skip")
        with patch("oa_fetch.fetch_open_access", self.result("failed")):
            self.pipeline.oa_run(self.item)
        self.assertEqual(("me@example.org", "skip"), (self.kwargs["email"], self.kwargs["challenge_mode"]))
        self.assertNotIn("me@example.org", json.dumps(self.store.snapshot()["events"]))

    def test_needs_human_flow_and_skip(self):
        with patch("oa_fetch.fetch_open_access", self.result("needs_human", manual_url="https://p.example/x")), patch("oa_fetch.browser_download") as launched:
            self.pipeline.oa_run(self.item)
            launched.assert_not_called()
        check = self.store.get(self.item)["oa_check"]
        self.assertEqual(("needs_human", "https://p.example/x"), (check["status"], check["manual_url"]))
        other = self.store.add("Other", "10.1234/other", account_id="default", request_confirmed=True)
        self.assertEqual("oa_fetch", plan_for(self.store)["action"])          # other items keep flowing
        self.assertEqual(other, plan_for(self.store)["item_id"])
        self.pipeline.oa_skip(self.item)
        self.assertEqual("skipped", self.store.get(self.item)["oa_check"]["status"])
        with self.assertRaises(ValueError):
            self.pipeline.oa_skip(self.item)
        self.assertEqual("queued", stage_of(self.store.get(self.item)))

    def test_human_window_success_and_still_blocked(self):
        self.store.update(self.item, oa_check=dict(status="needs_human", manual_url="https://p.example/x", attempts=[]))
        def human(url, dest, profile):
            self.assertEqual("https://p.example/x", url)
            self.assertEqual("oa-browser-profile", Path(profile).name)
            return dict(status="needs_human", attempts=[], source="Chromium (visible)")
        with patch("oa_fetch.human_fetch", human):
            self.pipeline.oa_run(self.item, human=True)
        self.assertEqual("needs_human", self.store.get(self.item)["oa_check"]["status"])
        def human_ok(url, dest, profile):
            Path(dest).write_bytes(PDF)
            return dict(status="downloaded", attempts=[], source="Chromium (visible)")
        with patch("oa_fetch.human_fetch", human_ok), self.verification("identity_match"):
            self.pipeline.oa_run(self.item, human=True)
        self.assertEqual("done", stage_of(self.store.get(self.item)))

    def test_execute_runs_in_worker_thread_one_at_a_time(self):
        import threading
        started, release = threading.Event(), threading.Event()
        def slow(doi, dest, email="", **kwargs):
            started.set()
            release.wait(5)
            return dict(status="failed", attempts=[])
        main = threading.current_thread()
        with patch("oa_fetch.fetch_open_access", slow):
            planned = self.pipeline.plan(NOW)[3]
            self.assertEqual("oa_fetch", planned["action"])
            self.assertTrue(self.pipeline.execute(planned))
            self.assertTrue(started.wait(5))
            self.assertEqual(self.item, self.pipeline.oa_running)
            self.assertNotEqual(main, self.pipeline.oa_thread)
            with self.assertRaises(ValueError):
                self.pipeline.oa_start(self.item)
            self.store.update_preferences(pipeline_enabled=True)
            self.assertIsNone(self.pipeline.tick(NOW))                      # busy: no second oa_fetch
            release.set()
            self.pipeline.oa_thread.join(5)
        self.assertIsNone(self.pipeline.oa_running)
        self.assertEqual("failed", self.store.get(self.item)["oa_check"]["status"])
        self.assertEqual([], self.agent.calls)                               # never went through BrowserAgent

    def test_tick_runs_oa_even_when_browser_busy(self):
        import threading
        self.store.update_preferences(pipeline_enabled=True)
        self.agent.busy = True
        with patch("oa_fetch.fetch_open_access", self.result("failed")):
            planned = self.pipeline.tick(NOW)
            self.assertEqual("oa_fetch", planned["action"])
            self.pipeline.oa_thread.join(5)
        self.assertEqual([], self.agent.calls)

    def test_oa_first_false_skips(self):
        self.store.update_preferences(oa_first=False, pipeline_enabled=True)
        self.assertEqual("wait_user", self.pipeline.plan(NOW)[3]["action"])      # no 科研通 session known
        self.store.update_account("default", points=100)
        self.assertEqual("prepare", self.pipeline.plan(NOW)[3]["action"])


def plan_for(store):
    snapshot = store.snapshot()
    return next_step(snapshot["items"], {}, snapshot["preferences"], NOW)


class ApiTests(unittest.TestCase):
    def setUp(self):
        import threading
        from urllib.request import urlopen
        from app import make_server
        self.temp = tempfile.TemporaryDirectory()
        self.server = make_server(0, self.temp.name, auto_screen=False)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = "http://127.0.0.1:%d" % self.server.server_port
        self.token = urlopen(self.base).read().decode().split("const TOKEN=")[1].split(";let state=")[0].strip('"')

    def tearDown(self):
        self.server.pipeline.close()
        self.server.shutdown()
        self.server.server_close()
        self.server.agent.commands.put(("close", None, None))
        self.server.agent.thread.join(timeout=2)
        self.temp.cleanup()

    def post(self, route, payload=None, expect=200):
        from urllib.error import HTTPError
        from urllib.request import Request, urlopen
        request = Request(self.base + "/api/" + route, json.dumps(payload or {}).encode(), method="POST",
                          headers={"Content-Type": "application/json", "Origin": self.base, "X-Demo-Token": self.token})
        try:
            return json.load(urlopen(request))
        except HTTPError as exc:
            self.assertEqual(expect, exc.code)

    def state(self):
        from urllib.request import urlopen
        return json.load(urlopen(self.base + "/api/state"))

    def test_preferences(self):
        pipe = self.state()["pipeline"]
        self.assertEqual((True, "prompt", None), (pipe["oa_first"], pipe["oa_challenge_mode"], pipe["oa_running"]))
        self.post("pipeline", {"oa_first": False})
        self.assertFalse(self.state()["pipeline"]["oa_first"])
        for bad in ({"oa_first": "no"}, {"oa_challenge_mode": "solve"}, {"oa_contact_email": "not-an-email"}, {"oa_contact_email": 5}):
            self.post("pipeline", bad, 400)
        self.post("pipeline", {"oa_challenge_mode": "skip", "oa_contact_email": " me@example.org "})
        state = self.state()
        self.assertEqual("skip", state["pipeline"]["oa_challenge_mode"])
        self.assertEqual("me@example.org", state["preferences"]["oa_contact_email"])
        self.assertNotIn("me@example.org", json.dumps(state["events"]))
        self.assertNotIn("me@example.org", json.dumps(state["pipeline"]))
        self.post("pipeline", {"oa_contact_email": ""})
        self.assertEqual("", self.state()["preferences"]["oa_contact_email"])

    def test_oa_fetch_skip_and_human_endpoints(self):
        self.post("import", {"text": "10.1234/api\tAn API paper"})
        item_id = self.state()["items"][0]["id"]
        self.post("oa-fetch", {"id": "nope"}, 400)
        self.post("oa-skip", {"id": item_id}, 400)
        self.post("oa-human", {"id": item_id}, 400)
        started = []
        self.server.pipeline.oa_start = lambda item, human=False: started.append((item, human))
        self.post("oa-fetch", {"id": item_id})
        self.assertEqual([(item_id, False)], started)
        self.server.store.update(item_id, oa_check=dict(status="needs_human", manual_url="https://p.example/x"))
        self.post("oa-human", {"id": item_id})
        self.assertEqual((item_id, True), started[-1])
        self.post("oa-skip", {"id": item_id})
        self.assertEqual("skipped", self.server.store.get(item_id)["oa_check"]["status"])
        self.post("oa-human", {"id": item_id}, 400)
        website = self.server.store.add("W", "10.1/w", source="website", account_id="default", owned=True)
        self.post("oa-fetch", {"id": website}, 400)
        self.server.store.update(item_id, pdf="uploads/x.pdf")
        self.post("oa-fetch", {"id": item_id}, 400)


if __name__ == "__main__":
    unittest.main()


class NoLoginTests(unittest.TestCase):
    """Zero 科研通 accounts logged in: the free-source path must still work end to end (all mocked)."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.agent = FakeAgent()
        self.pipeline = Pipeline(self.store, Accounts(self.temp.name), self.agent, start=False)
        self.item = self.store.add("Free paper", DOI, account_id="default", request_confirmed=True)

    def test_planner_without_session(self):
        from pipeline import NO_SESSION_NOTE, has_session
        items = [imp()]
        self.assertFalse(has_session({}, items))
        self.assertEqual("oa_fetch", next_step(items, {}, {}, NOW, session=False)["action"])
        done = [imp(oa_check=dict(status="not_oa"))]
        step = next_step(done, {}, {}, NOW, session=False)
        self.assertEqual(("wait_user", True, NO_SESSION_NOTE), (step["action"], step["requires_user"], step["reason"]))
        self.assertNotEqual("attention", stage_of(done[0]))
        self.assertEqual("idle", next_step([], {}, {}, NOW, session=False)["action"])
        self.assertTrue(has_session({"points": 120}, items))
        self.assertTrue(has_session({}, [imp(source="website")]))

    def test_oa_without_session_never_touches_browser_agent(self):
        def run(doi, dest, email="", **kwargs):
            Path(dest).write_bytes(PDF)
            return dict(status="downloaded", oa_status="gold", attempts=[], links=[dict(url="https://p.example/a.pdf", source="OpenAlex")],
                        landing_url="https://p.example/a")
        with patch("oa_fetch.fetch_open_access", run), patch("pipeline.verify_with_metadata", lambda *a, **k: dict(status="identity_match")):
            self.store.update_preferences(pipeline_enabled=True)
            self.assertEqual("oa_fetch", self.pipeline.tick(NOW)["action"])
            self.pipeline.oa_thread.join(5)
        self.assertEqual("done", stage_of(self.store.get(self.item)))
        self.assertEqual([], self.agent.calls)

    def test_not_oa_stays_queued_with_note_and_no_attention(self):
        with patch("oa_fetch.fetch_open_access", lambda *a, **k: dict(status="not_oa", attempts=[])):
            self.pipeline.oa_run(self.item)
        item = self.store.get(self.item)
        self.assertEqual(("queued", "无免费全文。登录科研通后可求助"), (stage_of(item), item["note"]))
        self.store.update_preferences(pipeline_enabled=True)
        planned = self.pipeline.plan(NOW)[3]
        self.assertEqual(("wait_user", True), (planned["action"], planned["requires_user"]))
        self.assertIsNone(self.pipeline.tick(NOW))
        self.assertEqual([], self.agent.calls)

    def test_lookup_only_shows_links_then_download_chosen_source(self):
        found = dict(status="found", oa_status="green", attempts=[], links=[dict(url="https://a.example/1.pdf", source="Europe PMC"),
                                                                           dict(url="https://b.example/2.pdf", source="OpenAlex")],
                     landing_url="https://a.example/landing")
        with patch("oa_fetch.lookup_only", lambda doi, email="", **kw: found), patch("oa_fetch.fetch_open_access") as full:
            self.pipeline.oa_run(self.item, lookup_only=True)
            full.assert_not_called()
        check = self.store.get(self.item)["oa_check"]
        self.assertEqual(("found", "https://a.example/landing", 2), (check["status"], check["landing_url"], len(check["links"])))
        self.assertFalse(self.store.get(self.item).get("pdf"))
        seen = {}
        def run(doi, dest, email="", **kwargs):
            seen.update(kwargs)
            Path(dest).write_bytes(PDF)
            return dict(status="downloaded", attempts=[], source="OpenAlex")
        with patch("oa_fetch.fetch_open_access", run), patch("pipeline.verify_with_metadata", lambda *a, **k: dict(status="identity_match")):
            self.pipeline.oa_run(self.item, only_url="https://b.example/2.pdf")
        self.assertEqual([dict(url="https://b.example/2.pdf", source="OpenAlex")], seen["candidates"])
        self.assertEqual("done", stage_of(self.store.get(self.item)))
        self.assertEqual(2, len(self.store.get(self.item)["oa_check"]["links"]))     # links stay visible

    def test_unknown_link_is_rejected(self):
        self.store.update(self.item, oa_check=dict(status="found", links=[dict(url="https://a.example/1.pdf", source="x")]))
        with patch("oa_fetch.fetch_open_access") as full:
            self.pipeline.oa_run(self.item, only_url="https://evil.example/x.pdf")
            full.assert_not_called()
        self.assertEqual("failed", self.store.get(self.item)["oa_check"]["status"])

    def test_fetch_open_access_with_chosen_candidates_skips_lookup(self):
        router = Router({"chosen": (200, PDF)})
        dest = Path(self.temp.name) / "c.pdf"
        with patch("oa_fetch._request", router):
            result = oa_fetch.fetch_open_access(DOI, dest, candidates=[dict(url="https://x.example/chosen.pdf", source="Europe PMC")])
        self.assertEqual("downloaded", result["status"])
        self.assertEqual(["https://x.example/chosen.pdf"], router.calls)           # no OpenAlex lookup

    def test_lookup_only_function(self):
        with patch("oa_fetch._request", Router({"api.openalex.org": j(OPENALEX)})):
            result = oa_fetch.lookup_only(DOI)
        self.assertEqual("found", result["status"])
        self.assertEqual("https://pub.example/landing", result["landing_url"])
        self.assertEqual("https://pub.example/best.pdf", result["links"][0]["url"])
        closed = j({"open_access": {"is_oa": False}, "locations": [], "ids": {}})
        with patch("oa_fetch._request", Router({"api.openalex.org": closed})):
            self.assertEqual("not_oa", oa_fetch.lookup_only(DOI)["status"])
