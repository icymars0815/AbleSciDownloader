"""Background monitor: read-only sync of the own-requests list on an adaptive schedule, change detection, notifications.
Only calls agent.enqueue("sync") (plus "close_idle" to release the browser); it never publishes, adopts, rejects or spends points.
The pipeline switch is independent: the monitor only hands a newly fulfilled item over when the pipeline is already on."""
import threading
import applog
import time
from datetime import datetime

from browser_agent import SessionExpired
from notifications import Notifier
from pipeline import FULFILLED, _auto_adopt_ok, has_session, stage_of, retry_due_items

MONITOR_DEFAULTS = dict(enabled=False, hot_enabled=True, hot_fast_seconds=30, hot_fast_min=5, normal_min=10)
MONITOR_RANGES = dict(hot_fast_seconds=(20, 120), hot_fast_min=(1, 15), normal_min=(10, 60))
HOT_MID_END_MIN = 15     # fast phase (configurable length) -> 1-minute phase until 15 min
HOT_MID_INTERVAL_SEC = 60
WARM_END_MIN = 60        # 15–60 min after publishing
WARM_INTERVAL_MIN = 3
IDLE_MIN = 30            # nothing is waiting: only watch for account changes
BACKOFF_CAP_MIN = 60
IDLE_CLOSE_SECONDS = 300
RESUME_DELAY = 60        # after sleep/resume wait a minute for the network
JUMP_SECONDS = 90        # a poll gap this large means the computer slept
POLL_SECONDS = 5
START_DELAY = 30
CLOSED = ("已关闭", "关闭")
FAIL_NOTIFY_AFTER = 3


def monitor_settings(prefs):
    prefs = prefs or {}
    out = dict(MONITOR_DEFAULTS)
    for key in ("enabled", "hot_enabled"):
        if isinstance(prefs.get("monitor_" + key), bool):
            out[key] = prefs["monitor_" + key]
    for key, (low, high) in MONITOR_RANGES.items():
        value = prefs.get("monitor_" + key)
        if isinstance(value, int) and not isinstance(value, bool):
            out[key] = min(high, max(low, value))
    return out


def validate_monitor(body):
    allowed = {"enabled", "hot_enabled", "hot_fast_seconds", "hot_fast_min", "normal_min", "sync_now"}
    if not isinstance(body, dict) or not body or set(body) - allowed:
        raise ValueError("后台监控设置无效")
    changes = {}
    for key in ("enabled", "hot_enabled", "sync_now"):
        if key in body and not isinstance(body[key], bool):
            raise ValueError("监控开关参数无效")
    for key, (low, high) in MONITOR_RANGES.items():
        if key in body:
            value = body[key]
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise ValueError("%s 须为 %d–%d 的整数" % (key, low, high))
    for key in ("enabled", "hot_enabled", *MONITOR_RANGES):
        if key in body:
            changes["monitor_" + key] = body[key]
    return changes


def _epoch(value):
    try:
        return datetime.fromisoformat(value).timestamp() if value else None
    except (TypeError, ValueError):
        return None


def waiting_age(item, now):
    """Seconds since a waiting request was published (confirmed publish time) or first seen by a sync; None if unknown."""
    action = item.get("publish_action") or {}
    stamp = _epoch(action.get("time")) if action.get("status") == "confirmed" else None
    if stamp is None:
        stamp = _epoch(item.get("first_seen"))
    return None if stamp is None else max(0.0, now - stamp)


def choose_interval(items, prefs, now, failures=0):
    """Pick the sync interval. Returns dict(seconds, band, reason). One request regardless of how many items are hot."""
    cfg = monitor_settings(prefs)
    waiting = [i for i in items if i.get("source") == "website" and stage_of(i) == "submitted"]
    normal = cfg["normal_min"] * 60
    backoff = lambda base: min(BACKOFF_CAP_MIN * 60, base * (2 ** min(failures, 6)))
    if not waiting:
        return dict(seconds=backoff(IDLE_MIN * 60), band="idle", reason="无等待应助的求助")
    ages = [a for a in (waiting_age(i, now) for i in waiting) if a is not None]
    if cfg["hot_enabled"] and ages:
        age = min(ages)   # the youngest waiting request decides; an item that left WAITING no longer counts
        fast_end = cfg["hot_fast_min"] * 60
        if age < HOT_MID_END_MIN * 60:
            if failures:   # no rapid retries inside the hot window after a failure
                return dict(seconds=WARM_INTERVAL_MIN * 60, band="hot", reason="同步失败，暂按 %d 分钟间隔重试" % WARM_INTERVAL_MIN)
            if age < fast_end:
                return dict(seconds=cfg["hot_fast_seconds"], band="hot", reason="求助发布未满 %d 分钟" % cfg["hot_fast_min"])
            return dict(seconds=HOT_MID_INTERVAL_SEC, band="hot", reason="有求助发布 %d–%d 分钟" % (cfg["hot_fast_min"], HOT_MID_END_MIN))
        if age < WARM_END_MIN * 60:
            return dict(seconds=WARM_INTERVAL_MIN * 60, band="warm", reason="有求助发布 %d–%d 分钟" % (HOT_MID_END_MIN, WARM_END_MIN))
    return dict(seconds=backoff(normal), band="normal", reason="等待应助的求助发布较早" if ages else "等待应助")


class Monitor:
    def __init__(self, store, accounts, agent, pipeline=None, notifier=None, clock=time.time, start=True, poll=POLL_SECONDS):
        self.store, self.accounts, self.agent, self.pipeline = store, accounts, agent, pipeline
        self.notifier = notifier or Notifier(store)
        self.clock = clock
        self.poll = poll
        self.lock = threading.RLock()
        with store.lock:
            self.state = store.data.setdefault("monitor", {})
        self.state.setdefault("known", {})
        self.failures = int(self.state.get("failures") or 0)
        self.login_required = bool(self.state.get("login_required"))
        self.last_sync = self.state.get("last_sync")
        self.last_error = ""
        self.inflight = False
        self.last_poll = None
        now = clock()
        self.next_due = now + START_DELAY
        if self.last_sync:
            self.next_due = max(now + START_DELAY, self.last_sync + self.interval(now)["seconds"])
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)
        agent.on_done = self.on_done
        if start:
            self.thread.start()

    # ---- views ----
    def prefs(self):
        return self.store.snapshot().get("preferences", {})

    def settings(self):
        return monitor_settings(self.prefs())

    def enabled(self):
        return self.settings()["enabled"]

    def items(self, snapshot=None):
        snapshot = snapshot or self.store.snapshot()
        active = self.accounts.active_id
        return [i for i in snapshot["items"] if i.get("source") != "demo" and i.get("account_id", "default") == active]

    def interval(self, now=None):
        return choose_interval(self.items(), self.prefs(), now or self.clock(), self.failures)

    def has_session(self, snapshot=None):
        snapshot = snapshot or self.store.snapshot()
        items = self.items(snapshot)
        account = snapshot.get("account_metrics", {}).get(self.accounts.active_id, {})
        return has_session(account, items, self.accounts.has_cookie_file())

    def pending_count(self, items=None):
        count = 0
        for i in items if items is not None else self.items():
            if stage_of(i) in ("attention", "awaiting_decision", "oa_review") or (i.get("oa_check") or {}).get("status") == "needs_human":
                count += 1
        return count

    def label(self):
        if self.login_required:
            return "需登录"
        if not self.enabled():
            return "已暂停"
        pending = self.pending_count()
        return "待处理 %d 条" % pending if pending else "监控中"

    def public(self):
        snapshot = self.store.snapshot()
        cfg = monitor_settings(snapshot.get("preferences", {}))
        items = self.items(snapshot)
        now = self.clock()
        choice = choose_interval(items, snapshot.get("preferences", {}), now, self.failures)
        due = self.next_due if cfg["enabled"] and not self.login_required else None
        iso = lambda t: datetime.fromtimestamp(t).astimezone().isoformat(timespec="seconds") if t else None
        return dict(enabled=cfg["enabled"], label=self.label(), login_required=self.login_required,
                    session=self.has_session(snapshot), pending=self.pending_count(items),
                    next_sync_at=iso(due), last_sync_at=iso(self.last_sync),
                    interval_seconds=choice["seconds"], band=choice["band"], reason=choice["reason"], failures=self.failures,
                    inflight=self.inflight, settings={k: v for k, v in cfg.items() if k != "enabled"},
                    ranges={k: list(v) for k, v in MONITOR_RANGES.items()},
                    defaults={k: v for k, v in MONITOR_DEFAULTS.items() if k != "enabled"},
                    browser_open=bool(getattr(self.agent, "context", None)))

    # ---- control ----
    def set_enabled(self, value):
        self.store.update_preferences(monitor_enabled=bool(value))
        with self.lock:
            if value:
                self.next_due = min(self.next_due, self.clock() + 2)
        self.store.event("后台监控已" + ("开启，仅读取求助列表。" if value else "暂停。"))

    def toggle(self):
        self.set_enabled(not self.enabled())

    def reschedule(self):
        """Settings changed: recompute the next sync from the last one."""
        with self.lock:
            now = self.clock()
            base = self.last_sync or now
            self.next_due = max(now + 1, base + self.interval(now)["seconds"])

    def sync_now(self):
        """Immediate read-only sync (tray / button). Raises ValueError when the browser is busy."""
        self.agent.enqueue("sync")
        self.inflight = True

    def close(self):
        self.stopped.set()

    # ---- loop ----
    def run(self):
        while not self.stopped.wait(self.poll):
            try:
                self.tick()
            except Exception as exc:
                applog.record(self.store, "monitor", "tick", exc)

    def tick(self, now=None):
        now = self.clock() if now is None else now
        with self.lock:
            if self.last_poll is not None and now - self.last_poll > JUMP_SECONDS:   # slept: let the network come back
                self.next_due = max(self.next_due, now + RESUME_DELAY)
            self.last_poll = now
        self.notifier.flush()
        try:
            revive = getattr(self.agent, "revive", None)
            if revive:
                revive()
        except Exception as exc:
            applog.record(self.store, "monitor", "watchdog", exc)
        try:
            import deletion
            deletion.sweep(self.store, self.notifier, datetime.fromtimestamp(now).astimezone())
        except Exception:
            pass
        self.close_idle_browser()
        if not self.enabled():
            return None
        self.scan(self.items())
        if self.retry_downloads(now):
            return "download"
        if self.login_required or self.inflight or self.agent_busy() or now < self.next_due:
            return None
        if not self.has_session():
            return None
        try:
            self.agent.enqueue("sync")
        except ValueError:
            return None
        self.inflight = True
        return "sync"

    def retry_downloads(self, now):
        """Start one due automatic download retry (never two at once: enqueue refuses while the agent is busy;
        the 科研通 request pacing is applied by the agent's own limiter). Returns True when one was enqueued."""
        if self.login_required or self.inflight or self.agent_busy():
            return False
        for item in retry_due_items(self.items(), datetime.fromtimestamp(now).astimezone()):
            try:
                self.agent.enqueue("download", item["id"])
                return True
            except ValueError:
                return False
        return False

    def agent_busy(self):
        return bool(self.agent.busy or not self.agent.commands.empty())

    def close_idle_browser(self):
        agent = self.agent
        if not agent.has_browser() or self.agent_busy() or getattr(agent, "interactive", False):
            return False
        if agent.clock() - agent.last_activity < IDLE_CLOSE_SECONDS:
            return False
        try:
            agent.enqueue("close_idle")
        except ValueError:
            return False
        agent.last_activity = agent.clock()
        return True

    # ---- agent callback (runs in the agent thread after the command finished) ----
    def on_done(self, action, item_id, error):
        now = self.clock()
        active = self.accounts.active_id
        notify = self.enabled()
        if action == "sync":
            self.inflight = False
            self.last_sync = now
            if error is None:
                self.failures, self.login_required, self.last_error = 0, False, ""
                self.notifier.reset("account:" + active, "login_expired")
                self.notifier.reset("sync", "sync_failed")
                self.after_sync(notify)
            elif isinstance(error, SessionExpired):
                self.login_required = True
                if notify:
                    self.notifier.notify("login_expired", "account:" + active, "科研通需登录", "科研通登录已失效，后台监控已暂停。请登录后恢复。")
            else:
                self.failures += 1
                self.last_error = type(error).__name__
                applog.record(self.store, "monitor", "sync", error)
                if self.failures >= FAIL_NOTIFY_AFTER and notify:
                    self.notifier.notify("sync_failed", "sync", "同步失败", "后台同步已连续失败 %d 次，请检查网络或登录状态。" % self.failures)
            self.persist()
            self.reschedule_after(now)
        elif action == "login" and error is None:
            self.login_required, self.failures = False, 0
            self.notifier.reset("account:" + active, "login_expired")
            self.next_due = now + 5
            self.persist()
        elif action == "switch_account":
            self.login_required = False
            self.next_due = now + START_DELAY
            self.persist()
        if notify and action != "close_idle":
            self.scan(self.items())

    def reschedule_after(self, now):
        with self.lock:
            self.next_due = now + self.interval(now)["seconds"]

    def persist(self):
        self.store.mutate(lambda _data: self.state.update(failures=self.failures, login_required=self.login_required, last_sync=self.last_sync))

    # ---- change detection ----
    def after_sync(self, notify):
        items = [i for i in self.items() if i.get("source") == "website"]
        stamp = datetime.fromtimestamp(self.clock()).astimezone().isoformat(timespec="seconds")
        for item in items:
            if not item.get("first_seen"):
                self.store.update(item["id"], first_seen=stamp)
        active = self.accounts.active_id
        known = self.state["known"].get(active)
        current = {i["id"]: (i.get("website_status") or "").strip() for i in items}
        handoff = False
        if self.pipeline is not None:
            try:   # a fulfilled item with the pipeline on is handed over now, not at the next poll
                handoff = self.pipeline.enabled() and any(stage_of(i) == "fulfilled" for i in items)
            except Exception:
                handoff = False
        if known is not None:
            for item in items:
                old, new = known.get(item["id"]), current[item["id"]]
                if old in FULFILLED and new not in FULFILLED:
                    self.notifier.reset(item["id"], "applied")
                if new in FULFILLED and old not in FULFILLED:
                    handoff = True
                    if notify:
                        self.notifier.notify("applied", item["id"], "有人应助", "《%s》已有人应助并上传文件。" % (item.get("title") or item.get("doi") or "?")[:40])
                if new in CLOSED and old not in CLOSED and notify:
                    self.notifier.notify("closed", item["id"], "求助已关闭", "《%s》已关闭（到期退还积分，或被关闭）。" % (item.get("title") or "?")[:40])
        self.store.mutate(lambda _data: self.state["known"].__setitem__(active, current))
        if handoff and notify and self.pipeline is not None:
            try:
                if self.pipeline.enabled():
                    self.pipeline.tick()   # immediate hand-over; no waiting for the next scheduler pass
            except Exception:
                pass

    def scan(self, items):
        """Notify about items that now need the person (deduplicated per item and kind)."""
        prefs = self.prefs()
        for item in items:
            stage, iid = stage_of(item), item["id"]
            title = "《%s》" % (item.get("title") or item.get("doi") or "?")[:40]
            verdict = (item.get("verification") or {}).get("status")
            # An attempted action is written before the website confirms it. While the worker
            # is still running it is progress, not yet a request for human intervention.
            in_flight = bool(self.agent.busy and any((item.get(k) or {}).get("status") == "attempted"
                             for k in ("publish_action", "adopt_action", "reject_action")))
            if stage == "attention" and not in_flight:
                self.notifier.notify("needs_action", iid, "需处理", title + "需处理：" + str(item.get("note") or item.get("website_status") or "请查看")[:80])
            elif stage == "awaiting_decision" and item.get("source") == "website":
                if verdict == "identity_match":
                    self.notifier.notify("download_ok", iid, "下载完成", title + "已下载并核对通过，待采纳。")
                elif not _auto_adopt_ok(item, prefs):
                    self.notifier.notify("needs_action", iid, "需处理", title + "核对未通过或需确认，请采纳或驳回。")
            else:
                self.notifier.reset(iid, "needs_action")
            status = (item.get("oa_check") or {}).get("status")
            if status == "needs_human" and not item.get("request_confirmed"):
                self.notifier.notify("needs_human", iid, "需人工验证", title + "的免费全文页面需人工验证。")
            else:
                self.notifier.reset(iid, "needs_human")
            if item.get("done_via") == "oa" and status == "downloaded":
                self.notifier.notify("oa_ok", iid, "免费全文已下载", title + "已下载并核对免费全文。")
