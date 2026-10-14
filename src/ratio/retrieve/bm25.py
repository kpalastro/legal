"""Okapi BM25 over paragraphs, implemented in-tree.

Why not a library: the ranking function has to be inspectable. When a partner
asks "why did this case come back fourth", the answer should be a score we can
print, term by term, not a number from a compiled extension.

Why BM25 first for legal text: legal queries are term-of-art queries. "special
share", "prescribed corporation", "substantial lessening of competition" are
phrases with no useful distributional neighbourhood; a dense retriever trained
on general prose blurs them. BM25 is the baseline that a dense model must beat
before it earns a place in the ensemble, and it is what we fall back to when
the embedding service is unavailable. See docs/03-retrieval-and-verification.md.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field

_TOKEN_RE = re.compile(r"[a-z0-9]+(?:[-/][a-z0-9]+)*")

# A short stop list. Legal text is dense with "of the said" filler; removing it
# costs nothing and stops common function words dominating short paragraph
# matches. Deliberately conservative -- "not", "no" and "any" are load-bearing
# in legal text and must survive.
_STOPWORDS = frozenset(
    """
    a an the of to in on at by for with from as is are was were be been being
    that this these those it its and or but if then than so such which who whom
    there herein hereof thereto wherein
    """.split()
)


def tokenize(text: str) -> list[str]:
    """Lowercase, keep hyphens and slashes inside tokens, drop stopwords.

    "s 674(2)" -> ["s", "674", "2"]: subsection markers are kept as tokens so
    that a query naming a subsection matches the paragraph citing it.
    """
    raw = _TOKEN_RE.findall(text.lower())
    # Split trailing parenthetical numbers: "674(2)" -> "674", "2"
    out: list[str] = []
    for tok in raw:
        out.append(tok)
        for part in re.split(r"[-/]", tok):
            if part and part != tok:
                out.append(part)
    return [t for t in out if t not in _STOPWORDS and len(t) > 1]


@dataclass
class ScoredDoc:
    doc_id: str
    score: float
    terms: dict[str, float] = field(default_factory=dict)


class BM25Index:
    """Inverted index with Okapi BM25 scoring.

    Parameters follow the usual defaults; ``k1`` controls term-frequency
    saturation and ``b`` length normalisation. For short, heavily numbered
    legal paragraphs a slightly lower ``b`` than the 0.75 default performs
    better, because paragraph length varies mostly with how much a judge chose
    to say rather than with topical focus.
    """

    def __init__(self, k1: float = 1.4, b: float = 0.6) -> None:
        self.k1 = k1
        self.b = b
        self._postings: dict[str, dict[str, int]] = {}
        self._lengths: dict[str, int] = {}
        self._doc_meta: dict[str, dict[str, str]] = {}
        self._avg_len: float = 0.0

    def __len__(self) -> int:
        return len(self._lengths)

    def add(
        self,
        doc_id: str,
        text: str,
        *,
        meta: dict[str, str] | None = None,
    ) -> None:
        tokens = tokenize(text)
        counts = Counter(tokens)
        self._lengths[doc_id] = len(tokens) or 1
        self._doc_meta[doc_id] = meta or {}
        for term, tf in counts.items():
            self._postings.setdefault(term, {})[doc_id] = tf
        self._recompute_avg()

    def add_many(self, docs: list[tuple[str, str, dict[str, str] | None]]) -> None:
        """Bulk load. Recomputes the average length once, not per document."""
        for doc_id, text, meta in docs:
            tokens = tokenize(text)
            counts = Counter(tokens)
            self._lengths[doc_id] = len(tokens) or 1
            self._doc_meta[doc_id] = meta or {}
            for term, tf in counts.items():
                self._postings.setdefault(term, {})[doc_id] = tf
        self._recompute_avg()

    def _recompute_avg(self) -> None:
        self._avg_len = (
            sum(self._lengths.values()) / len(self._lengths) if self._lengths else 0.0
        )

    def _idf(self, term: str) -> float:
        n = len(self._lengths)
        df = len(self._postings.get(term, {}))
        if df == 0:
            return 0.0
        return math.log(1.0 + (n - df + 0.5) / (df + 0.5))

    def idf(self, term: str) -> float:
        """Inverse document frequency, or 0.0 for a term no document contains.

        Public because the abstention gate needs it, and the gate is not a
        scoring concern: it answers "how much of what this question is *made of*
        does the corpus recognise at all", which is a different question from
        "which document should come first". A gate that reached into ``_idf``
        would be a second, silently diverging definition of the same quantity.
        """
        return self._idf(term)

    def max_idf(self) -> float:
        """The IDF a term would carry if exactly one document contained it.

        This is the weight the abstention gate gives a query term that appears
        in *no* document. The obvious alternative -- weight it at zero, since a
        term with no postings has no IDF -- inverts the measurement: the less
        the corpus had to say about a question, the better covered the question
        would look. A term the corpus has never seen is the strongest evidence
        available that the corpus does not answer the question, so it must
        weigh at least as heavily as the rarest term that does exist.
        """
        n = len(self._lengths)
        if n == 0:
            return 0.0
        return math.log(1.0 + (n - 0.5) / 1.5)

    def score(self, doc_id: str, query_terms: list[str]) -> float:
        total = 0.0
        length = self._lengths.get(doc_id, 0)
        for term in set(query_terms):
            tf = self._postings.get(term, {}).get(doc_id, 0)
            if not tf:
                continue
            denom = tf + self.k1 * (1 - self.b + self.b * length / (self._avg_len or 1))
            total += self._idf(term) * (tf * (self.k1 + 1)) / denom
        return total

    def search(
        self,
        query: str,
        *,
        limit: int = 20,
        explain: bool = False,
    ) -> list[ScoredDoc]:
        query_terms = tokenize(query)
        if not query_terms:
            return []
        # Only score documents that contain at least one query term.
        candidates: set[str] = set()
        for term in set(query_terms):
            candidates.update(self._postings.get(term, {}).keys())

        results: list[ScoredDoc] = []
        for doc_id in candidates:
            total = 0.0
            breakdown: dict[str, float] = {}
            length = self._lengths.get(doc_id, 0)
            for term in set(query_terms):
                tf = self._postings.get(term, {}).get(doc_id, 0)
                if not tf:
                    continue
                denom = tf + self.k1 * (
                    1 - self.b + self.b * length / (self._avg_len or 1)
                )
                contribution = self._idf(term) * (tf * (self.k1 + 1)) / denom
                total += contribution
                if explain:
                    breakdown[term] = round(contribution, 4)
            results.append(
                ScoredDoc(
                    doc_id=doc_id,
                    score=total,
                    terms=breakdown if explain else {},
                )
            )
        results.sort(key=lambda r: (-r.score, r.doc_id))
        return results[:limit]

    def meta(self, doc_id: str) -> dict[str, str]:
        return self._doc_meta.get(doc_id, {})

    def phrase_candidates(self, phrase: str, window: int = 3) -> set[str]:
        """Documents containing the query terms within ``window`` tokens.

        A crude proximity filter, used to boost exact term-of-art matches like
        "substantial lessening of competition" above documents that merely
        contain all three words in different paragraphs.
        """
        terms = tokenize(phrase)
        if len(terms) < 2:
            return set()
        docs: set[str] | None = None
        for term in terms:
            posting = set(self._postings.get(term, {}))
            docs = posting if docs is None else (docs & posting)
            if not docs:
                return set()
        return docs or set()
