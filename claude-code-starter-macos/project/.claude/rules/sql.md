---
paths:
  - "**/*.sql"
---
# SQL conventions

- Filter on exact IDs, not name patterns (`LIKE`, regex). Names drift and get re-labeled; IDs don't.
- No regex in view logic. It's slow at scale and hard to review; use explicit mappings or lookup tables.
- Cast only where the types actually differ. Redundant casts hide real type mismatches and clutter the query.
- Read `docs/schema/<table>.md` before querying a table you haven't used in this session.
- Verify a query by numbers, not by eye: compare totals to the base CTE, check for NULL and duplicate keys, and show those counts.
- Test queries through `uv run python scripts/q.py "<sql>"`, which uses a read-only user and caps rows.
