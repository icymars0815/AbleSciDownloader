import base64
import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

import pymupdf

from app import make_server
from core import Store, seed_demo, verify_pdf, site_url, checkin_result
from download_policy import RULES_VERSION


class VerificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        seed_demo(self.store)
        self.items = {i["demo_name"]: i for i in self.store.snapshot()["items"]}

    def tearDown(self):
        self.temp.cleanup()

    def check(self, name):
        item = self.items[name]
        return verify_pdf(self.store.root / item["pdf"], item["expected_title"], item["expected_doi"])

    def test_matching_front_page(self):
        self.assertEqual(self.check("matching")["status"], "identity_match")

    def test_pdf_verification_needs_no_network(self):
        with patch("socket.socket", side_effect=AssertionError("network unavailable")):
            self.assertEqual("identity_match", self.check("matching")["status"])

    def test_wrong_file_is_review_not_automatic_rejection(self):
        self.assertEqual(self.check("wrong")["status"], "conflict")
        self.assertNotIn("reject", self.check("wrong"))

    def test_reference_only_doi_never_matches(self):
        result = self.check("reference")
        self.assertNotEqual(result["status"], "identity_match")
        self.assertNotIn("10.0000/demo.match.001", result["primary_dois"])

    def test_correct_doi_wrong_title_never_matches(self):
        item = self.items["matching"]
        result = verify_pdf(self.store.root / item["pdf"], "A completely different physics paper", item["expected_doi"])
        self.assertNotEqual(result["status"], "identity_match")

    def test_missing_doi_never_auto_matches(self):
        item = self.items["matching"]
        self.assertEqual(verify_pdf(self.store.root / item["pdf"], item["expected_title"], "")["status"], "review")

    def test_empty_scan_requires_ocr(self):
        path = self.store.root / "scan.pdf"
        doc = pymupdf.open()
        doc.new_page()
        doc.save(path)
        doc.close()
        self.assertEqual(verify_pdf(path, "Title", "10.0000/demo")["status"], "ocr_required")

    def test_corrupt_pdf_is_not_success(self):
        path = self.store.root / "corrupt.pdf"
        path.write_bytes(b"%PDF-not-complete")
        self.assertEqual(verify_pdf(path, "Title", "")["status"], "invalid")

    def test_queue_dedup_and_persistence(self):
        first = self.store.add("Title", "10.0000/ABC")
        self.assertEqual(first, self.store.add("Title again", "https://doi.org/10.0000/abc"))
        self.assertIsNotNone(Store(self.temp.name).get(first))

    def test_demo_reload_is_idempotent(self):
        seed_demo(self.store)
        self.assertEqual(3, len(self.store.snapshot()["items"]))

    def test_strict_site_allowlist(self):
        self.assertTrue(site_url("https://www.ablesci.com/assist/detail?id=Ab12Cd", "/assist/detail"))
        self.assertFalse(site_url("https://www.ablesci.com.evil.test/assist/detail?id=Ab12Cd", "/assist/detail"))
        self.assertFalse(site_url("https://user@www.ablesci.com/assist/detail?id=Ab12Cd", "/assist/detail"))
        self.assertFalse(site_url("https://www.ablesci.com/assist/detail?id=../../private", "/assist/detail"))

    def test_checkin_success(self):
        self.assertEqual("签到成功", checkin_result({"code": 0, "msg": "签到成功"}))

    def test_already_signed_in_is_successful_noop(self):
        self.assertEqual("今日已签到", checkin_result({"code": 1, "msg": "签到失败，您今天已于 [00:26:56] 签到。"}))

    def test_checkin_failure_not_reported_as_success(self):
        with self.assertRaises(ValueError):
            checkin_result({"code": 1, "msg": "请先登录"})


class LocalApiTests(unittest.TestCase):
    def setUp(self):
        self.metadata_patch = patch("metadata.fetch_metadata", return_value={"status": "unavailable", "reason": "offline test"})
        self.metadata_patch.start()
        self.addCleanup(self.metadata_patch.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.server = make_server(0, self.temp.name, auto_screen=False)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        html = urlopen(self.base).read().decode()
        self.token = html.split("const TOKEN=")[1].split(";let state=")[0].strip('"')

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.server.agent.commands.put(("close", None, None))
        self.server.agent.thread.join(timeout=2)
        self.thread.join()
        self.temp.cleanup()

    def post(self, route, payload=None, token=None, origin=None):
        request = Request(self.base + "/api/" + route, json.dumps(payload or {}).encode(), method="POST", headers={"Content-Type": "application/json", "Origin": origin or self.base, "X-Demo-Token": self.token if token is None else token})
        return urlopen(request)

    def test_forbids_cross_site_requests(self):
        with self.assertRaises(HTTPError) as caught:
            self.post("demo", origin="https://evil.test")
        self.assertEqual(403, caught.exception.code)

    def test_forbids_bad_token(self):
        with self.assertRaises(HTTPError) as caught:
            self.post("demo", token="wrong")
        self.assertEqual(403, caught.exception.code)

    def test_paid_fallback_requires_consent_and_persists_switch(self):
        self.assertFalse(self.server.store.snapshot().get("preferences", {}).get("allow_paid_fallback"))
        with self.assertRaises(HTTPError) as caught:
            self.post("preferences", {"allow_paid_fallback": True})
        self.assertEqual(400, caught.exception.code)
        self.post("preferences", {"allow_paid_fallback": True, "rules_version": RULES_VERSION}).close()
        self.assertTrue(Store(self.temp.name).snapshot()["preferences"]["allow_paid_fallback"])
        self.post("preferences", {"allow_paid_fallback": False}).close()
        self.assertFalse(Store(self.temp.name).snapshot()["preferences"]["allow_paid_fallback"])

    def test_paid_fallback_rejects_non_boolean(self):
        with self.assertRaises(HTTPError) as caught:
            self.post("preferences", {"allow_paid_fallback": "false"})
        self.assertEqual(400, caught.exception.code)

    def test_auto_rejection_requires_rule_confirmation(self):
        with self.assertRaises(HTTPError) as caught:
            self.post("preferences", {"rejection_mode": "auto"})
        self.assertEqual(400, caught.exception.code)
        self.post("preferences", {"rejection_mode": "auto", "rejection_rules_version": RULES_VERSION}).close()
        self.assertEqual("auto", self.server.store.snapshot()["preferences"]["rejection_mode"])
        self.post("preferences", {"rejection_mode": "manual"}).close()

    def test_manual_rejection_rejects_imported_item(self):
        self.post("import", {"text": "A local paper"}).close()
        item = self.server.store.snapshot()["items"][0]
        with patch.object(self.server.agent, "enqueue") as enqueue:
            with self.assertRaises(HTTPError) as caught:
                self.post("browser", {"action": "reject", "id": item["id"], "confirmed": True, "reason": "Wrong paper"})
            self.assertEqual(400, caught.exception.code)
            enqueue.assert_not_called()

    def test_account_queue_and_mcp_permissions_do_not_leak_across_accounts(self):
        self.post("import", {"text": "10.1234/shared\tFirst-account fixture"}).close()
        self.post("preferences", {"mcp_website_actions": True}).close()
        other = self.server.accounts.add("Second test account")
        self.server.agent.switch_account(other)
        state = json.load(urlopen(self.base + "/api/state"))
        self.assertEqual([], state["items"])
        self.assertFalse(state["preferences"]["mcp_website_actions"])
        with self.assertRaises(HTTPError):
            self.post("browser", {"action": "signin", "mcp_call": True})
        self.post("import", {"text": "10.1234/shared\tSecond-account fixture"}).close()
        self.assertEqual(2, len(self.server.store.snapshot()["items"]))
        self.assertEqual(other, json.load(urlopen(self.base + "/api/state"))["items"][0]["account_id"])

    def test_demo_verify_and_export(self):
        self.post("demo").close()
        state = json.load(urlopen(self.base + "/api/state"))
        self.assertEqual(3, len(state["items"]))
        for item in state["items"]:
            self.post("verify", {"id": item["id"]}).close()
        state = json.load(urlopen(self.base + "/api/state"))
        self.assertEqual(1, sum(i["verification"]["status"] == "identity_match" for i in state["items"]))
        self.assertIn("identity_match", urlopen(self.base + "/api/export").read().decode("utf-8-sig"))

    def test_reject_action_is_unavailable(self):
        with self.assertRaises(HTTPError) as caught:
            self.post("browser", {"action": "reject", "id": "anything"})
        self.assertEqual(400, caught.exception.code)

    def test_import_dedup(self):
        self.post("import", {"text": "10.0000/demo\tA test title\n10.0000/demo\tA test title"}).close()
        self.assertEqual(1, len(json.load(urlopen(self.base + "/api/state"))["items"]))

    def test_invalid_upload(self):
        self.post("import", {"text": "A test title"}).close()
        item = json.load(urlopen(self.base + "/api/state"))["items"][0]
        with self.assertRaises(HTTPError):
            self.post("upload", {"id": item["id"], "base64": base64.b64encode(b"not a PDF").decode()})
        self.assertFalse(self.server.store.get(item["id"]).get("pdf"))

    def test_unreadable_pdf_preview_returns_clear_error(self):
        self.post("import", {"text": "Fictional corrupt file fixture"}).close()
        item = self.server.store.snapshot()["items"][0]
        self.post("upload", {"id": item["id"], "base64": base64.b64encode(b"%PDF-not-complete").decode()}).close()
        with self.assertRaises(HTTPError) as caught:
            urlopen(self.base + "/pdf-preview/" + item["id"] + "?page=0")
        self.assertEqual(400, caught.exception.code)

    def test_credentials_not_persisted_or_returned(self):
        with patch.object(self.server.agent, "enqueue") as enqueue:
            self.post("browser", {"action": "login", "email": "demo@example.invalid", "password": "fake-test-password"}).close()
            self.assertEqual(enqueue.call_args.args[2]["password"], "fake-test-password")
        self.assertNotIn("fake-test-password", json.dumps(self.server.store.snapshot()))
        self.assertNotIn("demo@example.invalid", urlopen(self.base + "/api/state").read().decode())

    def test_incomplete_credentials_rejected(self):
        with self.assertRaises(HTTPError) as caught:
            self.post("browser", {"action": "login", "email": "demo@example.invalid"})
        self.assertEqual(400, caught.exception.code)

    def test_saved_credentials_are_encrypted_and_can_be_reused(self):
        with patch.object(self.server.agent, "enqueue") as enqueue:
            self.post("browser", {"action": "login", "email": "fixture@example.invalid", "password": "fictional-password", "save_credentials": True}).close()
            self.assertTrue(json.load(urlopen(self.base + "/api/state"))["credentials_saved"])
            self.post("browser", {"action": "login", "use_saved": True}).close()
            self.assertEqual("fictional-password", enqueue.call_args.args[2]["password"])
        self.assertNotIn("fictional-password", urlopen(self.base + "/api/state").read().decode())
        self.post("forget-credentials").close()
        self.assertFalse(json.load(urlopen(self.base + "/api/state"))["credentials_saved"])

    def test_paste_preview_does_not_import_or_use_ai(self):
        result = json.load(self.post("parse", {"text": "10.1234/test\tFictional paper"}))
        self.assertFalse(result["ai_used"])
        self.assertEqual(1, len(result["records"]))
        self.assertEqual([], self.server.store.snapshot()["items"])

    def test_import_preview_records_cannot_set_ownership(self):
        self.post("import", {"records": [{"title": "Fictional record", "doi": "10.1234/test", "authors": ["Alice Smith"], "owned": True, "source": "website"}]}).close()
        item = self.server.store.snapshot()["items"][0]
        self.assertEqual("import", item["source"])
        self.assertFalse(item.get("owned"))

    def test_valid_upload_and_overwrite_guard(self):
        self.post("import", {"text": "10.0000/demo.match.001\tDemonstration article: Citrus flavonoids and gut microbiota"}).close()
        item = json.load(urlopen(self.base + "/api/state"))["items"][0]
        seed_demo(self.server.store)
        sample = next(i for i in self.server.store.snapshot()["items"] if i.get("demo_name") == "matching")
        binary = (self.server.store.root / sample["pdf"]).read_bytes()
        payload = {"id": item["id"], "base64": base64.b64encode(binary).decode()}
        self.post("upload", payload).close()
        self.assertEqual("review", self.server.store.get(item["id"])["verification"]["status"])
        with self.assertRaises(HTTPError):
            self.post("upload", payload)
        self.assertEqual(binary, urlopen(self.base + "/pdf/" + item["id"]).read())


if __name__ == "__main__":
    unittest.main()
