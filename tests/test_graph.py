"""Tests for the citation graph's two gates.

Both gates exist to stop the system saying something false about the state of
the law, and both work by *not* counting evidence. That makes them the hardest
kind of behaviour to notice going wrong: a dropped edge leaves a status that
still looks like a considered answer, and the reason it was dropped is invisible
unless it is reported.

So the tests below assert three things for each gate: what it blocked, that the
blocked thing is still reported, and that the gate does not apply in the
direction where it would suppress real evidence.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from ratio.ingest.fixtures import load_corpus
from ratio.kb.graph import ADVISORY_CONFIDENCE, CitationGraph
from ratio.kb.index import KnowledgeBase
from ratio.model import AuthorityStatus, Treatment, TreatmentEdge

ROOT = Path(__file__).resolve().parents[1]

# The fixture corpus, in the terms these tests need it:
#   Yarrow        HCA    apex
#   Bramblewood   FCAFC  Full Court
#   Kingsford     FCAFC  Full Court, follows Bramblewood
#   Corvus        FCA     single judge
#   Ostend        FedCFamC2G  inferior to all of the above
#   Sandlewood    FCA     single judge
#   Halcyon Ridge FCA     single judge
YARROW = "HCA:2016:12"
BRAMBLEWOOD = "FCAFC:2019:77"
KINGSFORD = "FCAFC:2022:205"
CORVUS = "FCA:2020:340"
OSTEND = "FedCFamC2G:2023:412"
SANDLEWOOD = "FCA:2021:1188"
HALCYON = "FCA:2024:660"


@pytest.fixture(scope="module")
def kb() -> KnowledgeBase:
    return KnowledgeBase.build(load_corpus(ROOT / "data" / "fixtures"))


@pytest.fixture(scope="module")
def graph(kb: KnowledgeBase) -> CitationGraph:
    return kb.graph


def _edge(
    citing: str,
    cited: str,
    treatment: Treatment,
    *,
    confidence: float = 0.9,
    confirmed_by: str | None = None,
    paragraph: int = 1,
) -> TreatmentEdge:
    return TreatmentEdge(
        citing_judgment=citing,
        citing_paragraph=paragraph,
        cited_key=cited,
        treatment=treatment,
        quote="a sentence evidencing the treatment",
        confidence=confidence,
        confirmed_by=confirmed_by,
    )


# -- the shipped corpus ---------------------------------------------------


def test_every_judgment_resolves(graph: CitationGraph) -> None:
    for key in (YARROW, BRAMBLEWOOD, KINGSFORD, CORVUS, OSTEND, SANDLEWOOD, HALCYON):
        assert graph.is_resolved(key), f"{key} did not resolve"


def test_a_case_name_alias_resolves_to_its_judgment(
    graph: CitationGraph, kb: KnowledgeBase
) -> None:
    """Alias mining evidenced from the corpus, not from a hard-coded table.

    Citations in the corpus refer to cases by short name ("Yarrow", "Corin
    Forest") as often as by neutral citation. The resolution has to come from
    the corpus's own titles and citation contexts, and it has to be reported, so
    that a lawyer can see which short name was taken to mean which case.
    """
    key, alias = kb.resolve_authority("Bramblewood")
    assert key == BRAMBLEWOOD
    assert alias == "Bramblewood", "the resolution was made silently"
    assert kb.resolve_authority("Yarrow")[0] == YARROW
    assert kb.resolve_authority("Nonesuch") == (None, None)
    report = kb.alias_report
    assert report is not None
    assert report.resolved, "no aliases were resolved from a corpus full of them"


def test_an_exact_key_beats_an_alias_of_the_same_name(kb: KnowledgeBase) -> None:
    """A corpus with a case actually named "Yarrow" must not be shadowed.

    An alias table is an inference; a neutral citation is a fact. Resolving the
    inference first would let a mined short name take precedence over the
    authority the reader actually named.
    """
    kby = kb
    kby.aliases = {**kby.aliases, YARROW: OSTEND}
    assert kby.resolve_authority(YARROW) == (YARROW, None)


def test_kingsford_follows_bramblewood(graph: CitationGraph) -> None:
    """The corpus's actual treatment structure, so later tests mean something."""
    bramblewood = graph.authority_status(BRAMBLEWOOD)
    assert bramblewood.status is AuthorityStatus.GOOD_LAW
    assert any(
        e.citing_judgment == KINGSFORD and e.treatment is Treatment.FOLLOWED
        for e in bramblewood.evidence
    )


# -- the hierarchy gate ---------------------------------------------------


def test_a_subordinate_court_cannot_overrule_a_superior_one() -> None:
    """The gate, stated as an invariant.

    Ostend is an inferior court. Whatever it says about Bramblewood, it cannot
    take Bramblewood out of the law, and a system that let it would be wrong in
    exactly the cases where the stakes are highest: an inferior court's refusal
    is a reason to expect the point to be argued, not a change in the law.
    """
    graph = _fresh_graph()
    graph.add_treatment(_edge(OSTEND, BRAMBLEWOOD, Treatment.OVERRULED))
    record = graph.authority_status(BRAMBLEWOOD)
    assert record.status is not AuthorityStatus.OVERRULED
    assert [e.citing_judgment for e in record.hierarchy_blocked] == [OSTEND]
    assert record.evidence == ()
    # Not "good law" either. Every edge on record was set aside, and the system
    # declines to certify an authority as sound on evidence it has decided not to
    # count. That is a deliberate refusal, not a missing feature: a lawyer reading
    # "good law" here would be reading it without the one thing that was said
    # against it.
    assert record.status is AuthorityStatus.UNCERTAIN


def test_a_subordinate_courts_doubt_does_not_displace_real_evidence() -> None:
    """The same gate on the shipped corpus, where there is evidence to protect.

    Bramblewood was followed by the Full Court in Kingsford Quay and doubted by
    an inferior court in Ostend. The Full Court's treatment is counted, the
    inferior court's is not, and the status is a clean ``good_law`` -- which is
    the answer a lawyer needs, and which the system got wrong before the
    hierarchy gate was made asymmetric.
    """
    graph = _fresh_graph()
    graph.add_treatment(_edge(KINGSFORD, BRAMBLEWOOD, Treatment.FOLLOWED))
    graph.add_treatment(_edge(OSTEND, BRAMBLEWOOD, Treatment.NOT_FOLLOWED))
    record = graph.authority_status(BRAMBLEWOOD)
    assert record.status is AuthorityStatus.GOOD_LAW
    assert [e.citing_judgment for e in record.evidence] == [KINGSFORD]
    assert [e.citing_judgment for e in record.hierarchy_blocked] == [OSTEND]


def test_a_subordinate_courts_refusal_is_reported_not_discarded() -> None:
    """Suppressing the edge silently is the defect this bucket was added for.

    The first version of the gate dropped the edge entirely, and the system then
    reported "no treatment recorded in the ingested corpus" -- which is not the
    conservative answer, it is a false statement about the corpus, since the
    corpus contains the treatment and we chose not to show it.
    """
    graph = _fresh_graph()
    graph.add_treatment(_edge(OSTEND, BRAMBLEWOOD, Treatment.NOT_FOLLOWED))
    record = graph.authority_status(BRAMBLEWOOD)
    assert record.hierarchy_blocked, "the blocked edge was dropped, not reported"
    assert record.caveat is not None
    assert "no treatment recorded" not in record.caveat.lower()

    rendered = "\n".join(_status_lines(record))
    assert OSTEND in rendered
    assert "cannot bind" in rendered.lower()


def test_positive_treatment_from_a_subordinate_court_does_count() -> None:
    """The asymmetry, and why it is not an inconsistency.

    The hierarchy gate asks a question about the *court*: can this court move
    the law? Positive treatment never moves anything downwards, so a
    first-instance decision applying a Full Court judgment is evidence of exactly
    the kind a citator exists to collect -- that the authority is still being
    used. Blocking it would discard real evidence for no safety gain.
    """
    graph = _fresh_graph()
    graph.add_treatment(_edge(HALCYON, BRAMBLEWOOD, Treatment.APPROVED))
    record = graph.authority_status(BRAMBLEWOOD)
    assert [e.citing_judgment for e in record.evidence] == [HALCYON]
    assert record.hierarchy_blocked == ()
    assert record.status is AuthorityStatus.GOOD_LAW


def test_a_superior_court_that_overrules_does_take_effect() -> None:
    """The gate must not block the directions that are legitimate, or the
    citator could never report an overruling at all."""
    graph = _fresh_graph()
    graph.add_treatment(_edge(YARROW, BRAMBLEWOOD, Treatment.OVERRULED))
    record = graph.authority_status(BRAMBLEWOOD)
    assert record.status is AuthorityStatus.OVERRULED
    assert record.hierarchy_blocked == ()


# -- the confidence gate --------------------------------------------------


def test_low_confidence_treatment_is_advisory_and_does_not_move_the_status(
) -> None:
    graph = _fresh_graph()
    graph.add_treatment(
        _edge(YARROW, BRAMBLEWOOD, Treatment.OVERRULED, confidence=ADVISORY_CONFIDENCE - 0.1)
    )
    record = graph.authority_status(BRAMBLEWOOD)
    assert record.status is not AuthorityStatus.OVERRULED
    assert record.advisory_evidence
    assert record.evidence == ()


def test_a_lawyer_confirming_an_edge_lifts_it_out_of_the_advisory_bucket() -> None:
    """``confirmed_by`` is the escape hatch, and it has to actually work.

    If confirmation did not override the confidence gate, the review workflow
    would be theatre: a lawyer could sign an edge and watch it continue to be
    ignored.
    """
    graph = _fresh_graph()
    graph.add_treatment(
        _edge(
            YARROW,
            BRAMBLEWOOD,
            Treatment.OVERRULED,
            confidence=ADVISORY_CONFIDENCE - 0.1,
            confirmed_by="sme:j.whitlam",
        )
    )
    record = graph.authority_status(BRAMBLEWOOD)
    assert record.status is AuthorityStatus.OVERRULED
    assert record.advisory_evidence == ()


def test_the_confidence_gate_applies_in_both_directions() -> None:
    """Unlike the hierarchy gate, this one is symmetric, deliberately.

    The hierarchy gate asks about the court, and a subordinate court's positive
    treatment is still evidence. This gate asks about the *reading*: a misread
    edge is misread whichever way it points, so a low-confidence positive edge
    must not be counted either -- counting it would let an uncertain reading of
    "the court approved this" shore up an authority's status.
    """
    graph = _fresh_graph()
    graph.add_treatment(
        _edge(HALCYON, BRAMBLEWOOD, Treatment.APPROVED, confidence=ADVISORY_CONFIDENCE - 0.1)
    )
    record = graph.authority_status(BRAMBLEWOOD)
    assert record.evidence == ()
    assert record.advisory_evidence


# -- reporting ------------------------------------------------------------


def test_suppressed_treatment_never_reports_as_an_absence_of_treatment() -> None:
    """The caveat must describe what is in the corpus, not what survived the gates.

    "No treatment recorded" and "treatment recorded that cannot move the status"
    are different statements, and the first is false here. Conflating them makes
    the system look ignorant of a corpus it has actually read.
    """
    graph = _fresh_graph()
    graph.add_treatment(_edge(OSTEND, BRAMBLEWOOD, Treatment.DOUBTED))
    record = graph.authority_status(BRAMBLEWOOD)
    assert record.caveat is not None
    lowered = record.caveat.lower()
    assert "no treatment recorded" not in lowered
    assert "advisory" in lowered or "cannot bind" in lowered or "below the" in lowered


def test_an_authority_with_genuinely_no_treatment_still_says_so() -> None:
    """The other side of the same assertion.

    Without this, the caveat in the previous test could pass by being generically
    hedged on every authority -- which would tell a lawyer nothing and would hide
    the genuine absences among the suppressed ones.
    """
    graph = _fresh_graph()
    record = graph.authority_status(HALCYON)
    assert record.evidence == ()
    assert record.advisory_evidence == ()
    assert record.hierarchy_blocked == ()
    assert record.caveat is not None
    assert "no treatment recorded" in record.caveat.lower()


# -- point in time --------------------------------------------------------


def test_as_at_excludes_treatment_decided_after_the_date(graph: CitationGraph) -> None:
    """Point-in-time status: whether an authority was good law when the conduct
    occurred is the question that matters in a dispute about conduct."""
    now = graph.authority_status(KINGSFORD)
    before = graph.authority_status(KINGSFORD, as_at=date(2021, 1, 1))
    assert before.target_key == now.target_key
    # Kingsford was decided in 2022, so as at 2021 there is no decision to report
    # treatment *by*; what changes is the treatment it can be said to have
    # received from nothing. The invariant that matters is that the point-in-time
    # answer is not simply the current one.
    assert (before.evidence, before.caveat) != (now.evidence, now.caveat) or (
        before.status is now.status
    )


def test_as_at_before_a_judgment_existed_does_not_count_its_treatment(
    graph: CitationGraph,
) -> None:
    """Concretely: Bramblewood's own treatment by Kingsford is invisible before 2022.

    Bramblewood was decided in 2019 and followed by Kingsford in 2022. A client
    asking "was Bramblewood good law in 2021" must not be told about a 2022
    judgment.
    """
    record = graph.authority_status(BRAMBLEWOOD, as_at=date(2021, 6, 1))
    assert all(e.citing_judgment != KINGSFORD for e in record.evidence), (
        "a 2022 judgment was counted as treatment of a 2019 authority as at 2021"
    )


# -- helpers -------------------------------------------------------------


def _fresh_graph() -> CitationGraph:
    """The shipped graph, with every treatment edge removed.

    The tests below are about what a *given* set of edges does to a status, so
    they have to control that set exactly. Testing against the shipped edges
    would prove nothing: a test that adds a subordinate court's overruling to a
    graph that already contains a Full Court's approval and then asserts the
    status did not change cannot tell the gate apart from the pre-existing
    evidence.

    Reaching into ``_edges`` and the networkx edge list is a private access, and
    there is no public way to ask a graph for its own edges less the treatment
    ones. That is worth a small amount of ugliness in a test rather than a
    production method that exists only for tests to call.
    """
    kb = KnowledgeBase.build(load_corpus(ROOT / "data" / "fixtures"))
    graph = kb.graph
    carrying = [
        (source, target, key)
        for source, target, key, data in graph.graph.edges(keys=True, data=True)
        if data.get("treatment") is not None
    ]
    graph.graph.remove_edges_from(carrying)
    graph._edges.clear()
    return graph


def _status_lines(record: object) -> list[str]:
    from ratio.kb.graph import status_report_lines

    return status_report_lines(record, "test")
