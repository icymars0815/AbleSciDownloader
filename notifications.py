"""System notifications: master + per-item toggles, quiet hours with a single summary afterwards, and de-duplication.
Delivery goes through an injected sink (the tray icon's toast); everything is also recorded in the app event list."""
import re
import threading
from datetime import datetime

KINDS = {   # kind: (label, default)
    "applied": ("新应助", True),
    "download_ok": ("下载并核对通过", True),
    "needs_action": ("需处理（采纳 / 驳回、核对未通过）", True),
    "needs_human": ("需人工验证（出版社人机验证）", True),
    "min_bounty": ("最低悬赏上调，待确认", True),
    "login_expired": ("登录失效", True),
    "closed": ("求助已关闭或到期退还积分", False),
    "oa_ok": ("免费全文已下载", False),
    "sync_failed": ("同步失败（连续 3 次以上提示）", False),
}
TIME_RE = re.compile(r"([01]\d|2[0-3]):[0-5]\d")
ACTIONS = {"applied": "focus", "download_ok": "focus", "needs_action": "focus", "needs_human": "oa-human", "min_bounty": "focus", "login_expired": "login"}
KEEP = 200   # remembered (item, kind) pairs


def defaults():
    return dict(master=True, items={k: v[1] for k, v in KINDS.items()}, quiet_enabled=False, quiet_start="22:00", quiet_end="08:00")


def settings(prefs):
    """Effective settings from preferences (missing or malformed values fall back to defaults)."""
    prefs = prefs or {}
    out = defaults()
    if isinstance(prefs.get("notify_master"), bool):
        out["master"] = prefs["notify_master"]
    stored = prefs.get("notify_items")
    if isinstance(stored, dict):
        for kind in KINDS:
            if isinstance(stored.get(kind), bool):
                out["items"][kind] = stored[kind]
    if isinstance(prefs.get("notify_quiet_enabled"), bool):
        out["quiet_enabled"] = prefs["notify_quiet_enabled"]
    for key in ("quiet_start", "quiet_end"):
        value = prefs.get("notify_" + key)
        if isinstance(value, str) and TIME_RE.fullmatch(value):
            out[key] = value
    return out


def validate(body, current):
    """Strict validation of a POST body; returns preference changes."""
    allowed = {"master", "items", "quiet_enabled", "quiet_start", "quiet_end"}
    if not isinstance(body, dict) or not body or set(body) - allowed:
        raise ValueError("通知设置无效")
    changes = {}
    for key in ("master", "quiet_enabled"):
        if key in body:
            if not isinstance(body[key], bool):
                raise ValueError("通知开关参数无效")
            changes["notify_" + key] = body[key]
    if "items" in body:
        items = body["items"]
        if not isinstance(items, dict) or not items or set(items) - set(KINDS) or any(not isinstance(v, bool) for v in items.values()):
            raise ValueError("单项通知开关无效")
        merged = dict(current["items"])
        merged.update(items)
        changes["notify_items"] = merged
    for key in ("quiet_start", "quiet_end"):
        if key in body:
            if not isinstance(body[key], str) or not TIME_RE.fullmatch(body[key]):
                raise ValueError("免打扰时间须为 HH:MM")
            changes["notify_" + key] = body[key]
    return changes


def in_quiet(cfg, moment):
    if not cfg["quiet_enabled"]:
        return False
    start, end = cfg["quiet_start"], cfg["quiet_end"]
    now = moment.strftime("%H:%M")
    if start == end:
        return False
    return start <= now < end if start < end else (now >= start or now < end)


class Notifier:
    """notify(kind, key, title, message): at most once per (key, kind) until reset(key, kind).
    sink(title, message) shows a toast; state lives in store.data["notifications"] so a restart does not repeat toasts."""

    def __init__(self, store, sink=None, clock=lambda: datetime.now().astimezone()):
        self.store, self.sink, self.clock = store, sink, clock
        self.lock = threading.RLock()
        with store.lock:
            self.state = store.data.setdefault("notifications", {"seen": [], "pending": []})
            self.state.setdefault("seen", [])
            self.state.setdefault("pending", [])

    def cfg(self):
        return settings(self.store.snapshot().get("preferences", {}))

    def _save(self):
        with self.store.lock:
            self.state["seen"] = self.state["seen"][-KEEP:]
            self.store.save()

    def reset(self, key, kind):
        """Allow the same (key, kind) to notify again, e.g. after a successful sync cleared a login problem."""
        with self.lock:
            pair = [key, kind]
            if pair in self.state["seen"]:
                self.state["seen"].remove(pair)
                self._save()

    def notify(self, kind, key, title, message):
        """Returns 'sent' | 'queued' | 'muted' | 'duplicate'."""
        if kind not in KINDS:
            raise ValueError("unknown notification kind")
        with self.lock:
            pair = [str(key), kind]
            if pair in self.state["seen"]:
                return "duplicate"
            self.state["seen"].append(pair)
            cfg = self.cfg()
            now = self.clock()
            if not cfg["master"] or not cfg["items"].get(kind):
                outcome = "muted"
            elif in_quiet(cfg, now):
                self.state["pending"].append(dict(kind=kind, title=title, message=message))
                outcome = "queued"
            else:
                outcome = "sent"
            self._save()
        key_text = str(key)
        self.store.event("[通知] " + message + {"muted": "（通知已关闭，仅记录）", "queued": "（免打扰时段，稍后汇总）"}.get(outcome, ""),
                         kind="notify", item_id=key_text.split(":")[0] if not key_text.startswith(("account", "sync", "batch")) else "",
                         batch_id=key_text.split(":", 1)[1] if key_text.startswith("batch:") else "",
                         action="open-batch" if key_text.startswith("batch:") else ACTIONS.get(kind, ""))
        if outcome == "sent":
            self._show(title, message)
        return outcome

    def flush(self):
        """After quiet hours: one summary toast for everything queued. Returns True if a summary was shown."""
        with self.lock:
            cfg = self.cfg()
            if not self.state["pending"] or not cfg["master"] or in_quiet(cfg, self.clock()):
                return False
            pending, self.state["pending"] = self.state["pending"], []
            self._save()
        lines = [p["message"] for p in pending if cfg["items"].get(p["kind"])]
        if not lines:
            return False
        text = "免打扰期间共 %d 条通知：" % len(lines) + "；".join(lines[:3]) + ("…" if len(lines) > 3 else "")
        self._show("文献通 通知汇总", text[:250])
        return True

    def pending(self):
        return len(self.state["pending"])

    def _show(self, title, message):
        if self.sink:
            try:
                self.sink(title, message[:250])
            except Exception:
                pass
