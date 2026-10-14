"""Every authoring operation, as plain data.

This module is the whole of the authoring surface's behaviour. The HTTP adapter
in :mod:`ratio.authoring.api` imports it; nothing here imports the adapter, and
nothing here imports a web framework. That is deliberate and it is the same
split the MCP layer makes: it means the rules about *what may be said and done*
can be tested without a transport, a port or a browser, and that a transport
cannot quietly acquire a policy of its own.

Two commitments shape the operations, and both come from
``docs/06-expert-authoring-surface.md``.

**A candidate is not a saved ruleset, and the difference is the point.** The
editor is asked to validate, preview and diff *before* saving, over a ruleset it
has constructed in memory and never wrote. If the only way to find out whether a
proposed edit is sound were to save it and reload, the surface would be one that
rewards committing first and checking afterwards -- which is how a rulebook
acquires revisions nobody can explain.

**The surface does not approve anything on the expert's behalf.** There is no
operation here that invents an approval, and no default for the approver's name.
``sign`` exists, and it refuses unless the caller supplies the name, the
capacity, and the *digest the expert was shown*. Making an expert retype a
digest is friction designed in on purpose: it is the difference between reading
a hash and copying one, and the whole governance value of the artefact is that
the expert had to look at it. The CLI states the rule and declines to write at
all; this operation writes, because a browser is a place where an act has to be
able to complete, and it pays for that with the confirmation step.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ratio.authoring.store import RulesetStore, StoreError, check_ruleset_id
from ratio.ingest.fixtures import load_corpus
from ratio.kb.index import KnowledgeBase
from ratio.model import Approval, AuthorityRef, RuleNode, RuleSet
from ratio.rules.engine import (
    RuleEngine,
    RuleEvaluationError,
    anchor_problems,
    provenance_problems,
    validate_ruleset,
)
from ratio.verify.grounding import normalise_quote

DEFAULT_FIXTURES = Path("data/fixtures")
DEFAULT_RULESETS = Path("rulesets")


class Refused(Exception):
    """The service declined, and says why.

    Deliberately not the MCP layer's ``Refusal``, though the payload shape is
    identical. The two transports share a *contract*, not an import: a shared
    class would mean an edit made for one surface's convenience changes what the
    other tells its users, and the two have different users.

    The refusal itself is a result rather than a failure, for the reason the MCP
    layer gives: a system that declines to apply a rule because the rule has been
    edited since sign-off has done the right thing, and reporting it as a
    transport fault would invite a client to retry it.
    """

    def __init__(self, message: str, *, reason: str, detail: dict[str, Any] | None = None):
        super().__init__(message)
        self.reason = reason
        self.detail = detail or {}

    def to_payload(self) -> dict[str, Any]:
        """The refusal as the wire object, with ``detail`` flattened in.

        Flattened rather than nested so that this payload is byte-comparable with
        what the MCP layer returns for the same refusal -- the two transports
        share a contract. The cost is that detail keys share a namespace with the
        envelope, so a detail named ``reason`` would silently rename the refusal.
        No caller passes one; a future one should not.
        """
        return {"refused": True, "reason": self.reason, "message": str(self), **self.detail}


@dataclass
class AuthoringService:
    """Inspect, edit, validate and sign the encoded rulebook.

    ``bundle`` and ``kb`` are rebuilt from disk after every write rather than
    patched in place. A service whose state is a hand-maintained mirror of the
    files will eventually disagree with them, and the disagreement shows up as an
    authorising surface displaying a rule that is not the rule being evaluated.
    """

    fixtures: Path
    store: RulesetStore

    def __post_init__(self) -> None:
        self.reload()

    @classmethod
    def build(
        cls,
        fixtures: Path | str = DEFAULT_FIXTURES,
        *,
        rulesets: Path | str | None = None,
        workspace: Path | str | None = None,
        in_place: bool = False,
    ) -> AuthoringService:
        store = RulesetStore.open(
            Path(rulesets or DEFAULT_RULESETS), workspace=workspace, in_place=in_place
        )
        return cls(fixtures=Path(fixtures), store=store)

    def reload(self) -> None:
        # The store first, then the corpus built from it. Forgetting the first
        # half is a bug this method had: the files on disk were correct and every
        # payload the service returned afterwards was served from the store's
        # stale in-memory copy, so a save appeared to succeed and a signature
        # appeared not to bind. There is one source of truth and it is the
        # directory, so it is re-read first.
        self.store.reload()
        self.bundle = load_corpus(self.fixtures, rulesets_dir=self.store.directory)
        self.kb = KnowledgeBase.build(self.bundle)

    # ------------------------------------------------------------------
    # inspection
    # ------------------------------------------------------------------

    def corpus(self) -> dict[str, Any]:
        return {
            "judgments": len(self.kb.judgments),
            "provisions": len(self.kb.provisions),
            "paragraphs_indexed": len(self.kb.refs),
            "synthetic": self.bundle.has_synthetic,
            "has_real_judgments": self.bundle.has_real,
            "warnings": list(self.bundle.problems) + list(self.kb.warnings),
            "store": self.store.describe(),
        }

    def list_rulesets(self) -> dict[str, Any]:
        out = []
        for ruleset in self.store.rulesets():
            out.append(
                {
                    "id": ruleset.id,
                    "title": ruleset.title,
                    "question": ruleset.question,
                    "domain": ruleset.domain,
                    "version": ruleset.version,
                    "status": ruleset.status,
                    "is_approved": ruleset.is_approved,
                    "node_count": len(ruleset.nodes),
                    "issue_count": len(validate_ruleset(ruleset)),
                    "edit_count": len(self.store.journal_entries(ruleset.id)),
                }
            )
        return {"rulesets": out}

    def ruleset(self, ruleset_id: str, *, candidate: dict | None = None) -> dict[str, Any]:
        """One ruleset in full, or a candidate in the same shape.

        With ``candidate`` supplied, everything that can be computed about a
        proposed ruleset is computed about it -- issues, provenance resolution,
        anchors, the diff against the last signed revision -- without anything
        being written. That is the call the editor makes on every keystroke.
        """
        ruleset = (
            self._parse_candidate(candidate)
            if candidate is not None
            else self._require(ruleset_id).ruleset
        )
        entry = self.store.get(ruleset.id)
        return {
            "ruleset": self._ruleset_payload(ruleset),
            "nodes": [self._node_payload(n) for n in ruleset.nodes],
            "issues": validate_ruleset(ruleset),
            "unresolved_provenance": self._provenance_problems(ruleset),
            "anchor_problems": anchor_problems(ruleset, judgments=self.kb.judgments),
            "provenance": self._provenance_resolution(ruleset),
            "diff_vs_signed": self._diff_vs_signed(ruleset),
            "journal": self.store.journal_entries(ruleset.id)[-20:],
            "source_path": str(entry.path) if entry else None,
            "origin_path": str(entry.origin) if entry and entry.origin else None,
            "unsaved": candidate is not None,
        }

    def authorities(self) -> dict[str, Any]:
        """The corpus, as a list an editor can pick a provenance anchor from."""
        out = []
        for key, judgment in sorted(self.kb.judgments.items()):
            ambiguous = sorted(
                {
                    p.number
                    for p in judgment.paragraphs
                    if judgment.ambiguous_anchor(p.number)
                }
            )
            out.append(
                {
                    "key": judgment.canonical_key(),
                    "id": judgment.id,
                    "title": judgment.title,
                    "neutral_citation": judgment.neutral_citation,
                    "court": judgment.court.value,
                    "date_decided": judgment.date_decided.isoformat(),
                    "synthetic": judgment.synthetic,
                    "paragraphs": [
                        {
                            "number": p.number,
                            "judge": p.judge,
                            "anchor": p.scoped_anchor,
                            "role": getattr(p.reasons_role, "value", str(p.reasons_role)),
                        }
                        for p in judgment.paragraphs
                    ],
                    # Anchors this judgment cannot resolve, so the editor knows
                    # which numbers it must not offer as safe choices.
                    "ambiguous_anchors": ambiguous,
                }
            )
        return {"authorities": out}

    def provisions(self) -> dict[str, Any]:
        out = []
        for key, provision in sorted(self.kb.provisions.items()):
            out.append(
                {
                    "key": provision.key,
                    "act_title": provision.act_title,
                    "act_year": provision.act_year,
                    "section": provision.section,
                    "heading": provision.heading,
                    "jurisdiction": provision.jurisdiction.value,
                    "compiled_as_at": provision.compiled_as_at.isoformat(),
                    "text": provision.text,
                }
            )
        return {"provisions": out}

    def authority_status(self, key: str) -> dict[str, Any]:
        judgment = self.kb.judgment_for_key(key) or self.kb.judgments.get(key)
        if judgment is None:
            raise Refused(
                f"{key!r} is not in the corpus", reason="authority_not_in_corpus",
                detail={"requested": key},
            )
        resolved = judgment.canonical_key()
        record = self.kb.graph.authority_status(resolved)
        return {
            "key": resolved,
            "title": judgment.title,
            "status": record.status.value,
            "caveat": record.caveat,
            "counted": [self._edge(e) for e in record.evidence],
            "advisory": [self._edge(e) for e in record.advisory_evidence],
            "hierarchy_blocked": [self._edge(e) for e in record.hierarchy_blocked],
        }

    def review_sheet(self, ruleset_id: str, *, limit: int = 40) -> dict[str, Any]:
        """The decision table an expert adjudicates, as data.

        The generated cases are returned with the engine's conclusion attached
        and nothing in the place where the expert's answer goes. Rendering it as
        a table with a pre-filled verdict would collect confirmation, which is
        the opposite of what the artefact is for.
        """
        from ratio.rules.generate_cases import generate_and_analyse

        ruleset = self._require(ruleset_id).ruleset
        cases, coverage = generate_and_analyse(
            ruleset, credence=self._credence_resolver(), max_cases=4096
        )
        return {
            "ruleset": ruleset.id,
            "version": ruleset.version,
            "status": ruleset.status,
            "is_approved": ruleset.is_approved,
            "coverage": {
                "total_cases": coverage.total_cases,
                "conclusion_counts": coverage.conclusion_counts,
                "inert_leaves": coverage.inert_leaves,
                "never_determined": coverage.never_determined,
                "unexercised_defeaters": coverage.unexercised_defeaters,
                "unknown_fields": coverage.unknown_fields,
                "is_healthy": coverage.is_healthy,
            },
            "fields": sorted({f for c in cases for f in c.facts}),
            "rows": [
                {
                    "case_id": c.case_id,
                    "facts": c.facts,
                    "conclusion": c.conclusion,
                    "defeaters_fired": list(c.defeaters_fired),
                    "gaps": list(c.gaps),
                }
                for c in cases[:limit]
            ],
            "shown": min(limit, len(cases)),
            "total": len(cases),
        }

    # ------------------------------------------------------------------
    # candidate operations: nothing is written
    # ------------------------------------------------------------------

    def preview(self, candidate: dict, facts: dict[str, Any]) -> dict[str, Any]:
        """Evaluate a candidate ruleset against a fact base.

        The proof tree is returned whole, including the defeats and the gaps.
        ``docs/06`` is emphatic that this is not an advanced view: an editor that
        tells an expert a conclusion without showing the derivation has replaced
        an explainable system with an oracle at exactly the moment the expert is
        trying to check the encoding.
        """
        ruleset = self._parse_candidate(candidate)
        try:
            engine = RuleEngine(ruleset, credence=self._credence_resolver())
        except RuleEvaluationError as exc:
            raise Refused(
                f"ruleset {ruleset.id!r} fails validation and cannot be evaluated",
                reason="ruleset_failed_validation",
                detail={"issues": [str(a) for a in exc.args]},
            ) from exc
        tree = engine.evaluate(facts)
        return {
            "ruleset": ruleset.id,
            "version": ruleset.version,
            "status": ruleset.status,
            "is_approved": ruleset.is_approved,
            "facts": facts,
            "conclusion": tree.conclusion,
            "gaps": list(tree.gaps),
            "defeaters_fired": list(tree.defeaters_fired),
            "findings": [
                {
                    "node": f.node_id,
                    "label": f.label,
                    "outcome": f.outcome,
                    "statement": f.statement,
                    "observed": f.observed,
                    "note": f.note,
                    "provenance": [
                        {
                            "kind": p.kind,
                            "judgment": p.judgment_id,
                            "paragraph": p.paragraph,
                            "provision_key": p.provision_key,
                        }
                        for p in f.provenance
                    ],
                }
                for f in tree.findings
            ],
        }

    def signature_block(
        self, ruleset_id: str, *, candidate: dict | None = None,
        approver: str = "", role: str = "", note: str | None = None,
    ) -> dict[str, Any]:
        """What the expert is being asked to sign, and what it binds.

        The digest here is :attr:`RuleSet.approval_digest` -- the hash the
        revision will carry *once approved*. Showing the hash of the draft
        instead is the defect this project spent a while shipping: the sign-off
        changes ``status``, ``status`` is inside the hashed payload, and so a
        digest taken before signing can never match after it.
        """
        ruleset = (
            self._parse_candidate(candidate)
            if candidate is not None
            else self._require(ruleset_id).ruleset
        )
        issues = validate_ruleset(ruleset)
        diff = self._diff_vs_signed(ruleset)
        return {
            "ruleset": ruleset.id,
            "version": ruleset.version,
            "current_status": ruleset.status,
            "digest": ruleset.approval_digest,
            "issues": issues,
            "signable": not issues,
            "diff_vs_signed": diff,
            "block": _approval_block(ruleset, approver=approver, role=role, note=note),
        }

    # ------------------------------------------------------------------
    # writes
    # ------------------------------------------------------------------

    def save(
        self,
        ruleset_id: str,
        candidate: dict,
        *,
        editor: str,
        note: str | None = None,
    ) -> dict[str, Any]:
        """Persist a candidate revision.

        Refused when the revision fails structural validation. Not because
        invalid rules are dangerous to hold -- an expert halfway through writing
        one is entitled to save their work -- but because ``load_corpus`` refuses
        to build a corpus containing an invalid ruleset, so writing one would
        make the whole service unable to start. The distinction that matters is
        between the checks: a *structural* failure blocks the save, while
        provenance that does not resolve does not. A dangling provision key is a
        state an expert may need to save while they go and find the provision,
        and unresolved provenance is reported loudly rather than refused.

        One structural failure is worth naming because it is not obvious from the
        outside. Editing a ruleset that carries an approval, and leaving
        ``status: approved`` on it, does not validate: the approval names a hash
        of the revision it was made over, and the revision has changed. So the
        editor demotes an approved ruleset to ``draft`` when it is edited, and
        this method will not do that for it -- a service that quietly changed a
        governance field to make a write succeed is the failure mode this whole
        module is arranged against. The refusal is legible ("it has been edited
        since sign-off"), which is what the surface needs to prompt for the
        demotion rather than surprise the expert with it.
        """
        ruleset = self._parse_candidate(candidate)
        check_ruleset_id(ruleset.id)
        if ruleset_id and ruleset.id != ruleset_id:
            raise Refused(
                f"this save names ruleset {ruleset_id!r} but the revision's id is "
                f"{ruleset.id!r}; changing a ruleset's id would orphan its history",
                reason="id_mismatch",
                detail={"expected": ruleset_id, "got": ruleset.id},
            )
        if not editor or not editor.strip():
            raise Refused(
                "a save must name the person making it; the journal records who "
                "changed a rulebook and why, and an anonymous entry records neither",
                reason="editor_required",
            )

        issues = validate_ruleset(ruleset)
        if issues:
            raise Refused(
                f"ruleset {ruleset.id!r} does not validate and will not be written",
                reason="ruleset_failed_validation",
                detail={"issues": issues},
            )

        previous = self.store.get(ruleset.id)
        before = previous.ruleset.current_approval if previous else None
        previous_status = previous.ruleset.status if previous else None

        path = self.store.write(ruleset)
        self.store.journal(
            {
                "action": "save",
                "ruleset": ruleset.id,
                "editor": editor,
                "note": note,
                "before_digest": before.artefact_hash if before else None,
                "after_digest": ruleset.approval_digest,
                "previous_status": previous_status,
                "previous_was_approved": bool(previous and previous.ruleset.is_approved),
                "invalidated_approval": bool(previous and previous.ruleset.is_approved
                                             and ruleset.status != "approved"),
            }
        )
        self.reload()

        payload = self.ruleset(ruleset.id)
        payload["wrote"] = str(path)
        payload["invalidated_approval"] = bool(
            previous_status == "approved" and ruleset.status != "approved"
        )
        # Warnings, not refusals: these are the things the expert should fix but
        # which must not stop them persisting a revision they are mid-way through.
        payload["warnings"] = payload["unresolved_provenance"] + payload["anchor_problems"]
        return payload

    def sign(
        self,
        ruleset_id: str,
        candidate: dict,
        *,
        approver: str,
        role: str,
        note: str | None,
        confirm_digest: str,
    ) -> dict[str, Any]:
        """Record an expert's signature over a revision.

        Three conditions, and each of them is the reason a different part of the
        governance story holds.

        The revision must validate, because signing an artefact the engine will
        refuse to load produces an "approved" rulebook that cannot be used --
        which is worse than a draft, since a draft is at least honestly unusable.

        The approver and their capacity must be non-empty, because an approval
        with no author is not a signature, and the whole claim this system makes
        is that a conclusion can be traced to the person accountable for it.

        And ``confirm_digest`` must equal the digest the expert was shown. This
        is the friction that makes the act deliberate: copying a hash is not
        reading one, and the value of the approval is that somebody looked at the
        artefact it binds. The service cannot check that the expert read the
        rules; it can check that they had the digest in front of them.
        """
        ruleset = self._parse_candidate(candidate)
        if not approver.strip() or not role.strip():
            raise Refused(
                "a signature needs a name and a capacity: an approval without an "
                "author is not a signature",
                reason="author_required",
            )

        issues = validate_ruleset(ruleset)
        if issues:
            raise Refused(
                f"ruleset {ruleset.id!r} does not validate and will not be signed",
                reason="ruleset_failed_validation",
                detail={"issues": issues},
            )

        digest = ruleset.approval_digest
        if (confirm_digest or "").strip().lower() != digest:
            raise Refused(
                "the digest you confirmed is not the digest of this revision. "
                "Nothing has been written. Re-read the digest and confirm it "
                "again -- the point of the step is that it is not a formality.",
                reason="digest_not_confirmed",
                detail={"expected": digest, "given": confirm_digest},
            )

        approval = Approval(
            approver=approver.strip(),
            role=role.strip(),
            approved_at=datetime.now(UTC),
            artefact_hash=digest,
            note=note,
        )
        signed = ruleset.model_copy(
            update={"status": "approved", "approvals": (*ruleset.approvals, approval)}
        )

        # The invariant, checked rather than assumed: a ruleset that reports
        # itself approved must be one whose approval matches its own revision.
        remaining = validate_ruleset(signed)
        if remaining:  # pragma: no cover - a failure here is a defect in this method
            raise Refused(
                "the signed revision does not validate; this is a defect in the "
                "authoring service, not in your ruleset, and nothing was written",
                reason="signature_did_not_bind",
                detail={"issues": remaining},
            )

        path = self.store.write(signed)
        self.store.journal(
            {
                "action": "approve",
                "ruleset": signed.id,
                "approver": approval.approver,
                "role": approval.role,
                "note": approval.note,
                "hash": digest,
                # The signed revision's own payload, kept because it is the only
                # surviving copy: the moment the revision is edited the file holds
                # the edit, and a diff against what was signed has nothing to
                # diff against.
                "payload": signed.canonical_payload(),
            }
        )
        self.reload()
        payload = self.ruleset(signed.id)
        payload["signed"] = str(path)
        payload["warnings"] = payload["unresolved_provenance"] + payload["anchor_problems"]
        return payload

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _require(self, ruleset_id: str):
        entry = self.store.get(ruleset_id)
        if entry is None:
            available = sorted(self.store.entries)
            raise Refused(
                f"no ruleset {ruleset_id!r}; loaded: {available}",
                reason="unknown_ruleset",
                detail={"available": available},
            )
        return entry

    def _parse_candidate(self, candidate: dict) -> RuleSet:
        if not isinstance(candidate, dict):
            raise Refused(
                "a ruleset candidate must be an object", reason="invalid_ruleset",
                detail={"issues": ["expected a JSON object"]},
            )
        try:
            return RuleSet.model_validate(candidate)
        except ValidationError as exc:
            # Pydantic's message is precise and unreadable. An expert editing a
            # leaf without a predicate should be told which field is missing, in
            # the terms of the model they are editing.
            raise Refused(
                "this revision is not a well-formed ruleset",
                reason="invalid_ruleset",
                detail={"issues": [_format_validation_error(e) for e in exc.errors()]},
            ) from exc

    def _provenance_problems(self, ruleset: RuleSet) -> list[str]:
        return provenance_problems(
            ruleset,
            judgment_ids=set(self.kb.judgments),
            provision_keys=set(self.kb.provisions),
        )

    def _credence_resolver(self):
        if not self.bundle.credence_engines:
            return None
        engine = self.bundle.credence_engines[0]

        def resolve(question: str, facts: dict) -> float:
            target = engine.network.target
            for node in engine.network.nodes:
                if question.strip().lower() == node.question.strip().lower():
                    target = node.id
                    break
            try:
                return float(engine.query({}, target=target).get("yes", float("nan")))
            except Exception:  # noqa: BLE001 - degrade to unknown, never to a number
                return float("nan")

        return resolve

    def _ruleset_payload(self, ruleset: RuleSet) -> dict[str, Any]:
        approval = ruleset.current_approval
        return {
            "id": ruleset.id,
            "title": ruleset.title,
            "question": ruleset.question,
            "domain": ruleset.domain,
            "version": ruleset.version,
            "effective_from": ruleset.effective_from.isoformat(),
            "effective_to": (
                ruleset.effective_to.isoformat() if ruleset.effective_to else None
            ),
            "root": ruleset.root,
            "status": ruleset.status,
            "notes": ruleset.notes,
            "provision_keys": list(ruleset.provision_keys),
            "is_approved": ruleset.is_approved,
            # Two digests, and they mean different things. ``digest`` is what a
            # signature on this revision must carry; ``current_hash`` is what the
            # revision hashes to as it stands. They differ whenever the status is
            # not already "approved", and showing only one of them is how a
            # sign-off flow ends up binding a hash nobody can reproduce.
            "digest": ruleset.approval_digest,
            "current_hash": Approval.hash_of(ruleset.canonical_payload()),
            "approval_count": len(ruleset.approvals),
            # The model's own field, in the model's own shape, and not only the
            # display views below. A client edits a ruleset by taking the payload
            # this endpoint returns and posting it back, so anything present on
            # the model but absent here is silently deleted by the first save --
            # and the thing that would have been deleted is the approval history.
            # ``approval`` and ``historical_approvals`` are for showing; this is
            # for sending.
            "approvals": [a.model_dump(mode="json") for a in ruleset.approvals],
            "approval": (
                {
                    "approver": approval.approver,
                    "role": approval.role,
                    "approved_at": approval.approved_at.isoformat(),
                    "artefact_hash": approval.artefact_hash,
                    "note": approval.note,
                }
                if approval
                else None
            ),
            "historical_approvals": [
                {
                    "approver": a.approver,
                    "role": a.role,
                    "approved_at": a.approved_at.isoformat(),
                    "artefact_hash": a.artefact_hash,
                }
                for a in ruleset.approvals
                if approval is None or a.artefact_hash != approval.artefact_hash
            ],
        }

    def _node_payload(self, node: RuleNode) -> dict[str, Any]:
        return {
            "id": node.id,
            "kind": node.kind.value,
            "label": node.label,
            "statement": node.statement,
            "children": list(node.children),
            "exception_of": node.exception_of,
            "predicate": (
                {"field": node.predicate.field, "op": node.predicate.op,
                 "value": node.predicate.value}
                if node.predicate
                else None
            ),
            "bn_query": node.bn_query,
            "bn_threshold": node.bn_threshold,
            "provenance": [
                {
                    "kind": ref.kind,
                    "judgment_id": ref.judgment_id,
                    "paragraph": ref.paragraph,
                    "provision_key": ref.provision_key,
                    "proposition_id": ref.proposition_id,
                    "quote": ref.quote,
                    "note": ref.note,
                }
                for ref in node.provenance
            ],
        }

    def _provenance_resolution(self, ruleset: RuleSet) -> list[dict[str, Any]]:
        """Resolve every provenance pointer, and report what it resolves to.

        This is the data behind the surface's central requirement: the expert
        sees, beside the node they are editing, the paragraph it points at, in
        the judgment's own words, with the anchor scoped by judge and the
        authority's status and caveat attached. Not a link. The words.

        The statuses are not a boolean. "Resolved" is not one thing:

        ``resolved``
            The pointer names a document and there is nothing further to check.
        ``quote_verified``
            The pointer names a paragraph and the quoted words are in it.
        ``quote_not_in_paragraph``
            The pointer names a paragraph and the quoted words are not in it.
            The dangerous one, because the citation looks checked.
        ``paragraph_missing`` / ``ambiguous_anchor`` / ``judgment_missing``
            The anchor does not hold, for three different reasons an expert needs
            told apart -- one is a typo, one is a judgment whose reasons are
            numbered per judge, and one is a judgment that is not here at all.
        ``provision_missing`` / ``names_no_source``
            The same, for the statutory and expert-statement branches.
        """
        out: list[dict[str, Any]] = []
        for node in ruleset.nodes:
            for index, ref in enumerate(node.provenance):
                out.append(self._resolve_ref(node.id, index, ref))
        return out

    def _resolve_ref(self, node_id: str, index: int, ref: AuthorityRef) -> dict[str, Any]:
        base: dict[str, Any] = {"node": node_id, "index": index, "ref": _ref_dict(ref)}

        if ref.kind == "statute":
            provision = self.kb.provisions.get(ref.provision_key or "")
            if provision is None:
                return {**base, "status": "provision_missing", "text": None,
                        "detail": f"{ref.provision_key!r} is not a provision in the corpus"}
            return {
                **base,
                "status": "resolved",
                "citation": f"{provision.act_title} {provision.act_year} (Cth) "
                            f"s {provision.section}",
                "anchor": f"s {provision.section}",
                "heading": provision.heading,
                "text": provision.text,
                "compiled_as_at": provision.compiled_as_at.isoformat(),
                "detail": None,
            }

        if ref.kind == "expert_statement":
            if not ref.proposition_id:
                return {**base, "status": "names_no_source", "text": None,
                        "detail": "an expert statement with no proposition id names "
                                  "no source at all"}
            return {**base, "status": "resolved", "citation": ref.proposition_id,
                    "anchor": ref.proposition_id, "text": ref.note, "detail": None}

        judgment = self.kb.judgments.get(ref.judgment_id or "")
        if judgment is None:
            return {**base, "status": "judgment_missing", "text": None,
                    "detail": f"{ref.judgment_id!r} is not a judgment in the corpus"}

        citations = {
            "citation": judgment.neutral_citation,
            "title": judgment.title,
            "court": judgment.court.value,
            "synthetic": judgment.synthetic,
        }
        record = self.kb.graph.authority_status(judgment.canonical_key())
        citations["authority_status"] = record.status.value
        citations["authority_caveat"] = record.caveat

        matches = [p for p in judgment.paragraphs if p.number == ref.paragraph]
        if len(matches) > 1:
            return {
                **base, **citations, "status": "ambiguous_anchor", "text": None,
                "anchor": f"[{ref.paragraph}]",
                "detail": f"this judgment's reasons are numbered separately by "
                          f"{len(matches)} judges, so [{ref.paragraph}] names "
                          f"{len(matches)} paragraphs and the reference does not say "
                          "whose",
                "judges": [p.judge for p in matches],
            }
        if not matches:
            return {
                **base, **citations, "status": "paragraph_missing", "text": None,
                "anchor": f"[{ref.paragraph}]",
                "detail": f"this judgment contains no paragraph [{ref.paragraph}]",
            }

        paragraph = matches[0]
        anchor = paragraph.scoped_anchor
        if not ref.quote:
            return {**base, **citations, "status": "resolved", "anchor": anchor,
                    "text": paragraph.text, "detail": None}

        found = normalise_quote(ref.quote) in normalise_quote(paragraph.text)
        return {
            **base, **citations,
            "status": "quote_verified" if found else "quote_not_in_paragraph",
            "anchor": anchor,
            "text": paragraph.text,
            "detail": None if found else (
                "the quoted words are not in this paragraph. A quotation that is "
                "not verbatim is a paraphrase wearing a citation."
            ),
        }

    def _diff_vs_signed(self, ruleset: RuleSet) -> dict[str, Any] | None:
        signed = self.store.last_signed_payload(ruleset.id)
        entries = self.store.journal_entries(ruleset.id)
        last = next(
            (e for e in reversed(entries) if e.get("action") == "approve"), None
        )
        if signed is None or last is None:
            return None
        current = ruleset.model_dump(mode="json", exclude_none=True)
        return _diff_payloads(signed, current, signed_at=last.get("at"),
                              signed_by=last.get("approver"))

    def _edge(self, edge) -> dict[str, Any]:
        return {
            "citing_judgment": edge.citing_judgment,
            "paragraph": edge.citing_paragraph,
            "treatment": edge.treatment.value,
            "quote": edge.quote,
            "confidence": edge.confidence,
            "confirmed_by": edge.confirmed_by,
            "via": edge.via,
            "needs_review": edge.needs_review,
        }


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _ref_dict(ref: AuthorityRef) -> dict[str, Any]:
    return {
        "kind": ref.kind,
        "judgment_id": ref.judgment_id,
        "paragraph": ref.paragraph,
        "provision_key": ref.provision_key,
        "proposition_id": ref.proposition_id,
        "quote": ref.quote,
        "note": ref.note,
    }


def _format_validation_error(error: dict[str, Any]) -> str:
    location = ".".join(str(part) for part in error.get("loc", ())) or "(root)"
    return f"{location}: {error.get('msg', 'invalid')}"


def _approval_block(
    ruleset: RuleSet, *, approver: str, role: str, note: str | None
) -> str:
    """The YAML block ``ratio rules approve`` prints, ready to paste.

    Offered so that an expert who prefers to finish the act in the file -- the
    way the CLI intends -- can. The sign-off in the interface is a separate,
    deliberate operation; this is the escape hatch back to the plain text.
    """
    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    return "\n".join(
        [
            "approvals:",
            f"  - approver: {approver!r}",
            f"    role: {role!r}",
            f"    approved_at: {stamp!r}",
            f"    artefact_hash: {ruleset.approval_digest}",
            "    note: >-",
            f"      {note or 'Reviewed against the review sheet.'}",
            "",
            "and set `status: approved`.",
        ]
    )


def _diff_payloads(
    signed: dict[str, Any], current: dict[str, Any], *,
    signed_at: str | None, signed_by: str | None,
) -> dict[str, Any]:
    """Structural diff between a signed revision and the current one."""
    ruleset_fields = ("title", "question", "domain", "version", "root",
                      "effective_from", "effective_to", "status", "notes")
    changes = []
    for name in ruleset_fields:
        before, after = signed.get(name), current.get(name)
        if before != after:
            changes.append({"field": name, "from": before, "to": after})
    if signed.get("provision_keys") != current.get("provision_keys"):
        changes.append({
            "field": "provision_keys",
            "from": signed.get("provision_keys"),
            "to": current.get("provision_keys"),
        })

    before_nodes = {n["id"]: n for n in signed.get("nodes", [])}
    after_nodes = {n["id"]: n for n in current.get("nodes", [])}
    added = sorted(set(after_nodes) - set(before_nodes))
    removed = sorted(set(before_nodes) - set(after_nodes))

    node_fields = ("kind", "label", "statement", "children", "predicate",
                   "provenance", "exception_of", "bn_query", "bn_threshold")
    changed: list[dict[str, Any]] = []
    for node_id in sorted(set(before_nodes) & set(after_nodes)):
        before, after = before_nodes[node_id], after_nodes[node_id]
        fields = [
            {"field": name, "from": before.get(name), "to": after.get(name)}
            for name in node_fields
            if before.get(name) != after.get(name)
        ]
        if fields:
            # The order of children is part of the encoding's readability only if
            # the set changed; a reordering is reported, because the review sheet
            # column order follows declaration order.
            changed.append({"id": node_id, "fields": fields})

    return {
        "signed_at": signed_at,
        "signed_by": signed_by,
        "signed_version": signed.get("version"),
        "current_version": current.get("version"),
        "changed": bool(changes or added or removed or changed),
        "ruleset_changes": changes,
        "nodes_added": added,
        "nodes_removed": removed,
        "nodes_changed": changed,
        "summary": _diff_summary(changes, added, removed, changed),
    }


def _diff_summary(
    changes: list[dict[str, Any]],
    added: list[str],
    removed: list[str],
    changed: list[dict[str, Any]],
) -> list[str]:
    lines = [
        f"{c['field']}: {_short(c['from'])} -> {_short(c['to'])}" for c in changes
    ]
    lines += [f"node {node_id!r} added" for node_id in added]
    lines += [f"node {node_id!r} removed" for node_id in removed]
    for entry in changed:
        fields = ", ".join(f["field"] for f in entry["fields"])
        lines.append(f"node {entry['id']!r}: {fields}")
    return lines


def _short(value: Any, limit: int = 60) -> str:
    text = "-" if value is None else str(value)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


__all__ = ["AuthoringService", "Refused", "StoreError"]
