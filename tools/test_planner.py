import unittest
from unittest.mock import patch

from actions import planner


class PlannerTests(unittest.TestCase):
    def test_gate_accepts_obvious_compound_request(self):
        self.assertTrue(
            planner.should_plan(
                "open Blender and model this from these references"
            )
        )

    def test_gate_rejects_short_single_command(self):
        self.assertFalse(planner.should_plan("open Blender"))

    def test_plan_normalization_keeps_short_ordered_steps(self):
        raw = {
            "is_compound": True,
            "summary": "Open Blender and model the reference.",
            "requires_confirmation": False,
            "steps": [
                {
                    "command": " open Blender ",
                    "purpose": "open the modelling application",
                },
                {
                    "command": "model this in Blender",
                    "purpose": "create the model",
                },
            ],
        }

        result = planner._normalise_plan(raw)

        self.assertEqual(result["summary"], raw["summary"])
        self.assertEqual(
            [step["command"] for step in result["steps"]],
            ["open Blender", "model this in Blender"],
        )

    def test_plan_normalization_preserves_false_plan_as_no_steps(self):
        result = planner._normalise_plan({
            "is_compound": False,
            "summary": "",
            "requires_confirmation": False,
            "steps": [],
        })

        self.assertFalse(result["is_compound"])
        self.assertEqual(result["steps"], ())

    def test_execute_runs_steps_in_order(self):
        calls = []

        def dispatch(command):
            calls.append(command)
            return {
                "kind": "action",
                "intent": "test",
                "action": lambda: True,
            }

        steps = (
            {"command": "open Blender", "purpose": "open"},
            {"command": "make the helmet blue", "purpose": "modify"},
        )

        self.assertTrue(planner.execute(steps, dispatch))
        self.assertEqual(
            calls,
            ["open Blender", "make the helmet blue"],
        )

    def test_plan_uses_structured_provider_response(self):
        payload = (
            '{"is_compound":true,"summary":"I will do this in two steps.",'
            '"requires_confirmation":false,"steps":['
            '{"command":"open Blender","purpose":"open it"},'
            '{"command":"make the helmet blue","purpose":"modify it"}'
            ']}'
        )

        with patch.object(planner.providers, "chat", return_value=payload):
            result = planner.plan(
                "open Blender and make the helmet blue",
                "Blender is available.",
            )

        self.assertIsNotNone(result)
        self.assertEqual(len(result["steps"]), 2)
        self.assertEqual(result["steps"][1]["command"], "make the helmet blue")


if __name__ == "__main__":
    unittest.main()
