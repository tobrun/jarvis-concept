# Contracts

## Harness

C-guard-default-deny: a tool call not matched by the current mode's allowlist is denied, and an unset mode denies every call.
  guaranteed by: `guard.py` decide
  relied on by: every hard rule in JARVIS.md section 3
  (2026-10-26, harness spec)

C-guard-fail-closed: any guard failure the wrapper can observe blocks the tool call.
  guaranteed by: `guard.sh`
  relied on by: `run.py` preflight, all modes
  (2026-10-26, harness spec)

C-raw-append-only: no script rewrites a raw line except `raw.py tombstone` and `raw.py forget`, and capture applies the tombstone registry before writing.
  guaranteed by: `capture.py`, `raw.py`; no mode's write scope includes `raw/`
  relied on by: replay, eval, claim verification, report gate
  (2026-10-26, harness spec)

C-clock: every script reads time through `common.now()`, which honors `JARVIS_NOW`.
  guaranteed by: `common.now`
  relied on by: replay, schedule, loops, metrics
  (2026-10-26, harness spec)

## Kit

C-render-deterministic: the same profile, adapters, templates, and date render byte-identical files, and a render of an unchanged setup writes nothing.
  guaranteed by: `bootstrap.py` outputs, changed
  relied on by: the guard pin, `bootstrap.py diff`, `doctor.py`
  (2026-10-27, kit plan)

C-generated-files: `JARVIS.md`, `config/jarvis.yaml`, `config/sources.yaml`, `config/allowed_tools.yaml`, `config/holidays.md`, and `config/scheduler/*` are written only by render; `OWNER.md`, `config/thresholds.md`, and `config/reactions.md` are created once and then owned by the owner.
  guaranteed by: `bootstrap.py` write; every mode but `dev` has them outside its write scope
  relied on by: re-rendering after a profile change without losing hand edits
  (2026-10-27, kit plan)

C-capture-equals-allow: every tool the capture map captures is in the allowlist's source group, and the reverse, because both are generated from the same adapters.
  guaranteed by: `bootstrap.py` sources_config, allowlist
  relied on by: the raw layer's completeness and the guard's default deny
  (2026-10-27, kit plan)

C-delivery-target: a delivery call is allowed only with the adapter's keys and exactly the rendered target.
  guaranteed by: `guard.py` check_post, `guard_pin.py` self-test
  relied on by: the hard rule that the report is the only outbound write
  (2026-10-27, kit plan)
