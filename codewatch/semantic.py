"""
semantic.py — Optional semantic duplicate detection via local embeddings.

This stage is skipped gracefully when sentence-transformers or sqlite-vec is
not installed. It can also be skipped explicitly via --skip-semantic for fast
CI runs.

Detection is local and offline — never the AI provider — so the detection
boundary stays deterministic (under a pinned environment). Results are
reproducible when model name, model SHA, hardware, and library versions are
fixed, but may differ across environments. Similarity scores near the
threshold (±0.02) are particularly susceptible to float nondeterminism in
matmul reductions. Pin embedding_model_sha in config.yaml for CI use.
"""

import logging
import sqlite3
import struct
from dataclasses import dataclass

from .models import FileProfile, Finding

logger = logging.getLogger(__name__)


@dataclass
class _FuncRecord:
    """Lightweight representation of one function extracted for embedding."""

    func_name: str
    relative_path: str
    abs_path: str
    lineno: int
    body: str


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def detect_duplicates(
    profiles: list[FileProfile],
    threshold: float = 0.85,
    model_name: str = "all-MiniLM-L6-v2",
) -> list[Finding]:
    """Detect semantically duplicate functions across all profiled files.

    Returns an empty list — not an error — when sentence-transformers or
    sqlite-vec is unavailable, or when fewer than 2 functions exist.
    """
    try:
        from sentence_transformers import SentenceTransformer  # type: ignore
    except ImportError:
        logger.warning(
            "sentence-transformers not installed; skipping DUPLICATE_LOGIC detection"
        )
        return []

    try:
        import sqlite_vec  # type: ignore
    except ImportError:
        logger.warning("sqlite-vec not installed; skipping DUPLICATE_LOGIC detection")
        return []

    records = _extract_function_records(profiles)
    if len(records) < 2:
        return []

    logger.info("embedding %d functions with %s", len(records), model_name)
    model = SentenceTransformer(model_name)
    embeddings = model.encode(
        [r.body for r in records],
        show_progress_bar=False,
        convert_to_numpy=True,
    )

    pairs = _find_similar_pairs(embeddings, records, threshold, sqlite_vec)
    return [_make_finding(a, b, sim, threshold) for a, b, sim in pairs]


# ---------------------------------------------------------------------------
# Function body extraction
# ---------------------------------------------------------------------------


def _extract_function_records(profiles: list[FileProfile]) -> list[_FuncRecord]:
    """Read source files to get the raw text of each function body.

    FileProfile stores metrics but not source text. We re-read each file here.
    Function-level embedding is preferred over file-level: a file with one
    duplicated function among nine unique ones would produce an unactionable
    file-level finding, whereas function-level gives a specific fix target.
    """
    records: list[_FuncRecord] = []

    for profile in profiles:
        try:
            with open(profile.path, encoding="utf-8", errors="replace") as fh:
                source_lines = fh.readlines()
        except OSError as exc:
            logger.warning(
                "could not read %s for semantic analysis: %s", profile.path, exc
            )
            continue

        for func in profile.functions:
            start = func.lineno - 1  # FunctionMetrics.lineno is 1-indexed
            end = start + func.lines
            body = "".join(source_lines[start:end])
            if not body.strip():
                continue
            records.append(
                _FuncRecord(
                    func_name=func.name,
                    relative_path=profile.relative_path,
                    abs_path=profile.path,
                    lineno=func.lineno,
                    body=body,
                )
            )

    return records


# ---------------------------------------------------------------------------
# Similarity computation
# ---------------------------------------------------------------------------


def _find_similar_pairs(
    embeddings,
    records: list[_FuncRecord],
    threshold: float,
    sqlite_vec,
) -> list[tuple["_FuncRecord", "_FuncRecord", float]]:
    """Return all (a, b, similarity) triples where cosine similarity ≥ threshold.

    Stores embeddings in an in-memory SQLite database with the sqlite-vec
    extension loaded. Uses an all-pairs cross-join query with
    vec_distance_cosine — exact, not approximate. Suitable for function counts
    in typical single-repo analysis; may be slow for very large monorepos.

    Cosine distance = 1 − cosine similarity, so the threshold is inverted
    before passing to the SQL predicate.
    """
    db = sqlite3.connect(":memory:")
    try:
        db.enable_load_extension(True)
        sqlite_vec.load(db)
        db.enable_load_extension(False)

        db.execute(
            "CREATE TABLE embeddings (id INTEGER PRIMARY KEY, vec BLOB)"
        )

        # Pack as little-endian float32 — the wire format sqlite-vec expects.
        for i in range(len(records)):
            blob = embeddings[i].astype("<f4").tobytes()
            db.execute("INSERT INTO embeddings VALUES (?, ?)", [i, blob])

        dist_threshold = 1.0 - threshold  # cosine distance ≤ this → similar

        rows = db.execute(
            """
            SELECT a.id, b.id,
                   vec_distance_cosine(a.vec, b.vec) AS dist
            FROM   embeddings a, embeddings b
            WHERE  a.id < b.id
              AND  vec_distance_cosine(a.vec, b.vec) <= ?
            """,
            [dist_threshold],
        ).fetchall()
    finally:
        db.close()

    return [
        (records[a_id], records[b_id], round(1.0 - dist, 4))
        for a_id, b_id, dist in rows
    ]


# ---------------------------------------------------------------------------
# Finding construction
# ---------------------------------------------------------------------------


def _make_finding(
    a: _FuncRecord,
    b: _FuncRecord,
    similarity: float,
    threshold: float,
) -> Finding:
    """Construct a DUPLICATE_LOGIC Finding for one similar function pair."""
    return Finding(
        rule="DUPLICATE_LOGIC",
        severity="medium",
        file=a.relative_path,
        # Target encodes both sides of the pair so the explanations dict key
        # "{rule}:{target}" is unique per pair even when function names collide.
        target=f"{a.func_name} in {a.relative_path} ↔ {b.func_name} in {b.relative_path}",
        metric_value=similarity,
        threshold=threshold,
        # Both files affected — the engineer needs to act on both sides.
        affected_nodes=sorted({a.relative_path, b.relative_path}),
    )
