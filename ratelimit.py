"""Central politeness limits for every 科研通 request (navigations and API calls), publishing and OA hosts.
Pure logic with injectable clocks so the spacing can be tested without sleeping."""
import contextlib
import threading
import time

REQUEST_GAP_DEFAULT, REQUEST_GAP_MIN, REQUEST_GAP_MAX = 3, 3, 30
PUBLISH_PER_HOUR_DEFAULT, PUBLISH_PER_HOUR_MIN, PUBLISH_PER_HOUR_MAX = 10, 1, 30
PUBLISH_GAP_DEFAULT, PUBLISH_GAP_MIN, PUBLISH_GAP_MAX = 60, 60, 600
LIMIT_RANGES = dict(request_gap_seconds=(REQUEST_GAP_MIN, REQUEST_GAP_MAX),
                    publish_per_hour=(PUBLISH_PER_HOUR_MIN, PUBLISH_PER_HOUR_MAX),
                    publish_gap_seconds=(PUBLISH_GAP_MIN, PUBLISH_GAP_MAX))
LIMIT_DEFAULTS = dict(request_gap_seconds=REQUEST_GAP_DEFAULT, publish_per_hour=PUBLISH_PER_HOUR_DEFAULT,
                      publish_gap_seconds=PUBLISH_GAP_DEFAULT)


def _int(value, key):
    low, high = LIMIT_RANGES[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return LIMIT_DEFAULTS[key]
    return int(min(high, max(low, value)))


def limit_settings(prefs):
    """Effective limits from preferences: clamped, so a hand-edited file can never go below the floor."""
    prefs = prefs or {}
    return {k: _int(prefs.get(k, LIMIT_DEFAULTS[k]), k) for k in LIMIT_DEFAULTS}


def validate_limits(body):
    if not isinstance(body, dict) or not body or set(body) - set(LIMIT_RANGES):
        raise ValueError("请求节奏字段无效")
    out = {}
    for key, value in body.items():
        low, high = LIMIT_RANGES[key]
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise ValueError("%s 须为 %d–%d 的整数" % (key, low, high))
        out[key] = value
    return out


class RateLimiter:
    """Serializes 科研通 requests: any two are at least gap() seconds apart (measured from the end of the previous one)."""

    def __init__(self, gap=lambda: REQUEST_GAP_DEFAULT, clock=time.monotonic, sleep=time.sleep):
        self.gap, self.clock, self.sleep = gap, clock, sleep
        self.last = None
        self.lock = threading.Lock()   # one request in flight at a time

    def _gap(self):
        try:
            return max(REQUEST_GAP_MIN, min(REQUEST_GAP_MAX, float(self.gap())))
        except (TypeError, ValueError):
            return REQUEST_GAP_DEFAULT

    def delay(self):
        """Seconds a request issued right now would have to wait."""
        if self.last is None:
            return 0.0
        return max(0.0, self.last + self._gap() - self.clock())

    @contextlib.contextmanager
    def slot(self, stop=None):
        """with limiter.slot(): <one navigation/request>. Raises ValueError if a stop was requested while waiting."""
        with self.lock:
            wait = self.delay()
            if wait > 0:
                if stop is not None:
                    if stop.wait(wait):
                        raise ValueError("已请求停止，未发出下一次请求")
                else:
                    self.sleep(wait)
            try:
                yield
            finally:
                self.last = self.clock()


class PublishLimiter:
    """Publish spacing (>= 60 s) and a rolling one-hour cap. Timestamps are wall-clock seconds and may be persisted."""

    def __init__(self, settings=lambda: LIMIT_DEFAULTS, clock=time.time, load=None, save=None):
        self.settings, self.clock, self.load, self.save = settings, clock, load, save
        self.times = []
        self.lock = threading.Lock()

    def _times(self):
        stored = self.load() if self.load else self.times
        now = self.clock()
        return sorted(t for t in (stored or []) if isinstance(t, (int, float)) and now - 3600 < t <= now + 1)

    def wait_seconds(self):
        """0 when a publish may start now, else seconds until both spacing and hourly cap allow it."""
        cfg = limit_settings(self.settings())
        with self.lock:
            times = self._times()
            now = self.clock()
            wait = 0.0
            if times:
                wait = max(wait, times[-1] + cfg["publish_gap_seconds"] - now)
            if len(times) >= cfg["publish_per_hour"]:
                wait = max(wait, times[len(times) - cfg["publish_per_hour"]] + 3600 - now)
            return max(0.0, wait)

    def record(self):
        with self.lock:
            times = self._times() + [self.clock()]
            self.times = times
            if self.save:
                self.save(times)

    def recent(self):
        with self.lock:
            return len(self._times())

    @classmethod
    def for_store(cls, store, clock=time.time):
        """Limiter whose settings come from preferences and whose history persists in the queue file."""
        def load():
            return list(store.snapshot().get("publish_times") or [])

        def save(times):
            with store.lock:
                store.data["publish_times"] = times[-60:]
                store.save()
        return cls(lambda: store.snapshot().get("preferences", {}), clock, load, save)
