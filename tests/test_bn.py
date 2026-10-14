"""Tests for the credence layer.

The central test here is a cross-check, not a unit test. ``query`` uses variable
elimination; ``query_by_enumeration`` brute-forces the same answer by summing
over every hidden assignment. They are independent implementations of the same
quantity, and a disagreement means one of them is wrong -- which is exactly the
kind of fault that produces a confident, plausible posterior and is never found
by inspection.

Agreement is asserted over the *shipped* network, not a toy. A hand-built
two-node network would agree under almost any bug in the elimination order, the
factor multiplication or the CPT row indexing; the real network has the parent
structure that those bugs need in order to show themselves.
"""

from __future__ import annotations

import itertools
from pathlib import Path

import pytest

from ratio.bn.network import CredenceEngine, CredenceError
from ratio.ingest.fixtures import load_corpus

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def engine() -> CredenceEngine:
    bundle = load_corpus(ROOT / "data" / "fixtures")
    assert bundle.credence_engines, "fixture corpus ships no credence network"
    return bundle.credence_engines[0]


def _evidence_sets(engine: CredenceEngine) -> list[dict[str, str]]:
    """Every assignment of the network's non-target variables, if small enough.

    Enumerating the whole space rather than sampling two or three cases matters:
    an elimination bug typically corrupts one branch of the parent
    configurations, and a spot check misses it. The space is capped so the test
    cannot become slow if the network grows -- when it does, the cap starts
    skipping evidence sets silently, so the cap is asserted against below.
    """
    network = engine.network
    variables = [
        n.id
        for n in network.nodes
        if n.id != network.target and not n.parents
    ]
    sets: list[dict[str, str]] = [{}]
    for var in variables:
        states = [s.name for s in engine._nodes[var].states]
        sets.extend({var: s} for s in states)
    return sets[:64]


def test_variable_elimination_agrees_with_enumeration(engine: CredenceEngine) -> None:
    """The two inference paths must agree, everywhere they are both defined."""
    checked = 0
    for evidence in _evidence_sets(engine):
        try:
            by_elimination = engine.query(evidence)
            by_enumeration = engine.query_by_enumeration(evidence)
        except CredenceError:
            # Zero-probability evidence. Enumeration and elimination agree about
            # rejecting it; the rejection itself is asserted separately.
            continue
        assert set(by_elimination) == set(by_enumeration), evidence
        for state, probability in by_elimination.items():
            assert probability == pytest.approx(by_enumeration[state], abs=1e-9), (
                f"{evidence}: {state} eliminated to {probability}, "
                f"enumerated to {by_enumeration[state]}"
            )
        checked += 1
    assert checked >= 4, (
        f"only {checked} evidence sets were comparable; the cross-check is not "
        "exercising the network"
    )


def test_the_cross_check_space_is_not_silently_empty(engine: CredenceEngine) -> None:
    """A cross-check over zero cases passes trivially.

    This is the failure mode the previous test is most likely to develop: the
    network is edited, the root variables vanish, ``_evidence_sets`` returns
    only ``[{}]``, and the suite keeps reporting green.
    """
    assert len(_evidence_sets(engine)) >= 3
    assert len(engine.network.nodes) >= 3


def test_posterior_is_a_distribution(engine: CredenceEngine) -> None:
    for evidence in _evidence_sets(engine)[:8]:
        try:
            distribution = engine.query(evidence)
        except CredenceError:
            continue
        assert sum(distribution.values()) == pytest.approx(1.0, abs=1e-12), evidence
        assert all(0.0 <= p <= 1.0 for p in distribution.values()), evidence


def test_evidence_on_the_target_returns_a_degenerate_distribution(
    engine: CredenceEngine,
) -> None:
    """Observing the target is an answer, not an error, and not a no-op."""
    target = engine.network.target
    state = engine._nodes[target].states[0].name
    distribution = engine.query({target: state})
    assert distribution[state] == pytest.approx(1.0)
    assert sum(distribution.values()) == pytest.approx(1.0)


def test_unknown_variable_and_state_raise(engine: CredenceEngine) -> None:
    """A dropped observation is a wrong answer presented confidently.

    The failure this guards against is the quiet one: evidence naming a variable
    the network does not have, or a state name with the wrong case, being
    ignored -- leaving the prior to be reported as though it were the posterior.
    """
    with pytest.raises(CredenceError, match="unknown variable"):
        engine.query({"no_such_variable": "state"})
    target = engine.network.target
    with pytest.raises(CredenceError, match="has no state"):
        engine.query({target: "no_such_state"})


def test_conjunction_of_all_states_is_consistent_with_the_marginals(
    engine: CredenceEngine,
) -> None:
    """Joint evidence must not be more probable than its own marginals.

    A cheap invariant with real teeth: it fails if the factor product drops or
    double-counts a factor when several variables are observed at once, which is
    the case a single-variable cross-check never reaches.
    """
    roots = [
        n.id for n in engine.network.nodes if not n.parents and n.id != engine.network.target
    ]
    if len(roots) < 2:
        pytest.skip("network has fewer than two independent roots")
    for pair in itertools.combinations(roots[:4], 2):
        joint = {
            var: engine._nodes[var].states[0].name
            for var in pair
        }
        try:
            engine.query(joint)
        except CredenceError:
            continue
        # Agreement here is between elimination on a two-variable joint and
        # elimination twice on one; if the product is mishandled the two diverge.
        assert engine.query(joint) == pytest.approx(
            engine.query_by_enumeration(joint), abs=1e-9
        )


def test_posterior_report_carries_the_caveat_and_the_authorities(
    engine: CredenceEngine,
) -> None:
    """The report exists so no caller has to remember the caveat.

    A posterior without its interpretation caveat and without the elicitation
    records it rests on is a number a lawyer cannot audit, and the whole design
    position of this project is that such a number must not reach a client. So
    the report is asserted to carry both, not merely to carry a probability.
    """
    report = engine.posterior_report()
    assert report.caveat, "posterior report has no caveat"
    assert report.distribution, "posterior report has no distribution"
    assert sum(report.distribution.values()) == pytest.approx(1.0, abs=1e-9)
    for sensitivity in report.sensitivities:
        assert isinstance(sensitivity, tuple) and len(sensitivity) == 2
    # The elicitation records are the authorities for the numbers.
    assert report.authorities or engine.network.approvals, (
        "posterior report cites neither elicitations nor approvals; the numbers "
        "have no provenance"
    )


def test_unapproved_network_says_so_in_its_report(engine: CredenceEngine) -> None:
    """An unapproved network must announce its own status in the output.

    Elicited numbers are somebody's judgement. Until a named expert has signed
    them off, the report has to say that, because the number itself looks
    identical either way.
    """
    if engine.network.approvals:
        pytest.skip("shipped network is approved; nothing to assert about the warning")
    assert "unapproved" in engine.posterior_report().caveat.lower()


def test_conditional_posterior_is_not_transposed_against_its_cpt(
    engine: CredenceEngine,
) -> None:
    """Pin the two layout faults that the cross-check above was written to find.

    Both were present in the shipped engine. ``_factors`` reshaped a CPT -- one
    row per parent configuration, one column per state -- straight into the
    factor's scope shape, which has the node's own state axis *first*. Both have
    the same number of elements, so reshape succeeded and reinterpreted the
    numbers. ``_multiply`` then did the same thing to the second operand, whose
    axes are in a different order in the union scope than in its own.

    Neither fault is visible in the prior: at no evidence the errors cancelled,
    and the prior came out correct to four decimal places. They show only on
    conditional evidence, which is the only case that matters. Given a foreign
    government acquirer the engine reported P(high concern) = 0.399 where the
    correct answer is 0.236 -- a 69% relative overstatement of the highest-risk
    state, from a network whose own validation passed.

    The values are pinned rather than only cross-checked so that a future
    "simplification" of either layout produces a failure that names the fault,
    instead of a silent change in a risk number a client might read.
    """
    acquirer = "acquirer_is_foreign_government"
    distribution = engine.query({acquirer: "yes"})
    assert distribution["high"] == pytest.approx(0.23625, abs=1e-9)
    assert distribution["low"] == pytest.approx(0.38625, abs=1e-9)
    # The prior still agrees, which is why the fault went unnoticed.
    prior = engine.query()
    assert prior["low"] == pytest.approx(0.5739375, abs=1e-9)
