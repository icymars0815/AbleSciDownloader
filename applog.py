"""Structured diagnostic log (JSON lines) + central redaction + redacted error report.

Logs live in <data>/logs/kytdl-YYYYMMDD.log and only files with that exact name pattern are ever rotated (14 days).
Everything is passed through redact() before it is written or shown. The log is a developer-diagnostics layer;
the user-facing event list (Store.event) is unchanged."""
import hashlib
import importlib.metadata
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import app_info

KEEP_DAYS = 14
LOG_RE = re.compile(r"^kytdl-(\d{8})\.log$")
SERVER_LOG_RE = re.compile(r"^server-[\w.\-]+\.log$")
KEEP_SCREENSHOTS = 200
ROOT = Path(__file__).resolve().parent
_URL_PARAMS = r"(?:token|access_token|refresh_token|id_token|key|api_key|apikey|api-key|sig|signature|secret|password|passwd|pwd|auth|authorization|sessionid|session|sid|code)"


def redact(text, root=None):
    """Remove personal data and secrets from free text. Safe to call on anything; never raises."""
    try:
        text = "" if text is None else str(text)
        if root:
            text = text.replace(str(root), "<data>")
        # cookies first: the whole header value goes
        text = re.sub(r"(?i)\b(set-cookie|cookie)\s*:[^\r\n]*", r"\1: <redacted>", text)
        # bearer / sk- style keys
        text = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=\-]{6,}", "Bearer <redacted>", text)
        text = re.sub(r"\bsk-[A-Za-z0-9_\-]{6,}", "sk-<redacted>", text)
        # JSON "password": "..." and password=... fields
        text = re.sub(r"(?i)([\"']?(?:password|passwd|pwd|密码)[\"']?\s*[:=：]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;&}\]]+)", r"\1<redacted>", text)
        # token/key/sig URL parameters
        text = re.sub(r"(?i)([?&;]" + _URL_PARAMS + r"=)[^&\s#\"'<>]*", r"\1<redacted>", text)
        # loose key=..., token: ... assignments with a secret-looking value
        text = re.sub(r"(?i)\b(api[_-]?key|access[_-]?token|token|secret|key)\s*[:=]\s*[\"']?[A-Za-z0-9._~+/=\-]{8,}[\"']?", r"\1=<redacted>", text)
        # e-mail addresses
        text = re.sub(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", "<email>", text)
        # Windows user directory names
        text = re.sub(r"(?i)([A-Za-z]:[\\/]+Users[\\/]+)[^\\/\s\"']+", r"\1<user>", text)
        text = re.sub(r"(?i)(/Users/|/home/)[^/\s\"']+", r"\1<user>", text)
        # long opaque tokens that survived
        text = re.sub(r"\b[A-Za-z0-9_\-]{40,}\b", "<redacted>", text)
        return text
    except Exception:
        return "<unprintable>"


_commit = None


def commit_short():
    global _commit
    if _commit is None:
        _commit = ""
        try:
            out = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=3)
            if out.returncode == 0 and re.fullmatch(r"[0-9a-f]{4,40}", out.stdout.strip()):
                _commit = out.stdout.strip()
        except Exception:
            pass
    return _commit


def project_stack(exc, limit=6):
    """Frames from this project's own files only (no library internals): file name, line, function."""
    frames = []
    try:
        for f in traceback.extract_tb(exc.__traceback__):
            try:
                path = Path(f.filename).resolve()
                inside = path.is_relative_to(ROOT) and ".venv" not in path.parts
            except Exception:
                inside = False
            if inside:
                frames.append("%s:%d %s" % (Path(f.filename).name, f.lineno, f.name))
    except Exception:
        pass
    return frames[-limit:]


def rotate_server_logs(folder, today=None, keep_days=KEEP_DAYS):
    """Delete launcher stdout/stderr files (server-*.log) whose last change is older than keep_days. Never raises."""
    removed = []
    cutoff = datetime.combine(today or datetime.now().date(), datetime.min.time()) - timedelta(days=keep_days)
    try:
        names = list(Path(folder).iterdir())
    except OSError:
        return removed
    for entry in names:
        try:
            if SERVER_LOG_RE.match(entry.name) and entry.is_file() and datetime.fromtimestamp(entry.stat().st_mtime) < cutoff:
                entry.unlink()
                removed.append(entry.name)
        except OSError:
            pass
    return removed


def trim_screenshots(folder, keep=KEEP_SCREENSHOTS):
    """Keep only the newest `keep` PNG screenshots (browser-last.png is always kept). Never raises."""
    removed = []
    try:
        files = [p for p in Path(folder).iterdir() if p.suffix.lower() == ".png" and p.is_file() and p.name != "browser-last.png"]
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return removed
    for entry in files[keep:]:
        try:
            entry.unlink()
            removed.append(entry.name)
        except OSError:
            pass
    return removed


class AppLog:
    def __init__(self, folder, clock=datetime.now):
        self.folder = Path(folder)
        self.clock = clock
        self.lock = threading.Lock()
        self._rotated_day = None

    def path_for(self, day):
        return self.folder / ("kytdl-%s.log" % day.strftime("%Y%m%d"))

    def rotate(self, today=None):
        """Delete only our own kytdl-YYYYMMDD.log files older than KEEP_DAYS. Anything else in the folder is left alone."""
        today = (today or self.clock()).date()
        removed = []
        try:
            names = list(self.folder.iterdir())
        except OSError:
            return removed
        for entry in names:
            m = LOG_RE.match(entry.name)
            if not m or not entry.is_file():
                continue
            try:
                day = datetime.strptime(m.group(1), "%Y%m%d").date()
            except ValueError:
                continue
            if today - day > timedelta(days=KEEP_DAYS):
                try:
                    entry.unlink()
                    removed.append(entry.name)
                except OSError:
                    pass
        removed += rotate_server_logs(self.folder, today)
        trim_screenshots(self.folder.parent / "screenshots")
        return removed

    def write(self, module, action, result, error_type="", message="", stack=None, **extra):
        """result: 'ok' | 'error' | 'warn'. Never raises."""
        try:
            now = self.clock()
            entry = dict(time=now.astimezone().isoformat(timespec="seconds"), module=redact(module), action=redact(action),
                         result=result, version=app_info.APP_VERSION)
            commit = commit_short()
            if commit:
                entry["commit"] = commit
            if error_type:
                entry["error_type"] = redact(error_type)
            if message:
                entry["message"] = redact(message)[:500]
            if stack:
                entry["stack"] = [redact(s) for s in stack]
            for k, v in extra.items():
                if v not in (None, ""):
                    entry[k] = redact(v)[:300] if isinstance(v, str) else v
            line = json.dumps(entry, ensure_ascii=False)
            with self.lock:
                self.folder.mkdir(parents=True, exist_ok=True)
                if self._rotated_day != now.date():
                    self.rotate(now)
                    self._rotated_day = now.date()
                with self.path_for(now).open("a", encoding="utf-8", newline="\n") as handle:
                    handle.write(line + "\n")
            return entry
        except Exception:
            return None

    def error(self, module, action, exc, **extra):
        return self.write(module, action, "error", type(exc).__name__, str(exc), project_stack(exc), **extra)

    def entries(self, days=KEEP_DAYS):
        out = []
        try:
            files = sorted((e for e in self.folder.iterdir() if LOG_RE.match(e.name) and e.is_file()), key=lambda e: e.name)
        except OSError:
            return out
        for entry in files[-days:]:
            try:
                for line in entry.read_text("utf-8", errors="replace").splitlines():
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(row, dict):
                        out.append(row)
            except OSError:
                pass
        return out

    def recent_errors(self, limit=20):
        rows = [r for r in self.entries() if r.get("result") == "error"][-limit:]
        for r in rows:
            r["id"] = hashlib.sha1(json.dumps(r, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:10]
        return list(reversed(rows))


def record(store, module, action, exc=None, result="error", message="", **extra):
    """Call-site helper: logs through store.log; silently does nothing if there is none."""
    log = getattr(store, "log", None)
    if log is None:
        return None
    try:
        if exc is not None:
            return log.error(module, action, exc, **extra)
        return log.write(module, action, result, message=message, **extra)
    except Exception:
        return None


# ---------- environment + report ----------
def chrome_present():
    names = [os.environ.get(v, "") for v in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")]
    candidates = [Path(n) / "Google" / "Chrome" / "Application" / "chrome.exe" for n in names if n]
    return any(c.is_file() for c in candidates) or bool(shutil.which("chrome") or shutil.which("google-chrome"))


def environment():
    versions = {}
    for label, dist in (("playwright", "playwright"), ("pymupdf", "PyMuPDF"), ("mcp", "mcp"), ("pystray", "pystray"), ("Pillow", "Pillow")):
        try:
            versions[label] = importlib.metadata.version(dist)
        except Exception:
            versions[label] = "未安装"
    return dict(app_version=app_info.APP_VERSION, commit=commit_short() or "未知", windows=platform.platform(),
                python=sys.version.split()[0], chrome="已找到" if chrome_present() else "未找到", dependencies=versions)


def build_report(store, error_ids=None, extra=None, now=None):
    """Redacted Markdown: selected (default: latest) log errors + last 50 events + environment."""
    root = getattr(store, "root", None)
    log = getattr(store, "log", None)
    errors = log.recent_errors(30) if log else []
    chosen = [e for e in errors if e.get("id") in set(error_ids)] if error_ids else errors[:10]
    env = environment()
    stamp = (now or datetime.now()).strftime("%Y-%m-%d %H:%M")
    lines = ["# 科研通下载器 错误报告", "", "生成时间：" + stamp, "", "## 环境", "",
             "- App 版本：%s（提交 %s）" % (env["app_version"], env["commit"]), "- 系统：" + env["windows"], "- Python：" + env["python"],
             "- Chrome：" + env["chrome"], "- 依赖：" + "，".join("%s %s" % kv for kv in env["dependencies"].items()), "", "## 所选错误", ""]
    if chosen:
        for e in chosen:
            lines.append("- `%s` %s / %s：%s %s" % (e.get("time", ""), e.get("module", ""), e.get("action", ""), e.get("error_type", ""), e.get("message", "")))
            if e.get("stack"):
                lines.append("  - 位置：" + " → ".join(e["stack"]))
    else:
        lines.append("（无错误记录）")
    events = []
    try:
        events = (store.snapshot().get("events") or [])[-50:]
    except Exception:
        pass
    lines += ["", "## 最近 50 条动态", ""]
    lines += ["- %s %s" % (str(e.get("time", ""))[:16], e.get("message", "")) for e in events] or ["（无）"]
    if extra:
        lines += ["", "## 补充", ""] + [str(x) for x in extra]
    lines += ["", "---", "已自动去除邮箱、密码、密钥、Cookie、链接参数和用户目录名。提交前请再次检查报告中不含个人信息。"]
    return redact("\n".join(lines), root)


def issue_url(repo_url, title, body):
    """GitHub new-issue link, or None when the repository address has not been filled in (or is not https://github.com/...)."""
    repo = (repo_url or "").strip().rstrip("/")
    if not re.fullmatch(r"https://github\.com/[\w.\-]+/[\w.\-]+", repo):
        return None
    return "%s/issues/new?title=%s&body=%s" % (repo, quote(title[:200]), quote(body[:1500]))
