# Profile

`profile/jarvis.profile.yaml` is the only file you edit to change what your Jarvis is.
Everything in `config/`, and `JARVIS.md`, is rendered from it by `uv run scripts/bootstrap.py render`.
`examples/profile.example.yaml` is a complete example.

After editing: `uv run scripts/bootstrap.py validate`, then `render`, then review `git diff`, then `uv run scripts/guard_pin.py pin`.
A scheduled run refuses to start until the pin matches.

## Keys

| Key | Default | Meaning |
| --- | --- | --- |
| `assistant.name` | `Jarvis` | What the assistant calls itself in the prompt and the report header. |
| `principal.name` | required | Your full name. |
| `principal.short_name` | first word of `name` | What the prompt calls you. |
| `principal.pronouns` | `they` | `they`, `he`, or `she`; sets "their" and "them" in the prompt. |
| `principal.emails` | `[]` | Your addresses; the first is the default target of `gmail-to-self`. |
| `principal.role` | required | One line, written to follow your name: "leads the Platform team at Example Corp". |
| `principal.scope` | `work` | `work`, `personal`, or `mixed`; changes the mission line and the privacy rule. |
| `principal.focus` | none | What matters most right now, in one line. |
| `principal.goal_documents` | plans, proposals, and charters | Where your planning documents live; they outrank inference in the goal model. |
| `timezone` | required | IANA zone; every date decision is made in it. |
| `working_hours.days` | `[mon, tue, wed, thu, fri]` | Working weekdays; report and consolidate jobs run on these and business days count them. |
| `working_hours.start`, `.end` | `"08:00"`, `"18:00"` | Consolidate runs hourly between them. Quote clock times. |
| `holidays.country`, `.subdivision` | none | Public holidays from the `holidays` package, this year and next. |
| `holidays.extra` | `[]` | Your own days off, `YYYY-MM-DD name`. |
| `schedule.morning`, `.midday`, `.eod`, `.dream` | `08:00`, `13:00`, `18:00`, `02:00` | Run slots in your timezone. `consolidate_minute` (default 30) and `consolidate_hours` can be set too. |
| `sources` | `{}` | Enabled source adapters by name, in any order; reading order comes from each adapter's `order` unless you set `order` here. |
| `sources.<name>.servers.<role>` | the adapter's candidates | The MCP server names this role loads under, as `doctor.py tools` prints them. |
| `sources.<name>.params` | the adapter's defaults | The adapter's parameters, such as `projects`, `repos`, or `spaces`. |
| `delivery.adapter` | required | `slack-self-dm`, `gmail-to-self`, or `file-only`. |
| `delivery.target` | per adapter | The one allowed target: your Slack user id, or your own address. |
| `delivery.servers` | the adapter's candidates | Server names the send tool loads under. |
| `runtime.claude_bin` | `claude` on `PATH` at render time | Absolute path to Claude Code; schedulers have no shell profile. |
| `runtime.claude_config_dir` | none (`~/.claude`) | `CLAUDE_CONFIG_DIR` for headless runs, if you use a separate one. |
| `runtime.canary`, `.canary_model` | `true`, `haiku` | The live hook proof before every scheduled run. |
| `runtime.models.<mode>` | see `scripts/bootstrap.py` | Per-mode `model`, `max_turns`, `timeout_minutes`; also `first_run` and `replay`. |
| `runtime.lock_wait_minutes` | `15` | How long a report run waits for a busy lock. |
| `runtime.eval_tolerance` | `0.05` | How far bump recall may drop against the baseline before the eval gate refuses a commit. |
| `scheduler.backend` | `launchd` on macOS, else `systemd` | `launchd`, `systemd`, `cron`, or `none`. |
| `scheduler.label` | `jarvis` | Job name prefix, e.g. `com.example.jarvis`. |
| `scheduler.path` | Homebrew, `/usr/local`, `~/.local/bin`, system dirs | `PATH` for scheduled runs; must reach `uv`, `git`, and any reader such as `gh`. |
| `index.backend` | `sqlite` | `sqlite` (no setup) or `postgres` (needs pgvector and `config/db.env`). |
| `files.opinions` | none | A file describing how you judge; read every run. |
| `files.voice` | none | A file describing how you write; the floor for drafts. |
| `curation` | `curate` | `curate` writes every run's candidate queue for labeling; switch to `auto` after two weeks. |
| `decisions` | `[]` | Lines for the decisions log in `OWNER.md`, written when it is first created. |

## Files render creates once

`OWNER.md` holds your standing context, extra rules, and the decisions log; it is loaded next to `JARVIS.md` and never overwritten.
`config/thresholds.md` holds the surfacing threshold and staleness thresholds; render only appends the key of a newly enabled source's loop kind.
`config/reactions.md` maps report reactions to actions.
