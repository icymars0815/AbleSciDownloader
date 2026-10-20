import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
import pymupdf
from accounts import Accounts
from core import Store
from browser_agent import BrowserAgent
from task_states import workflow_state
from ai_service import AIService


class AccountTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.accounts = Accounts(self.temp.name)
        self.store = Store(self.temp.name)

    def test_profiles_and_credentials_are_isolated(self):
        other = self.accounts.add("Second account")
        self.accounts.vault().save("first@example.invalid", "first-test-password")
        self.accounts.switch(other)
        self.assertFalse(self.accounts.vault().exists())
        self.assertNotEqual(self.accounts.account_root(), self.accounts.account_root("default"))
        self.accounts.vault().save("second@example.invalid", "second-test-password")
        self.assertEqual("first@example.invalid", self.accounts.vault("default").load()["email"])
        self.assertEqual(other, Accounts(self.temp.name).active_id)

    def test_queue_dedup_is_per_account(self):
        first = self.store.add("Same paper", "10.1234/same", account_id="default")
        second = self.store.add("Same paper", "10.1234/same", account_id="second")
        self.assertNotEqual(first, second)

    def test_public_accounts_do_not_contain_passwords_or_email(self):
        self.accounts.vault().save("fixture@example.invalid", "fictional-password")
        public = json.dumps(self.accounts.public())
        self.assertNotIn("fictional-password", public)
        self.assertNotIn("fixture@example.invalid", public)

    def test_unknown_account_is_rejected(self):
        with self.assertRaises(ValueError): self.accounts.switch("not-registered")

    def agent(self):
        agent = BrowserAgent.__new__(BrowserAgent)
        agent.store, agent.accounts, agent.context, agent.page = self.store, self.accounts, None, None
        return agent

    @patch("playwright.sync_api.sync_playwright")
    def test_background_browser_never_requests_popup(self, factory):
        engine = factory.return_value.start.return_value
        context = engine.chromium.launch_persistent_context.return_value
        context.pages = [MagicMock()]
        agent = self.agent()
        agent.ensure()
        options = engine.chromium.launch_persistent_context.call_args.kwargs
        self.assertTrue(options["headless"])
        self.assertEqual([], options["args"])

    @patch("playwright.sync_api.sync_playwright")
    def test_explicit_login_requests_popup(self, factory):
        engine = factory.return_value.start.return_value
        page = MagicMock()
        page.url = "https://www.ablesci.com/site/login"
        engine.chromium.launch_persistent_context.return_value.pages = [page]
        agent = self.agent()
        agent.login(None)
        options = engine.chromium.launch_persistent_context.call_args.kwargs
        self.assertFalse(options["headless"])
        self.assertTrue(any(a.startswith("--app=") for a in options["args"]))

    def test_pending_verification_window_is_not_closed_by_sync(self):
        agent = self.agent()
        context = MagicMock()
        agent.context = context
        agent.page = MagicMock()
        agent.page.url = "https://www.ablesci.com/site/login"
        agent.interactive = True
        with self.assertRaises(ValueError): agent.ensure()
        context.close.assert_not_called()

    def test_switch_account_closes_old_context_without_new_popup(self):
        other = self.accounts.add("Second account")
        agent = self.agent()
        old = MagicMock()
        agent.context = old
        agent.switch_account(other)
        old.close.assert_called_once()
        self.assertIsNone(agent.context)
        self.assertEqual(other, agent.accounts.active_id)

    def test_cross_account_download_is_blocked(self):
        agent = self.agent()
        with self.assertRaises(ValueError): agent.assert_account({"account_id": "other"})


class WorkflowTests(unittest.TestCase):
    def test_distinct_requested_workflow_states(self):
        self.assertEqual("queued", workflow_state({"status": "ready"}))
        self.assertEqual("downloading", workflow_state({"status": "downloading"}))
        self.assertEqual("review", workflow_state({"status": "checked", "pdf": "file.pdf", "verification": {"status": "review"}}))
        self.assertEqual("downloaded", workflow_state({"status": "checked", "pdf": "file.pdf", "verification": {"status": "identity_match"}}))


class AISettingsTests(unittest.TestCase):
    def setUp(self):
        self.service = AIService()

    def test_default_off_and_key_never_returned(self):
        self.assertFalse(self.service.public_config()["enabled"])
        self.service.configure(True, "fictional-ds-key", consent=True)
        self.assertNotIn("fictional-ds-key", json.dumps(self.service.public_config()))
        self.service.configure(False)
        self.assertFalse(self.service.public_config()["key_configured"])

    def test_settings_require_key_and_consent(self):
        with self.assertRaises(ValueError): self.service.configure(True, "", consent=True)
        with self.assertRaises(ValueError): self.service.configure(True, "fictional-ds-key", consent=False)

    def test_per_call_consent_required(self):
        self.service.configure(True, "fictional-ds-key", consent=True)
        with patch("ai_service.build_opener") as network:
            with self.assertRaises(ValueError): self.service.recognize("unused.pdf", [0], False)
            network.assert_not_called()

    def test_invalid_page_selection_never_sent(self):
        self.service.configure(True, "fictional-ds-key", consent=True)
        for pages in ([], [0, 0], [-1], [0, 1, 2, 3]):
            with self.assertRaises(ValueError): self.service.recognize("unused.pdf", pages, True)

    def test_ai_extracts_separately_and_only_sends_selected_image(self):
        self.service.configure(True, "fictional-ds-key", consent=True)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "private-path.pdf"
            doc = pymupdf.open();doc.new_page().insert_text((50,50),"Fictional software fixture");doc.save(path);doc.close()
            payload = {"choices": [{"message": {"content": json.dumps({"title": "Fictional title", "doi": "10.1234/fiction", "authors": ["Alice Smith"], "abstract": "", "evidence": ["Fictional title"]})}}]}
            with patch("ai_service.build_opener") as opener:
                opener.return_value.open.return_value.__enter__.return_value.read.return_value = json.dumps(payload).encode()
                result = self.service.recognize(path, [0], True)
                request = opener.return_value.open.call_args.args[0]
                self.assertNotIn(str(path), request.data.decode())
                self.assertNotIn("fictional-ds-key", request.data.decode())
                self.assertTrue(result["requires_manual_review"])
                self.assertFalse(result["affects_automatic_rejection"])
                self.assertEqual([1], result["pages"])

    def test_multiple_simultaneous_ai_requests_blocked(self):
        self.service.request_lock.acquire()
        try:
            with self.assertRaises(ValueError): self.service.recognize("unused.pdf", [0], True)
        finally: self.service.request_lock.release()
