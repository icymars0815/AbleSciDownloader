"""Progress and fallback policy, independent of browser/network details."""
import re
from dataclasses import dataclass, field

RULES_VERSION = "2026-10-19-v1"


def byte_size(text):
    match = re.search(r"([\d,.]+)\s*(B|KB|MB|GB|TB)\b", text, re.I)
    if not match:
        return 0
    return int(float(match[1].replace(",", "")) * 1024 ** ["B", "KB", "MB", "GB", "TB"].index(match[2].upper()))


def read_progress(body):
    # Help/FAQ text mentions failures even during successful transfers; exclude it.
    front = body.split("常见问题", 1)[0]
    received = re.search(r"已接收\s*([\d,.]+\s*[KMGT]?B)\s*/\s*([\d,.]+\s*[KMGT]?B)", front, re.I)
    current, total = (byte_size(received[1]), byte_size(received[2])) if received else (0, 0)
    errors = [line.strip() for line in front.splitlines() if re.search(r"^(?:下载失败|连接失败|请求失败|网络错误|下载超时|文件已删除|文件已过期|错误[:：])", line.strip())]
    return dict(received=current, total=total, percent=round(current / total * 100, 1) if total else 0,
                error=errors[0][:150] if errors else "")


def dynamic_cap(base_seconds, total, min_ok_bps, hard_cap=3600):
    """Per-line time cap: the configured limit, raised to size / minimum acceptable speed, never above hard_cap (60 min).
    Without a known size or minimum speed it is the configured limit."""
    if not total or not min_ok_bps or min_ok_bps <= 0:
        return base_seconds
    return min(hard_cap, max(base_seconds, total / min_ok_bps))


def dynamic_cap_for(settings, total, minutes=None):
    """Same, from download settings; `minutes` overrides line_max_minutes (patient / high-speed caps)."""
    base = (settings["line_max_minutes"] if minutes is None else minutes) * 60
    return dynamic_cap(base, total, settings["min_ok_kbps"] * 1024)


def longer_minutes(settings):
    """Patient / high-speed base limit: at least 30 min (3x the line limit), never above 60 min."""
    return min(60, max(30, settings["line_max_minutes"] * 3))


@dataclass
class TransferWatch:
    started: float
    last_progress: float
    received: int = 0
    # Observed 2026-10-19 on a real 14.9 MB file: the site pauses 30-60 s and then resumes,
    # and switching lines restarts from 0. So wait longer, and much longer once a line has
    # made real progress. Implementer-chosen values, not user-specified numbers.
    stall_seconds: float = 120
    committed_stall_seconds: float = 300
    committed_fraction: float = 0.10
    max_seconds: float = 2400   # raw default; make_watch() applies the configured cap (default 10 min)
    slow_threshold_bps: int = 20 * 1024
    speed_window: float = 30
    speed_bps: int = 0
    # Smart mode: after eta_min_observe seconds, a line whose projected remaining time at the
    # current speed exceeds the per-line cap (max_seconds) is treated as failed ("eta").
    eta_check: bool = False
    eta_min_observe: float = 90
    # Size-aware cap (O2): once the file size is known the per-line cap is max(max_seconds, size / min_ok_bps), at most hard_cap.
    min_ok_bps: float = 0
    hard_cap: float = 3600
    samples: list = field(default_factory=list)

    def __post_init__(self):
        self.samples = [(self.started, self.received)]

    def cap(self, total=0):
        return dynamic_cap(self.max_seconds, total, self.min_ok_bps, self.hard_cap)

    def observe(self, now, received, error="", total=0):
        if received < self.received:
            # A line switch/reconnect may reset the page's byte counter.
            # Start a fresh speed window rather than treating old bytes as a floor.
            self.received = received
            self.last_progress = now
            self.samples = [(now, received)]
        if received > self.received:
            self.received = received
            self.last_progress = now
        self.samples.append((now, received))
        while len(self.samples) > 2 and self.samples[1][0] <= now - self.speed_window:
            self.samples.pop(0)
        elapsed = now - self.samples[0][0]
        self.speed_bps = int(max(0, received - self.samples[0][1]) / elapsed) if elapsed > 0 else 0
        if error:
            return "error"
        cap = self.cap(total)
        if now - self.started > cap:
            return "timeout"
        committed = bool(total) and self.received >= total * self.committed_fraction
        if now - self.last_progress > (self.committed_stall_seconds if committed else self.stall_seconds):
            return "stalled"
        if (self.eta_check and total and now - self.started >= self.eta_min_observe and 0 < self.received < total
                and self.speed_bps > 0 and (total - self.received) / self.speed_bps > cap):
            return "eta"
        if self.received and elapsed >= self.speed_window and self.speed_bps < self.slow_threshold_bps:
            return "slow"
        return "receiving" if self.received else "connecting"


# ---- user-configurable judgement parameters (preferences download_*) and high-speed modes ----
DOWNLOAD_DEFAULTS = dict(line_max_minutes=10, stall_seconds=120, committed_stall_seconds=300,
                         committed_percent=10, slow_kbps=20, smart_eta_check=True,
                         probe_enabled=True, probe_seconds=15, min_ok_kbps=8,
                         normal_max_failures=5, highspeed_max_tries=5, auto_retry_max=10)
DOWNLOAD_RANGES = dict(line_max_minutes=(3, 60), stall_seconds=(30, 600), committed_stall_seconds=(60, 1200),
                       committed_percent=(1, 90), slow_kbps=(1, 1000), probe_seconds=(5, 60), min_ok_kbps=(1, 200),
                       normal_max_failures=(1, 20), highspeed_max_tries=(1, 20), auto_retry_max=(0, 50))
HIGHSPEED_MODES = ("off", "smart", "force")
FORCE_DEFAULT_POINTS = 500


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def validate_download_settings(body):
    """Return {download_<key>: value} for a dict of settings; raise ValueError (HTTP 400) for any bad value."""
    if not isinstance(body, dict) or not body:
        raise ValueError("下载规则参数无效")
    changes = {}
    for key, value in body.items():
        if key in ("smart_eta_check", "probe_enabled"):
            if not isinstance(value, bool):
                raise ValueError("开关必须为布尔值")
        elif key in DOWNLOAD_RANGES:
            low, high = DOWNLOAD_RANGES[key]
            if not _is_int(value) or not low <= value <= high:
                raise ValueError(f"{key} 须为 {low}～{high} 的整数")
        else:
            raise ValueError("未知的下载规则参数：" + str(key)[:40])
        changes["download_" + key] = value
    return changes


def download_settings(preferences):
    """Effective settings: stored value if valid, else the default (a damaged preference never widens a limit)."""
    preferences = preferences or {}
    result = dict(DOWNLOAD_DEFAULTS)
    for key in DOWNLOAD_DEFAULTS:
        value = preferences.get("download_" + key)
        try:
            validate_download_settings({key: value})
        except ValueError:
            continue
        result[key] = value
    return result


def download_defaults_as_preferences():
    return {"download_" + k: v for k, v in DOWNLOAD_DEFAULTS.items()}


def make_watch(start, settings, eta_check=False):
    return TransferWatch(start, start, stall_seconds=settings["stall_seconds"],
                         committed_stall_seconds=settings["committed_stall_seconds"],
                         committed_fraction=settings["committed_percent"] / 100,
                         max_seconds=settings["line_max_minutes"] * 60,
                         slow_threshold_bps=settings["slow_kbps"] * 1024, min_ok_bps=settings["min_ok_kbps"] * 1024,
                         eta_check=bool(eta_check and settings["smart_eta_check"]))


def chosen_mode(preferences):
    """(mode, user_chose). Legacy allow_paid_fallback maps True -> smart, False -> off."""
    preferences = preferences or {}
    mode = preferences.get("download_highspeed")
    if mode in HIGHSPEED_MODES:
        return mode, bool(preferences.get("download_highspeed_chosen", True))
    if isinstance(preferences.get("allow_paid_fallback"), bool):
        return ("smart" if preferences["allow_paid_fallback"] else "off"), True
    return "off", False


def migrate_preferences(store):
    """allow_paid_fallback True -> smart, False -> off (an explicit past choice). Idempotent."""
    prefs = store.snapshot().get("preferences", {})
    if prefs.get("download_highspeed") not in HIGHSPEED_MODES and isinstance(prefs.get("allow_paid_fallback"), bool):
        store.update_preferences(download_highspeed="smart" if prefs["allow_paid_fallback"] else "off", download_highspeed_chosen=True)


def resolve_highspeed(preferences, points):
    """Effective mode for one download.
    Returns (mode, source, pref_changes, event_message); source is "user" or "default".
    Never chosen: points >= 500 -> force (default), otherwise off. If a default-force was applied earlier and
    points have since dropped below 500, fall back to off (and report it once)."""
    preferences = preferences or {}
    mode, chose = chosen_mode(preferences)
    high = _is_int(points) and points >= FORCE_DEFAULT_POINTS
    if chose:
        return mode, "user", {}, ""
    if high:
        changes = {} if preferences.get("download_default_force_applied") else {"download_default_force_applied": True}
        return "force", "default", changes, ""
    if preferences.get("download_default_force_applied"):
        return "off", "default", {"download_default_force_applied": False}, "积分已低于 500，默认的强制高速已自动回落为关闭（未扣分）"
    return "off", "default", {}, ""


def highspeed_allowed(failed_lines, preferences, rules_text, mode=None, points=None):
    """Fee-text gate shared by smart and force. mode None -> derived from preferences (legacy callers).
    smart needs all 3 normal lines failed; force skips them. Both need the live fee text to match the
    consented tariff; the consent (rules_version) is also required, except when the account has >= 500
    points (high speed is free there, and that is the only case a default force can run without a click-through)."""
    preferences = preferences or {}
    if mode is None:
        mode = chosen_mode(preferences)[0]
    if mode not in ("smart", "force"):
        return False, "高速通道未开启"
    if mode == "smart" and set(failed_lines) != {1, 2, 3}:
        return False, "尚未确认 3 条普通线路全部失败"
    free = _is_int(points) and points >= FORCE_DEFAULT_POINTS
    if not free and preferences.get("rules_version") != RULES_VERSION:
        return False, "需要重新确认高速下载规则"
    # Fail closed if the website's displayed tariff no longer matches the consent.
    compact = re.sub(r"\s+", "", rules_text).replace("＜", "<").replace("≥", ">=")
    if not (re.search(r"积分<500.*?扣(?:除)?2积分", compact) and re.search(r"积分>=500.*?(?:免积分|不扣积分)", compact)):
        return False, "科研通费用规则与已确认的规则不一致，停止自动切换"
    if mode == "force":
        return True, "强制高速：费用规则匹配，直接使用高速通道"
    return True, "3 条普通线路全部失败，费用规则匹配，可切换高速"


# ---- automatic retry and human takeover (docs/DOWNLOAD_SETTINGS_DESIGN.md) ----
RETRY_BACKOFF_MIN = (1, 3, 5, 10)       # then every 10 minutes (user 2026-10-20)
DEADLINE_MARGIN_SECONDS = 2 * 3600       # takeover when less than 2 h remain before deletion / auto-adopt


def backoff_minutes(count):
    """Wait before automatic retry number `count` (1-based): 1, 3, 5, 10, then 10."""
    return RETRY_BACKOFF_MIN[min(max(count, 1), len(RETRY_BACKOFF_MIN)) - 1]


def takeover_reason(normal_failures, highspeed_failures, retry_count, settings, highspeed_possible, seconds_left=None):
    """Which takeover threshold (if any) is hit; returns a plain-language message or "". Pure."""
    nmax, hmax, rmax = settings["normal_max_failures"], settings["highspeed_max_tries"], settings["auto_retry_max"]
    if highspeed_failures >= hmax:
        return "高速通道已失败 %d 次（上限 %d 次），自动重试停止，请人工处理" % (highspeed_failures, hmax)
    if normal_failures >= nmax and not highspeed_possible:
        return "普通线路累计失败 %d 次（上限 %d 次）且高速通道未开启或不可用，自动重试停止，请人工处理" % (normal_failures, nmax)
    if rmax == 0:
        return "已设置为不自动重试（自动重试最多 0 轮），请人工处理"
    if retry_count >= rmax:
        return "自动重试已达 %d 轮（上限 %d 轮；普通线路失败 %d 次，高速失败 %d 次），请人工处理" % (retry_count, rmax, normal_failures, highspeed_failures)
    if seconds_left is not None and seconds_left < DEADLINE_MARGIN_SECONDS:
        return "距科研通文件删除或自动采纳不足 2 小时（普通线路失败 %d 次，高速通道失败 %d 次），已停止自动重试，需处理" % (normal_failures, highspeed_failures)
    return ""
