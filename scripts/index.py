"""Retrieval index over memory files: chunks, 384-dim embeddings, and claims, in SQLite or Postgres.

The files under memory/ are the source of truth; this index is a rebuildable retrieval layer.
Only memory files are indexed (never raw/, never memory/dreams/), so others' verbatim words stay in raw/.

Usage:
    uv run scripts/index.py rebuild
    uv run scripts/index.py search <text> [--limit 5]
    uv run scripts/index.py purge <raw_id>

The backend is JARVIS_INDEX_BACKEND, else `index.backend` in config/jarvis.yaml, else sqlite.
sqlite keeps the index in state/index.sqlite (scripts/index_sqlite.py); postgres connects to JARVIS_DB_URL,
else config/db.env, and needs pgvector.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import common
import yaml
from common import repo_root

MODEL = "BAAI/bge-small-en-v1.5"
DIMENSIONS = 384
CHUNK_CAP = 1200
OWN_WORDS_URIS = {"memory/bank/opinions.md"}
EXCLUDED_DIRS = ("memory/dreams/",)
SUFFIXES = {".md", ".yaml", ".yml"}

Embedder = Callable[[Sequence[str]], list[list[float]]]

BRACKET = re.compile(r"\[([^\[\]\n]+)\]")
ID_TOKEN = re.compile(r"^([a-z][a-z0-9_-]*):(\S+)$")
NOT_SOURCES = {"claim", "http", "https", "mailto", "file"}
RAW_ID_FIELD = re.compile(r"\braw_id\s*:\s*[\"']?([a-z][a-z0-9_-]*:[^\s\"',\]\}]+)")
CLAIM_REF = re.compile(r"\bclaim:([A-Za-z0-9][A-Za-z0-9_.-]*[A-Za-z0-9])")

SCHEMA = f"""
CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE IF NOT EXISTS chunks (
    id bigserial PRIMARY KEY,
    uri text NOT NULL,
    chunk_index int NOT NULL,
    text text NOT NULL,
    content_sha256 text NOT NULL,
    raw_ids text[] NOT NULL DEFAULT '{{}}',
    claim_ids text[] NOT NULL DEFAULT '{{}}',
    privacy_class text NOT NULL,
    model text NOT NULL,
    UNIQUE (uri, chunk_index, content_sha256)
);
CREATE INDEX IF NOT EXISTS chunks_raw_ids ON chunks USING gin (raw_ids);
CREATE TABLE IF NOT EXISTS embeddings (
    chunk_id bigint PRIMARY KEY REFERENCES chunks (id) ON DELETE CASCADE,
    embedding vector({DIMENSIONS}) NOT NULL
);
CREATE TABLE IF NOT EXISTS claims (
    id text PRIMARY KEY,
    statement text,
    confidence real,
    status text,
    supersedes text,
    evidence_json jsonb NOT NULL DEFAULT '[]'
);
"""


class DatabaseUnavailable(Exception):
    """The index database could not be reached; the message names the connection problem."""


@dataclass(frozen=True)
class Chunk:
    uri: str
    chunk_index: int
    text: str
    content_sha256: str
    raw_ids: list[str]
    claim_ids: list[str]
    privacy_class: str

    @property
    def key(self) -> tuple[str, int, str]:
        return (self.uri, self.chunk_index, self.content_sha256)


# Files and chunking


def is_indexable(uri: str) -> bool:
    """A repo-relative path is indexed only when it is a memory file outside memory/dreams/."""
    if not uri.startswith("memory/") or uri.startswith(EXCLUDED_DIRS):
        return False
    return Path(uri).suffix in SUFFIXES


def memory_files(root: Path) -> list[tuple[str, Path]]:
    """Every indexable file as (uri, path), sorted; only memory/ is walked, so raw/ is never seen."""
    base = root / "memory"
    if not base.is_dir():
        return []
    found = []
    for path in sorted(base.rglob("*")):
        if path.is_file() and not path.is_symlink():
            uri = path.relative_to(root).as_posix()
            if is_indexable(uri):
                found.append((uri, path))
    return found


def _pack(pieces: Iterable[str], cap: int, joiner: str) -> list[str]:
    """Greedily join pieces of at most cap characters into runs of at most cap characters."""
    packed: list[str] = []
    current = ""
    for piece in pieces:
        candidate = f"{current}{joiner}{piece}" if current else piece
        if len(candidate) > cap:
            packed.append(current)
            current = piece
        else:
            current = candidate
    if current:
        packed.append(current)
    return packed


def _split_long(paragraph: str, cap: int) -> list[str]:
    """Pack the words of an oversized paragraph into pieces of at most cap characters; cut longer words."""
    fragments = (
        word[start : start + cap] for word in paragraph.split() for start in range(0, len(word), cap)
    )
    return _pack(fragments, cap, " ")


def chunk_text(text: str, cap: int = CHUNK_CAP) -> list[str]:
    """Split on blank lines and pack whole paragraphs into chunks of at most cap characters."""
    pieces: list[str] = []
    for part in re.split(r"\n\s*\n", text):
        paragraph = part.strip()
        if len(paragraph) > cap:
            pieces.extend(_split_long(paragraph, cap))
        elif paragraph:
            pieces.append(paragraph)
    return _pack(pieces, cap, "\n\n")


def _unique(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(values))


def is_raw_id(token: str) -> bool:
    """A `source:id` token whose source is not a claim or a link scheme."""
    match = ID_TOKEN.match(token)
    return bool(match) and match.group(1) not in NOT_SOURCES and not match.group(2).startswith("//")


def _bracketed_raw_ids(text: str) -> list[tuple[int, str]]:
    """Raw ids inside `[...]` groups (several may share one), each with its group's offset."""
    found: list[tuple[int, str]] = []
    for group in BRACKET.finditer(text):
        tokens = (token.strip("\"'") for token in re.split(r"[,;\s]+", group.group(1)))
        found.extend((group.start(), token) for token in tokens if is_raw_id(token))
    return found


def raw_ids_in(text: str) -> list[str]:
    """Raw ids cited as `[source:id]` (several may share a bracket) or in raw_id fields, in text order."""
    found = _bracketed_raw_ids(text)
    found.extend((match.start(), match.group(1)) for match in RAW_ID_FIELD.finditer(text))
    return _unique(token for _, token in sorted(found, key=lambda item: item[0]))


def claim_ids_in(text: str) -> list[str]:
    return _unique(CLAIM_REF.findall(text))


def split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """YAML frontmatter between leading `---` lines, and the body after it."""
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---", 4)
    if end == -1:
        return {}, text
    meta = yaml.safe_load(text[4:end]) or {}
    body = text[end + 4 :].lstrip("-").lstrip("\n")
    return (meta if isinstance(meta, dict) else {}), body


def _confidence(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_claim(text: str, fallback_id: str) -> dict[str, Any]:
    """A claim file's frontmatter as a claims row; the id falls back to the file stem."""
    meta, _ = split_frontmatter(text)
    evidence = meta.get("evidence") or []
    if not isinstance(evidence, list):
        evidence = [evidence]
    supersedes = meta.get("supersedes")
    return {
        "id": str(meta.get("id") or fallback_id),
        "statement": meta.get("statement"),
        "confidence": _confidence(meta.get("confidence")),
        "status": meta.get("status"),
        "supersedes": None if supersedes in (None, "") else str(supersedes),
        "evidence_json": evidence,
    }


def is_claim_file(uri: str) -> bool:
    return uri.startswith("memory/claims/") and uri.endswith(".md")


def claim_citations(uri: str, text: str) -> tuple[list[str], list[str]]:
    """The raw ids and claim ids a whole file cites: a claim file's evidence raw ids and its own id."""
    if not is_claim_file(uri):
        return [], []
    claim = parse_claim(text, Path(uri).stem)
    raw = [
        str(item["raw_id"])
        for item in claim["evidence_json"]
        if isinstance(item, dict) and item.get("raw_id")
    ]
    return raw, [claim["id"]]


def file_chunks(uri: str, text: str, cap: int = CHUNK_CAP) -> list[Chunk]:
    """Chunks of one memory file; a claim file's chunks all carry its own id and evidence raw ids."""
    privacy = "own_words" if uri in OWN_WORDS_URIS else "derived"
    file_raw, file_claims = claim_citations(uri, text)
    chunks = []
    for index, piece in enumerate(chunk_text(text, cap)):
        chunks.append(
            Chunk(
                uri=uri,
                chunk_index=index,
                text=piece,
                content_sha256=hashlib.sha256(piece.encode()).hexdigest(),
                raw_ids=_unique([*file_raw, *raw_ids_in(piece)]),
                claim_ids=_unique([*file_claims, *claim_ids_in(piece)]),
                privacy_class=privacy,
            )
        )
    return chunks


def collect(root: Path) -> tuple[list[Chunk], list[dict[str, Any]]]:
    """All chunks and claim rows the memory files currently hold."""
    chunks: list[Chunk] = []
    claims: list[dict[str, Any]] = []
    for uri, path in memory_files(root):
        text = path.read_text(errors="replace")
        chunks.extend(file_chunks(uri, text))
        if is_claim_file(uri):
            claims.append(parse_claim(text, path.stem))
    return chunks, claims


# Embedding


class FastEmbedder:
    """The local fastembed model, constructed on first use so commands that embed nothing never load it."""

    def __init__(self, model: str = MODEL) -> None:
        self.model_name = model
        self._model: Any = None

    def __call__(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        if self._model is None:
            from fastembed import TextEmbedding

            self._model = TextEmbedding(self.model_name)
        return [[float(x) for x in vector] for vector in self._model.embed(list(texts))]


def vector_literal(vector: Sequence[float]) -> str:
    if len(vector) != DIMENSIONS:
        raise ValueError(f"embedding has {len(vector)} dimensions, expected {DIMENSIONS}")
    return "[" + ",".join(repr(float(x)) for x in vector) + "]"


# Database


def env_file_value(path: Path, name: str) -> str | None:
    """The value of the first `name=value` line in an env file, unquoted; None when absent."""
    if not path.exists():
        return None
    for line in path.read_text().splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and key.strip() == name:
            return value.strip().strip("\"'")
    return None


def database_url(root: Path | None = None) -> str:
    """JARVIS_DB_URL from the environment, else from config/db.env `KEY=VALUE` lines."""
    url = os.environ.get("JARVIS_DB_URL")
    if url:
        return url
    path = (root or repo_root()) / "config" / "db.env"
    url = env_file_value(path, "JARVIS_DB_URL")
    if url is not None:
        return url
    raise DatabaseUnavailable(f"JARVIS_DB_URL is not set and {path} does not define it")


def first_line(error: Exception, default: str) -> str:
    """The first non-blank line of an error's message, else the default."""
    return next((line.strip() for line in str(error).splitlines() if line.strip()), default)


def connect(url: str) -> Any:
    import psycopg

    try:
        return psycopg.connect(url, connect_timeout=5)
    except psycopg.OperationalError as error:
        raise DatabaseUnavailable(f"cannot connect to {url}: {first_line(error, 'unknown error')}") from None


def ensure_schema(conn: Any) -> None:
    with conn.transaction():
        conn.execute(SCHEMA)


def rebuild(conn: Any, root: Path, embed: Embedder, model: str = MODEL) -> dict[str, int]:
    """Bring the index in line with the memory files; only chunks not already indexed are embedded."""
    ensure_schema(conn)
    chunks, claims = collect(root)
    with conn.transaction():
        embedded, removed = sync_chunks(conn, chunks, embed, model)
        sync_claims(conn, claims)
    return {"chunks": len(chunks), "embedded": embedded, "removed": removed, "claims": len(claims)}


def sync_chunks(conn: Any, chunks: list[Chunk], embed: Embedder, model: str) -> tuple[int, int]:
    """Drop indexed chunks the files no longer hold (or another model embedded), embed and insert the new
    ones; returns (embedded, removed)."""
    rows = conn.execute("SELECT id, uri, chunk_index, content_sha256, model FROM chunks").fetchall()
    stale, new = plan_chunks(rows, chunks, model)
    if stale:
        conn.execute("DELETE FROM chunks WHERE id = ANY(%s)", (stale,))
    vectors = embed([chunk.text for chunk in new]) if new else []
    if len(vectors) != len(new):
        raise ValueError(f"embedder returned {len(vectors)} vectors for {len(new)} chunks")
    for chunk, vector in zip(new, vectors, strict=True):
        insert_chunk(conn, chunk, vector, model)
    return len(new), len(stale)


def plan_chunks(
    rows: Iterable[Sequence[Any]], chunks: list[Chunk], model: str
) -> tuple[list[int], list[Chunk]]:
    """Indexed rows (id, uri, chunk_index, content_sha256, model) whose content or model no longer matches
    are stale; the chunks no remaining row holds are new. Returns (stale ids, new chunks)."""
    current = {(*chunk.key, model) for chunk in chunks}
    stale: list[int] = []
    present: set[tuple[Any, ...]] = set()
    for chunk_id, *key, row_model in rows:
        if (*key, row_model) in current:
            present.add(tuple(key))
        else:
            stale.append(chunk_id)
    return stale, [chunk for chunk in chunks if chunk.key not in present]


def insert_chunk(conn: Any, chunk: Chunk, vector: Sequence[float], model: str) -> None:
    chunk_id = conn.execute(
        "INSERT INTO chunks (uri, chunk_index, text, content_sha256, raw_ids, claim_ids,"
        " privacy_class, model) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
        (
            chunk.uri,
            chunk.chunk_index,
            chunk.text,
            chunk.content_sha256,
            chunk.raw_ids,
            chunk.claim_ids,
            chunk.privacy_class,
            model,
        ),
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO embeddings (chunk_id, embedding) VALUES (%s, %s::vector)",
        (chunk_id, vector_literal(vector)),
    )


def sync_claims(conn: Any, claims: list[dict[str, Any]]) -> None:
    """Make the claims table hold exactly the claim files' rows."""
    claim_ids = [claim["id"] for claim in claims]
    conn.execute("DELETE FROM claims WHERE NOT (id = ANY(%s))", (claim_ids,))
    for claim in claims:
        conn.execute(
            "INSERT INTO claims (id, statement, confidence, status, supersedes, evidence_json)"
            " VALUES (%s, %s, %s, %s, %s, %s::jsonb) ON CONFLICT (id) DO UPDATE SET"
            " statement = EXCLUDED.statement, confidence = EXCLUDED.confidence,"
            " status = EXCLUDED.status, supersedes = EXCLUDED.supersedes,"
            " evidence_json = EXCLUDED.evidence_json",
            (
                claim["id"],
                claim["statement"],
                claim["confidence"],
                claim["status"],
                claim["supersedes"],
                json.dumps(claim["evidence_json"], default=str),
            ),
        )


def search(conn: Any, text: str, embed: Embedder, limit: int = 5) -> list[dict[str, Any]]:
    """The chunks nearest to the text by cosine similarity, best first."""
    ensure_schema(conn)
    query = vector_literal(embed([text])[0])
    rows = conn.execute(
        "SELECT c.uri, c.raw_ids, c.claim_ids, 1 - (e.embedding <=> %s::vector) AS score"
        " FROM chunks c JOIN embeddings e ON e.chunk_id = c.id"
        " ORDER BY e.embedding <=> %s::vector, c.uri, c.chunk_index LIMIT %s",
        (query, query, limit),
    ).fetchall()
    return [
        {"uri": uri, "raw_ids": list(raw), "claim_ids": list(claim), "score": round(float(score), 6)}
        for uri, raw, claim, score in rows
    ]


def purge(conn: Any, raw_id: str) -> int:
    """Delete every chunk citing the raw id (embeddings follow by cascade); returns the count."""
    ensure_schema(conn)
    with conn.transaction():
        cursor = conn.execute("DELETE FROM chunks WHERE %s = ANY(raw_ids)", (raw_id,))
        return cursor.rowcount


# SQLite backend


def backend(root: Path) -> str:
    configured = (
        (common.jarvis_config(root).get("index") or {}).get("backend")
        if (root / "config" / "jarvis.yaml").exists()
        else None
    )
    return os.environ.get("JARVIS_INDEX_BACKEND") or configured or "sqlite"


def sqlite_rebuild(store: Any, root: Path, embed: Embedder, model: str = MODEL) -> dict[str, int]:
    """The SQLite twin of rebuild(): only chunks not already indexed are embedded."""
    chunks, claims = collect(root)
    stale, new = plan_chunks(store.chunk_rows(), chunks, model)
    vectors = embed([chunk.text for chunk in new]) if new else []
    if len(vectors) != len(new):
        raise ValueError(f"embedder returned {len(vectors)} vectors for {len(new)} chunks")
    store.sync(stale, new, vectors, model)
    store.sync_claims(claims)
    return {"chunks": len(chunks), "embedded": len(new), "removed": len(stale), "claims": len(claims)}


def run_sqlite(args: argparse.Namespace, root: Path, embed: Embedder) -> str:
    from index_sqlite import SqliteStore, index_path

    store = SqliteStore(index_path(root), DIMENSIONS)
    try:
        if args.command == "rebuild":
            return json.dumps(sqlite_rebuild(store, root, embed), sort_keys=True)
        if args.command == "search":
            return json.dumps(store.search(embed([" ".join(args.text)])[0], args.limit), indent=2)
        return json.dumps({"purged": store.purge(args.raw_id), "raw_id": args.raw_id})
    finally:
        store.close()


# CLI


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="index.py", description="Retrieval index over memory files.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("rebuild", help="index changed memory files, drop removed ones")
    find = sub.add_parser("search", help="nearest chunks to a text, as JSON")
    find.add_argument("text", nargs="+")
    find.add_argument("--limit", type=int, default=5)
    drop = sub.add_parser("purge", help="delete chunks citing a raw id")
    drop.add_argument("raw_id")
    return parser.parse_args(argv)


def run_command(conn: Any, args: argparse.Namespace, root: Path, embed: Embedder) -> str:
    """The JSON output of one subcommand against an open connection."""
    if args.command == "rebuild":
        return json.dumps(rebuild(conn, root, embed), sort_keys=True)
    if args.command == "search":
        return json.dumps(search(conn, " ".join(args.text), embed, args.limit), indent=2)
    return json.dumps({"purged": purge(conn, args.raw_id), "raw_id": args.raw_id})


def main(argv: list[str] | None = None, embed: Embedder | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    root = repo_root()
    embedder = embed or FastEmbedder()
    if backend(root) == "sqlite":
        print(run_sqlite(args, root, embedder))
        return 0
    import psycopg

    try:
        with connect(database_url(root)) as conn:
            print(run_command(conn, args, root, embedder))
    except DatabaseUnavailable as error:
        print(f"index: {error}", file=sys.stderr)
        return 1
    except psycopg.Error as error:
        print(f"index: database error: {first_line(error, type(error).__name__)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
