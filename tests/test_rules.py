"""Tests for the rule engine.

The engine is where a modelling error becomes a wrong legal answer rather than a
bad ranking, so most of what is asserted here is about *refusing* to answer: gaps
must be recorded, exceptions must defeat rather than conjoin, and a ruleset that
has been edited after approval must not run at all.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from ratio.ingest.fixtures import load_corpus
from ratio.model import (
    Approval,
    AuthorityRef,
    NodeKind,
    Predicate,
    RuleNode,
    RuleSet,
)
from ratio.rules.engine import RuleEngine, RuleEvaluationError, validate_ruleset

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def ruleset() -> RuleSet:
    bundle = load_corpus(ROOT / "data" / "fixtures")
    return next(r for r in bundle.rulesets if r.id == "firb.notifiable_action")


@pytest.fixture(scope="module")
def engine(ruleset: RuleSet) -> RuleEngine:
    return RuleEngine(ruleset)


def _complete_facts(**overrides: object) -> dict[str, object]:
    """Every fact the shipped ruleset needs, so a test can vary exactly one.

    Written out rather than guessed, because a test that varies "the holding"
    while leaving four other facts absent is a test of the gap logic, not of the
    holding logic -- and it would pass for the wrong reason.
    """
    facts: dict[str, object] = {
        "action": {
            "is_significant_action": True,
            "covered_by_exemption_certificate": False,
            "is_exempt_action": False,
        },
        "acquirer": {
            "is_foreign_person": True,
            "holding_percentage": 25.0,
            "aggregated_holding_percentage": 0.0,
            "has_board_nomination_rights": False,
            "can_determine_financial_and_operating_policies": False,
            "is_associate_of_foreign_person": False,
        },
    }
    for key, value in overrides.items():
        section, _, field = key.partition("__")
        facts.setdefault(section, {})
        assert isinstance(facts[section], dict)
        facts[section][field] = value
    return facts


# -- gaps and abstention ---------------------------------------------------


def test_a_missing_fact_is_recorded_as_a_gap(engine: RuleEngine) -> None:
    """The single most important behaviour in the system.

    Before this worked, the engine reported a conclusion for a limb nobody had
    supplied, having silently treated the absent value as false. The conclusion
    was wrong and, worse, it was stated with the same confidence as a correct
    one.
    """
    facts = _complete_facts()
    del facts["acquirer"]["holding_percentage"]  # type: ignore[attr-defined]
    tree = engine.evaluate(facts)
    assert "acquirer.holding_percentage" in tree.gaps
    assert tree.conclusion == "undetermined"


def test_an_entirely_empty_fact_base_yields_gaps_not_a_conclusion(
    engine: RuleEngine,
) -> None:
    tree = engine.evaluate({})
    assert tree.gaps, "an empty fact base produced no gaps"
    assert tree.conclusion == "undetermined"


def test_a_complete_fact_base_yields_no_gaps(engine: RuleEngine) -> None:
    """The other half of the guard: gaps must be empty when nothing is missing.

    Without this, a gap list that is populated unconditionally -- by recording
    every predicate rather than every *absent* one -- would make the system
    abstain on everything and the first test would still pass.
    """
    tree = engine.evaluate(_complete_facts())
    assert tree.gaps == (), f"unexpected gaps: {tree.gaps}"


def test_gaps_are_deduplicated(engine: RuleEngine) -> None:
    """A fact consulted on several branches is one gap, not four."""
    tree = engine.evaluate({})
    assert len(tree.gaps) == len(set(tree.gaps))


# -- defeasibility ---------------------------------------------------------


def test_an_exception_defeats_even_when_every_element_is_satisfied(
    engine: RuleEngine,
) -> None:
    """The distinction the whole encoding rests on.

    If "unless an exemption certificate is in force" were encoded as another
    conjunct, a case with the certificate would come out ``not_satisfied`` for
    the wrong reason -- and the proof tree would say the acquisition was not a
    significant action, which is a different and false statement about the facts.
    """
    satisfied = engine.evaluate(_complete_facts())
    assert satisfied.conclusion == "applies"
    assert satisfied.defeaters_fired == ()

    defeated = engine.evaluate(
        _complete_facts(action__covered_by_exemption_certificate=True)
    )
    assert defeated.conclusion == "does_not_apply"
    assert "exemption_certificate_applies" in defeated.defeaters_fired


def test_the_defeater_is_named_by_its_own_id(engine: RuleEngine) -> None:
    """``defeaters_fired`` must name the defeater, not the defeated rule.

    Recording the defeated parent here made every exception look unexercised, so
    the coverage report announced defeaters as never swept by the case generator
    while they were in fact firing on every run -- the one defect that analysis
    exists to find was the one it could not see.
    """
    tree = engine.evaluate(
        _complete_facts(action__covered_by_exemption_certificate=True)
    )
    for fired in tree.defeaters_fired:
        node = next(n for n in engine.ruleset.nodes if n.id == fired)
        assert node.kind is NodeKind.EXCEPTION, (
            f"{fired!r} is recorded as a defeater but is a {node.kind.value} node"
        )


def test_a_fired_exception_does_not_hide_behind_an_unsatisfied_element(
    engine: RuleEngine,
) -> None:
    """A conclusion the facts defeat stays defeated, whatever else is missing.

    The counterpart to the test above. Downgrading an unevaluated defeater to
    "undetermined" must not be implemented so broadly that it swallows a
    definite answer: here the certificate covers the action, so the acquisition
    is not notifiable, and an absent fact elsewhere does not make that unclear.
    """
    facts = _complete_facts(action__covered_by_exemption_certificate=True)
    del facts["acquirer"]["holding_percentage"]  # type: ignore[attr-defined]
    tree = engine.evaluate(facts)
    assert tree.conclusion == "does_not_apply"
    assert tree.defeaters_fired == ("exemption_certificate_applies",)
    assert "acquirer.holding_percentage" in tree.gaps


# -- structure -------------------------------------------------------------


def test_not_node_inverts_its_child(ruleset: RuleSet) -> None:
    leaf = RuleNode(
        id="p",
        kind=NodeKind.LEAF,
        label="p",
        statement="a fact",
        predicate=Predicate(field="x", op="eq", value=True),
        provenance=[AuthorityRef(kind="statute", provision_key="statute:test 1:1")],
    )
    not_node = RuleNode(
        id="n",
        kind=NodeKind.NOT,
        label="not p",
        statement="not a fact",
        children=["p"],
    )
    rs = RuleSet(
        id="t",
        title="t",
        question="q",
        domain="d",
        version="1",
        root="n",
        effective_from=date(1975, 5, 11),
        nodes=(not_node, leaf),
    )
    built = RuleEngine(rs)
    assert built.evaluate({"x": True}).conclusion == "does_not_apply"
    assert built.evaluate({"x": False}).conclusion == "applies"


def test_validation_rejects_a_leaf_without_provenance(ruleset: RuleSet) -> None:
    """Every factual test must point at the provision or judgment requiring it.

    An unprovenanced leaf is an assertion about the law with nothing behind it,
    and it is invisible in the running system -- the engine will happily evaluate
    it and return an answer.
    """
    broken = ruleset.model_copy(deep=True)
    for node in broken.nodes:
        if node.kind is NodeKind.LEAF:
            broken.nodes = tuple(
                n.model_copy(update={"provenance": ()}) if n.id == node.id else n
                for n in broken.nodes
            )
            break
    issues = validate_ruleset(broken)
    assert any("no provenance" in issue for issue in issues), issues


def test_validation_rejects_an_unreachable_node(ruleset: RuleSet) -> None:
    orphan = RuleNode(
        id="orphan",
        kind=NodeKind.LEAF,
        label="orphan",
        statement="a node nothing reaches",
        predicate=Predicate(field="x", op="eq", value=1),
        provenance=[AuthorityRef(kind="statute", provision_key="statute:test 1:1")],
    )
    broken = ruleset.model_copy(deep=True)
    broken.nodes = (*broken.nodes, orphan)
    assert any("unreachable" in issue for issue in validate_ruleset(broken))


def test_validation_rejects_an_exception_attached_to_two_parents(
    ruleset: RuleSet,
) -> None:
    broken = ruleset.model_copy(deep=True)
    exception = next(n for n in broken.nodes if n.kind is NodeKind.EXCEPTION)
    root_id = broken.root
    broken.nodes = tuple(
        n.model_copy(update={"children": (*n.children, exception.id)})
        if n.id == root_id
        else n
        for n in broken.nodes
    )
    assert any("attached to 2 parents" in issue for issue in validate_ruleset(broken))


def test_engine_refuses_to_run_an_invalid_ruleset(ruleset: RuleSet) -> None:
    broken = ruleset.model_copy(deep=True)
    broken.root = "does_not_exist"
    with pytest.raises(RuleEvaluationError, match="failed validation"):
        RuleEngine(broken)


def test_a_cycle_is_a_hard_error_not_a_hang(ruleset: RuleSet) -> None:
    """A cycle must raise. Recursing into one would be an unbounded loop in a
    service that answers legal questions, which fails in the least debuggable
    way available."""
    broken = ruleset.model_copy(deep=True)
    root_id = broken.root
    broken.nodes = tuple(
        n.model_copy(update={"children": (*n.children, root_id)})
        if n.id == root_id
        else n
        for n in broken.nodes
    )
    with pytest.raises(RuleEvaluationError):
        RuleEngine(broken).evaluate(_complete_facts())


# -- approval binding ------------------------------------------------------


def test_editing_an_approved_ruleset_invalidates_its_approval(
    ruleset: RuleSet,
) -> None:
    """The mechanism behind "experts can inspect and govern what they use".

    An approval is a hash over the artefact's canonical form. If editing the
    encoded rule left the approval attached, an expert's sign-off would transfer
    to text they never saw, and the governance control would be decorative.
    """
    approved = ruleset.model_copy(deep=True)
    approved.status = "approved"
    digest = Approval.hash_of(approved.canonical_payload())
    approved.approvals = [
        Approval(
            approver="Test Approver",
            role="Partner, M&A",
            approved_at=datetime(2026, 1, 1, tzinfo=UTC),
            artefact_hash=digest,
        )
    ]
    assert approved.is_approved
    assert RuleEngine(approved).ruleset.current_approval is not None

    edited = approved.model_copy(deep=True)
    leaf = next(n for n in edited.nodes if n.kind is NodeKind.LEAF)
    edited.nodes = tuple(
        n.model_copy(update={"statement": "a materially different legal test"})
        if n.id == leaf.id
        else n
        for n in edited.nodes
    )
    assert edited.current_approval is None
    assert not edited.is_approved
    assert any(
        "edited since sign-off" in issue for issue in validate_ruleset(edited)
    ), validate_ruleset(edited)


def test_an_unapproved_ruleset_still_runs_but_is_not_approved(
    ruleset: RuleSet,
) -> None:
    """Draft rulesets are usable in development and must not be mistaken for
    approved ones. The distinction lives on the artefact, not in the caller."""
    assert not ruleset.is_approved
    assert RuleEngine(ruleset).evaluate(_complete_facts()).conclusion == "applies"


# -- provenance that resolves -------------------------------------------------


def test_the_shipped_ruleset_resolves_against_the_shipped_corpus(
    ruleset: RuleSet,
) -> None:
    """The regression guard for a defect that shipped.

    Every statutory node in the first revision of this ruleset cited
    ``statute:Foreign Acquisitions and Takeovers Act 1975 1975:18`` -- the corpus
    key with the year duplicated and a space where the colon belongs. Structural
    validation passed it, because structural validation can only ask whether a
    node *has* provenance, not whether the provenance names anything. So the
    entire statutory basis of the only encoded rule terminated in a string that
    pointed at nothing, and ``ratio rules validate`` said "ok".

    That is the invariant's own failure mode wearing the mechanism that is
    supposed to prevent it, so it is asserted here against the real corpus rather
    than a fixture.
    """
    from ratio.rules.engine import provenance_problems

    bundle = load_corpus(ROOT / "data" / "fixtures")
    problems = provenance_problems(
        ruleset,
        judgment_ids={j.id for j in bundle.judgments},
        provision_keys=set(_provision_keys(bundle)),
    )
    assert problems == [], problems


def _provision_keys(bundle: object) -> list[str]:
    """The corpus's own provision keys, as the KB builds them.

    Taken from the knowledge base rather than reconstructed by string
    formatting, because a test that builds the expected key with the same
    expression as the code under test proves nothing about either.
    """
    from ratio.kb.index import KnowledgeBase

    return list(KnowledgeBase.build(bundle).provisions)  # type: ignore[arg-type]


def test_a_provision_key_the_corpus_does_not_hold_is_reported(
    ruleset: RuleSet,
) -> None:
    """A near-miss key is still a key nobody checked."""
    from ratio.rules.engine import provenance_problems

    broken = _with_provenance(
        ruleset,
        AuthorityRef.model_construct(kind="statute", provision_key="statute:No Such Act 1999:1"),
    )
    problems = provenance_problems(
        broken, judgment_ids=set(), provision_keys=set()
    )
    assert any("No Such Act" in p for p in problems), problems


def test_a_judgment_a_node_cites_but_the_corpus_lacks_is_reported(
    ruleset: RuleSet,
) -> None:
    from ratio.rules.engine import provenance_problems

    broken = _with_provenance(
        ruleset,
        AuthorityRef.model_construct(
            kind="judgment_paragraph", judgment_id="au/nonesuch/1999/1", paragraph=4
        ),
    )
    problems = provenance_problems(
        broken, judgment_ids=set(), provision_keys=set()
    )
    assert any("au/nonesuch/1999/1" in p for p in problems), problems


def test_an_expert_statement_with_no_proposition_names_nothing(
    ruleset: RuleSet,
) -> None:
    """``expert_statement`` is the one provenance kind that does not point into
    the corpus, so it has to point at something else -- a proposition id. Without
    one it is the phrase "because the expert said so", which is the thing the
    whole provenance model exists to make impossible."""
    from ratio.rules.engine import provenance_problems

    broken = _with_provenance(
        ruleset, AuthorityRef.model_construct(kind="expert_statement")
    )
    problems = provenance_problems(
        broken, judgment_ids=set(), provision_keys=set()
    )
    assert any("no proposition id" in p for p in problems), problems


def _with_provenance(ruleset: RuleSet, ref: AuthorityRef) -> RuleSet:
    """Rebuild ``ruleset`` with ``ref`` appended to its first leaf.

    Rebuilt rather than mutated because ``nodes`` is a tuple on a frozen-ish
    model; appending would raise, and the fix that "works" by mutating in place is
    a different bug.
    """
    leaf = next(n for n in ruleset.nodes if n.kind is NodeKind.LEAF)
    return ruleset.model_copy(
        update={
            "nodes": tuple(
                n.model_copy(update={"provenance": (*n.provenance, ref)})
                if n.id == leaf.id
                else n
                for n in ruleset.nodes
            )
        }
    )
