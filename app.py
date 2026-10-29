"""Run a private loopback-only demo: python app.py"""
import argparse
import base64
import csv
import hashlib
import io
import json
import re
import os
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlparse

from browser_agent import BrowserAgent
from core import Store, seed_demo, verify_pdf, doi_value
from download_policy import (RULES_VERSION, DOWNLOAD_DEFAULTS, DOWNLOAD_RANGES, HIGHSPEED_MODES, FORCE_DEFAULT_POINTS,
                             validate_download_settings, download_settings, download_defaults_as_preferences,
                             migrate_preferences, resolve_highspeed, chosen_mode)
from metadata import verify_with_metadata, fetch_metadata
from rejection_policy import REJECTION_RULES_VERSION, rejection_decision, reject_rules, manual_reason
import publish_policy as pp
import oa_fetch
from paste_parser import parse_references, make_record, unparsed_segments
from import_files import import_file
from credential_vault import CredentialVault, AIKeyVault, OpenAlexKeyVault
from accounts import Accounts
from task_states import workflow_state
from pipeline import PIPELINE_RULES_VERSION, Pipeline, stage_of, tier, gate, next_step, rules_ok, has_session, acquisition_state
from ai_service import AIService
import app_info
from runtime_paths import python_executable
import applog
import notifications
import intake
import deletion
from notifications import Notifier
from monitor import Monitor, validate_monitor
from ratelimit import LIMIT_DEFAULTS, LIMIT_RANGES, limit_settings, validate_limits
from autostart import Autostart
from instance_lock import InstanceLock, AlreadyRunning
from mcp_api import McpApi, PermissionDenied

ROOT = Path(__file__).resolve().parent
VENDOR_FILES = {
    "/assets/vendor/bootstrap-5.3.3/bootstrap.min.css": "static/vendor/bootstrap-5.3.3/bootstrap.min.css",
    "/assets/vendor/bootstrap-5.3.3/bootstrap.bundle.min.js": "static/vendor/bootstrap-5.3.3/bootstrap.bundle.min.js",
}


def now_stamp():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def open_with_system(path):
    if not hasattr(os, "startfile"):
        raise ValueError("当前系统不支持直接打开")
    os.startfile(path)


def open_panel_window(url):
    """Open the app window on url (same window logic as the tray and launcher); fall back to the default browser."""
    try:
        import launcher
        launcher.open_window(url)
    except Exception:
        import webbrowser
        webbrowser.open(url)


def make_server(port=8769, data_root=None, startup_dir=None, start_monitor=True, auto_screen=True):
    """No tray icon, toast or Startup-folder access happens here: those belong to the real entry point (main below).
    An isolated server (custom data_root) may only touch the Startup folder if the caller passes one explicitly."""
    fresh_install = not ((Path(data_root) if data_root else ROOT / "data") / "queue.json").exists()
    store = Store(data_root or ROOT / "data")
    accounts = Accounts(store.root)
    agent = BrowserAgent(store, accounts, use_worker=True)
    ai = AIService(AIKeyVault(store.root))
    openalex_vault = OpenAlexKeyVault(store.root)
    ai.restore(store.snapshot().get("preferences", {}).get("ai_settings"))
    report_cache = {}   # the last previewed report: "让 AI 分析" sends exactly this text, nothing else
    pipeline = Pipeline(store, accounts, agent)
    notifier = Notifier(store)
    pipeline.notifier = notifier
    agent.notifier = notifier
    pipeline.auto_screen = auto_screen
    monitor = Monitor(store, accounts, agent, pipeline, notifier, start=start_monitor)
    autostart = Autostart(ROOT, startup_dir, allowed=data_root is None or startup_dir is not None)

    def session_now():
        snap = store.snapshot()
        mine = [i for i in snap["items"] if i.get("account_id", "default") == accounts.active_id]
        return has_session(snap.get("account_metrics", {}).get(accounts.active_id, {}), mine, accounts.has_cookie_file())
    mcp_api = McpApi(store, accounts, pipeline, monitor, session_now)
    with store.lock:
        store.data.setdefault("account_metrics", {}).setdefault("default", dict(store.data.get("account", {})))
        for item in store.data["items"]:
            if item.get("source") != "demo": item.setdefault("account_id", "default")
        store.save()
    migrate_preferences(store)
    if accounts.recovered:
        store.event("账户列表文件无法读取，已从备份恢复。请核对当前账户。", kind="needs_action")
    applog.rotate_server_logs(store.root / "logs")
    applog.trim_screenshots(store.root / "screenshots")
    if store.snapshot().get("preferences", {}).get("pipeline_restart_notice"):
        text = "自动流程已在重启后关闭。"
        store.event(text, kind="needs_action", action="pipeline-enable")
        try:
            notifier.notify("needs_action", "account:pipeline-restart", "自动流程已关闭", text)
        except Exception:
            pass
    if not fresh_install and "disclaimer_ack" not in store.snapshot().get("preferences", {}):
        store.update_preferences(disclaimer_ack="upgrade")   # an existing installation is not interrupted by the first-run dialog
    token = secrets.token_urlsafe(32)

    def shutdown_service(server_ref):
        agent.request_stop()
        pipeline.close()
        monitor.close()
        agent.commands.put(("close", None, None))
        def close_service():
            agent.thread.join(timeout=50)
            server_ref.shutdown()
        threading.Thread(target=close_service, daemon=True).start()

    def handle_verification(item_id, allow_website=True):
        item = store.get(item_id)
        decision = rejection_decision(item, store.snapshot().get("preferences", {}))
        if not allow_website and decision["action"] == "auto_reject":
            decision.update(action="ask_user", reason="仅完成本地核对，未提交驳回。")
        store.update(item_id, rejection=decision)
        if decision["action"] == "auto_reject" and allow_website:
            try:
                agent.enqueue("reject_auto", item_id)
            except ValueError:
                store.event("检测到文件与求助明确不符，但浏览器正忙，未提交驳回。请稍后重新核对或手动处理。")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def respond(self, status, body, content_type="application/json; charset=utf-8"):
            if not isinstance(body, bytes):
                body = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.end_headers()
            self.wfile.write(body)

        def valid_host(self):
            return self.headers.get("Host") == f"127.0.0.1:{self.server.server_port}"

        def do_GET(self):
            if not self.valid_host():
                return self.respond(403, {"error": "仅允许本地地址访问"})
            path = urlparse(self.path).path
            if path == "/":
                html = (ROOT / "index.html").read_text("utf-8").replace("__TOKEN__", json.dumps(token))
                return self.respond(200, html.encode(), "text/html; charset=utf-8")
            if path in {"/assets/app.js", "/assets/styles.css"}:
                name = path.rsplit("/", 1)[-1]
                return self.respond(200, (ROOT / name).read_bytes(), "text/javascript; charset=utf-8" if name.endswith(".js") else "text/css; charset=utf-8")
            vendor = VENDOR_FILES.get(path)
            if vendor:
                return self.respond(200, (ROOT / vendor).read_bytes(), "text/javascript; charset=utf-8" if vendor.endswith(".js") else "text/css; charset=utf-8")
            if path == "/api/state":
                try:
                    deletion.sweep(store, notifier)
                except Exception:
                    pass
                snapshot = store.snapshot()
                snapshot["account"] = snapshot.get("account_metrics", {}).get(accounts.active_id, {})
                active_items = [i for i in snapshot["items"] if i.get("source") == "demo" or i.get("account_id", "default") == accounts.active_id]
                snapshot["items"] = [dict(i, workflow=workflow_state(i), stage=stage_of(i), deletion=deletion.view(i),
                                          acquisition=acquisition_state(i, gate(active_items, snapshot["account"].get("points"), i.get("request_reward")))) for i in active_items]
                snapshot.pop("account_metrics", None)
                for internal in ("monitor", "notifications", "publish_times"):
                    snapshot.pop(internal, None)
                prefs = snapshot.setdefault("preferences", {})
                core_configured = bool(prefs.pop("core_api_key", ""))   # the key itself never leaves the server
                prefs["mcp_website_actions"] = bool(prefs.get("mcp_website_actions") and prefs.get("mcp_actions_account", "default") == accounts.active_id)
                prefs_now = snapshot["preferences"]
                points = snapshot["account"].get("points")
                hs_mode, hs_source, _, _ = resolve_highspeed(prefs_now, points)
                snapshot["download"] = dict(settings=download_settings(prefs_now), defaults=dict(DOWNLOAD_DEFAULTS),
                                            ranges={k: list(v) for k, v in DOWNLOAD_RANGES.items()},
                                            highspeed=dict(mode=hs_mode, source=hs_source, chosen=chosen_mode(prefs_now)[1],
                                                           points=points, force_default_points=FORCE_DEFAULT_POINTS,
                                                           rules_version=RULES_VERSION))
                snapshot["batches"] = intake.public_batches(store.snapshot(), accounts.active_id, prefs_now, points)
                snapshot["reward"] = dict(steps=list(intake.REWARD_STEPS), min=intake.REWARD_MIN, cap=intake.reward_cap(points),
                                          default=intake.default_reward(prefs_now, points))
                snapshot["about"] = dict(version=app_info.APP_VERSION, repo_url=app_info.REPO_URL, issue_enabled=bool(applog.issue_url(app_info.REPO_URL, "t", "b")),
                                         disclaimer_version=app_info.DISCLAIMER_VERSION, disclaimer_pending=prefs_now.get("disclaimer_ack") != app_info.DISCLAIMER_VERSION)
                snapshot["pipeline"] = dict(
                    enabled=bool(prefs_now.get("pipeline_enabled")), auto_publish=rules_ok(prefs_now, "pipeline_auto_publish"),
                    auto_adopt=rules_ok(prefs_now, "pipeline_auto_adopt"), sync_minutes=prefs_now.get("pipeline_sync_minutes", 10),
                    rules_version=PIPELINE_RULES_VERSION, points=points, tier=tier(points),
                    gate=gate(snapshot["items"], points),
                    next_step=next_step(snapshot["items"], snapshot["account"], prefs_now, datetime.now().astimezone(), session=has_session(snapshot["account"], snapshot["items"], accounts.has_cookie_file())),
                    session=has_session(snapshot["account"], snapshot["items"], accounts.has_cookie_file()),
                    last_tick=pipeline.last_tick, oa_first=prefs_now.get("oa_first", True) is not False,
                    oa_challenge_mode=prefs_now.get("oa_challenge_mode", "prompt"), oa_running=pipeline.oa_running,
                    core_key_configured=core_configured, batch_pending=tier(points) == "batch",
                    restart_notice=bool(prefs_now.get("pipeline_restart_notice")) and not prefs_now.get("pipeline_enabled"))
                snapshot["publish"] = dict(raise_settings=pp.raise_settings(prefs_now), raise_steps=list(pp.RAISE_STEPS),
                                           raise_range=list(pp.RAISE_MAX_RANGE), reserve_range=list(pp.RESERVE_RANGE),
                                           defaults=dict(raise_max=pp.RAISE_MAX_DEFAULT, reserve_points=pp.RESERVE_DEFAULT),
                                           batch_pending=tier(points) == "batch")
                snapshot["reject_rules"] = reject_rules()
                return self.respond(200, dict(snapshot, app_id="ablesci-local-demo", app_root=str(ROOT), busy=agent.busy, download_worker=agent.worker_status(),
                    waiting=agent.commands.qsize(), accounts=accounts.public(), active_account_id=accounts.active_id,
                    credentials_saved=accounts.vault().exists(), ai=ai.public_config(),
                    openalex_key_saved=openalex_vault.exists(), openalex_env_configured=bool(os.environ.get("OPENALEX_API_KEY", "").strip()),
                    monitor=monitor.public(), autostart=dict(enabled=autostart.enabled(), available=autostart.allowed),
                    notify=dict(settings=notifications.settings(prefs_now), kinds={k: v[0] for k, v in notifications.KINDS.items()},
                                defaults=notifications.defaults(), pending=notifier.pending()),
                    limits=dict(settings=limit_settings(prefs_now), defaults=dict(LIMIT_DEFAULTS),
                                ranges={k: list(v) for k, v in LIMIT_RANGES.items()},
                                publish_recent=pipeline.publish_limiter.recent(), publish_wait_seconds=round(pipeline.publish_limiter.wait_seconds()))))
            if path == "/api/mcp-config":
                command = str(python_executable(ROOT))
                script = str(ROOT / "mcp_server.py")
                claude = {"mcpServers": {"kytdl": {"type": "stdio", "command": command, "args": [script]}}}
                codex = '[mcp_servers.kytdl]\ncommand = ' + json.dumps(command) + '\nargs = [' + json.dumps(script) + ']\nstartup_timeout_sec = 20\ntool_timeout_sec = 660\n'
                return self.respond(200, dict(claude=claude, codex=codex))
            if path.startswith("/pdf-preview/"):
                item = store.get(path.split("/")[-1])
                if not item or not item.get("pdf"): return self.respond(404, {"error": "未找到本地 PDF"})
                from urllib.parse import parse_qs
                try: page_number = int(parse_qs(urlparse(self.path).query).get("page", ["0"])[0])
                except ValueError: return self.respond(400, {"error": "页码无效"})
                import pymupdf
                target = (store.root / item["pdf"]).resolve()
                if not target.is_relative_to(store.root.resolve()): return self.respond(400, {"error": "文件路径无效"})
                try:
                    with pymupdf.open(target) as doc:
                        if not 0 <= page_number < min(3, doc.page_count): return self.respond(400, {"error": "页码无效"})
                        rect = doc[page_number].rect
                        scale = min(.65, 600 / max(rect.width, rect.height, 1))
                        binary = doc[page_number].get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False).tobytes("png")
                except Exception:
                    return self.respond(400, {"error": "该 PDF 暂无法预览，请手动检查文件"})
                return self.respond(200, binary, "image/png")
            if path == "/api/browser-view":
                screenshot = store.root / "screenshots" / "browser-last.png"
                if screenshot.exists():
                    return self.respond(200, screenshot.read_bytes(), "image/png")
                return self.respond(404, {"error": "尚无浏览器错误截图"})
            if path.startswith("/api/shot/"):
                item = store.get(path.split("/")[-1])
                shot = ((item or {}).get("publish_check") or {}).get("screenshot", "")
                target = (store.root / shot).resolve() if shot else None
                if target and target.is_relative_to((store.root / "screenshots").resolve()) and target.is_file():
                    return self.respond(200, target.read_bytes(), "image/png")
                return self.respond(404, {"error": "该任务没有截图"})
            if path == "/api/export":
                output = io.StringIO()
                writer = csv.writer(output)
                writer.writerow(["title", "doi", "source", "status", "website_status", "verdict", "reason", "assist_url", "pdf"])
                for item in store.snapshot()["items"]:
                    verdict = item.get("verification") or {}
                    values = [item.get(k, "") for k in ["title", "doi", "source", "status", "website_status"]] + [verdict.get("status", ""), verdict.get("reason", ""), item.get("assist_url", ""), item.get("pdf", "")]
                    writer.writerow(["'" + str(value) if str(value).startswith(("=", "+", "-", "@")) else value for value in values])
                return self.respond(200, ("\ufeff" + output.getvalue()).encode(), "text/csv; charset=utf-8")
            if path.startswith("/pdf/"):
                item = store.get(path.split("/")[-1])
                if item and item.get("pdf"):
                    target = (store.root / item["pdf"]).resolve()
                    if target.is_relative_to(store.root.resolve()) and target.is_file():
                        return self.respond(200, target.read_bytes(), "application/pdf")
                return self.respond(404, {"error": "文件不存在"})
            return self.respond(404, {"error": "未找到页面"})

        def do_POST(self):
            origin = f"http://127.0.0.1:{self.server.server_port}"
            if (not self.valid_host() or self.headers.get("Origin") != origin
                    or self.headers.get("X-Demo-Token") != token
                    or not self.headers.get("Content-Type", "").startswith("application/json")):
                return self.respond(403, {"error": "操作校验失败，请刷新页面"})
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 36_000_000:
                    raise ValueError("上传内容过大或为空")
                body = json.loads(self.rfile.read(size))
                path = urlparse(self.path).path
                if path == "/api/literature-search":
                    from literature_search import search_works
                    key = openalex_vault.load() if openalex_vault.exists() else os.environ.get("OPENALEX_API_KEY", "")
                    return self.respond(200, search_works(body.get("query"), body.get("cursor", "*"),
                                                          body.get("year_from"), body.get("year_to"), body.get("open_access", False), body.get("page_size", 50), body.get("mode", "keyword"), key))
                if path == "/api/openalex-key":
                    key = body.get("key")
                    if not isinstance(key, str) or len(key) > 256 or not key.strip() or re.search(r"\s", key):
                        raise ValueError("请输入有效的 OpenAlex API Key")
                    openalex_vault.save(key)
                    return self.respond(200, {"ok": True, "saved": True})
                if path == "/api/openalex-key-clear":
                    openalex_vault.forget()
                    return self.respond(200, {"ok": True, "saved": False})
                if path == "/api/import":
                    if "records" in body:
                        if not isinstance(body["records"], list) or len(body["records"]) > 200:
                            raise ValueError("每次最多导入 200 篇")
                        if any(not isinstance(r, dict) or not isinstance(r.get("authors", []), list)
                               or any(r.get(k) is not None and not isinstance(r.get(k), str) for k in ("title", "doi"))
                               for r in body["records"]):
                            raise ValueError("文献记录须为对象，作者须为列表")
                        source = "AI整理" if body.get("ai_assisted") is True else "text"
                        records = [make_record(r.get("title") or "", [str(a)[:200] for a in r.get("authors", [])][:300], r.get("doi") or "", source) for r in body["records"]]
                        if any(not r["title"] and not r["doi"] for r in records):
                            raise ValueError("每条文献至少需要有效标题或 DOI")
                    else:
                        records = parse_references(body.get("text", ""))
                    batch_id = intake.new_batch_id()
                    for record in records:
                        store.add(record["title"], record["doi"], account_id=accounts.active_id, authors=record["authors"], import_format=record["format"],
                                  batch_id=batch_id, request_confirmed=False)
                    new_ids = [i["id"] for i in store.snapshot()["items"] if i.get("batch_id") == batch_id]
                    count = len(new_ids)
                    if new_ids:
                        intake.create_batch(store, batch_id, accounts.active_id, new_ids,
                                            origin="mcp" if body.get("mcp_call") is True else "app")
                    store.event(f"已导入 {count} 条新文献，重复项已跳过。尚未发布求助。")
                    if new_ids and pipeline.auto_screen:
                        pipeline.screen_step()
                    return self.respond(200, {"ok": True, "batch_id": batch_id if new_ids else None, "count": count, "item_ids": new_ids,
                                              "skipped_duplicates": max(0, len(records) - count)})
                elif path.startswith("/api/mcp/"):
                    return self.respond(200, mcp_api.handle(path[len("/api/mcp/"):], body))
                elif path == "/api/parse":
                    text = body.get("text", "")
                    return self.respond(200, {"records": parse_references(text), "ai_used": False, "unparsed": unparsed_segments(text)[:20]})
                elif path == "/api/parse-file":
                    binary = base64.b64decode(body.get("base64", ""), validate=True)
                    return self.respond(200, {"records": import_file(str(body.get("filename", "")), binary), "ai_used": False})
                elif path == "/api/forget-credentials":
                    accounts.vault().forget()
                    store.event("已删除保存的邮箱和密码，浏览器登录状态仍保留。")
                elif path == "/api/accounts/add":
                    if agent.busy or not agent.commands.empty(): raise ValueError("请等待当前操作完成后再切换账户")
                    account_id = accounts.add(body.get("label", ""))
                    agent.enqueue("switch_account", account_id)
                elif path == "/api/accounts/switch":
                    account_id = body.get("account_id", "")
                    accounts.account_root(account_id)
                    agent.enqueue("switch_account", account_id)
                elif path == "/api/ai/settings":
                    if not isinstance(body.get("enabled"), bool): raise ValueError("请选择是否开启 AI 辅助")
                    ai.configure(body["enabled"], str(body.get("api_key", "")), str(body.get("model", "")), body.get("consent") is True,
                                 provider=body.get("provider") or None, base_url=str(body.get("base_url", "")), storage=body.get("storage") or None)
                    store.update_preferences(ai_settings=ai.settings_dict())
                    store.event("AI 辅助已" + ("开启，每次发送前需预览并确认。" if body["enabled"] else "关闭。"))
                elif path == "/api/ai/test":
                    result = ai.test_connection(str(body.get("api_key", "")), body.get("provider") or None, str(body.get("base_url", "")), str(body.get("model", "")))
                    return self.respond(200, dict(result, note="仅查询模型列表，未发送文献内容"))
                elif path == "/api/ai/forget-key":
                    ai.forget_key()
                    store.update_preferences(ai_settings=ai.settings_dict())
                    store.event("已删除保存的 AI 密钥并关闭 AI。")
                elif path == "/api/ai/organize":
                    result = ai.organize_text(body.get("segments"), body.get("consent"))
                    store.event("AI 整理结果已生成，确认后才会导入，未发送其他内容。")
                    return self.respond(200, result)
                elif path == "/api/logs/recent":
                    return self.respond(200, {"errors": store.log.recent_errors(20)})
                elif path == "/api/report/build":
                    ids = body.get("error_ids")
                    if ids is not None and (not isinstance(ids, list) or len(ids) > 50 or any(not isinstance(i, str) for i in ids)):
                        raise ValueError("所选错误无效")
                    text = applog.build_report(store, ids or None)
                    report_cache.clear()
                    report_cache.update(id=hashlib.sha1(text.encode("utf-8")).hexdigest()[:12], text=text)
                    return self.respond(200, {"report_id": report_cache["id"], "text": text})
                elif path == "/api/report/analyze":
                    if not report_cache or body.get("report_id") != report_cache.get("id"):
                        raise ValueError("请先生成并预览报告")
                    result = ai.analyze_report(report_cache["text"], body.get("consent"))
                    store.event("AI 已分析预览的脱敏报告，结果仅供参考，未执行任何操作。")
                    return self.respond(200, result)
                elif path == "/api/report/issue":
                    url = applog.issue_url(app_info.REPO_URL, "科研通下载器 问题反馈", "请描述遇到的问题，并附上已保存的错误报告（.md）。提交前请确认报告中不含个人信息。")
                    if not url: raise ValueError("仓库地址未设置，无法打开 GitHub Issue")
                    return self.respond(200, {"url": url})
                elif path == "/api/ai/recognize":
                    item = store.get(body.get("id"))
                    if not item or not item.get("pdf"): raise ValueError("请先保存本地 PDF")
                    target = (store.root / item["pdf"]).resolve()
                    if not target.is_relative_to(store.root.resolve()): raise ValueError("文件路径无效")
                    result = ai.recognize(target, body.get("pages"), body.get("consent"))
                    store.update(item["id"], ai_assistance=result)
                    store.event("AI 识别结果已保存在本机，需人工复核，未改变本地核对结果。")
                elif path == "/api/demo":
                    seed_demo(store)
                elif path == "/api/preferences":
                    if "default_reward" in body:
                        points_now = store.snapshot().get("account_metrics", {}).get(accounts.active_id, {}).get("points")
                        store.update_preferences(default_reward=intake.validate_reward(body["default_reward"], points_now))
                        return self.respond(200, {"ok": True})
                    if "disclaimer_ack" in body:
                        if body["disclaimer_ack"] is not True:
                            raise ValueError("参数无效")
                        store.update_preferences(disclaimer_ack=app_info.DISCLAIMER_VERSION)
                        return self.respond(200, {"ok": True})
                    if "home_notice_hidden" in body:
                        if not isinstance(body["home_notice_hidden"], bool):
                            raise ValueError("首页提示参数无效")
                        store.update_preferences(home_notice_hidden=body["home_notice_hidden"])
                        return self.respond(200, {"ok": True})
                    if "mcp_website_actions" in body:
                        enabled = body["mcp_website_actions"]
                        if not isinstance(enabled, bool):
                            raise ValueError("请选择 MCP 的科研通操作权限")
                        store.update_preferences(mcp_website_actions=enabled, mcp_actions_account=accounts.active_id if enabled else "")
                        store.event("MCP 科研通操作权限已" + ("开启" if enabled else "关闭"))
                        return self.respond(200, {"ok": True})
                    if "rejection_mode" in body:
                        mode = body["rejection_mode"]
                        if mode not in {"manual", "auto"}:
                            raise ValueError("请选择手动或自动驳回模式")
                        if mode == "auto" and body.get("rejection_rules_version") != REJECTION_RULES_VERSION:
                            raise ValueError("请先确认自动驳回规则")
                        store.update_preferences(rejection_mode=mode, rejection_rules_version=REJECTION_RULES_VERSION if mode == "auto" else "")
                        store.event("驳回模式已设为" + ("自动，仅当 DOI 与标题均明确不符时才提交驳回。" if mode == "auto" else "手动，异常情况由用户判断。"))
                        return self.respond(200, {"ok": True})
                    if "download_settings" in body or body.get("download_reset") is not None:
                        if body.get("download_reset") is not None:
                            if body["download_reset"] is not True:
                                raise ValueError("恢复默认参数无效")
                            store.update_preferences(**download_defaults_as_preferences())
                            store.event("下载参数已恢复默认。")
                        else:
                            store.update_preferences(**validate_download_settings(body["download_settings"]))
                            store.event("下载参数已更新。")
                        return self.respond(200, {"ok": True})
                    if "download_highspeed" in body:
                        mode = body["download_highspeed"]
                        if mode not in HIGHSPEED_MODES:
                            raise ValueError("请选择关闭、智能或强制")
                        account_points = store.snapshot().get("account_metrics", {}).get(accounts.active_id, {}).get("points")
                        free = isinstance(account_points, int) and not isinstance(account_points, bool) and account_points >= FORCE_DEFAULT_POINTS
                        if mode != "off" and not free and body.get("rules_version") != RULES_VERSION:
                            raise ValueError("积分低于 500 或未知时，请先阅读并确认高速通道规则")
                        keep = store.snapshot().get("preferences", {}).get("rules_version", "")
                        store.update_preferences(download_highspeed=mode, download_highspeed_chosen=True, download_default_force_applied=False,
                                                 allow_paid_fallback=mode != "off",
                                                 rules_version=RULES_VERSION if body.get("rules_version") == RULES_VERSION and mode != "off" else ("" if mode == "off" else keep))
                        store.event({"off": "高速通道已设为关闭，仅使用 3 条普通线路。",
                                     "smart": "高速通道已设为智能，普通线路失败（含预估超时）后切换一次。",
                                     "force": "高速通道已设为强制，跳过普通线路；积分低于 500 时，每个文件首次使用高速扣 2 积分。"}[mode])
                        return self.respond(200, {"ok": True})
                    enabled = body.get("allow_paid_fallback")
                    if not isinstance(enabled, bool):
                        raise ValueError("请选择是否启用高速通道后备")
                    if enabled and body.get("rules_version") != RULES_VERSION:
                        raise ValueError("请先阅读并确认高速通道规则")
                    store.update_preferences(allow_paid_fallback=enabled, rules_version=RULES_VERSION if enabled else "",
                                             download_highspeed="smart" if enabled else "off", download_highspeed_chosen=True,
                                             download_default_force_applied=False)
                    store.event("已开启高速通道后备：3 条普通线路均失败后切换；积分低于 500 时，每个文件首次最多扣 2 积分。" if enabled else "已关闭高速通道后备。")
                elif path == "/api/monitor":
                    changes = validate_monitor(body)
                    if body.get("sync_now") is True:
                        if len(body) > 1: raise ValueError("立即同步需单独提交")
                        monitor.sync_now()
                        return self.respond(200, {"ok": True})
                    if "sync_now" in body: raise ValueError("立即同步参数无效")
                    if "monitor_enabled" in changes:
                        monitor.set_enabled(changes.pop("monitor_enabled"))
                    if changes:
                        store.update_preferences(**changes)
                        monitor.reschedule()
                        store.event("后台监控间隔已更新。")
                elif path == "/api/notifications":
                    changes = notifications.validate(body, notifications.settings(store.snapshot().get("preferences", {})))
                    store.update_preferences(**changes)
                    store.event("通知设置已更新。")
                elif path == "/api/autostart":
                    if set(body) != {"enabled"} or not isinstance(body["enabled"], bool):
                        raise ValueError("开机自启参数无效")
                    if body["enabled"]:
                        kind = autostart.enable()
                        store.event("已开启开机自启（" + ("“启动”文件夹快捷方式" if kind == "lnk" else "“启动”文件夹 .cmd 备用方式") + "）：仅启动服务与托盘，不弹出窗口。")
                    else:
                        autostart.disable()
                        store.event("已关闭开机自启，并删除“启动”文件夹中的项目。")
                elif path == "/api/limits":
                    store.update_preferences(**validate_limits(body))
                    store.event("请求间隔设置已更新。")
                elif path == "/api/pipeline":
                    allowed = {"enabled", "auto_publish", "auto_adopt", "sync_minutes", "rules_version", "oa_first", "oa_contact_email", "oa_challenge_mode",
                               "raise_mode", "raise_max", "reserve_points", "raise_confirmed", "core_api_key"}
                    if not isinstance(body, dict) or not body or set(body) - allowed or not set(body) & (allowed - {"rules_version"}):
                        raise ValueError("自动流程设置无效")
                    prefs_now = store.snapshot().get("preferences", {})
                    changes = {}
                    for key in ("enabled", "auto_publish", "auto_adopt"):
                        if key in body:
                            if not isinstance(body[key], bool):
                                raise ValueError("自动流程开关参数无效")
                            changes["pipeline_" + key] = body[key]
                    if "sync_minutes" in body:
                        minutes = body["sync_minutes"]
                        if not isinstance(minutes, int) or isinstance(minutes, bool) or not 5 <= minutes <= 120:
                            raise ValueError("同步间隔须为 5–120 的整数（分钟）")
                        changes["pipeline_sync_minutes"] = minutes
                    if "oa_first" in body:
                        if not isinstance(body["oa_first"], bool):
                            raise ValueError("免费全文优先开关参数无效")
                        changes["oa_first"] = body["oa_first"]
                    if "oa_challenge_mode" in body:
                        if body["oa_challenge_mode"] not in ("prompt", "popup", "skip"):
                            raise ValueError("人机验证处理方式无效")
                        changes["oa_challenge_mode"] = body["oa_challenge_mode"]
                    if "oa_contact_email" in body:
                        email = body["oa_contact_email"]
                        if not isinstance(email, str) or (email.strip() and (len(email.strip()) > 254 or not re.fullmatch(r"[^@\s]{1,64}@[^@\s]+\.[^@\s]{2,}", email.strip()))):
                            raise ValueError("联系邮箱格式不正确（可留空）")
                        changes["oa_contact_email"] = email.strip()
                    if set(body) & {"raise_mode", "raise_max", "reserve_points"}:
                        changes.update(pp.validate_raise(body, pp.raise_settings(prefs_now)))
                    if "core_api_key" in body:
                        key = body["core_api_key"]
                        if not isinstance(key, str) or len(key.strip()) > 200 or re.search(r"\s", key.strip()):
                            raise ValueError("CORE 密钥格式不正确（可留空）")
                        changes["core_api_key"] = key.strip()
                    final = {k: changes.get("pipeline_" + k, rules_ok(prefs_now, "pipeline_" + k)) for k in ("auto_publish", "auto_adopt")}
                    newly = [k for k in final if final[k] and not rules_ok(prefs_now, "pipeline_" + k)]
                    if newly and body.get("rules_version") != PIPELINE_RULES_VERSION:
                        raise ValueError("请先确认自动流程规则，再开启自动求助或自动采纳")
                    changes["pipeline_rules_version"] = PIPELINE_RULES_VERSION if any(final.values()) else ""
                    if "pipeline_enabled" in changes:
                        changes["pipeline_restart_notice"] = False     # the person has decided; stop reminding
                    store.update_preferences(**changes)
                    store.event("自动流程设置已更新：自动流程" + ("已开启" if changes.get("pipeline_enabled", prefs_now.get("pipeline_enabled")) else "未开启") + "；自动求助" + ("已开启" if final["auto_publish"] else "已关闭") + "，自动采纳" + ("已开启" if final["auto_adopt"] else "已关闭") + "。")
                elif path in ("/api/oa-fetch", "/api/oa-human", "/api/oa-skip", "/api/oa-accept", "/api/oa-discard"):
                    if ("lookup_only" in body and not isinstance(body["lookup_only"], bool)) or ("url" in body and not isinstance(body["url"], str)):
                        raise ValueError("参数无效")
                    item = store.get(body.get("id"))
                    if not item or item.get("source") != "import" or item.get("account_id", "default") != accounts.active_id:
                        raise ValueError("仅支持当前账户中本地导入的文献")
                    if path == "/api/oa-accept":
                        if body.get("confirmed") is not True:
                            raise ValueError("请确认保留该免费全文文件为完成")
                        pipeline.oa_accept(item["id"])
                    elif path == "/api/oa-discard":
                        pipeline.oa_discard(item["id"])
                    elif path == "/api/oa-skip":
                        pipeline.oa_skip(item["id"])
                    elif path == "/api/oa-human":
                        check = item.get("oa_check") or {}
                        if check.get("status") != "needs_human" or not str(check.get("manual_url", "")).startswith(("http://", "https://")):
                            raise ValueError("该条目没有待人工验证的免费全文页面")
                        pipeline.oa_start(item["id"], human=True)
                    else:
                        if not item.get("doi") or item.get("pdf") or stage_of(item) != "queued":
                            raise ValueError("仅支持尚未发布、没有 PDF 且带 DOI 的本地条目")
                        old = item.get("oa_check")
                        if old and old.get("status") == "downloaded":
                            raise ValueError("已有免费全文文件，不会覆盖")
                        if (store.root / "oa" / (item["id"] + ".pdf")).exists():
                            raise ValueError("免费全文目标文件已存在，不会覆盖")
                        options = {}
                        if body.get("lookup_only"):
                            options["lookup_only"] = True
                        if body.get("url"):
                            if not any(l.get("url") == body["url"] for l in (old or {}).get("links") or []):
                                raise ValueError("该链接不在已找到的免费全文来源中")
                            options["only_url"] = body["url"]
                        pipeline.oa_start(item["id"], **options)
                elif path in ("/api/batch/confirm", "/api/batch/defer", "/api/batch/seen"):
                    if not isinstance(body.get("batch_id"), str):
                        raise ValueError("导入记录参数无效")
                    snap_now = store.snapshot()
                    account_now = snap_now.get("account_metrics", {}).get(accounts.active_id, {})
                    if path == "/api/batch/confirm":
                        mine = [i for i in snap_now["items"] if i.get("account_id", "default") == accounts.active_id]
                        count = intake.confirm(store, body["batch_id"], body.get("item_ids"), body.get("rewards"), account_now.get("points"),
                                               has_session(account_now, mine, accounts.has_cookie_file()), accounts.active_id)
                        return self.respond(200, {"ok": True, "confirmed": count})
                    if path == "/api/batch/defer":
                        intake.mark(store, body["batch_id"], accounts.active_id, prompted=True, deferred_at=datetime.now().astimezone().isoformat(timespec="seconds"))
                        store.event("已选择稍后确认求助清单，可从右下角消息面板重新打开。", kind="intake", batch_id=body["batch_id"], action="open-batch")
                    else:
                        intake.mark(store, body["batch_id"], accounts.active_id, prompted=True)
                elif path == "/api/pipeline/step":
                    planned = pipeline.plan()[3]
                    if planned["requires_user"] or planned["action"] in pipeline.PASSIVE:
                        planned = pipeline.local_plan() or planned   # local OA work never waits on 科研通-side steps
                    if planned["requires_user"]:
                        raise ValueError(planned["reason"] or "该步骤需手动处理")
                    if not pipeline.execute(planned):
                        return self.respond(200, {"ok": True, "executed": False, "step": planned})
                    return self.respond(200, {"ok": True, "executed": True, "step": planned})
                elif path == "/api/verify":
                    item = store.get(body.get("id"))
                    if not item or not item.get("pdf"):
                        raise ValueError("请先为该条选择本地 PDF 或下载文件")
                    result = verify_with_metadata(store.root / item["pdf"], item.get("expected_title", item["title"]), item.get("expected_doi", item["doi"]), store.root, lookup=item["source"] != "demo",
                                                  email=store.snapshot().get("preferences", {}).get("oa_contact_email", ""))
                    store.update(item["id"], verification=result, status="oa_review" if item.get("status") == "oa_review" else "checked")
                    handle_verification(item["id"], allow_website=body.get("allow_website_actions") is not False)
                    store.event("已完成本地 PDF 核对，未操作科研通。")
                elif path == "/api/metadata":
                    item = store.get(body.get("id"))
                    if not item or item["source"] == "demo" or not item.get("doi"):
                        raise ValueError("请先提供真实 DOI；演示数据不查询在线数据库")
                    record = fetch_metadata(item["doi"], store.root, store.snapshot().get("preferences", {}).get("oa_contact_email", ""))
                    store.update(item["id"], metadata=record)
                    store.event("已按 DOI 查询公开书目并保存在本机，未发送 PDF 或账号信息。")
                elif path == "/api/upload":
                    item = store.get(body.get("id"))
                    if not item or item.get("pdf"):
                        raise ValueError("任务不存在或已有文件，不会覆盖已保存的 PDF")
                    binary = base64.b64decode(body["base64"], validate=True)
                    if len(binary) > 25_000_000 or not binary.startswith(b"%PDF-"):
                        raise ValueError("仅支持 25 MB 以内的 PDF 文件")
                    folder = store.root / "uploads"
                    folder.mkdir(exist_ok=True)
                    target = folder / (item["id"] + ".pdf")
                    # A leftover from an earlier upload that no item references is simply replaced (the item has no PDF).
                    referenced = any(i.get("pdf") and (store.root / i["pdf"]).resolve() == target.resolve() for i in store.snapshot()["items"])
                    if target.exists() and referenced:
                        raise ValueError("该文件已被其他记录使用，不会覆盖")
                    with target.open("wb") as output:
                        output.write(binary)
                    result = verify_with_metadata(target, item["title"], item["doi"], store.root, lookup=item["source"] != "demo",
                                                  email=store.snapshot().get("preferences", {}).get("oa_contact_email", ""))
                    store.update(item["id"], pdf=str(target.relative_to(store.root)), verification=result, status="checked")
                    handle_verification(item["id"])
                    store.event("已保存本地 PDF 并完成核对，文件未上传至任何网站。")
                elif path == "/api/browser":
                    action = body.get("action")
                    if body.get("mcp_call") is True:
                        prefs = store.snapshot().get("preferences", {})
                        mcp_api.need_permission()
                    if action not in {"login", "sync", "signin", "prepare", "review", "download", "reject", "publish", "adopt"}:
                        raise ValueError("演示数据不支持该操作")
                    if action in {"prepare", "review", "download", "reject", "publish", "adopt"} and not store.get(body.get("id")):
                        raise ValueError("未找到任务")
                    if action == "download" and (store.get(body["id"]) or {}).get("site_file_deleted") and not (store.get(body["id"]) or {}).get("pdf"):
                        raise ValueError("科研通文件已删除，无法下载")
                    if action == "adopt" and (body.get("confirmed") is not True or stage_of(store.get(body["id"])) != "awaiting_decision"):
                        raise ValueError("仅支持已下载核对、待处理的本人应助，请确认后采纳")
                    if action == "publish" and (body.get("confirmed") is not True or stage_of(store.get(body["id"])) != "queued"):
                        raise ValueError("仅支持尚未发布的本地求助。发布将扣除积分，请确认后再发布")
                    if action == "reject":
                        item = store.get(body.get("id"))
                        decision = rejection_decision(item, store.snapshot().get("preferences", {}))
                        category, reason = manual_reason(body.get("category"), body.get("detail"))
                        if not decision["eligible"] or body.get("confirmed") is not True:
                            raise ValueError("仅支持本人未完结、已下载核对的应助，请确认后驳回")
                        store.update(item["id"], manual_reject_reason=reason, manual_reject_category=category)
                    credentials = None
                    if action == "login" and body.get("use_saved"):
                        credentials = accounts.vault().load()
                    if action == "login" and (body.get("email") or body.get("password")):
                        if not body.get("email") or not body.get("password"):
                            raise ValueError("自动登录需同时填写邮箱和密码，或留空后在浏览器中自行登录")
                        if len(body["email"]) > 320 or len(body["password"]) > 1000:
                            raise ValueError("邮箱或密码过长")
                        credentials = {"email": body["email"], "password": body["password"]}
                        if body.get("save_credentials") is True:
                            accounts.vault().save(credentials["email"], credentials["password"])
                            store.event("邮箱和密码已使用 Windows 用户加密保存在本机；未保存明文。")
                    if action == "download":
                        if agent.busy or not agent.commands.empty() or agent.download_busy():
                            raise ValueError("已有任务运行，请等待完成或停止后再操作")
                        # A person pressing 下载/重试 starts fresh: failure counters and the retry schedule are cleared.
                        store.update(body["id"], normal_failures=0, highspeed_failures=0, highspeed_clicks=0, download_retry=None,
                                     **({"status": "ready"} if (store.get(body["id"]) or {}).get("status") == "retrying" else {}))
                    agent.enqueue(action, body.get("id"), credentials)
                    if action == "publish":
                        pipeline.publish_limiter.record()   # manual publishes count towards the hourly cap
                elif path == "/api/open":
                    item = store.get(body.get("id"))
                    if not item or not item.get("pdf") or body.get("target") not in ("pdf", "folder"):
                        raise ValueError("没有可打开的本地 PDF")
                    if item.get("source") != "demo" and item.get("account_id", "default") != accounts.active_id:
                        raise ValueError("该条目属于另一个账户")
                    target = (store.root / item["pdf"]).resolve()
                    if not target.is_relative_to(store.root.resolve()) or not target.is_file():
                        raise ValueError("文件不存在或不在数据目录内，无法打开")
                    if body["target"] == "pdf" and target.suffix.lower() != ".pdf":
                        raise ValueError("仅可直接打开 .pdf 文件。该文件不是 PDF，请使用“打开所在文件夹”查看")
                    self.server.open_path(str(target.parent if body["target"] == "folder" else target))
                elif path == "/api/open-panel":
                    item_id = str(body.get("id") or "")
                    if item_id and not store.get(item_id):
                        raise ValueError("未找到该条目")
                    url = origin + "/" + ("#item=" + quote(item_id, safe="") if item_id else "")
                    self.server.open_panel(url)
                    return self.respond(200, {"ok": True, "url": url})
                elif path == "/api/oa-cache-clear":
                    removed = oa_fetch.clear_oa_cache(store.root)
                    store.event("已清除免费全文查找缓存（%d 条），下次将重新查询。" % removed)
                elif path == "/api/item/ack":
                    item = store.get(body.get("id"))
                    if not item or item.get("status") != "attention" or not item.get("publish_check"):
                        raise ValueError("该条目不处于“发布已停止”状态")
                    if item.get("source") != "demo" and item.get("account_id", "default") != accounts.active_id:
                        raise ValueError("该条目属于另一个账户")
                    publish_state = (item.get("publish_action") or {}).get("status")
                    if publish_state in ("attempted", "uncertain") or (publish_state == "confirmed" and not item.get("assist_url")):
                        raise ValueError("发布结果不确定，请先在科研通核实，软件不会自动清除")
                    if publish_state == "confirmed":
                        store.update(item["id"], status="published", note="已发布，等待应助。")
                        store.event("已清除提示，该求助已在科研通发布：" + (item.get("title") or "")[:40])
                    else:
                        store.update(item["id"], status="ready", note="已由用户接手，不再自动发布")
                        store.event("已标记为用户接手，不再自动发布：" + (item.get("title") or "")[:40])
                elif path in ("/api/item/reset-publish", "/api/item/confirm-published"):
                    item = store.get(body.get("id"))
                    if not item or (item.get("publish_action") or {}).get("status") not in ("attempted", "uncertain"):
                        raise ValueError("该条目没有“结果不确定”的发布记录")
                    if item.get("source") != "demo" and item.get("account_id", "default") != accounts.active_id:
                        raise ValueError("该条目属于另一个账户")
                    if body.get("confirmed") is not True:
                        raise ValueError("请先在科研通“我的求助”核实，再确认")
                    title = (item.get("title") or "")[:40]
                    if path == "/api/item/reset-publish":
                        store.update(item["id"], publish_action=None, publish_blocked=False, status="ready", note="已核实科研通上无此求助，已重置，可重新发布")
                        store.event("已重置发布记录（核实科研通上无此求助）：" + title, item_id=item["id"], kind="publish")
                    else:
                        store.update(item["id"], publish_action=dict(item["publish_action"], status="uncertain", user_verified=now_stamp()),
                                     note="已核实科研通上已发出，将在下次同步时按标题对应")
                        store.event("已记录：《%s》经核实已在科研通发出，将在下次同步时自动对应。" % title, item_id=item["id"], kind="publish")
                        try:
                            agent.enqueue("sync")
                        except ValueError:
                            pass    # busy: the regular monitor/pipeline sync will pick it up
                elif path in ("/api/item/handover-done", "/api/item/handover-retry"):
                    item = store.get(body.get("id"))
                    if not item or not item.get("handover") or item.get("status") != "attention":
                        raise ValueError("该条目不处于“科研通弹窗遮挡、待手动处理”状态")
                    if item.get("source") != "demo" and item.get("account_id", "default") != accounts.active_id:
                        raise ValueError("该条目属于另一个账户")
                    title = (item.get("title") or "")[:40]
                    if path == "/api/item/handover-done":
                        store.update(item["id"], handover=dict(item["handover"], waiting_sync=True, done_claimed=now_stamp()),
                                     note="已在科研通处理，等待同步确认结果")
                        store.event("已记录：《%s》已在科研通处理，正在同步确认。" % title, item_id=item["id"], kind="sync")
                        try:
                            agent.enqueue("sync")
                        except ValueError:
                            pass
                    else:
                        store.update(item["id"], status="downloaded", handover=None, handover_last=dict(item["handover"], retried=now_stamp()),
                                     note="已恢复为待处理，可重新采纳或驳回")
                        store.event("已恢复为待处理，可重新尝试：" + title, item_id=item["id"], kind="adopt")
                elif path == "/api/stop":
                    agent.request_stop()
                    store.event("已请求停止当前下载，等待浏览器返回。已完成的文件会保留。")
                elif path == "/api/shutdown":
                    shutdown_service(self.server)
                else:
                    return self.respond(404, {"error": "不支持该操作"})
                return self.respond(200, {"ok": True})
            except PermissionDenied as exc:
                return self.respond(400, {"error": str(exc)[:250], "reason": "permission_denied"})
            except (ValueError, KeyError, TypeError) as exc:
                return self.respond(400, {"error": str(exc)[:250]})
            except Exception as exc:
                applog.record(store, "api", urlparse(self.path).path, exc)
                return self.respond(500, {"error": "操作未完成，请检查文件权限或重启软件。已保存的数据不受影响"})

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.store = store
    server.agent = agent
    server.accounts = accounts
    server.ai = ai
    server.pipeline = pipeline
    server.monitor = monitor
    server.mcp_api = mcp_api
    server.notifier = notifier
    server.open_path = open_with_system
    server.open_panel = open_panel_window
    server.autostart = autostart
    server.shutdown_service = lambda: shutdown_service(server)
    return server


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8769)
    parser.add_argument("--tray", action="store_true", help="show the system-tray icon and Windows notifications (set by launcher.py)")
    args = parser.parse_args(argv)
    lock = InstanceLock(ROOT / "data" / ".kytdl.lock")
    try:
        lock.acquire()
    except AlreadyRunning as exc:
        raise SystemExit(str(exc))
    server = None
    tray = None
    try:
        server = make_server(args.port)
        print(f"{app_info.APP_NAME_ASCII}: http://127.0.0.1:{server.server_port}", flush=True)
        if args.tray:
            try:
                import launcher
                from tray import Tray
                logs = ROOT / "data" / "logs"

                def toggle_pause():
                    server.monitor.toggle()

                def sync_now():
                    try:
                        server.monitor.sync_now()
                    except ValueError:
                        pass
                tray = Tray(server.monitor.label, lambda: not server.monitor.enabled(),
                            dict(open_window=launcher.open_window, sync_now=sync_now, toggle_pause=toggle_pause,
                                 quit=server.shutdown_service), logs).start()
                server.notifier.sink = tray.notify
            except Exception as exc:   # a missing tray library must not stop the service
                print("tray unavailable:", type(exc).__name__, flush=True)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if tray:
            tray.stop()
        if server:
            server.server_close()
        lock.release()


if __name__ == "__main__":
    main()
