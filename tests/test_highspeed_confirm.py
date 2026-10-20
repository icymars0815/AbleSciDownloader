import threading
import unittest
from unittest import mock

import browser_agent
from browser_agent import BrowserAgent, PreconditionError

RULE = "高速通道提示 高速通道积分规则 用户积分＜500：单个文件高速下载扣 2 积分，同一文件后续高速下载不重复扣分。 用户积分≥500：免积分 确认使用高速通道 取消"


class Buttons:
    def __init__(self, layer, text):
        self.layer, self.text = layer, text
    def filter(self, has_text):
        return Buttons(self.layer, has_text)
    def count(self):
        return 1 if self.text in self.layer.buttons else 0
    @property
    def first(self):
        return self
    def click(self):
        self.layer.clicked.append(self.text)


class Layer:
    def __init__(self, buttons=("确认使用高速通道", "取消")):
        self.buttons, self.clicked = buttons, []
    def locator(self, _selector):
        return Buttons(self, None)


def agent_with(layers):
    agent = object.__new__(BrowserAgent)
    agent.store = mock.Mock()
    agent.stop = threading.Event()
    agent.page = mock.Mock()
    agent.visible_layers = lambda: layers
    return agent


class HighspeedConfirmTests(unittest.TestCase):
    def test_confirms_dialog_when_tariff_matches(self):
        layer = Layer()
        agent = agent_with([(layer, RULE)])
        self.assertEqual(agent.confirm_highspeed("x"), "confirmed")
        self.assertEqual(layer.clicked, ["确认使用高速通道"])

    def test_tariff_mismatch_cancels_and_stops(self):
        layer = Layer()
        agent = agent_with([(layer, RULE.replace("扣 2 积分", "扣 5 积分"))])
        with self.assertRaises(PreconditionError):
            agent.confirm_highspeed("x")
        self.assertEqual(layer.clicked, ["取消"])

    def test_no_dialog_means_snoozed(self):
        agent = agent_with([])
        self.assertEqual(agent.confirm_highspeed("x", wait_ms=1000), "no_dialog")

    def test_unrelated_layer_is_not_clicked(self):
        layer = Layer(buttons=("确定",))
        agent = agent_with([(layer, "恭喜您，已经有人上传了文件 确定")])
        self.assertEqual(agent.confirm_highspeed("x", wait_ms=500), "no_dialog")
        self.assertEqual(layer.clicked, [])


if __name__ == "__main__":
    unittest.main()
