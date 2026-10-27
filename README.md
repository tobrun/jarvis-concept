# Jarvis kit

Jarvis is a personal assistant that watches the systems you work in, keeps track of what you owe and what is owed to you, and reports what needs you a few times a day.
It is a Claude Code prompt run headless on a schedule inside a Python harness that enforces what it may do, records what it read, and measures how well it does.
It never acts for you: the report is its only outbound write.

This repository is the kit: the harness, adapters for common systems, and an interview that turns it into your own Jarvis.
`docs/concepts.md` explains the ideas, `docs/architecture.md` maps the code, `docs/decisions.md` records why it is built this way.

## Requirements

- macOS or Linux, git, and [uv](https://docs.astral.sh/uv/).
- [Claude Code](https://docs.claude.com/en/docs/claude-code), logged in, with MCP connectors for the systems you want read (Slack, Gmail, Google Calendar, Drive, Jira, Confluence, Wispr Flow, or your own).
- For GitHub, the `gh` CLI, logged in.

## Bootstrap

```sh
git clone <this repo> ~/jarvis && cd ~/jarvis
scripts/setup.sh            # dependencies, git hooks, skeleton config, guard pin and self-test
scripts/jarvis bootstrap    # the interview: discovers your connectors and writes your profile
```

The interview writes `profile/jarvis.profile.yaml` (and `OWNER.md`), then renders `JARVIS.md` and everything in `config/` from it.
It ends with a checklist:

```sh
git diff                                  # read what Jarvis will be allowed to do
uv run scripts/guard_pin.py pin           # approve it: runs refuse an unpinned guard
uv run scripts/doctor.py --canary         # connectors, readers, index, schedule, live hook proof
scripts/run.sh morning                    # a first run by hand; it reads 30 days and is slow
uv run scripts/scheduler.py install       # when the report looks right
```

To change anything later, edit the profile (see `docs/profile.md`), then `uv run scripts/bootstrap.py render`, review, and pin again.
`examples/profile.example.yaml` is a complete profile for a fictional owner.

## Every session needs a mode

The guard in `.claude/settings.json` denies every tool call when `JARVIS_MODE` is unset, so a plain `claude` started in this directory can do nothing, by design.
Start sessions through the launcher:

```sh
scripts/jarvis              # interactive: questions, corrections, misses
scripts/jarvis curate       # label the candidate queue (first two weeks)
scripts/jarvis bootstrap    # set up or reconfigure
scripts/jarvis dev          # work on the harness itself
```

In `dev` mode local files and shell commands are open, but every outbound tool stays denied and `raw/` and the ledgers stay append-only.
Claude Code reloads project hooks while a session runs: a session started without a mode is locked out the moment the hooks appear, so restart it with `JARVIS_MODE=dev claude --continue`.

## Sources and delivery

Source adapters in `adapters/sources/`: `google-calendar`, `jira`, `slack`, `gmail`, `github`, `confluence`, `atlassian`, `google-drive`, `wispr`, `notes`.
Delivery adapters in `adapters/delivery/`: `slack-self-dm`, `gmail-to-self`, `file-only`.
A system without an adapter takes one YAML file and one Markdown file; the interview can write them, and `docs/writing-an-adapter.md` is the reference.

## Schedule

`uv run scripts/scheduler.py install|uninstall|status` manages launchd (macOS), systemd user timers, or cron, from the units rendered into `config/scheduler/`.
Reports run on working days at the morning, midday, and end-of-day slots, consolidation runs hourly through working hours, and the nightly dream job reflects on the day.
Each run writes `state/logs/<run_id>.log`, records itself in `state/runs.json`, and commits its data.
A failure lands in `state/last_error.md` and as an alert on top of the next report.

## Replay and eval

```sh
scripts/replay.sh replay 2026-10-26 morning   # replays a committed run offline, diff in eval/replays/
uv run scripts/freeze_golden.py 2026-W44       # after a real week plus 5 business days
scripts/replay.sh eval 2026-W44                # replays the golden week, writes eval/last.json
scripts/replay.sh eval --accept                # makes it eval/baseline.json
```

Once a golden week exists, a commit that changes `JARVIS.md`, `OWNER.md`, `config/thresholds.md`, or `scripts/` needs a passing `eval/last.json` for exactly the staged content and an `eval:` line in its message.

## Tests

```sh
uv run ruff check . && uv run ruff format --check .
uv run pytest -m "not e2e and not postgres"
uv run pytest -m e2e        # scheduled runs, replay, and eval with a fake claude binary
uv run pytest -m postgres   # the Postgres index backend against a throwaway cluster
```

The tests render the example profile into temporary repositories and never read your own profile or config.
