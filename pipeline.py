"""Request pipeline: stage derivation, gate and next-step planning (pure) plus a small scheduler.
The scheduler only enqueues steps that need no user decision, one at a time, through the serial browser agent."""
import threading
import applog
from datetime import datetime

import oa_fetch
from core import now as clock
from metadata import verify_with_metadata, fetch_metadata
from ratelimit import PublishLimiter
from rejection_policy import rejection_decision

PIPELINE_RULES_VERSION = "2026-10-19-p1"

# Status strings of the 科研通 "我的求助" list (docs/ABLESCI_RULES_FINDINGS.md); any other non-empty status becomes
# stage "attention" (never guessed).
DONE = ("已完结", "已采纳", "已关闭", "关闭")
WAITING = ("求助中",)      # 科研通 "我的求助" filter: 全部/求助中/待确认/已完结/已关闭
FULFILLED = ("待确认",)
GATE_STAGES = ("fulfilled", "downloading", "awaiting_decision")
POINTS_BATCH = 500
GATE_TEXT = "需先处理已收到的应助，才能继续求助"


def _time(value):
    try:
        return datetime.fromisoformat(value) if value else None
    except (TypeError, ValueError):
        return None


def _later(a, b):
    """True if timestamp a is after b; unparsable values count as not later."""
    ta, tb = _time(a), _time(b)
    try:
        return bool(ta and tb and ta > tb)
    except TypeError:
        return False


def stage_of(item):
    if item.get("source") == "demo":
        return "demo"
    status, site = item.get("status"), (item.get("website_status") or "").strip()
    actions = [(item.get(k) or {}).get("status") for k in ("publish_action", "adopt_action", "reject_action")]
    published = actions[0] == "confirmed"
    if status == "attention" or any(a in ("attempted", "uncertain") for a in actions):
        return "attention"
    if site and site not in DONE + WAITING + FULFILLED:
        return "attention"
    if item.get("done_via") == "oa" and item.get("pdf"):
        return "done"
    if status == "oa_review" and item.get("pdf") and item.get("source") == "import":
        return "oa_review"   # open-access file whose identity check was not a full match: waits for the user, blocks nothing
    if status == "downloading":
        return "downloading"
    if actions[1] == "confirmed" or site in DONE:
        return "done"
    website = item.get("source") == "website" and item.get("owned") and item.get("assist_url")
    if item.get("pdf") and website and actions[2] != "confirmed":
        return "awaiting_decision"
    if item.get("rejected_at") and website:
        # After a confirmed rejection wait for a fresh sync before trusting an older "fulfilled" status.
        fresh = _later(item.get("website_status_time"), item["rejected_at"])
        return "fulfilled" if fresh and site in FULFILLED else "submitted"
    if site in FULFILLED:
        return "fulfilled"
    if site in WAITING or published:
        return "submitted"
    if item.get("assist_url") and not site:
        return "attention"   # synced request without a readable status
    return "queued"


def retry_waiting(item, when):
    """True while a download is in automatic retry and its next attempt is not due yet (it blocks nothing else)."""
    if item.get("status") != "retrying":
        return False
    at = _time((item.get("download_retry") or {}).get("next_at"))
    try:
        return bool(at and when and at > when)
    except TypeError:
        return False


def retry_due_items(items, when):
    return [i for i in sorted(items, key=lambda i: i.get("created", "")) if i.get("status") == "retrying"
            and stage_of(i) == "fulfilled" and not retry_waiting(i, when)]


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def projected_points(points, reward=None):
    """Balance right after publishing a request with this reward (the reward is deducted at publish time).
    Unknown points stay None; an unknown/invalid reward subtracts nothing (callers pass the real one when planning a publish)."""
    if not _is_int(points):
        return None
    return points - reward if _is_int(reward) and reward > 0 else points


def tier(points, reward=None):
    """'batch' (back-to-back submissions allowed) only when the balance AFTER this publish is still >= 500."""
    after = projected_points(points, reward)
    return "single" if after is None or after < POINTS_BATCH else "batch"


def item_reward(item, prefs, points):
    import intake
    return item.get("request_reward") or intake.default_reward(prefs, points)


def gate(items, points, reward=None):
    if tier(points, reward) == "batch":
        return dict(blocked=False, item_id=None, reason="")
    for item in items:
        stage = stage_of(item)
        # An answer that arrived but is unusable sits in 'attention'; 科研通 still wants it handled first.
        answered = (stage == "attention" and item.get("source") == "website" and item.get("website_status") not in DONE and (
            item.get("website_status") in FULFILLED or item.get("pdf") or item.get("download_file_url") or item.get("download_attempts")))
        if stage in GATE_STAGES or answered:
            return dict(blocked=True, item_id=item["id"],
                        reason=GATE_TEXT + "（请先处理《" + item.get("title", "")[:40] + "》）")
    return dict(blocked=False, item_id=None, reason="")


def acquisition_state(item, blocked_gate=None):
    """Describe progress without treating an OA failure or site block as final acquisition failure.

    A closed site record alone does not identify whether it expired unanswered;
    that remains unresolved until the site's outcome is verified.
    """
    stage = stage_of(item)
    status = (item.get("website_status") or "").strip()
    if item.get("pdf"):
        if stage == "oa_review" or (item.get("verification") or {}).get("status") not in (None, "identity_match"):
            return dict(code="file_needs_review", label="文件待核对")
        return dict(code="file_available", label="已获得文件")
    if status in ("已关闭", "关闭"):
        return dict(code="closed_unverified", label="求助已关闭，结果待核实")
    if stage == "done":
        return dict(code="completed_without_file", label="求助已完结，文件未取得")
    if stage in ("fulfilled", "downloading", "awaiting_decision"):
        return dict(code="answer_available", label="已收到应助，待下载或处理")
    if stage == "submitted":
        return dict(code="waiting_answer", label="求助中，等待应助")
    if stage == "attention":
        return dict(code="needs_review", label="状态需核实")
    if item.get("request_confirmed") is True and (blocked_gate or {}).get("blocked"):
        return dict(code="blocked", label="等待处理既有应助后继续求助", blocking_item_id=blocked_gate.get("item_id"))
    if item.get("request_confirmed") is True:
        return dict(code="waiting_to_publish", label="已确认求助，等待发布")
    if (item.get("oa_check") or {}).get("status") in ("not_oa", "failed", "needs_human", "skipped"):
        return dict(code="oa_unavailable", label="免费全文未取得，可继续科研通求助")
    return dict(code="pending", label="待处理")


def step(action, item=None, label="", requires_user=False, reason=""):
    return dict(action=action, item_id=item["id"] if item else None, label=label, requires_user=requires_user, reason=reason)


def _title(item):
    return "《" + (item.get("title") or item.get("doi") or "?")[:40] + "》"


def rules_ok(prefs, key):
    return bool(prefs.get(key)) and prefs.get("pipeline_rules_version") == PIPELINE_RULES_VERSION


def _auto_adopt_ok(item, prefs):
    v = item.get("verification") or {}
    return (rules_ok(prefs, "pipeline_auto_adopt") and v.get("status") == "identity_match" and bool(v.get("sha256"))
            and (v.get("bibliography") or {}).get("status") == "match" and not item.get("adopt_action")
            and rejection_decision(item, prefs)["eligible"])


NO_SESSION_NOTE = "无免费全文。登录科研通后可求助"


def has_session(account, items, profile_cookie=False):
    """Best-effort evidence of a 科研通 login (no network call): a known balance, a past sync/check-in, a successful
    login action, an existing cookie database in the account's browser profile, or items synced from the site."""
    account = account or {}
    return (isinstance(account.get("points"), int) or bool(account.get("last_sync_time")) or bool(account.get("last_checkin_result"))
            or bool(account.get("last_login_time")) or bool(profile_cookie)
            or any(i.get("source") == "website" for i in items))


def oa_pending(item, prefs):
    """Local open-access lookup still to do for this item (public APIs only; no 科研通 session, no points)."""
    return (prefs.get("oa_first", True) and stage_of(item) == "queued" and item.get("source") == "import"
            and bool(item.get("doi")) and not item.get("oa_check") and not item.get("pdf"))


def next_local_step(items, prefs):
    """Local steps are independent of every 科研通-side wait (attention, awaiting decision, downloading, gate)."""
    prefs = prefs or {}
    for item in sorted((i for i in items if i.get("source") != "demo"), key=lambda i: i.get("created", "")):
        if oa_pending(item, prefs):
            return step("oa_fetch", item, "查找免费全文 PDF", False, "先查找合法的免费全文，不消耗积分：" + _title(item))
    return None


def next_step(items, account, prefs, now, session=True, local=True):
    """Pure planner. items: this account's items; account: metrics dict (points, last_sync_time); now: datetime.
    session=False (no 科研通 login evidence): only local open-access work is planned; site steps become wait_user.
    local=True also plans the local oa_fetch step (compat / UI); the scheduler passes local=False and evaluates
    next_local_step independently, while items still awaiting their OA lookup are never prepared or published."""
    items = sorted((i for i in items if i.get("source") != "demo"), key=lambda i: i.get("created", ""))
    stages = [(i, stage_of(i)) for i in items]
    of = lambda name: [i for i, s in stages if s == name]
    account, prefs = account or {}, prefs or {}
    points = account.get("points")
    # An item in attention blocks only itself and publishing; downloads, sync and decisions for the others carry on.
    bad = of("attention")
    attention_step = (lambda: step("wait_user", bad[0], "需处理", True, _title(bad[0]) + "出错或状态无法识别：" + str(bad[0].get("note") or bad[0].get("website_status") or "请在科研通查看")[:120])) if bad else None
    busy = of("downloading")
    if busy:
        return step("wait", busy[0], "等待下载完成", False, _title(busy[0]) + "下载中")
    for item in of("awaiting_decision"):
        if rejection_decision(item, prefs)["action"] == "auto_reject":
            return step("reject_auto", item, "自动驳回明确不符的应助", False, "首页 DOI 与标题均明确不符")
        if _auto_adopt_ok(item, prefs):
            return step("adopt", item, "自动采纳完全匹配的应助", False, "DOI、标题、作者与摘要均匹配")
    pending = of("awaiting_decision")
    ready = [i for i in of("fulfilled") if not retry_waiting(i, now)] if session else []
    if ready:
        return step("download", ready[0], "下载应助文件", False, _title(ready[0]) + "已有应助文件")
    if pending:
        return step("wait_user", pending[0], "待采纳或驳回", True, "请采纳或驳回" + _title(pending[0]) + "，才能继续受限制的求助")
    if local:
        # The actual scheduler also runs this independently while site actions are blocked.
        found = next_local_step(items, prefs)
        if found:
            return found
    awaiting_oa = {i["id"] for i in of("queued") if oa_pending(i, prefs)}
    if not session:
        queued_now = [i for i in of("queued") if i["id"] not in awaiting_oa]
        waiting_site = of("fulfilled") + of("submitted") + [i for i in queued_now if (i.get("oa_check") or {}).get("status") != "needs_human" and i.get("request_confirmed") is True]
        if waiting_site:
            return step("wait_user", waiting_site[0], "未登录科研通（可选）", True,
                        NO_SESSION_NOTE if waiting_site[0] in queued_now else "未检测到科研通登录状态，登录并同步后继续")
    if of("submitted"):
        try:
            minutes = int(prefs.get("pipeline_sync_minutes", 10))
        except (TypeError, ValueError):
            minutes = 10
        last = _time(account.get("last_sync_time"))
        try:
            due = last is None or (now - last).total_seconds() >= minutes * 60
        except TypeError:
            due = True
        if due:
            return step("sync", None, "同步求助状态", False, "距上次同步已超过 %d 分钟" % minutes)
    if bad:
        return attention_step()
    held = [i for i in of("queued") if (i.get("oa_check") or {}).get("status") == "needs_human" and i.get("request_confirmed") is not True]
    # publish_blocked: an earlier publish attempt stopped on a check and the person took it over -> never auto-published again
    queued = [i for i in of("queued") if i not in held and i["id"] not in awaiting_oa and i.get("request_confirmed") is True and not i.get("publish_blocked")]
    unconfirmed = [i for i in of("queued") if i.get("request_confirmed") is not True and i.get("batch_id") and i["id"] not in awaiting_oa]
    if not queued and held:
        return step("wait_user", held[0], "需人工验证", True, "免费全文页面需人工验证：请打开验证窗口，或改走科研通 " + _title(held[0]))
    if queued:
        item = queued[0]
        reward = item_reward(item, prefs, points)
        hold = _time(account.get("site_gate"))
        if hold:
            synced = _time(account.get("last_sync_time"))
            try:
                fresh = bool(synced and synced > hold)
            except TypeError:
                fresh = False
            if not fresh:   # the site itself said an answered request must be handled first; re-check after the next sync
                if of("submitted") or of("fulfilled"):
                    return step("sync", None, "同步求助状态", False, "科研通提示需先处理已收到的应助，先同步最新状态")
                return step("wait_user", item, "需先处理已收到的应助", True, GATE_TEXT + "（科研通已拒绝本次提交，请在科研通处理后同步）")
        if gate(items, points, reward)["blocked"]:
            return step("wait_user", item, "需先处理已收到的应助", True, gate(items, points, reward)["reason"])
        if not rules_ok(prefs, "pipeline_auto_publish"):
            if item.get("status") == "prepared":
                return step("wait_user", item, "等待发布", True, "表单已填写，请在求助页面确认并发布" + _title(item))
            return step("prepare", item, "填写求助表单（不发布）", True, "自动求助未开启，表单填写后由用户自行发布")
        return step("publish", item, "发布求助", False, ("发布后积分将低于 500，按逐条求助规则处理" if tier(points, reward) == "single" else "发布后积分仍不低于 500，可连续提交") + "：" + _title(item))
    if unconfirmed:
        return step("wait_user", unconfirmed[0], "待确认求助清单", True, "确认清单中勾选并确认后，才会向科研通求助：" + _title(unconfirmed[0]))
    return step("idle", None, "暂无待办", False, "")


class Pipeline:
    """Daemon scheduler. Enabled flag lives in preferences but is always reset to False on construction."""
    INTERVAL = 30
    PASSIVE = {"wait", "wait_user", "idle"}

    def __init__(self, store, accounts, agent, interval=None, start=True, publish_limiter=None):
        self.store, self.accounts, self.agent = store, accounts, agent
        self.publish_limiter = publish_limiter or PublishLimiter.for_store(store)
        self.interval = interval or self.INTERVAL
        self.last_tick = None
        self.oa_running = None
        self.oa_thread = None
        self._oa_lock = threading.Lock()
        self.stopped = threading.Event()
        was_on = bool(store.snapshot().get("preferences", {}).get("pipeline_enabled"))
        store.update_preferences(pipeline_enabled=False)
        if was_on:   # the switch is reset on every start; tell the person once that it was on before the restart
            store.update_preferences(pipeline_restart_notice=True)
        self.thread = threading.Thread(target=self.run, daemon=True)
        if start:
            self.thread.start()

    def enabled(self):
        return bool(self.store.snapshot().get("preferences", {}).get("pipeline_enabled"))

    def run(self):
        while not self.stopped.wait(self.interval):
            self.watchdog()
            try:
                self.screen_step()
                self.tick()
                self.tick_mcp_authorized()
            except Exception as exc:
                applog.record(self.store, "pipeline", "tick", exc)

    def tick_mcp_authorized(self, when=None):
        """Continue only explicitly confirmed MCP requests, even if global auto mode is off."""
        if self.agent.busy or not self.agent.commands.empty():
            return None
        items, account, prefs, session = self._view()
        if not prefs.get("mcp_website_actions") or prefs.get("mcp_actions_account", "default") != self.accounts.active_id:
            return None
        targets = [i for i in items if i.get("request_mcp_auto_publish") is True and i.get("request_confirmed") is True
                   and stage_of(i) in ("queued", "submitted", "fulfilled", "awaiting_decision")]
        if not targets:
            return None
        settings = dict(prefs, pipeline_auto_publish=True, pipeline_rules_version=PIPELINE_RULES_VERSION,
                        pipeline_auto_adopt=True, rejection_mode="manual")
        for target in sorted(targets, key=lambda i: i.get("created", "")):
            # Other site items remain visible to the points gate but cannot be acted on by this scheduler.
            relevant = [i for i in items if stage_of(i) != "queued" or i["id"] == target["id"]]
            planned = next_step(relevant, account, settings, when or datetime.now().astimezone(), session=session, local=False)
            action = planned.get("action")
            if action in ("publish", "download", "adopt") and planned.get("item_id") == target["id"]:
                floor = target.get("request_mcp_min_balance")
                points = account.get("points")
                if action in ("publish", "download") and isinstance(floor, int) and (not isinstance(points, int) or points < floor):
                    continue
                return planned if self.execute(planned) else None
            if action == "sync" and stage_of(target) == "submitted":
                return planned if self.execute(planned) else None
        return None

    def close(self):
        self.stopped.set()

    def watchdog(self):
        """Restart the browser worker thread if it died (it logs and records an event itself)."""
        try:
            revive = getattr(self.agent, "revive", None)
            return bool(revive and revive() is True)
        except Exception as exc:
            applog.record(self.store, "pipeline", "watchdog", exc)
            return False

    def _view(self):
        snapshot = self.store.snapshot()
        active = self.accounts.active_id
        items = [i for i in snapshot["items"] if i.get("source") != "demo" and i.get("account_id", "default") == active]
        account = snapshot.get("account_metrics", {}).get(active, {})
        return items, account, snapshot.get("preferences", {}), has_session(account, items, self.accounts.has_cookie_file())

    def plan(self, when=None, local=True):
        items, account, prefs, session = self._view()
        return items, account, prefs, next_step(items, account, prefs, when or datetime.now().astimezone(), session=session, local=local)

    def local_plan(self):
        items, _, prefs, _ = self._view()
        return next_local_step(items, prefs)

    # ---- open-access path: runs in its own worker thread, never in the Playwright BrowserAgent thread ----
    def oa_start(self, item_id, human=False, **options):
        with self._oa_lock:
            if self.oa_running:
                raise ValueError("已有免费全文任务在运行，请稍后再试")
            self.oa_running = item_id
        self.oa_thread = threading.Thread(target=self._oa_worker, args=(item_id, human, options), daemon=True)
        self.oa_thread.start()

    def _oa_worker(self, item_id, human, options=None):
        try:
            self.oa_run(item_id, human, **(options or {}))
        except Exception as exc:
            applog.record(self.store, "oa", "worker", exc, item_id=item_id)
        finally:
            self.oa_running = None
            try:
                self.screen_step()
            except Exception:
                pass

    def oa_run(self, item_id, human=False, lookup_only=False, only_url=None):
        store = self.store
        item = store.get(item_id)
        if not item:
            return
        prefs = store.snapshot().get("preferences", {})
        email, core_key = prefs.get("oa_contact_email", ""), prefs.get("core_api_key", "")
        item = self.fill_from_metadata(item_id, email) or item
        dest = store.root / "oa" / (item_id + ".pdf")
        dest.parent.mkdir(exist_ok=True)
        profile = store.root / "oa-browser-profile"
        old = item.get("oa_check") or {}
        try:
            if human:
                res = oa_fetch.human_fetch(old.get("manual_url"), dest, profile)
                res = dict(old, status=res["status"], source=res.get("source"), attempts=(old.get("attempts") or []) + res["attempts"])
            elif lookup_only:
                res = oa_fetch.lookup_only(item["doi"], email, core_api_key=core_key, cache_root=store.root)
            else:
                extra = {}
                if only_url:
                    chosen = [l for l in old.get("links") or [] if l.get("url") == only_url]
                    if not chosen:
                        raise ValueError("该链接不在已找到的免费全文来源中")
                    extra["candidates"] = chosen
                res = oa_fetch.fetch_open_access(item["doi"], dest, email, challenge_mode=prefs.get("oa_challenge_mode", "prompt"),
                                                 profile_dir=profile, core_api_key=core_key, cache_root=store.root, **extra)
        except Exception as exc:
            applog.record(self.store, "oa", "human_fetch" if human else "fetch", exc, item_id=item_id)
            res = dict(old, status="needs_human" if human else "failed", attempts=old.get("attempts") or [])
        check = dict(status=res["status"], oa_status=res.get("oa_status"), is_oa=res.get("is_oa"), license=res.get("license"),
                     source=res.get("source"), attempts=(res.get("attempts") or [])[-40:], manual_url=res.get("manual_url"), time=clock(),
                     links=res.get("links") or old.get("links") or [], landing_url=res.get("landing_url") or old.get("landing_url"),
                     version=res.get("version") or old.get("version"), cached=bool(res.get("cached")), fetched_at=res.get("fetched_at") or old.get("fetched_at"))
        title = (item.get("title") or item.get("doi") or "")[:40]
        if res["status"] != "downloaded":
            fields = dict(oa_check=check)
            snapshot = store.snapshot()
            mine = [i for i in snapshot["items"] if i.get("account_id", "default") == self.accounts.active_id]
            if res["status"] in ("not_oa", "failed") and not has_session(snapshot.get("account_metrics", {}).get(self.accounts.active_id, {}), mine, self.accounts.has_cookie_file()):
                fields["note"] = NO_SESSION_NOTE if res["status"] == "not_oa" else "免费全文下载未成功。登录科研通后可求助"
            store.update(item_id, **fields)
            store.event({"not_oa": "未找到免费全文，将向科研通求助：", "needs_human": "免费全文页面需人工验证：", "found": "已找到免费全文链接：",
                         "failed": "免费全文下载未成功，将向科研通求助："}.get(res["status"], "免费全文结果：") + title, item_id=item_id, kind="oa", action="oa-human" if res["status"] == "needs_human" else "")
            return
        try:
            verification = verify_with_metadata(dest, item.get("expected_title", item["title"]), item.get("expected_doi", item["doi"]), store.root, email=email)
        except Exception as exc:
            applog.record(self.store, "oa", "verify", exc, item_id=item_id)
            verification = dict(status="invalid", reason="免费全文 PDF 无法完成本地核对，请手动检查")
        rel = str(dest.relative_to(store.root))
        if verification.get("status") == "identity_match":
            store.update(item_id, pdf=rel, verification=verification, oa_check=check, done_via="oa", status="checked")
            store.event("已下载并核对免费全文（未消耗积分）：" + title, item_id=item_id, kind="oa")
        else:
            # Non-blocking "oa_review": the user keeps the file as done, or sends the item to 科研通 (file stays on disk).
            store.update(item_id, pdf=rel, verification=verification, oa_check=check, status="oa_review",
                         note="免费全文 PDF 已下载，但核对未完全匹配。请在待审核中选择“保留为完成”，或改走科研通")
            store.event("免费全文 PDF 已下载，但核对未完全匹配，待审核（不影响其他条目）：" + title, item_id=item_id, kind="oa", action="focus")

    def fill_from_metadata(self, item_id, email=""):
        """DOI-only imports get their title/authors from OpenAlex/Crossref before verification (and a trimmed public record for
        later title comparison). Public lookup only: the DOI is the only thing sent. Returns the refreshed item or None."""
        item = self.store.get(item_id)
        if not item or not item.get("doi") or (item.get("title") or "").strip():
            return None
        try:
            record = fetch_metadata(item["doi"], self.store.root, email)
        except Exception:
            return None
        if not isinstance(record, dict) or record.get("status") != "available" or not record.get("title"):
            return None
        changes = dict(title=str(record["title"]).strip()[:1000], title_from_metadata=True,
                       metadata_ref=dict(title=record["title"], authors=record.get("authors", [])[:50], year=record.get("year"),
                                         sources=record.get("sources", []), retraction=record.get("retraction"), fetched_at=record.get("fetched_at", "")))
        if not item.get("authors") and record.get("authors"):
            changes["authors"] = record["authors"][:300]
        self.store.update(item_id, **changes)
        self.store.event("已按 DOI 从公开书目补全标题：" + changes["title"][:60], item_id=item_id, kind="oa")
        return self.store.get(item_id)

    # ---- intake batches: automatic free-source screening, one item at a time ----
    def screen_step(self):
        """Start the next screening lookup of the oldest unscreened batch; finish the batch when none is left."""
        import intake
        with self._oa_lock:
            if self.oa_running:
                return None
        prefs = self.store.snapshot().get("preferences", {})
        target = intake.next_screening(self.store, prefs.get("oa_first", True) is not False, getattr(self, "notifier", None))
        if target:
            try:
                self.oa_start(target)
            except ValueError:
                return None
        return target

    def oa_skip(self, item_id):
        item = self.store.get(item_id)
        old = (item or {}).get("oa_check") or {}
        if old.get("status") != "needs_human":
            raise ValueError("该条目没有待人工验证的免费全文任务")
        self.store.update(item_id, oa_check=dict(old, status="skipped", time=clock()))
        self.store.event("已放弃人工验证，改走科研通：" + (item.get("title") or "")[:40])

    def _oa_review_item(self, item_id):
        item = self.store.get(item_id)
        if not item or stage_of(item) != "oa_review":
            raise ValueError("该条目没有待审核的免费全文文件")
        return item

    def oa_accept(self, item_id):
        """User confirms the open-access file as the answer: the item becomes done (via oa)."""
        item = self._oa_review_item(item_id)
        self.store.update(item_id, done_via="oa", status="checked", note="已确认保留免费全文文件")
        self.store.event("已确认保留免费全文文件为完成：" + (item.get("title") or "")[:40])

    def oa_discard(self, item_id):
        """User sends the item to 科研通: the file stays on disk but is listed in oa_rejected_files."""
        item = self._oa_review_item(item_id)
        old = item.get("oa_check") or {}
        archived = dict(pdf=item["pdf"], verification=item.get("verification"), source=old.get("source"), time=clock())
        self.store.update(item_id, pdf="", verification=None, status="ready", note="",
                          oa_check=dict(old, status="skipped", time=clock()),
                          oa_rejected_files=list(item.get("oa_rejected_files", [])) + [archived])
        self.store.event("未采用免费全文文件（文件保留在本机），改向科研通求助：" + (item.get("title") or "")[:40])

    def execute(self, planned):
        if planned["requires_user"] or planned["action"] in self.PASSIVE:
            return False
        if planned["action"] in ("publish", "prepare"):
            target = self.store.get(planned["item_id"])
            if not target or target.get("request_confirmed") is not True:
                return False   # never touch 科研通 for an item the user has not confirmed
        if planned["action"] == "oa_fetch":
            self.oa_start(planned["item_id"])
            return True
        if planned["action"] == "publish" and self.publish_limiter.wait_seconds() > 0:
            return False   # publish spacing (>= 60 s) or the hourly cap: the pipeline just waits
        self.agent.enqueue(planned["action"], planned["item_id"])
        if planned["action"] == "publish":
            self.publish_limiter.record()
        return True

    def tick(self, when=None):
        """One scheduling pass; returns the step that was enqueued, else None.
        The local open-access step is evaluated on its own every tick: no 科研通-side wait (attention, awaiting
        decision, downloading) or busy browser holds it back. It still runs single-concurrency, in its own thread."""
        if not self.enabled():
            return None
        self.last_tick = clock()
        started = None
        local = None if self.oa_running else self.local_plan()
        if local:
            try:
                self.oa_start(local["item_id"])
                started = local
            except ValueError:
                pass
        if self.agent.busy or not self.agent.commands.empty():
            return started
        planned = self.plan(when, local=False)[3]
        try:
            return planned if self.execute(planned) else started
        except ValueError:
            return started
