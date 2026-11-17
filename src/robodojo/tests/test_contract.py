"""Checks of the Direct action contract: schema, bounds, identity and fallback."""

from copy import deepcopy
import math
import unittest

from robodojo.direct.contract import (
    DecisionError, decision_schema, hold_decision, rotation_angle, validate_decision,
)

from helpers import hold


def request():
    return {
        "request_id": "abc123",
        "current_eef": {
            "left": {"position": [0.1, 0.2, 0.3], "quaternion_wxyz": [1.0, 0.0, 0.0, 0.0],
                     "gripper_opening_command": 1.0},
            "right": {"position": [0.1, -0.2, 0.3], "quaternion_wxyz": [1.0, 0.0, 0.0, 0.0],
                      "gripper_opening_command": 0.0},
        },
    }


def rotated(angle):
    """Unit wxyz quaternion for a rotation of ``angle`` radians about z."""
    return [math.cos(angle / 2), 0.0, 0.0, math.sin(angle / 2)]


class ContractTests(unittest.TestCase):
    def test_valid_decision_is_normalized(self):
        decision = hold(request(), steps=3)
        decision["target"]["left"]["position"] = [0.1, 0.2, 0.34]
        decision["target"]["right"]["gripper_closed"] = True
        result = validate_decision(decision, request())
        self.assertEqual(result["steps"], 3)
        self.assertEqual(result["target"]["left"]["position"], [0.1, 0.2, 0.34])
        self.assertIs(result["target"]["right"]["gripper_closed"], True)

    def test_translation_and_rotation_limits_are_inclusive(self):
        for position, quaternion, valid in (
            ([0.15, 0.2, 0.3], rotated(0.0), True),
            ([0.151, 0.2, 0.3], rotated(0.0), False),
            ([0.1, 0.2, 0.3], rotated(0.35), True),
            ([0.1, 0.2, 0.3], rotated(0.36), False),
            # The same orientation with the opposite sign is not a rotation.
            ([0.1, 0.2, 0.3], [-1.0, 0.0, 0.0, 0.0], True),
        ):
            with self.subTest(position=position, quaternion=quaternion):
                decision = hold(request())
                decision["target"]["left"].update(position=position, quaternion_wxyz=quaternion)
                if valid:
                    validate_decision(decision, request())
                else:
                    with self.assertRaises(DecisionError):
                        validate_decision(decision, request())

    def test_malformed_decisions_are_rejected(self):
        base = hold(request())
        mutations = (
            lambda d: d.update(request_id="stale"),
            lambda d: d.update(mode="joint"),
            lambda d: d.update(mode="stop"),
            lambda d: d.update(steps=0),
            lambda d: d.update(steps=6),
            lambda d: d.update(steps=True),
            lambda d: d.update(steps=2.0),
            lambda d: d.update(reason="  "),
            lambda d: d.update(reason=None),
            lambda d: d.update(extra=1),
            lambda d: d.pop("reason"),
            lambda d: d["target"].pop("right"),
            lambda d: d["target"].update(middle=d["target"]["left"]),
            lambda d: d["target"]["left"].update(position=[0.1, 0.2]),
            lambda d: d["target"]["left"].update(position=[0.1, 0.2, float("nan")]),
            lambda d: d["target"]["left"].update(position=[0.1, 0.2, float("inf")]),
            lambda d: d["target"]["left"].update(position=[0.1, 0.2, "0.3"]),
            lambda d: d["target"]["left"].update(position=[0.1, 0.2, True]),
            lambda d: d["target"]["left"].update(quaternion_wxyz=[1.0, 0.0, 0.0]),
            lambda d: d["target"]["left"].update(quaternion_wxyz=[2.0, 0.0, 0.0, 0.0]),
            lambda d: d["target"]["left"].update(quaternion_wxyz=[0.0, 0.0, 0.0, 0.0]),
            lambda d: d["target"]["left"].update(gripper_closed=1),
            lambda d: d["target"]["left"].update(gripper_opening=0.5),
            lambda d: d["target"]["left"].pop("gripper_closed"),
        )
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index):
                decision = deepcopy(base)
                mutate(decision)
                with self.assertRaises(DecisionError):
                    validate_decision(decision, request())
        for value in (None, [], "text", 3):
            with self.assertRaises(DecisionError):
                validate_decision(value, request())

    def test_rotation_angle_uses_wxyz_order(self):
        self.assertAlmostEqual(rotation_angle(rotated(0.3), rotated(0.0)), 0.3)
        self.assertAlmostEqual(rotation_angle(rotated(0.3), rotated(-0.2)), 0.5)
        # A quaternion read in xyzw order would look like a half turn.
        self.assertAlmostEqual(rotation_angle([0.0, 0.0, 0.0, 1.0], [1.0, 0.0, 0.0, 0.0]), math.pi)

    def test_hold_decision_is_valid_and_repeats_the_gripper_command(self):
        decision = hold_decision(request(), "Scaffold hold.")
        self.assertEqual(validate_decision(decision, request()), decision)
        self.assertIs(decision["target"]["left"]["gripper_closed"], False)
        self.assertIs(decision["target"]["right"]["gripper_closed"], True)
        self.assertEqual(decision["steps"], 1)

    def test_schema_matches_validator_fields(self):
        schema = decision_schema()
        self.assertEqual(set(schema["required"]), {"request_id", "mode", "steps", "reason", "target"})
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(schema["properties"]["steps"]["maximum"], 5)
        self.assertEqual(schema["properties"]["mode"]["enum"], ["eef"])
        self.assertEqual(schema["properties"]["target"]["required"], ["left", "right"])


if __name__ == "__main__":
    unittest.main()
