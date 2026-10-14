"""The systems under test.

Each one wraps a real component of the system and exposes it through the single
``predict`` method the harness calls. Keeping the adapters here rather than
sprinkling evaluation logic into the components matters: the components must not
know they are being evaluated, or the evaluation will end up shaped around what
they happen to do.

The abstention behaviour of each wrapper is the interesting part, and each is
justified below rather than left as a default.
"""

from __future__ import annotations

from datetime import date

from ratio.eval.harness import ABSTAIN, CaseSpec
from ratio.kb.index import KnowledgeBase
from ratio.model import RuleSet
from ratio.retrieve.bm25 import tokenize
from ratio.retrieve.hybrid import HybridRetriever, RetrievalFilters
from ratio.rules.engine import CredenceResolver, RuleEngine
from ratio.verify.grounding import Verifier


class RuleSystem:
    """Applies a ruleset and abstains when the facts do not determine the answer.

    The abstention rule is the important one. If the engine reports a gap -- a
    fact the rule needed and was not given -- the system abstains rather than
    returning the conclusion the engine happened to reach with the fact missing.
    The engine's own value for an absent fact is "not determinable", and reporting
    a conclusion that rests on one is precisely the behaviour this whole system is
    built to avoid.
    """

    name: str
    kinds = frozenset({"rule"})

    def __init__(
        self,
        ruleset: RuleSet,
        *,
        credence: CredenceResolver | None = None,
        name: str | None = None,
    ) -> None:
        self.ruleset = ruleset
        self.engine = RuleEngine(ruleset, credence=credence)
        self.name = name or f"rules:{ruleset.id}@{ruleset.version}"

    def predict(self, case: CaseSpec) -> str:
        tree = self.engine.evaluate(case.facts)
        if tree.gaps:
            return ABSTAIN
        return tree.conclusion


class RetrievalSystem:
    """Returns the leading judgment for a query, or abstains if nothing matches.

    The abstention gate is the whole substance of this wrapper, and it is worth
    being precise about, because the obvious implementation does not work.

    The obvious implementation is a threshold on the score the retriever returns.
    It cannot work here. The retriever returns a fused reciprocal-rank score, and
    RRF keeps only the *ordering* of each ranker, so its output is a function of
    rank alone: the top hit of a single ranker always scores 1/(60+1), whatever
    the corpus contained. A threshold on that number fires on an empty result set
    or not at all. It looks like a safety control and is not one.

    What the gate measures instead is how much of the question the corpus
    recognises. Each distinct query term is weighted by its IDF -- its
    distinctiveness -- and the top hit's coverage is the share of that weight it
    accounts for. The reading is plain: "the best paragraph in the corpus uses
    20% of the distinctive vocabulary of your question" is a refusal; "it uses
    84%" is an answer. Unweighted term counts do not separate the two: a lawyer
    who pads a question with ordinary words ("what is the effect of ... on a
    notifiable action") raises unweighted coverage without the corpus having any
    more to say, whereas padding with common words barely moves IDF-weighted
    coverage. Terms the corpus does not contain at all are weighted at
    ``max_idf``, so that a question about a topic we hold nothing on scores near
    zero rather than near one -- see ``BM25Index.max_idf``.

    The default threshold of 0.5 cleanly separated the positive and refusal cases
    of ``eval/tasks/04_retrieval.yaml`` (lowest positive 0.607, highest refusal
    0.206) and the margin is reported so a reviewer can see it. Two cautions, and
    they are the reason this is a dataclass field rather than a constant: six
    cases is not a calibration, and coverage bounds *topical* relevance only. A
    paragraph can use every word of the question and still not answer it, so
    coverage is a necessary condition for answering, never a sufficient one.
    """

    kinds = frozenset({"retrieval"})

    def __init__(
        self,
        kb: KnowledgeBase,
        *,
        embedder=None,
        filters: RetrievalFilters | None = None,
        min_coverage: float = 0.5,
        name: str = "retrieval:hybrid",
    ) -> None:
        self.retriever = HybridRetriever(kb, embedder=embedder)
        self.kb = kb
        self.filters = filters or RetrievalFilters()
        self.min_coverage = min_coverage
        self.name = name

    def coverage(self, query: str, matched: dict[str, float]) -> float:
        """The share of the query's IDF mass the matched paragraph accounts for."""
        index = self.kb.index
        total = 0.0
        covered = 0.0
        for term in set(tokenize(query)):
            weight = index.idf(term) or index.max_idf()
            total += weight
            if term in matched:
                covered += weight
        return covered / total if total else 0.0

    def predict(self, case: CaseSpec) -> str:
        query = case.query or case.prompt
        results = self.retriever.search(query, filters=self.filters, limit=5, explain=True)
        if not results:
            return ABSTAIN
        top = results[0]
        if self.coverage(query, top.matched_terms) < self.min_coverage:
            return ABSTAIN
        return top.ref.judgment_id


class VerificationSystem:
    """Runs the verifier over a claim and returns its verdict.

    A claim the verifier cannot ground is reported as a refusal rather than as a
    low-confidence answer, because the verifier's ``is_clean`` is a gate and not a
    score. This is the system whose guardrail cases matter most: every
    ``expect_refusal`` case in the suite runs through here.
    """

    name = "verification:fail-closed"
    kinds = frozenset({"verification"})

    def __init__(self, kb: KnowledgeBase) -> None:
        assert kb.verifier is not None, "knowledge base was built without a verifier"
        self.verifier: Verifier = kb.verifier

    def predict(self, case: CaseSpec) -> str:
        claim = case.claim or {}
        text = claim.get("text") or case.prompt
        report = self.verifier.verify_claim(
            text,
            quotes=claim.get("quotes"),
            citations=claim.get("citations"),
        )
        if not report.is_clean:
            return "__refused__"
        return report.verdict


class TreatmentSystem:
    """Reports the authority status of a judgment as at a date.

    Abstains when the authority is not in the corpus at all, rather than
    reporting "good law" by default. An authority we have not ingested has an
    unknown status, and reporting it as good law would be the single most
    dangerous default in the system.
    """

    name = "citation-graph:authority-status"
    kinds = frozenset({"treatment"})

    def __init__(self, kb: KnowledgeBase) -> None:
        self.kb = kb

    def predict(self, case: CaseSpec) -> str:
        if case.as_at:
            as_at = date.fromisoformat(case.as_at)
        else:
            as_at = None
        key = case.prompt
        # Prefer an explicit key in the case; fall back to resolving the prompt.
        if case.claim.get("key"):
            key = case.claim["key"]
        if not self.kb.graph.is_resolved(key):
            return ABSTAIN
        return self.kb.graph.authority_status(key, as_at=as_at).status.value


# -- registry --------------------------------------------------------------


def default_systems(kb: KnowledgeBase, ruleset: RuleSet) -> dict[str, object]:
    """The set a ``ratio eval`` run exercises by default."""
    return {
        "rule": RuleSystem(ruleset),
        "retrieval": RetrievalSystem(kb),
        "verification": VerificationSystem(kb),
        "treatment": TreatmentSystem(kb),
    }
