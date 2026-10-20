"""Account aliases and active selection. Passwords stay in each DPAPI vault."""
import json
import re
import threading
import uuid
from pathlib import Path
from credential_vault import CredentialVault
from core import read_json_with_backup, write_json_durable


class Accounts:
    def __init__(self, root):
        self.root = Path(root)
        self.file = self.root / "accounts.json"
        self.lock = threading.RLock()
        self.recovered = False
        if self.file.exists():
            self.data, self.recovered = read_json_with_backup(self.file)
        else:
            self.data = {"active_id": "default", "accounts": [{"id": "default", "label": "默认账户"}]}
            self.save()
        if self.recovered:
            self.save()

    @property
    def active_id(self):
        with self.lock: return self.data["active_id"]

    def save(self):
        write_json_durable(self.file, self.data)

    def account_root(self, account_id=None):
        account_id = account_id or self.active_id
        if not any(a["id"] == account_id for a in self.data["accounts"]):
            raise ValueError("未找到此账户")
        # Keep the existing cookie/profile and encrypted credentials in place.
        path = self.root if account_id == "default" else self.root / "accounts" / account_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def vault(self, account_id=None): return CredentialVault(self.account_root(account_id))

    def public(self):
        with self.lock:
            return [dict(a, credentials_saved=self.vault(a["id"]).exists(), active=a["id"] == self.active_id)
                    for a in self.data["accounts"]]

    def add(self, label):
        label = str(label).strip()
        if not label or len(label) > 60: raise ValueError("请填写 1～60 字的账户名称")
        with self.lock:
            if any(a["label"] == label for a in self.data["accounts"]): raise ValueError("账户名称已存在")
            account = dict(id=uuid.uuid4().hex[:12], label=label)
            self.data["accounts"].append(account)
            self.save()
            return account["id"]

    def switch(self, account_id):
        with self.lock:
            self.account_root(account_id)
            self.data["active_id"] = account_id
            self.save()

    def has_cookie_file(self, account_id=None):
        """True if the account's persistent browser profile already holds a cookie database (no network call)."""
        try:
            base = self.account_root(account_id) / "browser-profile" / "Default"
            return any((base / name).is_file() and (base / name).stat().st_size > 0 for name in ("Network/Cookies", "Cookies"))
        except (ValueError, OSError):
            return False
