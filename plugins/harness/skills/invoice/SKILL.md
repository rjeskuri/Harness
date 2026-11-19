---
name: invoice
description: Export one month of this project's billed hours for invoicing - per ticket and per epic, with each ticket's title and Resolution summary and the time outside any ticket on its own line, as CSV and markdown, reconciled with worklog/worklog.csv. Use when the user runs /harness:invoice or asks for a month's invoice by ticket.
argument-hint: "[--month yyyy-mm]"
disable-model-invocation: true
allowed-tools:
  - Bash(harness-worklog:*)
---
# Invoice

The tool is `harness-worklog` (on PATH while this plugin is enabled; otherwise `python3 "${CLAUDE_PLUGIN_ROOT}/bin/harness-worklog"`). It works on the current repository. Arguments given: "$ARGUMENTS".

1. The month is `--month yyyy-mm` from the arguments; with none, the month before the current one (the one being invoiced). If the month is the current one, run `harness-worklog update` first so its last days are in the log and on the tickets.
2. Run `harness-worklog invoice --month <yyyy-mm>`, adding `--out-dir DIR` when the user names a folder (the default is `worklog/invoices` in the main checkout, beside the log). It reads the hours comment on every ticket of the project's GitHub repository and the month's rows of `worklog/worklog.csv`, and writes `invoice-<yyyy-mm>.csv` (date, epic, ticket, title, hours, resolution; one row per ticket per day, one `unattributed` row per day for the time outside any ticket, manual rows included, and a `Total` row) and `invoice-<yyyy-mm>.md` (by epic, by ticket, by day).
3. Pass on any `warning:` line. A day whose tickets carry more hours than the log means the log was changed after the tickets were written: say which day, and that `harness-worklog update --since <day>` recounts it while its transcripts still exist. A project whose origin is not on GitHub exports every hour as time outside a ticket.
4. Report in three lines: the month's total hours, the hours per epic (and outside any ticket), and the two file paths. Do not send or share the files; the user does.
