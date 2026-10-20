import unittest

from download_policy import RULES_VERSION, TransferWatch, highspeed_allowed, read_progress

RULES = "用户积分＜500，高速下载扣 2 积分，同一文件不重复扣分；用户积分≥500，免积分。"


class DownloadPolicyTests(unittest.TestCase):
    def test_parse_real_progress_units(self):
        result = read_progress("当前负载 112%\n已接收\n15.1 KB / 14.3 MB\n常见问题\n下载失败怎么办？")
        self.assertEqual(int(15.1 * 1024), result["received"])
        self.assertEqual("", result["error"])
        self.assertLess(result["percent"], 1)

    def test_faq_failure_words_not_transfer_errors(self):
        self.assertFalse(read_progress("下载中\n常见问题\n下载失败、超时怎么办？")["error"])

    def test_stream_progress_extends_stall_timer(self):
        watch = TransferWatch(0, 0)
        self.assertEqual("slow", watch.observe(40, 1024))
        self.assertEqual("slow", watch.observe(70, 2048))
        self.assertEqual("slow", watch.observe(116, 2048))
        self.assertEqual("stalled", watch.observe(191, 2048))

    def test_slow_transfer_continues_while_bytes_arrive(self):
        watch = TransferWatch(0, 0)
        for tick in range(10, 301, 10):
            outcome = watch.observe(tick, tick * 1024)
            self.assertNotIn(outcome, {"error", "stalled", "timeout"})
        self.assertEqual("slow", outcome)

    def test_speed_recovers_after_slow_period(self):
        watch = TransferWatch(0, 0)
        self.assertEqual("slow", watch.observe(30, 1024))
        self.assertEqual("receiving", watch.observe(60, 3 * 1024 * 1024))

    def test_reconnect_resets_byte_counter_and_stall_window(self):
        watch = TransferWatch(0, 0)
        watch.observe(10, 100000)
        self.assertEqual("connecting", watch.observe(40, 0))
        self.assertEqual("receiving", watch.observe(60, 1024))
        self.assertEqual("stalled", watch.observe(181, 1024))

    def test_per_line_time_limit_is_bounded(self):
        watch = TransferWatch(0, 0)
        for tick in range(10, 2401, 10):
            watch.observe(tick, tick * 1024)
        self.assertEqual("timeout", watch.observe(2401, 2401 * 1024))

    def test_connection_stall(self):
        self.assertNotEqual("stalled", TransferWatch(0, 0).observe(100, 0))
        self.assertEqual("stalled", TransferWatch(0, 0).observe(121, 0))

    def test_line_with_real_progress_gets_longer_stall_window(self):
        # Real 2026-10-19 trace: 21.7% of 14.3 MB, then a 60 s pause before resuming.
        total = 15_000_000
        watch = TransferWatch(0, 0)
        watch.observe(400, 3_250_585, "", total)
        self.assertNotEqual("stalled", watch.observe(650, 3_250_585, "", total))
        self.assertEqual("stalled", watch.observe(701, 3_250_585, "", total))
        early = TransferWatch(0, 0)
        early.observe(10, 200_000, "", total)
        self.assertEqual("stalled", early.observe(131, 200_000, "", total))

    def test_explicit_page_failure(self):
        self.assertEqual("error", TransferWatch(0, 0).observe(1, 0, "下载失败"))

    def test_paid_disabled_by_default(self):
        self.assertFalse(highspeed_allowed([1, 2, 3], {}, RULES)[0])

    def test_requires_three_distinct_failures(self):
        preferences = dict(allow_paid_fallback=True, rules_version=RULES_VERSION)
        self.assertFalse(highspeed_allowed([1, 1, 2], preferences, RULES)[0])
        self.assertFalse(highspeed_allowed([1, 2], preferences, RULES)[0])

    def test_all_three_fail_and_consent_and_matching_price(self):
        preferences = dict(allow_paid_fallback=True, rules_version=RULES_VERSION)
        self.assertTrue(highspeed_allowed([2, 1, 3], preferences, RULES)[0])

    def test_price_change_stops_fallback(self):
        preferences = dict(allow_paid_fallback=True, rules_version=RULES_VERSION)
        self.assertFalse(highspeed_allowed([1, 2, 3], preferences, RULES.replace("2 积分", "3 积分"))[0])

    def test_old_consent_is_not_reused(self):
        self.assertFalse(highspeed_allowed([1, 2, 3], dict(allow_paid_fallback=True, rules_version="old"), RULES)[0])
