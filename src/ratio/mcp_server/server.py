"""A Model Context Protocol server over the judgment knowledge base.

Two design decisions shape everything in this module, and both are consequences
of what the corpus is rather than of what MCP can do.

**Resource-first, tool-second.** A tool is a *verb the model chooses*; a resource
is a *thing the model can look at*. This corpus is a body of authority -- it is
read, not operated. So the judgment text, the treatment history, the provision
text and the encoded ruleset are all exposed as resources, addressable by a URI
that names exactly one object, and a client can cite one down to the paragraph.
The tools exist only where a question genuinely needs computing: ranking
paragraphs, verifying a claim's quotes and citations, applying a ruleset to a
fact base. Every tool result names the resources it rests on, so a client can
follow any assertion back to a paragraph of a judgment.

**Stateless, and explicit about the case.** This server holds no per-client
session state and no notion of "the current matter". Every call that depends on
context takes that context as an argument -- a point-in-time date, a court
filter -- and every result says what context it was computed under. The reason is
not the protocol's; it is the audit's. A legal answer is only meaningful relative
to a date and a fact base, and a system that remembers "the case" produces
answers whose provenance cannot be reconstructed from the call that produced
them. In a deployment that logs every tool call, that is the difference between
an auditable transcript and a pile of unattributable output.

Nothing here is a substitute for the deterministic core: every method on
:class:`RatioService` returns plain JSON-serialisable data produced by the same
code paths the CLI and the evaluator use. The MCP layer is a transport, and it is
kept thin deliberately -- a transport is the easiest place in a system like this
to accidentally reimplement a policy.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from ratio.ingest.fixtures import CorpusBundle
from ratio.kb.index import KnowledgeBase
from ratio.model import RuleSet

# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


class Refusal(Exception):
    """The server declined to answer, and says why.

    A refusal is a first-class result, not an error. It is what the server
    returns when the corpus does not contain the authority a claim relies on, or
    a fact the ruleset needs, or a judgment the client named. Reporting those as
    failures of the *call* would be misleading in the same way reporting an
    abstention as a wrong answer is: the system did the right thing, and the
    transport called it a fault.
    """

    def __init__(self, message: str, *, reason: str, detail: dict[str, Any] | None = None):
        super().__init__(message)
        self.reason = reason
        self.detail = detail or {}

    def to_payload(self) -> dict[str, Any]:
        return {"refused": True, "reason": self.reason, "message": str(self), **self.detail}


# --------------------------------------------------------------------------
# Service: the testable core
# --------------------------------------------------------------------------


@dataclass
class RatioService:
    """Every MCP operation, as a plain function over JSON-serialisable data.

    Kept separate from the protocol adapter so that the behaviour can be tested
    without a transport, a subprocess or a client library. The adapter below
    contains no policy of its own; if a rule about *what may be said* appears in
    the adapter rather than here, it is in the wrong place and the tests will not
    see it.
    """

    bundle: CorpusBundle
    kb: KnowledgeBase
    rulesets: dict[str, RuleSet]

    @classmethod
    def from_paths(
        cls,
        fixtures: Path,
        *,
        allow_mixed: bool = False,
    ) -> RatioService:
        from ratio.ingest.fixtures import load_corpus

        bundle = load_corpus(fixtures, allow_mixed=allow_mixed)
        return cls(
            bundle=bundle,
            kb=KnowledgeBase.build(bundle),
            rulesets={r.id: r for r in bundle.rulesets},
        )

    # -- helpers -----------------------------------------------------------

    def unresolved_provenance(self, ruleset_id: str) -> list[str]:
        """Provenance in ``ruleset_id`` that names nothing in the corpus.

        Exposed on the ruleset resource rather than raised, because a dangling
        provenance key is not a reason to refuse to show a lawyer the rule: it is
        a reason to tell them, precisely, that the rule's stated basis cannot be
        found. Returning the rule with its problems attached is the only form of
        this answer that lets them fix it.
        """
        from ratio.rules.engine import provenance_problems

        ruleset = self.rulesets.get(ruleset_id)
        if ruleset is None:
            return []
        return provenance_problems(
            ruleset,
            judgment_ids=set(self.kb.judgments),
            provision_keys=set(self.kb.provisions),
        )

    def _require_judgment(self, key: str):
        judgment = self.kb.judgment_for_key(key) or self.kb.judgments.get(key)
        if judgment is None:
            resolved, alias = self.kb.resolve_authority(key)
            if resolved is None:
                raise Refusal(
                    f"{key!r} is neither an authority in the corpus nor a case "
                    "name the corpus evidences",
                    reason="authority_not_in_corpus",
                    detail={"requested": key},
                )
            judgment = self.kb.judgment_for_key(resolved)
        return judgment

    def _require_ruleset(self, ruleset_id: str) -> RuleSet:
        ruleset = self.rulesets.get(ruleset_id)
        if ruleset is None:
            if not self.rulesets:
                raise Refusal(
                    "no rulesets are loaded, so there is nothing to apply",
                    reason="no_rulesets",
                )
            # A single-ruleset deployment is the common case, and requiring the
            # caller to name the only rule there is adds a failure mode without
            # adding information. The substitution is reported in the result.
            if len(self.rulesets) == 1 and not ruleset_id:
                return next(iter(self.rulesets.values()))
            raise Refusal(
                f"no ruleset {ruleset_id!r}; loaded: {sorted(self.rulesets)}",
                reason="unknown_ruleset",
                detail={"available": sorted(self.rulesets)},
            )
        return ruleset

    # -- resources ---------------------------------------------------------

    def corpus_summary(self) -> dict[str, Any]:
        """``ratio://corpus`` -- what this server holds, and what it is.

        The ``synthetic`` flag travels at the top level and is not optional. A
        client that puts this corpus in front of a user as though it were real
        Australian authority would be fabricating law, and the one place that can
        be prevented is here.
        """
        return {
            "judgments": len(self.kb.judgments),
            "provisions": len(self.kb.provisions),
            "paragraphs_indexed": len(self.kb.refs),
            "rulesets": sorted(self.rulesets),
            "networks": [e.network.id for e in self.bundle.credence_engines],
            "synthetic": self.bundle.has_synthetic,
            "has_real_judgments": self.bundle.has_real,
            "case_names_resolved": dict(sorted(self.kb.aliases.items())),
            "warnings": list(self.bundle.problems) + list(self.kb.warnings),
        }

    def judgment(self, key: str) -> dict[str, Any]:
        """``ratio://judgment/{key}`` -- one judgment, paragraph by paragraph."""
        judgment = self._require_judgment(key)
        court = judgment.court.value if hasattr(judgment.court, "value") else str(judgment.court)
        return {
            "key": judgment.canonical_key(),
            "id": judgment.id,
            "title": judgment.title,
            "neutral_citation": judgment.neutral_citation,
            "court": court,
            "date_decided": judgment.date_decided.isoformat(),
            "synthetic": judgment.synthetic,
            "paragraphs": [
                {
                    "judge": p.judge,
                    "number": p.number,
                    "role": getattr(p.reasons_role, "value", str(p.reasons_role)),
                    "text": p.text,
                }
                for p in judgment.paragraphs
            ],
        }

    def provision(self, key: str) -> dict[str, Any]:
        """``ratio://provision/{key}`` -- one provision of an Act."""
        provision = self.kb.provisions.get(key)
        if provision is None:
            raise Refusal(
                f"{key!r} is not a provision in the corpus",
                reason="provision_not_in_corpus",
                detail={"requested": key},
            )
        return {
            "key": provision.key,
            "act_title": provision.act_title,
            "section": provision.section,
            "text": getattr(provision, "text", None),
        }

    def authority_status(self, key: str, *, as_at: str | None = None) -> dict[str, Any]:
        """``ratio://authority/{key}/status`` -- is this still good law?

        ``as_at`` is an explicit argument rather than a server-side notion of the
        current matter. It is reported back in the result, because a status
        computed as at a date that is not in the answer is a status nobody can
        check.
        """
        judgment = self._require_judgment(key)
        resolved = judgment.canonical_key()
        as_at_date = date.fromisoformat(as_at) if as_at else None
        record = self.kb.graph.authority_status(resolved, as_at=as_at_date)
        return {
            "key": resolved,
            "title": judgment.title,
            "as_at": as_at_date.isoformat() if as_at_date else None,
            "status": record.status.value,
            "caveat": record.caveat,
            "authority": {
                "evidence": [_edge_payload(e) for e in record.evidence],
                "advisory_evidence": [_edge_payload(e) for e in record.advisory_evidence],
                "hierarchy_blocked": [_edge_payload(e) for e in record.hierarchy_blocked],
            },
            "counted": len(record.evidence),
            "not_counted_advisory": len(record.advisory_evidence),
            "not_counted_hierarchy": len(record.hierarchy_blocked),
        }

    def ruleset(self, ruleset_id: str) -> dict[str, Any]:
        """``ratio://ruleset/{id}`` -- the encoded rule, with its approvals."""
        ruleset = self._require_ruleset(ruleset_id)
        approval = ruleset.current_approval
        return {
            "id": ruleset.id,
            "title": ruleset.title,
            "question": ruleset.question,
            "version": ruleset.version,
            "status": ruleset.status,
            "is_approved": ruleset.is_approved,
            "approved_by": (
                {"approver": approval.approver, "role": approval.role,
                 "approved_at": approval.approved_at.isoformat()}
                if approval
                else None
            ),
            "provision_keys": list(ruleset.provision_keys),
            "nodes": [
                {
                    "id": n.id,
                    "kind": n.kind.value,
                    "label": n.label,
                    "statement": n.statement,
                    "children": list(n.children),
                    "exception_of": n.exception_of,
                    "predicate": (
                        {"field": n.predicate.field, "op": n.predicate.op,
                         "value": n.predicate.value}
                        if n.predicate
                        else None
                    ),
                    "provenance": [_authority_payload(p) for p in n.provenance],
                }
                for n in ruleset.nodes
            ],
            # Provenance that does not resolve is provenance that does not
            # exist. Structural validation checks that a node *has*
            # provenance; only the corpus can say whether the key names
            # anything, and a client showing a lawyer "this rule rests on
            # s 18" must not be showing them a key that resolves to nothing.
            "unresolved_provenance": self.unresolved_provenance(ruleset.id),
        }

    # -- tools -------------------------------------------------------------

    def search(
        self,
        query: str,
        *,
        limit: int = 8,
        courts: list[str] | None = None,
        as_at: str | None = None,
        binding_only: bool = False,
        require_good_law: bool = False,
        exclude_synthetic: bool = False,
        min_coverage: float = 0.5,
    ) -> dict[str, Any]:
        """Rank paragraphs bearing on a question, and refuse when there are none.

        The coverage gate is applied here exactly as it is in the evaluator, and
        for the same reason: a retriever that always returns its best hit is
        confabulation with an index in front of it, and the difference between
        "here is the paragraph that governs" and "here is the paragraph that
        shares the most words with your question" is invisible in a ranked list.
        The score of each result is **not** returned as a confidence; only the
        coverage is, because coverage is the quantity that was actually
        calibrated.
        """
        from ratio.model import Court
        from ratio.retrieve.hybrid import HybridRetriever, RetrievalFilters

        filters = RetrievalFilters(
            courts=tuple(courts or ()),
            date_to=date.fromisoformat(as_at) if as_at else None,
            min_court_rank=Court.FCA.rank if binding_only else None,
            require_good_law=require_good_law,
            exclude_synthetic=exclude_synthetic,
        )
        retriever = HybridRetriever(self.kb)
        results = retriever.search(query, filters=filters, limit=limit, explain=True)

        from ratio.eval.systems import RetrievalSystem

        gate = RetrievalSystem(self.kb, min_coverage=min_coverage)
        payload = []
        for result in results:
            coverage = gate.coverage(query, result.matched_terms)
            payload.append(
                {
                    "citation": result.ref.citation_label,
                    "judgment": result.ref.judgment_id,
                    "paragraph": result.ref.paragraph,
                    "judge": result.ref.judge,
                    "court": result.ref.court,
                    "date_decided": result.ref.date_decided,
                    "heading": result.ref.heading,
                    "text": result.ref.text,
                    "coverage": round(coverage, 4),
                    "authority_caveat": result.authority_caveat,
                }
            )

        if not payload:
            raise Refusal(
                "no paragraph in the corpus contains any term of this query",
                reason="nothing_indexed_matches",
                detail={"query": query},
            )
        top_coverage = payload[0]["coverage"]
        if top_coverage < min_coverage:
            raise Refusal(
                "the best-matching paragraph accounts for only "
                f"{top_coverage:.0%} of the distinctive vocabulary of this "
                "question, which is not enough to answer it. The corpus may not "
                "address this question at all.",
                reason="below_retrieval_coverage",
                detail={
                    "query": query,
                    "coverage": top_coverage,
                    "threshold": min_coverage,
                    "nearest": payload[0]["citation"],
                },
            )
        return {
            "query": query,
            "coverage": top_coverage,
            "filters": {
                "courts": list(courts or ()),
                "as_at": as_at,
                "binding_only": binding_only,
                "require_good_law": require_good_law,
                "exclude_synthetic": exclude_synthetic,
            },
            "results": payload,
        }

    def verify(
        self,
        claim: str,
        *,
        quotes: list[dict] | None = None,
        citations: list[str] | None = None,
    ) -> dict[str, Any]:
        """Check a claim's quotations and citations against the corpus.

        This is the operation that matters most for a model-authored answer: a
        claim of the form "Yarrow holds that X" is worthless unless the words
        quoted are in the paragraph cited. Nothing here scores confidence. A
        quote either resolves to the cited span or it does not, and a claim
        without citation is not clean.
        """
        if self.kb.verifier is None:
            raise Refusal("no verifier is configured", reason="verifier_unavailable")
        report = self.kb.verifier.verify_claim(claim, quotes=quotes, citations=citations)
        return {
            "claim": claim,
            "verdict": report.verdict,
            "is_clean": report.is_clean,
            "quotes": [
                {
                    "citation": f"{q.neutral_citation} at [{q.paragraph}]",
                    "status": q.status,
                    "is_grounded": q.is_grounded,
                    "note": q.note,
                }
                for q in report.quotes
            ],
            "citations": [
                {"raw": c.raw, "status": c.status, "is_safe_to_rely_on": c.is_safe_to_rely_on,
                 "note": c.note}
                for c in report.citations
            ],
            "warnings": list(report.warnings),
            "explanation": report.explain(),
        }

    def apply_rules(
        self,
        facts: dict[str, Any],
        *,
        ruleset_id: str = "",
        as_at: str | None = None,
    ) -> dict[str, Any]:
        """Apply an encoded rule to a fact base.

        Returns ``undetermined`` with an explicit gap list whenever the fact base
        is incomplete, rather than the conclusion the engine reached with a fact
        missing. The gap list names the *fields a user can go and establish*,
        which is the only form of "I cannot answer this" that a user can act on.
        """
        from ratio.rules.engine import RuleEngine, RuleEvaluationError

        ruleset = self._require_ruleset(ruleset_id)
        try:
            engine = RuleEngine(ruleset)
        except RuleEvaluationError as exc:
            # A ruleset that fails validation is a refusal, not a crash.
            #
            # This is the case where the refusal matters most and it was the one
            # case that did not have one: a ruleset edited after sign-off makes
            # ``RuleEngine`` raise, and an exception escaping here reaches a model
            # client as a transport failure rather than as a statement of fact.
            # The client then has two bad options -- swallow it, or retry -- and
            # neither tells its user that the rule they are about to rely on has
            # been altered since an expert approved it. So it becomes the same
            # kind of payload as every other refusal, carrying the validation
            # issues verbatim.
            raise Refusal(
                f"ruleset {ruleset.id!r} failed validation and will not be applied",
                reason="ruleset_failed_validation",
                detail={
                    "ruleset": ruleset.id,
                    "version": ruleset.version,
                    "status": ruleset.status,
                    "is_approved": ruleset.is_approved,
                    "issues": [str(a) for a in exc.args],
                },
            ) from exc
        tree = engine.evaluate(facts)
        return {
            "ruleset": ruleset.id,
            "version": ruleset.version,
            "approved": ruleset.is_approved,
            "as_at": as_at,
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
                    "provenance": [_authority_payload(p) for p in f.provenance],
                    "note": f.note,
                }
                for f in tree.findings
            ],
        }


# --------------------------------------------------------------------------
# payload helpers
# --------------------------------------------------------------------------


def _edge_payload(edge) -> dict[str, Any]:
    return {
        "citing_judgment": edge.citing_judgment,
        "paragraph": edge.citing_paragraph,
        "treatment": edge.treatment.value,
        "quote": edge.quote,
        "confidence": edge.confidence,
        "confirmed_by": edge.confirmed_by,
        "via": edge.via,
    }


def _authority_payload(ref) -> dict[str, Any]:
    return {
        "kind": ref.kind,
        "judgment": ref.judgment_id,
        "paragraph": ref.paragraph,
        "provision_key": ref.provision_key,
        "quote": ref.quote,
    }


# --------------------------------------------------------------------------
# MCP adapter
# --------------------------------------------------------------------------


RESOURCE_TEMPLATES = (
    ("ratio://corpus", "What this server holds, and whether it is real authority"),
    ("ratio://judgment/{key}", "A judgment, paragraph by paragraph"),
    ("ratio://provision/{key}", "A provision of an Act"),
    ("ratio://authority/{key}/status", "Whether an authority is still good law"),
    ("ratio://ruleset/{id}", "An encoded rule, with its approvals and provenance"),
)

TOOLS = (
    "search_authorities",
    "verify_claim",
    "apply_ruleset",
    "authority_status",
)


def build_server(service: RatioService):  # pragma: no cover - requires the mcp extra
    """Wire :class:`RatioService` onto the MCP SDK.

    Imports the SDK inside the function so that the deterministic core, the
    evaluator and the tests all run without the ``mcp`` extra installed. The
    repository's dependency posture is that the legal core is offline and the
    transport is optional, and a top-level import here would quietly reverse
    that.
    """
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:  # pragma: no cover
        raise SystemExit(
            "the mcp extra is not installed; run: pip install 'ratio[mcp]'"
        ) from exc

    server = FastMCP("ratio")

    @server.resource("ratio://corpus")
    def _corpus() -> dict:
        return service.corpus_summary()

    @server.resource("ratio://judgment/{key}")
    def _judgment(key: str) -> dict:
        return service.judgment(key)

    @server.resource("ratio://provision/{key}")
    def _provision(key: str) -> dict:
        return service.provision(key)

    @server.resource("ratio://authority/{key}/status")
    def _status(key: str) -> dict:
        return service.authority_status(key)

    @server.resource("ratio://ruleset/{id}")
    def _ruleset(id: str) -> dict:
        return service.ruleset(id)

    @server.tool()
    def search_authorities(
        query: str,
        limit: int = 8,
        courts: list[str] | None = None,
        as_at: str | None = None,
        binding_only: bool = False,
        require_good_law: bool = False,
    ) -> dict:
        """Rank paragraphs bearing on a question, refusing if the corpus does not
        address it. The ``coverage`` figure is the share of the question's
        distinctive vocabulary the top paragraph accounts for."""
        return _resolve(
            lambda: service.search(
                query,
                limit=limit,
                courts=courts,
                as_at=as_at,
                binding_only=binding_only,
                require_good_law=require_good_law,
            )
        )

    @server.tool()
    def verify_claim(
        claim: str,
        quotes: list[dict] | None = None,
        citations: list[str] | None = None,
    ) -> dict:
        """Check a claim's quotations and citations against the corpus. A quote
        is either grounded in the paragraph cited or it is not; there is no
        confidence score."""
        return _resolve(lambda: service.verify(claim, quotes=quotes, citations=citations))

    @server.tool()
    def apply_ruleset(
        facts: dict,
        ruleset_id: str = "",
        as_at: str | None = None,
    ) -> dict:
        """Apply an encoded rule to a fact base. Returns ``undetermined`` with a
        gap list when the facts are incomplete."""
        return _resolve(lambda: service.apply_rules(facts, ruleset_id=ruleset_id, as_at=as_at))

    @server.tool()
    def authority_status(key: str, as_at: str | None = None) -> dict:
        """Whether an authority is still good law, optionally as at a date.
        Treatment that was recorded but not counted is reported alongside the
        treatment that was."""
        return _resolve(lambda: service.authority_status(key, as_at=as_at))

    return server


def _resolve(thunk):  # pragma: no cover - exercised through the SDK
    """Turn a :class:`Refusal` into a result the client can read.

    Not into a protocol error. A refusal carries the reason and the nearest
    thing the corpus does hold, and a client that receives it as a transport
    failure would retry, whereas a client that receives it as data can tell its
    user what is missing.
    """
    try:
        return thunk()
    except Refusal as refusal:
        return refusal.to_payload()


def main(argv: list[str] | None = None) -> int:  # pragma: no cover
    import argparse

    parser = argparse.ArgumentParser(prog="ratio-mcp", description=__doc__.split("\n")[0])
    parser.add_argument("--fixtures", required=True, type=Path)
    parser.add_argument("--allow-mixed", action="store_true")
    args = parser.parse_args(argv)

    service = RatioService.from_paths(args.fixtures, allow_mixed=args.allow_mixed)
    build_server(service).run()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = ["RESOURCE_TEMPLATES", "TOOLS", "RatioService", "Refusal", "build_server", "main"]
