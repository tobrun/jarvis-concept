"""The SQLite index backend: the same chunks, embeddings, and claims as the Postgres one, in one local file.

The default backend: no server, nothing to install. Embeddings are float32 blobs and search is an exact
cosine scan in numpy, which is fast at the size of one person's memory (tens of thousands of chunks).
The file is state/index.sqlite, git-ignored, and rebuildable from memory/ at any time.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np

SCHEMA = """
CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY,
    uri TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    text TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    raw_ids TEXT NOT NULL DEFAULT '[]',
    claim_ids TEXT NOT NULL DEFAULT '[]',
    privacy_class TEXT NOT NULL,
    model TEXT NOT NULL,
    embedding BLOB NOT NULL,
    UNIQUE (uri, chunk_index, content_sha256)
);
CREATE TABLE IF NOT EXISTS claims (
    id TEXT PRIMARY KEY,
    statement TEXT,
    confidence REAL,
    status TEXT,
    supersedes TEXT,
    evidence_json TEXT NOT NULL DEFAULT '[]'
);
"""


def index_path(root: Path) -> Path:
    return root / "state" / "index.sqlite"


class SqliteStore:
    """One open index file; every write runs in a transaction."""

    def __init__(self, path: Path, dimensions: int):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.dimensions = dimensions
        self.conn = sqlite3.connect(path)
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    def _vector(self, values: Sequence[float]) -> bytes:
        if len(values) != self.dimensions:
            raise ValueError(f"embedding has {len(values)} dimensions, expected {self.dimensions}")
        return np.asarray(values, dtype=np.float32).tobytes()

    def chunk_rows(self) -> list[tuple[Any, ...]]:
        """(id, uri, chunk_index, content_sha256, model) for every indexed chunk."""
        return self.conn.execute("SELECT id, uri, chunk_index, content_sha256, model FROM chunks").fetchall()

    def sync(self, stale: list[int], new: list[Any], vectors: list[Sequence[float]], model: str) -> None:
        with self.conn:
            self.conn.executemany("DELETE FROM chunks WHERE id = ?", [(i,) for i in stale])
            self.conn.executemany(
                "INSERT INTO chunks (uri, chunk_index, text, content_sha256, raw_ids, claim_ids,"
                " privacy_class, model, embedding) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        chunk.uri,
                        chunk.chunk_index,
                        chunk.text,
                        chunk.content_sha256,
                        json.dumps(chunk.raw_ids),
                        json.dumps(chunk.claim_ids),
                        chunk.privacy_class,
                        model,
                        self._vector(vector),
                    )
                    for chunk, vector in zip(new, vectors, strict=True)
                ],
            )

    def sync_claims(self, claims: list[dict[str, Any]]) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM claims")
            self.conn.executemany(
                "INSERT INTO claims (id, statement, confidence, status, supersedes, evidence_json)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (
                        c["id"],
                        c["statement"],
                        c["confidence"],
                        c["status"],
                        c["supersedes"],
                        json.dumps(c["evidence_json"], default=str),
                    )
                    for c in claims
                ],
            )

    def search(self, query: Sequence[float], limit: int) -> list[dict[str, Any]]:
        """The chunks nearest to the query vector by cosine similarity, best first, ties by uri and index."""
        rows = self.conn.execute(
            "SELECT uri, chunk_index, raw_ids, claim_ids, embedding FROM chunks ORDER BY uri, chunk_index"
        ).fetchall()
        if not rows:
            return []
        matrix = np.vstack([np.frombuffer(row[4], dtype=np.float32) for row in rows])
        target = np.frombuffer(self._vector(query), dtype=np.float32)
        norms = np.linalg.norm(matrix, axis=1) * (np.linalg.norm(target) or 1.0)
        scores = matrix @ target / np.where(norms == 0, 1.0, norms)
        order = sorted(range(len(rows)), key=lambda i: (-float(scores[i]), rows[i][0], rows[i][1]))[:limit]
        return [
            {
                "uri": rows[i][0],
                "raw_ids": json.loads(rows[i][2]),
                "claim_ids": json.loads(rows[i][3]),
                "score": round(float(scores[i]), 6),
            }
            for i in order
        ]

    def purge(self, raw_id: str) -> int:
        """Delete every chunk citing the raw id; returns the count."""
        with self.conn:
            cursor = self.conn.execute(
                "DELETE FROM chunks WHERE id IN"
                " (SELECT chunks.id FROM chunks, json_each(chunks.raw_ids) WHERE json_each.value = ?)",
                (raw_id,),
            )
            return cursor.rowcount
