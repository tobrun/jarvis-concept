# Decisions

The harness decisions were made building the first Jarvis and carried into this kit with the owner's specifics removed.
The kit's own decisions, made turning that Jarvis into something anyone can bootstrap, come first.

## Principles

None promoted yet; no rationale has recurred three times across specs.

## Kit

D-kit-shape: What does the shareable kit ship? (2026-10-27, kit plan)
  ✓ working harness plus an interview - the generic harness as tested code, source and delivery adapters, and a `bootstrap` skill that turns a profile into the prompt and config; the owner's Jarvis is a fork of the kit with its profile and generated files committed
  ✗ concept documents only - every owner rebuilds the guard, raw layer, and eval from prose, and gets them subtly wrong
  ✗ the first Jarvis with placeholders - the source wiring stays baked into the scripts and the prompt

D-profile: Where do the owner's specifics live?
  ✓ one profile, everything else rendered - `profile/jarvis.profile.yaml` is the single hand-edited source; `bootstrap.py render` writes `JARVIS.md` and `config/` deterministically, so adding a source is edit, render, pin
  ✗ hand-edited config files - the allowlist, capture map, and prompt drift apart; the first Jarvis needed a test to keep two lists equal

D-owner-file: Where do prompt edits that must survive a re-render go?
  ✓ `OWNER.md`, imported next to `JARVIS.md` - created once by render and never overwritten; holds standing context, extra rules, and the decisions log; eval-gated like the prompt
  ✗ edit the rendered `JARVIS.md` - the next render loses the edit

D-adapter-interface: What is a source adapter?
  ✓ data, not code - `adapter.yaml` (id form, match strategy, capture kind, read and forbidden tools per server role, staleness kinds, params) and `prompt.md` (the reading policy); the profile supplies server names and params; render generates both the capture map and the allowlist's source group from it
  ✗ a Python plugin per source - code loaded by the guard and capture would have to be pinned per adapter, and an owner adding a source would be editing the enforcement set

D-matchers-in-core: Where does source-specific raw id matching live?
  ✓ a fixed set in `scripts/raw.py` (`parts`, `github`) chosen by name in the adapter - matching is part of the report gate, so it stays in the pinned enforcement set; a new strategy is a core change with tests
  ✗ `matcher.py` per adapter - unpinned code on the report gate's path

D-server-names: How does an adapter cope with one connector loading under different names?
  ✓ candidates plus profile override - each server role lists every known name; the profile can name the exact ones `doctor.py tools` saw; all named variants are allowed and captured
  ✗ one name per adapter - interactive and headless sessions load the same connector under different names, so one of them is always denied

D-delivery-adapter: How is the one outbound write generalized?
  ✓ a delivery adapter with a constrained tool - the adapter names the send tool, the allowed keys, the target key and shape, the message key, and the thread key; render writes the exact target into the allowlist and the guard requires it verbatim; `file-only` has no send tool at all and the runner checks the report file instead
  ✗ keep the Slack-shaped check - every other channel would need guard code

D-bootstrap-mode: How does the interview run under a guard that denies everything without a mode?
  ✓ a `bootstrap` mode - local tools, writes to the profile, `OWNER.md`, and source adapters only, and Bash limited to `bootstrap.py` and `doctor.py`; no source or delivery tool; render without a profile writes a skeleton in which only `bootstrap` and `dev` can start
  ✗ bootstrap in `dev` - the interview would run with every local write open

D-pin-by-human: Who pins the guard after render changes the allowlist?
  ✓ the owner, from a shell - render can widen what Jarvis may do, so the pin is the human review point; no mode allows `guard_pin.py pin`, and every scheduled run refuses an unpinned change
  ✗ render pins - a profile written by the interview would approve its own permissions

D-tool-discovery: How does bootstrap learn which connectors exist?
  ✓ the headless init event - `doctor.py tools` runs one `claude -p` turn with stream-json output and reads `mcp_servers` (every server with its status) and `tools` (those of connected servers); servers still connecting are probed again up to three times - Confidence: 75% ⚠ a server that stays `pending` is reported with unverified tool names
  ✗ the interactive session's own tool list - interactive and headless sessions name some servers differently

D-index-sqlite: Which index backend is the default?
  ✓ SQLite with an exact numpy cosine scan - nothing to install or start, one file in `state/`, fast at one person's memory size; Postgres with pgvector stays as the `postgres` backend behind the same CLI and tests
  ✗ Postgres by default - a database server to install and keep running before the first run
  ✗ sqlite-vec - a loadable extension that some Python builds cannot load

D-scheduler-backends: Which schedulers are supported?
  ✓ launchd, systemd user timers, and cron, rendered into `config/scheduler/` - systemd timers carry the owner's timezone; launchd and cron fire on host time and `doctor.py` warns when the host zone differs; `schedule.py` decides in the owner's zone either way
  ✗ launchd only - excludes every Linux host

D-working-days: What is a business day?
  ✓ a working weekday from the profile that is not in `config/holidays.md` - holidays come from the `holidays` package for the profile's country and region, plus the owner's own days off
  ✗ Monday to Friday - wrong for anyone whose week is not

D-tests-render: What do the tests run against?
  ✓ the example profile rendered once per session - no test reads the developer's own profile or config, so the suite passes in a fresh clone and on any owner's fork
  ✗ the repo's live config - the suite would test whoever ran it last

## Guard

D-guard-fail-closed: What happens when the guard itself breaks? (2026-10-26, harness spec)
  ✓ wrapper plus second layer - `scripts/guard.sh` runs the guard with the absolute venv Python and turns any exit other than 0 into exit 2; a hook timeout or a deleted wrapper still fails open in Claude Code, which D-permission-mode's generated rules and D-canary cover - auto-applied at Confidence: 80%
  ✗ Python try/except only - a missing interpreter exits 127, which Claude Code lets through

D-permission-mode: How do headless runs get tool permissions? (2026-10-26, harness spec)
  ✓ generated `--allowedTools` plus guard - `guard.py rules <mode>` turns the allowlist into Claude Code permission rules (exact tool names, `Edit(reports/**)` path rules, which Claude Code applies to every file-editing tool, `Bash(uv run scripts/x.py:*)` prefixes); the session runs in default permission mode so anything outside them is refused even if hooks never load; the guard adds parameter checks the rules cannot express (reviewer) - auto-applied at Confidence: 70% verified 2026-10-27 in a headless session: only `Edit(path)` rules match file edits; `Write(path)` and the others are ignored with a warning, so only `Edit(path)` rules are generated
  ✗ bypassPermissions plus guard - one layer; a hook that silently fails to load lets everything through
  ✗ hand-written permission list - drifts from the allowlist

D-canary: How does a run prove Claude Code really loaded the hooks? (2026-10-26, harness spec)
  ✓ live canary in preflight - before the real session, `run.py` starts a short `claude -p` with the haiku model and `--max-turns 2`, asks it to call one forbidden tool, and aborts unless a matching line appears in `state/denied_calls.jsonl`; on by default, `canary: false` in `config/jarvis.yaml` turns it off - auto-applied at Confidence: 70% ⚠ costs one small session per run ? verify: the canary prompt reliably triggers the tool call
  ✗ hash check only - proves files are unchanged, not that hooks run

D-guard-hash: What does `state/guard.sha256` cover? (2026-10-26, harness spec)
  ✓ the enforcement set - `.claude/settings.json`, `scripts/guard.py`, `scripts/guard.sh`, `scripts/guard_pin.py`, `scripts/common.py`, `scripts/report_check.py`, `scripts/raw.py`, `config/allowed_tools.yaml`, and `config/jarvis.yaml`, hashed in a fixed order - every file the guard imports or reads; `guard_pin.py pin` rewrites it and is outside every mode's allowlist - auto-applied at Confidence: 80%
  ✗ settings file only (as section 3 says literally) - an edited allowlist or guard import would pass

D-hook-tamper: What else can switch the guard off? (2026-10-26, harness spec)
  ✓ `verify` also fails when `.claude/settings.local.json` exists, or any settings file in the repo or `CLAUDE_CONFIG_DIR` sets `disableAllHooks` - auto-applied at Confidence: 75%
  ✗ hash the project settings only - a local settings file or a global switch silently disables hooks

D-guard-mode: How does the guard know the mode, and what if it is unset? (2026-10-26, harness spec)
  ✓ environment, unset denies all - `run.py` exports `JARVIS_MODE`; hooks inherit it; unset means every call is denied; the owner starts interactive sessions with `scripts/jarvis` which sets `JARVIS_MODE=interactive` (reviewer: a forgotten variable must not widen permissions) - auto-applied at Confidence: 80%
  ✗ unset means interactive - a scheduled run that lost the variable gets interactive permissions

D-dev-mode: How does anyone develop the harness when the repo's own hooks guard every session? (2026-10-26, harness spec)
  ✓ `dev` mode - `JARVIS_MODE=dev`, set only by a human shell, allows built-in tools and Bash freely but keeps every outbound MCP write and every send denied; after changing the enforcement set the developer re-pins - auto-applied at Confidence: 75%
  ✗ a worktree without hooks - the hooks are in the tracked settings file, so every worktree has them

D-bash-matching: How are shell commands allowlisted? (2026-10-26, harness spec)
  ✓ argv prefix - split with `shlex`; deny any unquoted operator (`; & | < > ( )`), any newline, and any `$` or backtick outside single quotes; then match the leading tokens against the mode's prefixes - auto-applied at Confidence: 80% ⚠ the agent cannot pipe; scripts take files instead
  ✗ regex over the raw string - easy to bypass with quoting

D-gh-access: How does Jarvis read GitHub? (2026-10-26, harness spec)
  ✓ `scripts/gh_read.py` - GET only, host fixed to api.github.com, path checked against read patterns (pulls, reviews, commits, issues, search); raw `gh` is never allowlisted (reviewer: `gh api -f`, graphql mutations, `--hostname`, and `gh pr comment` all write or leak) - auto-applied at Confidence: 85%
  ✗ `gh` prefix with flag checks - too many write paths to enumerate

D-tool-matching: How are tools and parameters matched? (2026-10-26, harness spec)
  ✓ exact names and exact parameter sets - every allowlist entry names one full tool name; constrained tools also list the allowed parameter keys, and any other key is denied (reviewer: globs match several servers, `fetch` matches WebFetch) - auto-applied at Confidence: 85%
  ✗ globs and denied keys - new tools and new parameters pass silently

D-network-off: How is "network disabled" enforced in replay and eval? (2026-10-26, harness spec)
  ✓ allowlist plus offline env - the `replay` and `eval` modes allow no MCP tool, no web tool, no `gh_read.py`; `run.py` sets `UV_OFFLINE=1` and `HF_HUB_OFFLINE=1` - auto-applied at Confidence: 80% ⚠ the model itself still calls the Claude API
  ✗ macOS sandbox profile - `sandbox-exec` is deprecated and would also block the Claude API

## Raw layer

D-raw-capture: How do fetched items reach `raw/` verbatim before judgment? (2026-10-26, harness spec)
  ✓ PostToolUse hook - `scripts/capture.py` receives each read tool's exact `tool_response` and appends it; no model transcription - auto-applied at Confidence: 80% ⚠ connectors may truncate large responses before the hook sees them; the agent pages instead
  ✗ agent writes raw through a script - the model re-types payloads, which is neither verbatim nor cheap
  ✗ direct API fetchers - need Slack, Google, and Atlassian tokens the session does not have; the connectors hold them

D-raw-granularity: What is one raw line? (2026-10-26, harness spec)
  ✓ one line per tool call - the payload is the call's response; items are cited as `<source>:<native id>` and resolve by searching that source's payloads - auto-applied at Confidence: 65% ⚠ section 5 says one line per item ? verify: connector responses are JSON with stable ids; if so, split per item in the capture hook
  ✗ split per item now - connector response shapes are unknown and differ per tool; a wrong splitter loses data silently

D-raw-ids: What is a native id per source? (2026-10-26, harness spec)
  ✓ globally unique forms - each source adapter declares its form (`slack:<channel>:<ts>`, `jira:<issue>`, `github:<repo>#<number>`, ...), plus `<source>:call:<sha256 prefix>` for the whole call; resolve returns the latest untombstoned line that contains the id parts (reviewer: Slack ts is unique only per channel) - auto-applied at Confidence: 80%
  ✗ bare native ids - collide across Slack channels

D-tombstone-registry: How do HR and personal items stay out of raw when capture already wrote them, and when the 24h overlap fetches them again? (2026-10-26, harness spec)
  ✓ registry plus redaction - `raw.py tombstone <raw_id> --reason hr|personal` records the id and content hash in `state/tombstones.jsonl`, rewrites matching JSON objects in existing lines to `{tombstone, content_sha256, reason}` (a text payload is tombstoned whole, marked `scope: call`), and `capture.py` applies the registry before every new write, so a re-fetch never lands verbatim - auto-applied at Confidence: 75% ⚠ text payloads lose their other items for that call
  ✗ classify in the hook - the hook cannot judge HR content
  ✗ tombstone once, no registry - the next run's overlap writes it again

D-raw-in-git: Is `raw/` tracked in git? (2026-10-26, harness spec)
  ✓ git-ignored - raw is append-only, so its as-of state is a filter on `fetched_at` and needs no history; ignoring it lets a tombstone or forget really remove content and keeps the repo small; Time Machine is the backup; `eval/golden/` follows raw (both review passes) - auto-applied at Confidence: 70% ⚠ section 15 says everything is git-tracked ? verify: the owner accepts raw outside git
  ✗ tracked - a forget leaves content in history unless history is rewritten, which is destructive; the repo grows with every Slack payload

## Runner and schedule

D-schedule-logic: Where do late runs, collapse, lookback, and resume live? (2026-10-26, harness spec)
  ✓ `schedule.py` - pure functions over `now`, the slot table, and `state/runs.json`; the agent receives the plan in its prompt - auto-applied at Confidence: 85%
  ✗ in the prompt - date arithmetic is where models slip

D-late-collapse: What does a collapsed late run contain? (2026-10-26, harness spec)
  ✓ the latest passed slot, plus the missed morning brief - a run is late when now is more than 30 minutes past its effective slot; when no morning report exists for today the plan sets `include_morning: true` so Today and meeting prep are not lost (blind-spot pass) - auto-applied at Confidence: 80%
  ✗ the latest slot only - a wake at 14:00 would drop the day's meeting prep

D-resume: How does an interrupted run resume from raw? (2026-10-26, harness spec)
  ✓ plan detects it - a `run_id` in `state/runs.json` marked `started` without `finished`, with raw lines on disk, makes the next plan carry `resume_run_id`; the agent judges those lines before fetching (reviewer: prompt sections 5 and 14 promise this) - auto-applied at Confidence: 80%
  ✗ refetch everything - wastes the first run's work and breaks the promise

D-holidays: What do report runs do on a holiday? (2026-10-26, harness spec)
  ✓ same as OOO - read, record, write the report, do not post; the return brief covers it - auto-applied at Confidence: 65% ⚠ ? verify: the owner wants holiday silence rather than no run at all
  ✗ skip the run - the next run's lookback still covers it, but no report file exists for the return brief

D-weekly-day: When does the weekly addendum run? (2026-10-26, harness spec)
  ✓ eod of the last business day of the ISO week - a holiday on that day moves it to the business day before - auto-applied at Confidence: 70% ⚠ ? verify: the owner wants it on a working day rather than skipped
  ✗ Friday only - a Friday holiday report is unsent, so the week has no review

D-lock-policy: What if two jobs fire together? (2026-10-26, harness spec)
  ✓ wait for report modes, skip for memory jobs - one `fcntl` lock on `state/run.lock`; morning, midday, and eod wait up to 15 minutes; consolidate and dream skip when busy; consolidate fires at :30 so it never meets a report slot; a report that still cannot get the lock records `skipped` in `runs.json` and the next report shows it (reviewer: consolidate at 08:00 could swallow the morning) - auto-applied at Confidence: 80%
  ✗ everything skips on a busy lock - a report can vanish silently

D-run-bounds: What bounds a run? (2026-10-26, harness spec)
  ✓ per-mode wall clock, turns, and model in `config/jarvis.yaml` - `run.py` kills the session at the limit and records `timeout`; defaults morning 40 minutes, midday and eod 25, consolidate 15, dream 20, first run 120 - auto-applied at Confidence: 80%
  ✗ unbounded - one hung session holds the lock forever

D-session-launch: How does `run.sh` start the session under a scheduler? (2026-10-26, harness spec)
  ✓ absolute paths - schedulers have no aliases or shell profile; the unit sets `PATH`; `run.sh` unsets `JARVIS_NOW` outside replay and execs `run.py`, which calls `claude_bin` and sets `CLAUDE_CONFIG_DIR` from `config/jarvis.yaml` - auto-applied at Confidence: 80% ⚠ keychain and connector auth under the scheduler is unverified until the first installed run; `doctor.py --canary` is the check
  ✗ source the shell profile - slow and pulls in interactive-only state

D-commit-owner: Who commits at the end of a run, and what? (2026-10-26, harness spec)
  ✓ `run.py`, data paths only - after the session, it stages only `memory/ drafts/ reports/ curation/ state/ eval/replays/` and commits `<mode> <date> <time>` with `--no-verify` because those paths are outside the eval gate; never `git add -A` (both review passes) - auto-applied at Confidence: 85% ⚠ interactive sessions do not take the run lock
  ✗ the agent - one more write tool, and a crashed agent skips it

D-failed-job-restore: What does a failed consolidate or dream leave behind? (2026-10-26, harness spec)
  ✓ restore its paths - on non-zero exit or timeout, `run.py` restores `memory/` to HEAD (`git checkout` plus removal of untracked files under `memory/`) before committing the rest (reviewer: section 14 says failed memory jobs leave all files as they were) - auto-applied at Confidence: 80%
  ✗ temp dir and move - every write tool would need redirecting

D-error-visibility: How does the owner learn a run failed? (2026-10-26, harness spec)
  ✓ the next report says so - `run.py` keeps `state/alerts.json` (preflight failure, timeout, skipped slot, capture errors); the report linter requires each open alert on the lines after the header; a posted report clears them - auto-applied at Confidence: 80%
  ✗ log file only - breaks "never silently degrade"

D-scheduler-install: Does setup install the scheduled jobs? (2026-10-26, harness spec)
  ✓ generate, do not install - `bootstrap.py render` writes the units into `config/scheduler/`; `scheduler.py install` exists but neither setup nor bootstrap runs it, because installing starts real delivery - auto-applied at Confidence: 85%
  ✗ install during bootstrap - outward-facing; needs the owner present

D-clock: How do scripts get "now" in replay? (2026-10-26, harness spec)
  ✓ `JARVIS_NOW` - an ISO timestamp every script reads through `common.now()` - auto-applied at Confidence: 90%
  ✗ patch time in each test - replay runs real processes, which a test patch cannot reach

## Eval

D-candidate-log: Where does section 8's "log every candidate" go? (2026-10-26, harness spec)
  ✓ `state/candidates/<date>-<mode>.jsonl` - one line per candidate with raw id, score, features, reason, decision; the curation queue copies it while curation is on; drift reads it - auto-applied at Confidence: 80%
  ✗ only suppressed items logged - drift and calibration need surfaced scores too

D-replay-isolation: How does replay get the input the original run saw, without touching live state? (2026-10-26, harness spec)
  ✓ temp copy - `replay.py` finds the run's commit by its message, copies `memory/ state/ curation/` from that commit's parent, takes `raw/` lines with `fetched_at` up to the run's finish (so the run's own fetches are included), overlays the current prompt, scripts, and config, links the live `.venv`, sets `JARVIS_NOW` to the slot, and runs in mode `replay` (reviewer: the parent commit alone has none of the run's raw) - auto-applied at Confidence: 75%
  ✗ replay in place with writes denied - the run needs to write its working files

D-golden-week: How does the golden week come to exist? (2026-10-26, harness spec)
  ✓ freeze later - `freeze_golden.py <iso-week>` copies that week's raw, labels, outcomes, and start commit; run after the first real week plus 5 business days for outcomes - auto-applied at Confidence: 80%
  ✗ synthesize one now - fake data would gate prompt changes on fiction; synthetic data is for tests only

D-eval-scope: Which runs does eval replay? (2026-10-26, harness spec)
  ✓ the 15 report runs of the golden week - memory is the as-of snapshot from the week's start commit; consolidate and dream are not replayed; one sample per run - auto-applied at Confidence: 70% ⚠ memory does not evolve across the replayed week, and about 15 sessions per eval ? verify: eval results are stable enough to gate on
  ✗ every job - about 70 sessions per eval

D-eval-gate: How is "no commit without eval" enforced? (2026-10-26, harness spec)
  ✓ git hooks via `core.hooksPath` - `scripts/setup.sh` sets `core.hooksPath scripts/hooks`; `pre-commit` blocks a commit staging `JARVIS.md`, `config/thresholds.md`, or `scripts/` unless `eval/last.json` records the staged content hash of those files and shows bump recall not lower than `eval/baseline.json` minus `eval_tolerance` and miss count not higher; `commit-msg` requires an `eval:` line with the delta on such commits; `replay.py eval --accept` writes the baseline; with no golden week both hooks print a notice and allow - auto-applied at Confidence: 70% ⚠ `--no-verify` bypasses it; the gate is open until a golden week exists ? verify: golden week frozen
  ✗ trust the prompt - section 13 asks for a gate

D-metrics-compute: Who computes the weekly metrics table? (2026-10-26, harness spec)
  ✓ `metrics.py` - reads outcomes, candidates, coverage, denied calls, and claim checks; writes `state/metrics/<iso-week>.json` - auto-applied at Confidence: 85%
  ✗ the agent - a Brier score is arithmetic

D-e2e-env: What does the e2e environment fake? (2026-10-26, harness spec)
  ✓ fake claude binary - `tests/e2e/fake_claude.py` reads `.claude/settings.json` and runs the real hook commands with a scripted list of tool calls, honoring `--allowedTools` the way Claude Code does; everything inside the repo is real - auto-applied at Confidence: 80% ⚠ the hook payload shape is ours, not Claude Code's ? verify: the README probe against real Claude Code
  ✗ real `claude -p` in the suite - costs tokens, nondeterministic

D-live-hook-test: Run the real Claude Code in the suite? (2026-10-26, harness spec)
  ✗ live `claude -p` scenario - proves the real hook payload, but every run costs tokens and varies
  ⊘ not doing - the run-time canary covers it in production; a scope probe was blocked by the session's permission classifier; reopen if a Claude Code upgrade changes the hook payload

## Index

D-index-content: What goes into the index? (2026-10-26, harness spec)
  ✓ memory files only - claims, bank, people, daily notes, goals, loops, each chunk carrying its claim ids and raw ids; raw text stays out so others' verbatim words never leave `raw/` (both review passes) - auto-applied at Confidence: 80%
  ✗ memory plus raw chunks - copies others' words into Postgres

D-index-store: Where does the index run? (2026-10-26, harness spec)
  ✓ Postgres 17 with pgvector - survives reboots as a service; no Docker dependency for scheduled jobs; superseded as the default by D-index-sqlite, kept as the `postgres` backend - auto-applied at Confidence: 75%
  ✗ Docker pgvector image - scheduled runs fail whenever Docker is not running
  ✗ SQLite with sqlite-vec - an extension to load; see D-index-sqlite for the default that needs none

D-embeddings: Which embedding model? (2026-10-26, harness spec)
  ✓ local fastembed `BAAI/bge-small-en-v1.5` - 384 dims as section 10 says, CPU, no torch, data stays on the Mac (local models for personal infrastructure); the model name is stored per row; rebuild is incremental by content hash - auto-applied at Confidence: 75%
  ✗ hosted embeddings API - the owner's data leaves the machine
  ✗ sentence-transformers - pulls torch for one small model

## Reporting

D-report-gate: How is the report format of section 9 enforced? (2026-10-26, harness spec)
  ✓ guard calls the linter - a top-level post to the self-DM must pass `report_check.py`: header, section order, under 60 lines, no em dash, no exclamation mark, a `[raw_id]` on every item line that resolves in `raw/` and is not tombstoned, the Sources line, and the alerts in D-error-visibility; thread replies need only resolvable ids - auto-applied at Confidence: 80%
  ✗ prompt only - the format drifts and fabricated ids pass

D-ooo-gate: How is "do not post while OOO" enforced? (2026-10-26, harness spec)
  ✓ state file plus guard - the agent writes `state/ooo.json` with the covering event's raw id and end time; the guard denies top-level posts while it covers now, and `schedule.py` marks holidays the same way - auto-applied at Confidence: 75%
  ✗ prompt only - the most visible failure would rest on the model

D-delivery: How does the report reach the owner? (2026-10-26, harness spec)
  ✓ the connector's own send tool, hard-scoped - the delivery adapter names the tool, its only allowed keys, and the one target value (the owner's Slack user id for the self-DM, the owner's address for mail); see D-delivery-adapter - auto-applied at Confidence: 80%
  ✗ own script with a user token - needs a token the session does not have; the connector holds it

D-reaction-target: What does a snooze reaction attach to? (2026-10-26, harness spec)
  ✓ one thread reply per actionable item - the top-level report stays the 60-line summary; each Slipping, Waiting on you, and Waiting on others item is also a reply carrying its `[raw_id]`; posted ts values go to the `report_messages` ledger so the next run maps reactions back and tells Jarvis's posts from the owner's replies (blind-spot pass: reactions attach to messages, not lines) - auto-applied at Confidence: 65% ⚠ ? verify: the owner finds per-item replies acceptable
  ✗ reactions on the top-level report - cannot say which item was meant

D-reactions: Which reactions does the report thread understand? (2026-10-26, harness spec)
  ✓ three - `zzz` snoozes, `white_check_mark` resolves, `x` marks noise; listed in `config/reactions.md` - auto-applied at Confidence: 65% ⚠ ? verify: the owner's preferred emoji
  ✗ snooze only - section 15 calls the file a mapping, and noise marks feed the labels

D-staleness-units: Are source loop thresholds (Jira 5 days, PR 3 days) business or calendar days? (2026-10-26, harness spec)
  ✓ calendar days - section 7 writes "business days" for loops and plain "days" for Jira and PRs; `config/thresholds.md` names the unit; source adapters declare their kinds as `stale_<kind>_days` - auto-applied at Confidence: 70% ⚠ ? verify: the owner meant calendar days
  ✗ business days everywhere - reads more into the text than it says

D-coverage-available: What is `available` when a connector reports no total? (2026-10-26, harness spec)
  ✓ null with a read-count fallback - degradation then compares `read` against the trailing ten-run median of `read`; fewer than 3 history points never count as degraded - auto-applied at Confidence: 75%
  ✗ guess a total - invents a number the Sources line shows as fact

## General

D-problem-shape: What should "implement the Jarvis prompt" produce? (2026-10-26, harness spec)
  ✓ harness plus prompt - code for every mechanical rule the prompt names; judgment stays in the prompt (prompt sections 3, 5, 13 name the enforced rules) - auto-applied at Confidence: 85%
  ✗ prompt only - leaves "enforcement does not rely on you" and the eval gate false
  ✗ Python orchestrator calling the Claude API - drops the decided runtime (scheduled headless Claude Code runs) and the MCP connectors that hold the credentials

D-language: What language and toolchain for the harness? (2026-10-26, harness spec)
  ✓ Python with uv - stdlib covers JSON, zoneinfo, fcntl, hashlib; uv pins deps in `uv.lock` on Python 3.13 because onnxruntime has no 3.15 wheel (probe during scope); pytest and ruff - auto-applied at Confidence: 85%
  ✗ TypeScript - adds Node for a script-sized codebase
  ✗ bash only - JSON, timezones, and hashing in bash are fragile

D-prompt-loading: How does every session load the prompt? (2026-10-26, harness spec)
  ✓ CLAUDE.md import - repo `CLAUDE.md` holds `@JARVIS.md` and `@OWNER.md`, so scheduled and interactive sessions both load them - auto-applied at Confidence: 85% ⚠ dev sessions load it too
  ✗ `--append-system-prompt` - interactive sessions would miss it

D-append-only: How are append-only files kept append-only? (2026-10-26, harness spec)
  ✓ one append script - `scripts/append.py <ledger> --file <json>` appends one validated line under an exclusive `fcntl` lock to a fixed set of ledgers; no mode's write scope includes them - auto-applied at Confidence: 85%
  ✗ trust the prompt - the hard rule needs a mechanism

D-config-format: What format do config files use? (2026-10-26, harness spec)
  ✓ markdown with `key: value` lines - the prompt names `.md` files the owner edits by hand; the allowlist, sources, and jarvis config are YAML because they are nested - auto-applied at Confidence: 70% ⚠ a malformed line is skipped with a warning ? verify: the owner keeps hand edits to `key: value` form
  ✗ everything YAML - section 15 names markdown files

D-typecheck: Add a type checker? (2026-10-26, harness spec)
  ✗ mypy strict - setup and annotation cost across every script for little caught
  ⊘ not doing - ruff plus tests cover a script-sized codebase; reopen if scripts pass 3000 lines or a type bug ships
