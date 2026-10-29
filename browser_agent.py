"""Visible, isolated browser. Website publication and accept/reject are manual."""
import contextlib
import queue
import re
import threading
import applog
from runtime_paths import browser_options
import time
from urllib.parse import urljoin
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlparse
from download_policy import (make_watch, read_progress, highspeed_allowed, download_settings, resolve_highspeed,
                             takeover_reason, backoff_minutes, dynamic_cap_for, longer_minutes)
import deletion
from download_lines import (LINES, SpeedHistory, SwitchWatch, select_line, order_after, eta_seconds, plan_text, fmt_size,
                            fmt_duration, explain_failure, safe_filename, HIGHSPEED)
import intake
import publish_policy as pp

from core import doi_value, normalize, site_url, verify_pdf, checkin_result, now
from metadata import verify_with_metadata, fetch_metadata
from rejection_policy import rejection_decision, CATEGORY_KEYS
from accounts import Accounts
from pipeline import stage_of, gate, tier, item_reward, WAITING, FULFILLED, DONE
import hashlib
from ratelimit import RateLimiter, limit_settings

BASE = "https://www.ablesci.com"


def _count(locator):
    """Playwright count() as an int (mock pages may return non-ints, treated as 0)."""
    try:
        n = locator.count()
    except Exception:
        return 0
    return n if isinstance(n, int) else 0


class SessionExpired(ValueError):
    """The 科研通 session is gone (redirected to the login page)."""


class PreconditionError(ValueError):
    """A command refused by its own precondition: it did nothing, so the item's status must not change."""


class TransientError(ValueError):
    """A temporary network/site problem (timeout, busy site). Never pauses the pipeline; the monitor backs off."""


class RetryScheduled(ValueError):
    """A download did not finish but will be retried automatically; the item must NOT go to attention."""


OPEN_TIMEOUT_TEXT = "科研通页面在 45 秒内未能打开，可能较为繁忙，请稍后重试"


def is_transient(exc):
    """Network-ish failures that say nothing about the account or the workflow."""
    if isinstance(exc, (TransientError, TimeoutError, ConnectionError)):
        return True
    if any(c.__name__ == "TimeoutError" for c in type(exc).__mro__):   # playwright's TimeoutError
        return True
    if any(c.__name__ == "TargetClosedError" for c in type(exc).__mro__):   # browser was closed (idle close / crash): reopen
        return True
    return "net::ERR_" in str(exc) or "has been closed" in str(exc)


class BrowserAgent:
    def __init__(self, store, accounts=None, limiter=None, on_done=None, clock=time.monotonic, use_worker=False):
        self.store = store
        # One limiter for every 科研通 navigation/request: >= 3 s apart (3–30 s, preference request_gap_seconds).
        self.limiter = limiter or RateLimiter(lambda: limit_settings(store.snapshot().get("preferences", {}))["request_gap_seconds"])
        self.on_done = on_done          # callback(action, item_id, error) after each command; used by the monitor
        self.clock = clock
        self.last_activity = clock()
        self.accounts = accounts or Accounts(store.root)
        self.commands = queue.Queue()
        self.stop = threading.Event()
        self.busy = False
        self._enqueue_lock = threading.Lock()
        self._closed = False
        self.context = None
        self.page = None
        self.worker = None
        self._state_waiter = None
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()
        if use_worker:
            from download_worker import DownloadWorker
            self.worker = DownloadWorker(self)

    def request_stop(self):
        """Stop the current command and any download running in the dedicated worker."""
        self.stop.set()
        worker = getattr(self, "worker", None)
        if worker:
            worker.stop.set()

    def download_busy(self):
        worker = getattr(self, "worker", None)
        return bool(worker and worker.busy)

    def worker_status(self):
        worker = getattr(self, "worker", None)
        if not worker:
            return dict(enabled=False, status="idle", item_id="")
        return dict(enabled=True, status="downloading" if worker.busy else "idle", item_id=worker.current_item if worker.busy else "")

    def revive(self):
        """Watchdog: restart the worker thread if it died unexpectedly (not after a deliberate close). True if restarted."""
        thread = getattr(self, "thread", None)
        if thread is None or thread.is_alive() or getattr(self, "_closed", False):
            return False
        if self.commands.empty():
            self.busy = False
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()
        applog.record(self.store, "browser", "revive", result="warn", message="browser agent thread was not alive; restarted")
        try:
            self.store.event("后台浏览器线程意外停止，已自动重启。如反复出现，请生成错误报告。", kind="error")
        except Exception:
            pass
        return True

    def assert_mcp_authority(self):
        """Recheck queued/in-flight MCP work at the point of a site action."""
        scope = getattr(self, "_mcp_scope", None)
        if not scope:
            return
        prefs = self.store.snapshot().get("preferences", {})
        active = self.accounts.active_id
        if (self.stop.is_set() or active != scope["account_id"] or not prefs.get("mcp_website_actions")
                or prefs.get("mcp_actions_account", "default") != active):
            raise PreconditionError("MCP 后台操作已停止或授权已撤销，未继续操作")
        if scope.get("auto") and scope.get("item_id"):
            item = self.store.get(scope["item_id"]) or {}
            if (item.get("account_id", "default") != active or item.get("request_confirmed") is not True
                    or item.get("request_mcp_auto_publish") is not True):
                raise PreconditionError("该文献的 MCP 后台续办已暂停，请主动恢复后继续")

    @contextlib.contextmanager
    def throttled(self):
        """Context manager for one 科研通 navigation/request (agents built without __init__ in tests skip it)."""
        limiter = getattr(self, "limiter", None)
        with limiter.slot(self.stop) if limiter else contextlib.nullcontext():
            self.assert_mcp_authority()
            yield

    def has_browser(self):
        return bool(self.context)

    def enqueue(self, action, item_id=None, credentials=None):
        # Check-and-put is atomic and the agent counts as busy from this moment (run() resets it when the command ends).
        lock = self.__dict__.setdefault("_enqueue_lock", threading.Lock())
        if getattr(self, "worker", None) and self.worker.busy:
            if action == "download":
                raise ValueError("已有下载在进行，同一时间只下载一篇；请等它完成或停止")
            if action in ("login", "switch_account", "close_idle"):
                raise ValueError("下载进行中，暂不能登录或切换账户；请等下载完成或先停止")
        with lock:
            if self.busy or not self.commands.empty():
                raise ValueError("已有任务运行，请等待完成或停止后再操作")
            self.stop.clear()
            self.busy = True
            self.commands.put((action, item_id, credentials))

    def run(self):
        while True:
            action, item_id, credentials = self.commands.get()
            self.busy = True
            error = None
            self._mcp_scope = credentials.get("mcp_scope") if isinstance(credentials, dict) else None
            try:
                if action != "close":
                    self.assert_mcp_authority()
                if action == "close":
                    self._closed = True
                    if getattr(self, "worker", None):
                        self.worker.shutdown()
                    try:
                        if self.context:
                            self.context.close()
                    except Exception:
                        pass
                    finally:
                        if hasattr(self, "playwright"):
                            self.playwright.stop()
                    return
                elif action == "login":
                    self.login(item_id, credentials)
                elif action == "download" and getattr(self, "worker", None):
                    self.hand_over_download(item_id)
                else:
                    getattr(self, action)(item_id)
            except Exception as exc:
                error = exc
                try:
                    self.handle_failure(action, item_id, exc)
                except Exception as inner:   # a failing store write must never kill the worker thread
                    try:
                        applog.record(self.store, "browser", "handle_failure", inner, item_id=item_id or "")
                    except Exception:
                        pass
            finally:
                credentials = None
                self._mcp_scope = None
                self.busy = False
                try:
                    if action != "close_idle":
                        self.last_activity = self.clock()
                    self.commands.task_done()
                except Exception:
                    pass
                if self.on_done and action != "close":
                    try:
                        self.on_done(action, item_id, error)
                    except Exception:
                        pass

    def handle_failure(self, action, item_id, exc):
        applog.record(self.store, "browser", action, exc, item_id=item_id or "")
        known = bool(item_id and self.store.get(item_id))
        if isinstance(exc, PreconditionError):
            # Refused before doing anything: say so, but leave the item's status exactly as it was.
            self.store.event(str(exc), item_id=item_id if known else None, kind="error", action="focus" if known else "")
            return
        if isinstance(exc, RetryScheduled):
            # download() already stored status "retrying" + download_retry; just tell the person.
            self.store.event(str(exc), item_id=item_id if known else None, kind="download", action="focus" if known else "")
            return
        if self.page:
            try:
                screenshot_folder = self.store.root / "screenshots"
                screenshot_folder.mkdir(exist_ok=True)
                self.page.screenshot(path=str(screenshot_folder / "browser-last.png"), mask=[self.page.locator("input")])
            except Exception:
                pass
        # Do not log third-party response bodies, URLs with tokens or credentials.
        message = str(exc) if isinstance(exc, ValueError) else "浏览器操作未完成（登录失效、页面变化或网络超时），请查看独立浏览器窗口后重试"
        self.store.event(message, item_id=item_id if known else None, kind="error", action="focus" if item_id else "")
        if known:
            self.store.update(item_id, status="attention", note=message)
        elif is_transient(exc):
            self.store.event("网络或科研通暂时无法访问，已记录。自动流程保持开启，稍后由后台监控自动重试。")
        elif self.store.snapshot().get("preferences", {}).get("pipeline_enabled"):
            self.store.update_preferences(pipeline_enabled=False)
            self.store.event("自动流程步骤失败，已自动暂停。请处理后重新开启。")

    def close_context(self):
        # Closing an already-closed browser (idle close, crash) must never fail (real 2026-10-20: a scheduled retry was
        # handed to the human because of TargetClosedError here).
        try:
            if self.context:
                self.context.close()
        except Exception:
            pass
        self.context = None
        self.page = None

    def close_idle(self, _=None):
        """Release the headless browser after a quiet period; the next sync starts it again. Never closes a login window."""
        if self.context and getattr(self, "interactive", False):
            return
        self.close_context()
        if hasattr(self, "playwright"):
            try:
                self.playwright.stop()
            except Exception:
                pass
            del self.playwright

    def switch_account(self, account_id):
        self.close_context()
        self.accounts.switch(account_id)
        self.store.update_preferences(pipeline_enabled=False)
        self.store.event("已切换账户。各账户的登录状态和求助记录相互独立，未打开登录窗口。")

    def assert_account(self, item):
        if item.get("account_id", "default") != self.accounts.active_id:
            raise PreconditionError("该任务属于另一个账户，请先切换账户")

    def ensure(self, interactive=False):
        if self.context and getattr(self, "interactive", False) != interactive:
            if not interactive and "/site/login" in self.page.url:
                raise ValueError("请先在登录窗口完成扫码或验证码，再回到软件同步")
            self.close_context()
        if self.context:
            try:
                if self.page.is_closed():
                    self.page = self.context.new_page()
                return
            except Exception:
                self.context = None
        from playwright.sync_api import sync_playwright
        if hasattr(self, "playwright"):
            try:
                self.playwright.stop()
            except Exception:
                pass
        self.playwright = sync_playwright().start()
        self.interactive = interactive
        self.context = self.playwright.chromium.launch_persistent_context(
            str(self.accounts.account_root() / "browser-profile"), **browser_options(), headless=not interactive,
            args=["--app=" + BASE + "/site/login", "--window-size=1080,850"] if interactive else [],
            accept_downloads=True, viewport={"width": 1080, "height": 850})
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        self.page.set_default_timeout(12000)

    def open_page(self, url, timeout=45000):
        """page.goto with a plain-language message when the site does not answer in time."""
        try:
            self.page.goto(url, wait_until="domcontentloaded", timeout=timeout)
        except Exception as exc:
            if any(c.__name__ == "TimeoutError" for c in type(exc).__mro__):
                raise TransientError(OPEN_TIMEOUT_TEXT) from exc
            raise

    def goto(self, path, interactive=False):
        self.ensure(interactive=interactive)
        with self.throttled():
            self.open_page(urljoin(BASE, path))
        if "/site/login" in self.page.url:
            raise SessionExpired("账户登录状态已失效，请重新登录。后台操作不会自动弹出窗口")

    def login(self, _, credentials=None):
        self.ensure(interactive=True)
        with self.throttled():
            self.open_page(BASE + "/site/login")
        if not credentials:
            self.store.event("已打开独立浏览器，可自行登录，也可在软件中输入账号密码自动登录。")
            return
        if "/site/login" not in self.page.url:
            self.store.update_account(account_id=self.accounts.active_id, last_login_time=now())
            self.store.event("独立浏览器已有登录状态，可同步本人求助。")
            return
        self.page.get_by_role("combobox", name="登录邮箱（必填）").fill(credentials["email"])
        self.page.get_by_role("textbox", name="登录密码（必填）").fill(credentials["password"])
        self.page.get_by_role("button", name="登录", exact=True).click()
        try:
            self.page.wait_for_url(lambda url: "/site/login" not in str(url), timeout=45000)
        except Exception:
            body = self.page.locator("body").inner_text()
            if "邮箱或密码错误" in body:
                raise ValueError("登录失败：邮箱或密码错误。请核对后重试。")
            raise ValueError("登录未完成，请查看独立浏览器中的验证码、验证提示或网络状态。软件不会自动重试密码。")
        if "/site/" in self.page.url:
            raise ValueError("登录需要额外确认，请在独立浏览器中完成后再同步")
        self.store.update_account(account_id=self.accounts.active_id, last_login_time=now())
        self.store.event("账号密码登录成功，登录状态仅保留在本机独立浏览器中。可同步本人求助。")

    def signin(self, _):
        self.goto("/")
        with self.throttled():
            response = self.context.request.get(BASE + "/user/sign", timeout=30000)
        if not response.ok:
            raise ValueError("签到未正常返回，请查看科研通页面。未自动重试")
        try:
            result = response.json()
        except Exception:
            raise ValueError("签到未返回有效结果，请检查登录状态或验证提示")
        status = checkin_result(result)
        self.goto("/")
        balance = re.search(r"当前拥有\s*(\d+)\s*积分", self.page.locator("body").inner_text())
        fields = {"last_checkin_result": status, "last_checkin_time": now()}
        if balance:
            fields["points"] = int(balance.group(1))
        self.store.update_account(account_id=self.accounts.active_id, **fields)
        self.store.event(status + (f"，当前 {fields['points']} 积分。" if balance else "。"))

    def read_points(self):
        match = re.search(r"(?:当前拥有|您当前有|当前有|我的积分)[\s:：]*(\d+)\s*(?:积分)?", self.page.locator("body").inner_text(timeout=5000))
        return int(match.group(1)) if match else None

    def sync(self, _):
        # Record the attempt first so a failing sync is not retried on every scheduler tick.
        self.store.update_account(account_id=self.accounts.active_id, last_sync_time=now())
        self.goto("/my/assist-my")
        self.page.locator("table").wait_for()
        try:
            points = self.read_points()
        except Exception:
            points = None
        if points is not None:
            self.store.update_account(account_id=self.accounts.active_id, points=points)
        count = 0
        for row in self.page.locator("table tr").all():
            link = row.locator('a[href*="/assist/detail?id="]')
            if not link.count():
                continue
            title = link.first.inner_text().strip()
            url = urljoin(BASE, link.first.get_attribute("href"))
            if not site_url(url, "/assist/detail"):
                continue
            state = row.locator("td").last.inner_text().strip()
            # A request this app published (possibly with an unconfirmed result) is adopted by title, not duplicated.
            mine = self.match_published(title, url)
            if mine:
                self.store.update(mine, source="website", owned=True, assist_url=url, status="published",
                                  publish_action=dict(self.store.get(mine).get("publish_action") or {}, status="confirmed"))
            item_id = mine or self.store.add(title, "", "website", account_id=self.accounts.active_id, assist_url=url, owned=True, website_status=state)
            self.store.update(item_id, website_status=state, website_status_time=now())
            # The answer's arrival time is stamped once (deadline base for auto-adopt / 2-hour takeover); a new answer after
            # a rejection (status back to waiting) gets a fresh stamp.
            current = self.store.get(item_id) or {}
            if state in FULFILLED and not current.get("fulfilled_at"):
                self.store.update(item_id, fulfilled_at=now())
            elif state in WAITING and current.get("fulfilled_at"):
                self.store.update(item_id, fulfilled_at=None)
            self.resolve_handover(item_id, state)
            count += 1
        self.store.event(f"已同步本页 {count} 条求助，未打开登录窗口。")

    def resolve_handover(self, item_id, state):
        """After the human handled a blocked adopt/reject on the site, the synced status tells what they did."""
        item = self.store.get(item_id)
        if not item or not item.get("handover") or item.get("status") != "attention" or state in FULFILLED:
            return
        if state in DONE:
            self.store.update(item_id, status="adopted", handover=dict(item["handover"], resolved=now(), outcome="adopted_on_site"))
            self.store.event("已同步：《%s》已在科研通处理（已完结）。" % (item.get("title") or "")[:40], item_id=item_id, kind="adopt")
        elif state in WAITING:
            archived = dict(pdf=item.get("pdf", ""), verification=item.get("verification"), download_file_url=item.get("download_file_url", ""),
                            rejected_at=now(), by="human_on_site")
            self.store.update(item_id, status="rejected", pdf="", verification=None, download_file_url="", rejected_at=archived["rejected_at"],
                              rejected_files=list(item.get("rejected_files", [])) + [archived],
                              handover=dict(item["handover"], resolved=now(), outcome="rejected_on_site"))
            self.store.event("已同步：《%s》已在科研通驳回，求助重新等待应助，本地文件已归档保留。" % (item.get("title") or "")[:40], item_id=item_id, kind="reject")

    def match_published(self, title, url):
        """Local import item this app tried to publish whose title matches a synced row (and has no site URL yet)."""
        key = normalize(title)
        for item in self.store.snapshot()["items"]:
            if (item.get("source") == "import" and item.get("account_id", "default") == self.accounts.active_id
                    and not item.get("assist_url") and (item.get("publish_action") or {}).get("status") in ("attempted", "uncertain", "confirmed")
                    and key and normalize(item.get("title", "")) == key):
                return item["id"]
        return None

    def wait_published(self, timeout_s=25):
        """After a code-0 submit the site shows a success layer with 查看求助详情 instead of always navigating."""
        page = self.page
        for _ in range(timeout_s * 2):
            if site_url(str(page.url), "/assist/detail"):
                return True
            for layer, text in self.visible_layers():
                if "查看求助详情" in text:
                    button = layer.locator("a, button, span").filter(has_text=re.compile(r"^\s*查看求助详情\s*$"))
                    if button.count() >= 1:
                        try:
                            button.first.click()
                        except Exception:
                            pass
            page.wait_for_timeout(500)
        return site_url(str(page.url), "/assist/detail")

    def prepare(self, item_id):
        item = self.store.get(item_id)
        if not item or item["source"] == "demo":
            raise PreconditionError("请先导入真实 DOI 或标题。演示数据不能提交到科研通")
        self.assert_account(item)
        self.goto("/assist/create", interactive=True)
        if item["doi"]:
            self.page.locator('[id="Assist-doi"]').fill(item["doi"])
        if item["title"]:
            self.page.locator('[id="Assist-title"]').fill(item["title"])
        self.store.update(item_id, status="prepared", note="已填入现有信息。请在求助页面提取并核对文献信息后自行发布，软件未提交。")
        self.store.event("已填写求助表单，尚未发布。")

    def review(self, item_id):
        item = self.store.get(item_id)
        if not item or not site_url(item.get("assist_url", ""), "/assist/detail"):
            raise PreconditionError("该条尚无已同步的求助链接，请先同步本人记录")
        self.assert_account(item)
        self.goto(item["assist_url"], interactive=True)
        self.store.event("已打开求助详情。请结合核对结果自行采纳或驳回，软件未执行这两类操作。")

    def export_state(self, _=None):
        """Runs on THIS agent's thread: the session's cookies/storage as an in-memory dict (never written to disk)."""
        self.ensure()
        return self.context.storage_state()

    def serve_state_request(self, _=None):
        waiter = self._state_waiter
        if waiter is None:
            return
        try:
            waiter["state"] = self.export_state()
        except Exception as exc:
            waiter["error"] = exc
        waiter["event"].set()

    def fetch_state(self, stop, timeout=120):
        """Called from the worker thread: ask the main agent (on its own thread) for a fresh storage_state.
        Returns None when the main agent stays busy, fails, or a stop is requested."""
        waiter = dict(event=threading.Event(), state=None, error=None)
        deadline = time.monotonic() + timeout
        while True:
            if stop.is_set() or time.monotonic() > deadline:
                return None
            try:
                self._state_waiter = waiter
                self.enqueue("serve_state_request")
                break
            except ValueError:
                stop.wait(0.5)
        while not waiter["event"].wait(0.5):
            if stop.is_set() or time.monotonic() > deadline:
                return None
        return waiter["state"]

    def hand_over_download(self, item_id):
        """O1: the download runs on the dedicated worker so this agent stays free for sync/publish/adopt/reject.
        Cheap preconditions and the session export happen here, on this agent's own thread."""
        item = self.store.get(item_id)
        if not item or not item.get("owned") or item["source"] != "website":
            raise PreconditionError("只下载从本人求助列表同步的文件")
        self.assert_account(item)
        if item.get("verification") or item.get("pdf"):
            raise PreconditionError("该条目已有本地 PDF，不会重复下载（避免重复扣分）；请先在本地复核")
        state = self.export_state()
        scope = getattr(self, "_mcp_scope", None)
        if scope:
            self.worker.submit(item_id, state, authority={"mcp_scope": scope})
        else:
            self.worker.submit(item_id, state)

    def download(self, item_id):
        item = self.store.get(item_id)
        if not item or not item.get("owned") or item["source"] != "website":
            raise PreconditionError("仅可下载本人求助列表中同步的文件")
        self.assert_account(item)
        if item.get("verification") or item.get("pdf"):
            raise PreconditionError("该条目已有本地 PDF，不会重复下载（避免重复扣分）。请先在本地复核")
        self.goto(item["assist_url"])
        table = self.page.locator("table").first
        table.wait_for()
        text = table.inner_text()
        title_row = table.locator("tr").filter(has=self.page.locator("td", has_text=re.compile(r"^标题$")))
        actual_title = title_row.locator("td").last.inner_text().strip().splitlines()[0].strip() if title_row.count() else item["title"]
        doi_row = table.locator("tr").filter(has=self.page.locator("td", has_text=re.compile(r"^DOI$")))
        actual_doi = doi_value(doi_row.inner_text()) if doi_row.count() else ""
        if item["doi"] and actual_doi != item["doi"]:
            raise ValueError("求助详情 DOI 与任务不符，已停止")
        link = table.locator('a[href*="/assist/download?id="]')
        if not link.count():
            link = self.page.locator('a[href*="/assist/download?id="]')
        if not link.count():
            raise ValueError("尚无可下载的文件，请待有人应助后重试")
        url = urljoin(BASE, link.first.get_attribute("href"))
        if not site_url(url, "/assist/download"):
            raise ValueError("下载入口格式变化，已停止")
        # The download page starts automatically. Check paid auto-routing BEFORE opening it.
        self.goto("/my/assist-my-set")
        selected = self.page.locator('input[name="file_highspeed"]:checked')
        if selected.count() != 1 or selected.get_attribute("value") != "0":
            raise ValueError("科研通的“智能高速下载”未确认关闭。为避免额外扣分，请自行核对科研通的设置；软件不会修改该设置。")
        preferences = self.store.snapshot().get("preferences", {})
        settings = download_settings(preferences)
        try:
            fresh = self.read_points()
        except Exception:
            fresh = None
        if fresh is not None:
            self.store.update_account(account_id=self.accounts.active_id, points=fresh)
        points = fresh if fresh is not None else self.store.snapshot().get("account_metrics", {}).get(self.accounts.active_id, {}).get("points")
        mode, _source, changes, message = resolve_highspeed(preferences, points)
        if changes:
            self.store.update_preferences(**changes)
        if message:
            self.store.event(message)
        file_id = (parse_qs(urlparse(url).query).get("id") or [""])[0]
        extra = {}
        if actual_doi and not item.get("doi"):
            extra["doi_origin"] = "site"      # filled in by the site's smart extraction, not by the person
        self.store.update(item_id, status="downloading", doi=actual_doi, title=actual_title, download_progress=None, download_failure=None,
                          download_file_url=url, download_attempts=[], note="正在连接科研通普通下载", **extra)
        self._record_ctx = None
        pending, attached, failures = [], [], []
        ctx = dict(transient=False, hs_allowed=None, fee_mismatch=False, mode=mode)

        def bump(field):
            value = int((self.store.get(item_id) or {}).get(field) or 0) + 1
            self.store.update(item_id, **{field: value})
            return value
        def download_event(download):
            pending.append(download)
        observed = []
        def observe(response):
            # D1: passive, read-only record of how the file server answers (resume support); query strings dropped
            try:
                headers = response.headers
                kind = headers.get("content-type", "")
                size = int(headers.get("content-length") or 0)
                ranged = "content-range" in headers or response.status == 206 or "range" in (response.request.headers or {})
                filelike = "pdf" in kind or "octet-stream" in kind or size > 1_000_000
                if not (ranged or filelike) or re.search(r"javascript|css|image/|font/|text/html", kind) and not ranged:
                    return
                if len(observed) < 40:
                    parts = urlparse(response.url)
                    observed.append(dict(url=parts.netloc + parts.path, status=response.status, accept_ranges=headers.get("accept-ranges"),
                                         content_range=headers.get("content-range"), length=size or None, type=kind[:60],
                                         request_range=(response.request.headers or {}).get("range"), time=now()))
            except Exception:
                pass
        def attach(page):
            page.on("download", download_event)
            try:
                page.on("response", observe)
            except Exception:
                pass
            attached.append(page)
        for page in self.context.pages:
            attach(page)
        self.context.on("page", attach)
        try:
            self.goto(url)
            self.page.get_by_role("progressbar", name="下载进度").wait_for(timeout=20000)
            def active_line(n):
                control = self.line_control(n)
                if control.get_attribute("aria-pressed") == "true" or control.get_attribute("aria-checked") == "true":
                    return True
                try:
                    return control.is_checked()
                except Exception:
                    return False
            attempts, reasons = [], []
            # The ">= 500 points, no click-through" waiver may only rely on points read live for THIS
            # download; a stored balance can be stale and the user may now be charged.
            live_points = fresh
            paid_before = file_id and any(p.get("file_id") == file_id for p in (self.store.get(item_id).get("highspeed_paid") or []))
            history = SpeedHistory(self.store)
            hist_now = history.all() if settings["probe_enabled"] else {}
            self.report_plan(item_id, hist_now, settings, active_line)
            force_ok = False
            if mode == "force":
                force_ok, force_reason = highspeed_allowed([], preferences, self.page.locator("body").inner_text(), mode="force", points=live_points)
                ctx["hs_allowed"] = force_ok
                ctx["fee_mismatch"] = (not force_ok) and "费用规则" in force_reason
                if force_ok:
                    self.store.event("强制高速：费用规则核对通过，跳过普通线路。")
                else:
                    self.store.event("强制高速未执行（" + force_reason + "），改走普通线路，未扣分。")
            def run_normal_lines():
                current = next((n for n in LINES if active_line(n)), 1)
                order, active = [current] + [n for n in LINES if n != current], current
                if settings["probe_enabled"]:
                    order, active = self.choose_line(item_id, current, settings, pending, history)
                queue = list(order)
                if mode == "smart" and not pending and self.hs_early(item_id, settings, history, hist_now):
                    ctx["early_hs"] = True
                    queue = []
                while queue and not pending:
                    line = queue.pop(0)
                    if self.stop.is_set():
                        raise ValueError("下载已停止，未执行采纳或驳回")
                    if line != active:
                        self.line_control(line).click()
                        active = line
                    self.store.event(f"正在尝试普通线路 {line}；有持续进度时继续等待，停止接收 {settings['stall_seconds']} 秒后切换。", item_id=item_id, kind="download")
                    self._line_ctx = dict(line=line, hist=hist_now, history=history, probe=settings["probe_enabled"])
                    self._switch_target = None
                    outcome = self.wait_transfer(item_id, f"普通线路 {line}", pending, settings, eta=mode == "smart")
                    attempts.append(dict(line=line, outcome=outcome, time=now(), mode=mode, reason="" if outcome == "complete" else outcome))
                    self.store.update(item_id, download_attempts=attempts)
                    if outcome == "complete":
                        break
                    failures.append(line)
                    reasons.append(outcome)
                    bump("normal_failures")
                    explanation = {"stalled": f"{settings['stall_seconds']} 秒未继续接收", "error": "页面提示失败", "timeout": "等待时间过长",
                                   "eta": "按当前速度预计超出单线路上限", "switch": "明显比另一条线路慢"}.get(outcome, "未完成")
                    target = self._switch_target if outcome == "switch" else None
                    if target in queue:
                        queue.remove(target)
                        queue.insert(0, target)
                    self.store.event(f"普通线路 {line} 未完成（{explanation}），将检查下一条线路。", item_id=item_id, kind="download")
            def patient_best_line():
                """Every free line was dropped only for being slow ("eta"): waiting on the fastest beats giving up on all."""
                if pending or not reasons or any(r != "eta" for r in reasons) or self.stop.is_set():
                    return
                if mode == "smart" and self.hs_early(item_id, settings, history, hist_now, after_lines=True):
                    ctx["early_hs"] = True
                    return
                measured = ((self.store.get(item_id) or {}).get("line_probe") or {}).get("measured") or {}
                speeds = {int(k): v for k, v in measured.items() if str(k).isdigit()}
                best = max(speeds, key=speeds.get) if speeds else LINES[0]
                self.store.event(f"三条普通线路都很慢；改为耐心等最快的线路 {best} 下完（只按停滞和时间上限判断）。", item_id=item_id, kind="download")
                self.line_control(best).click()
                self._line_ctx = None            # patient mode: no mid-download switching
                # Last free option: give it the same longer limit as a paid high-speed attempt (real 2026-10-20: 10 min was not enough).
                patient = dict(settings, line_max_minutes=longer_minutes(settings))
                outcome = self.wait_transfer(item_id, f"普通线路 {best}", pending, patient, eta=False)
                attempts.append(dict(line=best, outcome=outcome, time=now(), mode=mode, reason="patient"))
                self.store.update(item_id, download_attempts=attempts)
                if outcome != "complete":
                    bump("normal_failures")

            if not force_ok:
                run_normal_lines()
                patient_best_line()
            if not pending:
                # Smart reaches this only after all 3 normal lines failed; force reaches it directly.
                # Both modes go through the same fee-text / consent gate.
                if force_ok:
                    allowed, explanation = True, ""
                elif mode == "force":
                    allowed, explanation = False, "强制高速未通过费用核对，已改用普通线路，但均未完成。"
                else:
                    allowed, explanation = highspeed_allowed(list(LINES) if ctx.get("early_hs") else failures, preferences, self.page.locator("body").inner_text(), mode=mode, points=live_points)
                    ctx["hs_allowed"] = allowed
                    ctx["fee_mismatch"] = (not allowed) and "费用规则" in explanation
                if not allowed:
                    ctx["transient"] = not ctx["fee_mismatch"]
                    raise ValueError("3 条普通线路均未完成。" + explanation)
                clicks = int((self.store.get(item_id) or {}).get("highspeed_clicks") or 0)
                if clicks >= settings["highspeed_max_tries"]:
                    raise ValueError("该文件的高速通道已尝试 %d 次（上限 %d 次），不再尝试。请在科研通查看；已扣分但未下载成功的，可提交工单" % (clicks, settings["highspeed_max_tries"]))
                if paid_before:
                    self.store.event("该文件此前已支付过高速费用。按科研通规则，同一文件只扣一次，本次再次使用高速不会重复扣分。", item_id=item_id, kind="download")
                if self.stop.is_set():
                    raise ValueError("下载已停止，未启用高速通道")
                tries = "本次点击高速通道一次（该文件最多 %d 次，当前为第 %d 次）。" % (settings["highspeed_max_tries"], clicks + 1)
                self.store.event(("准备按确认的费用规则使用高速通道；" if force_ok else "3 条普通线路全部失败，准备按确认的费用规则切换高速通道；") + tries)
                speed_button = self.page.locator("#download-highspeed-direct")
                if speed_button.count() != 1:
                    raise ValueError("高速通道入口已变化，未点击，请手动检查")
                # Recorded BEFORE the click: whatever happens next, this file is never paid for a second time.
                charged = not paid_before and not (isinstance(live_points, int) and not isinstance(live_points, bool) and live_points >= 500)
                self.store.update(item_id, highspeed_paid=list(self.store.get(item_id).get("highspeed_paid") or [])
                                  + [dict(file_id=file_id, time=now(), charged=charged, mode=mode)], highspeed_clicks=clicks + 1)
                self._record_ctx = dict(line=HIGHSPEED, history=history)   # speed history only; the switch rule never applies
                self._line_ctx = None                # the free-line switch rule never applies to high speed
                speed_button.click()
                self.confirm_highspeed(item_id)
                # Paid attempt: don't cut it off with the free-line cap (real 2026-10-20: high speed ran ~20 KB/s and restarted once).
                hs_settings = dict(settings, line_max_minutes=longer_minutes(settings))
                outcome = self.wait_transfer(item_id, "高速通道", pending, hs_settings)
                attempts.append(dict(line="highspeed", outcome=outcome, time=now(), mode=mode,
                                     reason="forced" if force_ok else ("early_eta" if ctx.get("early_hs") else (reasons[-1] if reasons else ""))))
                self.store.update(item_id, download_attempts=attempts)
                if outcome != "complete":
                    bump("highspeed_failures")
                if outcome != "complete" and force_ok and not failures and not self.stop.is_set():
                    # Forced high speed failed before any free line was tried: fall back to the free lines (no second high-speed click).
                    self.store.event("强制高速未完成，改用免费的普通线路继续下载（不会再次点击高速，不会重复扣分）。", item_id=item_id, kind="download")
                    self.goto(url)
                    self.page.get_by_role("progressbar", name="下载进度").wait_for(timeout=20000)
                    run_normal_lines()
                    outcome = "complete" if pending else outcome
                if outcome != "complete":
                    ctx["transient"] = True
                    raise ValueError("高速通道未完成，请检查页面（同一文件只扣一次分，后续重试不会重复扣分）")
            self.save_download(item_id, pending[0], actual_title, actual_doi)
            self.store.update(item_id, download_retry=None)
        except Exception as exc:
            screenshot_folder = self.store.root / "screenshots"
            screenshot_folder.mkdir(exist_ok=True)
            try:
                self.page.screenshot(path=str(screenshot_folder / f"download-{item_id}-{datetime.now():%H%M%S}.png"), mask=[self.page.locator("input")])
            except Exception:
                pass
            # D8: say what kind of failure this is and what to do next.
            message = str(exc) if isinstance(exc, ValueError) else "下载页面未正常打开，或页面结构已变化"
            failure = explain_failure(message, exc)
            record = dict(kind=failure["kind"], label=failure["label"], advice=failure["advice"], time=now())
            if failure["kind"] == "stopped":
                self.store.update(item_id, status="attention", download_failure=record)
                raise
            full = f"{message}（{failure['label']}。建议：{failure['advice']}）"
            transient = (ctx["transient"] or is_transient(exc)) and not isinstance(exc, (SessionExpired, PreconditionError))
            retry, text = self.retry_or_takeover(item_id, transient, ctx, settings, record, full)
            if retry:
                raise RetryScheduled(text) from exc
            raise (type(exc)(text) if isinstance(exc, ValueError) else ValueError(text)) from exc
        finally:
            self.context.remove_listener("page", attach)
            for page in attached:
                try:
                    page.remove_listener("download", download_event)
                    page.remove_listener("response", observe)
                except Exception:
                    pass
            if observed:
                try:
                    self.store.update(item_id, range_observations=observed)
                except Exception:
                    pass
            try:
                with self.throttled():
                    self.page.goto(item["assist_url"], wait_until="domcontentloaded", timeout=15000)
            except Exception:
                pass

    def retry_or_takeover(self, item_id, transient, ctx, settings, record, full):
        """Download did not finish. Returns (True, text) when an automatic retry was scheduled (item NOT in attention),
        or (False, text) after handing the item to the person (attention + needs_action notification)."""
        item = self.store.get(item_id) or {}
        normal, fast = int(item.get("normal_failures") or 0), int(item.get("highspeed_failures") or 0)
        done_rounds = int((item.get("download_retry") or {}).get("count") or 0)
        hs_possible = ctx["hs_allowed"] if ctx["hs_allowed"] is not None else ctx["mode"] in ("smart", "force")
        if not transient:
            reason = "遇到需手动处理的问题（非网络卡顿），不自动重试（普通线路失败 %d 次，高速通道失败 %d 次）" % (normal, fast)
        else:
            reason = takeover_reason(normal, fast, done_rounds, settings, hs_possible, deletion.seconds_left(item))
        if reason:
            text = reason + "。" + full
            self.store.update(item_id, status="attention", download_failure=record, download_retry=None, note=text)
            self.notify_user("needs_action", item_id, "需处理", "《%s》下载需处理：%s" % ((item.get("title") or item.get("doi") or "?")[:40], reason))
            return False, text
        count = done_rounds + 1
        at = datetime.now().astimezone() + timedelta(minutes=backoff_minutes(count))
        status_text = "自动重试中（第 %d 次，预计 %s 重试）" % (count, at.strftime("%H:%M"))
        self.store.update(item_id, status="retrying", download_failure=record, note=status_text,
                          download_retry=dict(count=count, next_at=at.isoformat(timespec="seconds"), last_reason=record["label"]))
        return True, "%s：%s（普通线路失败 %d 次，高速通道失败 %d 次）" % (status_text, full, normal, fast)

    def line_control(self, number):
        name = re.compile(rf"^线路{number}(?:\s|$)")
        return self.page.get_by_role("button", name=name).or_(self.page.get_by_role("checkbox", name=name)).first

    def line_speed_sample(self, pending, seconds):
        """Average bytes/s the page reports over `seconds` (None if the file finished meanwhile)."""
        self.page.wait_for_timeout(350)
        first = read_progress(self.page.locator("body").inner_text(timeout=5000))["received"]
        start, last = time.monotonic(), first
        while time.monotonic() - start < seconds:
            if self.stop.is_set():
                raise ValueError("下载已停止，未执行采纳或驳回")
            self.page.wait_for_timeout(350)
            if pending:
                return None
            metrics = read_progress(self.page.locator("body").inner_text(timeout=5000))
            last = metrics["received"]
            if metrics["error"]:
                return 0.0
        gained = last - first if last >= first else last      # a line switch may reset the page's byte counter
        return max(0, gained) / max(time.monotonic() - start, 0.001)

    def choose_line(self, item_id, current, settings, pending, history):
        """D3: probe the normal lines (history first when there is recent history) and settle on the fastest."""
        state = dict(active=current)
        hist = history.all()

        def probe(line, seconds):
            if pending:
                return None
            if line != state["active"]:
                self.line_control(line).click()
                state["active"] = line
            return self.line_speed_sample(pending, seconds)
        order = [current] + [n for n in LINES if n != current]
        self.store.event("正在测试普通线路速度（每条约 %d 秒），用于选择最快的一条。" % settings["probe_seconds"], item_id=item_id, kind="download")
        result = select_line(order, probe, hist, settings["probe_seconds"], record=history.record)
        measured = result["measured"]
        if not result["done"] and result["chosen"] != state["active"]:
            self.line_control(result["chosen"]).click()
            state["active"] = result["chosen"]
        speeds = "、".join("线路%d %.1f KB/s" % (l, v / 1024) for l, v in sorted(measured.items()))
        how = {"history": "已按近期同时段的历史速度优先测试最快的一条", "history_then_full": "历史最快的线路本次变慢，已改为逐条测速", "full": "逐条测速"}[result["mode"]]
        self.store.update(item_id, line_probe=dict(measured={str(k): int(v) for k, v in measured.items()}, chosen=result["chosen"],
                                                   mode=result["mode"], seconds=result["secs"], time=now()))
        self.store.event(f"{how}：{speeds or '无数据'}；选用线路 {result['chosen']}。", item_id=item_id, kind="download")
        return order_after(result["chosen"], measured), state["active"]

    def hs_early(self, item_id, settings, history, hist_now, after_lines=False):
        """O6: every free line is projected to need longer than the dynamic cap AND the high-speed history for this hour is
        at least twice the best free line. Only decides to skip waiting on free lines; the fee gate, max tries and the
        charged-once rule still apply afterwards."""
        try:
            total = read_progress(self.page.locator("body").inner_text(timeout=5000))["total"]
            hs_speed = history.expected(HIGHSPEED)
            if not total or not hs_speed:
                return False
            measured = ((self.store.get(item_id) or {}).get("line_probe") or {}).get("measured") or {}
            speeds = {int(k): v for k, v in measured.items() if str(k).isdigit() and isinstance(v, (int, float))}
            for line in LINES:
                if line not in speeds and hist_now.get(line):
                    speeds[line] = hist_now[line]
            if not after_lines and set(speeds) != set(LINES):
                return False
            if not speeds:
                return False
            cap = dynamic_cap_for(settings, total)
            if any(v > 0 and total / v <= cap for v in speeds.values()) or hs_speed < 2 * max(speeds.values()):
                return False
            self.store.event("三条普通线路按速度预计都超过 %s，而高速通道同时段历史速度明显更快（至少 2 倍）；直接评估高速通道（仍需通过费用核对）。" % fmt_duration(cap),
                             item_id=item_id, kind="download")
            return True
        except Exception:
            return False

    def report_plan(self, item_id, hist, settings, active_line):
        """D4: size and expected time before the transfer starts."""
        try:
            metrics = read_progress(self.page.locator("body").inner_text(timeout=5000))
            line = next((n for n in LINES if active_line(n)), 1)
            text = plan_text(metrics["total"], hist.get(line) if hist else None, int(dynamic_cap_for(settings, metrics["total"]) // 60))
            self.store.update(item_id, download_progress=dict(line="", speed_bps=0, slow=False, eta_seconds=None, plan=text, **metrics))
            self.store.event(text, item_id=item_id, kind="download")
        except Exception:
            pass

    def wait_transfer(self, item_id, label, pending, settings=None, eta=False):
        start = time.monotonic()
        watch = make_watch(start, settings or download_settings({}), eta)
        ctx = getattr(self, "_line_ctx", None) or getattr(self, "_record_ctx", None) or {}
        line, history = ctx.get("line"), ctx.get("history")
        switcher = SwitchWatch(line, ctx["hist"]) if ctx.get("probe") and line in LINES and ctx.get("hist") else None
        last_saved = 0
        slow_reported = False
        size_reported = False
        milestones = set()
        last = dict(received=0, total=0)

        def finish(outcome):
            # D3: remember how fast this line really was at this hour.
            elapsed = time.monotonic() - start
            if history and (line in LINES or line == HIGHSPEED) and last["received"] > 0 and elapsed >= 10:
                history.record(line, last["received"] / elapsed)
            return outcome
        while not pending:
            if self.stop.is_set():
                raise ValueError("下载已停止，未执行采纳或驳回")
            self.page.wait_for_timeout(350)
            if pending:
                return finish("complete")
            metrics = read_progress(self.page.locator("body").inner_text(timeout=5000))
            last = metrics
            tick = time.monotonic()
            outcome = watch.observe(tick, metrics["received"], metrics["error"], metrics["total"])
            if metrics["total"] and not size_reported:
                size_reported = True
                self.store.event(f"{label}：文件大小 {fmt_size(metrics['total'])}。", item_id=item_id, kind="download")
            remaining = eta_seconds(metrics["received"], metrics["total"], watch.speed_bps)
            if outcome == "slow" and not slow_reported:
                self.store.event(f"{label}速度偏慢但仍在接收，继续等待，不会因此切换收费通道。", item_id=item_id, kind="download")
                slow_reported = True
            if tick - last_saved >= 5:
                suffix = "，速度偏慢，继续等待" if outcome == "slow" else ""
                total_text = f" / {fmt_size(metrics['total'])}" if metrics["total"] else ""
                eta_text = f"，预计还需 {fmt_duration(remaining)}" if remaining is not None else ""
                self.store.update(item_id, download_progress=dict(line=label, speed_bps=watch.speed_bps, slow=outcome == "slow",
                                                                  eta_seconds=None if remaining is None else int(remaining), **metrics),
                                  note=f"{label}：{metrics['percent']}%，已接收 {fmt_size(metrics['received'])}{total_text}，约 {watch.speed_bps / 1024:.1f} KB/s{eta_text}{suffix}。")
                last_saved = tick
                for mark in (25, 50, 75):
                    if metrics["percent"] >= mark and mark not in milestones:
                        milestones.add(mark)
                        self.store.event(f"{label}：已下载 {mark}%，约 {watch.speed_bps / 1024:.1f} KB/s" + (f"，预计还需 {fmt_duration(remaining)}" if remaining is not None else "") + "。",
                                         item_id=item_id, kind="download")
            if outcome in {"error", "stalled", "timeout", "eta"}:
                return finish(outcome)
            if switcher and tick - start >= watch.speed_window:
                target = switcher.observe(tick, watch.speed_bps)
                if target:
                    self._switch_target = target
                    return finish("switch")
        return finish("complete")

    def save_download(self, item_id, download, title, doi):
        if self.stop.is_set():
            download.cancel()
            raise ValueError("下载已停止")
        # D7: keep the site's original file name (only characters Windows cannot store are replaced):
        # downloads/{item_id}/{suggested_filename}. A rejected earlier file stays where it is; a later answer gets answer-N/.
        suggested = getattr(download, "suggested_filename", "")
        original = suggested if isinstance(suggested, str) else ""
        name = safe_filename(original) or (item_id + ".pdf")
        if not name.lower().endswith(".pdf"):
            name += ".pdf"      # whatever the site calls it, it is saved as a document and never as something executable
        earlier = len(self.store.get(item_id).get("rejected_files", []))
        folder = self.store.root / "downloads" / item_id / (f"answer-{earlier + 1}" if earlier else "")
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / name
        if path.exists():
            raise ValueError("目标文件已存在，未覆盖")
        download.save_as(str(path))
        try:
            with path.open("rb") as handle:
                head = handle.read(1024)
        except OSError:
            head = b""
        if b"%PDF-" not in head:
            quarantine = self.store.root / "quarantine"
            quarantine.mkdir(parents=True, exist_ok=True)
            target = quarantine / f"{item_id}-{datetime.now():%Y%m%d%H%M%S}-{path.name}.quarantined"
            try:
                path.replace(target)
            except OSError:
                target = path
            note = "收到的文件不是 PDF（缺少 PDF 标记），已隔离到 quarantine 文件夹，未作为应助文件处理。请在科研通查看该应助是否有问题。"
            self.store.update(item_id, status="attention", note=note, original_filename=original)
            self.store.event(note, item_id=item_id, kind="error", action="focus")
            raise ValueError("收到的文件不是 PDF，已隔离，未标记下载成功")
        email = self.store.snapshot().get("preferences", {}).get("oa_contact_email", "")
        result = verify_with_metadata(path, title, doi, self.store.root, email=email)
        names = dict(original_filename=original, local_filename=path.name)
        if result["status"] == "invalid":
            self.store.update(item_id, status="attention", pdf=str(path.relative_to(self.store.root)), verification=result, **names,
                              note="已保存浏览器返回的文件，但 PDF 解析失败。文件已保留，供检查。")
            raise ValueError("收到文件但 PDF 解析失败，未标记下载成功")
        self.store.update(item_id, status="downloaded", pdf=str(path.relative_to(self.store.root)), verification=result, **names,
                          download_progress=None, note="文件已保存并完成初步核对。请核对版本和完整性后自行采纳。")
        self.store.event("文件已保存（保留科研通原文件名 " + path.name + "）并完成 PDF 核对，未自动采纳或驳回。", item_id=item_id, kind="download", action="focus")
        decision = rejection_decision(self.store.get(item_id), self.store.snapshot().get("preferences", {}))
        self.store.update(item_id, rejection=decision)
        if decision["action"] == "auto_reject":
            self.reject(item_id, automatic=True)

    def reject_auto(self, item_id):
        self.reject(item_id, automatic=True)

    def reject(self, item_id, automatic=False):
        item = self.store.get(item_id)
        if not item:
            raise PreconditionError("未找到应助")
        self.assert_account(item)
        decision = rejection_decision(item, self.store.snapshot().get("preferences", {}))
        if not decision["eligible"] or item.get("reject_action"):
            raise PreconditionError("该条不满足驳回条件，或已提交过驳回。请在科研通查看，不自动重试")
        if automatic and decision["action"] != "auto_reject":
            raise PreconditionError("当前规则不允许自动驳回")
        if automatic and decision.get("category") != "file_mismatch":
            raise PreconditionError("自动驳回仅限“文件不一致”（首页 DOI 明确为另一篇文献）")
        category = "file_mismatch" if automatic else item.get("manual_reject_category", "")
        reason = decision["reason"] if automatic else item.get("manual_reject_reason", "")
        if category not in CATEGORY_KEYS:
            raise PreconditionError("请先选择科研通规定的 4 类驳回理由之一")
        if not reason:
            raise PreconditionError("请填写具体说明")
        path = (self.store.root / item["pdf"]).resolve()
        if not path.is_relative_to(self.store.root.resolve()) or hashlib.sha256(path.read_bytes()).hexdigest() != item["verification"]["sha256"]:
            raise PreconditionError("PDF 已变化，请重新核对后再驳回")
        self.goto(item["assist_url"])
        self.dismiss_info_layers()
        # Work only inside the timeline entry for the exact downloaded file.
        relative = item["download_file_url"].replace(BASE, "", 1)
        link = self.page.locator('a[href="' + relative + '"]').last
        if not link.count():
            raise ValueError("科研通上的应助文件已变化，未驳回")
        card = link.locator("xpath=ancestor::li[1]")
        if card.count() != 1 or any(s in card.inner_text() for s in ("已采纳", "已驳回", "已拒绝")):
            raise ValueError("应助已处理或科研通页面结构已变化，未驳回")
        controls = card.locator('button.btn-handle-file[data-type="reject"]')
        if controls.count() != 1:
            controls = card.get_by_role("button", name=re.compile(r"^\W*(?:驳回|拒绝)(?:应助|文件|文献)?\W*$")).or_(card.get_by_role("link", name=re.compile(r"^\W*(?:驳回|拒绝)(?:应助|文件|文献)?\W*$")))
        if controls.count() != 1:
            raise ValueError("无法唯一确认该文件的驳回入口，未提交。请打开求助详情手动处理")
        if self.stop.is_set():
            raise ValueError("已停止，未驳回")
        # From here on, a timeout means unknown outcome; never click again automatically.
        self.clear_blocking_layers()
        self.click_or_handover(item_id, controls, "驳回")
        self.store.update(item_id, reject_action=dict(status="attempted", automatic=automatic, time=now(), reason=reason, category=category))
        self.page.wait_for_timeout(800)
        dialog = self.page.locator('dialog[open], [role="dialog"]:visible, .layui-layer:visible')
        if dialog.count() == 1:
            reason_box = dialog.locator("textarea")
            if reason_box.count() != 1:
                raise ValueError("未识别科研通的驳回表单，已停止。请查看求助详情，不自动重试")
            reason_box.fill(reason)
            # layui buttons are <a> without href (no link role): match by text inside .layui-layer-btn.
            submit = dialog.locator(".layui-layer-btn a").filter(has_text=re.compile(r"^\s*(?:确定|确认|确认驳回|确认拒绝|提交)\s*$"))
            if submit.count() != 1:
                submit = dialog.get_by_role("button", name=re.compile(r"^(?:确定|确认|确认驳回|确认拒绝|提交)$")).or_(dialog.get_by_role("link", name=re.compile(r"^(?:确定|确认|确认驳回|确认拒绝|提交)$")))
            if submit.count() != 1 or self.stop.is_set():
                raise ValueError("未确认驳回提交按钮，或已停止，请手动查看")
            if automatic and rejection_decision(self.store.get(item_id), self.store.snapshot().get("preferences", {}))["mode"] != "auto":
                raise ValueError("自动驳回已关闭，未提交")
            submit.click()
        # Confirmation must belong to this exact file entry, never the live site feed.
        try:
            card.get_by_text(re.compile(r"已(?:驳回|拒绝)")).first.wait_for(timeout=15000)
        except Exception:
            self.store.update(item_id, reject_action=dict(status="uncertain", automatic=automatic, time=now(), reason=reason, category=category))
            raise ValueError("驳回结果未确认。请在科研通查看，不自动重复提交")
        done = dict(status="confirmed", automatic=automatic, time=now(), reason=reason, category=category)
        # Keep the rejected file on disk and in the record; free the item for the next answer.
        archived = dict(pdf=item["pdf"], verification=item["verification"], download_file_url=item["download_file_url"],
                        reject_action=done, rejected_at=done["time"])
        self.store.update(item_id, status="rejected", reject_action=None, rejected_at=done["time"], pdf="", verification=None,
                          download_file_url="", rejection=None, rejected_files=list(item.get("rejected_files", [])) + [archived])
        self.store.event("已确认驳回该应助文件，本地 PDF 与核对记录已归档保留。", item_id=item_id, kind="reject", action="focus")

    def dismiss_info_layers(self):
        """The detail page opens with an informational layer ("已经有人上传了文件，请在 48 小时内查看并审核") that blocks clicks.
        Only that kind of purely informational layer is closed (its single 确定 button)."""
        for _ in range(3):
            closed = False
            for layer, text in self.visible_layers():
                if "已经有人上传了文件" in text or ("信息" in text and "审核" in text and "采纳" not in text and "驳回" not in text):
                    button = layer.locator(".layui-layer-btn a").filter(has_text=re.compile(r"^\s*确定\s*$"))
                    if button.count() == 1:
                        button.first.click()
                        self.page.wait_for_timeout(600)
                        closed = True
            if not closed:
                return

    RISKY_LAYER = re.compile(r"采纳|驳回|拒绝|提交|积分|扣|删除|支付|付费|充值|悬赏")
    CLOSE_TEXT = re.compile(r"^\s*(?:确定|我知道了|知道了|关闭|好的|朕知道了|×|x|X)\s*$")

    def clear_blocking_layers(self, rounds=3):
        """The site sometimes pops up layers with varying close controls. Close only purely informational ones
        (no inputs, no wording about adopt/reject/submit/points/payment): try the × icon, then a common close button.
        Returns the texts of layers that could NOT be closed safely."""
        left = []
        for _ in range(rounds):
            left, closed = [], False
            for layer, text in self.visible_layers():
                if _count(layer.locator("textarea, input:not([type=hidden]), select")) or self.RISKY_LAYER.search(text):
                    left.append(" ".join(text.split())[:120])
                    continue
                for candidate in (layer.locator(".layui-layer-close, .layui-layer-setwin a, [aria-label*=close i], [class*=close]"),
                                  layer.locator(".layui-layer-btn a, button, a").filter(has_text=self.CLOSE_TEXT)):
                    if _count(candidate):
                        try:
                            candidate.first.click(timeout=3000)
                            closed = True
                            break
                        except Exception:
                            continue
                else:
                    left.append(" ".join(text.split())[:120])
            if not closed:
                break
            self.page.wait_for_timeout(600)
        return left

    def click_or_handover(self, item_id, control, what):
        """Click an adopt/reject control. If something covers it, clear informational layers and try ONE more time.
        Playwright never dispatches a click that fails its actionability checks, so a failed click sent nothing.
        Still blocked -> hand over to the human (gate stays locked, nothing retried later)."""
        for attempt in range(2):
            try:
                control.click(timeout=8000)
                return
            except Exception:
                left = self.clear_blocking_layers()
        shot = ""
        try:
            folder = self.store.root / "screenshots"
            folder.mkdir(exist_ok=True)
            shot = str((folder / f"blocked-{item_id}-{datetime.now():%H%M%S}.png").relative_to(self.store.root))
            self.page.screenshot(path=str(self.store.root / shot), mask=[self.page.locator("input")])
        except Exception:
            pass
        message = f"{what}科研通弹窗遮挡了按钮，软件未能点击（未提交）。请在浏览器中手动{what}。完成后点击“已在科研通处理”。"
        self.store.update(item_id, status="attention", note=message, handover=dict(kind=what, time=now(), screenshot=shot, layers=left))
        self.store.event(message, kind="needs_action", item_id=item_id, action="open_site")
        self.notify_user("needs_action", f"blocked:{item_id}", "需处理", message)
        raise ValueError(message)

    def card_for(self, item):
        self.goto(item["assist_url"])
        self.dismiss_info_layers()
        relative = item["download_file_url"].replace(BASE, "", 1)
        link = self.page.locator('a[href="' + relative + '"]').last
        if not link.count():
            raise ValueError("科研通上的应助文件已变化，未操作")
        card = link.locator("xpath=ancestor::li[1]")
        if card.count() != 1 or any(s in card.inner_text() for s in ("已采纳", "已驳回", "已拒绝")):
            raise ValueError("应助已处理或科研通页面结构已变化，未操作")
        # Real card text (2026-10-20): "超过24小时未处理，系统将默认采纳" -> keep it as this answer's handling deadline.
        hours = re.search(r"超过\s*(\d+)\s*小时未处理[，,]?\s*系统将默认采纳", card.inner_text())
        if hours and not item.get("auto_adopt_hours"):
            self.store.update(item["id"], auto_adopt_hours=int(hours.group(1)))
        return card

    def confirm_highspeed(self, item_id, wait_ms=6000):
        """The site asks "高速通道提示 / 确认使用高速通道" after the high-speed click unless the browser has snoozed it
        (localStorage, 30 days). Without this click high speed never starts (real 2026-10-20: every earlier
        "high-speed" attempt sat behind this dialog while the auto-started free line 1 crawled).
        The dialog's own tariff text must match the consented rule; otherwise press 取消 and stop."""
        waited = 0
        while waited <= wait_ms:
            self.assert_mcp_authority()
            for layer, text in self.visible_layers():
                compact = re.sub(r"\s+", "", text)
                if "确认使用高速通道" not in compact:
                    continue
                rule = compact.replace("＜", "<").replace("≥", ">=")
                if not (re.search(r"积分<500.*?扣(?:除)?2积分", rule) and re.search(r"积分>=500.*?(?:免积分|不扣积分)", rule)):
                    cancel = layer.locator(".layui-layer-btn a").filter(has_text="取消")
                    if _count(cancel) == 1:
                        cancel.first.click()
                    self.store.update(item_id, highspeed_confirm=dict(status="fee_mismatch", time=now()))
                    raise PreconditionError("科研通高速通道提示中的费用规则与已确认的规则不一致，已取消，未使用高速通道")
                ok = layer.locator(".layui-layer-btn a").filter(has_text="确认使用高速通道")
                if _count(ok) != 1 or self.stop.is_set():
                    raise ValueError("未能唯一确认高速通道提示的确认按钮，或已停止")
                ok.first.click()
                self.store.update(item_id, highspeed_confirm=dict(status="confirmed", time=now()))
                self.store.event("已在科研通的“高速通道提示”中确认使用高速通道（费用规则与已确认的一致）。", item_id=item_id, kind="download")
                return "confirmed"
            self.page.wait_for_timeout(500)
            waited += 500
        self.store.update(item_id, highspeed_confirm=dict(status="no_dialog", time=now()))
        return "no_dialog"     # snoozed in this browser: the site starts high speed directly

    def confirm_dialog(self, pattern, max_dialogs=2, log=None):
        """Confirmation layer(s) after a click (adopt may show two in a row). layui buttons are <a> without href,
        so match by text inside .layui-layer-btn; press only one unambiguous non-cancel button per layer."""
        handled = 0
        for _ in range(max_dialogs * 6):
            self.assert_mcp_authority()
            if handled >= max_dialogs:
                return handled
            layers = [(l, t) for l, t in self.visible_layers() if _count(l.locator(".layui-layer-btn a"))]
            if not layers:
                if handled:
                    return handled
                self.page.wait_for_timeout(500)
                continue
            layer, text = layers[-1]
            if log is not None:
                log.append(" ".join(text.split())[:200])
            ok = layer.locator(".layui-layer-btn a").filter(has_text=pattern)
            if ok.count() != 1 or self.stop.is_set():
                raise ValueError("未能唯一确认科研通的确认按钮，或已停止。请查看求助详情，不自动重试")
            ok.first.click()
            handled += 1
            self.page.wait_for_timeout(800)
        return handled

    def adopt(self, item_id):
        item = self.store.get(item_id)
        if not item or stage_of(item) != "awaiting_decision" or item.get("adopt_action"):
            raise PreconditionError("仅可采纳已下载核对、等待决定且未处理过的本人应助")
        self.assert_account(item)
        path = (self.store.root / item["pdf"]).resolve()
        if not path.is_relative_to(self.store.root.resolve()) or hashlib.sha256(path.read_bytes()).hexdigest() != (item.get("verification") or {}).get("sha256"):
            raise PreconditionError("PDF 已变化，请重新核对后再采纳")
        card = self.card_for(item)
        self.clear_blocking_layers()
        controls = card.locator('button.btn-handle-file[data-type="accept"]')
        if controls.count() != 1:
            name = re.compile(r"^\W*采纳(?:应助|文件|文献)?\W*$")
            controls = card.get_by_role("button", name=name).or_(card.get_by_role("link", name=name))
        if controls.count() != 1:
            raise ValueError("无法唯一确认该文件的采纳入口，未提交。请打开求助详情手动处理")
        if self.stop.is_set():
            raise ValueError("已停止，未采纳")
        self.assert_mcp_authority()
        # From here on a timeout means unknown outcome; never click again automatically.
        try:
            self.click_or_handover(item_id, controls, "采纳")
        except ValueError:
            self.store.update(item_id, adopt_action=None)
            raise
        self.store.update(item_id, adopt_action=dict(status="attempted", time=now()))
        try:
            seen = []
            self.confirm_dialog(re.compile(r"^\s*(?:确定|确认|确认采纳|采纳|提交)\s*$"), log=seen)
            card.get_by_text(re.compile(r"已采纳")).first.wait_for(timeout=15000)
        except Exception:
            self.store.update(item_id, adopt_action=dict(status="uncertain", time=now(), dialogs=locals().get("seen", [])))
            raise ValueError("采纳结果未确认。请在科研通查看，不自动重复提交")
        self.store.update(item_id, status="adopted", adopt_action=dict(status="confirmed", time=now()))
        self.store.event("已确认采纳该应助文件。", item_id=item_id, kind="adopt", action="focus")

    # ------------------------------------------------------------------------------------------------
    # 发布求助（单篇）。流程与二次核对见 docs/PUBLISH_FLOW_DESIGN.md。页面选择器里确认来自侦察的：
    # #onekey、button.onekey-search、layer 弹窗、#Assist-doi/#Assist-title/#Assist-point/#Assist-close_at、#form-submit-btn、
    # check-before-assist-create 的 code 0/1/2；弹窗里各字段的具体文字、弹窗确认按钮的文字属推测，已做成容错解析。
    # ------------------------------------------------------------------------------------------------
    def notify_user(self, kind, key, title, message):
        notifier = getattr(self, "notifier", None)
        if notifier:
            try:
                notifier.notify(kind, key, title, message)
            except Exception:
                pass

    def visible_layers(self):
        """[(locator, text)] of the layer popups that are visible right now."""
        layers = self.page.locator(".layui-layer")
        found = []
        total = layers.count()
        for n in range(total if isinstance(total, int) else 0):
            layer = layers.nth(n)
            try:
                if layer.is_visible():
                    found.append((layer, layer.inner_text(timeout=3000)))
            except Exception:
                pass
        return found

    def publish_stop(self, item_id, kind, reason, check=None, pause=False, notify_kind="needs_action"):
        """Nothing is (or no longer can be) submitted: item -> attention with what was read and a screenshot, then stop."""
        if kind == "gate":
            # The site wants an answered request handled first: not an error. Nothing was submitted, the item stays queued;
            # the stored balance is treated as stale and the pipeline waits until the next sync (pipeline.next_step "site_gate").
            self.store.update_account(account_id=self.accounts.active_id, site_gate=now(), points=None)
            self.store.event("求助未提交：" + reason, item_id=item_id, kind="publish", action="focus")
            self.notify_user(notify_kind, f"publish:{item_id}:{kind}", "需先处理应助", reason)
            raise PreconditionError(reason)
        shot = ""
        page = getattr(self, "page", None)
        if page is not None:
            try:
                folder = self.store.root / "screenshots"
                folder.mkdir(exist_ok=True)
                target = folder / f"publish-{item_id}-{datetime.now():%H%M%S}.png"
                page.screenshot(path=str(target), mask=[page.locator("input")])
                shot = str(target.relative_to(self.store.root))
            except Exception:
                shot = ""
        self.store.update(item_id, status="attention", note=reason, publish_blocked=True,
                          publish_check=dict(check or {}, kind=kind, reason=reason, time=now(), screenshot=shot))
        self.store.event("求助未提交：" + reason, item_id=item_id, kind="publish", action="focus")
        if pause:
            self.store.update_preferences(pipeline_enabled=False)
            self.store.event("已暂停自动流程：科研通提示账号受限或次数已达上限。请先在科研通确认原因。", item_id=item_id, kind="publish", action="focus")
        self.notify_user(notify_kind, f"publish:{item_id}:{kind}", "求助需处理" if notify_kind == "needs_action" else "最低悬赏上调，待确认", reason)
        raise ValueError(reason)

    def reference_title(self, item, doi, email):
        """The public (OpenAlex/Crossref) title this item is compared against; '' when none can be had."""
        for key in ("metadata_ref", "metadata"):
            title = (item.get(key) or {}).get("title")
            if title:
                return str(title)
        try:
            record = fetch_metadata(doi, self.store.root, email)
            if record.get("status") == "available" and record.get("title"):
                return str(record["title"])
        except Exception:
            pass
        return ""

    def wait_lookup_layer(self, item_id, check):
        """Wait (<= 50 s) for the 智能提取 result layer; known failure wording stops immediately."""
        deadline = time.monotonic() + 50
        for _ in range(50):
            for layer, text in self.visible_layers():
                failure = pp.classify_failure(text)
                if failure:
                    self.publish_stop(item_id, failure["kind"], failure["message"], dict(check, layer_text=text[:500]), pause=failure["pause"])
                # Title queries show 相似度; an exact DOI query shows only 标题/DOI/期刊/作者 + three buttons (real page, 2026-10-20).
                if "相似度" in text or ("DOI" in text and "标题" in text and "信息正确" in text):
                    return layer, text
                if re.search(r"未(找到|查到|检索到)|没有找到|查询失败|识别失败|无法识别", text):
                    self.publish_stop(item_id, "lookup_failed", "科研通未能识别该 DOI：" + " ".join(text.split())[:80], dict(check, layer_text=text[:500]))
            self.page.wait_for_timeout(1000)
            if time.monotonic() > deadline:
                break
        self.publish_stop(item_id, "lookup_timeout", "科研通在 50 秒内未返回识别结果，未提交。请打开求助页面查看", check)

    def confirm_quick_tip(self):
        """「快捷提示」layer.confirm: press 直接提交 (only reached after every check passed). Returns an error text or ''."""
        for _ in range(4):
            for layer, text in self.visible_layers():
                if "快捷提示" in text:
                    button = layer.locator(".layui-layer-btn a", has_text="直接提交")
                    if button.count() != 1:
                        return "出现“快捷提示”，但未找到唯一的“直接提交”按钮，未提交"
                    button.first.click()
                    return ""
            self.page.wait_for_timeout(500)
        return ""

    def publish(self, item_id):
        item = self.store.get(item_id)
        if not item or item["source"] == "demo":
            raise PreconditionError("请先导入真实 DOI 或标题。演示数据不能提交到科研通")
        self.assert_account(item)
        if stage_of(item) != "queued" or item.get("publish_action"):
            raise PreconditionError("仅可发布尚未发布、未尝试过的本地求助")
        prefs = self.store.snapshot().get("preferences", {})
        email = prefs.get("oa_contact_email", "")
        doi = doi_value(item.get("doi", ""))
        check = dict(expected_doi=doi, item_title=item.get("title", ""))
        if not doi:
            self.publish_stop(item_id, "no_doi", "无 DOI 的文献不支持自动求助（科研通的智能提取需要 DOI）。请补充 DOI，或在科研通页面自行求助。", check)
        ref_title = self.reference_title(item, doi, email)
        check["reference_title"] = ref_title
        block = pp.auto_publish_block([item.get("title", ""), ref_title], doi)
        if block:
            self.publish_stop(item_id, "not_auto", block, check)

        def recheck():
            snapshot = self.store.snapshot()
            active = self.accounts.active_id
            mine = [i for i in snapshot["items"] if i.get("source") != "demo" and i.get("account_id", "default") == active]
            stored = snapshot.get("account_metrics", {}).get(active, {}).get("points")
            blocked = gate(mine, stored, item_reward(item, prefs, stored))
            if blocked["blocked"]:
                raise PreconditionError(blocked["reason"])
            return stored
        stored_before = recheck()
        self.goto("/assist/create", interactive=False)
        try:
            points = self.read_points()
        except Exception:
            points = None
        if points is not None:
            self.store.update_account(account_id=self.accounts.active_id, points=points)
        else:
            points = self.store.snapshot().get("account_metrics", {}).get(self.accounts.active_id, {}).get("points")
        recheck()
        if tier(stored_before, item_reward(item, prefs, stored_before)) == "batch" and tier(points, item_reward(item, prefs, points)) == "single":
            raise PreconditionError("科研通页面显示当前积分 %s，发布本条（悬赏 %d）后将低于 500，与之前记录的积分不符；未提交。已更新积分，请按逐条求助规则重新规划"
                                    % (points, item_reward(item, prefs, points)))
        page = self.page
        if pp.page_restricted(page.locator("body").inner_text(timeout=5000)):
            failure = pp.classify_failure("受限")
            self.publish_stop(item_id, failure["kind"], failure["message"], check, pause=True)
        base = item.get("request_reward") or intake.default_reward(prefs, points)
        reserve = pp.raise_settings(prefs)["reserve"]
        if isinstance(points, int) and not isinstance(points, bool) and base > points - reserve:
            self.publish_stop(item_id, "reserve_points", "悬赏 %d 积分将使剩余积分低于保留值（现有 %d，保留 %d），未提交。请调整悬赏或保留积分" % (base, points, reserve), check)

        # 1) 智能提取：只用 DOI
        page.locator("#onekey").fill(doi)
        with self.throttled():
            page.locator("button.onekey-search").click()
        layer, text = self.wait_lookup_layer(item_id, check)
        scraped = pp.parse_lookup_text(text)
        check.update(doi=scraped["doi"], title=scraped["title"], similarity=scraped["similarity"], journal=scraped["journal"],
                     type=scraped["type"], min_reward=scraped["min_reward"], layer_text=scraped["text"][:600])
        seen = list(self.store.snapshot().get("account_metrics", {}).get(self.accounts.active_id, {}).get("min_reward_seen") or [])
        if isinstance(scraped["min_reward"], int):
            self.store.update_account(account_id=self.accounts.active_id, min_reward_seen=(seen + [scraped["min_reward"]])[-10:])
        if pp.min_reward_jump(scraped["min_reward"], seen):
            # A jump alone is ambiguous (a 10-point journal followed by a 50-point publisher is normal), so warn only;
            # the reward-raise setting still decides whether to pay, and explicit "受限" page text still pauses.
            self.store.event("提示：页面要求的最低悬赏变为 %d 积分（此前约 %d）。可能是该出版社要求较高，也可能是账号受限，建议在科研通查看。"
                             % (scraped["min_reward"], min(seen[-5:])), kind="needs_action", item_id=item_id)
            check["min_reward_jump"] = True

        # 2) 二次核对：DOI 精确一致、标题与公开记录 >= 0.90、相似度 >= 50、类型为期刊论文
        problems = pp.evaluate_lookup(doi, ref_title, scraped)
        if problems:
            self.publish_stop(item_id, "lookup_mismatch", "弹窗中的识别结果与该文献不符，已停止：" + "；".join(problems), dict(check, problems=problems))
        block = pp.auto_publish_block([scraped["title"]], scraped["doi"])
        if block:
            self.publish_stop(item_id, "not_auto", block, check)

        # 3) 填入表单：只点「信息正确，还需修改」（只填表、不发布）；绝不点「信息正确，直接发布」（会跳过本地核对直接提交）
        buttons = layer.locator("a, button, span").filter(has_text=re.compile(r"^\s*信息正确[，,]\s*还需修改\s*$"))
        if buttons.count() != 1:
            buttons = layer.locator(".layui-layer-btn a", has_text=re.compile(r"^\s*(?:确定|确认|使用|填入|填充|采用)"))
        if buttons.count() != 1:
            self.publish_stop(item_id, "lookup_confirm", "未找到唯一的“信息正确，还需修改”按钮，未提交。请在页面中手动操作", check)
        buttons.first.click()
        page.wait_for_timeout(800)
        got_doi = doi_value(page.locator("#Assist-doi").input_value())
        got_title = page.locator("#Assist-title").input_value()
        if got_doi != doi or pp.title_similarity(got_title, ref_title) < pp.TITLE_MIN:
            self.publish_stop(item_id, "form_mismatch", "表单中的 DOI 或标题与该文献不一致，未提交", dict(check, form_doi=got_doi, form_title=got_title))
        # DOI lookups don't show type / minimum in the popup: read them from the filled form instead.
        try:
            type_value = page.locator('input[name="Assist[type]"]:checked').get_attribute("value")
        except Exception:
            type_value = None
        if type_value != "1" and not scraped.get("type"):
            self.publish_stop(item_id, "not_auto", "表单中的文献类型不是“期刊论文”，不自动求助", dict(check, form_type=type_value))
        if scraped["min_reward"] is None:
            try:
                low = re.search(r"\d+", page.locator("#point-low").first.inner_text(timeout=3000))
                scraped["min_reward"] = int(low.group()) if low else None
            except Exception:
                pass
        if scraped["min_reward"] is None:
            self.publish_stop(item_id, "lookup_mismatch", "未读取到页面要求的最低悬赏，未提交", check)
        check["min_reward"] = scraped["min_reward"]

        # 4) 悬赏 = max(用户悬赏, 页面最低值)，被提高时按设置处理
        reward, minimum = base, scraped["min_reward"]
        if minimum > base:
            ok, why = pp.raise_decision(minimum, points, pp.raise_settings(prefs))
            if not ok:
                self.publish_stop(item_id, "min_reward_raised", why + "（当前设置的悬赏为 %d 积分）" % base, dict(check, needed=minimum, base=base), notify_kind="min_bounty")
            reward = minimum
        if isinstance(points, int) and reward > points:
            self.publish_stop(item_id, "points_short", "积分不足：需要悬赏 %d 积分，现有 %d" % (reward, points), dict(check, needed=reward))

        page.locator("#Assist-point").fill(str(reward))
        if str(page.locator("#Assist-point").input_value()).strip() != str(reward):
            self.publish_stop(item_id, "form_mismatch", "悬赏积分未能填入表单，未提交", check)
        try:
            close_at = str(page.locator("#Assist-close_at").input_value())[:40]
        except Exception:
            close_at = ""
        check.update(reward=reward, close_at=close_at)

        # 5) 提交
        submit = page.locator("#form-submit-btn")
        if submit.count() != 1:
            self.publish_stop(item_id, "no_submit", "无法唯一确认发布入口，未提交。请在已填写的表单中手动发布", check)
        if self.stop.is_set():
            raise ValueError("已停止，未发布")
        self.assert_mcp_authority()

        def action(status):
            return dict(status=status, point=reward, close_at=close_at, time=now())
        self.store.update(item_id, publish_action=action("attempted"), publish_check=dict(check, kind="submitting", time=now()))
        try:
            with self.throttled():
                with page.expect_response(lambda r: "check-before-assist-create" in r.url, timeout=30000) as info:
                    submit.click()
                    tip_error = self.confirm_quick_tip()
                    if tip_error:
                        raise _TipError(tip_error)
            data = info.value.json()
        except _TipError as exc:
            self.store.update(item_id, publish_action=None)
            self.publish_stop(item_id, "quick_tip", str(exc), check)
        except Exception:
            self.store.update(item_id, publish_action=action("uncertain"))
            raise ValueError("发布结果未确认。请在科研通“我的求助”查看，不自动重复提交")
        data = data if isinstance(data, dict) else {}
        code, message = data.get("code"), str(data.get("msg") or "")
        if code == 1:
            self.store.update(item_id, publish_action=None)
            failure = pp.classify_failure(message)
            if failure:
                self.publish_stop(item_id, failure["kind"], failure["message"], dict(check, site_message=message[:200]), pause=failure["pause"])
            self.publish_stop(item_id, "site_refused", "科研通未接受本次提交：" + (message[:120] or "未说明原因") + "（未重试）", dict(check, site_message=message[:200]))
        if code == 2:
            reward = self.handle_confirm_prompt(item_id, message, reward, points, prefs, check)
        elif code != 0:
            self.store.update(item_id, publish_action=action("uncertain"))
            raise ValueError("发布结果未确认。请在科研通“我的求助”查看，不自动重复提交")
        if not self.wait_published():
            self.store.update(item_id, publish_action=action("uncertain"))
            raise ValueError("发布结果未确认，下次同步将按标题自动对应，不自动重复提交")
        # Adopt the new request as a website-owned record so later syncs match it by URL.
        check["reward"] = reward
        self.store.update(item_id, source="website", owned=True, assist_url=page.url, status="published", request_reward=reward,
                          publish_action=action("confirmed"), publish_check=dict(check, kind="published", time=now()), publish_blocked=False,
                          note="已发布，等待应助。")
        self.store.event("已发布求助，悬赏 %d 积分，等待同步状态。" % reward, item_id=item_id, kind="publish")
        if isinstance(points, int) and not isinstance(points, bool):
            # The reward is deducted at publish time: keep the stored balance current for the next gate/tier decision.
            self.store.update_account(account_id=self.accounts.active_id, points=points - reward)

    def handle_confirm_prompt(self, item_id, message, reward, points, prefs, check):
        """code 2 「确认提示」. Only a pure “最低悬赏被提高” within the person's setting may be accepted; anything else stops."""
        page = self.page
        layer, text = None, ""
        for _ in range(8):
            for candidate, content in self.visible_layers():
                if "确认提示" in content or "仍然提交" in content:
                    layer, text = candidate, content
            if layer is not None:
                break
            page.wait_for_timeout(500)
        full = (message + "\n" + text).strip()
        self.store.update(item_id, publish_action=None)
        check = dict(check, confirm_text=full[:400])
        if layer is None:
            self.publish_stop(item_id, "code2_unknown", "科研通要求再次确认，但未出现确认窗口，未提交：" + " ".join(message.split())[:100], check)
        failure = pp.classify_failure(full)
        if failure:
            self.publish_stop(item_id, failure["kind"], failure["message"], check, pause=failure["pause"])
        needed = pp.parse_raise(full)
        if needed is None or needed <= reward:
            self.publish_stop(item_id, "code2_other", "科研通弹出了需用户亲自查看的确认提示（并非单纯的“最低悬赏上调”），未提交：" + " ".join(full.split())[:140], check)
        ok, why = pp.raise_decision(needed, points, pp.raise_settings(prefs))
        if not ok:
            self.publish_stop(item_id, "min_reward_raised", why + "（当前设置的悬赏为 %d 积分）" % reward, dict(check, needed=needed, base=reward), notify_kind="min_bounty")
        button = layer.locator(".layui-layer-btn a", has_text="仍然提交")
        if button.count() != 1:
            self.publish_stop(item_id, "code2_other", "未找到唯一的“仍然提交”按钮，未提交", check)
        if self.stop.is_set():
            raise ValueError("已停止，未发布")
        self.store.update(item_id, publish_action=dict(status="attempted", point=needed, time=now()))
        with self.throttled():
            button.first.click()
        self.store.event("科研通将最低悬赏上调至 %d 积分，已按设置自动接受。" % needed, item_id=item_id, kind="publish")
        return needed


class _TipError(Exception):
    pass
