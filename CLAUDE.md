# Harness

Ryan's Claude Code harness: one plugin (agents, skills, hooks, command-line tools) that every project on this Mac loads in place from this checkout. Correct means the hooks deny exactly what `docs/design.md` says and the tools keep its contracts.

## Commands
- Verify, the whole suite, one process per test module in parallel (CI runs the same): `python3 scripts/run_suite.py`
- One module: `python3 -m unittest discover -s plugins/harness/tests -p test_work.py`
- Canary, is the plugin every project loads still working: `sh scripts/smoke.sh`

## Where things live
- `plugins/harness/tests/`: the suite; `fakegh.py` is the scripted fake `gh` every tool test uses, and fake executables are written once per module.
- `plugins/harness/{agents,skills,hooks,bin,templates}/`: the plugin. `docs/design.md` is the contract; `docs/research/` holds the verified platform facts.
- `user/` and `scripts/install.sh`: the personal CLAUDE.md and settings snippet, and how they are installed.
- `scripts/verify.sh`: the checks behind the verify command; `.github/workflows/ci.yml` runs it on every pull request and by hand, not again on the push to main (a squash merge lands the tree the pull request run verified).
- `.githooks/`: `pre-commit` (format and lint on staged files, no commits on main) and `pre-push` (no pushes to main, no force pushes).

## Conventions
- python3 standard library only, 3.9-compatible (`from __future__ import annotations`); tests are unittest modules that drive the tools as subprocesses, and a suite split across several `test_<x>*.py` modules keeps its fixtures in `<x>_support.py` beside them (not collected). Tests pin behavior, never the wording of a document, skill or agent.
- A change to a hook or tool updates its contract in `docs/design.md` in the same pull request.
