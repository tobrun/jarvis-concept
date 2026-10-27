from claims import sample
from conftest import run_script


def claim(number, status="active"):
    return {"id": f"c{number:02d}", "statement": f"claim {number}", "status": status}


def test_sample_of_twenty_active_claims_is_two_ids_and_the_same_two_for_the_same_run_id():
    claims = [claim(n) for n in range(1, 21)]

    first = sample(claims, 10, "morning-2026-10-26")
    again = sample(list(reversed(claims)), 10, "morning-2026-10-26")

    assert len(first) == 2
    assert len(set(first)) == 2
    assert again == first


def test_superseded_claims_are_never_sampled():
    claims = [claim(1)] + [claim(n, "superseded") for n in range(2, 21)]
    for run in range(30):
        assert sample(claims, 50, f"run-{run}") == ["c01"]


def test_no_active_claims_samples_nothing():
    assert sample([claim(1, "retracted")], 10, "manual") == []


def test_sample_cli_reads_claim_files_and_is_stable_per_run_id(repo):
    folder = repo / "memory" / "claims"
    folder.mkdir(parents=True)
    for n in range(1, 21):
        status = "active" if n <= 10 else "superseded"
        folder.joinpath(f"c{n:02d}.md").write_text(
            f"---\nid: c{n:02d}\nstatement: s\nstatus: {status}\n---\nbody\n"
        )
    env = {"JARVIS_RUN_ID": "eod-2026-10-26"}

    first = run_script(repo, "claims.py", "sample", env=env).stdout.split()
    second = run_script(repo, "claims.py", "sample", env=env).stdout.split()

    assert len(first) == 1
    assert first[0] in {f"c{n:02d}" for n in range(1, 11)}
    assert second == first
