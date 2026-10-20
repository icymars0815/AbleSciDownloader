"""Hand-over scenarios: a received 应助 PDF is unusable or does not match -> the flow goes to a human, nothing is adopted or
rejected by itself (except the one documented auto-reject), the <500 gate stays closed, the local file survives, and the
pipeline continues once the person acts. Synthetic files only; fake Playwright objects; nothing contacts ablesci.com."""
import hashlib
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

import pymupdf

import deletion
import metadata
from accounts import Accounts
from app import make_server
from core import Store
from monitor import Monitor
from notifications import Notifier
from pipeline import PIPELINE_RULES_VERSION, Pipeline, gate, next_step, stage_of
from rejection_policy import REJECTION_RULES_VERSION, manual_reason, rejection_decision
from tests import test_pipeline as tp

TITLE = "Citrus flavonoids modulate gut microbiota in healthy adults"
DOI = "10.1234/target.2026"
AUTHORS = ["Alice Smith", "Bob Jones"]
ABSTRACT = ("We examined how citrus flavonoid intake changes the composition of the gut microbiota in healthy adults "
            "using a randomised crossover design and shotgun sequencing of stool samples collected at baseline.")
META = dict(status="available", doi=DOI, title=TITLE, authors=AUTHORS, abstract=ABSTRACT, sources=["Test"], fetched_at="2026-10-19T00:00:00+08:00",
            retraction=dict(retracted=False, updates=[], source=None), errors=[], field_sources={})
URL, FILE = tp.URL, tp.FILE
PUBLISH_PREFS = dict(pipeline_auto_publish=True, pipeline_rules_version=PIPELINE_RULES_VERSION, oa_first=False)


# ---- synthetic files --------------------------------------------------------------------------------------------------
def make_pdf(*pages, **save):
    doc = pymupdf.open()
    for text in pages:
        doc.new_page().insert_textbox((50, 50, 540, 780), text, fontsize=11)
    data = doc.tobytes(**save)
    doc.close()
    return data


def correct_pdf():
    return make_pdf("Journal of Synthetic Food Science\n%s\n%s\nhttps://doi.org/%s\n\nAbstract\n%s\n\nIntroduction\nCitrus fruit are widely eaten." %
                    (TITLE, ", ".join(AUTHORS), DOI, ABSTRACT), "Methods\nSynthetic body text.")


def other_paper_pdf():
    return make_pdf("Journal of Polymer Letters\nPolymer coatings for lithium battery separators\nCarol White, Dan Black\n"
                    "https://doi.org/10.9999/other.77\n\nAbstract\nWe coat separators with a thin polymer film and measure ionic conductivity at several temperatures.")


def references_only_pdf():
    return make_pdf("Journal of Polymer Letters\nPolymer coatings for lithium battery separators\nCarol White, Dan Black\n\n"
                    "Abstract\nWe coat separators with a thin polymer film.\n\nReferences\n[1] Smith A, Jones B. %s. doi:%s" % (TITLE, DOI))


def preprint_pdf():
    # right DOI and title, but a preprint banner, no authors and no abstract: identity cannot be fully confirmed
    return make_pdf("arXiv:2601.01234v1 [q-bio.GN] preprint, not peer reviewed\n%s\nhttps://doi.org/%s\n\n(page 1 of 1)" % (TITLE, DOI))


def scanned_pdf():
    doc = pymupdf.open()
    page = doc.new_page()
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 120, 160))
    pix.clear_with(200)
    page.insert_image(pymupdf.Rect(50, 50, 300, 380), pixmap=pix)
    data = doc.tobytes()
    doc.close()
    return data


def encrypted_pdf():
    return make_pdf("secret page", encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="user-pw", owner_pw="owner-pw")


HTML = b"<!DOCTYPE html><html><head><title>403</title></head><body>Forbidden: please log in to download this file</body></html>"


class FakeDownload:
    def __init__(self, name, data):
        self.suggested_filename, self.data = name, data

    def save_as(self, path):
        Path(path).write_bytes(self.data)

    def cancel(self):
        pass


class FakeAgent:
    """Stands in for the serial browser agent inside Pipeline: records what the scheduler would run."""
    busy = False

    def __init__(self):
        self.commands, self.enqueued = queue.Queue(), []

    def enqueue(self, action, item_id=None, credentials=None):
        self.enqueued.append((action, item_id))


# ---- harness ----------------------------------------------------------------------------------------------------------
class Base(unittest.TestCase):
    auto = False         # True: auto-reject mode ON and auto-adopt ON (plus auto-publish in both)
    NAME = "Original Name.pdf"

    def setUp(self):
        tp.ArchiveAndActionTests.setUp(self)          # real BrowserAgent + Store + mocked Playwright page
        patcher = patch.object(metadata, "fetch_metadata", side_effect=lambda *a, **k: json.loads(json.dumps(META)))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.accounts = Accounts(self.temp.name)
        self.toasts = []
        self.notifier = Notifier(self.store, sink=lambda t, m: self.toasts.append((t, m)))
        self.agent.notifier = self.notifier
        self.monitor = Monitor(self.store, self.accounts, self.agent, None, self.notifier, start=False)
        self.fake = FakeAgent()
        self.pipeline = Pipeline(self.store, self.accounts, self.fake, start=False)
        prefs = dict(PUBLISH_PREFS)
        if self.auto:
            prefs.update(rejection_mode="auto", rejection_rules_version=REJECTION_RULES_VERSION, pipeline_auto_adopt=True)
        prefs["pipeline_enabled"] = True
        self.store.update_preferences(**prefs)
        self.store.update_account("default", points=312, last_sync_time=datetime.now().astimezone().isoformat())
        # the next confirmed request, waiting behind the <500 gate
        self.next_id = self.store.add("A different paper to request next", "10.1234/next", source="import", account_id="default",
                                      request_confirmed=True, oa_check=dict(status="not_oa"))
        self.agent.stop.clear()

    # -- receiving the answer through the real run loop (BrowserAgent.run -> save_download) --
    def receive(self, data, name=None, doi_origin="user"):
        self.data = data
        item_id = self.store.add(TITLE, DOI, source="website", account_id="default", owned=True, assist_url=URL, website_status="待确认",
                                 download_file_url=FILE, doi_origin=doi_origin, status="fulfilled")
        self.agent.receive_answer = lambda iid: self.agent.save_download(iid, FakeDownload(name or self.NAME, data), TITLE, DOI)
        self.agent.enqueue("receive_answer", item_id)
        self.agent.commands.join()
        for _ in range(100):                       # on_done runs just after task_done
            if not self.agent.busy:
                break
            threading.Event().wait(0.02)
        return item_id

    # -- views --
    def item(self, item_id):
        return self.store.get(item_id)

    def items(self):
        return self.store.snapshot()["items"]

    def plan(self):
        snap = self.store.snapshot()
        return next_step(snap["items"], snap["account_metrics"]["default"], snap["preferences"], datetime.now().astimezone(), session=True)

    def events_for(self, item_id, kind=None):
        return [e for e in self.store.snapshot()["events"] if e.get("item_id") == item_id and (kind is None or e.get("kind") == kind)]

    def path_of(self, item_id):
        return self.store.root / self.item(item_id)["pdf"]

    def notifications(self, item_id):
        seen = [k for i, k in self.store.snapshot()["notifications"]["seen"] if i == item_id]
        return seen, self.events_for(item_id, "notify")

    # -- shared assertions --
    def assert_handed_over(self, item_id, stage, kinds=("needs_action",), gate_blocked=True):
        """Item waits for the person: right stage, deduped notification, nothing planned, nothing adopted/rejected, file kept."""
        self.assertEqual(stage, stage_of(self.item(item_id)))
        for _ in range(3):                         # repeated monitor passes must not repeat the toast
            self.monitor.scan(self.monitor.items())
        seen, events = self.notifications(item_id)
        self.assertEqual(sorted(kinds), sorted(seen), "notification kinds")
        self.assertEqual(len(kinds), len(events), "one message-panel entry per notification (dedup)")
        self.assertEqual(len(kinds), len(self.toasts))
        for e in events:
            self.assertEqual("focus", e.get("action"))
            self.assertEqual(item_id, e["item_id"])
        step = self.plan()
        self.assertEqual(("wait_user", item_id, True), (step["action"], step["item_id"], step["requires_user"]))
        self.assertNotIn(step["action"], ("publish", "prepare", "adopt", "reject_auto"))
        self.assertIsNone(self.pipeline.tick())
        self.assertEqual([], self.fake.enqueued)   # not even with auto-publish / auto-adopt on
        if gate_blocked:
            self.assertTrue(gate(self.items(), 312)["blocked"])
        after = self.item(item_id)
        self.assertFalse(after.get("adopt_action") or after.get("reject_action") or after.get("rejected_files"))
        self.controls.click.assert_not_called()
        self.assertTrue(self.path_of(item_id).is_file())
        self.assertEqual(self.data, self.path_of(item_id).read_bytes())
        self.assertEqual("pending", deletion.view(after)["state"])   # the 24 h clock still applies; shown as "完结后 24 小时删除"
        self.assertTrue(deletion.view(after)["local_copy"])
        # every message-panel entry about this item carries item_id + an action
        for e in self.events_for(item_id):
            self.assertTrue(e.get("action"), e)

    def reject_by_human(self, item_id, category, detail="首页写的是另一篇文献，对不上"):
        key, reason = manual_reason(category, detail)
        self.store.update(item_id, manual_reject_category=key, manual_reject_reason=reason)
        before = self.path_of(item_id), self.data
        self.agent.reject(item_id)
        return before

    def assert_released(self, item_id, archived_path, data):
        """After the person acted: file archived (never deleted), gate open, next confirmed request is planned and enqueued."""
        after = self.item(item_id)
        self.assertEqual("", after["pdf"])
        self.assertEqual("submitted", stage_of(after))
        self.assertTrue(archived_path.is_file())
        self.assertEqual(data, archived_path.read_bytes())
        self.assertEqual(str(archived_path.relative_to(self.store.root)).replace("\\", "/"), after["rejected_files"][0]["pdf"].replace("\\", "/"))
        self.assertFalse(gate(self.items(), 312)["blocked"])
        step = self.plan()
        self.assertEqual(("publish", self.next_id), (step["action"], step["item_id"]))
        self.assertEqual(("publish", self.next_id), (self.pipeline.tick()["action"], self.fake.enqueued[-1][1]))
        self.assertEqual([("publish", self.next_id)], self.fake.enqueued)

    def assert_no_auto_reject(self, item_id):
        self.assertFalse(self.item(item_id).get("reject_action") or self.item(item_id).get("rejected_files"))
        self.controls.click.assert_not_called()


class AttentionBase(Base):
    """Files that cannot be parsed at all: item goes to stage 'attention' (verification status 'invalid')."""
    def setUp(self):
        if type(self) in (AttentionBase,):
            self.skipTest("abstract")
        super().setUp()

    def make(self):
        raise NotImplementedError

    def test_handover_and_manual_reject_releases_pipeline(self):
        item_id = self.receive(self.make())
        item = self.item(item_id)
        self.assertEqual("invalid", item["verification"]["status"])
        self.assertEqual("attention", item["status"])
        decision = rejection_decision(item, self.store.snapshot()["preferences"])
        self.assertEqual(("ask_user", ""), (decision["action"], decision["category"]))     # never an automatic reject
        self.assert_handed_over(item_id, "attention", gate_blocked=False)
        # the failure entry in the message panel carries the item and an action
        self.assertTrue(any(e.get("kind") == "error" and e.get("action") == "focus" for e in self.events_for(item_id)))
        # adopt is refused for a file that never reached the decision stage
        with self.assertRaises(ValueError):
            self.agent.adopt(item_id)
        # a second download of an item that already holds a (bad) local file is refused: re-downloading needs the reject first
        with self.assertRaises(ValueError):
            self.agent.download(item_id)
        path, data = self.reject_by_human(item_id, "cannot_open", "已用多个阅读器打开，文件无法打开")
        self.assert_released(item_id, path, data)
        rec = self.item(item_id)["rejected_files"][0]["reject_action"]
        self.assertEqual(("confirmed", "cannot_open", False), (rec["status"], rec["category"], rec["automatic"]))
        self.assertTrue(rec["reason"].startswith("文件无法打开"))

    def test_redownload_after_reject_lands_in_a_new_folder(self):
        item_id = self.receive(self.make())
        path, data = self.reject_by_human(item_id, "cannot_open", "已用多个阅读器打开，文件无法打开")
        # fresh sync says 待确认 again -> pipeline plans the download
        self.store.update(item_id, website_status="待确认", website_status_time=datetime.now().astimezone().isoformat(), download_file_url=FILE)
        step = self.plan()
        self.assertEqual(("download", item_id), (step["action"], step["item_id"]))
        good = correct_pdf()
        self.agent.save_download(item_id, FakeDownload(self.NAME, good), TITLE, DOI)
        again = self.item(item_id)
        self.assertEqual("awaiting_decision", stage_of(again))
        self.assertIn("answer-2", again["pdf"])
        self.assertEqual(data, path.read_bytes())                       # the unusable original is untouched
        self.assertEqual(good, (self.store.root / again["pdf"]).read_bytes())

    def test_gate_flag_stays_blocked(self):
        # Fixed 2026-10-20: an unusable saved answer (stage 'attention') still locks the gate in the UI.
        self.receive(self.make())
        self.assertTrue(gate(self.items(), 312)["blocked"])


class S1CorruptedPdf(AttentionBase):
    def make(self):
        return correct_pdf()[:90]          # cut in the middle of the header objects


class S1CorruptedPdfAuto(S1CorruptedPdf):
    auto = True


class NonPdfBase(Base):
    """H1: an answer that is not a PDF at all (HTML error page, empty file) is quarantined, never recorded as item.pdf."""
    def setUp(self):
        if type(self) is NonPdfBase:
            self.skipTest("abstract")
        super().setUp()

    def make(self):
        raise NotImplementedError

    def test_quarantined_and_not_recorded_as_the_answer(self):
        item_id = self.receive(self.make())
        item = self.item(item_id)
        self.assertFalse(item.get("pdf"))
        self.assertIsNone(item.get("verification"))
        self.assertEqual("attention", item["status"])
        self.assertIn("不是 PDF", item["note"])
        held = list((self.store.root / "quarantine").glob(item_id + "-*"))
        self.assertEqual(1, len(held))
        self.assertTrue(held[0].name.endswith(".quarantined"))
        self.assertEqual(self.data, held[0].read_bytes())
        self.assertEqual([], list((self.store.root / "downloads" / item_id).glob("*")))
        self.assertTrue(any(e.get("kind") == "error" and e.get("action") == "focus" for e in self.events_for(item_id)))
        self.assertEqual("attention", stage_of(item))
        self.assertTrue(gate(self.items(), 312)["blocked"])      # the unusable answer still locks publishing
        self.assertEqual([], self.fake.enqueued)
        self.assertIsNone(self.pipeline.tick())                  # nothing is planned for it, and nothing was adopted or rejected
        self.assert_no_auto_reject(item_id)

    def test_a_later_good_answer_is_saved_normally(self):
        item_id = self.receive(self.make())
        self.store.update(item_id, status="fulfilled", website_status="待确认", download_file_url=FILE)
        good = correct_pdf()
        self.agent.save_download(item_id, FakeDownload(self.NAME, good), TITLE, DOI)
        again = self.item(item_id)
        self.assertEqual("awaiting_decision", stage_of(again))
        self.assertEqual(good, (self.store.root / again["pdf"]).read_bytes())


class S2aHtmlErrorPage(NonPdfBase):
    NAME = "download.pdf"

    def make(self):
        return HTML


class S2aHtmlErrorPageAuto(S2aHtmlErrorPage):
    auto = True


class S2bEmptyFile(NonPdfBase):
    def make(self):
        return b""


class S2bEmptyFileAuto(S2bEmptyFile):
    auto = True


class ExecutableSuffixTests(Base):
    """H1: whatever extension the site suggests, the answer is saved as .pdf."""
    def test_suffix_forced_to_pdf(self):
        for name in ("paper.pdf.exe", "paper.scr", "paper.lnk", "noext", "Original Name.PDF"):
            with self.subTest(name=name):
                item_id = self.receive(correct_pdf(), name)
                saved = self.item(item_id)
                self.assertTrue(saved["pdf"].lower().endswith(".pdf"), saved["pdf"])
                self.assertTrue(saved["local_filename"].lower().endswith(".pdf"))
                self.assertEqual("awaiting_decision", stage_of(saved))
                self.store.update(item_id, source="import", status="ready", website_status="", assist_url="")   # keep later subtests independent


class S3PasswordProtected(AttentionBase):
    def make(self):
        return encrypted_pdf()


class S3PasswordProtectedAuto(S3PasswordProtected):
    auto = True


class DecisionBase(Base):
    """Files that parse but cannot be confirmed: item waits in 'awaiting_decision' (gate closed)."""

    def adopt_by_human(self, item_id):
        self.agent.adopt(item_id)
        after = self.item(item_id)
        self.assertEqual(("adopted", "done"), (after["status"], stage_of(after)))
        self.assertTrue(self.path_of(item_id).is_file())
        self.assertFalse(gate(self.items(), 312)["blocked"])
        step = self.plan()
        self.assertEqual(("publish", self.next_id), (step["action"], step["item_id"]))
        self.assertEqual(("publish", self.next_id), (self.pipeline.tick()["action"], self.fake.enqueued[-1][1]))
        # deletion clock: completion makes the site file expire after 24 h; the local copy stays
        self.assertEqual("scheduled", deletion.view(self.item(item_id))["state"])
        deletion.sweep(self.store, self.notifier)                               # first sighting of the completion is stamped (an estimate)
        deletion.sweep(self.store, self.notifier, datetime.now().astimezone() + timedelta(hours=25))
        gone = self.item(item_id)
        self.assertTrue(gone["site_file_deleted"])
        self.assertTrue(self.path_of(item_id).is_file())
        self.assertIn("本地副本保留", deletion.view(gone)["text"])


class S4ScannedImageOnly(DecisionBase):
    def test_goes_to_human_who_may_adopt(self):
        item_id = self.receive(scanned_pdf())
        self.assertEqual("ocr_required", self.item(item_id)["verification"]["status"])
        self.assertEqual("ask_user", self.item(item_id)["rejection"]["action"])
        self.assert_handed_over(item_id, "awaiting_decision")
        self.assert_no_auto_reject(item_id)
        self.adopt_by_human(item_id)

    def test_scan_is_not_an_allowed_reject_reason(self):
        item_id = self.receive(scanned_pdf())
        with self.assertRaises(ValueError):
            manual_reason("scan_unclear", "扫描版不够清晰")
        self.assertEqual("awaiting_decision", stage_of(self.item(item_id)))


class S4ScannedImageOnlyAuto(S4ScannedImageOnly):
    auto = True


class S5DifferentPaper(DecisionBase):
    def test_conflict_detected_with_user_doi(self):
        item_id = self.receive(other_paper_pdf())
        after = self.item(item_id)
        v = after["verification"] or after["rejected_files"][0]["verification"]   # auto mode has already archived it
        self.assertEqual(("conflict", ["10.9999/other.77"]), (v["status"], v["primary_dois"]))
        self.assertLess(v["title_score"], 65)
        decision = after["rejection"] or {}
        if not self.auto:
            self.assertTrue(decision["definite_mismatch"])
            self.assertEqual(("ask_user", "file_mismatch"), (decision["action"], decision["category"]))
            self.assert_handed_over(item_id, "awaiting_decision")
            self.assert_no_auto_reject(item_id)
            path, data = self.reject_by_human(item_id, "file_mismatch")
            self.assert_released(item_id, path, data)
            self.assertFalse(self.item(item_id)["rejected_files"][0]["reject_action"]["automatic"])
        else:
            # the one automatic reject: first-page DOI is another paper AND the title does not fit
            rec = after["rejected_files"][0]["reject_action"]
            self.assertEqual(("confirmed", "file_mismatch", True), (rec["status"], rec["category"], rec["automatic"]))
            self.assertTrue(rec["reason"].startswith("文件不一致："))
            self.controls.click.assert_called_once()
            archived = self.store.root / after["rejected_files"][0]["pdf"]
            self.assertEqual(self.data, archived.read_bytes())                      # kept on disk, byte for byte
            self.assertEqual("submitted", stage_of(after))
            self.assertFalse(gate(self.items(), 312)["blocked"])
            self.assertEqual("publish", self.plan()["action"])
            self.assertEqual("publish", self.pipeline.tick()["action"])
            self.assertEqual([("publish", self.next_id)], self.fake.enqueued)
            # the panel tells the person what happened, with item and action
            done = self.events_for(item_id, "reject")
            self.assertTrue(done and done[0].get("action") == "focus")

    def test_site_filled_doi_never_auto_rejects(self):
        item_id = self.receive(other_paper_pdf(), doi_origin="site")
        decision = self.item(item_id)["rejection"]
        self.assertEqual("ask_user", decision["action"])
        self.assertFalse(decision["definite_mismatch"])
        self.assert_handed_over(item_id, "awaiting_decision")
        self.assert_no_auto_reject(item_id)
        path, data = self.reject_by_human(item_id, "file_mismatch")
        self.assert_released(item_id, path, data)

    def test_completed_request_is_never_rejected(self):
        # website_status already finished -> not eligible even in auto mode (decision level, no page involved)
        decision = rejection_decision(dict(source="website", owned=True, pdf="d/x.pdf", doi=DOI, doi_origin="user", assist_url=URL,
                                           website_status="已完结", download_file_url=FILE,
                                           verification=dict(status="conflict", primary_dois=["10.9999/other.77"], title_score=10, sha256="x")),
                                      self.store.snapshot()["preferences"])
        self.assertNotEqual("auto_reject", decision["action"])


class S5DifferentPaperAuto(S5DifferentPaper):
    auto = True


class S6TargetDoiOnlyInReferences(DecisionBase):
    def test_reference_list_doi_does_not_count(self):
        item_id = self.receive(references_only_pdf())
        v = self.item(item_id)["verification"]
        self.assertNotEqual("identity_match", v["status"])
        self.assertNotIn(DOI, v["primary_dois"])
        self.assertEqual("ask_user", self.item(item_id)["rejection"]["action"])    # no first-page DOI -> not a definite mismatch
        self.assert_handed_over(item_id, "awaiting_decision")
        self.assert_no_auto_reject(item_id)
        path, data = self.reject_by_human(item_id, "file_mismatch", "目标 DOI 只出现在参考文献里，正文是另一篇")
        self.assert_released(item_id, path, data)


class S6TargetDoiOnlyInReferencesAuto(S6TargetDoiOnlyInReferences):
    auto = True


class S7PreprintOrMissingPages(DecisionBase):
    def test_goes_to_human_not_adopted(self):
        item_id = self.receive(preprint_pdf())
        v = self.item(item_id)["verification"]
        self.assertEqual("review", v["status"])                                      # DOI+title fit, authors/abstract unverified
        self.assertEqual(1, v["pages"])
        self.assertEqual("ask_user", self.item(item_id)["rejection"]["action"])
        self.assert_handed_over(item_id, "awaiting_decision")
        self.assert_no_auto_reject(item_id)
        path, data = self.reject_by_human(item_id, "missing_pages", "官网正式版有 12 页，这份只有 1 页")
        self.assert_released(item_id, path, data)

    def test_human_may_keep_it_instead(self):
        item_id = self.receive(preprint_pdf())
        self.assert_handed_over(item_id, "awaiting_decision")
        self.adopt_by_human(item_id)


class S7PreprintOrMissingPagesAuto(S7PreprintOrMissingPages):
    auto = True


class S8CorrectPaperControl(DecisionBase):
    def test_correct_file(self):
        item_id = self.receive(correct_pdf())
        v = self.item(item_id)["verification"]
        self.assertEqual("identity_match", v["status"])
        self.assertEqual("match", v["bibliography"]["status"])
        self.assertEqual("none", self.item(item_id)["rejection"]["action"])
        self.assert_no_auto_reject(item_id)
        self.assertEqual("awaiting_decision", stage_of(self.item(item_id)))
        self.assertFalse(self.item(item_id).get("adopt_action"))                    # saving never adopts by itself
        if not self.auto:
            self.assert_handed_over(item_id, "awaiting_decision", kinds=("download_ok",))
            self.adopt_by_human(item_id)
        else:
            for _ in range(2):
                self.monitor.scan(self.monitor.items())
            seen, _ = self.notifications(item_id)
            self.assertEqual(["download_ok"], seen)
            step = self.plan()
            self.assertEqual(("adopt", item_id, False), (step["action"], step["item_id"], step["requires_user"]))
            self.assertEqual("adopt", self.pipeline.tick()["action"])
            self.assertEqual([("adopt", item_id)], self.fake.enqueued)
            self.agent.adopt(item_id)                                               # what the queued step then does
            self.assertEqual("done", stage_of(self.item(item_id)))
            self.assertEqual(("publish", self.next_id), (self.plan()["action"], self.plan()["item_id"]))

    def test_full_match_but_auto_adopt_off_waits(self):
        self.store.update_preferences(pipeline_auto_adopt=False)
        item_id = self.receive(correct_pdf())
        step = self.plan()
        self.assertEqual(("wait_user", item_id), (step["action"], step["item_id"]))
        self.assertTrue(gate(self.items(), 312)["blocked"])


class S8CorrectPaperControlAuto(S8CorrectPaperControl):
    auto = True


# ---- through the HTTP API -----------------------------------------------------------------------------------------------
class ApiHandoverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.server = make_server(0, self.temp.name, start_monitor=False, auto_screen=False)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = "http://127.0.0.1:%d" % self.server.server_port
        self.token = urlopen(self.base).read().decode().split("const TOKEN=")[1].split(";let state=")[0].strip('"')
        self.addCleanup(self.stop)
        patcher = patch.object(metadata, "fetch_metadata", side_effect=lambda *a, **k: json.loads(json.dumps(META)))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.store = self.server.store
        self.store.update_account("default", points=312, last_sync_time=datetime.now().astimezone().isoformat())
        self.store.update_preferences(oa_first=False)
        self.server.agent.enqueue = MagicMock()          # never let a real browser command run
        self.next_id = self.store.add("Next", "10.1234/next", source="import", account_id="default", request_confirmed=True, oa_check=dict(status="not_oa"))

    def stop(self):
        self.server.pipeline.close()
        self.server.shutdown()
        self.server.server_close()
        self.server.agent.commands.put(("close", None, None))
        self.server.agent.thread.join(timeout=2)

    def post(self, route, body):
        request = Request(self.base + "/api/" + route, json.dumps(body).encode(), {"Content-Type": "application/json", "Origin": self.base, "X-Demo-Token": self.token})
        return json.load(urlopen(request))

    def bad(self, route, body):
        with self.assertRaises(HTTPError) as caught:
            self.post(route, body)
        self.assertEqual(400, caught.exception.code)

    def state(self):
        return json.load(urlopen(self.base + "/api/state"))

    def receive(self, data, n=0):
        title, doi = (TITLE, DOI) if not n else ("Another title %d" % n, "10.1234/t%d" % n)
        item_id = self.store.add(title, doi, source="website", account_id="default", owned=True, assist_url=URL + str(n), website_status="待确认",
                                 download_file_url=FILE + str(n), doi_origin="user", status="fulfilled")
        try:
            self.server.agent.save_download(item_id, FakeDownload("A%d.pdf" % n, data), title, doi)
        except ValueError:
            pass
        return item_id

    def view(self, item_id):
        return next(i for i in self.state()["items"] if i["id"] == item_id)

    def test_wrong_paper_api_view_and_manual_reject(self):
        item_id = self.receive(other_paper_pdf())
        state = self.state()
        self.assertEqual("awaiting_decision", self.view(item_id)["stage"])
        self.assertEqual("pending", self.view(item_id)["deletion"]["state"])
        self.assertTrue(state["pipeline"]["gate"]["blocked"])
        self.assertEqual(item_id, state["pipeline"]["gate"]["item_id"])
        self.assertEqual(("wait_user", item_id), (state["pipeline"]["next_step"]["action"], state["pipeline"]["next_step"]["item_id"]))
        self.assertEqual("ask_user", self.view(item_id)["rejection"]["action"])
        self.assertTrue(any(e.get("item_id") == item_id and e.get("action") for e in state["events"]))
        # reject needs confirmation, one of the 4 categories and a concrete detail
        self.bad("browser", {"action": "reject", "id": item_id, "category": "file_mismatch", "detail": "首页是另一篇文献"})
        self.bad("browser", {"action": "reject", "id": item_id, "category": "scan_unclear", "detail": "扫描不清楚的文件", "confirmed": True})
        self.bad("browser", {"action": "reject", "id": item_id, "category": "file_mismatch", "detail": "", "confirmed": True})
        self.server.agent.enqueue.assert_not_called()
        self.post("browser", {"action": "reject", "id": item_id, "category": "file_mismatch", "detail": "首页是另一篇文献", "confirmed": True})
        self.server.agent.enqueue.assert_called_once_with("reject", item_id, None)
        after = self.store.get(item_id)
        self.assertEqual("file_mismatch", after["manual_reject_category"])
        self.assertTrue(after["manual_reject_reason"].startswith("文件不一致："))
        self.assertTrue((self.store.root / after["pdf"]).is_file())

    def test_unreadable_file_api_view_adopt_refused_reject_allowed(self):
        for n, data in enumerate((encrypted_pdf(), correct_pdf()[:90]), 1):
            with self.subTest(size=len(data)):
                self.server.agent.enqueue.reset_mock()
                item_id = self.receive(data, n)
                view = self.view(item_id)
                self.assertEqual(("attention", "invalid"), (view["stage"], view["verification"]["status"]))
                self.assertEqual("wait_user", self.state()["pipeline"]["next_step"]["action"])
                self.bad("browser", {"action": "adopt", "id": item_id, "confirmed": True})
                self.post("browser", {"action": "reject", "id": item_id, "category": "cannot_open", "detail": "多次下载、换软件均无法打开", "confirmed": True})
                self.server.agent.enqueue.assert_called_once_with("reject", item_id, None)
                self.assertTrue((self.store.root / self.store.get(item_id)["pdf"]).is_file())
                self.store.update(item_id, adopt_action=None, reject_action=None, status="rejected", rejected_at=datetime.now().astimezone().isoformat())   # as after a confirmed reject

    def test_after_adopt_gate_opens_and_next_item_planned(self):
        item_id = self.receive(correct_pdf())
        self.assertTrue(self.state()["pipeline"]["gate"]["blocked"])
        self.store.update(item_id, adopt_action=dict(status="confirmed", time=datetime.now().astimezone().isoformat()), status="adopted")
        state = self.state()
        self.assertFalse(state["pipeline"]["gate"]["blocked"])
        self.assertEqual("done", self.view(item_id)["stage"])
        self.assertEqual(("prepare", self.next_id), (state["pipeline"]["next_step"]["action"], state["pipeline"]["next_step"]["item_id"]))


if __name__ == "__main__":
    unittest.main()


class GateIgnoresFinishedRequests(unittest.TestCase):
    def test_finished_request_with_failed_download_does_not_lock(self):
        from pipeline import DONE
        item = dict(id="x", source="website", status="attention", website_status=DONE[0],
                    assist_url="https://www.ablesci.com/assist/detail?id=X", download_attempts=[{"line": 1}])
        self.assertFalse(gate([item], 312)["blocked"])
