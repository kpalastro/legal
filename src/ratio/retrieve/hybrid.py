"""Hybrid retrieval: lexical, dense, and the fusion between them.

The published evidence on legal retrieval is unusually clear, and it runs
against the default instinct to reach for embeddings first:

* On CLERC (1.84M US case-law passages), **BM25 outperformed zero-shot dense
  retrieval**. A fine-tuned domain encoder beat both -- but only a *fine-tuned*
  one, which is a project, not a dependency.
* Legal queries are term-of-art queries. "Special share", "prescribed
  corporation", "substantial lessening of competition" have no useful
  distributional neighbourhood; a general-purpose encoder blurs them into the
  surrounding vocabulary.
* Citations and section numbers are exact-match objects. No embedding
  represents the difference between s 52 and s 52A.
* Document-level retrieval mismatch -- retrieving a passage from the wrong
  document -- is severe in long-document corpora, and summarisation-augmented
  chunking roughly halves it.

So the design here is: lexical as the backbone, dense as an opt-in signal,
reciprocal rank fusion to combine them, and a document-level collapse after
fusion. The document collapse is the DRM mitigation and it runs late, on the
fused list, so that it cannot distort either ranker.

The offline default is lexical-only. That is a deliberate choice, not a
limitation being papered over: everything else in this repository -- the rules,
the networks, the evaluator -- runs with no model and no network, and retrieval
should not be the one component that secretly needs one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Protocol, runtime_checkable

from ratio.kb.index import KnowledgeBase, ParagraphRef
from ratio.model import Court


@runtime_checkable
class Embedder(Protocol):
    """The seam for a real embedding model.

    Deliberately narrow, and deliberately not shipped with an implementation.
    A production deployment wires in a legal-domain encoder (voyage-law-2 is the
    commonly cited commercial option; Qwen3-Embedding-8B and ReaKase-8B are the
    open alternatives) and *benchmarks it on Australian material before
    committing*, because the domain-specific encoders are trained predominantly
    on US, EU and Chinese legal text and their transfer to Australian judgments
    is an open question, not a given.

    Nothing in this repository fabricates an embedding. A hashing-based
    "embedding" would let the fusion code path be exercised, but it would be a
    lexical projection wearing a dense retriever's clothes, and the temptation to
    report its scores as if they measured semantic similarity is exactly the kind
    of quiet dishonesty this system exists to avoid. Tests inject a small
    deterministic double instead, where its nature is obvious.
    """

    name: str

    def embed(self, texts: list[str]) -> list[list[float]]:
        ...


@dataclass(frozen=True)
class RetrievalFilters:
    """Constraints applied *before* fusion.

    Filtering first is both cheaper and more correct: a first-instance decision
    that is not binding on the forum should not occupy a slot in the fused list
    and then be removed, because it will already have displaced something
    relevant during fusion.
    """

    courts: tuple[str, ...] = ()
    jurisdictions: tuple[str, ...] = ()
    min_court_rank: int | None = None
    max_court_rank: int | None = None
    date_from: date | None = None
    date_to: date | None = None
    judgments: tuple[str, ...] = ()
    exclude_synthetic: bool = False
    require_good_law: bool = False

    def admits(self, ref: ParagraphRef) -> bool:
        if self.courts and ref.court not in self.courts:
            return False
        if self.jurisdictions and ref.jurisdiction not in self.jurisdictions:
            return False
        if self.judgments and ref.judgment_id not in self.judgments:
            return False
        if self.exclude_synthetic and ref.synthetic:
            return False
        if self.min_court_rank is not None or self.max_court_rank is not None:
            try:
                rank = Court(ref.court).rank
            except ValueError:
                rank = Court.OTHER.rank
            if self.min_court_rank is not None and rank < self.min_court_rank:
                return False
            if self.max_court_rank is not None and rank > self.max_court_rank:
                return False
        decided = date.fromisoformat(ref.date_decided)
        if self.date_from and decided < self.date_from:
            return False
        if self.date_to and decided > self.date_to:
            return False
        return True

    @property
    def is_binding_only(self) -> bool:
        """Whether the filter restricts retrieval to courts that can bind."""
        return self.min_court_rank is not None and self.min_court_rank >= Court.FCA.rank


@dataclass
class SearchResult:
    ref: ParagraphRef
    score: float
    lexical_rank: int | None = None
    dense_rank: int | None = None
    # What the retriever can tell an expert about why this came back.
    matched_terms: dict[str, float] = field(default_factory=dict)
    authority_caveat: str | None = None

    def explain(self) -> str:
        bits = [f"{self.ref.citation_label}  score={self.score:.4f}"]
        if self.lexical_rank is not None:
            bits.append(f"lexical rank {self.lexical_rank}")
        if self.dense_rank is not None:
            bits.append(f"dense rank {self.dense_rank}")
        if self.matched_terms:
            top = sorted(self.matched_terms.items(), key=lambda kv: -kv[1])[:4]
            bits.append("terms: " + ", ".join(f"{t}({w:.2f})" for t, w in top))
        return " | ".join(bits)


class HybridRetriever:
    """Lexical backbone with optional dense fusion, via reciprocal rank fusion.

    RRF is used rather than score normalisation because BM25 scores and cosine
    similarities are not on comparable scales and their distributions shift with
    query length. RRF discards the magnitudes and keeps the orderings, which is
    the part that is actually comparable. ``k`` damps the influence of the very
    top ranks; the standard value of 60 is used.
    """

    def __init__(
        self,
        kb: KnowledgeBase,
        *,
        embedder: Embedder | None = None,
        rrf_k: int = 60,
        dense_candidates: int = 200,
    ) -> None:
        self.kb = kb
        self.embedder = embedder
        self.rrf_k = rrf_k
        self.dense_candidates = dense_candidates
        self._dense_index: list[tuple[str, list[float]]] | None = None

    # -- dense index -------------------------------------------------------

    def _ensure_dense_index(self) -> None:
        if self.embedder is None or self._dense_index is not None:
            return
        doc_ids = list(self.kb.refs)
        vectors = self.embedder.embed([self.kb.refs[d].text for d in doc_ids])
        if len(vectors) != len(doc_ids):
            raise ValueError(
                f"embedder {self.embedder.name} returned {len(vectors)} vectors "
                f"for {len(doc_ids)} texts"
            )
        self._dense_index = list(zip(doc_ids, vectors, strict=True))

    @staticmethod
    def _cosine(a: list[float], b: list[float]) -> float:
        dot = sum(x * y for x, y in zip(a, b, strict=True))
        na = sum(x * x for x in a) ** 0.5
        nb = sum(y * y for y in b) ** 0.5
        return dot / (na * nb) if na and nb else 0.0

    def _dense_ranking(self, query: str) -> list[str]:
        if self.embedder is None:
            return []
        self._ensure_dense_index()
        assert self._dense_index is not None
        query_vector = self.embedder.embed([query])[0]
        scored = [
            (doc_id, self._cosine(query_vector, vector))
            for doc_id, vector in self._dense_index
        ]
        scored.sort(key=lambda pair: (-pair[1], pair[0]))
        return [doc_id for doc_id, _ in scored[: self.dense_candidates]]

    # -- search ------------------------------------------------------------

    def search(
        self,
        query: str,
        *,
        filters: RetrievalFilters | None = None,
        limit: int = 10,
        explain: bool = False,
        collapse_to_document: bool = True,
    ) -> list[SearchResult]:
        """Retrieve paragraphs, fused, filtered and collapsed to documents."""
        filters = filters or RetrievalFilters()

        # 1. Lexical. Over-fetch so that filtering and fusion have candidates.
        over_fetch = max(limit * 8, 40)
        lexical = self.kb.index.search(query, limit=over_fetch * 3, explain=explain)
        lexical = [r for r in lexical if self._admits(r.doc_id, filters)][:over_fetch]

        # 2. Dense, if configured.
        dense_ids = [
            d
            for d in self._dense_ranking(query)
            if d in self.kb.refs and self._admits(d, filters)
        ][:over_fetch]

        # 3. Reciprocal rank fusion.
        scores: dict[str, float] = {}
        lexical_rank: dict[str, int] = {}
        dense_rank: dict[str, int] = {}
        terms: dict[str, dict[str, float]] = {}

        for rank, result in enumerate(lexical, start=1):
            scores[result.doc_id] = scores.get(result.doc_id, 0.0) + 1.0 / (
                self.rrf_k + rank
            )
            lexical_rank[result.doc_id] = rank
            terms[result.doc_id] = result.terms

        for rank, doc_id in enumerate(dense_ids, start=1):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (self.rrf_k + rank)
            dense_rank[doc_id] = rank

        if not scores:
            return []

        # 4. Collapse to documents (DRM mitigation). Keep each document's best
        #    paragraph. Done after fusion, so neither ranker's ordering is
        #    distorted by it.
        ordered = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
        if collapse_to_document:
            seen: set[str] = set()
            collapsed: list[tuple[str, float]] = []
            for doc_id, score in ordered:
                judgment_id = self.kb.refs[doc_id].judgment_id
                if judgment_id in seen:
                    continue
                seen.add(judgment_id)
                collapsed.append((doc_id, score))
            ordered = collapsed + [
                (d, s)
                for d, s in sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
                if d not in {c[0] for c in collapsed}
            ]

        results: list[SearchResult] = []
        for doc_id, score in ordered[:limit]:
            ref = self.kb.refs[doc_id]
            caveat = None
            key = self._judgment_key(ref.judgment_id)
            if key and filters.require_good_law:
                from ratio.model import AuthorityStatus

                status = self.kb.graph.authority_status(key)
                if status.status in {
                    AuthorityStatus.OVERRULED,
                    AuthorityStatus.SUPERSEDED_BY_STATUTE,
                }:
                    continue
                caveat = status.caveat
            elif key:
                caveat = self.kb.graph.authority_status(key).caveat
            results.append(
                SearchResult(
                    ref=ref,
                    score=score,
                    lexical_rank=lexical_rank.get(doc_id),
                    dense_rank=dense_rank.get(doc_id),
                    matched_terms=terms.get(doc_id, {}),
                    authority_caveat=caveat,
                )
            )
        return results

    # -- internals ---------------------------------------------------------

    def _admits(self, doc_id: str, filters: RetrievalFilters) -> bool:
        ref = self.kb.refs.get(doc_id)
        return ref is not None and filters.admits(ref)

    def _judgment_key(self, judgment_id: str) -> str | None:
        judgment = self.kb.judgments.get(judgment_id)
        return judgment.canonical_key() if judgment else None
