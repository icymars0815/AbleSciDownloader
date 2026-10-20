"""Single-instance lock file (data/.kytdl.lock holding the PID) with stale-lock detection.
It complements the port probe: two services must never drive the same account at once."""
import os
import sys
from pathlib import Path


class AlreadyRunning(RuntimeError):
    def __init__(self, pid):
        super().__init__("科研通下载器 已在运行（PID %s）" % pid)
        self.pid = pid


def pid_alive(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        kernel = ctypes.windll.kernel32
        kernel.OpenProcess.restype = ctypes.c_void_p
        handle = kernel.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.GetLastError() == 5   # access denied: the process exists
        try:
            code = ctypes.c_ulong()
            return bool(kernel.GetExitCodeProcess(ctypes.c_void_p(handle), ctypes.byref(code))) and code.value == 259   # STILL_ACTIVE
        finally:
            kernel.CloseHandle(ctypes.c_void_p(handle))
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class InstanceLock:
    def __init__(self, path, pid=None, alive=pid_alive):
        self.path = Path(path)
        self.pid = pid or os.getpid()
        self.alive = alive
        self.held = False

    def owner(self):
        try:
            return int(self.path.read_text("ascii").strip())
        except (OSError, ValueError):
            return None

    def acquire(self):
        """Take the lock or raise AlreadyRunning. A lock whose PID is gone (crash, power loss) is replaced."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(3):
            try:
                with open(self.path, "x", encoding="ascii") as handle:
                    handle.write(str(self.pid))
                self.held = True
                return self
            except FileExistsError:
                owner = self.owner()
                if owner == self.pid:
                    self.held = True
                    return self
                if owner is not None and self.alive(owner):
                    raise AlreadyRunning(owner)
                try:
                    self.path.unlink()   # stale or unreadable
                except FileNotFoundError:
                    pass
        raise AlreadyRunning(self.owner())

    def release(self):
        if self.held and self.owner() == self.pid:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
        self.held = False
