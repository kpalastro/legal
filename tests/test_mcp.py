"""Tests for the MCP service layer.

The adapter over the MCP SDK cannot be tested without the SDK, and is not where
the behaviour lives anyway. What is tested here is :class:`RatioService`: the
payloads a client receives, the contexts each one carries, and the shape of a
refusal. Those are the things a client's *user* sees, and the things a reviewer
would have to trust if the transport were the only thing between them and the
corpus.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ratio.mcp_server.server import RESOURCE_TEMPLATES, TOOLS, RatioService, Refusal
from ratio.rules.engine import provenance_problems

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def service() -> RatioService:
    return RatioService.from_paths(ROOT / "data" / "fixtures")


# -- the corpus resource ---------------------------------------------------


def test_the_corpus_resource_declares_that_the_material_is_synthetic(
    service: RatioService,
) -> None:
    """The one thing that must never be missable.

    This corpus is invented for development. A client that passes it to a user as
    though it were Australian law is fabricating authority, and the server is the
    last place that can be prevented.
    """
    summary = service.corpus_summary()
    assert summary["synthetic"] is True
    assert summary["has_real_judgments"] is False
    assert summary["judgments"] > 0


def test_every_resource_and_tool_in_the_manifest_is_implemented(
    service: RatioService,
) -> None:
    """A manifest that names something the server does not implement is worse
    than no manifest: a client will call it and get a crash rather than a
    refusal."""
    assert TOOLS and RESOURCE_TEMPLATES
    assert service.corpus_summary()
    assert service.judgment("FCAFC:2019:77")
    assert service.provision("statute:Foreign Acquisitions and Takeovers Act 1975:18")
    assert service.authority_status("FCAFC:2019:77")
    assert service.ruleset("firb.notifiable_action")


# -- judgments and provisions ---------------------------------------------


def test_a_judgment_resource_is_paragraph_addressable(service: RatioService) -> None:
    """Every assertion the system makes points at a paragraph, so a judgment has
    to be retrievable as paragraphs and not as a blob of text."""
    payload = service.judgment("Bramblewood")
    assert payload["key"] == "FCAFC:2019:77"
    assert payload["paragraphs"], "the judgment has no paragraphs"
    for paragraph in payload["paragraphs"]:
        assert isinstance(paragraph["number"], int)
        assert paragraph["text"]
        assert paragraph["role"]


def test_a_missing_authority_is_a_refusal_with_a_reason(service: RatioService) -> None:
    with pytest.raises(Refusal) as caught:
        service.judgment("Nonesuch v Nobody")
    assert caught.value.reason == "authority_not_in_corpus"
    payload = caught.value.to_payload()
    assert payload["refused"] is True
    assert payload["requested"] == "Nonesuch v Nobody"


def test_every_payload_is_json_serialisable(service: RatioService) -> None:
    """The transport will serialise these. A ``date`` or a pydantic model reaching
    the client as a repr is a defect the type checker will not catch."""
    payloads = [
        service.corpus_summary(),
        service.judgment("HCA:2016:12"),
        service.authority_status("Bramblewood"),
        service.ruleset("firb.notifiable_action"),
        service.apply_rules({}),
    ]
    for payload in payloads:
        json.dumps(payload)  # raises TypeError if anything is not serialisable


# -- authority status ------------------------------------------------------


def test_authority_status_reports_what_it_did_not_count(service: RatioService) -> None:
    """The three buckets travel to the client, not just the status.

    A status alone is a conclusion the client cannot audit. The buckets are what
    let a lawyer see the treatment that was recorded and set aside, with the words
    that produced it.
    """
    payload = service.authority_status("Bramblewood")
    assert payload["status"] == "good_law"
    assert payload["counted"] >= 1
    assert payload["not_counted_advisory"] >= 1
    assert payload["not_counted_hierarchy"] >= 1
    for bucket in payload["authority"].values():
        for edge in bucket:
            assert edge["quote"], "an edge reached the client without its words"
            assert edge["citing_judgment"]


def test_authority_status_echoes_the_point_in_time_it_was_computed_at(
    service: RatioService,
) -> None:
    """A status computed as at a date that is not in the answer is a status
    nobody can check."""
    payload = service.authority_status("Bramblewood", as_at="2021-06-01")
    assert payload["as_at"] == "2021-06-01"
    assert all(e["citing_judgment"] != "FCAFC:2022:205" for e in payload["authority"]["evidence"])


# -- search ----------------------------------------------------------------


def test_search_refuses_rather_than_returning_its_best_hit(service: RatioService) -> None:
    """The confabulation case, at the MCP boundary.

    A model driving this server will call ``search_authorities`` and put whatever
    comes back in front of a user. The refusal has to happen here, because the
    model cannot tell a relevant paragraph from a lexically similar one -- that is
    precisely what it is delegating.
    """
    with pytest.raises(Refusal) as caught:
        service.search("exemption certificate effect on notifiable action")
    assert caught.value.reason == "below_retrieval_coverage"
    assert caught.value.detail["nearest"]


def test_search_refuses_when_nothing_matches_at_all(service: RatioService) -> None:
    with pytest.raises(Refusal) as caught:
        service.search("zzzqqq wwwwvvv")
    assert caught.value.reason == "nothing_indexed_matches"


def test_search_returns_results_with_their_coverage_for_a_question_it_holds(
    service: RatioService,
) -> None:
    payload = service.search("minority board nomination right substantial interest")
    assert payload["coverage"] >= 0.5
    assert payload["results"]
    assert payload["results"][0]["paragraph"] >= 1
    assert payload["results"][0]["coverage"] >= 0.5
    # The authority caveat travels with the hit, so a client cannot show a
    # paragraph without the warning about the judgment it came from.
    assert "authority_caveat" in payload["results"][0]


def test_search_echoes_its_filters(service: RatioService) -> None:
    """The filters change what may be said -- a binding-only search cannot cite
    a first-instance decision -- so the answer has to say which were applied."""
    payload = service.search(
        "association substance form enforceable arrangement", courts=["HCA"]
    )
    assert payload["filters"]["courts"] == ["HCA"]
    assert all(r["court"] == "HCA" for r in payload["results"])


# -- verification ----------------------------------------------------------


def test_verification_grounds_a_real_quote_and_rejects_an_invented_one(
    service: RatioService,
) -> None:
    real = service.verify(
        "Yarrow holds that enforceability is not required.",
        quotes=[
            {
                "judgment": "HCA:2016:12",
                "paragraph": 7,
                "quote": "It is not necessary that those wishes be enforceable",
            }
        ],
        # The verifier parses citations as they are written in a document, not
        # as this system's internal keys. "[2016] HCA 12" is what a lawyer types
        # and what the corpus contains; "HCA:2016:12" is our own identifier for
        # the same thing, and is deliberately not accepted here.
        citations=["Yarrow v Minister for Foreign Investment [2016] HCA 12"],
    )
    assert real["is_clean"] and real["verdict"] == "supported"

    invented = service.verify(
        "Yarrow holds that a deed is required.",
        quotes=[
            {
                "judgment": "HCA:2016:12",
                "paragraph": 7,
                "quote": "It is necessary that those wishes be recorded in a deed",
            }
        ],
        citations=["Yarrow v Minister for Foreign Investment [2016] HCA 12"],
    )
    assert not invented["is_clean"]
    assert invented["verdict"] == "unsupported"
    assert "FAIL" in invented["explanation"]


def test_an_uncited_claim_is_not_clean(service: RatioService) -> None:
    """A claim with nothing behind it has not been verified, even though nothing
    in it is false."""
    report = service.verify("The acquisition was notifiable.")
    assert not report["is_clean"]


# -- rules -----------------------------------------------------------------


def test_applying_a_rule_to_incomplete_facts_returns_the_gaps(
    service: RatioService,
) -> None:
    """The gap list is the only form of "I cannot answer" a user can act on.

    Returning the conclusion the engine reached with a fact missing would be
    confident and wrong; returning a bare refusal would leave the user with
    nothing to do.
    """
    payload = service.apply_rules({})
    assert payload["conclusion"] == "undetermined"
    assert payload["gaps"]
    assert all("." in gap for gap in payload["gaps"]), (
        "gaps must name the field a user can establish, not the node that needed it"
    )


def test_a_complete_fact_base_determines_the_conclusion(service: RatioService) -> None:
    payload = service.apply_rules(
        {
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
    )
    assert payload["gaps"] == []
    assert payload["conclusion"] == "applies"
    assert payload["approved"] is False, (
        "the shipped ruleset is a draft; the result must say so rather than "
        "report a conclusion as though an expert had signed the rule off"
    )


def test_the_ruleset_resource_carries_its_approval_state(service: RatioService) -> None:
    payload = service.ruleset("firb.notifiable_action")
    assert payload["status"] == "draft"
    assert payload["is_approved"] is False
    assert payload["approved_by"] is None
    for node in payload["nodes"]:
        if node["kind"] in {"leaf", "exception"}:
            assert node["provenance"], f"{node['id']} has no provenance"


def test_an_unknown_ruleset_lists_the_ones_that_exist(service: RatioService) -> None:
    with pytest.raises(Refusal) as caught:
        service.ruleset("no.such.ruleset")
    assert caught.value.reason == "unknown_ruleset"
    assert caught.value.detail["available"] == ["firb.notifiable_action"]


def test_the_ruleset_resource_reports_provenance_that_does_not_resolve(
    service: RatioService,
) -> None:
    """The corpus is what makes a provenance key mean something.

    Structural validation can only ask whether a node *has* provenance. Only a
    client holding both the ruleset and the corpus can ask whether the key names
    an authority that exists, and if it does not, the client is the last place
    that can say so before a lawyer reads "this rule rests on s 18" and believes
    it. The shipped ruleset resolves cleanly; this asserts the channel is live
    rather than trusting that it is.
    """
    payload = service.ruleset("firb.notifiable_action")
    assert "unresolved_provenance" in payload
    assert payload["unresolved_provenance"] == [], payload["unresolved_provenance"]


def test_the_resolution_check_is_actually_connected_to_the_corpus(
    service: RatioService,
) -> None:
    """A check that always returns ``[]`` passes the test above.

    So this one makes the check fail on purpose: a ruleset whose provenance names
    nothing must report it, through the same method the resource calls. Without
    this, the previous test asserts that a function which returns a constant is a
    constant.
    """
    from ratio.model import AuthorityRef, NodeKind

    ruleset = service.rulesets["firb.notifiable_action"]
    leaf = next(n for n in ruleset.nodes if n.kind is NodeKind.LEAF)
    broken = ruleset.model_copy(
        update={
            "nodes": tuple(
                n.model_copy(
                    update={
                        "provenance": n.provenance
                        + (
                            AuthorityRef.model_construct(
                                kind="statute", provision_key="statute:No Such Act 1999:1"
                            ),
                        )
                    }
                )
                if n.id == leaf.id
                else n
                for n in ruleset.nodes
            )
        }
    )
    problems = provenance_problems(
        broken,
        judgment_ids=set(service.kb.judgments),
        provision_keys=set(service.kb.provisions),
    )
    assert any("No Such Act" in p for p in problems), problems


def test_a_ruleset_edited_after_approval_is_refused_not_raised(
    service: RatioService,
) -> None:
    """The refusal that matters most, and the one that was missing.

    A ruleset edited after an expert signed it off makes ``RuleEngine`` raise.
    That exception used to escape ``apply_rules``, so the single case where a
    client most needs to be told something -- *the rule you are about to rely on
    has been altered since it was approved* -- reached it as a transport failure.
    A client can swallow that, or retry it, and neither one tells its user
    anything true.

    It is now a ``Refusal`` like any other: a reason, and the validation issues
    verbatim. This test asserts the type, not the wording, because the point is
    that the failure is *readable*.
    """
    from datetime import UTC, datetime

    from ratio.model import Approval, NodeKind
    from ratio.rules.engine import RuleEvaluationError

    ruleset = service.rulesets["firb.notifiable_action"]
    approved = ruleset.model_copy(
        update={
            "status": "approved",
            "approvals": (
                Approval(
                    approver="A. Practitioner",
                    role="Partner, M&A",
                    approved_at=datetime(2026, 1, 1, tzinfo=UTC),
                    artefact_hash=Approval.hash_of(ruleset.canonical_payload()),
                ),
            ),
        }
    )
    leaf = next(n for n in approved.nodes if n.kind is NodeKind.LEAF)
    edited = approved.model_copy(
        update={
            "nodes": tuple(
                n.model_copy(update={"statement": "a materially different test"})
                if n.id == leaf.id
                else n
                for n in approved.nodes
            )
        }
    )
    # The engine is right to refuse this, and says so in a way a caller can catch.
    with pytest.raises(RuleEvaluationError):
        from ratio.rules.engine import RuleEngine

        RuleEngine(edited)

    # The service layer must turn that into something a client can *read*.
    service.rulesets["tampered"] = edited
    try:
        with pytest.raises(Refusal) as caught:
            service.apply_rules({}, ruleset_id="tampered")
        assert caught.value.reason == "ruleset_failed_validation"
        payload = caught.value.to_payload()
        assert payload["refused"] is True
        assert caught.value.detail["issues"], "the validation issues did not travel"
        assert any(
            "edited since sign-off" in i for i in caught.value.detail["issues"]
        )
        json.dumps(payload)  # the client will have to serialise this
    finally:
        del service.rulesets["tampered"]
