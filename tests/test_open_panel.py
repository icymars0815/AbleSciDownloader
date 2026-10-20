"""Open-panel feature (MCP tool, CLI command, endpoint) and portable MCP setup files. The opener is always mocked: no browser starts."""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import app
import kytdl
from kytdl_client import ApiError, Client, panel_url
from tests.test_harness import ROOT, add_batch, call, connect, start, stop


class PanelUrlTests(unittest.TestCase):
    def test_plain_and_item_urls(self):
        self.assertEqual("http://127.0.0.1:8769/", panel_url("http://127.0.0.1:8769"))
        self.assertEqual("http://127.0.0.1:8769/#item=a%20b", panel_url("http://127.0.0.1:8769/", "a b"))


class OpenPanelEndpointTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.server = start(self.folder.name)
        self.server.open_panel = MagicMock()
        self.client = Client("http://127.0.0.1:%d" % self.server.server_port)
        self.ids = add_batch(self.server, 1)

    def tearDown(self):
        stop(self.server)
        self.folder.cleanup()

    def test_opens_main_window(self):
        reply = self.client.post("/api/open-panel", {})
        self.assertEqual(self.client.base + "/", reply["url"])
        self.server.open_panel.assert_called_once_with(self.client.base + "/")

    def test_opens_item_and_rejects_unknown_item(self):
        reply = self.client.post("/api/open-panel", {"id": self.ids[0]})
        self.assertEqual(self.client.panel_url(self.ids[0]), reply["url"])
        self.server.open_panel.assert_called_once_with(reply["url"])
        self.server.open_panel.reset_mock()
        with self.assertRaises(ApiError):
            self.client.post("/api/open-panel", {"id": "nope"})
        self.server.open_panel.assert_not_called()

    def test_requires_token(self):
        request = Request(self.client.base + "/api/open-panel", b"{}", {"Content-Type": "application/json", "Origin": self.client.base})
        with self.assertRaises(HTTPError) as caught:
            urlopen(request, timeout=5)
        self.assertEqual(403, caught.exception.code)
        self.server.open_panel.assert_not_called()

    def test_default_opener_uses_launcher_then_falls_back(self):
        with patch("launcher.open_window") as window:
            app.open_panel_window("http://127.0.0.1:8769/#item=x")
        window.assert_called_once_with("http://127.0.0.1:8769/#item=x")
        with patch("launcher.open_window", side_effect=OSError), patch("webbrowser.open") as browser:
            app.open_panel_window("http://127.0.0.1:8769/")
        browser.assert_called_once_with("http://127.0.0.1:8769/")


class OpenPanelMcpTests(unittest.IsolatedAsyncioTestCase):
    async def test_tool_and_panel_urls_in_status_outputs(self):
        with tempfile.TemporaryDirectory() as folder:
            server = start(folder)
            server.open_panel = MagicMock()
            ids = add_batch(server, 1)
            base = "http://127.0.0.1:%d" % server.server_port
            try:
                async with connect(server) as session:
                    tool = next(t for t in (await session.list_tools()).tools if t.name == "open_panel")
                    self.assertTrue(tool.annotations.readOnlyHint)
                    error, status = await call(session, "workbench_status")
                    self.assertEqual(base + "/", status["panel_url"])
                    error, opened = await call(session, "open_panel", {"paper_id": ids[0]})
                    self.assertFalse(error)
                    self.assertTrue(opened["ok"])
                    self.assertEqual(base + "/#item=" + ids[0], opened["panel_url"])
                    server.open_panel.assert_called_once_with(opened["panel_url"])
                    error, missing = await call(session, "open_panel", {"paper_id": "nope"})
                    self.assertEqual("not_found", missing["reason"])
                    error, progress = await call(session, "download_progress", {"paper_id": ids[0]})
                    self.assertEqual(base + "/#item=" + ids[0], progress["panel_url"])
                    error, batch = await call(session, "batch_status", {"batch_id": "B1"})
                    self.assertEqual(base + "/", batch["panel_url"])
                    error, batches = await call(session, "batch_status")
                    self.assertEqual(base + "/", batches["panel_url"])
            finally:
                stop(server)


class OpenPanelCliTests(unittest.TestCase):
    def run_cli(self, client, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = kytdl.main(list(argv), client=client)
        return code, out.getvalue()

    def test_open_command(self):
        with tempfile.TemporaryDirectory() as folder:
            server = start(folder)
            server.open_panel = MagicMock()
            ids = add_batch(server, 1)
            client = Client("http://127.0.0.1:%d" % server.server_port)
            try:
                code, text = self.run_cli(client, "open", "--json")
                self.assertEqual(0, code)
                self.assertEqual(client.base + "/", json.loads(text)["panel_url"])
                code, text = self.run_cli(client, "open", ids[0], "--json")
                self.assertEqual(client.base + "/#item=" + ids[0], json.loads(text)["panel_url"])
                server.open_panel.assert_called_with(client.base + "/#item=" + ids[0])
                code, _ = self.run_cli(client, "open", "nope", "--json")
                self.assertEqual(1, code)
                code, text = self.run_cli(client, "status", "--json")
                self.assertEqual(client.base + "/", json.loads(text)["panel_url"])
            finally:
                stop(server)


class PortableSetupTests(unittest.TestCase):
    def test_configs_have_no_absolute_paths(self):
        mcp_json = (ROOT / ".mcp.json").read_text("utf-8")
        codex = (ROOT / ".codex" / "config.toml").read_text("utf-8")
        for text in (mcp_json, codex):
            self.assertIn("kytdl-mcp.cmd", text)
            self.assertNotIn(":\\", text)
            self.assertNotIn("D:", text)
        self.assertEqual(["/c", "kytdl-mcp.cmd"], json.loads(mcp_json)["mcpServers"]["kytdl"]["args"])

    def test_launcher_locates_files_next_to_itself(self):
        text = (ROOT / "kytdl-mcp.cmd").read_text("utf-8")
        self.assertIn("%~dp0", text)
        self.assertIn(".venv\\Scripts\\python.exe", text)
        self.assertIn("mcp_server.py", text)

    def test_cli_runs_from_another_directory(self):
        with tempfile.TemporaryDirectory() as elsewhere:
            result = subprocess.run([sys.executable, str(ROOT / "kytdl.py"), "status", "--json"], cwd=elsewhere, capture_output=True, text=True,
                                    env={**os.environ, "ABLESCI_LOCAL_URL": "http://127.0.0.1:1"})
        self.assertEqual(2, result.returncode)
        self.assertEqual("service_not_running", json.loads(result.stdout)["reason"])

    def test_mcp_server_starts_from_another_directory(self):
        with tempfile.TemporaryDirectory() as elsewhere:
            result = subprocess.run([sys.executable, str(ROOT / "mcp_server.py")], cwd=elsewhere, input="", capture_output=True, text=True, timeout=60)
        self.assertEqual(0, result.returncode, result.stderr)


if __name__ == "__main__":
    unittest.main()
