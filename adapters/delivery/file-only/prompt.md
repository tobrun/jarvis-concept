Reports are written to `reports/<date>-<mode>.md` only; nothing is sent anywhere.
`scripts/run.py` runs the report check on that file after the session, and a passing report clears the open alerts it carries.
Snoozes, corrections, and promotions arrive in interactive sessions (`scripts/jarvis`).
