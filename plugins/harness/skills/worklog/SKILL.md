---
name: worklog
description: Time tracking for invoicing - recompute this project's hours from its session transcripts into worklog/worklog.csv, write the day's client-facing description, record a time-zone change, or export a month. Use when the user runs /harness:worklog, at the end of a working session on a billable project, or when they ask about hours or an invoice.
argument-hint: "[update | describe | export --month yyyy-mm | zone | add | init]"
disable-model-invocation: true
allowed-tools:
  - Bash(harness-worklog:*)
---
# Work log

The tool is `harness-worklog` (on PATH while this plugin is enabled; otherwise `python3 "${CLAUDE_PLUGIN_ROOT}/bin/harness-worklog"`). It works on the current repository. Arguments given: "$ARGUMENTS".

- No arguments, or `update`: run `harness-worklog update`. If it refuses because `worklog/timezones.csv` is missing, run `harness-worklog init` and show the user the zone it recorded. If it refuses because the Mac changed time zone, show its suggestion and ask the user to confirm before running the `zone` command it proposes.
- `update` also writes each ticket's hours by day into its hours comment on the GitHub issue (one comment per ticket, kept current in place), so this end-of-session run is what keeps the tickets' hours right for the month's invoice. Pass on any `warning:` line about a ticket whose comment was not updated; the next `update` within 14 days catches it up.
- After an update, read today's row. If its description is empty or was filled from commit subjects, write one with `harness-worklog describe --date <today> --text "..."`: one to three plain, client-facing sentences about what was done today, no names of people, no jargon, no file paths.
- `export --month yyyy-mm [--rate N] [--out file.csv]`: the invoice rows by day. Show the total. For a month by ticket and epic, use `/harness:invoice`.
- `add --date --hours --description`: a manual row for work done outside Claude Code. Never change the hours of an auto row.
- `zone`: show the zone timeline, or record a switch with `--since` and `--zone` once the user has confirmed the moment.

Report the day's hours and the description you wrote, in two lines.
