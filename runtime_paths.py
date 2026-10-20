"""Resolve portable runtimes without changing system Python or browser settings."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def python_executable(root=ROOT, windowed=False):
    root = Path(root)
    name = "pythonw.exe" if windowed else "python.exe"
    portable = root / "runtime" / "python" / name
    return portable if portable.is_file() else root / ".venv" / "Scripts" / name


def bundled_browser(root=ROOT):
    folder = Path(root) / "runtime" / "browsers"
    for pattern in ("chromium-*/chrome-win64/chrome.exe", "chromium-*/chrome-win/chrome.exe"):
        for candidate in sorted(folder.glob(pattern), reverse=True):
            if candidate.is_file():
                return candidate
    return None


def browser_options(root=ROOT):
    browser = bundled_browser(root)
    return {"executable_path": str(browser)} if browser else {"channel": "chrome"}
