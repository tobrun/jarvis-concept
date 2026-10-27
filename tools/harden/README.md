# Gauntlet tools

Repo-fitted tools the ship gauntlet reuses on every run.

- `coveragerc` - coverage config that measures `scripts/` in subprocesses and in temp repo copies:
  `COVERAGE_FILE=<dir>/.coverage uv run --with coverage==7.10.7 coverage run --rcfile=tools/harden/coveragerc -m pytest`,
  then `coverage combine <dir>` and `coverage json -o coverage.json` with the same rcfile.
- `complexity.py` - coverage-weighted complexity per function:
  `uvx --from radon==6.0.1 radon cc -j scripts > radon.json`, then `python3 tools/harden/complexity.py radon.json coverage.json`.
- Dead code: `[tool.vulture]` in pyproject.toml lists the framework-used names;
  run `uvx --from vulture==2.14 vulture`.
- `flaky.sh` - runs the given tests 5 times in random order with pytest-randomly; any disagreement is a flaky test.

Security: `gitleaks git`, `osv-scanner scan --lockfile uv.lock`, `semgrep scan --config p/python --config p/secrets`.
Duplication: `npx jscpd@4.0.5 --min-tokens 50 --format python,shell scripts tests`.
