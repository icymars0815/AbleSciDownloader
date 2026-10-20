"""Start the loopback app once (with its tray icon) and open the window; keep the server console hidden.
    launcher.py               start the service if needed, then open the app window
    launcher.py --background  start only the service and tray icon (used by the optional Startup shortcut)"""
import argparse
import json
import subprocess
import sys
import time
import webbrowser
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

root = Path(__file__).resolve().parent
address = "http://127.0.0.1:8769"
CHROME = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")


def running():
    try:
        with urlopen(address + "/api/state", timeout=1) as response:
            return json.load(response).get("app_id") == "ablesci-local-demo"
    except (URLError, ValueError, TimeoutError, OSError):
        return False


def start_service():
    # Timestamped logs avoid overwriting earlier execution records; they live in data/logs (tray: open log folder).
    stamp = time.strftime("%Y%m%d-%H%M%S")
    logs = root / "data" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    with (logs / f"server-{stamp}.stdout.log").open("ab") as stdout, (logs / f"server-{stamp}.stderr.log").open("ab") as stderr:
        process = subprocess.Popen([sys.executable, str(root / "app.py"), "--tray"], cwd=root,
                                   stdout=stdout, stderr=stderr,
                                   creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
    for _ in range(30):
        if running():
            return
        if process.poll() is not None:
            raise SystemExit("无法启动工作台，端口可能已被占用或已有实例在运行。请查看 data/logs 中的 server 日志。")
        time.sleep(.3)
    raise SystemExit("启动超时，请查看 data/logs 中的 server 日志。")


def open_window(target=None):
    """Open the app window; target may be the panel URL, e.g. with an #item=<id> fragment."""
    target = target or address
    if sys.platform == "win32" and CHROME.exists():
        subprocess.Popen([str(CHROME), "--app=" + target, "--user-data-dir=" + str(root / "data" / "app-ui-profile"),
                          "--disable-extensions", "--window-size=1440,1000"],
                         creationflags=subprocess.CREATE_NO_WINDOW)
    else:
        webbrowser.open(target)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--background", action="store_true", help="only start the service and tray icon; no window")
    args = parser.parse_args(argv)
    if not running():
        start_service()
    if not args.background:
        open_window()


if __name__ == "__main__":
    main()
