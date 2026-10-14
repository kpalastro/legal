"""Tests for the evaluation harness and the retrieval abstention gate.

Everything this project claims about itself is reported through the harness, so
a fault here is worse than a fault in any component: it does not produce a wrong
answer, it produces a wrong *account* of how the system behaves, and it does so
in the artefact a reviewer is most likely to trust.

The retrieval gate gets its own tests here rather than a file of its own because
its two claims -- that it refuses when the corpus has nothing, and that it does
not refuse when the corpus has something -- are only meaningful as a pair, and
they are calibrated against the same task file the harness reads.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ratio.eval.harness import (
    ABSTAIN,
    AbstainSystem,
    CaseSpec,
    MajoritySystem,
    TaskSuite,
    run_suite,
)
from ratio.eval.metrics import bootstrap_ci, exact_ci, mcnemar, score
from ratio.eval.systems import RetrievalSystem
from ratio.ingest.fixtures import load_corpus
from ratio.kb.index import KnowledgeBase
from ratio.retrieve.hybrid import HybridRetriever, RetrievalFilters

ROOT = Path(__file__).resolve().parents[1]
TASKS = ROOT / "eval" / "tasks"


@pytest.fixture(scope="module")
def kb() -> KnowledgeBase:
    return KnowledgeBase.build(load_corpus(ROOT / "data" / "fixtures"))


@pytest.fixture(scope="module")
def retrieval_suite() -> TaskSuite:
    return TaskSuite.load(TASKS / "04_retrieval.yaml")


def _case(case_id: str, expected: str, *, kind: str = "rule", **kw: object) -> CaseSpec:
    return CaseSpec(id=case_id, kind=kind, prompt=case_id, expected=expected, **kw)


class _Scripted:
    """A system that returns what it is told, for testing the scoring."""

    def __init__(self, name: str, answers: dict[str, str], kinds=None) -> None:
        self.name = name
        self.answers = answers
        self.kinds = kinds

    def predict(self, case: CaseSpec) -> str:
        return self.answers[case.id]


def _suite(*cases: CaseSpec, suite_id: str = "t") -> TaskSuite:
    return TaskSuite(id=suite_id, title="t", domain="d", version="1", cases=list(cases))


# -- scoring ---------------------------------------------------------------


def test_abstention_is_a_category_not_a_failure() -> None:
    """Two accuracies, because one number cannot express both things.

    A system that abstains on the hard half and is right on the rest is behaving
    *better* than one that guesses, and any single-number score says the
    opposite. So both are computed and both are printed.
    """
    suite = _suite(
        _case("a", "yes"),
        _case("b", "no"),
        _case("c", "yes"),
        _case("d", "no"),
    )
    # Right on both easy cases, abstains on both hard ones.
    system = _Scripted("s", {"a": "yes", "b": "no", "c": ABSTAIN, "d": ABSTAIN})
    result = run_suite(suite, system, bootstrap=False)
    assert result.report.answered_accuracy == 1.0
    assert result.report.end_to_end_accuracy == 0.5
    assert result.report.n_abstained == 2


def test_a_false_answer_is_counted_as_a_false_answer_before_it_is_an_error() -> None:
    """Answering wrongly and abstaining are not the same failure.

    Collapsing them into one accuracy number hides the distinction that matters
    most to a lawyer, which is not how often the system is right but whether it
    says things that are wrong.
    """
    suite = _suite(_case("a", "yes"), _case("b", "no"))
    wrong = _Scripted("w", {"a": "no", "b": "no"})
    silent = _Scripted("s", {"a": "yes", "b": ABSTAIN})
    assert run_suite(suite, wrong, bootstrap=False).report.n_false == 1
    assert run_suite(suite, silent, bootstrap=False).report.n_false == 0


def test_an_alternative_answer_counts_as_correct() -> None:
    suite = _suite(_case("a", "yes", alternatives=("maybe",)))
    assert run_suite(suite, _Scripted("s", {"a": "maybe"}), bootstrap=False).report.n_correct == 1


def test_a_system_that_raises_is_recorded_as_an_abstention_not_a_crash() -> None:
    """One bad case must not be able to suppress the rest of the report.

    A suite is a batch of a hundred questions; a system that dies on the fourth
    and takes the run with it turns a partial result into no result, and the
    reviewer learns nothing about the other ninety-six.
    """

    class Exploding:
        name = "boom"

        def predict(self, case: CaseSpec) -> str:
            raise RuntimeError("kaboom")

    suite = _suite(_case("a", "yes"), _case("b", "no"))
    result = run_suite(suite, Exploding(), bootstrap=False)
    assert len(result.outcomes) == 2
    assert all(o.predicted == ABSTAIN for o in result.outcomes)
    assert "kaboom" in (suite.cases[0].notes or "")


def test_guardrails_invert_the_scoring() -> None:
    """On a guardrail case, answering correctly is failing.

    These are the cases where the only right behaviour is to refuse: a claim
    citing a judgment that is not in the corpus, a fabricated quotation. A
    system that answers them confidently is not merely inaccurate, it is
    inventing authority, so the failure has to be recorded as one even though
    the *label* matches.
    """
    suite = _suite(
        _case("g", "__refused__", kind="verification", expect_refusal=True),
        _case("h", "__refused__", kind="verification", expect_refusal=True),
    )
    good = _Scripted("good", {"g": "__refused__", "h": ABSTAIN}, kinds=frozenset({"verification"}))
    bad = _Scripted("bad", {"g": "__refused__", "h": "supported"}, kinds=frozenset({"verification"}))
    assert run_suite(suite, good, bootstrap=False).passes
    failed = run_suite(suite, bad, bootstrap=False)
    assert not failed.passes
    assert failed.guardrail_failures == ["h"]


def test_a_system_is_only_scored_on_the_kinds_it_answers() -> None:
    """Scoring the verifier on "was this acquisition notifiable" produces a
    number with no meaning -- and a non-inert one, because it flows into
    comparisons and guardrail counts."""
    suite = _suite(_case("a", "yes", kind="rule"), _case("b", "yes", kind="retrieval"))
    system = _Scripted("rules-only", {"a": "yes"}, kinds=frozenset({"rule"}))
    result = run_suite(suite, system, bootstrap=False)
    assert len(result.outcomes) == 1
    assert result.skipped == 1
    assert "skipped" in result.render()


def test_a_suite_with_no_matching_cases_is_not_a_clean_sheet() -> None:
    """A suite half of which was skipped is not a suite that passed."""
    suite = _suite(_case("a", "yes", kind="retrieval"))
    result = run_suite(suite, _Scripted("r", {}, kinds=frozenset({"rule"})), bootstrap=False)
    assert result.outcomes == []
    assert "Not evaluated" in result.render()
    assert result.skipped == 1


def test_an_unknown_case_kind_is_rejected_at_load(tmp_path: Path) -> None:
    """A typo in ``kind`` must announce itself rather than silently
    contributing nothing to every score in the file."""
    path = tmp_path / "bad.yaml"
    path.write_text(
        "id: x\ntitle: x\ndomain: d\nversion: '1'\n"
        "cases:\n  - id: c\n    kind: retrival\n    prompt: p\n    expected: e\n"
    )
    with pytest.raises(ValueError, match="unknown kind"):
        TaskSuite.load(path)


def test_comparison_refuses_to_compare_different_case_sets() -> None:
    a = _suite(_case("a", "yes"), suite_id="s")
    b = _suite(_case("a", "yes"), _case("z", "no"), suite_id="s")
    with pytest.raises(Exception):
        mcnemar(
            run_suite(a, _Scripted("x", {"a": "yes"}), bootstrap=False).outcomes,
            run_suite(b, _Scripted("y", {"a": "no", "z": "no"}), bootstrap=False).outcomes,
        )


# -- baselines -------------------------------------------------------------


def test_the_baselines_are_reported_so_a_score_can_be_judged() -> None:
    """An accuracy is meaningless without the floor it has to clear.

    Most published benchmarks for legal classification are cleared by a
    majority-class baseline, and the fact is usually left out.
    """
    suite = _suite(_case("a", "yes"), _case("b", "yes"), _case("c", "no"))
    abstaining = run_suite(suite, AbstainSystem(), bootstrap=False)
    majority = run_suite(suite, MajoritySystem([suite]), bootstrap=False)
    assert abstaining.report.n_answered == 0
    assert abstaining.report.answered_accuracy == 0.0
    assert majority.report.end_to_end_accuracy == pytest.approx(2 / 3)
    # The majority baseline is fitted on the suite it is scored on, which
    # flatters it. That is the point: a system that cannot beat a generously
    # fitted baseline has no demonstrated signal at all.
    assert majority.report.n_correct == 2


def test_the_interval_is_exact_not_the_degenerate_bootstrap() -> None:
    """A perfect score on a small suite must not be reported as precise.

    The percentile bootstrap has zero width when the observed proportion is 0 or
    1, so an 8/8 suite reported a 95% interval of [1.000, 1.000] -- the most
    confident-looking result available, produced by the smallest amount of
    evidence, in the artefact a reviewer is most likely to quote. The exact
    interval for the same data is [0.631, 1.000].
    """
    suite = _suite(*[_case(str(i), "yes") for i in range(8)])
    result = run_suite(suite, _Scripted("s", {str(i): "yes" for i in range(8)}))
    assert result.ci[0] == pytest.approx(0.6306, abs=1e-3), result.ci
    assert result.ci[1] == 1.0
    # And it is genuinely the exact interval, not the bootstrap.
    assert result.ci != bootstrap_ci(result.outcomes, metric="end_to_end")


def test_the_exact_interval_is_symmetric_under_relabelling() -> None:
    """CI(x, n) must be the mirror of CI(n-x, n).

    An interval that is not symmetric is not a Clopper-Pearson interval, and the
    bisections that produce it have their comparisons the wrong way round -- a
    fault that is invisible on the all-correct and all-wrong cases, which are
    exactly the cases a task suite produces.
    """
    for n in (6, 8, 15):
        for x in range(n + 1):
            lo, hi = exact_ci(x, n)
            mirrored = (1 - exact_ci(n - x, n)[1], 1 - exact_ci(n - x, n)[0])
            assert (lo, hi) == pytest.approx(mirrored, abs=1e-6), (x, n)
            assert lo <= x / n <= hi, f"the interval excludes the estimate: {x}/{n}"


def test_an_empty_suite_reports_no_interval_rather_than_a_perfect_one() -> None:
    """Zero cases is not a clean sheet.

    A system that abstains from a suite entirely must never render as a system
    with a 100% record.
    """
    result = run_suite(_suite(), _Scripted("s", {}), bootstrap=False)
    assert result.outcomes == []
    assert "Not evaluated" in result.render()
    assert score([]).end_to_end_accuracy == 0.0


# -- the retrieval abstention gate ----------------------------------------


def test_the_rrf_score_cannot_be_the_basis_of_an_abstention(
    kb: KnowledgeBase, retrieval_suite: TaskSuite
) -> None:
    """Why the gate is not a threshold on the retriever's score.

    Reciprocal rank fusion keeps only the *ordering* of each ranker, so the top
    hit of a single ranker always scores 1/(k+1) whatever the corpus contained.
    The threshold this system originally used was expressed in that unit and
    therefore could essentially never fire: the confabulation case scored the
    same 0.0164 as a case with a perfect answer. A safety control that cannot
    fire is worse than none, because it is reported as one.
    """
    retriever = HybridRetriever(kb)
    scores = []
    for case in retrieval_suite.cases:
        results = retriever.search(case.query or case.prompt, limit=1)
        if results:
            scores.append(results[0].score)
    assert scores, "no retrieval case returned anything"
    assert max(scores) - min(scores) < 1e-6, (
        "the fused scores are not near-constant, so they may be usable after all"
    )


def test_the_gate_abstains_when_the_corpus_has_nothing(
    kb: KnowledgeBase, retrieval_suite: TaskSuite
) -> None:
    """The confabulation the gate exists to stop.

    No judgment in the corpus construes the exemption certificate. Before the
    gate was fixed, this question returned a judgment about structured interests
    as a confident answer, because it shares the words "action" and
    "notifiable" with the question -- and the two words it shares are precisely
    the two that carry no information.
    """
    system = RetrievalSystem(kb)
    case = next(
        c for c in retrieval_suite.cases if c.id == "retrieve-exemption-certificate-not-covered"
    )
    assert system.predict(case) == ABSTAIN


def test_the_gate_does_not_refuse_when_the_corpus_has_something(
    kb: KnowledgeBase, retrieval_suite: TaskSuite
) -> None:
    """The other half, and the harder one to keep.

    A gate tuned to refuse confabulations is trivially satisfied by refusing
    everything, and a retriever that refuses everything is not a safe system, it
    is a broken one.
    """
    system = RetrievalSystem(kb)
    positive = [c for c in retrieval_suite.cases if c.expected != ABSTAIN]
    assert len(positive) >= 4
    for case in positive:
        assert system.predict(case) != ABSTAIN, f"{case.id} was refused"


def test_the_gate_separates_the_two_families_with_a_margin(
    kb: KnowledgeBase, retrieval_suite: TaskSuite
) -> None:
    """The margin is the thing to assert, not the pass rate.

    Six cases is not a calibration, so the suite cannot show that the threshold
    is right -- but it can show how much room there is before adding a case
    changes the decision. If a new case closes this gap, the suite should fail
    loudly and someone should re-derive the threshold rather than discover the
    problem in production.
    """
    system = RetrievalSystem(kb)
    retriever = HybridRetriever(kb)

    def coverage(case: CaseSpec) -> float:
        query = case.query or case.prompt
        results = retriever.search(query, filters=RetrievalFilters(), limit=1, explain=True)
        if not results:
            return 0.0
        return system.coverage(query, results[0].matched_terms)

    positives = [coverage(c) for c in retrieval_suite.cases if c.expected != ABSTAIN]
    refusals = [coverage(c) for c in retrieval_suite.cases if c.expected == ABSTAIN]
    assert min(positives) > max(refusals), (
        f"the families overlap: lowest positive {min(positives):.3f}, "
        f"highest refusal {max(refusals):.3f}"
    )
    assert min(positives) - max(refusals) > 0.15, (
        f"the margin has closed to {min(positives) - max(refusals):.3f}; the "
        "threshold needs re-deriving rather than nudging"
    )
    assert system.min_coverage > max(refusals)
    assert system.min_coverage < min(positives)


def test_a_term_the_corpus_never_contains_drives_coverage_down(
    kb: KnowledgeBase,
) -> None:
    """An absent term must weigh as much as the rarest term that does exist.

    Weighting it at zero -- its IDF, since it has no postings -- inverts the
    measurement: the less the corpus had to say about a question, the better
    covered the question would look. This is the single most likely way for the
    gate to be quietly broken by a later "simplification" of the IDF weighting.
    """
    system = RetrievalSystem(kb)
    index = kb.index
    assert index.max_idf() > index.idf("the") if index.idf("the") else True
    assert index.idf("nonexistenttermzzz") == 0.0
    # A question made half of words the corpus has never seen cannot score above
    # roughly a half, however well the other half matches.
    matched = {"action": 1.0}
    assert system.coverage("action nonexistenttermzzz", matched) < 0.5
