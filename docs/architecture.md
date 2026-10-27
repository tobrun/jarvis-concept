# Architecture

Purpose: Jarvis is a personal assistant made of a Claude Code prompt (`JARVIS.md` plus `OWNER.md`) run headless on a schedule inside a deterministic Python harness.
A scheduler starts a run, the harness locks, verifies the guard, plans the run, and starts one bounded Claude Code session.
Every tool call passes the guard before it runs and the capture hook after it.
The model reads sources and writes memory, drafts, and a report; the guard lets the report out only through the one delivery adapter and only when it passes the linter; the harness records the run and commits its data.
The model judges; the harness enforces, records, and measures.
This kit adds the layer that makes the harness anyone's: a profile, adapters, and a renderer that turn them into the prompt and config, an interview that writes the profile, and a doctor that checks the result.

## Components

| Component | Responsibility | Lives at | Talks to |
| --------- | -------------- | -------- | -------- |
| profile | the owner's specifics: who, when, which sources and params, delivery, host | `profile/jarvis.profile.yaml` | renderer |
| adapters | per source: id form, match strategy, capture kind, read and forbidden tools, staleness kinds, params, reading policy; per delivery: send tool, keys, target | `adapters/sources/*/`, `adapters/delivery/*/`, `scripts/adapters.py` | renderer |
| renderer | validate the profile and adapters; render the prompt, capture map, allowlist, runtime config, holidays, scheduler units | `scripts/bootstrap.py`, `templates/` | adapters, scheduler |
| bootstrap interview | discover connectors, interview the owner, write the profile and new adapters, render, hand over | `.claude/skills/bootstrap/SKILL.md`, mode `bootstrap` | renderer, doctor |
| doctor | check Claude Code, the guard, the profile, connectors (headless probe), readers, index, schedule, and the live canary | `scripts/doctor.py` | Claude Code, guard integrity |
| prompt | operating instructions loaded into every session, and the hand-edited config it reads | `JARVIS.md` (rendered), `OWNER.md`, `CLAUDE.md`, `config/thresholds.md`, `config/reactions.md`, `config/holidays.md` | ledger tools, report linter, index (through the model) |
| hooks | wire guard and capture onto every tool call | `.claude/settings.json` | guard, capture |
| guard | per-mode allowlist of tool names, write globs, Bash prefixes; the delivery gate; permission rules for `--allowedTools` | `scripts/guard.py`, `scripts/guard.sh`, `config/allowed_tools.yaml` | report linter, raw store |
| guard integrity | pin and verify the enforcement set, detect hook overrides, self-test the guard | `scripts/guard_pin.py`, `state/guard.sha256` | guard |
| capture | write every read tool's response verbatim to the raw store | `scripts/capture.py`, `scripts/capture.sh`, `config/sources.yaml` | raw store |
| raw store | resolve raw ids in each adapter's form, tombstone and forget items | `scripts/raw.py` | none |
| readers | non-MCP sources behind a narrow command: GET-only GitHub | `scripts/gh_read.py` | external HTTP |
| ledger tools | append-only ledgers, loop staleness, coverage, dedup, claim sampling, business days | `scripts/append.py`, `scripts/loops.py`, `scripts/source_coverage.py`, `scripts/dedup.py`, `scripts/claims.py`, `scripts/bizdays.py` | raw store |
| report linter | report format, resolvable raw ids, open alerts | `scripts/report_check.py` | raw store |
| planner | plan a run: effective mode, late collapse, lookback, weekly, holiday, resume | `scripts/schedule.py` | none |
| runner | lock, preflight, session, timeout, alerts, memory restore, data commit; interactive launcher; one-time setup | `scripts/run.py`, `scripts/run.sh`, `scripts/jarvis`, `scripts/setup.sh` | guard integrity, planner, guard, Claude Code |
| scheduler | render, install, and uninstall launchd, systemd, or cron jobs | `scripts/scheduler.py`, `config/scheduler/` | runner |
| replay and eval | replay a committed run or the golden week offline in a temp copy; freeze golden weeks | `scripts/replay.py`, `scripts/replay.sh`, `scripts/freeze_golden.py` | runner, metrics, eval gate |
| eval gate | refuse prompt, owner file, threshold, or script commits without a passing eval | `scripts/eval_gate.py`, `scripts/hooks/` | none |
| metrics | weekly outcome metrics | `scripts/metrics.py` | none |
| index | retrieval over memory files with local embeddings, in SQLite (default) or Postgres with pgvector | `scripts/index.py`, `scripts/index_sqlite.py`, `scripts/db_setup.sh` | Postgres when configured |
| shared | repo root, clock and timezone, working days, config readers, locked jsonl append, atomic json | `scripts/common.py` | none |

## Flows

### Bootstrap
1. `scripts/setup.sh` syncs dependencies, installs the eval gate hooks, renders the skeleton config (only `bootstrap` and `dev` can start), pins the guard, and self-tests it.
2. `scripts/jarvis bootstrap` opens a Claude Code session in mode `bootstrap` running the `bootstrap` skill.
3. The skill runs `doctor.py tools` to see the connectors a headless session gets, interviews the owner, writes the profile (and any new source adapter), and runs `bootstrap.py validate` and `render`.
4. The owner reviews the diff, pins the guard, runs `doctor.py --canary`, does a first run by hand, and installs the schedule.

### Scheduled run
1. The scheduler runs `run.sh <mode>`, which clears test clocks and execs the runner.
2. The runner refuses to run before bootstrap, then takes the run lock: report modes wait, memory jobs skip when busy.
3. Preflight: guard integrity verifies the pin and self-tests the guard, then a canary session proves the hooks load.
4. The planner plans the run; the runner starts `claude -p` with the guard's permission rules, the mode, and the run id.
5. The session runs to completion or the wall clock; the runner records the status, restores memory after a failed memory job, raises alerts, clears the alerts a delivered report carried, and commits the data directories only.

### Guarded tool call
1. Claude Code calls the PreToolUse hook, which runs the guard.
2. The guard denies (exit 2, a line in the denial ledger) or allows; a delivery call first passes the target check, the report linter, and the OOO and holiday checks.
3. Claude Code's own permission rules, generated by the guard, refuse anything outside the mode even without the hook.
4. After the tool, the PostToolUse hook runs capture, which appends a source tool's response to the raw store.

### Report delivery
1. The model writes the report file and runs the report linter itself.
2. With a sending adapter, it delivers; the guard re-runs the linter, resolves every cited raw id, and requires every open alert. The model records the sent message with the ledger tools.
3. With `file-only`, nothing is sent; the runner checks the report file after the session.
4. Either way, a delivered report clears the alerts that were open when the run started.

### Replay and eval
1. Replay and eval verify the live guard, then build a temp copy: data from the run's parent commit or the golden week's start commit, raw cut at the run's finish, current prompt, owner file, scripts, and config.
2. The runner's session code starts Claude Code in mode `replay` in the copy, offline, with the copy's guard.
3. Replay writes the report diff and denials to `eval/replays`; eval rescoring writes `eval/last.json`, which the eval gate checks on commit.

### Consolidate and reindex
1. The hourly consolidate session extracts and verifies claims through the raw store and the ledger tools.
2. It ends with the index rebuild, which embeds changed memory files.

## Boundaries

| Boundary | Kind | Owned by | Notes |
| -------- | ---- | -------- | ----- |
| Claude Code CLI | external process | runner, doctor | `claude_bin` and `claude_config_dir` from the profile; hooks loaded from `.claude/settings.json` |
| MCP connectors | external services | guard | read tools per mode from the source adapters; the delivery adapter's send tool is the only write |
| Command-line readers | external HTTP | readers | GET only, e.g. `scripts/gh_read.py` through the owner's `gh` login |
| Postgres with pgvector | store | index | only with `index.backend: postgres`; started by the owner |
| `raw/` and `eval/golden/` | files | capture, raw store | git-ignored, local only, append-only |
| git | store | runner | data commits with `--no-verify`; eval gate hooks via `core.hooksPath` |
| launchd, systemd, cron | scheduler | scheduler | installed by the owner, never by setup or bootstrap |
| clock | time | shared | the profile's timezone; `JARVIS_NOW` overrides it for replay and tests |

## Cross-cutting

Mode: `JARVIS_MODE` selects the allowlist and an unset mode denies every call; human sessions start through `scripts/jarvis`.
Run identity: `JARVIS_RUN_ID` stamps raw lines, denials, and alerts.
Clock: every script reads time through `common.now()` in the owner's timezone.
Ledgers: one locked single-write append in `scripts/common.py`; the model never edits a ledger directly.
Failure visibility: `state/alerts.json` (written by the runner) must appear on every delivered report until a delivered report carries it.
Generated files: render owns `JARVIS.md` and `config/` (but not thresholds and reactions); a hand edit there is lost on the next render.

## Entry points

`scripts/setup.sh` (one-time setup), `scripts/jarvis` (interactive sessions, including `bootstrap`), `scripts/run.sh` (the scheduler), `scripts/replay.sh` (replay and eval), `scripts/guard.sh` and `scripts/capture.sh` (Claude Code hooks), `scripts/hooks/pre-commit` and `scripts/hooks/commit-msg` (git), and the CLIs `scripts/bootstrap.py`, `scripts/doctor.py`, `scripts/scheduler.py`.
