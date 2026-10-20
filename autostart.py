"""Optional start-with-Windows: a shortcut in the user's Startup folder (shell:startup) that runs
pythonw.exe from the project .venv with "launcher.py --background". No registry, no administrator rights; default OFF."""
import os
import sys
from pathlib import Path

LINK_NAME = "KYTDL Background Monitor.lnk"   # ASCII: WScript.Shell mangles non-ASCII shortcut names
CMD_NAME = "KYTDL Background Monitor.cmd"


def default_startup_dir():
    base = os.environ.get("APPDATA")
    if not base:
        raise OSError("找不到 APPDATA，无法定位“启动”文件夹")
    return Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


class Autostart:
    def __init__(self, root, startup_dir=None, allowed=True):
        self.root = Path(root)
        self.startup_dir = Path(startup_dir) if startup_dir else None
        self.last_error = ""
        self.allowed = allowed   # False for isolated servers: they must never touch the real Startup folder

    def folder(self):
        return self.startup_dir or default_startup_dir()

    def paths(self):
        folder = self.folder()
        return folder / LINK_NAME, folder / CMD_NAME

    def target(self):
        return self.root / ".venv" / "Scripts" / "pythonw.exe"

    def enabled(self):
        if not self.allowed:
            return False
        try:
            return any(p.exists() for p in self.paths())
        except OSError:
            return False

    def enable(self):
        if not self.allowed:
            raise ValueError("当前为隔离运行环境，不修改“启动”文件夹")
        folder = self.folder()
        folder.mkdir(parents=True, exist_ok=True)
        link, cmd = self.paths()
        try:
            self._shortcut(link)
            cmd.unlink(missing_ok=True)
            return "lnk"
        except Exception as exc:
            self.last_error = repr(exc)
            # pywin32/COM unavailable: a tiny .cmd in the same folder starts the same command without a console window.
            line = '@echo off\r\ncd /d "%s"\r\nstart "" "%s" launcher.py --background\r\n' % (self.root, self.target())
            cmd.write_bytes(line.encode("mbcs" if sys.platform == "win32" else "utf-8"))
            link.unlink(missing_ok=True)
            return "cmd"

    def disable(self):
        if not self.allowed:
            raise ValueError("当前为隔离运行环境，不修改“启动”文件夹")
        for path in self.paths():
            path.unlink(missing_ok=True)

    def _shortcut(self, link):
        import pythoncom   # pywin32; HTTP handler threads have no COM apartment yet
        import win32com.client
        pythoncom.CoInitialize()
        try:
            shell = win32com.client.Dispatch("WScript.Shell")
            shortcut = shell.CreateShortCut(str(link))
            shortcut.TargetPath = str(self.target())
            shortcut.Arguments = "launcher.py --background"
            shortcut.WorkingDirectory = str(self.root)
            shortcut.Description = "科研通下载器 (KYTDL) background monitor (service and tray only, no window)"
            shortcut.WindowStyle = 7
            shortcut.save()
        finally:
            pythoncom.CoUninitialize()
        if not link.exists():
            raise OSError("shortcut was not created")
