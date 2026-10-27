# Concepts

Jarvis is a personal assistant that watches the systems you work in, keeps track of what you owe and what is owed to you, and tells you a few times a day what needs you.
It never acts for you: it reads, records, ranks, drafts, and reports, and you decide.
This page is the why behind its shape, independent of which systems you connect.

## The model judges; the harness enforces, records, and measures

A language model is good at judgment: is this message an ask, is this thread stale, does this matter to a goal.
It is bad at being trusted with rules, at date arithmetic, and at remembering.
So Jarvis splits the work.

The prompt (`JARVIS.md`) holds the judgment: how to read, what counts as a candidate, how to rank, what a good report looks like.
The harness (`scripts/`) holds everything mechanical: what tools may run, what was read, which day is a business day, which runs happened, whether the report is well formed, how good the last week was.
Every rule the prompt calls a hard rule is backed by code, so the prompt can say "enforcement does not rely on you" and mean it.

## Read everything, act on nothing

Jarvis reads every source it is connected to and writes nowhere outside its own repository, with exactly one exception: the report, delivered to you through the delivery adapter you chose.
No message under your name, no calendar change, no ticket transition, no reaction.
Replies, comments, and nudges are written as drafts in `drafts/` for you to send yourself.

The point is trust.
An assistant that may act has to be supervised; one that only observes can be left running.

## Two layers of enforcement, and proof that they are on

Every tool call passes a guard (`scripts/guard.py`) as a Claude Code PreToolUse hook.
The guard holds a per-mode allowlist: exact tool names, the paths each mode may write, the shell commands it may run, and the one delivery tool with exactly one allowed target.
Anything not listed is denied, and a session started without a mode can do nothing at all.

The same allowlist is turned into Claude Code's own permission rules for every scheduled session, so a call outside them is refused even if the hook somehow did not load.
Before each scheduled run, the harness checks the guard's files against a pinned hash, self-tests the guard, and runs a tiny canary session that tries a forbidden call and must see it denied.
If any of that fails, the run does not start, and the next report says why.

Changing what Jarvis may do is therefore always a visible, deliberate act: edit the profile, render, read the diff, pin.

## Raw before judgment

Everything a source returns is written verbatim to `raw/<source>/<date>.jsonl` by a capture hook, before the model forms any opinion about it.
Every claim in memory and every line in a report cites a raw id, and the report gate refuses a report that cites an id the raw store cannot resolve.
So nothing is ever "the model said so": every statement can be traced to the record it came from, and every run can be replayed from the same input.

Raw is append-only and stays on your machine, outside git.
HR and personal items are tombstoned: only an id, a hash, and a reason remain.
Forgetting is a workflow that removes an item and everything derived from it.

## Open loops

The core object is the open loop: something with a pending obligation.
Someone asked you for something, you promised something, you are waiting on someone, a decision is pending, a date is coming.
Loops are files in `memory/loops/`, detected from the words in messages, mail, tickets, and meeting records.
A loop that goes too long without movement is slipping, and a slipping loop appears in every report until it moves, is resolved, or you snooze it.

Staleness is mechanical: business days come from your working days and your holiday calendar, and each source can add its own kind of loop with its own threshold.

## Bias toward silence

Every candidate gets a score: the probability you act on it within five business days if shown it.
Only items above a threshold reach the report; everything else is logged with its score and reason, and a weekly digest shows you what was held back.
The first two weeks are a curation period in which you label what you would and would not have wanted to see.

## Measured against what happened

Your opinion of the report is a secondary signal.
The primary one is what actually happened afterwards.
The headline metric is bump recall: when someone had to chase you, had Jarvis surfaced it first?
Late replies, missed dates, stale items, and meeting topics the prep missed are misses; items you acted on are hits; items you ignored are noise.
These outcomes are detected from raw every evening and become weekly metrics, including a calibration score for the probabilities.

Once a real week has passed, it is frozen as the golden week.
From then on, a commit that changes the prompt, your owner file, the thresholds, or the scripts needs a passing replay of that week, offline, with no regression in bump recall or misses.
A prompt change is an experiment with a result, not a hunch.

## Memory lives in files

A scheduled run starts with a blank context.
Everything Jarvis knows is in the repository: daily notes, verified claims with their evidence, people pages, an inferred goal model you can correct by editing it, the loops, and a short synthesis of how to work with you.
An hourly job consolidates notes into claims and re-verifies a sample of old claims against their sources; a nightly job reflects on the day and updates the synthesis.
A local index over the memory files makes recall fast; the files stay the source of truth.

## What this kit adds

The first Jarvis was built for one person and one set of systems.
This kit separates the concepts above, which hold for anyone, from the specifics, which do not:

- The profile (`profile/jarvis.profile.yaml`) holds who you are, your hours, your sources and their parameters, where the report goes, and how the machine runs.
- Source adapters (`adapters/sources/`) describe one system each: which tools read it, which must never be called, how its items are cited, how to read it, and what kinds of loops it adds.
- Delivery adapters (`adapters/delivery/`) describe the one outbound channel: a Slack self-DM, an email to yourself, or no send at all.
- The renderer (`scripts/bootstrap.py`) turns the profile and adapters into the prompt, the allowlist, the capture map, the holiday calendar, and the scheduler units.
- The bootstrap interview (`scripts/jarvis bootstrap`) discovers your connectors, asks what it needs, writes the profile, and hands you the pin and the first run.

Your Jarvis is a fork of this kit with your profile and the rendered files committed.
