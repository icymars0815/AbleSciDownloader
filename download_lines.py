"""Fastest-line selection for the three normal download lines (IMPROVEMENT_PLAN D3), size/ETA text (D4) and
failure explanations (D8). Pure logic with injected probes/clocks: nothing here touches the browser or the network."""
import re
import time

LINES = (1, 2, 3)
PROBE_RANGE = (5, 60)
PROBE_DEFAULT = 15
HISTORY_KEEP = 6            # samples kept per (line, hour of day)
HISTORY_DAYS = 7            # "recent" history
SWITCH_FRACTION = 0.5       # switch only if current < half of another line's historical speed ...
SWITCH_HOLD = 60            # ... for this many seconds in a row
HISTORY_PROBE_SECONDS = 10  # lean probing: the historically fastest line is probed this long
HIGHSPEED = "highspeed"     # speed-history key for the paid high-speed channel
KEEP_FRACTION = 0.5         # history-first probe: keep the favourite if it still reaches this share of its history


def _hour_gap(a, b):
    d = abs(a - b) % 24
    return min(d, 24 - d)


class SpeedHistory:
    """Per-line speeds by hour of day, persisted in the queue file (store.data['line_speeds'])."""

    def __init__(self, store, clock=time.time):
        self.store, self.clock = store, clock

    @staticmethod
    def _hour(when):
        return time.localtime(when).tm_hour

    def record(self, line, bps, when=None):
        if not isinstance(bps, (int, float)) or isinstance(bps, bool) or bps <= 0 or (line not in LINES and line != HIGHSPEED):
            return
        when = self.clock() if when is None else when
        with self.store.lock:
            table = self.store.data.setdefault("line_speeds", {})
            samples = table.setdefault(str(line), [])
            samples.append(dict(t=int(when), hour=self._hour(when), bps=int(bps)))
            table[str(line)] = [s for s in samples if when - s["t"] < HISTORY_DAYS * 86400][-HISTORY_KEEP * 24:]
            self.store.save()

    def expected(self, line, when=None):
        """Mean of recent samples at this hour of day (+-1 hour), or None when there is no recent history."""
        when = self.clock() if when is None else when
        hour = self._hour(when)
        with self.store.lock:
            samples = list((self.store.data.get("line_speeds") or {}).get(str(line)) or [])
        near = [s["bps"] for s in samples if 0 <= when - s["t"] < HISTORY_DAYS * 86400 and _hour_gap(s["hour"], hour) <= 1]
        near = near[-HISTORY_KEEP:]
        return sum(near) / len(near) if near else None

    def all(self, when=None):
        return {line: self.expected(line, when) for line in LINES}


def select_line(lines, probe, history, seconds, record=None):
    """Choose the line to download on.
    probe(line, secs) -> measured bytes/s, or None if the file finished during the probe.
    history: {line: expected bytes/s or None}. With recent history the historically fastest line is probed first,
    for a shorter time, and kept if it still delivers; otherwise every line is probed and the fastest wins.
    Returns dict(chosen, measured, mode, secs, done)."""
    measured, record = {}, record or (lambda line, bps: None)
    known = [l for l in lines if history.get(l)]
    out = dict(chosen=lines[0], measured=measured, mode="full", secs=seconds, done=False)

    def run(line, secs):
        bps = probe(line, secs)
        if bps is None:
            out.update(chosen=line, done=True)
            return None
        measured[line] = bps
        record(line, bps)
        return bps

    if known:
        secs = HISTORY_PROBE_SECONDS
        best = max(known, key=lambda l: history[l])
        out.update(mode="history", secs=secs, chosen=best)
        bps = run(best, secs)
        if out["done"] or bps >= history[best] * KEEP_FRACTION:
            return out
        out["mode"] = "history_then_full"
        for line in lines:
            if line != best:
                if run(line, secs) is None:
                    return out
    else:
        for line in lines:
            if run(line, seconds) is None:
                return out
    out["chosen"] = max(lines, key=lambda l: (measured.get(l, -1), l == lines[0]))
    return out


def order_after(chosen, measured, lines=LINES):
    """Fallback order: chosen first, then the rest by measured speed (unmeasured last, line number as tie-break)."""
    rest = sorted((l for l in lines if l != chosen), key=lambda l: (-(measured.get(l) or 0), l))
    return [chosen] + rest


class SwitchWatch:
    """Mid-download switch rule: only when the current line stays below half of another line's historical speed for
    `hold` seconds in a row. Returns the line to switch to, else None."""

    def __init__(self, current, history, fraction=SWITCH_FRACTION, hold=SWITCH_HOLD):
        self.current, self.history, self.fraction, self.hold = current, history, fraction, hold
        self.since = None

    def observe(self, now, bps):
        others = {l: v for l, v in self.history.items() if l != self.current and v}
        if not others:
            return None
        target = max(others, key=others.get)
        if bps < others[target] * self.fraction:
            if self.since is None:
                self.since = now
            return target if now - self.since >= self.hold else None
        self.since = None
        return None


# ---------- D4: size and time ----------
def fmt_size(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return ("%d %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024


def fmt_duration(seconds):
    seconds = int(max(0, seconds))
    if seconds < 60:
        return "%d 秒" % seconds
    if seconds < 3600:
        return "%d 分 %d 秒" % divmod(seconds, 60) if seconds % 60 else "%d 分钟" % (seconds // 60)
    return "%d 小时 %d 分" % (seconds // 3600, seconds % 3600 // 60)


def eta_seconds(received, total, bps):
    if not total or not bps or bps <= 0:
        return None
    return max(0, (total - received) / bps)


def plan_text(total, bps, line_max_minutes):
    """Text shown before/while a download starts: size, expected time, and advice when it will not fit one line's limit."""
    if not total:
        return "文件大小尚未读取"
    text = "文件大小 " + fmt_size(total)
    eta = eta_seconds(0, total, bps)
    if eta is None:
        return text + "；暂无速度数据，开始后将显示预计用时"
    text += "，按当前速度预计还需约 " + fmt_duration(eta)
    if eta > line_max_minutes * 60:
        text += "（超过单线路时限 %d 分钟；可在“设置 → 下载”中启用智能高速，或换个时段重试）" % line_max_minutes
    return text


# ---------- D8: failure messages ----------
def explain_failure(message, exc=None):
    """Plain-language failure class + next step. Returns dict(kind, label, advice)."""
    text = str(message or "")
    name = type(exc).__name__ if exc is not None else ""
    if name == "SessionExpired" or re.search(r"会话已失效|登录失效|请先.{0,6}登录", text):
        return dict(kind="login_expired", label="登录失效", advice="请先点击“登录”重新登录科研通，再重新下载")
    if re.search(r"文件已删除|文件已过期|已被删除", text):
        return dict(kind="file_deleted", label="科研通文件已删除", advice="科研通在求助完结约 24 小时后清理文件；本地没有副本时，需重新求助")
    if re.match(r"下载已停止|已停止，未|已请求停止", text):
        return dict(kind="stopped", label="已停止", advice="下载已停止，需要时可重新下载")
    if re.search(r"高速", text) and re.search(r"未完成|失败|停止|入口", text) and "普通线路均未完成" not in text:
        return dict(kind="highspeed_failed", label="高速通道未完成", advice="软件不会重复点击高速通道，也不会重复扣分。请在科研通查看是否已扣分；已扣分但未下载成功的，可提交工单")
    if re.search(r"PDF 解析失败|解析失败", text):
        return dict(kind="bad_file", label="收到的文件无法打开", advice="文件已保留在本机。请手动打开检查，确认损坏的，可按科研通规则驳回（需先多次尝试）")
    if re.search(r"页面提示失败|线路.{0,4}(报错|失败)|error", text):
        return dict(kind="line_error", label="线路报错", advice="请稍后重试；若 3 条线路均报错，请先在科研通确认文件是否仍存在")
    if re.search(r"普通线路均未完成|未完成|卡|超时|预计超出|速度", text):
        return dict(kind="site_slow", label="科研通响应慢或下载速度过低", advice="建议换个时段重试（凌晨通常更快），或在“设置 → 下载”中启用“智能”高速")
    if re.search(r"下载入口|入口格式|找不到|尚未找到|页面变化|超时", text):
        return dict(kind="page_changed", label="下载页面无法打开，或页面结构已变化", advice="请先确认该求助在科研通中可正常打开，再重试")
    return dict(kind="unknown", label="下载未完成", advice="请打开求助详情查看，再决定是否重试")


# ---------- D7: keep the site's original file name ----------
RESERVED = {"CON", "PRN", "AUX", "NUL"} | {"COM%d" % n for n in range(1, 10)} | {"LPT%d" % n for n in range(1, 10)}


def safe_filename(name, limit=150):
    """The site's suggested file name with ONLY characters Windows cannot store replaced; "" when nothing usable is left."""
    name = re.split(r"[\\/]", str(name or ""))[-1]
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip().rstrip(". ")
    if not name.strip("._ "):
        return ""
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    if stem.split(".")[0].upper() in RESERVED:
        stem = "_" + stem
    if len(stem) + len(ext) + 1 > limit:
        stem = stem[:max(1, limit - len(ext) - 1)]
    return stem + ("." + ext if ext else "")
