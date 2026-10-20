import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from app import make_server
from core import seed_demo


class McpTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_stdio_handshake_and_local_tools(self):
        with tempfile.TemporaryDirectory() as folder:
            server = make_server(0, folder)
            seed_demo(server.store)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                params = StdioServerParameters(command=sys.executable,
                    args=[str(Path(__file__).resolve().parents[1] / "mcp_server.py")],
                    env={"ABLESCI_LOCAL_URL": f"http://127.0.0.1:{server.server_port}", "PYTHONIOENCODING": "utf-8"})
                async with stdio_client(params) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        tools = await session.list_tools()
                        self.assertIn("verify_paper", [t.name for t in tools.tools])
                        parsed = await session.call_tool("preview_references", {"text": "10.1234/fixture"})
                        self.assertFalse(parsed.isError)
                        status = await session.call_tool("workbench_status", {})
                        self.assertFalse(status.isError)
                        papers = await session.call_tool("list_papers", {})
                        self.assertFalse(papers.isError)
                        self.assertNotIn("excerpt", str(papers))
                        self.assertNotIn(folder, str(papers))
                        denied = await session.call_tool("website_action", {"action": "signin"})
                        self.assertTrue(denied.isError)
                        self.assertTrue(server.agent.commands.empty())
                        imported = await session.call_tool("import_references", {"text": "10.1234/mcp-fixture\tFictional MCP test record"})
                        self.assertFalse(imported.isError)
                        item = next(i for i in server.store.snapshot()["items"] if i.get("demo_name") == "matching")
                        checked = await session.call_tool("verify_paper", {"paper_id": item["id"]})
                        self.assertFalse(checked.isError)
            finally:
                server.shutdown()
                server.server_close()
                server.agent.commands.put(("close", None, None))
                server.agent.thread.join(timeout=2)
                thread.join(timeout=2)
