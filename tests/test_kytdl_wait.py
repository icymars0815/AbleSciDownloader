import unittest

from kytdl_client import item_states


class OaWaitStateTests(unittest.TestCase):
    def test_old_human_check_does_not_end_wait_during_retry(self):
        paper = {"id": "p1", "stage": "queued", "oa_check": {"status": "needs_human"}}
        running = item_states(paper, {"pipeline": {"oa_running": "p1"}})
        self.assertNotIn("needs_human", running)
        self.assertNotIn("oa_finished", running)
        finished = item_states(paper, {"pipeline": {"oa_running": None}})
        self.assertIn("needs_human", finished)
        self.assertIn("oa_finished", finished)
