"""Tests for the HTTP transport.

The transport is thin on purpose, and this file is correspondingly short: it
checks the two things that are properties of the *transport* rather than of the
service behind it, and nothing else.

A refusal must be a ``200`` carrying ``{"refused": true}``. That is the contract
the MCP layer already returns, and it is what stops a client from reading a
governance control as a transport fault and retrying it. If a refusal ever starts
arriving as a 4xx, every client written against this surface starts doing the
wrong thing, quietly.

And the candidate round trip: what ``GET`` returns must be postable back. Any
field the view omits is a field the first save deletes -- which is a real defect
this endpoint had, where the model's ``approvals`` list was absent from the view
and a save would have erased the approval history.

Skipped when the ``api`` extra is not installed, which is the same arrangement
``tests/test_mcp.py`` uses for the MCP SDK: the extra is optional, and a missing
optional extra must not fail the suite.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ratio.authoring.service import AuthoringService

fastapi = pytest.importorskip("fastapi", reason="requires the api extra")
pytest.importorskip("httpx", reason="the TestClient needs httpx")

from fastapi.testclient import TestClient  # noqa: E402

from ratio.authoring.api import build_app  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
FIRB = "firb.notifiable_action"


@pytest.fixture
def client(tmp_path: Path):
    service = AuthoringService.build(
        ROOT / "data" / "fixtures", rulesets=ROOT / "rulesets", workspace=tmp_path / "ws"
    )
    return TestClient(build_app(service)), service


def test_health_reports_where_edits_will_land(client) -> None:
    http, service = client
    body = http.get("/api/health").json()
    assert body["ok"] is True
    assert body["store"]["directory"] == service.store.describe()["directory"]
    assert body["store"]["in_place"] is False


def test_an_unknown_ruleset_is_a_200_carrying_a_refusal(client) -> None:
    """A refusal is the system working, and the client must not retry it."""
    http, _ = client
    response = http.get("/api/rulesets/nope")
    assert response.status_code == 200
    body = response.json()
    assert body["refused"] is True
    assert body["reason"] == "unknown_ruleset"
    assert body["message"], "a refusal without a message is not actionable"


def test_an_unconfirmed_digest_is_refused_over_the_wire(client) -> None:
    """The transport must not turn the digest step into a 4xx to retry."""
    http, _ = client
    view = http.get(f"/api/rulesets/{FIRB}").json()
    response = http.post(
        f"/api/rulesets/{FIRB}/sign",
        json={
            "ruleset": {**view["ruleset"], "nodes": view["nodes"]},
            "approver": "A. Expert",
            "role": "Special Counsel",
            "confirm_digest": "0" * 64,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["reason"] == "digest_not_confirmed"
    assert body["expected"] == view["ruleset"]["digest"]


def test_what_get_returns_can_be_posted_back(client) -> None:
    """The view is the candidate.

    The surface has no other way to hold a revision: it renders what ``GET``
    returns and posts it back on save. So the reassembled payload must parse,
    validate, and hash to the same digest -- otherwise every save from the UI is
    a save of a subtly different ruleset, and the approval the expert read on
    screen is not the approval that gets recorded.
    """
    http, _ = client
    view = http.get(f"/api/rulesets/{FIRB}").json()
    candidate = {**view["ruleset"], "nodes": view["nodes"]}

    response = http.post(
        f"/api/rulesets/{FIRB}/candidate", json={"ruleset": candidate}
    )
    assert response.status_code == 200
    returned = response.json()
    assert returned["issues"] == []
    assert returned["unresolved_provenance"] == []
    assert returned["anchor_problems"] == []
    assert returned["ruleset"]["digest"] == view["ruleset"]["digest"]
    assert returned["ruleset"]["current_hash"] == view["ruleset"]["current_hash"]
    # Field for field. A key the view drops is a key the save drops.
    assert returned["ruleset"]["approvals"] == view["ruleset"]["approvals"]
    assert len(returned["nodes"]) == len(view["nodes"])


def test_a_signed_ruleset_still_carries_its_approval_history_through_the_view(
    client,
) -> None:
    """The defect this endpoint had.

    ``_ruleset_payload`` exposed ``approval`` and ``historical_approvals`` -- two
    display views -- and not the model's own ``approvals``. A client that took the
    view and posted it back would have saved a ruleset with no approvals at all:
    the signature silently gone, ``is_approved`` false, and no error anywhere.
    """
    http, _ = client
    view = http.get(f"/api/rulesets/{FIRB}").json()
    candidate = {**view["ruleset"], "nodes": view["nodes"]}

    signed = http.post(
        f"/api/rulesets/{FIRB}/sign",
        json={
            "ruleset": candidate,
            "approver": "A. Expert",
            "role": "Special Counsel",
            "confirm_digest": view["ruleset"]["digest"],
        },
    ).json()
    assert signed["ruleset"]["is_approved"] is True
    assert len(signed["ruleset"]["approvals"]) == 1

    round_tripped = http.post(
        f"/api/rulesets/{FIRB}/candidate",
        json={"ruleset": {**signed["ruleset"], "nodes": signed["nodes"]}},
    ).json()
    assert round_tripped["ruleset"]["is_approved"] is True
    assert round_tripped["ruleset"]["approvals"] == signed["ruleset"]["approvals"]


def test_the_review_sheet_comes_back_with_the_experts_column_empty(client) -> None:
    """The one column that must never be pre-filled.

    A review sheet with the verdict already in it collects confirmation instead of
    adjudication, which is the opposite of what the artefact is for.
    """
    http, _ = client
    body = http.get(f"/api/rulesets/{FIRB}/review-sheet", params={"limit": 5}).json()
    assert body["total"] > 0
    assert body["rows"], "the sheet must have rows to adjudicate"
    assert all("conclusion" in row for row in body["rows"])
    assert not any("verdict" in row or "expert" in row for row in body["rows"])


def test_the_corpus_endpoint_says_the_material_is_synthetic(client) -> None:
    """The one thing a user of this surface must never be able to miss."""
    http, _ = client
    corpus = http.get("/api/corpus").json()
    assert corpus["synthetic"] is True
    assert corpus["judgments"] > 0
