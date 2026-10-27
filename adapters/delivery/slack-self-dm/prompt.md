Reports post to {{principal}}'s Slack self-DM (channel id `{{target}}`) with `{{tool}}`, and are also written to `reports/<date>-<mode>.md`.
The top-level message is the report.
Each Slipping, Waiting on you, and Waiting on others item is also posted as its own thread reply (`thread_ts` set to the report's ts) carrying its `raw_id`, so a reaction maps to exactly one item.
Record the report's ts with `uv run scripts/append.py report_messages` as `message_id` with kind `report`, then each item reply's ts with kind `item` and its `raw_id`.
Reactions on item replies are read by the next run, per `config/reactions.md`.
