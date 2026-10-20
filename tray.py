"""System-tray icon (pystray + Pillow) in its own thread. Created only by the real service entry point (app.py --tray),
never by make_server, so tests and isolated servers never get a tray icon or a toast."""
import os
import threading

STATE_COLORS = {"监控中": (46, 160, 90), "已暂停": (130, 130, 130), "需要登录": (214, 69, 65), "待处理": (230, 150, 30)}


def state_color(label):
    for key, color in STATE_COLORS.items():
        if label.startswith(key):
            return color
    return STATE_COLORS["已暂停"]


def make_image(image_module, draw_module, label, size=64):
    image = image_module.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = draw_module.Draw(image)
    draw.ellipse((2, 2, size - 2, size - 2), fill=state_color(label) + (255,))
    draw.rectangle((size * .34, size * .24, size * .42, size * .76), fill=(255, 255, 255, 255))
    draw.polygon([(size * .42, size * .5), (size * .68, size * .24), (size * .78, size * .24), (size * .5, size * .52),
                  (size * .78, size * .76), (size * .68, size * .76)], fill=(255, 255, 255, 255))
    return image


class Tray:
    """actions: dict(open_window, sync_now, toggle_pause, open_logs, quit) callables; label(): str; paused(): bool.
    pystray / PIL modules are injectable so tests never create a real icon."""

    def __init__(self, label, paused, actions, logs_dir, pystray=None, image=None, draw=None, refresh_seconds=5):
        self.label, self.paused, self.actions, self.logs_dir = label, paused, actions, logs_dir
        self.pystray, self.image_module, self.draw_module = pystray, image, draw
        self.refresh_seconds = refresh_seconds
        self.icon = None
        self.thread = None
        self.stopped = threading.Event()
        self.shown = None

    def title(self):
        return "科研通下载器 · " + self.label()

    def menu_items(self):
        p = self.pystray
        return (p.MenuItem("打开窗口", lambda *_: self.actions["open_window"](), default=True),
                p.MenuItem("立即同步", lambda *_: self.actions["sync_now"]()),
                p.MenuItem(lambda *_: "恢复监控" if self.paused() else "暂停监控", lambda *_: self.actions["toggle_pause"]()),
                p.MenuItem("打开日志文件夹", lambda *_: self.open_logs()),
                p.Menu.SEPARATOR,
                p.MenuItem("退出", lambda *_: self.quit()))

    def open_logs(self):
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        opener = getattr(os, "startfile", None)
        if opener:
            opener(str(self.logs_dir))
        self.actions.get("opened_logs", lambda path: None)(self.logs_dir)

    def quit(self):
        self.actions["quit"]()

    def start(self):
        if self.pystray is None:
            import pystray
            from PIL import Image, ImageDraw
            self.pystray, self.image_module, self.draw_module = pystray, Image, ImageDraw
        label = self.label()
        self.icon = self.pystray.Icon("AbleSciDownloader", make_image(self.image_module, self.draw_module, label), self.title(),
                                      self.pystray.Menu(*self.menu_items()))
        self.shown = label
        self.thread = threading.Thread(target=self.icon.run, daemon=True, name="kytdl-tray")
        self.thread.start()
        threading.Thread(target=self._refresh_loop, daemon=True, name="kytdl-tray-refresh").start()
        return self

    def refresh(self):
        """Update tooltip and colour when the state label changed."""
        if not self.icon:
            return
        label = self.label()
        self.icon.title = self.title()
        if label != self.shown:
            self.shown = label
            self.icon.icon = make_image(self.image_module, self.draw_module, label)
        try:
            self.icon.update_menu()
        except Exception:
            pass

    def _refresh_loop(self):
        while not self.stopped.wait(self.refresh_seconds):
            try:
                self.refresh()
            except Exception:
                pass

    def notify(self, title, message):
        """Windows toast through the tray icon (no extra dependency)."""
        if self.icon:
            self.icon.notify(message, title)

    def stop(self):
        self.stopped.set()
        if self.icon:
            try:
                self.icon.stop()
            except Exception:
                pass
