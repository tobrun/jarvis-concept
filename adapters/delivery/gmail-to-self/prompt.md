Reports are mailed to {{principal}}'s own address with `{{tool}}`, `to` exactly `["{{target}}"]`, subject `<header line>`, and the report as plain-text `body`; they are also written to `reports/<date>-<mode>.md`.
Do not post item replies: email has no per-item reactions.
Record the sent message id with `uv run scripts/append.py report_messages` as `message_id` with kind `report`.
{{principal}}'s replies in that mail thread (read through the mail source) carry snoozes, corrections, and promotions: `snooze <raw_id>`, `done <raw_id>`, `noise <raw_id>`, or free text.
