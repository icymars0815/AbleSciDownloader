"""Server-side half of the harness (MCP / CLI) integration: /api/mcp/* endpoints.
Anything that must not be forgeable by an external harness lives here: confirm tokens, permission checks, path confinement.
Nothing here changes download/highspeed mode, AI settings, MCP permission, or reveals credentials. Confirmed MCP requests are processed by the bounded background pipeline."""
import re
import secrets
import threading
import time
from datetime import datetime
from pathlib import Path

import applog
import intake
import oa_fetch
from core import doi_value, now
from pipeline import stage_of, acquisition_state, gate

TOKEN_TTL = 300
RENDER_MAX_PAGE = 3
ASSIST_KEEP = 5
FAIL_WORDS = ("失败", "错误", "停下", "无法", "超时", "未完成", "过期", "不符", "异常", "拒绝", "登录失效")


class PermissionDenied(ValueError):
    """Needs the App-side MCP 科研通 permission for the active account (served as HTTP 400 with reason permission_denied)."""
    reason = "permission_denied"


class ConfirmTokens:
    """Short-lived, single-use confirm tokens bound to batch + items + rewards + account. Memory only."""
    def __init__(self, ttl=TOKEN_TTL, clock=time.time):
        self.ttl, self.clock = ttl, clock
        self.lock = threading.Lock()
        self.items = {}

    @staticmethod
    def binding(batch_id, account_id, chosen):
        return (batch_id, account_id, tuple(sorted((iid, int(reward)) for iid, reward in chosen)))

    def issue(self, binding):
        with self.lock:
            now_ = self.clock()
            self.items = {t: v for t, v in self.items.items() if v[1] > now_}
            token = secrets.token_urlsafe(24)
            self.items[token] = (binding, now_ + self.ttl)
            return token

    def consume(self, token, batch_id, account_id):
        """Return the bound (item, reward) pairs; the token is spent either way once it was found."""
        with self.lock:
            entry = self.items.pop(token, None) if isinstance(token, str) else None
        if entry is None:
            raise ValueError("确认令牌无效或已使用；请重新预览")
        binding, expires = entry
        if self.clock() > expires:
            raise ValueError("确认令牌已过期；请重新预览")
        if binding[0] != batch_id or binding[1] != account_id:
            raise ValueError("确认令牌与导入记录或账户不符；请重新预览")
        return binding[2]


def redact(text, root=None):
    """Central redaction (applog.redact) plus tighter MCP limits: raw paths, link queries and long token-like strings go too."""
    text = applog.redact(text, root)
    text = re.sub(r"[A-Za-z]:[\\/][^\s\"'，。；]*", "<path>", text)
    text = re.sub(r"(https?://[^\s/?#]+)[^\s]*", r"\1/…", text)
    text = re.sub(r"\b[A-Za-z0-9_\-]{28,}\b", "<redacted>", text)
    return text[:300]


class McpApi:
    def __init__(self, store, accounts, pipeline, monitor, session, tokens=None):
        self.store, self.accounts, self.pipeline, self.monitor = store, accounts, pipeline, monitor
        self.session = session
        self.tokens = tokens or ConfirmTokens()

    # ---- shared ----
    def allowed(self):
        prefs = self.store.snapshot().get("preferences", {})
        return bool(prefs.get("mcp_website_actions") and prefs.get("mcp_actions_account", "default") == self.accounts.active_id)

    def need_permission(self):
        if not self.allowed():
            raise PermissionDenied("当前账户未开启 MCP 科研通操作权限，请在设置的 MCP 页中开启")

    def item(self, item_id):
        item = self.store.get(item_id) if isinstance(item_id, str) else None
        if not item or (item.get("source") != "demo" and item.get("account_id", "default") != self.accounts.active_id):
            raise ValueError("未找到该条目（或属于另一个账户）")
        return item

    def points(self):
        return self.store.snapshot().get("account_metrics", {}).get(self.accounts.active_id, {}).get("points")

    def pdf_path(self, item):
        if not item.get("pdf"):
            return None
        target = (self.store.root / item["pdf"]).resolve()
        return target if target.is_relative_to(self.store.root.resolve()) and target.is_file() else None

    def handle(self, name, body):
        method = getattr(self, "do_" + name.replace("-", "_"), None)
        if method is None or not isinstance(body, dict):
            raise ValueError("不支持该操作")
        return method(body)

    # ---- two-step confirmation ----
    def do_confirm_preview(self, body):
        chosen = intake.prepare_confirm(self.store, body.get("batch_id"), body.get("item_ids"), body.get("rewards"), self.points(), self.accounts.active_id)
        binding = ConfirmTokens.binding(body.get("batch_id"), self.accounts.active_id, [(i["id"], r) for i, r in chosen])
        token = self.tokens.issue(binding)
        rows = [dict(id=i["id"], title=i.get("title", ""), doi=i.get("doi", ""), reward=r, version=(i.get("oa_check") or {}).get("version") or "",
                     retracted=bool(intake.classify(i)["retracted"])) for i, r in chosen]
        return dict(batch_id=body["batch_id"], items=rows, estimated_points=sum(r for _, r in chosen), points_now=self.points(),
                    confirm_token=token, expires_in=self.tokens.ttl, permission=self.allowed(), session=bool(self.session()))

    def do_confirm(self, body):
        self.need_permission()
        if body.get("confirmed") is not True:
            raise ValueError("请先向用户展示预览，并在确认后带 confirmed=true")
        bound = self.tokens.consume(body.get("confirm_token"), body.get("batch_id"), self.accounts.active_id)
        ids = [iid for iid, _ in bound]
        if body.get("item_ids") is not None and sorted(body["item_ids"]) != sorted(ids):
            raise ValueError("条目与预览不一致；请重新预览")
        if isinstance(body.get("rewards"), dict) and any(dict(bound).get(k) != v for k, v in body["rewards"].items()):
            raise ValueError("悬赏与预览不一致；请重新预览")
        count = intake.confirm(self.store, body["batch_id"], ids, dict(bound), self.points(), self.session(), self.accounts.active_id)
        floors = self.store.snapshot().get("preferences", {}).get("mcp_min_balance_by_account") or {}
        floor = floors.get(self.accounts.active_id) if isinstance(floors, dict) else None
        for iid in ids:
            changes = dict(request_mcp_auto_publish=True)
            if isinstance(floor, int) and not isinstance(floor, bool) and 0 <= floor <= 5000:
                changes["request_mcp_min_balance"] = floor
            self.store.update(iid, **changes)
        return dict(ok=True, confirmed=count, estimated_points=sum(r for _, r in bound))

    def do_auto_continue(self, body):
        """Resume only an already explicitly confirmed request; no other queue item is authorised."""
        self.need_permission()
        item = self.item(body.get("id"))
        if (body.get("retry_unsubmitted") is True and stage_of(item) == "attention"
                and item.get("request_confirmed") is True and not item.get("publish_action")
                and not item.get("publish_check") and not item.get("assist_url") and not item.get("publish_blocked")):
            self.store.update(item["id"], status="ready", note="已恢复尚未提交的后台求助")
            item = self.store.get(item["id"])
        if item.get("request_confirmed") is not True or stage_of(item) not in ("queued", "submitted", "fulfilled", "awaiting_decision") or item.get("publish_blocked"):
            raise ValueError("仅已确认且未暂停的有效求助可继续后台处理")
        floor = body.get("min_balance")
        if floor is not None and (isinstance(floor, bool) or not isinstance(floor, int) or not 0 <= floor <= 5000):
            raise ValueError("最低余额须为 0–5000 的整数")
        changes = dict(request_mcp_auto_publish=True)
        if floor is not None:
            changes["request_mcp_min_balance"] = floor
            floors = self.store.snapshot().get("preferences", {}).get("mcp_min_balance_by_account") or {}
            floors = dict(floors) if isinstance(floors, dict) else {}
            floors[self.accounts.active_id] = floor
            self.store.update_preferences(mcp_min_balance_by_account=floors)
        self.store.update(item["id"], **changes)
        items = [i for i in self.store.snapshot().get("items", []) if i.get("account_id", "default") == self.accounts.active_id]
        blocked = gate(items, self.points(), item.get("request_reward"))
        return dict(ok=True, id=item["id"], stage=stage_of(item), gate=blocked)

    # ---- results ----
    def do_result(self, body):
        item = self.item(body.get("id"))
        path = self.pdf_path(item)
        v = item.get("verification") or {}
        b = v.get("bibliography") or {}
        retraction = v.get("retraction") or (item.get("metadata") or {}).get("retraction") or {}
        check = item.get("oa_check") or {}
        mine = [i for i in self.store.snapshot().get("items", []) if i.get("account_id", "default") == self.accounts.active_id]
        return dict(
            id=item["id"], title=item.get("title", ""), doi=item.get("doi", ""), stage=stage_of(item), has_pdf=path is not None,
            acquisition=acquisition_state(item, gate(mine, self.points(), item.get("request_reward"))),
            local_path=str(path) if path else "", original_filename=item.get("original_filename", ""), local_filename=item.get("local_filename", "") or (path.name if path else ""),
            size_bytes=path.stat().st_size if path else 0,
            verification=dict({k: v.get(k) for k in ("status", "reason", "title_score", "primary_dois", "pages")},
                              bibliography={k: b.get(k) for k in ("status", "sources", "title_status", "author_status", "abstract_status", "abstract_score", "matched_authors", "missing_authors")}),
            retraction=dict(retracted=retraction.get("retracted"), updates=(retraction.get("updates") or [])[:5], source=retraction.get("source")),
            version=check.get("version") or "", via="oa" if item.get("done_via") == "oa" else ("website" if item.get("source") == "website" else ""),
            external_assist=item.get("external_assist") or [])

    def do_render(self, body):
        item = self.item(body.get("id"))
        path = self.pdf_path(item)
        if path is None:
            raise ValueError("该条目没有 App 保存的 PDF")
        pages = body.get("pages") or [1]
        if not isinstance(pages, list) or not 0 < len(pages) <= RENDER_MAX_PAGE or any(isinstance(p, bool) or not isinstance(p, int) for p in pages):
            raise ValueError("pages 须为 1–%d 页的整数列表" % RENDER_MAX_PAGE)
        import pymupdf
        out_dir = self.store.root / "renders" / item["id"]
        out_dir.mkdir(parents=True, exist_ok=True)
        files = []
        try:
            with pymupdf.open(path) as doc:
                for number in dict.fromkeys(pages):
                    if not 1 <= number <= min(RENDER_MAX_PAGE, doc.page_count):
                        raise ValueError("只能渲染前 %d 页（该 PDF 共 %d 页）" % (RENDER_MAX_PAGE, doc.page_count))
                    rect = doc[number - 1].rect
                    zoom = min(1.6, 1400 / max(rect.width, rect.height, 1))
                    pix = doc[number - 1].get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
                    target = out_dir / ("page-%d.png" % number)
                    pix.save(target)
                    files.append(dict(page=number, path=str(target), width=pix.width, height=pix.height))
        except ValueError:
            raise
        except Exception:
            raise ValueError("此 PDF 暂无法渲染，请先人工检查文件") from None
        return dict(id=item["id"], pages=files, page_count_limit=RENDER_MAX_PAGE)

    def do_assist(self, body):
        item = self.item(body.get("id"))
        fields, evidence = body.get("fields"), body.get("evidence", "")
        if not isinstance(fields, dict) or not fields or len(fields) > 30:
            raise ValueError("fields 须为 1–30 项的对象")
        clean = {}
        for key, value in fields.items():
            if not isinstance(key, str) or not 0 < len(key) <= 60:
                raise ValueError("字段名无效")
            if isinstance(value, str):
                value = value[:1000]
            elif isinstance(value, list) and len(value) <= 50 and all(isinstance(v, str) for v in value):
                value = [v[:200] for v in value]
            elif not (value is None or isinstance(value, (bool, int, float))):
                raise ValueError("字段 %s 的值类型不支持（可用文本、数字、布尔或文本列表）" % key[:20])
            clean[key] = value
        if not isinstance(evidence, str) or len(evidence) > 4000:
            raise ValueError("evidence 须为 4000 字以内文本")
        entry = dict(time=now(), source="external_harness", status="unverified_assist", fields=clean, evidence=evidence,
                     doi_valid=bool(doi_value(str(clean.get("doi", "")))) if "doi" in clean else None)
        history = (item.get("external_assist") or [])[-(ASSIST_KEEP - 1):] + [entry]
        self.store.update(item["id"], external_assist=history)   # only this key: verification / rejection / status stay untouched
        self.store.event("已保存外部工具辅助结果（仅供参考，未更改核对结论或任何决定）：" + (item.get("title") or "")[:40], item_id=item["id"], kind="assist")
        return dict(ok=True, stored=True, count=len(history), note="外部辅助结果，不影响核对结论、拒绝、采纳或发布")

    # ---- open access ----
    def do_oa_lookup(self, body):
        doi = doi_value(body.get("doi", ""))
        if not doi:
            raise ValueError("DOI 格式不正确")
        prefs = self.store.snapshot().get("preferences", {})
        res = oa_fetch.lookup_only(doi, prefs.get("oa_contact_email", ""), prefs.get("core_api_key", ""), self.store.root)
        links = [dict(url=l.get("url"), source=l.get("source"), version=l.get("version"), license=l.get("license")) for l in res.get("links") or []]
        return dict(doi=doi, status=res["status"], oa_status=res.get("oa_status"), is_oa=res.get("is_oa"), license=res.get("license"),
                    version=res.get("version"), links=links, landing_url=res.get("landing_url"), cached=res.get("cached"),
                    tried=[str(a)[:120] if not isinstance(a, dict) else {k: str(v)[:80] for k, v in a.items()} for a in (res.get("attempts") or [])[-12:]])

    def do_oa_fetch(self, body):
        doi = doi_value(body.get("doi", ""))
        if not doi:
            raise ValueError("DOI 格式不正确")
        active = self.accounts.active_id
        item = next((i for i in self.store.snapshot()["items"] if i.get("source") == "import" and i.get("doi") == doi and i.get("account_id", "default") == active), None)
        if item and item.get("pdf"):
            return dict(started=False, reason="already_downloaded", id=item["id"])
        if item and (item.get("oa_check") or {}).get("status") == "downloaded":
            return dict(started=False, reason="already_downloaded", id=item["id"])
        if item and stage_of(item) != "queued":
            return dict(started=False, reason="already_in_request_flow", id=item["id"])
        item_id = item["id"] if item else self.store.add("", doi, account_id=active, import_format="oa", request_confirmed=False)
        if (self.store.root / "oa" / (item_id + ".pdf")).exists():
            return dict(started=False, reason="target_exists", id=item_id)
        self.pipeline.oa_start(item_id)
        return dict(started=True, id=item_id, next="wait_for(id, until='oa_finished') then get_result(id)")

    # ---- error report ----
    def do_error_report(self, body):
        snap = self.store.snapshot()
        root = self.store.root
        events = [e for e in snap.get("events") or [] if any(w in e.get("message", "") for w in FAIL_WORDS)][-15:]
        active = self.accounts.active_id
        problems = []
        for i in snap["items"]:
            if i.get("source") == "demo" or i.get("account_id", "default") != active:
                continue
            if stage_of(i) == "attention" or i.get("download_failure"):
                problems.append(dict(id=i["id"], stage=stage_of(i), title=redact(i.get("title"), root)[:80], note=redact(i.get("note"), root),
                                     download_failure=redact(i.get("download_failure"), root)))
        pub = self.monitor.public()
        return dict(redacted=True, generated=now(), report_markdown=applog.build_report(self.store, extra=[
                        "来自外部工具(MCP)的请求；以下为本机最近的问题条目"] + ["%s：%s" % (p["stage"], p["title"]) for p in problems[:15]]),
                    recent_errors=[dict(time=e.get("time"), module=e.get("module"), action=e.get("action"), error_type=e.get("error_type"), message=e.get("message"))
                                   for e in self.store.log.recent_errors(10)],
                    recent_problem_events=[dict(time=e.get("time"), message=redact(e.get("message"), root)) for e in events],
                    problem_items=problems[:15],
                    monitor=dict(enabled=pub["enabled"], failures=pub["failures"], login_required=pub["login_required"], last_error=redact(getattr(self.monitor, "last_error", ""), root)),
                    note="已去除路径、邮箱、链接参数和疑似密钥；不含凭据、Cookie 或 PDF 内容")
