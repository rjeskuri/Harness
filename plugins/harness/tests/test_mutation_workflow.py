"""Tests for #476 and #502: .github/workflows/mutation.yml, the monthly mutation run on Harness's main.

    python3 -m unittest discover -s plugins/harness/tests -p test_mutation_workflow.py

The standard library has no YAML parser, so these tests read the small block-style subset a workflow uses: comments
and blank lines dropped, nesting by indentation, a step list as `- ` items under `steps:`. They pin what the
definitions of done say and nothing about wording: the triggers (a monthly cron and a manual dispatch with `full`
and `file` inputs, never a pull request or a push), read-only permissions and no secret, hosted ubuntu jobs under
the six-hour limit, a step that runs `scripts/mutate.py run`, and a step that uploads the report as an artifact.

#502: one static matrix (`strategy.matrix`) whose entries name exactly the scoped files by their path under
`plugins/harness/` (with or without that prefix), each once, no glob, no shard and no month rotation; the checkout
has the whole history (`fetch-depth: 0`); the runner passes `--changed` (the base is main's commit of 35 days ago)
and its handling depends on the `full` input; the `file` input is read by the matrix job; the job summary
(`$GITHUB_STEP_SUMMARY`) is written at or after the run. Which lines `--changed` keeps is test_mutate_script.py's.
"""
from __future__ import annotations

import collections
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
WORKFLOW = REPO / ".github" / "workflows" / "mutation.yml"
PLUGIN = REPO / "plugins" / "harness"
SCOPE = {"hooks/branch_guard.py", "hooks/backup_gate.py", "hooks/tests_first.py", "hooks/readonly_allow.py",
         "hooks/readonly_bash.py", "hooks/land_ask.py", "lib/harness_lib/backups.py", "lib/harness_lib/shellwalk.py"}
PLUGIN_PATH = re.compile(r"(?:plugins/harness/)?((?:hooks|lib|bin)/[\w./*?\[\]-]+)")

Lines = list  # list of (indent, text) pairs


def parse_lines(text: str) -> Lines:
    out = []
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        s = re.sub(r"\s+#.*$", "", raw.rstrip())
        out.append((len(s) - len(s.lstrip(" ")), s.strip()))
    return out


def children(lines: Lines, i: int) -> Lines:
    j = i + 1
    while j < len(lines) and lines[j][0] > lines[i][0]:
        j += 1
    return lines[i + 1:j]


def get(lines: Lines, name: str):
    """(inline value, child lines) of the first key `name` at the top level of `lines`, or None."""
    if not lines:
        return None
    level = min(ind for ind, _ in lines)
    pattern = re.compile(r"^[\"']?%s[\"']?\s*:\s*(.*)$" % re.escape(name))
    for i, (ind, text) in enumerate(lines):
        m = pattern.match(text)
        if ind == level and m:
            return m.group(1).strip(), children(lines, i)
    return None


def keys(lines: Lines) -> list:
    if not lines:
        return []
    level = min(ind for ind, _ in lines)
    found = []
    for ind, text in lines:
        m = re.match(r"^[\"']?([\w.-]+)[\"']?\s*:", text)
        if ind == level and m:
            found.append(m.group(1))
    return found


def joined(lines: Lines) -> str:
    return "\n".join(t for _, t in lines)


def split_steps(step_lines: Lines) -> list:
    if not step_lines:
        return []
    dash = min(ind for ind, _ in step_lines)
    steps: list = []
    for ind, text in step_lines:
        if ind == dash and text.startswith("-"):
            steps.append([])
        if steps:
            steps[-1].append((ind, text))
    return steps


class MutationWorkflowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = WORKFLOW.read_text(encoding="utf-8") if WORKFLOW.is_file() else None

    def setUp(self) -> None:
        self.assertIsNotNone(self.text, "%s does not exist" % WORKFLOW)
        self.lines = parse_lines(self.text)

    def triggers(self) -> dict:
        on = get(self.lines, "on")
        self.assertIsNotNone(on, "the workflow has no `on:` key")
        value, kids = on
        if value:  # `on: workflow_dispatch` or `on: [push, workflow_dispatch]`
            return {n: [] for n in re.findall(r"[\w-]+", value)}
        level = min(ind for ind, _ in kids) if kids else 0
        found = {}
        for i, (ind, text) in enumerate(kids):
            m = re.match(r"^[\"']?([\w-]+)[\"']?\s*:", text)
            if ind == level and m:
                found[m.group(1)] = children(kids, i)
        return found

    def jobs(self) -> dict:
        got = get(self.lines, "jobs")
        self.assertIsNotNone(got, "the workflow has no `jobs:` key")
        kids = got[1]
        level = min(ind for ind, _ in kids)
        return {m.group(1): children(kids, i) for i, (ind, t) in enumerate(kids)
                if ind == level for m in [re.match(r"^([\w-]+)\s*:\s*$", t)] if m}

    def all_steps(self) -> list:
        steps = []
        for job in self.jobs().values():
            got = get(job, "steps")
            if got:
                steps += split_steps(got[1])
        return steps

    def test_parses_as_a_workflow_with_a_name_triggers_and_jobs(self) -> None:
        self.assertIsNotNone(get(self.lines, "on"))
        self.assertTrue(self.jobs(), "no job under `jobs:`")
        self.assertNotIn("\t", self.text, "YAML forbids tabs")

    def test_triggers_are_a_monthly_schedule_and_a_manual_dispatch_with_full_and_file_inputs(self) -> None:
        triggers = self.triggers()
        self.assertEqual({"schedule", "workflow_dispatch"}, set(triggers), "only a schedule and a manual dispatch")
        crons = re.findall(r"cron:\s*[\"']?([^\"'\n]+?)[\"']?\s*$", joined(triggers["schedule"]), re.M)
        self.assertTrue(crons, "the schedule has no cron entry")
        for cron in crons:
            fields = cron.split()
            self.assertEqual(5, len(fields), cron)
            minute, hour, dom = fields[:3]
            # monthly: pinned minute, hour and day of the month (a list of numbers), so never more than a run a day
            for field in (minute, hour, dom):
                self.assertRegex(field, r"^\d+(,\d+)*$", "not a monthly cron: %s" % cron)
        inputs = get(triggers["workflow_dispatch"], "inputs")
        self.assertIsNotNone(inputs, "workflow_dispatch has no inputs")
        names = set(keys(inputs[1]))
        self.assertLessEqual({"full", "file"}, names, "workflow_dispatch needs a `full` and a `file` input")
        self.assertNotIn("shard", names, "the shard rotation is gone")

    # ---------------------------------------------------------------------------------------- #502 matrix

    def matrix_job(self) -> tuple:
        """(job name, job lines, matrix lines) of the one job with a strategy.matrix."""
        found = []
        for name, job in self.jobs().items():
            strategy = get(job, "strategy")
            matrix = get(strategy[1], "matrix") if strategy else None
            if matrix:
                found.append((name, job, matrix))
        self.assertEqual(len(found), 1, "exactly one job runs over a matrix")
        name, job, (value, kids) = found[0]
        self.assertEqual("", value, "the matrix must be listed in the workflow, not computed: %s" % value)
        return name, job, kids

    def test_the_matrix_lists_exactly_the_scoped_files_once_each(self) -> None:
        for rel in SCOPE:
            self.assertTrue((PLUGIN / rel).is_file(), "scoped file missing: %s" % rel)
        _, job, matrix = self.matrix_job()
        named = collections.Counter(m.rstrip(".,'\"") for m in PLUGIN_PATH.findall(joined(matrix)))
        self.assertEqual(SCOPE, set(named), "the matrix names exactly the scoped files (no bin/, no glob)")
        self.assertEqual({rel: 1 for rel in SCOPE}, dict(named), "one matrix entry per file")
        strategy = get(job, "strategy")[1]
        fail_fast = get(strategy, "fail-fast")
        self.assertIsNotNone(fail_fast, "a survivor in one file must not cancel the other files' runs")
        self.assertEqual("false", fail_fast[0].strip("\"'"))

    def test_no_month_rotation_or_shards(self) -> None:
        text = joined(self.lines)
        self.assertEqual([], re.findall(r".*%m\b.*", text), "no month number is read")
        self.assertEqual([], re.findall(r".*\bshift\s+\$\(\(.*", text), "no rotation over a list")
        self.assertEqual([], re.findall(r"(?i).*\bshard.*", text)[:3], "no shards")

    def test_checkout_has_the_whole_history(self) -> None:
        _, job, _ = self.matrix_job()
        steps = split_steps(get(job, "steps")[1])
        checkout = [s for s in steps if re.search(r"uses:\s*actions/checkout@", joined(s))]
        self.assertTrue(checkout, "the matrix job checks nothing out")
        self.assertRegex(joined(checkout[0]), r"(?m)^fetch-depth:\s*[\"']?0[\"']?$", "fetch-depth: 0")

    def test_schedule_mutates_changed_lines_and_full_decides_the_exit(self) -> None:
        _, job, _ = self.matrix_job()
        text = joined(job)
        steps = split_steps(get(job, "steps")[1])
        runner = [s for s in steps if "scripts/mutate.py run" in joined(s)]
        self.assertTrue(runner, "the matrix job runs no scripts/mutate.py run")
        run_text = joined(runner[0])
        self.assertIn("--changed", run_text, "the schedule mutates only changed lines")
        self.assertRegex(joined(self.lines), r"(?i)\b35[\s.]*days?\b|\bdays?\W{0,3}35\b", "the base is 35 days back")
        self.assertRegex(run_text, r"(?i)full", "the run and its exit handling depend on the `full` input")
        self.assertRegex(text, r"inputs\.full", "the `full` input is read by the matrix job")
        self.assertRegex(text, r"inputs\.file", "the `file` input picks the matrix entry")
        self.assertIn("matrix.", run_text, "the runner mutates its matrix entry's file")

    def test_survivors_go_to_the_job_summary(self) -> None:
        _, job, _ = self.matrix_job()
        steps = split_steps(get(job, "steps")[1])
        runner = [i for i, s in enumerate(steps) if "scripts/mutate.py run" in joined(s)]
        self.assertTrue(runner)
        summary = [i for i, s in enumerate(steps) if "GITHUB_STEP_SUMMARY" in joined(s)]
        self.assertTrue(summary, "no step writes $GITHUB_STEP_SUMMARY")
        self.assertGreaterEqual(max(summary), runner[0], "the summary is written at or after the run")

    def test_each_matrix_entry_uploads_its_own_artifact(self) -> None:
        _, job, _ = self.matrix_job()
        steps = split_steps(get(job, "steps")[1])
        uploads = [s for s in steps if re.search(r"uses:\s*actions/upload-artifact@", joined(s))]
        self.assertTrue(uploads, "no step uploads an artifact")
        for step in uploads:
            name = re.search(r"^name:\s*(.+)$", joined(step), re.M)
            self.assertTrue(name and "matrix." in name.group(1), "artifact names must differ per matrix entry")

    def test_never_runs_on_a_pull_request_or_a_push(self) -> None:
        text = joined(self.lines)
        triggers = self.triggers()
        for banned in ("pull_request", "pull_request_target", "push", "workflow_run", "issue_comment"):
            self.assertNotIn(banned, triggers)
        self.assertIsNone(re.search(r"^\s*-?\s*(pull_request(_target)?|push)\s*:", text, re.M))

    def test_permissions_are_read_only_and_there_is_no_secret(self) -> None:
        top = get(self.lines, "permissions")
        self.assertIsNotNone(top, "no top-level permissions")
        self.assertEqual("", top[0], "permissions must be a block, not write-all/read-all")
        # #502's kill rule may read earlier runs, so `actions: read` is allowed beside `contents: read`
        granted = [tuple(re.split(r"\s*:\s*", t)) for _, t in top[1]]
        self.assertIn(("contents", "read"), granted)
        self.assertLessEqual(set(granted), {("contents", "read"), ("actions", "read")}, "read-only scopes only")
        for i, (_, text) in enumerate(self.lines):  # a job may not widen it either
            if re.match(r"^permissions\s*:", text):
                value = text.split(":", 1)[1].strip()
                scope = [t for _, t in children(self.lines, i)]
                self.assertFalse(value and value != "{}", "inline permissions: %s" % text)
                self.assertTrue(all(re.match(r"^(contents|actions)\s*:\s*read$", t) for t in scope), scope)
        self.assertNotRegex(joined(self.lines), r"secrets\s*[.\[]")
        self.assertNotRegex(joined(self.lines), r"^secrets\s*:", "no `secrets:` key either")

    def test_one_hosted_ubuntu_job_with_a_timeout_under_the_six_hour_limit(self) -> None:
        for name, job in self.jobs().items():
            runs_on = get(job, "runs-on")
            self.assertIsNotNone(runs_on, name)
            self.assertEqual("ubuntu-latest", runs_on[0].strip("\"'"), name)
            timeout = get(job, "timeout-minutes")
            self.assertIsNotNone(timeout, "%s has no timeout-minutes" % name)
            self.assertRegex(timeout[0], r"^\d+$")
            self.assertTrue(0 < int(timeout[0]) < 360, "timeout-minutes %s is not under 360" % timeout[0])

    def test_a_step_runs_the_mutation_runner_and_a_step_uploads_the_report(self) -> None:
        steps = self.all_steps()
        runner = [s for s in steps if "scripts/mutate.py run" in joined(s)]
        self.assertTrue(runner, "no step runs scripts/mutate.py run")
        self.assertIn("--json", joined(runner[0]))
        self.assertIn("mutation.json", joined(runner[0]))
        uploads = [s for s in steps if re.search(r"uses:\s*actions/upload-artifact@", joined(s))]
        self.assertTrue(uploads, "no step uploads an artifact")
        self.assertIn("mutation.json", joined(uploads[0]), "the artifact must carry mutation.json")
        self.assertLess(steps.index(runner[0]), steps.index(uploads[0]), "upload comes after the run")
        self.assertTrue(any(re.search(r"uses:\s*actions/checkout@", joined(s)) for s in steps), "no checkout")


if __name__ == "__main__":
    unittest.main()
