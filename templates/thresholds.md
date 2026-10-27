# Thresholds

Surfacing threshold and staleness settings, read by `scripts/loops.py` and `scripts/source_coverage.py`.
Lines of the form `key: value` are read; everything else is prose.
`scripts/bootstrap.py render` writes this file once and then only adds the staleness key of a newly enabled source.
For the first two weeks the surfacing threshold is hand-set (JARVIS.md section 8).

## Surfacing

surface_threshold: 0.6

## Staleness

Loops waiting on someone count business days.
Source-specific loop kinds (`stale_<kind>_days`) count calendar days.

stale_waiting_business_days: 2
snooze_default_business_days: 3

## Coverage

A source is degraded when read/available falls below this fraction of its trailing median.

coverage_degraded_fraction: 0.5
coverage_history_runs: 10
coverage_min_history: 3
