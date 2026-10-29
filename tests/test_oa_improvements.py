"""OA lookup improvements: landing pages, Retry-After, URL normalization, polite pool, retraction flags,
embedded DOI, disk cache, CORE, versions. Everything is mocked: no network, no browser."""
import itertools
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import pymupdf

import core
import metadata
import oa_fetch
from core import doi_value, verify_pdf
from oa_fetch import OAError

PDF = b"%PDF-1.7\n" + b"x" * 100
DOI = "10.1234/oa.test"


def j(data):
    return 200, json.dumps(data).encode()


class Router:
    def __init__(self, routes):
        self.routes, self.calls, self.core_keys = routes, [], []

    def __call__(self, url, accept="*/*", max_bytes=0, timeout=0):
        self.calls.append(url)
        self.core_keys.append(getattr(oa_fetch._ctx, "core_key", ""))
        for key, value in self.routes.items():
            if key in url:
                if isinstance(value, list):
                    value = value.pop(0) if len(value) > 1 else value[0]
                if isinstance(value, Exception):
                    raise value
                return value
        raise OAError("HTTP 404", 404)


class FastClock(unittest.TestCase):
    """Throttle never waits; sleeps are recorded instead of slept."""
    def setUp(self):
        self.sleeps = []
        tick = itertools.count(0, 100)
        for name, fake in (("_clock", lambda: float(next(tick))), ("_sleep", self.sleeps.append)):
            patcher = patch.object(oa_fetch, name, fake)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)


class LandingTests(FastClock):
    HTML = """<html><head>
      <meta name="citation_pdf_url" content="/files/paper.pdf?utm_source=x">
      <link rel="alternate" type="application/pdf" href="http://cdn.example/alt.pdf#page=1">
      <meta http-equiv="refresh" content="0; url=https://other.example/go.pdf">
      <link rel="stylesheet" href="/a.css"><meta name="description" content="x">
    </head><body>text</body></html>"""

    def test_parser_finds_three_kinds_resolves_and_normalizes(self):
        urls = oa_fetch.parse_landing(self.HTML, "https://pub.example/article/1")
        self.assertEqual(["https://pub.example/files/paper.pdf", "https://cdn.example/alt.pdf", "https://other.example/go.pdf"], urls)

    def test_parser_dedupes_ignores_junk_and_bad_html(self):
        html = '<meta name="citation_pdf_url" content="https://a.example/x.pdf"><link rel="Alternate" type="application/pdf" href="https://a.example/x.pdf"><meta name="citation_pdf_url" content="javascript:alert(1)"><<<'
        self.assertEqual(["https://a.example/x.pdf"], oa_fetch.parse_landing(html, "https://a.example/"))
        self.assertEqual([], oa_fetch.parse_landing("", "https://a.example/"))
        self.assertEqual([], oa_fetch.parse_landing(None, "https://a.example/"))

    def test_parser_reads_only_first_2mb(self):
        html = " " * oa_fetch.LANDING_MAX + '<meta name="citation_pdf_url" content="https://late.example/x.pdf">'
        self.assertEqual([], oa_fetch.parse_landing(html, "https://a.example/"))

    OPENALEX = {"open_access": {"is_oa": True, "oa_status": "bronze"}, "ids": {},
                "best_oa_location": {"pdf_url": None, "landing_page_url": "https://pub.example/article/1", "license": None},
                "locations": []}

    def fetch(self, routes, browser=None):
        calls = []

        def fake_browser(url, dest, timeout, visible=False, profile_dir=None):
            calls.append(url)
            raise OAError("browser: no PDF captured")
        router = Router(dict({"api.openalex.org": j(self.OPENALEX)}, **routes))
        with patch("oa_fetch._request", router):
            result = oa_fetch.fetch_open_access(DOI, Path(self.temp.name) / "x.pdf", browser=fake_browser)
        return result, router, calls

    def test_landing_pdf_is_found_before_the_browser(self):
        result, router, browser_calls = self.fetch({"pub.example/article/1": (200, TestPage(LandingTests.HTML)),
                                                    "pub.example/files/paper.pdf": (200, PDF)})
        self.assertEqual("downloaded", result["status"])
        self.assertEqual("landing", result["source"])
        self.assertEqual([], browser_calls)
        self.assertTrue(any(a["kind"] == "landing" and a["ok"] for a in result["attempts"]))
        self.assertTrue(any("citation" not in c and c.endswith("paper.pdf") for c in router.calls))

    def test_landing_challenge_is_recorded_not_solved_and_browser_still_runs(self):
        page = b"<html><title>Just a moment...</title>cf-challenge</html>"
        result, router, browser_calls = self.fetch({"pub.example/article/1": (200, page), "doi.org": OAError("HTTP 404", 404)})
        self.assertEqual("failed", result["status"])
        landing = [a for a in result["attempts"] if a["kind"] == "landing"]
        self.assertTrue(landing[0]["challenge"])
        self.assertEqual([], browser_calls)   # a landing-page check is recorded, never solved or escalated

    def test_landing_429_is_recorded_and_not_retried_without_header(self):
        result, router, _ = self.fetch({"pub.example/article/1": OAError("HTTP 429", 429)})
        self.assertEqual("failed", result["status"])
        self.assertEqual(1, len([c for c in router.calls if "pub.example/article/1" in c]))
        self.assertEqual(429, [a for a in result["attempts"] if a["kind"] == "landing"][0]["status"])

    def test_no_landing_for_closed_article(self):
        closed = j({"open_access": {"is_oa": False, "oa_status": "closed"}, "locations": [], "ids": {}})
        result, router, _ = self.fetch({"api.openalex.org": closed})
        self.assertEqual("not_oa", result["status"])
        self.assertFalse([c for c in router.calls if "doi.org/10.1234" in c])

    def test_landing_requests_carry_no_cookies(self):
        class Opener:
            def __init__(self):
                self.request = None

            def open(self, request, timeout=0):
                self.request = request
                raise OAError("stop")
        opener = Opener()
        with patch("oa_fetch.build_opener", lambda *h: opener):
            with self.assertRaises(OAError):
                oa_fetch._request("https://pub.example/article/1", "text/html")
        self.assertNotIn("cookie", {k.lower() for k in opener.request.headers})


def TestPage(text):
    return text.encode()


class RetryTests(FastClock):
    def run_get(self, errors, deadline=None):
        seq = list(errors) + [(200, b"ok")]
        calls = []

        def fake(url, accept, max_bytes, timeout):
            calls.append(url)
            item = seq.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        with patch("oa_fetch._request", fake):
            try:
                return oa_fetch._get("https://a.example/x", "*/*", 10, 5, deadline), calls
            except OAError as exc:
                return exc, calls

    @staticmethod
    def err(status, after=None):
        error = OAError("HTTP %d" % status, status)
        error.retry_after = after
        return error

    def test_retry_after_within_10s_waits_once_then_succeeds(self):
        result, calls = self.run_get([self.err(429, 3.0)])
        self.assertEqual((200, b"ok"), result)
        self.assertEqual([3.0], self.sleeps)
        self.assertEqual(2, len(calls))

    def test_retry_after_over_10s_skips_without_waiting(self):
        result, calls = self.run_get([self.err(429, 30.0)])
        self.assertIsInstance(result, OAError)
        self.assertEqual([], self.sleeps)
        self.assertEqual(1, len(calls))

    def test_429_without_header_is_not_retried(self):
        result, calls = self.run_get([self.err(429)])
        self.assertIsInstance(result, OAError)
        self.assertEqual(1, len(calls))

    def test_500_and_503_get_exactly_one_backoff_retry(self):
        for status in (500, 503):
            self.sleeps.clear()
            result, calls = self.run_get([self.err(status), self.err(status)])
            self.assertIsInstance(result, OAError)
            self.assertEqual(2, len(calls))
            self.assertEqual([oa_fetch.BACKOFF], self.sleeps)

    def test_other_codes_and_challenges_are_never_retried(self):
        for error in (self.err(404), self.err(403), OAError("HTTP 503", 503, True)):
            error.retry_after = 1.0
            result, calls = self.run_get([error])
            self.assertEqual(1, len(calls))

    def test_retry_is_bounded_by_the_total_deadline(self):
        result, calls = self.run_get([self.err(429, 5.0)], deadline=time.monotonic() + 4)
        self.assertIsInstance(result, OAError)
        self.assertEqual(1, len(calls))
        self.assertEqual([], self.sleeps)

    def test_header_parsing(self):
        self.assertEqual(7.0, oa_fetch._retry_after("7"))
        self.assertIsNone(oa_fetch._retry_after("soon"))
        self.assertIsNone(oa_fetch._retry_after(None))
        with patch.object(oa_fetch, "_wall", lambda: 2_000_000_000.0):
            self.assertEqual(0.0, oa_fetch._retry_after("Wed, 21 Oct 2015 07:28:00 GMT"))

    def test_lookup_records_retry_and_download_retries_503(self):
        router = Router({"api.openalex.org": [(OAError("HTTP 503", 503)), j({"open_access": {"is_oa": False}, "locations": []})]})
        router.routes["api.openalex.org"][0].retry_after = 2.0
        with patch("oa_fetch._request", router):
            info = oa_fetch.find_oa_locations(DOI)
        first = info["attempts"][0]
        self.assertTrue(first["retried"] and first["ok"])
        self.assertEqual([2.0], self.sleeps)

    def test_redirect_limits(self):
        self.assertEqual(10, oa_fetch._Redirects.max_redirections)
        self.assertEqual(2, oa_fetch._Redirects.max_repeats)


class NormalizeTests(unittest.TestCase):
    def test_https_upgrade_only(self):
        n = oa_fetch.normalize_url
        self.assertEqual("https://a.example/x.pdf", n("http://A.Example/x.pdf"))
        self.assertEqual("https://a.example/x.pdf", n("https://a.example:443/x.pdf#frag"))
        self.assertEqual("http://127.0.0.1/x.pdf", n("http://127.0.0.1/x.pdf"))
        self.assertEqual("http://a.example:8080/x", n("http://a.example:8080/x"))
        self.assertEqual("https://a.example/x", n("http://a.example:80/x"))
        for bad in ("ftp://a.example/x", "javascript:1", "", None, 5):
            self.assertIsNone(n(bad))

    def test_tracking_stripped_whitelist_kept(self):
        n = oa_fetch.normalize_url
        url = "https://a.example/get?utm_source=x&utm_medium=y&fbclid=1&gclid=2&mc_cid=3&mc_eid=4&download=1&pdf=true&type=printable&id=9&article=7&file=a.pdf&render=1&version=2&keep=me"
        self.assertEqual("https://a.example/get?download=1&pdf=true&type=printable&id=9&article=7&file=a.pdf&render=1&version=2&keep=me", n(url))
        self.assertEqual("https://a.example/x", n("https://a.example/x?utm_campaign=z"))

    def test_dedupe_and_per_source_cap(self):
        urls = ["http://p.example/f.pdf?utm_source=a", "https://p.example/f.pdf?fbclid=2", "https://p.example/f.pdf#x"]
        works = {"open_access": {"is_oa": True}, "ids": {}, "best_oa_location": {},
                 "locations": [{"pdf_url": u, "landing_page_url": None, "source": {"type": "journal"}} for u in urls] +
                              [{"pdf_url": "https://m%d.example/a.pdf" % i, "landing_page_url": None, "source": {"type": "journal"}} for i in range(9)]}
        with patch("oa_fetch._request", Router({"api.openalex.org": j(works)})):
            info = oa_fetch.find_oa_locations(DOI)
        got = [c["url"] for c in info["candidates"]]
        self.assertEqual(1, got.count("https://p.example/f.pdf"))
        self.assertEqual(oa_fetch.MAX_PER_SOURCE, len(got))   # all from OpenAlex: capped at 6
        self.assertFalse([u for u in got if u.startswith("http://")])


class PoliteTests(FastClock):
    WORK = {"open_access": {"is_oa": False}, "locations": [], "ids": {}}

    def test_email_adds_mailto_and_user_agent_only_for_openalex_and_crossref(self):
        agents = []

        class Opener:
            def open(self, request, timeout=0):
                agents.append((request.full_url, request.get_header("User-agent")))
                raise OAError("stop")
        with patch("oa_fetch.build_opener", lambda *h: Opener()):
            oa_fetch.find_oa_locations(DOI, "me@uni.edu")
        by_host = {url.split("/")[2]: ua for url, ua in agents}
        self.assertEqual("KYTDL/1.0 (mailto:me@uni.edu)", by_host["api.openalex.org"])
        self.assertEqual("KYTDL/1.0 (mailto:me@uni.edu)", by_host["api.crossref.org"])
        self.assertEqual(oa_fetch.USER_AGENT, by_host["api.semanticscholar.org"])
        urls = [u for u, _ in agents]
        self.assertTrue(all("mailto=me@uni.edu" in u for u in urls if "openalex" in u or "crossref" in u))
        self.assertTrue(any("unpaywall" in u and "email=me@uni.edu" in u for u in urls))

    def test_without_email_nothing_is_sent_and_nothing_logged(self):
        router = Router({"api.openalex.org": j(self.WORK)})
        with patch("oa_fetch._request", router):
            info = oa_fetch.find_oa_locations(DOI)
        self.assertFalse([u for u in router.calls if "mailto" in u or "email" in u or "unpaywall" in u])
        with patch("oa_fetch._request", Router({})):
            info = oa_fetch.find_oa_locations(DOI, "secret@uni.edu")
        self.assertNotIn("secret@uni.edu", json.dumps(info["attempts"]))
        self.assertEqual("", getattr(oa_fetch._ctx, "email", ""))   # context restored afterwards

    def test_metadata_polite_pool(self):
        seen = []

        def fake(url):
            seen.append((url, getattr(metadata._ctx, "email", "")))
            raise OSError("down")
        with patch("metadata.get_json", fake):
            metadata.fetch_metadata(DOI, self.temp.name, "me@uni.edu")
            metadata.fetch_metadata("10.1234/other", self.temp.name)
        polite, anonymous = seen[:2], seen[2:]
        self.assertTrue(all("mailto=me@uni.edu" in u and e == "me@uni.edu" for u, e in polite))
        self.assertTrue(all("mailto" not in u and e == "" for u, e in anonymous))

    def test_help_text_names_the_three_services(self):
        for name in ("OpenAlex", "Crossref", "Unpaywall"):
            self.assertIn(name, oa_fetch.CONTACT_EMAIL_HELP)


class DoiTests(unittest.TestCase):
    def test_balanced_parentheses_survive(self):
        self.assertEqual("10.1016/s0140-6736(20)30183-5", doi_value("10.1016/S0140-6736(20)30183-5"))
        self.assertEqual("10.1016/s0140-6736(20)30183-5", doi_value("(see 10.1016/S0140-6736(20)30183-5)"))
        self.assertEqual("10.1016/s0140-6736(20)30183-5", doi_value("doi: 10.1016/S0140-6736(20)30183-5)."))

    def test_unmatched_trailing_characters_are_stripped(self):
        self.assertEqual("10.1000/abc", doi_value("(doi:10.1000/abc)"))
        self.assertEqual("10.1000/abc", doi_value("10.1000/abc."))
        self.assertEqual("10.1000/abc", doi_value("10.1000/abc,"))
        self.assertEqual("10.1000/abc", doi_value("10.1000/abc;"))
        self.assertEqual("10.1000/abc", doi_value("10.1000/abc).,"))
        self.assertEqual("", doi_value("no doi here"))
        self.assertEqual("", doi_value(None))


def make_pdf(path, lines, info=None, xmp=None):
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_textbox((40, 40, 560, 780), "\n".join(lines), fontsize=10)
    if info:
        doc.set_metadata(info)
    if xmp:
        doc.set_xml_metadata(xmp)
    doc.save(path)
    doc.close()


TITLE = "Dietary fibre and gut microbiota in adults"
ABSTRACT = ("Dietary fibre intake shaped the composition of the gut microbiota in a cohort of adult volunteers "
            "followed for twelve weeks, with measurable changes in short chain fatty acid production.")


class VerifyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.pdf = str(Path(self.temp.name) / "p.pdf")
        make_pdf(self.pdf, [TITLE, "Alice Smith, Bob Jones", "doi: " + DOI, "", "Abstract", ABSTRACT, "", "Introduction", "x " * 40])

    def meta(self, retraction="absent"):
        record = dict(status="available", doi=DOI, title=TITLE, authors=["Alice Smith", "Bob Jones"], abstract=ABSTRACT, sources=["OpenAlex"])
        if retraction != "absent":
            record["retraction"] = retraction
        return record

    def test_baseline_identity_match(self):
        result = verify_pdf(self.pdf, TITLE, DOI, metadata=self.meta())
        self.assertEqual("identity_match", result["status"])
        self.assertEqual(dict(retracted=None, updates=[], source=None), result["retraction"])

    def test_retracted_paper_is_never_identity_match(self):
        flag = dict(retracted=True, updates=[dict(type="retraction", doi="10.1234/r", role="paper")], source="Crossref")
        result = verify_pdf(self.pdf, TITLE, DOI, metadata=self.meta(flag))
        self.assertEqual("review", result["status"])
        self.assertEqual("该文献已被撤稿或有更正通知，请人工确认", result["reason"])
        self.assertTrue(result["retraction"]["retracted"])

    def test_notice_in_updates_without_flag_also_blocks(self):
        flag = dict(retracted=None, updates=[dict(type="withdrawal", doi="", role="paper")], source="Crossref")
        self.assertEqual("review", verify_pdf(self.pdf, TITLE, DOI, metadata=self.meta(flag))["status"])

    def test_unknown_or_clean_or_correction_only_is_not_retracted(self):
        for flag in (dict(retracted=None, updates=[], source=None), dict(retracted=False, updates=[], source="OpenAlex"),
                     dict(retracted=None, updates=[dict(type="correction", doi="", role="paper")], source="Crossref"),
                     dict(retracted=None, updates=[dict(type="retraction", doi="10.1234/t", role="notice")], source="Crossref")):
            self.assertEqual("identity_match", verify_pdf(self.pdf, TITLE, DOI, metadata=self.meta(flag))["status"], flag)
        self.assertIsNone(core.is_retracted(None))
        self.assertIsNone(core.is_retracted({}))

    def test_verify_with_metadata_uses_fetched_flag(self):
        record = dict(self.meta(dict(retracted=True, updates=[], source="OpenAlex")))
        with patch("metadata.fetch_metadata", lambda *a, **k: record):
            result = metadata.verify_with_metadata(self.pdf, TITLE, DOI, self.temp.name)
        self.assertEqual("review", result["status"])

    def test_embedded_doi_from_info_and_xmp_is_evidence_only(self):
        other = str(Path(self.temp.name) / "q.pdf")
        make_pdf(other, [TITLE, "Alice Smith", "no doi on the page", "x " * 80], info={"subject": "doi:10.1234/emb.1)"})
        result = verify_pdf(other, TITLE, "10.1234/emb.1")
        self.assertEqual("10.1234/emb.1", result["embedded_doi"])
        self.assertNotEqual("identity_match", result["status"])
        xmp = str(Path(self.temp.name) / "x.pdf")
        make_pdf(xmp, [TITLE, "x " * 80], xmp='<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"><rdf:Description xmlns:prism="http://prismstandard.org/namespaces/basic/2.0/" prism:doi="10.5555/xmp.7"/></rdf:RDF></x:xmpmeta>')
        self.assertEqual("10.5555/xmp.7", verify_pdf(xmp, TITLE, "10.5555/xmp.7")["embedded_doi"])
        self.assertEqual("", verify_pdf(self.pdf, TITLE, DOI)["embedded_doi"])

    def test_parenthesis_doi_on_first_page_matches(self):
        pdf = str(Path(self.temp.name) / "lancet.pdf")
        make_pdf(pdf, [TITLE, "https://doi.org/10.1016/S0140-6736(20)30183-5", "x " * 80])
        result = verify_pdf(pdf, TITLE, "10.1016/S0140-6736(20)30183-5")
        self.assertIn("10.1016/s0140-6736(20)30183-5", result["primary_dois"])
        self.assertEqual("identity_match", result["status"])


class RetractionRecordTests(unittest.TestCase):
    def test_crossref_updated_by_retraction(self):
        raw = {"message": {"DOI": DOI, "updated-by": [{"DOI": "10.1234/notice", "type": "retraction", "label": "Retraction"}]}}
        record = metadata.crossref_record(raw)["retraction"]
        self.assertTrue(record["retracted"])
        self.assertEqual("paper", record["updates"][0]["role"])
        self.assertEqual("10.1234/notice", record["updates"][0]["doi"])

    def test_crossref_update_to_is_a_notice_not_a_retracted_paper(self):
        raw = {"message": {"DOI": "10.1234/notice", "update-to": [{"DOI": DOI, "type": "retraction"}]}}
        record = metadata.crossref_record(raw)["retraction"]
        self.assertIsNone(record["retracted"])
        self.assertEqual("notice", record["updates"][0]["role"])

    def test_crossref_relation_and_correction(self):
        raw = {"message": {"DOI": DOI, "relation": {"is-retracted-by": [{"id-type": "doi", "id": "10.1234/r"}], "has-preprint": [{"id": "x"}]}}}
        self.assertTrue(metadata.crossref_record(raw)["retraction"]["retracted"])
        raw = {"message": {"DOI": DOI, "updated-by": [{"DOI": "10.1234/e", "type": "correction"}]}}
        record = metadata.crossref_record(raw)["retraction"]
        self.assertIsNone(record["retracted"])
        self.assertEqual("correction", record["updates"][0]["type"])

    def test_openalex_flag_and_missing(self):
        self.assertTrue(metadata.openalex_record({"doi": "https://doi.org/" + DOI, "is_retracted": True})["retraction"]["retracted"])
        self.assertFalse(metadata.openalex_record({"doi": "https://doi.org/" + DOI, "is_retracted": False})["retraction"]["retracted"])
        self.assertIsNone(metadata.openalex_record({"doi": "https://doi.org/" + DOI})["retraction"]["retracted"])

    def test_merge_never_invents(self):
        m = metadata.merge_retraction
        self.assertIsNone(m([dict(retracted=None, updates=[], source="OpenAlex"), None])["retracted"])
        self.assertTrue(m([dict(retracted=False, updates=[], source="OpenAlex"), dict(retracted=True, updates=[], source="Crossref")])["retracted"])
        self.assertFalse(m([dict(retracted=False, updates=[], source="OpenAlex"), dict(retracted=None, updates=[], source="Crossref")])["retracted"])

    def test_fetch_metadata_returns_retraction_field(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        openalex = {"doi": "https://doi.org/" + DOI, "title": "T", "authorships": [], "is_retracted": True}
        crossref = {"message": {"DOI": DOI, "title": ["T"], "updated-by": [{"DOI": "10.1234/n", "type": "retraction"}]}}
        with patch("metadata.get_json", side_effect=[openalex, crossref]) as query:
            record = metadata.fetch_metadata(DOI, temp.name)
        self.assertIn("is_retracted", query.call_args_list[0].args[0])
        self.assertEqual("OpenAlex+Crossref", record["retraction"]["source"])
        self.assertTrue(record["retraction"]["retracted"])
        self.assertEqual("10.1234/n", record["retraction"]["updates"][0]["doi"])

    def test_unavailable_metadata_has_no_retraction_claim(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        with patch("metadata.get_json", side_effect=OSError("down")):
            record = metadata.fetch_metadata(DOI, temp.name)
        self.assertNotIn("retraction", record)
        self.assertIsNone(core.is_retracted(record))


class CacheTests(FastClock):
    OA = {"open_access": {"is_oa": True, "oa_status": "gold"}, "ids": {},
          "best_oa_location": {"pdf_url": "https://pub.example/a.pdf", "version": "publishedVersion", "landing_page_url": "https://pub.example/l"},
          "locations": [{"pdf_url": "https://repo.example/b.pdf", "version": "acceptedVersion", "landing_page_url": None, "source": {"type": "repository"}}]}
    CLOSED = {"open_access": {"is_oa": False, "oa_status": "closed"}, "locations": [], "ids": {}}

    def setUp(self):
        super().setUp()
        self.now = 1_000_000.0
        patcher = patch.object(oa_fetch, "_wall", lambda: self.now)
        patcher.start()
        self.addCleanup(patcher.stop)

    def find(self, routes, doi=DOI):
        router = Router(routes)
        with patch("oa_fetch._request", router):
            return oa_fetch.find_oa_locations(doi, cache_root=self.temp.name), router

    def test_hit_cached_seven_days(self):
        info, first = self.find({"api.openalex.org": j(self.OA)})
        self.assertTrue(first.calls)
        self.assertFalse(info.get("cached"))
        self.now += 7 * 86400 - 1
        again, second = self.find({})
        self.assertEqual([], second.calls)
        self.assertTrue(again["cached"])
        self.assertTrue(again["fetched_at"])
        self.assertEqual(info["candidates"], again["candidates"])
        self.now += 2
        _, third = self.find({"api.openalex.org": j(self.OA)})
        self.assertTrue(third.calls)

    def test_miss_cached_one_day(self):
        info, first = self.find({"api.openalex.org": j(self.CLOSED)})
        self.assertEqual([], info["candidates"])
        self.now += 86400 - 1
        _, second = self.find({})
        self.assertEqual([], second.calls)
        self.now += 2
        _, third = self.find({"api.openalex.org": j(self.CLOSED)})
        self.assertTrue(third.calls)

    def test_legacy_pmc_cache_is_refreshed_without_deletion(self):
        old = dict(self.OA, ids={"pmcid": "PMC123456"})
        info, _ = self.find({"api.openalex.org": j(old)})
        cache = oa_fetch._cache_file(self.temp.name, DOI)
        saved = json.loads(cache.read_text("utf-8"))
        saved["result"]["attempts"] = [a for a in saved["result"]["attempts"] if a.get("source") != "PMC Cloud"]
        cache.write_text(json.dumps(saved), "utf-8")
        refreshed, router = self.find({"api.openalex.org": j(old)})
        self.assertTrue(router.calls)
        self.assertFalse(refreshed.get("cached"))
        self.assertTrue(any(a.get("source") == "PMC Cloud" for a in refreshed["attempts"]))
        self.assertTrue(cache.exists())

    def test_failures_are_not_cached(self):
        info, _ = self.find({"": OAError("URLError")})
        self.assertFalse(info["lookup_ok"])
        self.assertFalse(list((Path(self.temp.name) / "cache" / "oa").glob("*.json")) if (Path(self.temp.name) / "cache" / "oa").exists() else [])
        info, _ = self.find({"api.openalex.org": OAError("HTTP 500", 500), "api.crossref.org": j({"message": {}})})
        self.assertFalse(list((Path(self.temp.name) / "cache" / "oa").glob("*.json")))   # partial miss: not cached

    def test_clear_cache_and_key_is_normalized_doi(self):
        self.find({"api.openalex.org": j(self.OA)})
        folder = Path(self.temp.name) / "cache" / "oa"
        self.assertEqual(1, len(list(folder.glob("*.json"))))
        self.find({}, doi="HTTPS://doi.org/" + DOI.upper())
        self.assertEqual(1, len(list(folder.glob("*.json"))))   # same key
        self.assertEqual(1, oa_fetch.clear_oa_cache(self.temp.name))
        self.assertEqual(0, oa_fetch.clear_oa_cache(self.temp.name))
        self.assertEqual(0, oa_fetch.clear_oa_cache(Path(self.temp.name) / "missing"))

    def test_versions_on_candidates_and_result(self):
        info, _ = self.find({"api.openalex.org": j(self.OA)})
        versions = {c["url"]: c["version"] for c in info["candidates"]}
        self.assertEqual("publishedVersion", versions["https://pub.example/a.pdf"])
        self.assertEqual("acceptedVersion", versions["https://repo.example/b.pdf"])
        self.assertEqual("publishedVersion", info["version"])
        with patch("oa_fetch._request", Router({"api.openalex.org": j(self.OA)})):
            lookup = oa_fetch.lookup_only(DOI)
        self.assertEqual("publishedVersion", lookup["version"])
        self.assertEqual("publishedVersion", lookup["links"][0]["version"])

    def test_unpaywall_version_and_unknown_version_is_none(self):
        up = {"is_oa": True, "oa_status": "green", "best_oa_location": {"url_for_pdf": "https://r.example/u.pdf", "version": "submittedVersion", "host_type": "repository"}, "oa_locations": []}
        with patch("oa_fetch._request", Router({"api.unpaywall.org": j(up), "api.openalex.org": j(dict(self.OA, best_oa_location={"pdf_url": "https://p.example/n.pdf", "version": "weird"}, locations=[]))})):
            info = oa_fetch.find_oa_locations(DOI, "me@uni.edu")
        by_url = {c["url"]: c["version"] for c in info["candidates"]}
        self.assertEqual("submittedVersion", by_url["https://r.example/u.pdf"])
        self.assertIsNone(by_url["https://p.example/n.pdf"])

    def test_download_reports_version_of_the_file_that_worked(self):
        router = Router({"api.openalex.org": j(self.OA), "pub.example/a.pdf": OAError("HTTP 404", 404), "repo.example/b.pdf": (200, PDF)})
        with patch("oa_fetch._request", router):
            result = oa_fetch.fetch_open_access(DOI, Path(self.temp.name) / "d.pdf", cache_root=self.temp.name, browser=lambda *a, **k: 0 / 0)
        self.assertEqual("downloaded", result["status"])
        self.assertEqual("acceptedVersion", result["version"])


class CoreTests(FastClock):
    OPENALEX = {"open_access": {"is_oa": False}, "locations": [], "ids": {}}

    def find(self, routes, key=""):
        router = Router(dict({"api.openalex.org": j(self.OPENALEX)}, **routes))
        with patch("oa_fetch._request", router):
            return oa_fetch.find_oa_locations(DOI, core_api_key=key), router

    def test_no_key_means_no_core_request(self):
        info, router = self.find({"core.ac.uk": j({"results": []})})
        self.assertFalse([c for c in router.calls if "core.ac.uk" in c])

    def test_key_adds_repository_candidates_with_version(self):
        results = {"results": [{"doi": "https://doi.org/" + DOI, "downloadUrl": "https://core.ac.uk/download/1.pdf", "version": "acceptedVersion"},
                               {"doi": "10.9/other", "downloadUrl": "https://core.ac.uk/download/2.pdf"}]}
        info, router = self.find({"core.ac.uk/v3/search": j(results)}, key="K")
        core_urls = [c for c in info["candidates"] if c["source"] == "CORE"]
        self.assertEqual(["https://core.ac.uk/download/1.pdf"], [c["url"] for c in core_urls])
        self.assertEqual("acceptedVersion", core_urls[0]["version"])
        self.assertTrue(info["is_oa"])
        self.assertIn("K", [k for url, k in zip(router.calls, router.core_keys) if "core.ac.uk" in url])
        self.assertEqual("", getattr(oa_fetch._ctx, "core_key", ""))

    def test_401_and_403_skip_core_quietly(self):
        for status in (401, 403):
            info, router = self.find({"core.ac.uk": OAError("HTTP %d" % status, status)}, key="BAD")
            self.assertEqual([], [c for c in info["candidates"] if c["source"] == "CORE"])
            self.assertEqual(1, len([c for c in router.calls if "core.ac.uk" in c]))
            self.assertNotIn("BAD", json.dumps(info["attempts"]))

    def test_authorization_header_only_for_core_host(self):
        sent = []

        class Opener:
            def open(self, request, timeout=0):
                sent.append((request.full_url, request.get_header("Authorization")))
                raise OAError("stop")
        with patch("oa_fetch.build_opener", lambda *h: Opener()):
            oa_fetch.find_oa_locations(DOI, core_api_key="SECRET")
        for url, auth in sent:
            self.assertEqual("Bearer SECRET" if "core.ac.uk" in url else None, auth)


if __name__ == "__main__":
    unittest.main()
