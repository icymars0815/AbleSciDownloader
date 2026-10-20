"""O1: dedicated download worker. Runs the (long) download on its own thread with its own Playwright and a NON-persistent
browser context built from the main session's storage_state (in memory only, never written to disk). The main
BrowserAgent stays free for sync/publish/adopt/reject/sign-in. Both share ONE request-pacing limiter, and there is at
most one download at a time. All download safety rules live in BrowserAgent.download, which this class reuses."""
import queue
import threading

from browser_agent import BrowserAgent, SessionExpired


class DownloadWorker(BrowserAgent):
    def __init__(self, main):
        # Deliberately not calling BrowserAgent.__init__: only what is listed here is shared with the main agent.
        self.main = main
        self.store, self.accounts, self.limiter, self.clock = main.store, main.accounts, main.limiter, main.clock
        self.last_activity = self.clock()
        self.commands = queue.Queue()
        self.stop = threading.Event()
        self.busy = False
        self._enqueue_lock = threading.Lock()
        self._closed = False
        self.context = self.page = self.browser = None
        self.interactive = False
        self.worker = None
        self._state_waiter = None
        self._state = None
        self._refreshed = False
        self.current_item = ""
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    @property
    def on_done(self):
        return self.main.on_done

    @on_done.setter
    def on_done(self, _):
        pass

    @property
    def notifier(self):
        return getattr(self.main, "notifier", None)

    def submit(self, item_id, state):
        """Called from the main agent's thread. Raises ValueError if a download is already running."""
        if self.busy:
            raise ValueError("已有下载在进行，同一时间只下载一篇")
        self._state = state
        self._refreshed = False
        self.current_item = item_id
        try:
            self.enqueue("download", item_id)       # atomic busy check: only one download at a time
        except ValueError:
            self._state, self.current_item = None, ""
            raise

    # ---- browser: own Playwright, non-persistent context from the handed-over state ----
    def ensure(self, interactive=False):
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
        self.browser = self.playwright.chromium.launch(channel="chrome", headless=True)
        self.context = self.browser.new_context(storage_state=self._state, accept_downloads=True, viewport={"width": 1080, "height": 850})
        self.page = self.context.new_page()
        self.page.set_default_timeout(12000)

    def close_context(self):
        for obj in (self.context, self.browser):
            try:
                if obj:
                    obj.close()
            except Exception:
                pass
        self.context = self.page = self.browser = None

    def goto(self, path, interactive=False):
        try:
            return super().goto(path)
        except SessionExpired:
            # The handed-over session is no longer valid: ask the main agent for a fresh export ONCE, else take over.
            if self._refreshed:
                raise
            self._refreshed = True
            fresh = self.main.fetch_state(self.stop)
            if not fresh:
                raise
            self._state = fresh
            self.close_context()
            return super().goto(path)

    def download(self, item_id):
        self.current_item = item_id
        try:
            return super().download(item_id)
        finally:
            self._state = None                      # the session never lingers here after the job
            self.close_idle()
            self.current_item = ""

    def shutdown(self, timeout=30):
        """Stop a running download and end the worker thread (and its browser) cleanly."""
        self.stop.set()
        self.commands.put(("close", None, None))
        if self.thread.is_alive() and threading.current_thread() is not self.thread:
            self.thread.join(timeout)
