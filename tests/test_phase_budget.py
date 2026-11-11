import tempfile
import unittest
from pathlib import Path

from sale_monitor.budget import HostBudget
from sale_monitor.pipeline import saved_budget_use
from sale_monitor.runner import Collector

HOST = "shop.example"
CFG = {"stores": {"ark": {"adapter": "html", "seed_urls": ["https://shop.example/"], "product_patterns": ["/i/"],
                           "sale_patterns": [], "default_condition": "new"}}}


class Fake:
    def __init__(self, budget):
        self.budget = budget
        self.count = 0


class PhaseBudget(unittest.TestCase):
    def test_usage_resumes_only_for_the_same_stage_of_the_same_run(self):
        saved = {"budget": {"run_id": "r1", "phase": "discovery", "used": {HOST: 150}}}
        self.assertEqual(saved_budget_use(saved, "r1", "discovery"), {HOST: 150})
        self.assertIsNone(saved_budget_use(saved, "r1", "verification"))
        self.assertIsNone(saved_budget_use(saved, "r2", "discovery"))
        self.assertIsNone(saved_budget_use({}, "r1", "discovery"))

    def test_verification_keeps_its_own_budget_after_an_exhausted_discovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spent = HostBudget({"default": {"min_interval_seconds": 2}}, 300, used={HOST: 150}, sleep=lambda s: None)
            discovery = Collector(root, "ark", CFG, "r1", Fake(spent), phase="discovery")
            discovery.save()
            state = discovery.state
            self.assertEqual(state["budget"]["phase"], "discovery")
            self.assertEqual(state["budget"]["exhausted_hosts"], [HOST])
            fresh = HostBudget({"default": {"min_interval_seconds": 2}}, 240,
                               used=saved_budget_use(state, "r1", "verification"), sleep=lambda s: None)
            self.assertEqual(fresh.remaining(HOST), 120)
            plan = {"run_id": "r1", "confirm": {"ark": []}, "explore": {"ark": []}}
            verification = Collector(root, "ark", CFG, "r1", Fake(fresh), phase="verification", plan=plan)
            fresh.spend(HOST)
            verification.save()
            self.assertEqual(verification.state["budget"], {"run_id": "r1", "phase": "verification",
                                                            "used": {HOST: 1}, "exhausted_hosts": []})
            # The run total covers both stages, not only the last client.
            self.assertEqual(verification.state["request_count"], 151)
            self.assertEqual(verification.state["request_counts"]["by_phase"], {"discovery": 150, "verification": 1})


if __name__ == "__main__":
    unittest.main()
