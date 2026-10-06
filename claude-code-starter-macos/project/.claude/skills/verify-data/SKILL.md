---
name: verify-data
description: Checklist for verifying code that transforms, aggregates, joins or loads data with pandas, polars or SQL. Use when changing ETL steps, queries, metrics or report logic, or when asked whether numbers are right. Not needed for pure refactors already covered by golden-file tests.
---
# Verifying data changes

Data bugs rarely raise errors; they produce plausible wrong numbers. Before calling a data change done, show evidence for each check that applies:

- **Row counts**: before and after each join or filter, with a reason for every change. An unexpected increase after a join usually means duplicate keys.
- **Keys**: the table's grain key is unique (`df.duplicated(keys).sum() == 0`, or `GROUP BY key HAVING COUNT(*) > 1` returns nothing).
- **Nulls**: null counts in the columns the change touches, before and after.
- **Totals**: reconcile totals against the source or a known report. State both numbers and the difference.
- **Edge cases**: empty input, a single row, duplicates, and time-zone, month-end and year-end boundaries when dates are involved.
- **Regression**: when replacing logic, run old and new on the same input and diff the outputs. Explain every difference or treat it as a bug.

Automate the checks with small fixtures in `tests/fixtures/`. For checks against real data, use read-only access and report aggregates rather than raw rows.
