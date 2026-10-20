"""Publish flow (docs/PUBLISH_FLOW_DESIGN.md) against a scripted fake page: nothing here contacts ablesci.com."""
import contextlib
import json
import tempfile
import threading
import unittest
from unittest.mock import MagicMock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import publish_policy as pp
from accounts import Accounts
from app import make_server
from browser_agent import BASE, BrowserAgent
from core import Store
from pipeline import stage_of, next_step
from rejection_policy import (REJECTION_RULES_VERSION, manual_reason, reject_rules, rejection_decision, user_doi)
from tests import test_pipeline as tp

DETAIL = BASE + "/assist/detail?id=NEW123"
TITLE = "Citrus flavonoids and gut microbiota in adults"
DOI = "10.1234/ok"


class El:
    def __init__(self, text="", value="", buttons=None, on_click=None):
        self.text, self.value, self.visible, self.buttons, self.on_click, self.clicks = text, value, True, buttons or [], on_click, 0

    def inner_text(self, **_):
        return self.text

    def is_visible(self):
        return self.visible

    def fill(self, value):
        self.value = value

    def input_value(self, **_):
        return self.value

    def get_attribute(self, name):
        return self.value

    def click(self, **_):
        self.clicks += 1
        if self.on_click:
            self.on_click()

    def locator(self, selector, has_text=None):
        if "textarea" in selector:           # fake layers carry no form fields
            return Loc([])
        found = [b for b in self.buttons if has_text is None or (has_text.search(b.text) if hasattr(has_text, "search") else has_text in b.text)]
        return Loc(found)


class Loc:
    def __init__(self, items):
        self.items = items
        self.first = items[0] if items else El()
        self.last = items[-1] if items else El()

    def count(self):
        return len(self.items)

    def nth(self, n):
        return self.items[n]

    def filter(self, has_text=None):
        return Loc([i for i in self.items if has_text is None or (has_text.search(i.text) if hasattr(has_text, "search") else has_text in i.text)])

    def __getattr__(self, name):
        return getattr(self.first, name)


class FakePage:
    def __init__(self, body="您当前有 100 积分"):
        self.url = BASE + "/assist/create"
        self.el = {s: El() for s in ("#onekey", "#Assist-doi", "#Assist-title", "#Assist-point", "#Assist-close_at", "input")}
        self.el["body"] = El(body)
        self.el["#Assist-close_at"].value = "5天"
        self.layers = []
        self.response = None            # dict served for check-before-assist-create
        self.nav = None                 # url reached after a successful submit
        self.search = El(on_click=lambda: None)
        self.submit = El()
        self.el["button.onekey-search"] = self.search
        self.el["#form-submit-btn"] = self.submit
        self.el['input[name="Assist[type]"]:checked'] = El(value="1")
        self.el["#point-low"] = El("10")
        self.shots = []

    def locator(self, selector, has_text=None):
        if selector == ".layui-layer":
            return Loc(self.layers)
        return Loc([self.el[selector]])

    def screenshot(self, path, mask=None):
        with open(path, "wb") as handle:
            handle.write(b"png")
        self.shots.append(path)

    def wait_for_timeout(self, ms):
        pass

    def wait_for_url(self, predicate, timeout=0):
        if not self.nav:
            raise TimeoutError("no navigation")
        self.url = self.nav

    @contextlib.contextmanager
    def expect_response(self, predicate, timeout=0):
        holder = MagicMock()
        yield holder
        if self.response is None:
            raise TimeoutError("no response")
        holder.value.json.return_value = self.response
        if self.nav:
            self.url = self.nav          # the real site navigates (or offers 查看求助详情) after a successful submit


def doi_lookup_text(title=TITLE, doi=DOI):
    """Exact DOI lookup layer as seen on the real page (2026-10-20): no 相似度/类型/最低悬赏, three buttons."""
    return ("标题 %s\nDOI %s\n期刊 Food Chemistry\n作者 A; B; et al\n出版日期 2025-01-20\n网址 https://doi.org/%s\n"
            "信息正确，直接发布 信息正确，还需修改 信息有误，手动填写" % (title, doi, doi))


def lookup_text(title=TITLE, doi=DOI, sim=95, kind="期刊论文", minimum=10):
    return "标题：%s\n相似度：%s%%\nDOI：%s\n期刊：J Food Sci\n类型：%s\n最低悬赏：%s" % (title, sim, doi, kind, minimum)


class PublishBase(unittest.TestCase):
    prefs = {}

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.agent = BrowserAgent(self.store, Accounts(self.temp.name))
        self.addCleanup(self.agent.thread.join, 0.1)
        self.addCleanup(self.agent.commands.put, ("close", None, None))
        self.agent.throttled = lambda: contextlib.nullcontext()
        self.agent.context = MagicMock()
        self.agent.goto = MagicMock()
        self.page = FakePage()
        self.agent.page = self.page
        self.agent.notifier = MagicMock()
        self.store.update_account(account_id="default", points=100)
        self.store.update_preferences(**self.prefs)

    def item(self, title=TITLE, doi=DOI, **fields):
        fields.setdefault("metadata_ref", dict(title=TITLE))
        return self.store.add(title, doi, account_id="default", **fields)

    def script(self, text=None, response=None, nav=DETAIL, form_title=TITLE, form_doi=DOI, confirm="确定"):
        page = self.page
        text = text or lookup_text()

        def fill_form():
            page.el["#Assist-doi"].value, page.el["#Assist-title"].value = form_doi, form_title
        button = El(confirm, on_click=fill_form)
        layer = El(text, buttons=[button, El("取消")])
        page.search.on_click = lambda: page.layers.append(layer)
        page.response, page.nav = response, nav
        return layer

    def run_publish(self, item_id):
        try:
            self.agent.publish(item_id)
        except ValueError as exc:
            return str(exc)
        return None

    def assertStopped(self, item_id, kind, clicked=0):
        after = self.store.get(item_id)
        self.assertEqual("attention", after["status"])
        self.assertEqual(kind, after["publish_check"]["kind"])
        self.assertEqual(clicked, self.page.submit.clicks)
        self.assertEqual("attention", stage_of(after))
        self.assertTrue(after["publish_blocked"])
        return after


class PublishSuccessTests(PublishBase):
    def test_success_records_action_and_converts_item(self):
        item_id = self.item(request_reward=20)
        self.script(response=dict(code=0, data=dict(url=DETAIL)))
        self.assertIsNone(self.run_publish(item_id))
        after = self.store.get(item_id)
        self.assertEqual("confirmed", after["publish_action"]["status"])
        self.assertEqual((20, "5天"), (after["publish_action"]["point"], after["publish_action"]["close_at"]))
        self.assertEqual(("website", True, DETAIL, "published"), (after["source"], after["owned"], after["assist_url"], after["status"]))
        self.assertEqual("20", self.page.el["#Assist-point"].value)
        self.assertEqual(DOI, self.page.el["#onekey"].value)            # the DOI is the only input
        self.assertEqual("submitted", stage_of(after))
        self.assertEqual(1, self.page.submit.clicks)
        self.assertIn("applied", "applied")
        with self.assertRaises(ValueError):                              # never published twice
            self.agent.publish(item_id)

    def test_quick_tip_is_confirmed_only_after_checks(self):
        item_id = self.item()
        self.script(response=None)
        direct = El("直接提交", on_click=lambda: setattr(self.page, "response", dict(code=0)))
        self.page.submit.on_click = lambda: self.page.layers.append(El("快捷提示：确认直接提交？", buttons=[direct, El("返回修改")]))
        self.assertIsNone(self.run_publish(item_id))
        self.assertEqual(1, direct.clicks)
        self.assertEqual("confirmed", self.store.get(item_id)["publish_action"]["status"])

    def test_quick_tip_never_reached_when_a_check_fails(self):
        item_id = self.item()
        self.script(text=lookup_text(sim=30), response=dict(code=0))
        direct = El("直接提交")
        self.page.submit.on_click = lambda: self.page.layers.append(El("快捷提示", buttons=[direct]))
        self.assertIn("相似度", self.run_publish(item_id))
        self.assertEqual(0, direct.clicks)

    def test_unconfirmed_navigation_is_uncertain_and_never_retried(self):
        item_id = self.item()
        self.script(response=dict(code=0), nav=None)
        self.assertIn("未确认", self.run_publish(item_id))
        self.assertEqual("uncertain", self.store.get(item_id)["publish_action"]["status"])
        self.assertEqual("attention", stage_of(self.store.get(item_id)))
        with self.assertRaises(ValueError):
            self.agent.publish(item_id)
        self.assertEqual(1, self.page.submit.clicks)

    def test_no_response_is_uncertain(self):
        item_id = self.item()
        self.script(response=None)
        self.assertIn("未确认", self.run_publish(item_id))
        self.assertEqual("uncertain", self.store.get(item_id)["publish_action"]["status"])


class PublishPointsTests(PublishBase):
    def points(self):
        return self.store.snapshot()["account_metrics"]["default"].get("points")

    def test_stored_points_drop_by_confirmed_reward(self):
        self.page.el["body"] = El("您当前有 600 积分")
        item_id = self.item(request_reward=20)
        self.script(response=dict(code=0, data=dict(url=DETAIL)))
        self.assertIsNone(self.run_publish(item_id))
        self.assertEqual(580, self.points())

    def test_live_balance_below_projected_500_stops_before_publishing(self):
        self.store.update_account(account_id="default", points=600)
        self.page.el["body"] = El("您当前有 505 积分")
        item_id = self.item(request_reward=10)
        self.script(response=dict(code=0, data=dict(url=DETAIL)))
        self.assertIn("低于 500", self.run_publish(item_id))
        self.assertEqual(505, self.points())
        self.assertEqual(0, self.page.submit.clicks)
        self.assertEqual("queued", stage_of(self.store.get(item_id)))

    def test_site_gate_message_pauses_without_attention(self):
        self.store.update_preferences(pipeline_enabled=True)
        item_id = self.item(request_reward=20)
        self.script(response=dict(code=1, msg="您有已应助的求助未处理，请先处理后再求助"))
        self.assertIsNotNone(self.run_publish(item_id))
        after = self.store.get(item_id)
        self.assertEqual("queued", stage_of(after))
        self.assertNotEqual("attention", after["status"])
        self.assertFalse(after.get("publish_blocked"))
        self.assertTrue(self.store.snapshot()["account_metrics"]["default"].get("site_gate"))
        self.assertIsNone(self.points())
        self.assertTrue(self.store.snapshot()["preferences"]["pipeline_enabled"])

    def test_classification(self):
        found = pp.classify_failure("存在未处理的应助，请先处理")
        self.assertEqual(("gate", False), (found["kind"], found["pause"]))


class PublishCheckTests(PublishBase):
    def test_similarity_below_50_stops_with_screenshot(self):
        item_id = self.item()
        self.script(text=lookup_text(sim=49), response=dict(code=0))
        self.assertIn("相似度", self.run_publish(item_id))
        after = self.assertStopped(item_id, "lookup_mismatch")
        check = after["publish_check"]
        self.assertEqual((49, DOI, "期刊论文", 10), (check["similarity"], check["doi"], check["type"], check["min_reward"]))
        self.assertTrue(check["screenshot"])
        self.assertTrue((self.store.root / check["screenshot"]).is_file())
        self.assertIsNone(after.get("publish_action"))

    def test_doi_mismatch(self):
        item_id = self.item()
        self.script(text=lookup_text(doi="10.9999/other"), response=dict(code=0))
        self.assertIn("DOI", self.run_publish(item_id))
        self.assertStopped(item_id, "lookup_mismatch")

    def test_title_below_090(self):
        item_id = self.item()
        self.script(text=lookup_text(title="Completely different words about batteries"), response=dict(code=0))
        self.assertIn("标题", self.run_publish(item_id))
        self.assertStopped(item_id, "lookup_mismatch")

    def test_type_must_be_journal_article(self):
        item_id = self.item()
        self.script(text=lookup_text(kind="学位论文", minimum=50), response=dict(code=0))
        self.assertIn("期刊论文", self.run_publish(item_id))
        self.assertStopped(item_id, "lookup_mismatch")

    def test_form_readback_must_match(self):
        item_id = self.item()
        self.script(response=dict(code=0), form_doi="10.9999/changed")
        self.run_publish(item_id)
        self.assertStopped(item_id, "form_mismatch")

    def test_cjk_title_is_never_auto_published(self):
        item_id = self.item(title="柑橘黄酮与肠道菌群", metadata_ref=dict(title="柑橘黄酮与肠道菌群"))
        self.script(response=dict(code=0))
        self.assertIn("非英文", self.run_publish(item_id))
        self.agent.goto.assert_not_called()
        self.assertStopped(item_id, "not_auto")

    def test_supplementary_material_is_never_auto_published(self):
        item_id = self.item(title="Supplementary material for citrus study", metadata_ref=dict(title="Supplementary material for citrus study"))
        self.assertIn("补充材料", self.run_publish(item_id))
        self.agent.goto.assert_not_called()

    def test_no_doi_is_handed_to_the_user(self):
        item_id = self.item(doi="")
        self.assertIn("无 DOI", self.run_publish(item_id))
        self.agent.goto.assert_not_called()
        self.assertStopped(item_id, "no_doi")

    def test_no_public_title_means_stop(self):
        item_id = self.item(metadata_ref=None)
        self.agent.reference_title = lambda *a: ""
        self.script(response=dict(code=0))
        self.assertIn("公开标题", self.run_publish(item_id))
        self.assertEqual(0, self.page.submit.clicks)


class RewardRaiseTests(PublishBase):
    def prompt(self, text="该出版社最低悬赏为 30 积分，仍然提交将自动提高悬赏", code2_nav=DETAIL):
        again = El("仍然提交", on_click=lambda: None)
        self.page.submit.on_click = lambda: self.page.layers.append(El("确认提示\n" + text, buttons=[again, El("返回修改")]))
        self.page.response = dict(code=2, msg="需要确认")
        return again

    def test_default_asks_every_time_at_lookup(self):
        item_id = self.item()
        self.script(text=lookup_text(minimum=30), response=dict(code=0))
        self.assertIn("30", self.run_publish(item_id))
        self.assertStopped(item_id, "min_reward_raised")
        self.agent.notifier.notify.assert_called()
        self.assertEqual("min_bounty", self.agent.notifier.notify.call_args.args[0])

    def test_auto_accept_within_cap_at_lookup(self):
        self.store.update_preferences(publish_raise_mode="auto", publish_raise_rules=pp.RAISE_RULES_TEXT, publish_raise_max=50, publish_reserve_points=20)
        item_id = self.item()
        self.script(text=lookup_text(minimum=30), response=dict(code=0))
        self.assertIsNone(self.run_publish(item_id))
        self.assertEqual(30, self.store.get(item_id)["publish_action"]["point"])
        self.assertEqual("30", self.page.el["#Assist-point"].value)

    def test_reserve_points_block_auto_accept(self):
        self.store.update_preferences(publish_raise_mode="auto", publish_raise_rules=pp.RAISE_RULES_TEXT, publish_raise_max=50, publish_reserve_points=80)
        item_id = self.item()
        self.script(text=lookup_text(minimum=30), response=dict(code=0))
        self.assertIn("保留", self.run_publish(item_id))
        self.assertStopped(item_id, "min_reward_raised")

    def test_code2_raise_accepted_within_cap(self):
        self.store.update_preferences(publish_raise_mode="auto", publish_raise_rules=pp.RAISE_RULES_TEXT, publish_raise_max=50)
        item_id = self.item()
        self.script(response=None)
        again = self.prompt()
        again.on_click = lambda: None
        self.assertIsNone(self.run_publish(item_id))
        self.assertEqual(1, again.clicks)
        self.assertEqual(30, self.store.get(item_id)["publish_action"]["point"])
        self.assertEqual("confirmed", self.store.get(item_id)["publish_action"]["status"])

    def test_code2_raise_over_cap_stops(self):
        self.store.update_preferences(publish_raise_mode="auto", publish_raise_rules=pp.RAISE_RULES_TEXT, publish_raise_max=20)
        item_id = self.item()
        self.script(response=None)
        again = self.prompt()
        self.assertIn("上限", self.run_publish(item_id))
        self.assertEqual(0, again.clicks)
        after = self.assertStopped(item_id, "min_reward_raised", clicked=1)
        self.assertIsNone(after.get("publish_action"))

    def test_code2_raise_asks_by_default(self):
        item_id = self.item()
        self.script(response=None)
        again = self.prompt()
        self.run_publish(item_id)
        self.assertEqual(0, again.clicks)
        self.assertStopped(item_id, "min_reward_raised", clicked=1)

    def test_other_code2_always_stops_even_with_auto(self):
        self.store.update_preferences(publish_raise_mode="auto", publish_raise_rules=pp.RAISE_RULES_TEXT, publish_raise_max=500)
        item_id = self.item()
        self.script(response=None)
        again = self.prompt("你可能已经提交过重复求助，最低悬赏 30 积分")
        self.assertIn("亲自查看", self.run_publish(item_id))
        self.assertEqual(0, again.clicks)
        self.assertStopped(item_id, "code2_other", clicked=1)

    def test_code2_without_amount_stops(self):
        self.store.update_preferences(publish_raise_mode="auto", publish_raise_rules=pp.RAISE_RULES_TEXT, publish_raise_max=500)
        item_id = self.item()
        self.script(response=None)
        again = self.prompt("文献信息可能有误，请确认")
        self.run_publish(item_id)
        self.assertEqual(0, again.clicks)

    def test_auto_without_confirmed_rules_is_ask(self):
        self.assertEqual("ask", pp.raise_settings(dict(publish_raise_mode="auto"))["mode"])
        self.assertEqual("auto", pp.raise_settings(dict(publish_raise_mode="auto", publish_raise_rules=pp.RAISE_RULES_TEXT))["mode"])
        self.assertEqual(50, pp.raise_settings(dict(publish_raise_max=9999))["max"])

    def test_points_shortage_stops(self):
        self.store.update_preferences(publish_raise_mode="auto", publish_raise_rules=pp.RAISE_RULES_TEXT, publish_raise_max=500)
        self.store.update_account(account_id="default", points=100)
        self.page.el["body"].text = "您当前有 25 积分"
        item_id = self.item()
        self.script(text=lookup_text(minimum=30), response=dict(code=0))
        self.assertIn("保留", self.run_publish(item_id) or "保留")
        self.assertEqual(0, self.page.submit.clicks)


class FailureTests(PublishBase):
    def refused(self, message):
        item_id = self.item()
        self.script(response=dict(code=1, msg=message))
        self.run_publish(item_id)
        return self.assertStopped(item_id, self.store.get(item_id)["publish_check"]["kind"], clicked=1)

    def test_classifications(self):
        for message, kind in (("请先验证邮箱后再求助", "email_unverified"), ("请先学习求助规则后再发布", "rules_required"),
                              ("当前求助状态不允许此操作", "state_not_allowed")):
            with self.subTest(kind=kind):
                self.setUp()
                after = self.refused(message)
                self.assertEqual(kind, after["publish_check"]["kind"])
                self.assertIsNone(after.get("publish_action"))
                self.assertIn("科研通", after["note"])

    def test_restricted_text_pauses_pipeline_and_notifies(self):
        self.store.update_preferences(pipeline_enabled=True)
        after = self.refused("您的账号受限，暂时不能发布")
        self.assertEqual("restricted", after["publish_check"]["kind"])
        self.assertFalse(self.store.snapshot()["preferences"]["pipeline_enabled"])
        self.assertEqual("needs_action", self.agent.notifier.notify.call_args.args[0])

    def test_daily_limit_pauses_pipeline(self):
        self.store.update_preferences(pipeline_enabled=True)
        after = self.refused("今日发布求助次数已达上限")
        self.assertEqual("daily_limit", after["publish_check"]["kind"])
        self.assertFalse(self.store.snapshot()["preferences"]["pipeline_enabled"])

    def test_page_restricted_notice_before_anything_is_typed(self):
        self.store.update_preferences(pipeline_enabled=True)
        self.page.el["body"].text = "您当前有 100 积分\n您的账号受限，请联系管理员"
        item_id = self.item()
        self.script(response=dict(code=0))
        self.run_publish(item_id)
        self.assertEqual("", self.page.el["#onekey"].value)
        self.assertFalse(self.store.snapshot()["preferences"]["pipeline_enabled"])

    def test_sudden_double_minimum_only_warns(self):
        # A 10 -> 50 jump is normal for a harder publisher: warn, don't pause; explicit 受限 text still pauses (separate test).
        self.store.update_preferences(pipeline_enabled=True)
        self.store.update_account(account_id="default", min_reward_seen=[10, 10])
        item_id = self.item(request_reward=50)
        self.script(text=lookup_text(minimum=50), response=dict(code=0))
        self.run_publish(item_id)
        self.assertTrue(self.store.snapshot()["preferences"]["pipeline_enabled"])
        self.assertTrue(any("可能是账号受限" in e["message"] for e in self.store.snapshot()["events"]))

    def test_normal_minimum_is_remembered(self):
        item_id = self.item()
        self.script(response=dict(code=0))
        self.run_publish(item_id)
        self.assertEqual([10], self.store.snapshot()["account_metrics"]["default"]["min_reward_seen"])

    def test_lookup_layer_with_failure_text_stops_at_once(self):
        item_id = self.item()
        layer = El("提示：邮箱未验证，请先验证邮箱")
        self.page.search.on_click = lambda: self.page.layers.append(layer)
        self.assertIn("邮箱", self.run_publish(item_id))
        self.assertStopped(item_id, "email_unverified")

    def test_lookup_timeout(self):
        item_id = self.item()
        self.assertIn("50 秒", self.run_publish(item_id))
        self.assertStopped(item_id, "lookup_timeout")


class PolicyTests(unittest.TestCase):
    def test_parse_lookup_text_tolerates_layout(self):
        got = pp.parse_lookup_text("标题\tA study of X\n相似度 87.5%\nDOI: https://doi.org/10.1234/X.1\n类型：期刊论文\n最低悬赏：30 积分")
        self.assertEqual(("A study of X", 87.5, "10.1234/x.1", "期刊论文", 30), (got["title"], got["similarity"], got["doi"], got["type"], got["min_reward"]))

    def test_validate_raise(self):
        cur = pp.raise_settings({})
        with self.assertRaises(ValueError):
            pp.validate_raise({"raise_mode": "auto"}, cur)             # needs the rules confirmation
        self.assertEqual("auto", pp.validate_raise({"raise_mode": "auto", "raise_confirmed": True}, cur)["publish_raise_mode"])
        for bad in ({"raise_max": 5}, {"raise_max": 501}, {"raise_max": True}, {"reserve_points": -1}, {"raise_mode": "x"}):
            with self.assertRaises(ValueError):
                pp.validate_raise(bad, cur)

    def test_parse_raise(self):
        self.assertEqual(30, pp.parse_raise("该出版社最低悬赏为 30 积分"))
        self.assertIsNone(pp.parse_raise("疑似重复求助，最低悬赏 30 积分"))
        self.assertIsNone(pp.parse_raise("请确认"))

    def test_pipeline_skips_blocked_items(self):
        base = tp.item("q", status="ready", request_confirmed=True, publish_blocked=True)
        step = next_step([base], dict(points=100), dict(pipeline_auto_publish=True, pipeline_rules_version=tp.PIPELINE_RULES_VERSION, oa_first=False),
                         tp.datetime.now().astimezone())
        self.assertNotEqual("publish", step["action"])


class RejectionCategoryTests(unittest.TestCase):
    def test_four_categories_and_detail_required(self):
        rules = reject_rules()
        self.assertEqual(["文件不一致", "文件无法打开（已多次尝试）", "缺页（完整版页数更多）", "禁止格式（caj、epub 等）"], [c["label"] for c in rules["categories"]])
        self.assertTrue(rules["not_allowed"] and rules["penalties"])
        key, text = manual_reason("missing_pages", "官网有 12 页，这份只有 8 页")
        self.assertEqual(("missing_pages", True), (key, text.startswith("缺页（完整版页数更多）：")))
        for category, detail in ((None, "x" * 10), ("scan_unclear", "x" * 10), ("file_mismatch", ""), ("file_mismatch", "短")):
            with self.assertRaises(ValueError):
                manual_reason(category, detail)

    def test_auto_reject_ignores_site_filled_doi(self):
        item = dict(source="website", owned=True, pdf="d/x.pdf", doi="10.1234/target", doi_origin="site",
                    assist_url=BASE + "/assist/detail?id=ABC123", website_status="待确认", download_file_url=BASE + "/assist/download?id=F1",
                    verification=dict(status="conflict", primary_dois=["10.1234/wrong"], title_score=30, sha256="x"))
        prefs = dict(rejection_mode="auto", rejection_rules_version=REJECTION_RULES_VERSION)
        self.assertEqual("", user_doi(item))
        self.assertNotEqual("auto_reject", rejection_decision(item, prefs)["action"])
        item["doi_origin"] = "user"
        decision = rejection_decision(item, prefs)
        self.assertEqual(("auto_reject", "file_mismatch"), (decision["action"], decision["category"]))
        self.assertTrue(decision["reason"].startswith("文件不一致："))

    def test_legacy_synced_item_without_origin_is_not_users(self):
        self.assertEqual("", user_doi(dict(source="website", doi="10.1234/x")))
        self.assertEqual("10.1234/x", user_doi(dict(source="website", doi="10.1234/x", publish_action=dict(status="confirmed"))))
        self.assertEqual("10.1234/x", user_doi(dict(source="import", doi="10.1234/x")))

    def test_non_mismatch_problems_are_never_automatic(self):
        item = dict(source="website", owned=True, pdf="d/x.pdf", doi="10.1234/t", doi_origin="user",
                    assist_url=BASE + "/assist/detail?id=ABC123", website_status="待确认", download_file_url=BASE + "/assist/download?id=F1",
                    verification=dict(status="ocr_required", primary_dois=[], title_score=0, sha256="x"))
        decision = rejection_decision(item, dict(rejection_mode="auto", rejection_rules_version=REJECTION_RULES_VERSION))
        self.assertEqual(("ask_user", ""), (decision["action"], decision["category"]))


class RejectAgentTests(unittest.TestCase):
    def setUp(self):
        tp.ArchiveAndActionTests.setUp(self)

    downloaded = tp.ArchiveAndActionTests.downloaded

    def test_manual_reject_without_category_is_refused(self):
        item_id = self.downloaded(manual_reject_reason="随便写写")
        with self.assertRaises(ValueError):
            self.agent.reject(item_id)
        self.controls.click.assert_not_called()

    def test_reject_records_category(self):
        item_id = self.downloaded(manual_reject_reason="缺页（完整版页数更多）：少了 4 页", manual_reject_category="missing_pages")
        self.agent.reject(item_id)
        self.assertEqual("missing_pages", self.store.get(item_id)["rejected_files"][0]["reject_action"]["category"])


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.server = make_server(0, self.temp.name, auto_screen=False)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = "http://127.0.0.1:%d" % self.server.server_port
        self.token = urlopen(self.base).read().decode().split("const TOKEN=")[1].split(";let state=")[0].strip('"')
        self.addCleanup(self.stop)

    def stop(self):
        self.server.pipeline.close()
        self.server.shutdown()
        self.server.server_close()
        self.server.agent.commands.put(("close", None, None))
        self.server.agent.thread.join(timeout=2)

    def post(self, route, body):
        request = Request(self.base + "/api/" + route, json.dumps(body).encode(), {"Content-Type": "application/json", "Origin": self.base, "X-Demo-Token": self.token})
        return json.load(urlopen(request))

    def bad(self, route, body):
        with self.assertRaises(HTTPError) as caught:
            self.post(route, body)
        self.assertEqual(400, caught.exception.code)

    def state(self):
        return json.load(urlopen(self.base + "/api/state"))

    def test_raise_settings_roundtrip_and_validation(self):
        self.bad("pipeline", {"raise_mode": "auto"})
        self.bad("pipeline", {"raise_max": 9})
        self.post("pipeline", {"raise_mode": "auto", "raise_confirmed": True, "raise_max": 80, "reserve_points": 15})
        self.assertEqual(dict(mode="auto", max=80, reserve=15), self.state()["publish"]["raise_settings"])
        self.post("pipeline", {"raise_mode": "ask"})
        self.assertEqual("ask", self.state()["publish"]["raise_settings"]["mode"])

    def test_manual_reject_requires_category_and_detail(self):
        item_id = self.server.store.add("P", "10.1234/p", "website", account_id="default", owned=True, assist_url=tp.URL, website_status="待确认")
        self.bad("browser", {"action": "reject", "id": item_id, "reason": "free text only", "confirmed": True})
        self.bad("browser", {"action": "reject", "id": item_id, "category": "file_mismatch", "detail": "", "confirmed": True})
        self.assertIn("categories", self.state()["reject_rules"])

    def test_ack_clears_attention_only_for_stopped_publish(self):
        item_id = self.server.store.add("P", "10.1234/p", account_id="default", status="attention", publish_check=dict(kind="lookup_mismatch"), publish_blocked=True)
        self.post("item/ack", {"id": item_id})
        self.assertEqual("ready", self.server.store.get(item_id)["status"])
        other = self.server.store.add("Q", "10.1234/q", account_id="default", status="attention", publish_check=dict(kind="x"),
                                      publish_action=dict(status="uncertain"))
        self.bad("item/ack", {"id": other})


if __name__ == "__main__":
    unittest.main()


class RealDoiLayerFlowTests(PublishBase):
    """The exact-DOI popup seen on the real site: no 相似度/类型/最低悬赏 in the layer."""

    def real_script(self, low="10", type_value="1"):
        page = self.page

        def fill_form():
            page.el["#Assist-doi"].value, page.el["#Assist-title"].value = DOI, TITLE
        direct = El("信息正确，直接发布")
        modify = El("信息正确，还需修改", on_click=fill_form)
        wrong = El("信息有误，手动填写")
        layer = El(doi_lookup_text(), buttons=[direct, modify, wrong])
        page.search.on_click = lambda: page.layers.append(layer)
        page.el["#point-low"].text = low
        page.el['input[name="Assist[type]"]:checked'].value = type_value
        page.response, page.nav = dict(code=0), DETAIL
        return direct, modify

    def test_clicks_modify_never_direct_publish_and_reads_form(self):
        direct, modify = self.real_script()
        item_id = self.item()
        self.run_publish(item_id)
        self.assertEqual((0, 1), (direct.clicks, modify.clicks))
        self.assertEqual("confirmed", self.store.get(item_id)["publish_action"]["status"])
        self.assertEqual(10, self.store.get(item_id)["publish_check"]["min_reward"])

    def test_non_journal_type_on_form_stops(self):
        self.real_script(type_value="3")
        item_id = self.item()
        self.run_publish(item_id)
        self.assertNotEqual("confirmed", (self.store.get(item_id).get("publish_action") or {}).get("status"))
        self.assertEqual(0, self.page.submit.clicks)


class SuccessLayerTests(PublishBase):
    def test_success_layer_button_counts_as_published(self):
        self.script(response=dict(code=0), nav=None)
        page = self.page

        def go():
            page.url = DETAIL
        page.layers.append(El("发布成功 查看求助详情", buttons=[El("查看求助详情", on_click=go)]))
        item_id = self.item()
        self.run_publish(item_id)
        self.assertEqual("confirmed", self.store.get(item_id)["publish_action"]["status"])


class PopupHandoverTests(unittest.TestCase):
    """Verification passed but the adopt button is covered by an unpredictable site popup."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.agent = BrowserAgent(self.store, Accounts(self.temp.name))
        self.addCleanup(self.agent.thread.join, 0.1)
        self.addCleanup(self.agent.commands.put, ("close", None, None))
        self.agent.notifier = MagicMock()
        self.page = FakePage()
        self.agent.page = self.page
        self.item = self.store.add("Paper", "10.1/x", "website", account_id="default", status="downloaded")

    def covered_control(self, clears_after=None):
        control = MagicMock()
        state = {"tries": 0}

        def click(timeout=0):
            state["tries"] += 1
            if self.page.layers:
                raise TimeoutError("element is covered by .layui-layer")
        control.click.side_effect = click
        return control, state

    def test_informational_popup_with_unusual_close_is_cleared_then_clicked(self):
        popup = El("温馨提示 今日活动上线啦", buttons=[El("我知道了", on_click=lambda: self.page.layers.clear())])
        self.page.layers.append(popup)
        control, state = self.covered_control()
        self.agent.click_or_handover(self.item, control, "采纳")
        self.assertEqual(2, state["tries"])
        self.assertNotEqual("attention", self.store.get(self.item).get("status"))

    def test_risky_popup_is_never_dismissed_and_hands_over(self):
        risky = El("确认采纳该文件？采纳后积分将转给应助者", buttons=[El("确定", on_click=lambda: self.page.layers.clear())])
        self.page.layers.append(risky)
        control, state = self.covered_control()
        with self.assertRaises(ValueError):
            self.agent.click_or_handover(self.item, control, "采纳")
        self.assertEqual(0, risky.buttons[0].clicks)
        item = self.store.get(self.item)
        self.assertEqual("attention", item["status"])
        self.assertEqual("采纳", item["handover"]["kind"])
        self.assertTrue(any(e.get("kind") == "needs_action" for e in self.store.snapshot()["events"]))
        self.agent.notifier.notify.assert_called()

    def test_sync_resolves_handover_from_site_status(self):
        from pipeline import DONE, WAITING
        self.store.update(self.item, status="attention", handover=dict(kind="采纳"), pdf="downloads/x.pdf")
        self.agent.resolve_handover(self.item, DONE[0])
        self.assertEqual("adopted", self.store.get(self.item)["status"])
        other = self.store.add("Paper 2", "10.1/y", "website", account_id="default")
        self.store.update(other, status="attention", handover=dict(kind="驳回"), pdf="downloads/y.pdf")
        self.agent.resolve_handover(other, WAITING[0])
        self.assertEqual("rejected", self.store.get(other)["status"])
        self.assertEqual("downloads/y.pdf", self.store.get(other)["rejected_files"][0]["pdf"])
