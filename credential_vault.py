"""Opt-in credentials encrypted for the current Windows user with DPAPI."""
import ctypes
import json
import os
import threading
from pathlib import Path
from ctypes import wintypes


class Blob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_byte))]


def protect(data, decrypt=False):
    if os.name != "nt": raise ValueError("保存密码目前仅支持 Windows 用户加密")
    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    output = Blob()
    dll = ctypes.WinDLL("crypt32", use_last_error=True)
    if decrypt:
        ok = dll.CryptUnprotectData(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output))
    else:
        ok = dll.CryptProtectData(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output))
    if not ok: raise ValueError("本机凭据加密/解密未完成，请重新输入账号密码")
    try:
        return ctypes.string_at(output.data, output.size)
    finally:
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree.restype = ctypes.c_void_p
        kernel.LocalFree(ctypes.cast(output.data, ctypes.c_void_p))


class CredentialVault:
    def __init__(self, root):
        self.file = Path(root) / "credentials.dpapi"
        self.lock = threading.Lock()

    def exists(self): return self.file.is_file()

    def save(self, email, password):
        encrypted = protect(json.dumps(dict(email=email, password=password)).encode())
        with self.lock:
            temp = self.file.with_suffix(".tmp")
            temp.write_bytes(encrypted)
            temp.replace(self.file)

    def load(self):
        with self.lock:
            if not self.exists(): raise ValueError("尚未保存本机凭据")
            try: return json.loads(protect(self.file.read_bytes(), decrypt=True))
            except Exception: raise ValueError("无法读取保存的凭据，请在登录弹窗重新输入") from None

    def forget(self):
        with self.lock:
            self.file.unlink(missing_ok=True)


class AIKeyVault:
    """AI API key encrypted with DPAPI in its own file, separate from the 科研通 credentials. Stores only the key."""
    def __init__(self, root):
        self.file = Path(root) / "ai_key.dpapi"
        self.lock = threading.Lock()

    @staticmethod
    def available(): return os.name == "nt"

    def exists(self): return self.file.is_file()

    def save(self, key):
        encrypted = protect(json.dumps(dict(key=key)).encode())
        with self.lock:
            temp = self.file.with_suffix(".tmp")
            temp.write_bytes(encrypted)
            temp.replace(self.file)

    def load(self):
        with self.lock:
            if not self.exists(): return ""
            try: return str(json.loads(protect(self.file.read_bytes(), decrypt=True)).get("key", ""))
            except Exception: raise ValueError("无法读取已保存的 AI 密钥，请重新输入") from None

    def forget(self):
        with self.lock:
            self.file.unlink(missing_ok=True)


class OpenAlexKeyVault(AIKeyVault):
    """Dedicated DPAPI file; never shares the AI service key."""
    def __init__(self, root):
        self.file = Path(root) / "openalex_key.dpapi"
        self.lock = threading.Lock()

    def load(self):
        try:
            return super().load()
        except ValueError:
            raise ValueError("无法读取已保存的 OpenAlex 密钥，请重新输入") from None
