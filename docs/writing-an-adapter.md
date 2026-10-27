# Writing an adapter

A source adapter tells Jarvis how to read one system; a delivery adapter tells it how to send you the report.
Both are data: an `adapter.yaml` and a `prompt.md`.
Neither can add code to the guard's path, so adding one never weakens enforcement.
The bootstrap interview can write a source adapter for you; this page is the reference.

## Source adapter

Copy `adapters/sources/_template/` to `adapters/sources/<name>/`.
`<name>` is lowercase with hyphens.

```yaml
name: linear                      # must equal the directory name
title: Linear
description: Issues and projects the principal owns or follows.
order: 25                         # reading order; calendar 10, tickets 20, chat 30, mail 40, code 50
source: linear                    # raw source name (lowercase, digits, underscore); defaults to name
id: "linear:<issue>"              # the citable id form
match: parts                      # parts | github
capture: mcp                      # mcp | bash | read
servers:
  linear:                         # a role name, used by the profile to name the real server
    candidates: [claude_ai_Linear, linear]
    tools: [list_issues, get_issue, list_comments]
    forbidden: [create_issue, update_issue, create_comment]
staleness:
  linear_in_progress:
    days: 5
    description: a Linear issue in progress with no update
params:
  teams:
    type: list
    description: Linear team keys whose whole board matters.
    example: [ENG]
```

### The id

Every item Jarvis cites is `<source>:<parts>`, and every part must appear verbatim in the captured payload or the call input, so `scripts/raw.py resolve` can find it.
Use the id the system itself uses in links.
When one id is not unique on its own, use more parts with literal separators: Slack timestamps are unique only per channel, so Slack is `slack:<channel>:<ts>`.
Every part but the last is matched up to the next separator; the last takes the rest.

`match: parts` finds an item in a line by its parts as whole tokens.
`match: github` is a stricter matcher for issues and pull requests named by repo and number together.
A new matching strategy is a change to `scripts/raw.py` with tests, because the report gate depends on it.

### Tools

Tool names are `mcp__<server>__<tool>`.
List only the tool part; the server part comes from `candidates`, or from the profile when the owner's server loads under another name.
`uv run scripts/doctor.py tools` prints every server a headless session sees, with its tools.

`tools` are the read tools Jarvis may call, and only these are captured to raw.
`forbidden` lists every tool on the server that writes, sends, creates, edits, deletes, reacts, schedules, or shares.
The guard denies anything not in `tools` anyway; `forbidden` documents the boundary, feeds the tests, and lets `doctor` notice a tool it does not know.
When unsure, a tool is forbidden.

### Capture kinds

- `mcp`: the tools above.
- `bash`: a command-line reader, `bash: [uv, run, scripts/<reader>.py]`. The reader is a script under `scripts/` that can only read (see `scripts/gh_read.py`, GET only, fixed host, checked paths). It is allowed in every mode that reads sources. Writing one is a `dev` session change.
- `read`: files under `folder:` in the repo, read with the Read tool.

### Staleness

Each kind becomes a loop `kind` the prompt tells the model to use, and a `stale_<kind>_days` key render appends to `config/thresholds.md`.
A loop of that kind slips after that many calendar days without movement.

### prompt.md

The reading policy for the source, inserted into section 5 of `JARVIS.md` in reading order.
The first line is the source title and its main read, ending with a period; render appends "Cite as `<id>`."
Further lines are indented under it.
Use `{{principal}}` for the owner's name, `{{their}}` and `{{them}}` for pronouns, and `{{<param>}}` for a parameter (lists render as code spans).

Say what to read in its rawest form (history and changelogs, not summaries), how far back, which items are candidates, and which are only skimmed.

### Check it

```sh
uv run scripts/bootstrap.py validate
uv run scripts/bootstrap.py render
uv run pytest tests/test_bootstrap.py
```

Then add it to the profile, render, review the diff, and pin.

## Delivery adapter

```yaml
name: slack-self-dm
title: Slack self-DM
server:
  candidates: [claude_ai_Slack, plugin_enterprise-search_slack]
  tool: slack_send_message
keys: [channel_id, message, thread_ts]   # every other parameter is denied
target_key: channel_id                   # must equal the profile's delivery.target exactly
target_shape: scalar                     # scalar | list (a list must be exactly [target])
message_key: message                     # the text the report linter checks
thread_key: thread_ts                    # set means a reply; replies need only resolvable raw ids
reactions: true                          # whether item replies can carry snooze reactions
target:
  description: Your own Slack user id.
  default: principal_email               # optional: default to the owner's first email
place: "{{principal}}'s Slack self-DM"   # how the prompt names the destination
```

`prompt.md` explains to the model how to deliver and how to record what it sent with `uv run scripts/append.py report_messages`.
An adapter with `server: null` sends nothing; the runner checks the report file instead (`file-only`).

Choose a target that can only ever be the owner.
A channel id that other people read is not a self-DM, and a mail adapter must not allow `cc` or `bcc`.
