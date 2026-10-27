GitHub: pull requests where {{principal}} is author, reviewer, or requested reviewer, plus repos {{repos}}, with review and commit timestamps, not just PR state.
Read GitHub only through `uv run scripts/gh_read.py <api path>`, which makes GET requests and nothing else.
Loops for a pull request awaiting review use kind `pr_review`.
