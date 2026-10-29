"""Harness integration: MCP tools over a real stdio session, the /api/mcp/* endpoints, and the kytdl CLI. Temp data roots only."""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import MagicMock, patch

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

import intake
import kytdl
from app import make_server
from core import now, seed_demo
from kytdl_client import Client

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_TOOLS = {
    "workbench_status", "list_accounts", "switch_account", "list_papers", "preview_references", "import_references", "query_public_metadata", "search_literature",
    "verify_paper", "website_action", "reject_paper", "stop_website_task", "unparsed_segments", "batch_status", "review_table", "confirm_batch",
    "download_paper", "download_progress", "wait_for", "get_result", "render_pages", "submit_assist_result", "error_report", "monitor_status",
    "events", "download_settings_get", "oa_lookup", "oa_fetch", "open_panel", "continue_authorized_request", "resolve_oa_review"}


def start(folder, permission=False, session=True):
    server = make_server(0, folder, start_monitor=False, auto_screen=False)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    if permission:
        server.store.update_preferences(mcp_website_actions=True, mcp_actions_account="default")
    if session:
        server.store.update_account("default", points=500)
    return server


def stop(server):
    server.shutdown()
    server.server_close()
    server.agent.commands.put(("close", None, None))
    server.agent.thread.join(timeout=2)


def add_batch(server, count=3, batch="B1"):
    store = server.store
    ids = [store.add("Fictional title %s %d" % (batch, n), "10.1234/b%s%d" % (batch.lower(), n), account_id="default", request_confirmed=False, batch_id=batch) for n in range(count)]
    intake.create_batch(store, batch, "default", ids)
    for item_id in ids:
        store.update(item_id, oa_check=dict(status="not_oa"))
    with store.lock:
        intake.find_batch(store, batch)["screening_done_at"] = now()
        store.save()
    return ids


@contextlib.asynccontextmanager
async def connect(server):
    params = StdioServerParameters(command=sys.executable, args=[str(ROOT / "mcp_server.py")],
                                   env={"ABLESCI_LOCAL_URL": "http://127.0.0.1:%d" % server.server_port, "PYTHONIOENCODING": "utf-8"})
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


async def call(session, name, args=None):
    result = await session.call_tool(name, args or {})
    text = result.content[0].text if result.content else ""
    if result.isError:
        return True, text
    try:
        return False, json.loads(text)
    except ValueError:
        return False, text


class HarnessMcpTests(unittest.IsolatedAsyncioTestCase):
    async def test_mcp_can_resolve_oa_review_without_publishing(self):
        with tempfile.TemporaryDirectory() as folder:
            server = start(folder)
            try:
                ids = []
                for n in (1, 2):
                    iid = server.store.add("Paper %d" % n, "10.1234/oa%d" % n, account_id="default")
                    file = Path(folder) / ("paper%d.pdf" % n)
                    file.write_bytes(b"%PDF-1.4 fixture")
                    server.store.update(iid, status="oa_review", pdf=file.name, verification={"status": "review"}, oa_check={"status": "downloaded"})
                    ids.append(iid)
                async with connect(server) as session:
                    error, _ = await call(session, "resolve_oa_review", {"paper_id": ids[0], "decision": "keep"})
                    self.assertTrue(error)
                    self.assertEqual("oa_review", __import__("pipeline").stage_of(server.store.get(ids[0])))
                    error, _ = await call(session, "resolve_oa_review", {"paper_id": ids[0], "decision": "keep", "confirmed": True})
                    self.assertFalse(error)
                    self.assertEqual("done", __import__("pipeline").stage_of(server.store.get(ids[0])))
                    error, _ = await call(session, "resolve_oa_review", {"paper_id": ids[1], "decision": "site_fallback", "confirmed": True})
                    self.assertFalse(error)
                    second = server.store.get(ids[1])
                    self.assertEqual("queued", __import__("pipeline").stage_of(second))
                    self.assertTrue((Path(folder) / "paper2.pdf").exists())
                    self.assertFalse(second.get("request_confirmed"))
            finally:
                stop(server)

    async def test_search_import_oa_and_confirmed_site_fallback_end_to_end(self):
        with tempfile.TemporaryDirectory() as folder:
            server = start(folder, permission=True)
            try:
                found = {"records": [{"title": "Open paper", "doi": "10.1234/open", "authors": ["A"]},
                                     {"title": "Closed paper", "doi": "10.1234/closed", "authors": ["B"]}], "total": 2}
                def fetch(doi, dest, *args, **kwargs):
                    if doi.endswith("/open"):
                        Path(dest).write_bytes(b"%PDF-1.4\nfixture")
                        return {"status": "downloaded", "attempts": [], "source": "fixture"}
                    return {"status": "not_oa", "attempts": []}
                with patch("literature_search.search_works", return_value=found), patch("oa_fetch.fetch_open_access", side_effect=fetch), \
                     patch("pipeline.verify_with_metadata", return_value={"status": "identity_match", "sha256": "fixture"}), \
                     patch.object(server.agent, "enqueue") as enqueue:
                    async with connect(server) as session:
                        error, search = await call(session, "search_literature", {"query": "probiotic"})
                        self.assertFalse(error)
                        error, imported = await call(session, "import_references", {"records": search["records"]})
                        self.assertFalse(error)
                        bid = imported["batch_id"]
                        self.assertEqual("mcp", intake.find_batch(server.store, bid)["origin"])
                        for _ in range(2):
                            iid = intake.next_screening(server.store)
                            self.assertIsNotNone(iid)
                            server.pipeline.oa_run(iid)
                        self.assertIsNone(intake.next_screening(server.store))
                        error, review = await call(session, "review_table", {"batch_id": bid})
                        self.assertFalse(error)
                        rows = {r["doi"]: r for r in review["rows"]}
                        self.assertEqual("oa_done", rows["10.1234/open"]["code"])
                        self.assertEqual("not_oa", rows["10.1234/closed"]["code"])
                        closed = rows["10.1234/closed"]["id"]
                        error, preview = await call(session, "confirm_batch", {"batch_id": bid, "item_ids": [closed]})
                        self.assertFalse(error)
                        error, result = await call(session, "confirm_batch", {"batch_id": bid, "confirm_token": preview["confirm_token"], "confirmed": True})
                        self.assertFalse(error)
                        self.assertEqual(1, result["confirmed"])
                        planned = server.pipeline.tick_mcp_authorized()
                        self.assertEqual(("publish", closed), (planned["action"], planned["item_id"]))
                        enqueue.assert_called_once_with("publish", closed)
            finally:
                stop(server)

    async def test_openalex_key_setting_is_private_and_used_for_search(self):
        with tempfile.TemporaryDirectory() as folder:
            server = start(folder)
            try:
                client = Client("http://127.0.0.1:%d" % server.server_port)
                with patch("credential_vault.protect", side_effect=lambda data, decrypt=False: data[::-1]), patch("literature_search.search_works", return_value={"records": []}) as search:
                    client.post("/api/openalex-key", {"key": "test-secret"})
                    state = client.state()
                    self.assertTrue(state["openalex_key_saved"])
                    self.assertNotIn("test-secret", json.dumps(state))
                    client.post("/api/literature-search", {"query": "fiber"})
                    self.assertEqual("test-secret", search.call_args.args[-1])
                    client.post("/api/openalex-key-clear", {})
                    self.assertFalse(client.state()["openalex_key_saved"])
            finally:
                stop(server)

    async def test_search_literature_through_mcp_without_import(self):
        with tempfile.TemporaryDirectory() as folder:
            server = start(folder)
            result = {"records": [{"title": "Test", "doi": "10.1234/test", "authors": []}], "next_cursor": None, "total": 1}
            try:
                with patch("literature_search.search_works", return_value=result) as search:
                    async with connect(server) as session:
                        error, payload = await call(session, "search_literature", {"query": "fiber", "page_size": 5})
                    self.assertFalse(error)
                    self.assertEqual("10.1234/test", payload["records"][0]["doi"])
                    self.assertEqual(5, search.call_args.args[-3])
                    self.assertEqual([], server.store.snapshot()["items"])
            finally:
                stop(server)

    async def test_tool_list_has_no_forbidden_tools(self):
        with tempfile.TemporaryDirectory() as folder:
            server = start(folder)
            try:
                async with connect(server) as session:
                    names = {t.name for t in (await session.list_tools()).tools}
                    self.assertEqual(EXPECTED_TOOLS, names)
                    for name in names:
                        for banned in ("highspeed", "adopt", "publish", "permission", "credential", "cookie", "api_key", "ai_"):
                            self.assertNotIn(banned, name)
                    described = {t.name: t.description for t in (await session.list_tools()).tools}
                    self.assertIn("model provider", described["get_result"])
                    self.assertIn("permission", described["confirm_batch"])
                    self.assertIn("permission", described["download_paper"])
            finally:
                stop(server)

    async def test_permission_refusals(self):
        with tempfile.TemporaryDirectory() as folder:
            server = start(folder, permission=False)
            ids = add_batch(server)
            server.store.update(ids[0], source="website", owned=True, assist_url="https://example.invalid/x", website_status="待确认")
            try:
                async with connect(server) as session:
                    error, text = await call(session, "download_paper", {"paper_id": ids[0]})
                    self.assertTrue(error)
                    self.assertIn("permission_denied", text)
                    error, preview = await call(session, "confirm_batch", {"batch_id": "B1"})
                    self.assertFalse(error)   # preview changes nothing and needs no permission
                    self.assertFalse(preview["permission"])
                    error, text = await call(session, "confirm_batch", {"batch_id": "B1", "confirm_token": preview["confirm_token"], "confirmed": True})
                    self.assertTrue(error)
                    self.assertIn("permission_denied", text)
                    self.assertFalse(any(i.get("request_confirmed") for i in server.store.snapshot()["items"]))
                    error, _ = await call(session, "website_action", {"action": "download", "paper_id": ids[0]})
                    self.assertTrue(error)
                    self.assertTrue(server.agent.commands.empty())
            finally:
                stop(server)

    async def test_download_paper_with_permission_queues_without_touching_highspeed(self):
        with tempfile.TemporaryDirectory() as folder:
            server = start(folder, permission=True)
            ids = add_batch(server)
            server.store.update(ids[0], source="website", owned=True, assist_url="https://example.invalid/x", website_status="待确认")
            before = server.store.snapshot()["preferences"].get("download_highspeed")
            try:
                async with connect(server) as session:
                    error, not_ready = await call(session, "download_paper", {"paper_id": ids[1]})
                    self.assertFalse(error)
                    self.assertEqual("not_ready", not_ready["reason"])
                    server.agent.enqueue = MagicMock()
                    error, queued = await call(session, "download_paper", {"paper_id": ids[0]})
                    self.assertFalse(error)
                    self.assertTrue(queued["queued"])
                    server.agent.enqueue.assert_called_once()
                    self.assertEqual("download", server.agent.enqueue.call_args[0][0])
                    self.assertEqual(before, server.store.snapshot()["preferences"].get("download_highspeed"))
                    error, progress = await call(session, "download_progress", {"paper_id": ids[0]})
                    self.assertEqual("fulfilled", progress["stage"])
            finally:
                stop(server)

    async def test_confirm_token_flow(self):
        with tempfile.TemporaryDirectory() as folder:
            server = start(folder, permission=True)
            ids = add_batch(server, 3, "B1")
            other = add_batch(server, 2, "B2")
            try:
                async with connect(server) as session:
                    error, preview = await call(session, "confirm_batch", {"batch_id": "B1", "item_ids": ids[:2], "rewards": {ids[0]: 30}})
                    self.assertFalse(error)
                    self.assertTrue(preview["permission"])
                    self.assertEqual(30 + 10, preview["estimated_points"])
                    self.assertEqual({ids[0], ids[1]}, {r["id"] for r in preview["items"]})
                    self.assertFalse(any(i.get("request_confirmed") for i in server.store.snapshot()["items"]), "preview must not change anything")
                    token = preview["confirm_token"]
                    # confirmed flag missing / wrong token / token for another batch / different items are all refused
                    error, _ = await call(session, "confirm_batch", {"batch_id": "B1", "confirm_token": token})
                    self.assertTrue(error)
                    error, _ = await call(session, "confirm_batch", {"batch_id": "B1", "confirm_token": "forged-token", "confirmed": True})
                    self.assertTrue(error)
                    error, _ = await call(session, "confirm_batch", {"batch_id": "B2", "confirm_token": token, "confirmed": True})
                    self.assertTrue(error)   # burns the token: it was presented for the wrong batch
                    error, preview2 = await call(session, "confirm_batch", {"batch_id": "B1", "item_ids": ids[:2], "rewards": {ids[0]: 30}})
                    error, text = await call(session, "confirm_batch", {"batch_id": "B1", "confirm_token": preview2["confirm_token"], "confirmed": True, "item_ids": ids})
                    self.assertTrue(error)
                    self.assertFalse(any(i.get("request_confirmed") for i in server.store.snapshot()["items"]))
                    # expired
                    error, preview3 = await call(session, "confirm_batch", {"batch_id": "B1", "item_ids": ids[:2], "rewards": {ids[0]: 30}})
                    entries = server.mcp_api.tokens.items
                    binding, _ = entries[preview3["confirm_token"]]
                    entries[preview3["confirm_token"]] = (binding, time.time() - 1)
                    error, text = await call(session, "confirm_batch", {"batch_id": "B1", "confirm_token": preview3["confirm_token"], "confirmed": True})
                    self.assertTrue(error)
                    self.assertIn("过期", text)
                    # good path, then reuse refused
                    error, preview4 = await call(session, "confirm_batch", {"batch_id": "B1", "item_ids": ids[:2], "rewards": {ids[0]: 30}})
                    error, done = await call(session, "confirm_batch", {"batch_id": "B1", "confirm_token": preview4["confirm_token"], "confirmed": True})
                    self.assertFalse(error, done)
                    self.assertEqual(2, done["confirmed"])
                    items = {i["id"]: i for i in server.store.snapshot()["items"]}
                    self.assertTrue(all(items[i].get("request_mcp_auto_publish") for i in ids[:2]))
                    self.assertFalse(items[ids[2]].get("request_mcp_auto_publish", False))
                    self.assertTrue(items[ids[0]]["request_confirmed"] and items[ids[1]]["request_confirmed"])
                    self.assertFalse(items[ids[2]].get("request_confirmed"))
                    self.assertEqual(30, items[ids[0]]["request_reward"])
                    self.assertFalse(any(items[i].get("request_confirmed") for i in other))
                    error, _ = await call(session, "confirm_batch", {"batch_id": "B1", "confirm_token": preview4["confirm_token"], "confirmed": True})
                    self.assertTrue(error)
            finally:
                stop(server)

    async def test_truncation_and_paging(self):
        with tempfile.TemporaryDirectory() as folder:
            server = start(folder)
            add_batch(server, 30)
            try:
                async with connect(server) as session:
                    error, page = await call(session, "list_papers", {"max_chars": 900})
                    self.assertFalse(error)
                    self.assertLess(len(page["papers"]), 30)
                    self.assertTrue(page["truncation"]["truncated"])
                    self.assertIn("offset=", page["deferred"])
                    nxt = page["truncation"]["offset"] + page["truncation"]["shown"]
                    error, page2 = await call(session, "list_papers", {"max_chars": 900, "offset": nxt})
                    self.assertNotEqual(page["papers"][0]["id"], page2["papers"][0]["id"])
                    error, table = await call(session, "review_table", {"batch_id": "B1", "max_chars": 1200})
                    self.assertTrue(table["truncation"]["truncated"])
                    self.assertEqual(30, table["truncation"]["total"])
                    self.assertEqual(30 * 10, table["estimated_points_if_confirmed"])
                    error, full = await call(session, "review_table", {"batch_id": "B1", "limit": 5, "max_chars": 40000})
                    self.assertEqual(5, len(full["rows"]))
                    error, missing = await call(session, "review_table", {"batch_id": "nope"})
                    self.assertEqual("batch_not_found", missing["reason"])
                    self.assertIn("tried", missing)
            finally:
                stop(server)

    async def test_render_pages_confinement(self):
        with tempfile.TemporaryDirectory() as folder, tempfile.TemporaryDirectory() as outside:
            server = start(folder)
            seed_demo(server.store)
            demo = next(i for i in server.store.snapshot()["items"] if i.get("demo_name") == "matching")
            escape = Path(outside) / "escape.pdf"
            escape.write_bytes((Path(folder) / demo["pdf"]).read_bytes())
            plain = server.store.add("No pdf", "10.1234/nopdf", account_id="default")
            traversal = server.store.add("Traversal", "10.1234/trav", account_id="default", pdf=os.path.relpath(escape, folder))
            try:
                async with connect(server) as session:
                    error, ok = await call(session, "render_pages", {"paper_id": demo["id"], "pages": [1]})
                    self.assertFalse(error)
                    path = Path(ok["pages"][0]["path"])
                    self.assertTrue(path.is_file())
                    self.assertTrue(path.resolve().is_relative_to(Path(folder).resolve()))
                    self.assertTrue(path.read_bytes().startswith(b"\x89PNG"))
                    for args in ({"paper_id": demo["id"], "pages": [4]}, {"paper_id": demo["id"], "pages": [0]}, {"paper_id": demo["id"], "pages": [1, 2, 3, 4]},
                                 {"paper_id": plain}, {"paper_id": traversal}, {"paper_id": "unknown"}):
                        error, refused = await call(session, "render_pages", args)
                        self.assertFalse(error)
                        self.assertEqual("render_refused", refused["reason"], args)
                    self.assertFalse((Path(folder) / "renders" / traversal).exists())
            finally:
                stop(server)

    async def test_submit_assist_result_changes_nothing_else(self):
        with tempfile.TemporaryDirectory() as folder:
            server = start(folder)
            seed_demo(server.store)
            demo = next(i for i in server.store.snapshot()["items"] if i.get("demo_name") == "wrong")
            from core import verify_pdf
            server.store.update(demo["id"], verification=verify_pdf(Path(folder) / demo["pdf"], demo["expected_title"], demo["expected_doi"]), rejection=dict(action="ask_user"))
            before = server.store.get(demo["id"])
            try:
                async with connect(server) as session:
                    error, stored = await call(session, "submit_assist_result", {"paper_id": demo["id"], "fields": {"title": demo["expected_title"], "doi": demo["expected_doi"], "identity_match": True}, "evidence": "I read page 1."})
                    self.assertFalse(error, stored)
                    self.assertTrue(stored["stored"])
                    after = server.store.get(demo["id"])
                    for key in ("verification", "rejection", "status", "pdf", "reject_action", "website_status"):
                        self.assertEqual(before.get(key), after.get(key), key)
                    self.assertEqual("external_harness", after["external_assist"][0]["source"])
                    self.assertEqual("unverified_assist", after["external_assist"][0]["status"])
                    error, result = await call(session, "get_result", {"paper_id": demo["id"]})
                    self.assertEqual(before["verification"]["status"], result["verification"]["status"])
                    self.assertEqual(1, len(result["external_assist"]))
                    self.assertNotIn("excerpt", json.dumps(result))
                    for bad in ({"fields": {}}, {"fields": {"x": {"nested": 1}}}, {"fields": {"a": "b"}, "evidence": "e" * 5000}):
                        error, _ = await call(session, "submit_assist_result", dict(bad, paper_id=demo["id"]))
                        self.assertTrue(error)
            finally:
                stop(server)

    async def test_import_unparsed_and_wait_for(self):
        with tempfile.TemporaryDirectory() as folder:
            server = start(folder)
            try:
                async with connect(server) as session:
                    error, imported = await call(session, "import_references", {"records": [{"title": "Fictional harness paper", "doi": "10.1234/harness.1", "authors": ["A One"]},
                                                                                           {"title": "Second", "doi": "10.1234/harness.2"}]})
                    self.assertFalse(error, imported)
                    self.assertEqual(2, imported["count"])
                    batch_id = imported["batch_id"]
                    error, again = await call(session, "import_references", {"records": [{"title": "Fictional harness paper", "doi": "10.1234/harness.1"}]})
                    self.assertFalse(again["ok"])
                    self.assertEqual("nothing_new", again["reason"])
                    error, status = await call(session, "batch_status", {"batch_id": batch_id})
                    self.assertEqual("screening", status["phase"])
                    error, listing = await call(session, "batch_status")
                    self.assertEqual(batch_id, listing["batches"][0]["id"])
                    error, parsed = await call(session, "unparsed_segments", {"text": "10.1234/ok.1\tGood record here\n\n!!! ???\n\nA title without any identifier at all"})
                    reasons = {s["reason"] for s in parsed["segments"]}
                    self.assertTrue(reasons & {"no_title_no_doi", "partial"})
                    error, reached = await call(session, "wait_for", {"target_id": batch_id, "until": "screening", "timeout_seconds": 5})
                    self.assertTrue(reached["reached"])
                    error, gone = await call(session, "wait_for", {"target_id": "nothing", "until": "done"})
                    self.assertEqual("not_found", gone["reason"])
                    threading.Timer(0.5, lambda: server.store.event("harness test event")).start()
                    error, timed = await call(session, "wait_for", {"target_id": batch_id, "until": "done", "timeout_seconds": 3})
                    self.assertFalse(timed["reached"])
                    self.assertEqual("timeout", timed["reason"])
                    self.assertIn("harness test event", [e["message"] for e in timed["events"]])
                    # oversize timeout is capped, bad until refused
                    error, _ = await call(session, "wait_for", {"target_id": batch_id, "until": " , "})
                    self.assertTrue(error)
            finally:
                stop(server)

    async def test_read_tools_and_error_report_redaction(self):
        with tempfile.TemporaryDirectory() as folder:
            server = start(folder)
            seed_demo(server.store)
            server.store.event("下载失败 C:\\Users\\someone\\secret\\file.pdf 联系 person@example.com 见 https://example.org/a?token=abcdef12345 key=" + "A" * 40)
            attention = server.store.add("Stuck", "10.1234/stuck", account_id="default", status="attention", note="失败：" + folder)
            try:
                async with connect(server) as session:
                    error, report = await call(session, "error_report")
                    text = json.dumps(report, ensure_ascii=False)
                    for leaked in ("someone", "person@example.com", "abcdef12345", "A" * 30, folder):
                        self.assertNotIn(leaked, text)
                    self.assertTrue(report["redacted"])
                    self.assertTrue(report["recent_problem_events"])
                    self.assertTrue(any(p["id"] == attention for p in report["problem_items"]))
                    error, monitor = await call(session, "monitor_status")
                    self.assertIn("enabled", monitor)
                    error, evs = await call(session, "events", {"limit": 3})
                    self.assertLessEqual(len(evs["events"]), 3)
                    error, settings = await call(session, "download_settings_get")
                    self.assertIn(settings["highspeed"]["mode"], ("off", "smart", "force"))
                    self.assertNotIn("api_key", json.dumps(settings))
            finally:
                stop(server)

    async def test_oa_tools_use_public_lookup_only(self):
        with tempfile.TemporaryDirectory() as folder:
            server = start(folder)
            try:
                async with connect(server) as session:
                    found = dict(status="found", oa_status="gold", is_oa=True, license="cc-by", version="publishedVersion", landing_url=None, cached=False,
                                 links=[dict(url="https://example.org/paper.pdf", source="Unpaywall", version="publishedVersion", license="cc-by")], attempts=["unpaywall: ok"])
                    with patch("oa_fetch.lookup_only", return_value=found):
                        error, hit = await call(session, "oa_lookup", {"doi": "10.1234/oa.1"})
                    self.assertTrue(hit["ok"])
                    self.assertEqual("https://example.org/paper.pdf", hit["links"][0]["url"])
                    with patch("oa_fetch.lookup_only", return_value=dict(found, status="not_oa", links=[], attempts=["unpaywall: closed", "openalex: closed"])):
                        error, miss = await call(session, "oa_lookup", {"doi": "10.1234/oa.2"})
                    self.assertEqual("not_oa", miss["reason"])
                    self.assertEqual(2, len(miss["tried"]))
                    error, bad = await call(session, "oa_lookup", {"doi": "not a doi"})
                    self.assertEqual("bad_request", bad["reason"])
                    server.pipeline.oa_start = MagicMock()
                    error, started = await call(session, "oa_fetch", {"doi": "10.1234/oa.3"})
                    self.assertTrue(started["ok"], started)
                    server.pipeline.oa_start.assert_called_once_with(started["id"])
                    error, same = await call(session, "oa_fetch", {"doi": "10.1234/oa.3"})
                    self.assertTrue(same["ok"])   # still queued, no pdf: allowed to retry
            finally:
                stop(server)


class WarningTests(unittest.TestCase):
    def test_structured_import_rejects_invalid_batch_without_partial_changes(self):
        with tempfile.TemporaryDirectory() as folder:
            server = start(folder)
            try:
                client = Client("http://127.0.0.1:%d" % server.server_port)
                from kytdl_client import ApiError
                for bad in ({"title": None, "doi": None}, {"title": "x", "authors": "not a list"}, "not an object"):
                    with self.assertRaises(ApiError):
                        client.post("/api/import", {"records": [{"title": "valid"}, bad]})
                    self.assertEqual([], server.store.snapshot()["items"])
            finally:
                stop(server)
    def test_resume_pre_submit_only(self):
        with tempfile.TemporaryDirectory() as folder:
            server = start(folder, permission=True)
            try:
                iid = server.store.add("Test", "10.1234/test", account_id="default")
                server.store.update(iid, request_confirmed=True, status="attention")
                result = server.mcp_api.do_auto_continue({"id": iid, "retry_unsubmitted": True})
                self.assertTrue(result["queued"])
                server.store.update(iid, status="attention", publish_action={"status": "uncertain"})
                with self.assertRaises(ValueError):
                    server.mcp_api.do_auto_continue({"id": iid, "retry_unsubmitted": True})
            finally:
                stop(server)
    def test_importing_mcp_server_emits_no_pydantic_warning(self):
        done = subprocess.run([sys.executable, "-W", "error", "-c", "import mcp_server"], cwd=ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(0, done.returncode, done.stderr[-500:])
        self.assertNotIn("IncompleteFieldDefinitionWarning", done.stderr)


# ---------- CLI ----------
class FakeService(BaseHTTPRequestHandler):
    state = {"items": [{"id": "p1", "stage": "fulfilled", "pdf": ""}], "events": [], "batches": [], "monitor": {"enabled": False}, "about": {"version": "x"},
             "preferences": {}, "pipeline": {}, "active_account_id": "default"}
    posts = []

    def log_message(self, *_):
        pass

    def send(self, code, body, kind="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/":
            return self.send(200, b'<script>const TOKEN="fake-token";let state={}</script>', "text/html")
        if self.path == "/api/state":
            return self.send(200, self.state)
        self.send(404, {"error": "nope"})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeService.posts.append((self.path, body, self.headers.get("X-Demo-Token")))
        if self.path == "/api/mcp/result":
            return self.send(403, {"error": "当前账户未开启 MCP 科研通操作权限", "reason": "permission_denied"})
        if self.path == "/api/import":
            return self.send(200, {"ok": True, "batch_id": "bb1", "count": 1, "item_ids": ["p1"], "skipped_duplicates": 0})
        if self.path == "/api/monitor":
            return self.send(200, {"ok": True})
        self.send(400, {"error": "business problem"})


class CliTests(unittest.TestCase):
    def setUp(self):
        FakeService.posts = []
        self.fake = ThreadingHTTPServer(("127.0.0.1", 0), FakeService)
        threading.Thread(target=self.fake.serve_forever, daemon=True).start()
        self.base = "http://127.0.0.1:%d" % self.fake.server_port
        self.client = Client(self.base)

    def tearDown(self):
        self.fake.shutdown()
        self.fake.server_close()

    def run_cli(self, *argv, client=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = kytdl.main(list(argv), client or self.client)
        return code, out.getvalue(), err.getvalue()

    def test_status_ok_json(self):
        code, out, _ = self.run_cli("status", "--json")
        self.assertEqual(0, code)
        data = json.loads(out)
        self.assertTrue(data["ok"] and data["running"])
        self.assertEqual(0, data["exit"])

    def test_service_not_running_is_exit_2(self):
        dead = ThreadingHTTPServer(("127.0.0.1", 0), FakeService)
        port = dead.server_port
        dead.server_close()
        code, out, _ = self.run_cli("status", "--json", client=Client("http://127.0.0.1:%d" % port))
        self.assertEqual(2, code)
        self.assertEqual("service_not_running", json.loads(out)["reason"])
        code, _, err = self.run_cli("events", client=Client("http://127.0.0.1:%d" % port))
        self.assertEqual(2, code)
        self.assertIn("启动", err)

    def test_permission_denied_is_exit_3_and_business_error_is_1(self):
        code, out, _ = self.run_cli("result", "p1", "--json")
        self.assertEqual(3, code)
        self.assertEqual("permission_denied", json.loads(out)["reason"])
        code, out, _ = self.run_cli("oa", "10.1/x", "--json")   # fake answers 400
        self.assertEqual(1, code)
        code, out, _ = self.run_cli("batch", "missing", "--json")
        self.assertEqual(1, code)

    def test_import_file_and_token_header(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "refs.txt"
            path.write_text("10.1234/x\tFictional\n", "utf-8")
            code, out, _ = self.run_cli("import", str(path), "--json")
            self.assertEqual(0, code)
            self.assertEqual("bb1", json.loads(out)["batch_id"])
            self.assertEqual("fake-token", FakeService.posts[-1][2])
            code, out, _ = self.run_cli("import", str(Path(folder) / "none.txt"), "--json")
            self.assertEqual(1, code)

    def test_wait_exit_codes(self):
        code, out, _ = self.run_cli("wait", "p1", "--until", "fulfilled", "--timeout", "5", "--json")
        self.assertEqual(0, code)
        self.assertTrue(json.loads(out)["reached"])
        code, out, _ = self.run_cli("wait", "p1", "--until", "done", "--timeout", "1", "--json")
        self.assertEqual(1, code)
        self.assertEqual("timeout", json.loads(out)["reason"])
        code, out, _ = self.run_cli("wait", "zzz", "--until", "done", "--json")
        self.assertEqual(1, code)

    def test_monitor_toggle_posts_only_monitor_setting(self):
        code, _, _ = self.run_cli("monitor", "on", "--json")
        self.assertEqual(0, code)
        self.assertEqual(("/api/monitor", {"enabled": True}), FakeService.posts[-1][:2])

    def test_no_spending_commands_exist(self):
        for word in ("confirm", "publish", "adopt", "reject", "download", "highspeed"):
            with self.assertRaises(SystemExit):
                with contextlib.redirect_stderr(io.StringIO()):
                    kytdl.main([word])

    def test_cli_script_runs_as_subprocess(self):
        env = dict(os.environ, ABLESCI_LOCAL_URL=self.base)
        done = subprocess.run([sys.executable, str(ROOT / "kytdl.py"), "status", "--json"], env=env, capture_output=True, text=True, timeout=60, cwd=ROOT)
        self.assertEqual(0, done.returncode, done.stderr)
        self.assertTrue(json.loads(done.stdout)["running"])
        env["ABLESCI_LOCAL_URL"] = "http://127.0.0.1:1"
        done = subprocess.run([sys.executable, str(ROOT / "kytdl.py"), "status"], env=env, capture_output=True, text=True, timeout=60, cwd=ROOT)
        self.assertEqual(2, done.returncode)


class CliAgainstRealServer(unittest.TestCase):
    def test_import_batch_events_result(self):
        with tempfile.TemporaryDirectory() as folder:
            server = start(folder)
            try:
                client = Client("http://127.0.0.1:%d" % server.server_port)
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    with patch("sys.stdin", io.StringIO("10.1234/cli.1\tFictional CLI paper\n")):
                        self.assertEqual(0, kytdl.main(["import", "-", "--json"], client))
                batch_id = json.loads(out.getvalue())["batch_id"]
                for argv in (["batch", batch_id], ["batch"], ["events", "-n", "5"], ["monitor", "status"], ["status"]):
                    with contextlib.redirect_stdout(io.StringIO()):
                        self.assertEqual(0, kytdl.main(argv + ["--json"], client), argv)
                item_id = server.store.snapshot()["items"][0]["id"]
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(1, kytdl.main(["result", item_id, "--json"], client))   # no PDF yet: business result, not an error crash
            finally:
                stop(server)


if __name__ == "__main__":
    unittest.main()
