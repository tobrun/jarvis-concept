"""Index: memory files chunked with their citations, claims parsed, and a Postgres rebuild/search/purge."""

from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import types
from collections.abc import Iterator, Sequence
from pathlib import Path

import index
import pytest
from conftest import run_script

PG_BIN = Path("/opt/homebrew/opt/postgresql@17/bin")

CLAIM = """---
id: c-2026-10-22-billing-owner
statement: Ana owns the billing service migration
kind: fact
salience: 0.7
confidence: 0.9
status: active
supersedes: c-2026-09-01-billing-owner
evidence:
  - raw_id: slack:C1:1729.0001
    paraphrase: Ana said she is taking over the billing service migration
  - raw_id: jira:PLAT-42
    paraphrase: PLAT-42 assigned to Ana
---

Ana owns the billing service migration.
"""


class FakeEmbedder:
    """A deterministic 384-dim hashed bag of words, recording every text it embeds."""

    def __init__(self) -> None:
        self.embedded: list[str] = []

    def __call__(self, texts: Sequence[str]) -> list[list[float]]:
        self.embedded.extend(texts)
        vectors = []
        for text in texts:
            vector = [0.0] * 384
            for word in re.findall(r"[a-z]+", text.lower()):
                vector[int(hashlib.sha256(word.encode()).hexdigest(), 16) % 384] += 1.0
            norm = math.sqrt(sum(x * x for x in vector)) or 1.0
            vectors.append([x / norm for x in vector])
        return vectors


def write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


# Unit


def test_claim_file_becomes_a_claim_row_with_statement_status_and_evidence():
    row = index.parse_claim(CLAIM, "fallback")

    assert row == {
        "id": "c-2026-10-22-billing-owner",
        "statement": "Ana owns the billing service migration",
        "confidence": 0.9,
        "status": "active",
        "supersedes": "c-2026-09-01-billing-owner",
        "evidence_json": [
            {
                "raw_id": "slack:C1:1729.0001",
                "paraphrase": "Ana said she is taking over the billing service migration",
            },
            {"raw_id": "jira:PLAT-42", "paraphrase": "PLAT-42 assigned to Ana"},
        ],
    }


def test_claim_file_chunks_carry_the_claim_id_and_its_evidence_raw_ids():
    chunks = index.file_chunks("memory/claims/c-2026-10-22-billing-owner.md", CLAIM)

    assert chunks[0].claim_ids == ["c-2026-10-22-billing-owner"]
    assert chunks[0].raw_ids == ["slack:C1:1729.0001", "jira:PLAT-42"]
    assert chunks[0].privacy_class == "derived"


def test_long_daily_note_is_chunked_under_the_cap_with_uri_and_cited_raw_ids():
    paragraphs = [
        f"Paragraph {n}: the roads team discussed tile pipeline item {n} at length, "
        f"with follow ups for review and planning across several weeks. [slack:C1:17{n:02d}.0001]"
        for n in range(40)
    ]
    paragraphs.append("A single overlong paragraph " + "word " * 400 + "[jira:PLAT-7] closes it.")
    note = "# 2026-10-26\n\n" + "\n\n".join(paragraphs) + "\n"

    chunks = index.file_chunks("memory/daily/2026-10-26.md", note)

    assert len(chunks) > 3
    assert all(len(chunk.text) <= 1200 for chunk in chunks)
    assert {chunk.uri for chunk in chunks} == {"memory/daily/2026-10-26.md"}
    assert [chunk.chunk_index for chunk in chunks] == list(range(len(chunks)))
    third = next(chunk for chunk in chunks if "Paragraph 3:" in chunk.text)
    assert "slack:C1:1703.0001" in third.raw_ids
    assert "slack:C1:1739.0001" not in third.raw_ids
    last = chunks[-1]
    assert last.raw_ids == ["jira:PLAT-7"]
    cited = {raw for chunk in chunks for raw in chunk.raw_ids}
    assert len(cited) == 41


def test_raw_ids_are_read_from_brackets_and_evidence_fields_but_not_claims_or_links():
    text = (
        "Shipped [slack:C1:1729.0001, gmail:18a2b] see [claim:c-1] and [docs](https://x.y) [x]\n"
        "evidence:\n  - raw_id: calendar:evt_9\n"
        "raw_ids: [github:example-org/repo#12, 'jira:PLAT-3']\n"
    )

    assert index.raw_ids_in(text) == [
        "slack:C1:1729.0001",
        "gmail:18a2b",
        "calendar:evt_9",
        "github:example-org/repo#12",
        "jira:PLAT-3",
    ]
    assert index.claim_ids_in(text) == ["c-1"]


def test_opinions_page_is_classed_as_own_words():
    chunks = index.file_chunks("memory/bank/opinions.md", "Evals are not optional. [claim:c-9]")

    assert chunks[0].privacy_class == "own_words"
    assert chunks[0].claim_ids == ["c-9"]


def test_files_under_raw_and_dreams_are_never_chunked(tmp_path):
    write(tmp_path, "raw/slack/2026-10-26.jsonl", '{"raw_id": "slack:C1:1", "payload": "secret words"}\n')
    write(tmp_path, "raw/notes.md", "secret raw words\n")
    write(tmp_path, "memory/dreams/2026-10-26.md", "a dream\n")
    write(tmp_path, "memory/daily/2026-10-26.md", "Daily entry [slack:C1:1]\n")
    write(tmp_path, "memory/loops/open.yaml", "- id: loop-1\n  raw_id: jira:PLAT-1\n")
    (tmp_path / "memory" / "raw_link.md").symlink_to(tmp_path / "raw" / "notes.md")

    chunks, _ = index.collect(tmp_path)

    assert sorted({chunk.uri for chunk in chunks}) == ["memory/daily/2026-10-26.md", "memory/loops/open.yaml"]
    assert not any("secret" in chunk.text for chunk in chunks)
    assert index.is_indexable("raw/slack/2026-10-26.md") is False


def test_words_longer_than_the_cap_are_cut_and_the_rest_packed_by_words():
    text = "short intro\n\n" + "abcdefghij" * 3 + " tail words here"

    assert index.chunk_text(text, cap=12) == [
        "short intro",
        "abcdefghijab",
        "cdefghijabcd",
        "efghij tail",
        "words here",
    ]


def test_claim_without_frontmatter_falls_back_to_the_file_stem_with_empty_fields():
    assert index.parse_claim("Just a body, no frontmatter.\n", "c-stem") == {
        "id": "c-stem",
        "statement": None,
        "confidence": None,
        "status": None,
        "supersedes": None,
        "evidence_json": [],
    }


def test_claim_with_odd_frontmatter_keeps_what_parses():
    text = "---\nconfidence: high\nsupersedes: ''\nevidence: slack:C1:1\n---\nbody\n"

    row = index.parse_claim(text, "c-stem")

    assert row["confidence"] is None
    assert row["supersedes"] is None
    assert row["evidence_json"] == ["slack:C1:1"]


def test_frontmatter_needs_a_closing_fence_and_a_mapping():
    assert index.split_frontmatter("---\nid: c-1\nno closing fence\n") == (
        {},
        "---\nid: c-1\nno closing fence\n",
    )
    assert index.split_frontmatter("---\n- a list\n---\nbody\n") == ({}, "body\n")
    assert index.split_frontmatter("---\n\n---\nbody\n") == ({}, "body\n")


def test_a_repo_without_memory_collects_nothing(tmp_path):
    assert index.collect(tmp_path) == ([], [])


def test_database_url_prefers_the_environment_then_config_db_env(tmp_path, monkeypatch):
    monkeypatch.delenv("JARVIS_DB_URL", raising=False)
    write(tmp_path, "config/db.env", "# local index\nOTHER=x\nJARVIS_DB_URL = 'postgresql:///jarvis'\n")

    assert index.database_url(tmp_path) == "postgresql:///jarvis"
    monkeypatch.setenv("JARVIS_DB_URL", "postgresql:///from-env")
    assert index.database_url(tmp_path) == "postgresql:///from-env"


def test_database_url_without_a_setting_names_the_missing_file(tmp_path, monkeypatch):
    monkeypatch.delenv("JARVIS_DB_URL", raising=False)

    with pytest.raises(index.DatabaseUnavailable, match=r"config/db\.env does not define it"):
        index.database_url(tmp_path)
    write(tmp_path, "config/db.env", "OTHER=x\n")
    with pytest.raises(index.DatabaseUnavailable, match=r"config/db\.env does not define it"):
        index.database_url(tmp_path)


def test_fast_embedder_loads_its_model_once_and_only_when_there_is_text(monkeypatch):
    loaded: list[str] = []

    class TextEmbedding:
        def __init__(self, name: str) -> None:
            loaded.append(name)

        def embed(self, texts: list[str]) -> Iterator[list[int]]:
            return iter([[len(text), 1] for text in texts])

    fake = types.ModuleType("fastembed")
    fake.TextEmbedding = TextEmbedding  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "fastembed", fake)
    embedder = index.FastEmbedder()

    assert embedder([]) == []
    assert loaded == []
    assert embedder(["abc", "de"]) == [[3.0, 1.0], [2.0, 1.0]]
    assert embedder(["f"]) == [[1.0, 1.0]]
    assert loaded == [index.MODEL]


# Integration


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture(scope="module")
def pg_cluster(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, str]]:
    """A throwaway Postgres 17 cluster on a unix socket only, stopped and removed at teardown."""
    if not (PG_BIN / "pg_ctl").exists():
        pytest.skip("Homebrew postgresql@17 is not installed")
    data = tmp_path_factory.mktemp("pgdata")
    socket_dir = tempfile.mkdtemp(prefix="pg", dir="/tmp")
    port = str(_free_port())
    subprocess.run(
        [PG_BIN / "initdb", "-D", data, "-A", "trust", "-U", "postgres", "--no-sync"],
        check=True,
        capture_output=True,
    )
    options = f"-k {socket_dir} -p {port} -c listen_addresses='' -c fsync=off"
    subprocess.run(
        [PG_BIN / "pg_ctl", "-D", data, "-o", options, "-l", data / "log", "-w", "start"],
        check=True,
        capture_output=True,
    )
    try:
        yield {"host": socket_dir, "port": port}
    finally:
        subprocess.run([PG_BIN / "pg_ctl", "-D", data, "-m", "immediate", "stop"], capture_output=True)
        shutil.rmtree(socket_dir, ignore_errors=True)


@pytest.fixture
def db_url(
    pg_cluster: dict[str, str], request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> str:
    """A fresh database in the throwaway cluster, exported as JARVIS_DB_URL."""
    name = "t_" + re.sub(r"\W", "_", request.node.name)[:50].lower()
    host, port = pg_cluster["host"], pg_cluster["port"]
    subprocess.run(
        [PG_BIN / "createdb", "-h", host, "-p", port, "-U", "postgres", name], check=True, capture_output=True
    )
    url = f"postgresql://postgres@/{name}?host={host}&port={port}"
    monkeypatch.setenv("JARVIS_DB_URL", url)
    monkeypatch.setenv("JARVIS_INDEX_BACKEND", "postgres")
    return url


@pytest.fixture(params=["sqlite", pytest.param("postgres", marks=pytest.mark.postgres)])
def backend(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> str:
    """Each retrieval behavior runs against both index backends; postgres needs the throwaway cluster."""
    monkeypatch.setenv("JARVIS_INDEX_BACKEND", request.param)
    if request.param == "postgres":
        request.getfixturevalue("db_url")
    return request.param


def seed_memory(root: Path) -> None:
    write(root, "memory/claims/c-2026-10-22-billing-owner.md", CLAIM)
    write(
        root,
        "memory/claims/c-2026-10-20-budget.md",
        "---\nid: c-2026-10-20-budget\nstatement: The quarterly budget review moved to Thursday\n"
        "confidence: 0.8\nstatus: active\nevidence:\n  - raw_id: calendar:evt_77\n"
        "    paraphrase: budget review rescheduled\n---\n\nThe quarterly budget review moved to Thursday.\n",
    )
    write(
        root,
        "memory/daily/2026-10-26.md",
        "# 2026-10-26\n\nLunch plans with the design group. [slack:C9:1800.0001]\n\n"
        "Hiring loop feedback is due Friday. [gmail:abc123]\n",
    )


def run_index(*args: str, embed: FakeEmbedder, capsys: pytest.CaptureFixture[str]) -> object:
    assert index.main(list(args), embed=embed) == 0
    return json.loads(capsys.readouterr().out)


def test_search_for_a_claims_words_returns_that_claim_first(repo, backend, capsys):
    seed_memory(repo)
    embed = FakeEmbedder()

    run_index("rebuild", embed=embed, capsys=capsys)
    results = run_index("search", "who owns the billing service migration", embed=embed, capsys=capsys)

    assert results[0]["claim_ids"] == ["c-2026-10-22-billing-owner"]
    assert results[0]["uri"] == "memory/claims/c-2026-10-22-billing-owner.md"
    assert results[0]["raw_ids"] == ["slack:C1:1729.0001", "jira:PLAT-42"]
    assert len(results) == 3
    assert results[0]["score"] > results[1]["score"]


def test_rebuild_twice_without_file_changes_embeds_nothing_new(repo, backend, capsys):
    seed_memory(repo)
    embed = FakeEmbedder()

    first = run_index("rebuild", embed=embed, capsys=capsys)
    embedded_after_first = len(embed.embedded)
    second = run_index("rebuild", embed=embed, capsys=capsys)

    assert first == {"chunks": 3, "claims": 2, "embedded": 3, "removed": 0}
    assert second == {"chunks": 3, "claims": 2, "embedded": 0, "removed": 0}
    assert len(embed.embedded) == embedded_after_first == 3


def test_rebuild_reembeds_only_the_changed_file_and_drops_deleted_ones(repo, backend, capsys):
    seed_memory(repo)
    embed = FakeEmbedder()
    run_index("rebuild", embed=embed, capsys=capsys)

    write(
        repo, "memory/daily/2026-10-26.md", "# 2026-10-26\n\nOnly the hiring loop remains. [gmail:abc123]\n"
    )
    (repo / "memory/claims/c-2026-10-20-budget.md").unlink()
    embed.embedded.clear()
    stats = run_index("rebuild", embed=embed, capsys=capsys)
    results = run_index("search", "quarterly budget review", "--limit", "10", embed=embed, capsys=capsys)

    assert stats == {"chunks": 2, "claims": 1, "embedded": 1, "removed": 2}
    assert embed.embedded[:1] == ["# 2026-10-26\n\nOnly the hiring loop remains. [gmail:abc123]"]
    assert {result["uri"] for result in results} == {
        "memory/claims/c-2026-10-22-billing-owner.md",
        "memory/daily/2026-10-26.md",
    }


def test_purge_removes_the_chunks_citing_a_raw_id(repo, backend, capsys):
    seed_memory(repo)
    embed = FakeEmbedder()
    run_index("rebuild", embed=embed, capsys=capsys)

    purged = run_index("purge", "slack:C1:1729.0001", embed=embed, capsys=capsys)
    results = run_index("search", "roads tile pipeline", "--limit", "10", embed=embed, capsys=capsys)

    assert purged == {"purged": 1, "raw_id": "slack:C1:1729.0001"}
    assert len(results) == 2
    assert all("slack:C1:1729.0001" not in result["raw_ids"] for result in results)


def test_sqlite_index_lives_in_state_and_is_rebuilt_without_any_server(repo, capsys, monkeypatch):
    monkeypatch.delenv("JARVIS_INDEX_BACKEND", raising=False)
    seed_memory(repo)
    stats = run_index("rebuild", embed=FakeEmbedder(), capsys=capsys)
    assert stats == {"chunks": 3, "claims": 2, "embedded": 3, "removed": 0}
    assert (repo / "state" / "index.sqlite").is_file()


def test_unreachable_database_exits_1_with_the_connection_error_and_no_traceback(repo):
    seed_memory(repo)
    url = "postgresql:///jarvis?host=/tmp/jarvis-no-such-socket-dir&port=1"

    result = run_script(
        repo, "index.py", "rebuild", env={"JARVIS_DB_URL": url, "JARVIS_INDEX_BACKEND": "postgres"}
    )

    assert result.returncode == 1
    assert result.stdout == ""
    lines = result.stderr.strip().splitlines()
    assert len(lines) == 1
    assert lines[0].startswith(f"index: cannot connect to {url}: ")
    assert "jarvis-no-such-socket-dir" in lines[0]
    assert "Traceback" not in result.stderr


def test_embedder_returning_too_few_vectors_fails_the_rebuild_and_indexes_nothing(repo, backend, capsys):
    seed_memory(repo)

    with pytest.raises(ValueError, match="embedder returned 0 vectors for 3 chunks"):
        index.main(["rebuild"], embed=lambda texts: [])
    results = run_index("search", "roads", embed=FakeEmbedder(), capsys=capsys)

    assert results == []


@pytest.mark.postgres
def test_database_error_exits_1_with_its_first_line_and_no_traceback(repo, db_url, capsys):
    import psycopg

    seed_memory(repo)
    with psycopg.connect(db_url, autocommit=True) as conn:
        conn.execute("CREATE TABLE chunks (id int)")

    assert index.main(["rebuild"], embed=FakeEmbedder()) == 1
    captured = capsys.readouterr()

    assert captured.out == ""
    assert captured.err == 'index: database error: column "raw_ids" does not exist\n'
