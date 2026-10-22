"""Tests for plugins/harness/lib/harness_lib/routing.py (ticket #173, epic #172): the routing
settings, the tier rubric and each stage's route, in one module so harness-board,
harness-work and harness-pr agree.

- label_tier: docs/design.md's sentence ("size:S and risk:low and type not bug gives sonnet;
  size:L or risk:high gives fable; everything else gives opus"), checked for every type, size
  and risk, None included, against an oracle written here from that sentence plus pinned
  literal cases; review_label_tier: fable for risk high, opus for everything else.
- load_routing(main): defaults with no environment and no settings file; the environment when
  not blank, else <main>/.claude/settings.json's env; settings.local.json never read; values
  stripped and matched case-insensitively; an invalid value ignored with exactly one problem
  line naming the key, the value and the allowed values.
- code_route / review_route: the dicts of the ticket's examples, plus tables over tiers, risks
  and overrides (effort is xhigh on a fable review only; the code route never carries one
  unless HARNESS_CODE_EFFORT is set).
- Module: the constants, and only the standard library and harness_lib imported.

Run: python3 -m unittest plugins/harness/tests/test_harness_lib_routing.py -v
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
import itertools
import json
import os
import shutil
import sys
import sysconfig
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True

TESTS = Path(__file__).resolve().parent
PLUGIN = TESTS.parent
LIB = PLUGIN / "lib"
ROUTING_PY = LIB / "harness_lib" / "routing.py"

KEYS = (
    "HARNESS_CODE_TIER",
    "HARNESS_CODE_EFFORT",
    "HARNESS_REVIEW_TIER",
    "HARNESS_ULTRAREVIEW",
    "HARNESS_WAVE_REVIEW",
)
TIERS = ("sonnet", "opus", "fable")
EFFORTS = ("low", "medium", "high", "xhigh", "max")

DEFAULTS = {
    "code_tier": None,
    "code_effort": None,
    "review_tier": None,
    "ultrareview": True,
    "wave_tier": None,
    "wave_effort": None,
    "problems": [],
}
OKINAWA_ENV = {
    "HARNESS_CODE_TIER": "opus",
    "HARNESS_CODE_EFFORT": "xhigh",
    "HARNESS_REVIEW_TIER": "opus",
    "HARNESS_ULTRAREVIEW": "off",
    "HARNESS_WAVE_REVIEW": "fable",
}
OKINAWA = {
    "code_tier": "opus",
    "code_effort": "xhigh",
    "review_tier": "opus",
    "ultrareview": False,
    "wave_tier": "fable",
    "wave_effort": "xhigh",
    "problems": [],
}


def load_routing_module():
    """harness_lib.routing imported from the checkout's lib/, as a tool's prologue would."""
    lib = str(LIB)
    if lib not in sys.path:
        sys.path.insert(0, lib)
    return importlib.import_module("harness_lib.routing")


def expected_label_tier(kind, size, risk):
    """design.md: `size:S` and `risk:low` and type not `bug` gives `sonnet`; `size:L` or
    `risk:high` gives `fable`; everything else gives `opus`."""
    if size == "L" or risk == "high":
        return "fable"
    if size == "S" and risk == "low" and kind != "bug":
        return "sonnet"
    return "opus"


class RoutingCase(unittest.TestCase):
    """A temporary main checkout, and an environment with no HARNESS_* routing keys."""

    def setUp(self):
        self.main = Path(tempfile.mkdtemp(prefix="harness-routing-main-")).resolve()
        self.addCleanup(shutil.rmtree, str(self.main), True)
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        for key in KEYS:
            os.environ.pop(key, None)
        self.routing = load_routing_module()

    def write_settings(self, env, name="settings.json"):
        claude = self.main / ".claude"
        claude.mkdir(exist_ok=True)
        (claude / name).write_text(json.dumps({"env": env}), encoding="utf-8")

    def load(self):
        return self.routing.load_routing(str(self.main))


class LabelTier(RoutingCase):
    def test_rubric_for_every_combination(self):
        # any name but "bug" is an ordinary kind, so kinds outside the board's labels count too
        kinds = ("task", "bug", "chore", "refactor", "feature", "docs", "spike", "", "unknown", "epic")
        sizes = ("S", "M", "L", None)
        risks = ("low", "medium", "high", None)
        seen = set()
        for kind, size, risk in itertools.product(kinds, sizes, risks):
            with self.subTest(kind=kind, size=size, risk=risk):
                got = self.routing.label_tier(kind, size, risk)
                self.assertEqual(got, expected_label_tier(kind, size, risk))
                seen.add(got)
        self.assertEqual(seen, set(TIERS))  # the oracle exercises all three tiers

    def test_rubric_pinned_cases(self):
        cases = [
            ("task", "S", "low", "sonnet"),
            ("chore", "S", "low", "sonnet"),
            ("refactor", "S", "low", "sonnet"),
            ("bug", "S", "low", "opus"),  # a bug is never sonnet
            ("task", "S", "high", "fable"),  # risk:high beats size:S
            ("task", "L", "low", "fable"),  # size:L beats risk:low
            ("bug", "L", "high", "fable"),
            ("task", "M", "low", "opus"),
            ("task", "M", "high", "fable"),
            ("task", "S", None, "opus"),  # an unlabelled risk is not risk:low
            ("task", None, "low", "opus"),  # an unlabelled size is not size:S
            ("task", None, None, "opus"),
            ("bug", None, "high", "fable"),
            ("task", "L", None, "fable"),
            # any kind but "bug" is sonnet at S/low, whatever its name
            ("docs", "S", "low", "sonnet"),
            ("spike", "S", "low", "sonnet"),
            ("", "S", "low", "sonnet"),
            ("unknown", "S", "low", "sonnet"),
            ("docs", "S", "high", "fable"),
            ("spike", "M", "low", "opus"),
            ("unknown", "L", "low", "fable"),
        ]
        for kind, size, risk, want in cases:
            with self.subTest(kind=kind, size=size, risk=risk):
                self.assertEqual(self.routing.label_tier(kind, size, risk), want)

    def test_review_label_tier_is_fable_only_for_risk_high(self):
        self.assertEqual(self.routing.review_label_tier("high"), "fable")
        for risk in ("low", "medium", None, ""):
            with self.subTest(risk=risk):
                self.assertEqual(self.routing.review_label_tier(risk), "opus")


class LoadRouting(RoutingCase):
    def test_defaults_without_settings(self):
        self.assertEqual(self.load(), DEFAULTS)

    def test_defaults_with_settings_that_name_no_routing_key(self):
        self.write_settings({"HARNESS_TEST_PATTERNS": "tests/**", "OTHER": "x"})
        self.assertEqual(self.load(), DEFAULTS)

    def test_main_may_be_a_path(self):
        self.write_settings(OKINAWA_ENV)
        self.assertEqual(self.routing.load_routing(self.main), OKINAWA)

    def test_okinawa_settings_from_the_settings_file(self):
        self.write_settings(OKINAWA_ENV)
        self.assertEqual(self.load(), OKINAWA)

    def test_each_key_alone_from_the_file(self):
        cases = [
            ("HARNESS_CODE_TIER", "sonnet", {"code_tier": "sonnet"}),
            ("HARNESS_CODE_TIER", "fable", {"code_tier": "fable"}),
            ("HARNESS_CODE_EFFORT", "low", {"code_effort": "low"}),
            ("HARNESS_CODE_EFFORT", "max", {"code_effort": "max"}),
            ("HARNESS_REVIEW_TIER", "sonnet", {"review_tier": "sonnet"}),
            ("HARNESS_REVIEW_TIER", "fable", {"review_tier": "fable"}),
            ("HARNESS_ULTRAREVIEW", "on", {"ultrareview": True}),
            ("HARNESS_ULTRAREVIEW", "off", {"ultrareview": False}),
            ("HARNESS_WAVE_REVIEW", "sonnet", {"wave_tier": "sonnet", "wave_effort": "xhigh"}),
            ("HARNESS_WAVE_REVIEW", "opus", {"wave_tier": "opus", "wave_effort": "xhigh"}),
        ]
        for key, value, changes in cases:
            with self.subTest(key=key, value=value):
                self.write_settings({key: value})
                want = dict(DEFAULTS)
                want.update(changes)
                self.assertEqual(self.load(), want)

    def test_every_tier_and_effort_is_accepted(self):
        for tier in TIERS:
            for key, field in (("HARNESS_CODE_TIER", "code_tier"), ("HARNESS_REVIEW_TIER", "review_tier"),
                               ("HARNESS_WAVE_REVIEW", "wave_tier")):
                with self.subTest(key=key, value=tier):
                    self.write_settings({key: tier})
                    got = self.load()
                    self.assertEqual(got[field], tier)
                    self.assertEqual(got["problems"], [])
        for effort in EFFORTS:
            with self.subTest(effort=effort):
                self.write_settings({"HARNESS_CODE_EFFORT": effort})
                got = self.load()
                self.assertEqual(got["code_effort"], effort)
                self.assertEqual(got["problems"], [])

    def test_ultrareview_truthy_and_falsy_words(self):
        for value in ("on", "1", "true", "yes"):
            with self.subTest(value=value):
                self.write_settings({"HARNESS_ULTRAREVIEW": value})
                got = self.load()
                self.assertIs(got["ultrareview"], True)
                self.assertEqual(got["problems"], [])
        for value in ("off", "0", "false", "no"):
            with self.subTest(value=value):
                self.write_settings({"HARNESS_ULTRAREVIEW": value})
                got = self.load()
                self.assertIs(got["ultrareview"], False)
                self.assertEqual(got["problems"], [])

    def test_wave_review_off_words_mean_unset(self):
        for value in ("off", "0", "false", "no"):
            with self.subTest(value=value):
                self.write_settings({"HARNESS_WAVE_REVIEW": value})
                got = self.load()
                self.assertIsNone(got["wave_tier"])
                self.assertIsNone(got["wave_effort"])
                self.assertEqual(got["problems"], [])

    def test_json_numbers_in_the_file_read_as_their_text(self):
        self.write_settings({"HARNESS_ULTRAREVIEW": 0, "HARNESS_WAVE_REVIEW": 0})
        got = self.load()
        self.assertIs(got["ultrareview"], False)
        self.assertIsNone(got["wave_tier"])
        self.assertEqual(got["problems"], [])
        self.write_settings({"HARNESS_ULTRAREVIEW": 1})
        self.assertIs(self.load()["ultrareview"], True)

    def test_environment_wins_when_not_blank(self):
        self.write_settings(OKINAWA_ENV)
        os.environ["HARNESS_CODE_TIER"] = "sonnet"
        got = self.load()
        self.assertEqual(got["code_tier"], "sonnet")
        # the other keys still come from the file
        self.assertEqual(got["code_effort"], "xhigh")
        self.assertEqual(got["review_tier"], "opus")
        self.assertIs(got["ultrareview"], False)
        self.assertEqual(got["wave_tier"], "fable")

        # a blank environment value falls through to the file
        for blank in ("", "   ", "\t"):
            with self.subTest(blank=repr(blank)):
                os.environ["HARNESS_CODE_TIER"] = blank
                self.assertEqual(self.load()["code_tier"], "opus")

    def test_environment_alone_without_a_settings_file(self):
        os.environ.update(
            HARNESS_CODE_TIER="fable",
            HARNESS_CODE_EFFORT="low",
            HARNESS_REVIEW_TIER="sonnet",
            HARNESS_ULTRAREVIEW="no",
            HARNESS_WAVE_REVIEW="opus",
        )
        self.assertEqual(
            self.load(),
            {
                "code_tier": "fable",
                "code_effort": "low",
                "review_tier": "sonnet",
                "ultrareview": False,
                "wave_tier": "opus",
                "wave_effort": "xhigh",
                "problems": [],
            },
        )

    def test_environment_overrides_every_key_over_the_file(self):
        self.write_settings(OKINAWA_ENV)
        os.environ.update(
            HARNESS_CODE_TIER="fable",
            HARNESS_CODE_EFFORT="low",
            HARNESS_REVIEW_TIER="sonnet",
            HARNESS_ULTRAREVIEW="on",
            HARNESS_WAVE_REVIEW="off",
        )
        got = self.load()
        self.assertEqual(got["code_tier"], "fable")
        self.assertEqual(got["code_effort"], "low")
        self.assertEqual(got["review_tier"], "sonnet")
        self.assertIs(got["ultrareview"], True)
        self.assertIsNone(got["wave_tier"])
        self.assertIsNone(got["wave_effort"])

    def test_settings_local_json_is_never_read(self):
        # alone: nothing is read
        self.write_settings(OKINAWA_ENV, name="settings.local.json")
        self.assertEqual(self.load(), DEFAULTS)
        # beside settings.json: it does not override, even key by key
        self.write_settings({"HARNESS_CODE_TIER": "opus"})
        self.write_settings(
            {"HARNESS_CODE_TIER": "fable", "HARNESS_REVIEW_TIER": "sonnet", "HARNESS_ULTRAREVIEW": "off"},
            name="settings.local.json",
        )
        want = dict(DEFAULTS)
        want["code_tier"] = "opus"
        self.assertEqual(self.load(), want)
        # an invalid value there adds no problem
        self.write_settings({"HARNESS_CODE_TIER": "gpt", "HARNESS_WAVE_REVIEW": "yes"}, name="settings.local.json")
        self.assertEqual(self.load()["problems"], [])

    def test_a_broken_settings_file_gives_the_defaults(self):
        (self.main / ".claude").mkdir()
        (self.main / ".claude" / "settings.json").write_text("{not json", encoding="utf-8")
        self.assertEqual(self.load(), DEFAULTS)

    def test_case_and_whitespace_are_ignored(self):
        # from the file
        self.write_settings(
            {
                "HARNESS_CODE_TIER": " Opus ",
                "HARNESS_CODE_EFFORT": "XHIGH",
                "HARNESS_REVIEW_TIER": "\tFABLE\n",
                "HARNESS_ULTRAREVIEW": "OFF",
                "HARNESS_WAVE_REVIEW": " Sonnet ",
            }
        )
        self.assertEqual(
            self.load(),
            {
                "code_tier": "opus",
                "code_effort": "xhigh",
                "review_tier": "fable",
                "ultrareview": False,
                "wave_tier": "sonnet",
                "wave_effort": "xhigh",
                "problems": [],
            },
        )
        # and from the environment
        os.environ.update(
            HARNESS_CODE_TIER=" SONNET",
            HARNESS_CODE_EFFORT=" Max ",
            HARNESS_REVIEW_TIER="Opus",
            HARNESS_ULTRAREVIEW=" YES ",
            HARNESS_WAVE_REVIEW="OFF",
        )
        self.assertEqual(
            self.load(),
            {
                "code_tier": "sonnet",
                "code_effort": "max",
                "review_tier": "opus",
                "ultrareview": True,
                "wave_tier": None,
                "wave_effort": None,
                "problems": [],
            },
        )

    def test_invalid_values_fail_toward_todays_behaviour(self):
        cases = [
            ("HARNESS_CODE_TIER", "gpt", "code_tier", None),
            ("HARNESS_CODE_EFFORT", "extreme", "code_effort", None),
            ("HARNESS_REVIEW_TIER", "haiku", "review_tier", None),
            ("HARNESS_ULTRAREVIEW", "maybe", "ultrareview", True),
            ("HARNESS_WAVE_REVIEW", "yes", "wave_tier", None),
        ]
        for key, value, field, today in cases:
            with self.subTest(key=key, value=value, source="file"):
                self.write_settings({key: value})
                got = self.load()
                self.assertEqual(got[field], today)
                self.assertEqual(got["wave_effort"], None)
                self.assertEqual(len(got["problems"]), 1, got["problems"])
                line = got["problems"][0]
                self.assertIsInstance(line, str)
                self.assertIn(key, line)
                self.assertIn(repr(value), line)
                self.assertNotIn("\n", line)
                # nothing else changed
                rest = {k: v for k, v in got.items() if k not in (field, "problems")}
                rest_default = {k: v for k, v in DEFAULTS.items() if k not in (field, "problems")}
                self.assertEqual(rest, rest_default)
            with self.subTest(key=key, value=value, source="environment"):
                self.write_settings({})
                os.environ[key] = value
                try:
                    got = self.load()
                finally:
                    del os.environ[key]
                self.assertEqual(got[field], today)
                self.assertEqual(len(got["problems"]), 1, got["problems"])
                self.assertIn(key, got["problems"][0])
                self.assertIn(repr(value), got["problems"][0])

    def test_problem_line_wording_names_the_allowed_values(self):
        self.write_settings({"HARNESS_CODE_TIER": "gpt"})
        self.assertEqual(
            self.load()["problems"],
            ["HARNESS_CODE_TIER='gpt' is not one of sonnet, opus, fable; ignored"],
        )
        self.write_settings({"HARNESS_CODE_EFFORT": "extreme"})
        self.assertEqual(
            self.load()["problems"],
            ["HARNESS_CODE_EFFORT='extreme' is not one of low, medium, high, xhigh, max; ignored"],
        )
        self.write_settings({"HARNESS_REVIEW_TIER": "haiku"})
        self.assertEqual(
            self.load()["problems"],
            ["HARNESS_REVIEW_TIER='haiku' is not one of sonnet, opus, fable; ignored"],
        )
        self.write_settings({"HARNESS_ULTRAREVIEW": "maybe"})
        self.assertEqual(
            self.load()["problems"],
            ["HARNESS_ULTRAREVIEW='maybe' is not one of on, 1, true, yes, off, 0, false, no; ignored"],
        )
        self.write_settings({"HARNESS_WAVE_REVIEW": "yes"})
        self.assertEqual(
            self.load()["problems"],
            ["HARNESS_WAVE_REVIEW='yes' is not one of sonnet, opus, fable, off, 0, false, no; ignored"],
        )

    def test_the_problem_shows_the_value_stripped(self):
        self.write_settings({"HARNESS_CODE_TIER": "  GPT-5  "})
        (line,) = self.load()["problems"]
        self.assertIn("HARNESS_CODE_TIER", line)
        # stripped; whether the case is kept is the implementation's choice
        self.assertIn("'gpt-5'", line.lower())
        self.assertNotIn("  ", line)

    def test_several_invalid_values_give_one_problem_each_and_valid_keys_still_apply(self):
        self.write_settings(
            {
                "HARNESS_CODE_TIER": "gpt",
                "HARNESS_CODE_EFFORT": "extreme",
                "HARNESS_REVIEW_TIER": "opus",
                "HARNESS_ULTRAREVIEW": "maybe",
                "HARNESS_WAVE_REVIEW": "yes",
            }
        )
        got = self.load()
        self.assertEqual(
            {k: v for k, v in got.items() if k != "problems"},
            {
                "code_tier": None,
                "code_effort": None,
                "review_tier": "opus",
                "ultrareview": True,
                "wave_tier": None,
                "wave_effort": None,
            },
        )
        self.assertEqual(len(got["problems"]), 4)
        for key in ("HARNESS_CODE_TIER", "HARNESS_CODE_EFFORT", "HARNESS_ULTRAREVIEW", "HARNESS_WAVE_REVIEW"):
            self.assertEqual(sum(key in line for line in got["problems"]), 1, key)
        self.assertFalse(any("HARNESS_REVIEW_TIER" in line for line in got["problems"]))

    # Malformed settings files: settings_env skips a file it cannot read, parse or use, and
    # setting() takes only a string or number (never a bool, null or list) from the file.
    def write_raw(self, text, name="settings.json"):
        (self.main / ".claude").mkdir(exist_ok=True)
        (self.main / ".claude" / name).write_text(text, encoding="utf-8")

    def test_malformed_settings_files_give_the_defaults_and_no_problems(self):
        good = dict(OKINAWA_ENV)
        cases = {
            "env missing": json.dumps({"other": good}),
            "env a list": json.dumps({"env": [good]}),
            "env a string": json.dumps({"env": "HARNESS_CODE_TIER=opus"}),
            "env null": json.dumps({"env": None}),
            "top level a list": json.dumps([{"env": good}]),
            "top level a string": json.dumps("env"),
            "top level null": "null",
            "empty file": "",
            "truncated json": '{"env": {"HARNESS_CODE_TIER": "opus"',
            "keys nested one level down": json.dumps({"env": {"env": good}}),
            "keys at the top level": json.dumps(good),
        }
        for label, text in cases.items():
            with self.subTest(label):
                self.write_raw(text)
                self.assertEqual(self.load(), DEFAULTS)

    def test_settings_json_that_is_a_directory_gives_the_defaults(self):
        (self.main / ".claude" / "settings.json").mkdir(parents=True)
        self.assertEqual(self.load(), DEFAULTS)

    def test_a_dot_claude_that_is_a_file_gives_the_defaults(self):
        (self.main / ".claude").write_text("x", encoding="utf-8")
        self.assertEqual(self.load(), DEFAULTS)

    def test_null_list_object_and_bool_values_in_the_file_count_as_unset(self):
        for value in (None, ["opus"], {"tier": "opus"}, True, False):
            for key in KEYS:
                with self.subTest(key=key, value=value):
                    self.write_settings({key: value})
                    self.assertEqual(self.load(), DEFAULTS)

    def test_one_bad_key_does_not_hide_the_good_ones(self):
        self.write_settings({"HARNESS_CODE_TIER": None, "HARNESS_CODE_EFFORT": ["low"], "HARNESS_REVIEW_TIER": "fable"})
        self.assertEqual(self.load(), dict(DEFAULTS, review_tier="fable"))

    def test_a_broken_local_file_beside_a_good_settings_json_is_moot(self):
        self.write_settings({"HARNESS_CODE_TIER": "opus"})
        self.write_raw("{broken", name="settings.local.json")
        self.assertEqual(self.load(), dict(DEFAULTS, code_tier="opus"))

    def test_the_environment_is_read_at_call_time(self):
        self.write_settings({"HARNESS_CODE_TIER": "opus"})
        self.assertEqual(self.load()["code_tier"], "opus")
        os.environ["HARNESS_CODE_TIER"] = "sonnet"
        self.assertEqual(self.load()["code_tier"], "sonnet")
        os.environ["HARNESS_CODE_TIER"] = "fable"
        self.assertEqual(self.load()["code_tier"], "fable")
        os.environ["HARNESS_CODE_TIER"] = "gpt"  # invalid: ignored, not the earlier or the file value
        got = self.load()
        self.assertIsNone(got["code_tier"])
        self.assertEqual(len(got["problems"]), 1)
        del os.environ["HARNESS_CODE_TIER"]
        got = self.load()
        self.assertEqual(got["code_tier"], "opus")
        self.assertEqual(got["problems"], [])

    def test_the_settings_file_is_read_at_call_time(self):
        self.write_settings({"HARNESS_REVIEW_TIER": "opus"})
        self.assertEqual(self.load()["review_tier"], "opus")
        self.write_settings({"HARNESS_REVIEW_TIER": "fable", "HARNESS_ULTRAREVIEW": "off"})
        got = self.load()
        self.assertEqual(got["review_tier"], "fable")
        self.assertIs(got["ultrareview"], False)
        (self.main / ".claude" / "settings.json").unlink()
        self.assertEqual(self.load(), DEFAULTS)

    def test_main_as_a_relative_path_or_with_a_trailing_slash(self):
        self.write_settings(OKINAWA_ENV)
        self.assertEqual(self.routing.load_routing(str(self.main) + "/"), OKINAWA)
        self.assertEqual(self.routing.load_routing(str(self.main) + "//"), OKINAWA)
        cwd = os.getcwd()
        self.addCleanup(os.chdir, cwd)
        other = Path(tempfile.mkdtemp(prefix="harness-routing-other-", dir=str(self.main.parent))).resolve()
        self.addCleanup(shutil.rmtree, str(other), True)
        os.chdir(str(self.main.parent))
        self.assertEqual(self.routing.load_routing(self.main.name), OKINAWA)
        self.assertEqual(self.routing.load_routing(self.main.name + "/"), OKINAWA)
        self.assertEqual(self.routing.load_routing("./" + self.main.name), OKINAWA)
        # a relative path names its own directory, not the one an earlier call used
        self.assertEqual(self.routing.load_routing(other.name), DEFAULTS)
        os.chdir(str(self.main))
        self.assertEqual(self.routing.load_routing("."), OKINAWA)
        self.assertEqual(self.routing.load_routing("./"), OKINAWA)

    def test_a_missing_main_gives_the_defaults(self):
        self.assertEqual(self.routing.load_routing(str(self.main / "nope")), DEFAULTS)

    def test_load_routing_does_not_mutate_the_environment_or_settings(self):
        self.write_settings(OKINAWA_ENV)
        path = self.main / ".claude" / "settings.json"
        before_file = path.read_bytes()
        before_env = dict(os.environ)
        self.load()
        self.assertEqual(path.read_bytes(), before_file)
        self.assertEqual(dict(os.environ), before_env)


class Routes(RoutingCase):
    def test_default_routes_match_the_ticket_examples(self):
        d = dict(DEFAULTS)
        self.assertEqual(
            self.routing.code_route("task", "L", "high", d),
            {"tier": "fable", "effort": None, "from": "labels", "labels_tier": "fable"},
        )
        self.assertEqual(
            self.routing.review_route("high", d),
            {"tier": "fable", "effort": "xhigh", "from": "labels", "labels_tier": "fable"},
        )
        self.assertEqual(
            self.routing.review_route("low", d),
            {"tier": "opus", "effort": None, "from": "labels", "labels_tier": "opus"},
        )

    def test_okinawa_routes_match_the_ticket_examples(self):
        self.write_settings(OKINAWA_ENV)
        r = self.load()
        self.assertEqual(
            self.routing.code_route("task", "L", "high", r),
            {"tier": "opus", "effort": "xhigh", "from": "HARNESS_CODE_TIER", "labels_tier": "fable"},
        )
        self.assertEqual(
            self.routing.review_route("high", r),
            {"tier": "opus", "effort": None, "from": "HARNESS_REVIEW_TIER", "labels_tier": "fable"},
        )

    def test_code_route_with_and_without_the_override(self):
        # without: the rubric, over a spread of labels, effort None, from labels
        table = [
            ("task", "S", "low", "sonnet"),
            ("bug", "S", "low", "opus"),
            ("task", "M", "low", "opus"),
            ("task", None, None, "opus"),
            ("task", "L", "low", "fable"),
            ("chore", "S", "high", "fable"),
        ]
        for kind, size, risk, want in table:
            with self.subTest(kind=kind, size=size, risk=risk, override=False):
                self.assertEqual(
                    self.routing.code_route(kind, size, risk, dict(DEFAULTS)),
                    {"tier": want, "effort": None, "from": "labels", "labels_tier": want},
                )
        # with a tier override: it replaces the tier whatever the labels say
        for override in TIERS:
            for kind, size, risk, labels in table:
                with self.subTest(kind=kind, size=size, risk=risk, override=override):
                    routing = dict(DEFAULTS, code_tier=override)
                    self.assertEqual(
                        self.routing.code_route(kind, size, risk, routing),
                        {"tier": override, "effort": None, "from": "HARNESS_CODE_TIER", "labels_tier": labels},
                    )

    def test_code_route_effort_is_independent_of_the_tier_override(self):
        for effort in EFFORTS:
            with self.subTest(effort=effort, tier=None):
                routing = dict(DEFAULTS, code_effort=effort)
                self.assertEqual(
                    self.routing.code_route("task", "S", "low", routing),
                    {"tier": "sonnet", "effort": effort, "from": "labels", "labels_tier": "sonnet"},
                )
            with self.subTest(effort=effort, tier="fable"):
                routing = dict(DEFAULTS, code_tier="fable", code_effort=effort)
                self.assertEqual(
                    self.routing.code_route("task", "S", "low", routing),
                    {"tier": "fable", "effort": effort, "from": "HARNESS_CODE_TIER", "labels_tier": "sonnet"},
                )

    def test_code_route_ignores_the_review_and_wave_settings(self):
        routing = dict(DEFAULTS, review_tier="sonnet", ultrareview=False, wave_tier="fable", wave_effort="xhigh")
        self.assertEqual(
            self.routing.code_route("task", "M", "low", routing),
            {"tier": "opus", "effort": None, "from": "labels", "labels_tier": "opus"},
        )

    def test_review_route_effort_is_xhigh_only_on_fable(self):
        # without an override: the rubric decides, and fable carries xhigh
        for risk, tier, effort in (("high", "fable", "xhigh"), ("low", "opus", None), (None, "opus", None)):
            with self.subTest(risk=risk, override=None):
                self.assertEqual(
                    self.routing.review_route(risk, dict(DEFAULTS)),
                    {"tier": tier, "effort": effort, "from": "labels", "labels_tier": tier},
                )
        # with an override: effort follows the resulting tier, from names the setting
        for risk, labels in (("high", "fable"), ("low", "opus")):
            for override in TIERS:
                with self.subTest(risk=risk, override=override):
                    routing = dict(DEFAULTS, review_tier=override)
                    self.assertEqual(
                        self.routing.review_route(risk, routing),
                        {
                            "tier": override,
                            "effort": "xhigh" if override == "fable" else None,
                            "from": "HARNESS_REVIEW_TIER",
                            "labels_tier": labels,
                        },
                    )

    def test_review_route_reads_review_settings_only(self):
        routing = dict(DEFAULTS, code_tier="sonnet", code_effort="max", ultrareview=False, wave_tier="opus",
                       wave_effort="xhigh")
        self.assertEqual(
            self.routing.review_route("high", routing),
            {"tier": "fable", "effort": "xhigh", "from": "labels", "labels_tier": "fable"},
        )
        self.assertEqual(
            self.routing.review_route("low", routing),
            {"tier": "opus", "effort": None, "from": "labels", "labels_tier": "opus"},
        )

    def test_routes_from_load_routing_end_to_end(self):
        self.write_settings({"HARNESS_CODE_EFFORT": "max", "HARNESS_REVIEW_TIER": "fable"})
        r = self.load()
        self.assertEqual(
            self.routing.code_route("bug", "S", "low", r),
            {"tier": "opus", "effort": "max", "from": "labels", "labels_tier": "opus"},
        )
        self.assertEqual(
            self.routing.review_route("low", r),
            {"tier": "fable", "effort": "xhigh", "from": "HARNESS_REVIEW_TIER", "labels_tier": "opus"},
        )

    def test_routes_do_not_mutate_the_routing(self):
        routing = dict(DEFAULTS, code_tier="opus", review_tier="fable")
        before = dict(routing)
        self.routing.code_route("task", "L", "high", routing)
        self.routing.review_route("high", routing)
        self.assertEqual(routing, before)


class Module(RoutingCase):
    def test_constants(self):
        self.assertEqual(self.routing.TIERS, TIERS)
        self.assertEqual(self.routing.EFFORTS, EFFORTS)
        self.assertEqual(self.routing.KEYS, KEYS)
        self.assertEqual(self.routing.WAVE_EFFORT, "xhigh")

    def test_imports_only_the_standard_library_and_harness_lib(self):
        self.assertTrue(ROUTING_PY.is_file(), "plugins/harness/lib/harness_lib/routing.py does not exist")
        tree = ast.parse(ROUTING_PY.read_text(encoding="utf-8"))
        stdlib_dir = Path(sysconfig.get_paths()["stdlib"]).resolve()
        found = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                found.extend((alias.name.split(".")[0], 0) for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                found.append(((node.module or "").split(".")[0], node.level))
        self.assertIn(("__future__", 0), found)  # from __future__ import annotations
        for top, level in found:
            if level:  # a relative import inside harness_lib
                continue
            with self.subTest(module=top):
                if top in ("__future__", "harness_lib") or top in sys.builtin_module_names:
                    continue
                spec = importlib.util.find_spec(top)
                origin = getattr(spec, "origin", None)
                self.assertIsNotNone(origin, f"{top} cannot be located")
                self.assertTrue(
                    Path(origin).resolve().is_relative_to(stdlib_dir)
                    if hasattr(Path, "is_relative_to")
                    else str(Path(origin).resolve()).startswith(str(stdlib_dir)),
                    f"{top} is not in the standard library ({origin})",
                )
                self.assertNotIn("site-packages", str(origin))

    def test_design_md_lists_the_five_settings(self):
        design = (PLUGIN.parent.parent / "docs" / "design.md").read_text(encoding="utf-8")
        for key in KEYS:
            with self.subTest(key=key):
                self.assertIn(key, design)
        self.assertIn("routing.py", design)

    def test_future_annotations_is_enabled(self):
        text = ROUTING_PY.read_text(encoding="utf-8")
        self.assertIn("from __future__ import annotations", text)


if __name__ == "__main__":
    unittest.main()
