---
name: bootstrap
description: Interview a new owner and turn this kit into their own Jarvis - discover their connectors, write profile/jarvis.profile.yaml, author missing source adapters, render the prompt and config, and hand over the guard pin and first run. Use in a session started with `scripts/jarvis bootstrap`, or when the owner asks to set up, reconfigure, or add a source or delivery channel to Jarvis.
---

# Bootstrap

You are setting up Jarvis for a new owner.
The result is one file you write, `profile/jarvis.profile.yaml`, plus `OWNER.md` and any new source adapter.
Everything else is generated from them by `uv run scripts/bootstrap.py render`.

This session runs in mode `bootstrap`.
The guard lets you write only `profile/**`, `OWNER.md`, `adapters/sources/**`, and `state/tmp/**`, and run only `uv run scripts/bootstrap.py` and `uv run scripts/doctor.py`.
You have no source, delivery, or web tool, and you never pin the guard: the owner does that after reading the diff.

## 0. Learn the kit

Read these before asking anything:

- `docs/concepts.md`: what Jarvis is and why it is built this way. You will explain it in two sentences at the start.
- `docs/profile.md`: every profile key and its default.
- `examples/profile.example.yaml`: a complete profile to copy the shape from.
- `uv run scripts/bootstrap.py adapters`: the shipped source and delivery adapters. Read each `adapters/sources/<name>/adapter.yaml` you might enable.

If `profile/jarvis.profile.yaml` already exists, this is a reconfiguration: load it, summarize it in five lines, and ask only about what the owner wants to change.

## 1. Discover the connectors

Run `uv run scripts/doctor.py tools`.
It starts one short headless Claude Code session, the same way scheduled runs start, and prints every MCP server it sees with its status and, for connected servers, its tools.
Headless sessions can load a server under a different name than interactive ones; the probe's names are the ones that count.

Match the servers to adapters by each adapter's `servers.<role>.candidates`.
Put the result in front of the owner as a short table: adapter, server, status.
For a server with status `needs-auth` or `failed`, tell the owner to authenticate it with `/mcp` inside `claude` and probe again later; do not block the interview on it.

A connected server that no adapter covers is a candidate for a new adapter (step 4).
Ignore servers that hold nothing about the owner's obligations (design tools, analytics, and the like) unless the owner asks for them.

## 2. Interview

Ask with AskUserQuestion, a few related questions per round, recommended option first.
Offer defaults; never ask what the example profile or a probe already answers.

1. Who: full name, the short name Jarvis uses, pronouns (they, he, she), email addresses, role in one line, scope (`work`, `personal`, or `mixed`), current focus, and where their planning documents live (these outrank inference in the goal model).
2. When: IANA timezone, working days and hours, the country (and region) for public holidays, and known days off.
3. Sources: which discovered adapters to enable, in reading order. Then each enabled adapter's `params` (for example Jira project keys, GitHub repos, Confluence spaces). Explain that Jarvis reads everything in a source and ranks it; it does not filter by channel.
4. Delivery: where the report goes. `slack-self-dm` needs their own Slack user id; `gmail-to-self` defaults to their first email; `file-only` sends nothing and is the safe choice while they try it out.
5. Cadence: the morning, midday, end of day, and nightly slots.
6. Host: the scheduler (`launchd` on macOS, `systemd` or `cron` on Linux, `none` to schedule by hand), the index (`sqlite` unless they already run Postgres with pgvector), and `runtime.claude_bin` and `runtime.claude_config_dir` when they run Claude Code from a non-default place.
7. Judgment: optional paths to an opinions file (how they decide) and a voice file (how they write). Offer to start each from a short template in `profile/` if they have none.
8. Decisions: any standing decision worth logging (scope limits, hard nos, privacy lines). These go to `decisions` in the profile and seed `OWNER.md`.

## 3. Write and render

1. Write `profile/jarvis.profile.yaml` in the shape of the example, with a comment on any non-obvious choice. Quote clock times (`"08:00"`).
2. Run `uv run scripts/bootstrap.py validate` and fix every problem it prints.
3. Run `uv run scripts/bootstrap.py render`.
4. Fill the "Standing context" section of `OWNER.md` from the interview: who matters and why, what is in flight. Keep one sentence per line.
5. Read the rendered `JARVIS.md` sections 1, 2, 5, and 9 back to yourself and check they describe this owner correctly. If a sentence is wrong, the fix is in the profile or an adapter's `prompt.md`, never in `JARVIS.md`.

## 4. A source with no adapter

When the owner wants a connected server no adapter covers:

1. Load the server's tool schemas with ToolSearch (`+<server prefix>`) and read each description.
2. Sort every tool into read (`tools`) or anything that writes, sends, creates, edits, deletes, reacts, or schedules (`forbidden`). When unsure, it is forbidden.
3. Pick the citable id: the stable native id an item carries in a read payload, written as `<source>:<part>...`. Every part must appear verbatim in the payload or call input. Prefer the id the source itself uses in URLs.
4. Copy `adapters/sources/_template/` to `adapters/sources/<name>/`, fill `adapter.yaml`, and write `prompt.md`: what to read in its rawest form, how far back, what makes an item a candidate. First line is the source title and its main read.
5. `uv run scripts/bootstrap.py validate`, add it to the profile, render.

`docs/writing-an-adapter.md` has the full field reference.
A source reached through a command-line reader instead of MCP (like `scripts/gh_read.py`) needs a new script under `scripts/`, which this mode cannot write: note it as a follow-up for a `dev` session.

## 5. Hand over

Finish with a short checklist for the owner, in this order, and stop:

1. Review what changed: `git status` and `git diff`.
2. Pin the guard over the generated config: `! uv run scripts/guard_pin.py pin`.
3. Check everything, including the live hook canary: `! uv run scripts/doctor.py --canary`.
4. Do a first run by hand and read the report: `! scripts/run.sh morning` (the first run reads 30 days and is slow).
5. When the report is right, install the schedule: `! uv run scripts/scheduler.py install`.
6. Commit the profile, `OWNER.md`, and the generated files.

The first two weeks run in curation mode: tell the owner about `scripts/jarvis curate` and that `curation: auto` in the profile ends it.
