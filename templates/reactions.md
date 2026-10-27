# Reactions

Reactions on an item's thread reply, read by the next run, when the delivery adapter supports reactions.
Each item is posted as its own reply in the report thread so a reaction maps to exactly one item.
Lines of the form `reaction: action` are read; the names are Slack emoji names by default.

zzz: snooze
white_check_mark: resolve
x: noise

- `snooze` sets `snoozed_until` to the default snooze in `config/thresholds.md`.
- `resolve` closes the loop with outcome `acted`.
- `noise` records an `exclude` label with reason `reaction`.
