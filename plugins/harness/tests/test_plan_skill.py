"""Tests for the plan skill's text (ticket #160): plans are created as status:proposed and the turn ends.

    python3 -m unittest plugins/harness/tests/test_plan_skill.py -v

The skill is instructions, not code, so these read plugins/harness/skills/plan/SKILL.md: every create command
carries `--proposed`, nothing asks for approval, the skill ends after creating and names `harness-board promote`,
and `--no-interview` (in the argument hint) skips the interview step.
"""
from __future__ import annotations

import os
import re
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL = os.path.join(os.path.dirname(HERE), "skills", "plan", "SKILL.md")
STEP = re.compile(r"(?m)^(\d+)\. ")


def skill_text():
    with open(SKILL, encoding="utf-8") as fh:
        return fh.read()


def front_and_body():
    text = skill_text()
    if not text.startswith("---"):
        return "", text
    front, rest = text[3:].split("\n---", 1)
    return front, rest.split("\n", 1)[1]


def steps(body):
    """The numbered steps of the skill, {number: text}."""
    marks = list(STEP.finditer(body))
    out = {}
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(body)
        out[int(m.group(1))] = body[m.start():end]
    return out


def step_named(body, name):
    """The step whose bold title is `name` (e.g. Interview), or an empty string."""
    return next((text for text in steps(body).values() if f"**{name}**" in text), "")


class PlanSkill(unittest.TestCase):
    def test_creates_proposed_and_ends_the_turn(self):
        _, body = front_and_body()
        creates = re.findall(r"`harness-board (?:epic|task) create[^`]*`", body)
        self.assertTrue(any("epic create" in c for c in creates), creates)
        self.assertTrue(any("task create" in c for c in creates), creates)
        for command in creates:
            with self.subTest(command=command):
                self.assertIn("--proposed", command)
        self.assertIn("harness-board promote <epic#>", body)
        self.assertRegex(body.lower(), r"end the turn")
        # The table shows the real issue numbers, so the issues exist before it is shown.
        last = steps(body)[max(steps(body))]
        self.assertIn("harness-board promote", last)
        self.assertNotIn("/harness:work", last)

    def test_no_approval_question_remains(self):
        front, body = front_and_body()
        lowered = body.lower()
        for word in ("approve", "approval", "on approval"):
            with self.subTest(word=word):
                self.assertNotIn(word, lowered)
        self.assertNotIn("approval", front.lower())
        # AskUserQuestion belongs to the interview alone.
        interview = step_named(body, "Interview")
        self.assertIn("AskUserQuestion", interview)
        self.assertEqual(body.count("AskUserQuestion"), interview.count("AskUserQuestion"))

    def test_no_interview_option(self):
        front, body = front_and_body()
        hint = next((ln for ln in front.splitlines() if ln.startswith("argument-hint:")), "")
        self.assertIn("--no-interview", hint)
        interview = step_named(body, "Interview")
        self.assertIn("--no-interview", interview)
        self.assertRegex(interview.lower(), r"skip")


if __name__ == "__main__":
    unittest.main()
