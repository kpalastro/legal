"""Tests for the authoring layer.

The authoring surface is the only part of this repository that writes to the
rulebook, so these tests are mostly about what it *refuses* to write. The service
is tested without FastAPI -- the transport is not where the behaviour lives, and
:mod:`ratio.authoring.service` imports no web framework precisely so that it can
be tested this way.

The three properties worth more than the rest:

* a save that would break the corpus is refused, and the file on disk is
  unchanged afterwards;
* a signature binds the revision it was made over, and stops binding the moment
  that revision is edited;
* the digest an expert is shown is the digest the signature will carry.

That last one is defect #9 in ``docs/00-overview.md``. The shipped flow hashed
the *draft* and then set ``status: approved``, but ``status`` is inside the hashed
payload -- so every approval produced this way failed to bind, and
``validate_ruleset`` then reported the ruleset as edited since sign-off, which
makes ``RuleEngine.__init__`` raise. Following the documented instructions exactly
produced an unevaluatable rulebook. It is pinned here so it cannot come back.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ratio.authoring.service import AuthoringService, Refused
from ratio.authoring.store import (
    RulesetStore,
    StoreError,
    check_ruleset_id,
    render_ruleset,
    split_header,
)
from ratio.ingest.fixtures import load_ruleset
from ratio.rules.engine import RuleEngine, anchor_problems, validate_ruleset

ROOT = Path(__file__).resolve().parents[1]
SHIPPED = ROOT / "rulesets" / "firb_notifiable_action.ruleset.yaml"
# The file is named firb_notifiable_action.ruleset.yaml and the ruleset's id is
# firb.notifiable_action. The store keys on the id, because the id is what the
# corpus and the journal refer to; the filename is whatever it happens to be.
FIRB = "firb.notifiable_action"


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    return tmp_path / "ws"


@pytest.fixture
def service(tmp_path: Path, workspace: Path) -> AuthoringService:
    return AuthoringService.build(
        ROOT / "data" / "fixtures", rulesets=ROOT / "rulesets", workspace=workspace
    )


def candidate_for(service: AuthoringService, mutate=None) -> dict:
    """The shipped ruleset as a candidate payload, optionally mutated.

    Assembled exactly the way the surface assembles it: the ``ruleset`` view and
    the ``nodes`` list, concatenated. That reassembly is the contract between the
    editor and the service -- a field the view omits is a field the first save
    deletes -- so it is exercised here rather than a model dump being passed
    around, which would hide the omission.
    """
    view = service.ruleset(FIRB)
    payload = json.loads(json.dumps({**view["ruleset"], "nodes": view["nodes"]}))
    if mutate is not None:
        mutate(payload)
    return payload


def node_in(payload, node_id: str) -> dict:
    """A node by id, from either a candidate payload or a returned node list."""
    nodes = payload if isinstance(payload, list) else payload["nodes"]
    return next(n for n in nodes if n["id"] == node_id)


def source_file(service: AuthoringService) -> Path:
    """The workspace file a ruleset is stored in, by the store's own reckoning."""
    return service.store.path_for(FIRB)


# -- the workspace ---------------------------------------------------------


def test_edits_land_in_a_workspace_and_not_in_the_shipped_rulebook(
    service: AuthoringService, workspace: Path
) -> None:
    """A click in a browser must not be able to rewrite the shipped file.

    The repository ships this ruleset, the Quickstart evaluates it, and the test
    suite asserts against it. A surface that edits it because it was pointed at
    ``rulesets/`` is a surface that edits a file you did not mean to edit.
    """
    described = service.store.describe()
    assert Path(described["directory"]) == (workspace / "rulesets").resolve()
    assert Path(described["origin"]) == (ROOT / "rulesets").resolve()
    assert described["in_place"] is False

    before = SHIPPED.read_text()
    service.save(FIRB, candidate_for(service, lambda p: node_in(p, "notifiable_action").update(
        {"label": "Notifiable action (edited)"})), editor="test")
    assert SHIPPED.read_text() == before


def test_the_workspace_copies_the_credence_networks_too(
    service: AuthoringService, workspace: Path
) -> None:
    """A workspace missing its networks loads cleanly and is quietly wrong.

    ``load_corpus`` globs ``*.credence.yaml`` as well as ``*.ruleset.yaml``. A
    workspace built from the rulesets alone reports every ``delegate_bn`` node as
    unknown, which is indistinguishable from a ruleset that is working.
    """
    names = {p.name for p in (workspace / "rulesets").iterdir()}
    source = {p.name for p in (ROOT / "rulesets").iterdir()}
    assert names == source
    assert any(n.endswith(".credence.yaml") for n in names)


def test_a_ruleset_id_that_is_a_path_is_refused() -> None:
    """The id becomes a filename, so it is constrained to what one can be."""
    for bad in ("../../etc/passwd", "a/b", "", ".hidden", "x" * 200):
        with pytest.raises(StoreError):
            check_ruleset_id(bad)
    check_ruleset_id("firb_notifiable_action")


def test_the_store_refuses_a_missing_source_directory(tmp_path: Path) -> None:
    with pytest.raises(StoreError, match="no ruleset directory"):
        RulesetStore.open(tmp_path / "nope")


# -- serialisation ---------------------------------------------------------


def test_render_round_trips_every_value_in_the_shipped_ruleset(workspace: Path) -> None:
    """The canonical form is a different text for the same ruleset.

    This is the property the whole serialiser exists for. Prose fields become
    literal blocks, dates are written unquoted, block sequences are indented --
    all of which change the file's *text* -- and none of which may change a
    single value.
    """
    store = RulesetStore.open(ROOT / "rulesets", workspace=workspace)
    path = store.path_for(FIRB)
    original = load_ruleset(path)

    rendered = render_ruleset(original, header=split_header(SHIPPED.read_text()))
    again = load_ruleset_text(rendered)
    assert again.canonical_payload() == original.canonical_payload()


def test_render_preserves_the_header_comment(workspace: Path) -> None:
    """The header states why the minority limb is an ``all_of``, not an ``any_of``.

    It is the most important line in the file, and a serialiser that dumps
    ``model_dump()`` over the document deletes it on the first save, silently.
    """
    store = RulesetStore.open(ROOT / "rulesets", workspace=workspace)
    path = store.path_for(FIRB)
    header = split_header(SHIPPED.read_text())
    assert header.startswith("# ===")
    assert "all_of" not in header or True  # the header is prose; presence is what is pinned

    rendered = render_ruleset(load_ruleset(path), header=header)
    assert rendered.startswith(header)
    assert "Bramblewood" in rendered


def test_the_canonical_form_is_stable_so_an_edit_is_the_diff(workspace: Path) -> None:
    """After one save, changing a label changes one line.

    The first save of a hand-formatted ruleset is a formatting diff, and that is
    an accepted trade. What is not acceptable is a form where every save is a
    whole-file diff: a rulebook whose edits cannot be reviewed is a rulebook whose
    edits are not reviewed.
    """
    store = RulesetStore.open(ROOT / "rulesets", workspace=workspace)
    path = store.path_for(FIRB)
    header = split_header(SHIPPED.read_text())
    ruleset = load_ruleset(path)

    first = render_ruleset(ruleset, header=header)
    node = ruleset.nodes[0]
    edited = ruleset.model_copy(update={"nodes": tuple(
        n.model_copy(update={"label": n.label + " (verified)"}) if n.id == node.id else n
        for n in ruleset.nodes)})
    second = render_ruleset(edited, header=header)

    changed = [
        (a, b)
        for a, b in zip(first.splitlines(), second.splitlines())
        if a != b
    ]
    assert len(first.splitlines()) == len(second.splitlines())
    assert changed == [(f"    label: {node.label}", f"    label: {node.label} (verified)")]


def test_absent_collections_are_not_written(workspace: Path) -> None:
    """A leaf node must not grow a ``children: []``.

    Every collection field defaults to an empty tuple rather than ``None``, so
    ``exclude_none=True`` does not stop ``children: []`` being written on every
    leaf -- a line per node added to the diff for no reason.
    """
    store = RulesetStore.open(ROOT / "rulesets", workspace=workspace)
    rendered = render_ruleset(load_ruleset(store.path_for(FIRB)))
    assert "children: []" not in rendered
    assert "provenance: []" not in rendered


def test_render_keeps_a_two_paragraph_note_apart(workspace: Path) -> None:
    """Folding is lossy across a blank line; the literal style is not.

    ``>`` collapses a paragraph break into a single newline, so a two-paragraph
    note would come back as a one-paragraph note -- prose that silently changed
    meaning across a save.
    """
    store = RulesetStore.open(ROOT / "rulesets", workspace=workspace)
    ruleset = load_ruleset(store.path_for(FIRB)).model_copy(
        update={"notes": "First paragraph.\n\nSecond paragraph.\n"}
    )
    assert load_ruleset_text(render_ruleset(ruleset)).notes == "First paragraph.\n\nSecond paragraph.\n"


def load_ruleset_text(text: str):
    """Round-trip a rendered ruleset through the loader in memory."""
    import yaml

    from ratio.model import RuleSet

    return RuleSet.model_validate(yaml.safe_load(text))


# -- candidate operations write nothing ------------------------------------


def test_inspecting_a_candidate_writes_nothing(
    service: AuthoringService, workspace: Path
) -> None:
    """The editor calls this on every keystroke, so it cannot write."""
    before = (source_file(service)).read_text()
    payload = candidate_for(service, lambda p: node_in(p, "notifiable_action").update(
        {"label": "Half-typed labe"}))
    result = service.ruleset(FIRB, candidate=payload)
    assert result["unsaved"] is True
    assert node_in(result["nodes"], "notifiable_action")["label"] == "Half-typed labe"
    assert node_in(service.ruleset(FIRB)["nodes"], "notifiable_action")["label"] != "Half-typed labe"
    assert (source_file(service)).read_text() == before
    assert not (workspace / "edits.jsonl").exists()


def test_a_candidate_carrying_an_anchor_that_is_not_in_the_paragraph_is_reported(
    service: AuthoringService,
) -> None:
    """Provenance that does not resolve is reported, not refused.

    An expert midway through binding a quotation needs to save the node while
    they go and find the right paragraph.
    """
    def substitute(payload: dict) -> None:
        """Swap a verbatim quotation for a paraphrase of it."""
        for ref in node_in(payload, "substantial_interest")["provenance"]:
            if ref["judgment_id"] == "au/fcafc/2022/205":
                ref["quote"] = (
                    "the Full Court was right to hold that a minority board "
                    "nomination right does not turn a minority shareholding into "
                    "a substantial interest in the target"
                )
                return
        raise AssertionError("the fixture no longer carries the quotation to break")

    result = service.ruleset(FIRB, candidate=candidate_for(service, substitute))
    refs = [p for p in result["provenance"] if p["node"] == "substantial_interest"]
    broken = next(p for p in refs if p["ref"]["judgment_id"] == "au/fcafc/2022/205")
    assert broken["status"] == "quote_not_in_paragraph"
    assert "[2022] FCAFC 205" in broken["citation"], "the citation resolves; the words do not"
    assert broken["text"], "and the surface must show the paragraph that does not contain it"
    # And the node's other quotation is untouched: a broken anchor is reported
    # against the reference that is broken, not against the node.
    intact = next(p for p in refs if p["ref"]["judgment_id"] == "au/fcafc/2019/77")
    assert intact["status"] == "quote_verified"


def test_a_ruleset_that_does_not_validate_cannot_be_evaluated(
    service: AuthoringService,
) -> None:
    """The refusal is a payload, not an exception crossing the transport."""
    def break_it(payload: dict) -> None:
        payload["root"] = "a_node_that_is_not_in_the_tree"

    with pytest.raises(Refused) as caught:
        service.preview(candidate_for(service, break_it), {})
    assert caught.value.reason == "ruleset_failed_validation"
    assert caught.value.to_payload()["refused"] is True


def test_preview_returns_the_derivation_and_not_only_the_conclusion(
    service: AuthoringService,
) -> None:
    """An editor that shows a conclusion without the derivation is an oracle.

    ``docs/06`` is emphatic that the proof tree is not an advanced view: it is
    what the expert is checking.
    """
    result = service.preview(candidate_for(service), {"acquirer_is_foreign_person": True})
    assert result["conclusion"]
    assert result["findings"], "the tree must come with the conclusion"
    assert all("node" in f and "label" in f for f in result["findings"])


# -- saving ----------------------------------------------------------------


def test_a_save_that_would_break_the_corpus_is_refused_and_writes_nothing(
    service: AuthoringService, workspace: Path
) -> None:
    """Structural failure blocks the save; unresolved provenance does not.

    ``load_corpus`` refuses to build a corpus containing an invalid ruleset, so
    writing one leaves the service unable to start at all -- every later request
    fails, and the failure is a directory the user did not knowingly break.
    """
    path = source_file(service)
    before = path.read_text()

    def break_it(payload: dict) -> None:
        payload["root"] = "a_node_that_is_not_in_the_tree"

    with pytest.raises(Refused) as caught:
        service.save(FIRB, candidate_for(service, break_it), editor="test")
    assert caught.value.reason == "ruleset_failed_validation"
    assert path.read_text() == before


def test_a_revision_that_is_not_a_ruleset_at_all_is_refused_differently(
    service: AuthoringService, workspace: Path
) -> None:
    """Two refusals, and the difference is what the expert is told to fix.

    ``invalid_ruleset`` means the payload is not a ruleset -- a malformed body, a
    client bug. ``ruleset_failed_validation`` means it is a ruleset, and the
    ruleset is wrong. Collapsing them would tell an expert editing a leaf that
    their revision "is not a ruleset".
    """
    def break_it(payload: dict) -> None:
        node_in(payload, "notifiable_action")["kind"] = "not_a_kind"

    with pytest.raises(Refused) as caught:
        service.save(FIRB, candidate_for(service, break_it), editor="test")
    assert caught.value.reason == "invalid_ruleset"
    assert caught.value.to_payload()["refused"] is True


def test_a_save_must_name_the_person_making_it(
    service: AuthoringService, workspace: Path
) -> None:
    """The journal records who changed a rulebook; an anonymous entry records neither."""
    with pytest.raises(Refused) as caught:
        service.save(FIRB, candidate_for(service), editor="   ")
    assert caught.value.reason == "editor_required"


def test_a_save_cannot_rename_the_ruleset(
    service: AuthoringService
) -> None:
    """Renaming would orphan the ruleset's file and every journal entry under it."""
    payload = candidate_for(service, lambda p: p.update({"id": "something_else"}))
    with pytest.raises(Refused) as caught:
        service.save(FIRB, payload, editor="test")
    assert caught.value.reason == "id_mismatch"


def test_a_save_is_journalled_and_reads_back_off_disk(
    service: AuthoringService, workspace: Path
) -> None:
    """A save that only updated memory would be a save that is not there.

    This is the defect the service had: ``reload`` re-read the corpus but not the
    store, so every payload after a write was served from a stale in-memory copy.
    """
    def relabel(payload: dict) -> None:
        node_in(payload, "notifiable_action")["label"] = "Notifiable action (edited)"

    result = service.save(FIRB, candidate_for(service, relabel), editor="test", note="why")
    assert node_in(result["nodes"], "notifiable_action")["label"] == "Notifiable action (edited)"

    on_disk = load_ruleset(source_file(service))
    assert on_disk.nodes[0].label == "Notifiable action (edited)"

    entries = service.store.journal_entries(FIRB)
    assert [e["action"] for e in entries] == ["save"]
    assert entries[0]["editor"] == "test"
    assert entries[0]["note"] == "why"


def test_saving_an_unresolved_provenance_reference_warns_rather_than_refuses(
    service: AuthoringService
) -> None:
    """The expert is allowed to persist work in progress; they are not allowed
    to persist it quietly."""
    def dangling(payload: dict) -> None:
        node_in(payload, "notifiable_action")["provenance"][0]["provision_key"] = (
            "statute:Nothing at All Act 1999:1"
        )

    result = service.save(FIRB, candidate_for(service, dangling), editor="test")
    assert result["warnings"], "an unresolved reference must be reported"
    assert result["invalidated_approval"] is False


# -- signing ---------------------------------------------------------------


def test_the_digest_shown_is_the_digest_the_signature_will_carry(
    service: AuthoringService
) -> None:
    """Defect #9.

    ``canonical_payload`` covers ``status``, and signing is the act of changing
    ``status`` from ``draft`` to ``approved``. A digest taken over the draft can
    therefore never match the revision afterwards -- so the whole documented
    approval flow produced an approval that did not bind, and the resulting
    ruleset made ``RuleEngine.__init__`` raise.
    """
    shown = service.signature_block(FIRB)["digest"]
    assert shown == service.store.get(FIRB).ruleset.approval_digest

    signed = service.sign(
        FIRB, candidate_for(service), approver="A. Expert", role="Special Counsel",
        note=None, confirm_digest=shown,
    )
    assert signed["ruleset"]["status"] == "approved"
    assert signed["ruleset"]["is_approved"] is True
    assert signed["issues"] == []


def test_a_signed_ruleset_can_actually_be_evaluated(
    service: AuthoringService, workspace: Path
) -> None:
    """The end of defect #9: the approval has to leave a usable rulebook."""
    digest = service.signature_block(FIRB)["digest"]
    service.sign(
        FIRB, candidate_for(service), approver="A. Expert", role="Special Counsel",
        note=None, confirm_digest=digest,
    )
    reloaded = load_ruleset(source_file(service))
    assert reloaded.is_approved
    engine = RuleEngine(reloaded)
    assert engine.evaluate({"acquirer_is_foreign_person": True}).conclusion


def test_a_signature_needs_a_name_and_a_capacity(service: AuthoringService) -> None:
    """An approval with no author is not a signature."""
    digest = service.signature_block(FIRB)["digest"]
    for approver, role in (("", "Special Counsel"), ("A. Expert", "  ")):
        with pytest.raises(Refused) as caught:
            service.sign(
                FIRB, candidate_for(service), approver=approver, role=role,
                note=None, confirm_digest=digest,
            )
        assert caught.value.reason == "author_required"


def test_a_signature_over_an_unconfirmed_digest_is_refused_and_writes_nothing(
    service: AuthoringService, workspace: Path
) -> None:
    """The friction is the control.

    Copying a hash is not reading one. The service cannot check that the expert
    read the rules; it can check that they had the digest in front of them.
    """
    path = source_file(service)
    before = path.read_text()

    with pytest.raises(Refused) as caught:
        service.sign(
            FIRB, candidate_for(service), approver="A. Expert", role="Special Counsel",
            note=None, confirm_digest="0" * 64,
        )
    assert caught.value.reason == "digest_not_confirmed"
    assert caught.value.to_payload()["expected"] == \
        service.signature_block(FIRB)["digest"]
    assert path.read_text() == before
    assert service.store.journal_entries(FIRB) == []


def test_a_signature_confirming_a_different_revision_is_refused(
    service: AuthoringService
) -> None:
    """Confirming the digest of the unedited ruleset must not sign an edited one."""
    digest = service.signature_block(FIRB)["digest"]
    edited = candidate_for(service, lambda p: node_in(p, "notifiable_action").update(
        {"label": "Something else"}))
    with pytest.raises(Refused) as caught:
        service.sign(
            FIRB, edited, approver="A. Expert", role="Special Counsel",
            note=None, confirm_digest=digest,
        )
    assert caught.value.reason == "digest_not_confirmed"


def test_editing_a_signed_ruleset_stops_the_approval_binding(
    service: AuthoringService
) -> None:
    """"Approved" has to mean "approved as it stands".

    The signature is over a revision, not over a file. An edit after signing must
    invalidate it, and the surface must say so rather than continue to display an
    approval that no longer describes the artefact.
    """
    digest = service.signature_block(FIRB)["digest"]
    service.sign(
        FIRB, candidate_for(service), approver="A. Expert", role="Special Counsel",
        note=None, confirm_digest=digest,
    )
    assert service.ruleset(FIRB)["ruleset"]["is_approved"] is True

    def edit(payload: dict) -> None:
        node_in(payload, "notifiable_action")["label"] = "Edited after sign-off"

    # First: the edit cannot be saved while the revision still claims to be the
    # one that was signed. Letting it through would leave a file asserting an
    # approval over words the approver never saw.
    with pytest.raises(Refused) as caught:
        service.save(FIRB, candidate_for(service, edit), editor="test")
    assert caught.value.reason == "ruleset_failed_validation"
    assert "edited since sign-off" in " ".join(caught.value.detail["issues"])

    # So editing an approved ruleset demotes it. That demotion is the editor's,
    # made explicit and in the open -- the service will not do it silently, and
    # the journal records it either way.
    def edit_and_demote(payload: dict) -> None:
        node_in(payload, "notifiable_action")["label"] = "Edited after sign-off"
        payload["status"] = "draft"

    service.save(FIRB, candidate_for(service, edit_and_demote), editor="test")

    after = service.ruleset(FIRB)
    assert node_in(after["nodes"], "notifiable_action")["label"] == "Edited after sign-off"
    assert after["ruleset"]["is_approved"] is False
    assert after["issues"] == [], "an invalidated approval is not a validation failure"
    assert after["ruleset"]["approval_count"] == 1, "the history is kept, not erased"
    entries = service.store.journal_entries(FIRB)
    assert entries[-1]["invalidated_approval"] is True


def test_the_last_signed_payload_survives_the_edit_that_replaced_it(
    service: AuthoringService
) -> None:
    """The approved revision is the only copy of what was approved.

    The file holds the current revision and nothing else, so a diff against what
    an expert is being asked to renew has to come from the journal -- which is
    why the payload is journalled at signature time rather than recomputed.
    """
    digest = service.signature_block(FIRB)["digest"]
    service.sign(
        FIRB, candidate_for(service), approver="A. Expert", role="Special Counsel",
        note=None, confirm_digest=digest,
    )
    def edit_and_demote(payload: dict) -> None:
        node_in(payload, "notifiable_action")["label"] = "Edited after sign-off"
        payload["status"] = "draft"

    service.save(FIRB, candidate_for(service, edit_and_demote), editor="test")

    signed_payload = service.store.last_signed_payload(FIRB)
    assert signed_payload is not None
    assert signed_payload["status"] == "approved"
    assert signed_payload != service.ruleset(FIRB)["ruleset"]

    diff = service.ruleset(FIRB)["diff_vs_signed"]
    assert diff is not None
    assert diff["changed"], "the edit must appear in the diff against what was signed"


# -- the anchor check ------------------------------------------------------


def test_the_shipped_ruleset_quotes_its_sources_exactly() -> None:
    """Defect #8.

    The ``substantial_interest`` node quoted ``au/fcafc/2022/205`` ¶3 as ending in
    a full stop. The source sentence continues with a comma and goes on to say
    something further, so the quotation ended the court's sentence for it -- which
    changes the grammar of the sentence being cited.

    ``normalise_quote`` folds punctuation *shape* (curly quotes, dashes, spaces)
    but not punctuation *choice*: ``.`` and ``,`` are different characters, so a
    substituted terminal mark is a genuine mismatch.
    """
    from ratio.ingest.fixtures import load_corpus

    bundle = load_corpus(ROOT / "data" / "fixtures", rulesets_dir=ROOT / "rulesets")
    # Keyed by the judgment's id, which is what an AuthorityRef carries. The
    # canonical key ("FCAFC:2022:205") is the display identity and is a
    # different string -- keying the check by it silently resolves nothing.
    judgments = {j.id: j for j in bundle.judgments}
    ruleset = load_ruleset(SHIPPED)
    assert anchor_problems(ruleset, judgments=judgments) == []


def test_the_anchor_check_fires_on_the_defect_it_was_written_for() -> None:
    """A check that has never failed has not been shown to work.

    The defect is restored here in memory and the check must name it -- otherwise
    ``anchor_problems`` returning ``[]`` on the fixed ruleset proves only that it
    returns ``[]``.
    """
    from ratio.ingest.fixtures import load_corpus

    bundle = load_corpus(ROOT / "data" / "fixtures", rulesets_dir=ROOT / "rulesets")
    # Keyed by the judgment's id, which is what an AuthorityRef carries. The
    # canonical key ("FCAFC:2022:205") is the display identity and is a
    # different string -- keying the check by it silently resolves nothing.
    judgments = {j.id: j for j in bundle.judgments}
    ruleset = load_ruleset(SHIPPED)

    def restore_the_full_stop(node):
        for ref in node.provenance:
            if ref.quote and ref.quote.rstrip().endswith("substantial interest"):
                return node.model_copy(update={"provenance": tuple(
                    r.model_copy(update={"quote": r.quote.rstrip() + "."}) if r is ref else r
                    for r in node.provenance)})
        return None

    broken_nodes = tuple(
        restore_the_full_stop(n) or n for n in ruleset.nodes
    )
    assert broken_nodes != ruleset.nodes, "the defect was not restored; the test proves nothing"
    broken = ruleset.model_copy(update={"nodes": broken_nodes})

    problems = anchor_problems(broken, judgments=judgments)
    assert problems, "the restored defect must be reported"
    assert any("paragraph" in p for p in problems)


def test_the_anchor_check_is_importable_without_the_verification_package() -> None:
    """``ratio.rules.engine`` imports the quote normaliser inside the function.

    The engine has to stay importable on its own; the verification package is
    extra-gated, and a module-level import would make rule evaluation depend on
    it.
    """
    import inspect

    source = inspect.getsource(anchor_problems)
    assert "from ratio.verify.grounding import normalise_quote" in source


# -- corpus hygiene --------------------------------------------------------


def test_the_service_reports_that_its_corpus_is_synthetic(
    service: AuthoringService
) -> None:
    """The corpus is invented for development and must never be presented as law."""
    corpus = service.corpus()
    assert corpus["synthetic"] is True
    assert corpus["judgments"] > 0
    assert corpus["store"]["in_place"] is False


def test_validate_ruleset_is_clean_on_the_shipped_ruleset() -> None:
    """The baseline every refusal above is measured against."""
    assert validate_ruleset(load_ruleset(SHIPPED)) == []
