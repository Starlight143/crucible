"""
features/run_deduplication.py
==============================
Semantic run deduplication — detects when a new analysis topic is highly
similar to a previous run, preventing redundant LLM pipeline executions.

Algorithm
---------
Similarity is measured using **TF-IDF cosine similarity**.  The implementation
is pure Python standard library with no third-party dependencies.  When
``scikit-learn`` is installed it is preferred because it provides superior
n-gram tokenisation and sublinear TF scaling; the stdlib path produces
equivalent results on English text.

Corpus
------
The corpus is built from the ``analysis_result.json`` files in all
``saved_projects/`` subdirectories.  Text is extracted from:
  - ``project_name``
  - ``summary``
  - ``consensus``
  - ``disagreement``
  - First three experiment ``goal`` strings

Lookback window and corpus size are configurable via env vars to keep the
check fast even on large workspaces.

Usage::

    from crucible.features.run_deduplication import (
        check_duplicate_run,
        DedupCheckResult,
    )

    result = check_duplicate_run(
        topic="Funding rate arbitrage strategy for BTC perpetuals",
        workspace_dir="/path/to/repo",
    )
    if result.has_similar_runs:
        best = result.most_similar_run
        print(f"Similar run: {best.project_name}  sim={best.similarity:.0%}")

Or via the enhanced runner::

    python run_crucible_enhanced.py run --dedup-check

Environment variables
---------------------
DEDUP_SIMILARITY_THRESHOLD   Cosine similarity [0.0, 1.0] above which a run is
                              flagged as a duplicate (default: 0.85).
DEDUP_LOOKBACK_DAYS          Only consider runs within the last N days
                              (default: 30; set to 0 for no time limit).
DEDUP_MAX_CORPUS_RUNS        Maximum number of past runs to compare against
                              (default: 100).
"""
from __future__ import annotations

import json
import math
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# ── Configuration ─────────────────────────────────────────────────────────────

def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, ""))
    except (ValueError, TypeError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, ""))
    except (ValueError, TypeError):
        return default


DEDUP_SIMILARITY_THRESHOLD: float = _env_float("DEDUP_SIMILARITY_THRESHOLD", 0.85)
DEDUP_LOOKBACK_DAYS: int = _env_int("DEDUP_LOOKBACK_DAYS", 30)
DEDUP_MAX_CORPUS_RUNS: int = _env_int("DEDUP_MAX_CORPUS_RUNS", 100)

# Minimal English stop-word list (avoids NLTK / spaCy dependency)
_STOP_WORDS = frozenset({
    "a", "an", "the", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "is", "was", "are", "were", "be", "been",
    "has", "have", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "shall", "can", "not", "no", "nor", "so",
    "this", "that", "these", "those", "it", "its", "as", "if", "than",
    "then", "when", "where", "how", "what", "which", "who", "whom",
    "i", "we", "you", "he", "she", "they", "them", "their", "our",
    "via", "using", "use", "used", "based", "into", "over", "through",
    "up", "out", "about", "more", "all", "also", "very", "just",
})


# ── Data models ───────────────────────────────────────────────────────────────

@dataclass
class CorpusRun:
    """Metadata and extracted topic text for one past run."""
    run_id: str
    run_dir: str
    project_name: str
    topic_text: str
    score: Optional[float]
    risk_level: Optional[str]
    timestamp: Optional[str]


@dataclass
class SimilarRun:
    """One past run that exceeded the similarity threshold."""
    run_id: str
    project_name: str
    similarity: float
    score: Optional[float]
    risk_level: Optional[str]
    run_dir: str
    timestamp: Optional[str]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "project_name": self.project_name,
            "similarity": round(self.similarity, 4),
            "score": self.score,
            "risk_level": self.risk_level,
            "run_dir": self.run_dir,
            "timestamp": self.timestamp,
        }


@dataclass
class DedupCheckResult:
    """Result of a duplicate-run check."""
    candidate_topic: str
    similar_runs: List[SimilarRun] = field(default_factory=list)
    highest_similarity: float = 0.0
    threshold_used: float = DEDUP_SIMILARITY_THRESHOLD
    corpus_size: int = 0

    @property
    def has_similar_runs(self) -> bool:
        return len(self.similar_runs) > 0

    @property
    def most_similar_run(self) -> Optional[SimilarRun]:
        if not self.similar_runs:
            return None
        return max(self.similar_runs, key=lambda r: r.similarity)

    def summary_text(self) -> str:
        if not self.has_similar_runs:
            return (
                f"[Dedup] No similar runs found in corpus of "
                f"{self.corpus_size} run(s). "
                f"(threshold={self.threshold_used:.0%})"
            )
        lines = [
            f"[Dedup] {len(self.similar_runs)} similar run(s) found "
            f"(threshold={self.threshold_used:.0%}):",
        ]
        for sr in sorted(self.similar_runs, key=lambda r: -r.similarity):
            lines.append(
                f"  sim={sr.similarity:.0%}  project={sr.project_name}"
                f"  score={sr.score}  risk={sr.risk_level}  id={sr.run_id}"
            )
        return "\n".join(lines)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "candidate_topic": self.candidate_topic,
            "has_similar_runs": self.has_similar_runs,
            "highest_similarity": round(self.highest_similarity, 4),
            "threshold_used": self.threshold_used,
            "corpus_size": self.corpus_size,
            "similar_runs": [r.to_dict() for r in self.similar_runs],
        }


# ── TF-IDF engine (pure stdlib) ───────────────────────────────────────────────

def _tokenize(text: str) -> List[str]:
    """
    Lowercase, strip punctuation, split on whitespace, remove stop words and
    single-character tokens.
    """
    text = text.lower()
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    return [t for t in text.split() if len(t) > 1 and t not in _STOP_WORDS]


def _term_frequency(tokens: List[str]) -> Dict[str, float]:
    """Compute raw (non-normalised) term frequency dict."""
    if not tokens:
        return {}
    counts: Dict[str, int] = {}
    for t in tokens:
        counts[t] = counts.get(t, 0) + 1
    total = len(tokens)
    return {term: count / total for term, count in counts.items()}


def _build_idf(corpus_token_lists: List[List[str]]) -> Dict[str, float]:
    """
    Build a smoothed IDF mapping over *corpus_token_lists*.

    Uses the formula:  IDF(t) = log((1 + N) / (1 + df(t))) + 1
    where N is the number of documents and df(t) the document frequency.
    """
    n = len(corpus_token_lists)
    if n == 0:
        return {}
    df: Dict[str, int] = {}
    for doc_tokens in corpus_token_lists:
        for term in set(doc_tokens):
            df[term] = df.get(term, 0) + 1
    return {
        term: math.log((1.0 + n) / (1.0 + doc_freq)) + 1.0
        for term, doc_freq in df.items()
    }


def _tfidf_vector(tokens: List[str], idf: Dict[str, float]) -> Dict[str, float]:
    """Compute a sparse TF-IDF vector for *tokens* given precomputed *idf*."""
    tf = _term_frequency(tokens)
    return {term: tf_val * idf.get(term, 1.0) for term, tf_val in tf.items()}


def _cosine_similarity(
    vec_a: Dict[str, float], vec_b: Dict[str, float]
) -> float:
    """Cosine similarity between two sparse TF-IDF vectors."""
    if not vec_a or not vec_b:
        return 0.0
    common = set(vec_a) & set(vec_b)
    if not common:
        return 0.0
    dot = sum(vec_a[t] * vec_b[t] for t in common)
    norm_a = math.sqrt(sum(v * v for v in vec_a.values()))
    norm_b = math.sqrt(sum(v * v for v in vec_b.values()))
    # Use project-standard subnormal-divisor threshold (1e-14) rather than
    # the IEEE 754 minimum (1e-300).  Norms in the range (1e-300, 1e-14)
    # would pass the previous check and produce cosine values around 1e+286
    # before any downstream clamping, which is misleading even if the final
    # output ends up clipped.
    if not (norm_a > 1e-14) or not (norm_b > 1e-14):
        return 0.0
    return dot / (norm_a * norm_b)


def _try_sklearn_cosine(
    candidate: str, corpus_texts: List[str]
) -> Optional[List[float]]:
    """
    Attempt to compute similarities using sklearn's TfidfVectorizer.

    Returns a flat list of similarity scores (one per corpus document), or
    ``None`` if sklearn is not installed or an error occurs.
    """
    try:
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.metrics.pairwise import cosine_similarity

        vectorizer = TfidfVectorizer(
            stop_words="english",
            ngram_range=(1, 2),
            min_df=1,
            sublinear_tf=True,
        )
        all_texts = [candidate] + corpus_texts
        matrix = vectorizer.fit_transform(all_texts)
        sims = cosine_similarity(matrix[0:1], matrix[1:])
        # sims has shape (1, len(corpus_texts))
        return [float(s) for s in sims[0]]
    except Exception:  # noqa: BLE001
        return None


# ── Corpus loading ────────────────────────────────────────────────────────────

def _extract_topic_text(analysis_data: Dict[str, Any]) -> str:
    """
    Build a representative text string from an analysis_result.json payload.

    Concatenates: project_name, summary, consensus, disagreement, and the
    goal text from the first three experiments.
    """
    parts: List[str] = [
        str(analysis_data.get("project_name") or ""),
        str(analysis_data.get("summary") or ""),
        str(analysis_data.get("consensus") or ""),
        str(analysis_data.get("disagreement") or ""),
    ]
    for exp in list(analysis_data.get("experiments") or [])[:3]:
        if isinstance(exp, dict):
            goal = str(exp.get("goal") or "")
            if goal:
                parts.append(goal)
    return " ".join(p for p in parts if p).strip()


def _safe_mtime(p: str) -> float:
    """Return a path's mtime; return 0.0 on OSError (e.g. race-deleted directory)."""
    try:
        return os.path.getmtime(p)
    except OSError:
        return 0.0


def _load_corpus_runs(
    workspace_dir: str,
    lookback_days: int,
    max_runs: int,
) -> List[CorpusRun]:
    """
    Scan ``saved_projects/`` and build the comparison corpus.

    Directories are sorted newest-first; the lookback filter and max_runs cap
    are applied before loading JSON so large workspaces stay fast.
    """
    saved_dir = os.path.join(workspace_dir, "saved_projects")
    if not os.path.isdir(saved_dir):
        return []

    try:
        all_dirs = [
            os.path.join(saved_dir, d)
            for d in os.listdir(saved_dir)
            if os.path.isdir(os.path.join(saved_dir, d))
        ]
    except OSError:
        return []

    # Sort newest-first so the max_runs cap keeps the most recent runs.
    # Secondary key (basename, descending) ensures determinism when two
    # directories have the same mtime (e.g. created in the same second).
    all_dirs.sort(key=lambda p: (_safe_mtime(p), os.path.basename(p)), reverse=True)

    if lookback_days > 0:
        cutoff = time.time() - lookback_days * 86400
        all_dirs = [p for p in all_dirs if _safe_mtime(p) >= cutoff]

    all_dirs = all_dirs[:max_runs]

    corpus: List[CorpusRun] = []
    for run_dir in all_dirs:
        analysis_path = os.path.join(run_dir, "analysis_result.json")
        if not os.path.isfile(analysis_path):
            continue
        try:
            with open(analysis_path, "r", encoding="utf-8") as fh:
                data: Dict[str, Any] = json.load(fh)
            if not isinstance(data, dict):
                continue
        except (OSError, json.JSONDecodeError):
            continue

        topic_text = _extract_topic_text(data)
        if not topic_text.strip():
            continue

        corpus.append(CorpusRun(
            run_id=os.path.basename(run_dir),
            run_dir=run_dir,
            project_name=str(data.get("project_name") or os.path.basename(run_dir)),
            topic_text=topic_text,
            score=data.get("score"),
            risk_level=data.get("risk_level"),
            timestamp=data.get("timestamp"),
        ))

    return corpus


# ── Public API ────────────────────────────────────────────────────────────────

def check_duplicate_run(
    topic: str,
    workspace_dir: str,
    threshold: Optional[float] = None,
    lookback_days: Optional[int] = None,
    max_corpus_runs: Optional[int] = None,
) -> DedupCheckResult:
    """
    Check whether *topic* is semantically similar to any past run.

    Scans ``{workspace_dir}/saved_projects/`` for ``analysis_result.json``
    files and computes TF-IDF cosine similarity between *topic* and each past
    run's extracted topic text.

    Parameters
    ----------
    topic:
        The analysis topic or run description to check.
    workspace_dir:
        Repository root containing ``saved_projects/``.
    threshold:
        Similarity threshold [0.0, 1.0].  Runs with similarity ≥ threshold
        are reported as duplicates.  Defaults to ``DEDUP_SIMILARITY_THRESHOLD``
        (env: ``DEDUP_SIMILARITY_THRESHOLD``, default 0.85).
    lookback_days:
        Only include runs created within the last N days.  ``0`` means no time
        limit.  Defaults to ``DEDUP_LOOKBACK_DAYS``.
    max_corpus_runs:
        Maximum number of past runs in the corpus.  Defaults to
        ``DEDUP_MAX_CORPUS_RUNS``.
    """
    eff_threshold = threshold if threshold is not None else _env_float("DEDUP_SIMILARITY_THRESHOLD", 0.85)
    eff_lookback = lookback_days if lookback_days is not None else _env_int("DEDUP_LOOKBACK_DAYS", 30)
    eff_max = max_corpus_runs if max_corpus_runs is not None else _env_int("DEDUP_MAX_CORPUS_RUNS", 100)

    result = DedupCheckResult(
        candidate_topic=topic,
        threshold_used=eff_threshold,
    )

    corpus = _load_corpus_runs(workspace_dir, eff_lookback, eff_max)
    result.corpus_size = len(corpus)

    if not corpus:
        return result

    corpus_texts = [cr.topic_text for cr in corpus]

    # Try sklearn first for better quality; fall back to stdlib TF-IDF
    similarities = _try_sklearn_cosine(topic, corpus_texts)
    if similarities is None or len(similarities) != len(corpus):
        # Stdlib TF-IDF path
        all_token_lists = [_tokenize(topic)] + [_tokenize(t) for t in corpus_texts]
        idf = _build_idf(all_token_lists)
        candidate_vec = _tfidf_vector(all_token_lists[0], idf)
        similarities = []
        for corpus_tokens in all_token_lists[1:]:
            corpus_vec = _tfidf_vector(corpus_tokens, idf)
            similarities.append(_cosine_similarity(candidate_vec, corpus_vec))

    similar: List[SimilarRun] = []
    for cr, sim in zip(corpus, similarities):
        if sim >= eff_threshold:
            similar.append(SimilarRun(
                run_id=cr.run_id,
                project_name=cr.project_name,
                similarity=sim,
                score=cr.score,
                risk_level=cr.risk_level,
                run_dir=cr.run_dir,
                timestamp=cr.timestamp,
            ))

    similar.sort(key=lambda r: -r.similarity)
    result.similar_runs = similar
    result.highest_similarity = max(similarities) if similarities else 0.0

    return result
