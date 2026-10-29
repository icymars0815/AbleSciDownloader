"""Regressions for release review: temporary data and mocked site actions only."""
import contextlib
import importlib.util
import io
from pathlib import Path
import queue
import tempfile
import threading
import unittest
import zipfile
from unittest.mock import Mock, patch

from accounts import Accounts
from browser_agent import BrowserAgent, PreconditionError
from core import Store
from literature_search import search_works
from pipeline import Pipeline


class ContinuationBoundaryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = Store(temp.name)
        self.accounts = Accounts(temp.name)
        self.agent = Mock(busy=False, commands=queue.Queue())
        self.pipeline = Pipeline(self.store, self.accounts, self.agent, start=False)
        self.store.update_preferences(oa_first=False, mcp_website_actions=True, mcp_actions_account="default")
        self.store.update_account("default", points=600)
        self.iid = self.store.add("Fictional", "10.1234/fictional", account_id="default",
                                  request_confirmed=True, request_mcp_auto_publish=True, request_reward=10)
        self.pipeline.publish_limiter = Mock()
        self.pipeline.publish_limiter.wait_seconds.return_value = 0

    def scoped_agent(self):
        agent = object.__new__(BrowserAgent)
        agent.store, agent.accounts, agent.stop = self.store, self.accounts, threading.Event()
        agent._mcp_scope = {"account_id": "default", "item_id": self.iid, "auto": True,
                            "generation": self.store.snapshot()["preferences"]["mcp_action_generation"]}
        return agent

    def test_pausing_keeps_confirmation_and_prevents_next_tick(self):
        self.pipeline.pause_mcp()
        item = self.store.get(self.iid)
        self.assertTrue(item["request_confirmed"])
        self.assertEqual(10, item["request_reward"])
        self.assertFalse(item["request_mcp_auto_publish"])
        self.assertIsNone(self.pipeline.tick_mcp_authorized())
        self.agent.enqueue.assert_not_called()

    def test_restart_does_not_resume_background_site_writes(self):
        other = self.store.add("Other", "10.1234/other", account_id="other", request_mcp_auto_publish=True)
        Pipeline(self.store, self.accounts, self.agent, start=False)
        self.assertFalse(self.store.get(self.iid)["request_mcp_auto_publish"])
        self.assertFalse(self.store.get(other)["request_mcp_auto_publish"])
        self.assertIsNone(self.pipeline.tick_mcp_authorized())

    def test_scope_checked_after_pacing_wait(self):
        agent = self.scoped_agent()
        @contextlib.contextmanager
        def slot(_):
            self.store.update_preferences(mcp_website_actions=False)
            yield
        agent.limiter = Mock(slot=slot)
        with self.assertRaises(PreconditionError):
            with agent.throttled():
                self.fail("Revoked authority must never reach a site action")

    def test_stopped_or_wrong_account_or_unconfirmed_request_refused(self):
        agent = self.scoped_agent()
        agent.assert_mcp_authority()
        agent.stop.set()
        with self.assertRaises(PreconditionError): agent.assert_mcp_authority()
        agent.stop.clear()
        agent._mcp_scope["account_id"] = "other"
        with self.assertRaises(PreconditionError): agent.assert_mcp_authority()
        agent._mcp_scope["account_id"] = "default"
        self.store.update(self.iid, request_confirmed=False)
        with self.assertRaises(PreconditionError): agent.assert_mcp_authority()

    def test_paused_request_refused_even_after_permission_reenabled(self):
        agent = self.scoped_agent()
        self.pipeline.pause_mcp()
        self.store.update_preferences(mcp_website_actions=True)
        with self.assertRaises(PreconditionError): agent.assert_mcp_authority()

    def test_scheduled_command_carries_bound_authority(self):
        self.assertEqual("publish", self.pipeline.tick_mcp_authorized()["action"])
        self.agent.enqueue.assert_called_once_with("publish", self.iid,
            {"mcp_scope": {"account_id": "default", "item_id": self.iid, "auto": True,
                           "generation": self.store.snapshot()["preferences"]["mcp_action_generation"]}})

    def test_direct_mcp_command_invalid_after_stop_and_reenable(self):
        agent = self.scoped_agent()
        agent._mcp_scope["auto"] = False
        self.pipeline.pause_mcp()
        self.store.update_preferences(mcp_website_actions=True)
        with self.assertRaises(PreconditionError): agent.assert_mcp_authority()

    def test_monitor_retry_retains_authority_and_cannot_bypass_pause(self):
        from datetime import datetime
        from monitor import Monitor
        monitor = object.__new__(Monitor)
        monitor.store, monitor.accounts, monitor.agent = self.store, self.accounts, self.agent
        monitor.login_required, monitor.inflight = False, False
        self.store.update(self.iid, status="retrying", source="website", owned=True, website_status="待确认",
                          assist_url="https://www.ablesci.com/assist/detail?id=F1",
                          download_mcp_scope=self.scoped_agent()._mcp_scope,
                          download_retry={"next_at": "2026-10-20T00:00:00+00:00"})
        now = datetime.now().timestamp()
        self.assertTrue(monitor.retry_downloads(now))
        scope = self.store.get(self.iid)["download_mcp_scope"]
        self.agent.enqueue.assert_called_once_with("download", self.iid, {"mcp_scope": scope})
        self.agent.enqueue.reset_mock()
        self.pipeline.pause_mcp()
        self.assertFalse(monitor.retry_downloads(now))
        self.agent.enqueue.assert_not_called()

    def test_download_handoff_retains_authority(self):
        agent = self.scoped_agent()
        self.store.update(self.iid, source="website", owned=True)
        agent.assert_account, agent.export_state, agent.worker = Mock(), Mock(return_value={}), Mock()
        agent.hand_over_download(self.iid)
        agent.worker.submit.assert_called_once_with(self.iid, {}, authority={"mcp_scope": agent._mcp_scope})

    def test_stop_api_pauses_mcp_and_global_pipeline(self):
        from test_harness import start, stop
        from kytdl_client import Client
        with tempfile.TemporaryDirectory() as folder:
            server = start(folder, permission=True)
            try:
                iid = server.store.add("Fictional", "10.1234/stop", account_id="default",
                                       request_confirmed=True, request_mcp_auto_publish=True)
                server.store.update_preferences(pipeline_enabled=True)
                client = Client("http://127.0.0.1:%d" % server.server_port)
                client.post("/api/stop", {})
                self.assertFalse(server.pipeline.enabled())
                self.assertTrue(server.agent.stop.is_set())
                self.assertFalse(server.store.get(iid)["request_mcp_auto_publish"])
                self.assertTrue(server.store.get(iid)["request_confirmed"])
            finally:
                stop(server)

    def test_confirmed_published_request_can_resume_after_pause(self):
        from mcp_api import McpApi
        self.store.update(self.iid, source="website", owned=True,
                          assist_url="https://www.ablesci.com/assist/detail?id=F1", website_status="求助中")
        self.pipeline.pause_mcp()
        api = McpApi(self.store, self.accounts, self.pipeline, None, lambda: True)
        result = api.do_auto_continue({"id": self.iid})
        self.assertEqual("submitted", result["stage"])
        self.assertTrue(self.store.get(self.iid)["request_mcp_auto_publish"])
        self.store.update(self.iid, publish_action={"status": "uncertain"})
        with self.assertRaises(ValueError): api.do_auto_continue({"id": self.iid})


class SearchBoundaryTests(unittest.TestCase):
    def test_multibyte_query_rejected_before_network_if_encoded_url_too_long(self):
        with patch("literature_search.get_json") as get:
            with self.assertRaisesRegex(ValueError, "长度上限"):
                search_works("科研" * 250, api_key="")
            get.assert_not_called()

    def test_malformed_nested_public_metadata_does_not_crash(self):
        data = {"meta": [], "results": [{"doi": 123, "primary_location": "bad",
                "open_access": [], "authorships": [{"author": "bad"}]}]}
        with patch("literature_search.get_json", return_value=data):
            result = search_works("test", api_key="")
        self.assertEqual("", result["records"][0]["doi"])
        self.assertEqual("", result["records"][0]["journal"])


class PackagingBoundaryTests(unittest.TestCase):
    def test_source_snapshot_reads_committed_bytes(self):
        path = Path(__file__).resolve().parents[1] / "packaging/build_portable.py"
        spec = importlib.util.spec_from_file_location("release_build", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, "w") as archive:
            archive.writestr("app.py", b"committed public source")
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(module, "source_files", return_value=[Path("app.py")]), \
                patch.object(module.subprocess, "check_output", return_value=payload.getvalue()):
            module.copy_sources("public-commit", Path(folder))
            self.assertEqual(b"committed public source", (Path(folder) / "app.py").read_bytes())
