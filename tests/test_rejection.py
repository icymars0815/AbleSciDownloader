import unittest
from rejection_policy import rejection_decision, REJECTION_RULES_VERSION


class RejectionTests(unittest.TestCase):
    def setUp(self):
        self.item = dict(source="website", owned=True, pdf="downloads/test.pdf", doi="10.1234/target", doi_origin="user",
                         assist_url="https://www.ablesci.com/assist/detail?id=ABC123", website_status="待处理",
                         download_file_url="https://www.ablesci.com/assist/download?id=FILE123",
                         verification=dict(status="conflict", primary_dois=["10.1234/wrong"], title_score=30, sha256="fixture"))
        self.prefs = dict(rejection_mode="auto", rejection_rules_version=REJECTION_RULES_VERSION)

    def test_default_manual(self):
        self.assertEqual("ask_user", rejection_decision(self.item, {})["action"])

    def test_explicit_auto_rejects_two_independent_conflicts(self):
        self.assertEqual("auto_reject", rejection_decision(self.item, self.prefs)["action"])

    def test_missing_metadata_is_not_auto_rejection(self):
        self.item["verification"] = dict(status="review", title_score=100, primary_dois=[self.item["doi"]], sha256="fixture")
        self.assertEqual("ask_user", rejection_decision(self.item, self.prefs)["action"])

    def test_matching_title_prevents_auto_rejection(self):
        self.item["verification"]["title_score"] = 95
        self.assertNotEqual("auto_reject", rejection_decision(self.item, self.prefs)["action"])

    def test_missing_doi_is_not_rejection(self):
        self.item["verification"]["primary_dois"] = []
        self.assertNotEqual("auto_reject", rejection_decision(self.item, self.prefs)["action"])

    def test_completed_request_never_rejected(self):
        self.item["website_status"] = "已完结"
        self.assertFalse(rejection_decision(self.item, self.prefs)["eligible"])

    def test_demo_never_rejected_online(self):
        self.item["source"] = "demo"
        self.assertFalse(rejection_decision(self.item, self.prefs)["eligible"])

    def test_unowned_request_never_rejected(self):
        self.item["owned"] = False
        self.assertFalse(rejection_decision(self.item, self.prefs)["eligible"])

    def test_uncertain_attempt_never_retried(self):
        self.item["reject_action"] = {"status": "uncertain"}
        self.assertNotEqual("auto_reject", rejection_decision(self.item, self.prefs)["action"])

    def test_changed_rules_require_new_consent(self):
        self.prefs["rejection_rules_version"] = "old"
        self.assertNotEqual("auto_reject", rejection_decision(self.item, self.prefs)["action"])

    def test_no_download_evidence_disables_rejection(self):
        self.item.pop("download_file_url")
        self.assertFalse(rejection_decision(self.item, self.prefs)["eligible"])
