<!--
Maintainer notes (HTML comments are stripped before Claude sees this file, so they cost no context).
- Keep this under ~60 lines. Only write what Claude can't infer from the code.
- Test each line: "Would removing this cause Claude to make a mistake?" If not, delete it.
- When Claude makes the same mistake twice, add one line here (or a path-scoped rule in .claude/rules/).
- Replace every <placeholder>. Delete sections that don't apply.
-->

# <project-name>

<One or two sentences: what this project does, who uses its output, and what "correct" means for it.>

## Commands
- Setup: `uv sync`
- Fast tests: `uv run pytest -q -x --tb=short` (one test: `uv run pytest tests/test_x.py::test_name -q`)
- Lint and format: `uv run ruff check --fix . && uv run ruff format .` (a hook formats each file you edit)
- Types: `uv run pyright src/`
- Run a notebook headless: `uv run jupyter nbconvert --to notebook --execute <path.ipynb> --output-dir .nbout`

## Where things live
- `src/<pkg>/`: all reusable logic. Notebooks import from here.
- `notebooks/`: exploration only, paired with `.py` files via jupytext (see `.claude/rules/notebooks.md`).
- `tests/`: pytest. Small sample data lives in `tests/fixtures/`.
- `docs/agent/`: specs, plans and handoff notes written during Claude sessions.

## Conventions
- Use `logging`, not `print`, in `src/`: scheduled runs keep logs, not stdout.
- Config and credentials come from environment variables loaded in `src/<pkg>/config.py`. Don't open `.env`.
- Ask before adding a dependency, and say what it does that the standard library or existing deps can't.

## Data access
- Query databases only through `uv run python scripts/q.py "<sql>"` (read-only user, capped rows). Table notes: `docs/schema/`.

## Done means
- A behavior change comes with a test, and the fast tests pass. Show the command you ran and its output.
- If something can't be verified here (needs prod data, credentials, a GUI), say so plainly instead of implying it works.

## Gotchas
- Run Python through `uv run python`: the system `python3` doesn't have this project's dependencies.
- <One line per recurring mistake.>

## Compaction
When compacting, keep the list of changed files, the path of the current spec or plan, and the latest test result.
