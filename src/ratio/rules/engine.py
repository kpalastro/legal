"""Defeasible rule evaluation, with proof trees.

The encoding has one unusual feature, and it is the whole point: exceptions are
a distinct node kind from conjuncts.

Most attempts to encode a legal test as a decision tree model "the Minister may
exempt" or "...unless the acquirer is a foreign government investor" as another
AND-branch. That encodes a different rule from the one in the Act. A defeater
is not required for the conclusion to hold; it is sufficient, on its own, to
destroy it, and it destroys it *even where every other element is made out*.
Collapsing that distinction produces a system that is confidently wrong in
exactly the cases a lawyer cares about.

So: a node's normal children must all be satisfied; its EXCEPTION children must
all be *not* satisfied. A firing exception marks the node ``defeated``, and the
proof tree reports which one fired and why. See docs/04-rule-encoding.md.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Collection, Mapping
from datetime import UTC, datetime
from typing import Any

from ratio.model import (
    Finding,
    NodeKind,
    Predicate,
    ProofTree,
    RuleNode,
    RuleSet,
)

# A credence bridge: given a bn_query id and the fact base, return a posterior
# probability for the target state the rule cares about.
CredenceResolver = Callable[[str, Mapping[str, Any]], float]


class RuleEvaluationError(ValueError):
    pass


class RuleEngine:
    """Evaluates one :class:`RuleSet` against a fact base."""

    def __init__(
        self,
        ruleset: RuleSet,
        *,
        credence: CredenceResolver | None = None,
    ) -> None:
        self.ruleset = ruleset
        self.credence = credence
        self._nodes: dict[str, RuleNode] = {n.id: n for n in ruleset.nodes}
        issues = validate_ruleset(ruleset)
        if issues:
            raise RuleEvaluationError(
                "ruleset failed validation:\n  - " + "\n  - ".join(issues)
            )

    # -- public API --------------------------------------------------------

    def evaluate(self, facts: Mapping[str, Any]) -> ProofTree:
        """Run the ruleset, returning a full derivation."""
        findings: list[Finding] = []
        gaps: list[str] = []
        defeaters: list[str] = []
        outcome, _ = self._eval(
            self.ruleset.root, facts, findings, gaps, defeaters, visiting=()
        )
        conclusion = {
            "satisfied": "applies",
            "defeated": "does_not_apply",
            "not_satisfied": "does_not_apply",
            "unknown": "undetermined",
        }[outcome]
        return ProofTree(
            ruleset_id=self.ruleset.id,
            ruleset_version=self.ruleset.version,
            conclusion=conclusion,
            findings=tuple(findings),
            defeaters_fired=tuple(defeaters),
            gaps=tuple(dict.fromkeys(gaps)),
            generated_at=datetime.now(UTC),
        )

    # -- internals ---------------------------------------------------------

    def _eval(
        self,
        node_id: str,
        facts: Mapping[str, Any],
        findings: list[Finding],
        gaps: list[str],
        defeaters: list[str],
        *,
        visiting: tuple[str, ...],
    ) -> tuple[str, Finding]:
        if node_id in visiting:
            raise RuleEvaluationError(f"cycle detected at node {node_id!r}")
        node = self._nodes.get(node_id)
        if node is None:
            raise RuleEvaluationError(f"unknown node {node_id!r}")

        child_outcomes: list[tuple[str, Finding]] = []
        for child_id in node.children:
            child_outcomes.append(
                self._eval(
                    child_id,
                    facts,
                    findings,
                    gaps,
                    defeaters,
                    visiting=(*visiting, node_id),
                )
            )

        outcome = self._combine(node, child_outcomes, facts, defeaters, gaps)
        finding = Finding(
            node_id=node.id,
            label=node.label,
            outcome=outcome,  # type: ignore[arg-type]
            statement=node.statement,
            predicate=node.predicate,
            observed=self._observed(node, facts),
            children=node.children,
            provenance=node.provenance,
            note=self._note(node, outcome, child_outcomes),
        )
        findings.append(finding)
        if outcome == "defeated":
            # Deliberately *not* recorded here. The node that comes out
            # "defeated" is the rule that was defeated, not the defeater, and
            # ``defeaters_fired`` is read by the coverage report and the audit
            # ledger as a list of *defeaters*. Recording the defeated parent
            # here made every exception look unexercised: the report announced
            # defeaters that had in fact fired, so the one defect the coverage
            # analysis exists to find was the one it could not see. The firing
            # exception is recorded where it fires, in ``_combine``.
            pass
        return outcome, finding

    def _combine(
        self,
        node: RuleNode,
        child_outcomes: list[tuple[str, Finding]],
        facts: Mapping[str, Any],
        defeaters: list[str],
        gaps: list[str],
    ) -> str:
        outcomes = [o for o, _ in child_outcomes]

        if node.kind is NodeKind.LEAF:
            return self._eval_predicate(node.predicate, facts, gaps)

        if node.kind is NodeKind.DELEGATE_BN:
            if self.credence is None:
                raise RuleEvaluationError(
                    f"node {node.id!r} delegates to the credence layer but no "
                    "resolver was supplied"
                )
            assert node.bn_query is not None
            probability = self.credence(node.bn_query, facts)
            threshold = node.bn_threshold if node.bn_threshold is not None else 0.5
            return "satisfied" if probability >= threshold else "not_satisfied"

        # Separate defeaters from ordinary children.
        normal = [
            o
            for (o, f) in child_outcomes
            if self._nodes[f.node_id].kind is not NodeKind.EXCEPTION
        ]
        exceptions = [
            o
            for (o, f) in child_outcomes
            if self._nodes[f.node_id].kind is NodeKind.EXCEPTION
        ]

        if node.kind is NodeKind.ALL_OF:
            base = _conjunction(normal)
        elif node.kind is NodeKind.ANY_OF:
            base = _disjunction(normal)
        elif node.kind is NodeKind.NOT:
            if len(normal) != 1:
                raise RuleEvaluationError(
                    f"NOT node {node.id!r} must have exactly one normal child"
                )
            base = _negate(normal[0])
        elif node.kind is NodeKind.EXCEPTION:
            # An exception node is itself satisfiable; the defeat happens in
            # the parent's _combine. It is either a bare predicate or a subtree.
            if node.predicate is not None:
                base = self._eval_predicate(node.predicate, facts, gaps)
            else:
                base = _conjunction(outcomes)
        else:  # pragma: no cover - enum is exhaustive
            raise RuleEvaluationError(f"unhandled node kind {node.kind!r}")

        # A firing exception defeats the conclusion outright, regardless of how
        # well the elements are made out. This is the defeasibility rule.
        #
        # Each firing exception is named here, by its own id, so that a reader of
        # the proof -- or of the coverage report, or of the ledger -- can see which
        # defeater did it. "The conclusion was defeated" is not an auditable
        # statement; "the conclusion was defeated by exemption_certificate_applies"
        # is, and it is the one an expert reviewing the encoding needs.
        firing = [
            child.node_id
            for outcome, child in child_outcomes
            if outcome == "satisfied" and self._nodes[child.node_id].kind is NodeKind.EXCEPTION
        ]
        if firing:
            defeaters.extend(firing)
            return "defeated"
        if base == "satisfied" and any(o == "unknown" for o in exceptions):
            # A defeater we could not evaluate has not *failed* to fire; it has
            # not been tried. Returning ``base`` here would say the conclusion
            # holds when the one thing that could destroy it is unevaluated --
            # "we do not know whether an exemption certificate covers this, so
            # the acquisition is notifiable". The gap is recorded either way, and
            # the caller that checks ``gaps`` will abstain, but the conclusion
            # travels further than the gap list does: it is what gets rendered,
            # logged and quoted. It must not be the optimistic one.
            #
            # Only an otherwise-satisfied conclusion is downgraded. If the
            # elements are not made out, no defeater can change that, and
            # reporting "undetermined" there would hide a definite answer the
            # facts support.
            return "unknown"
        return base

    def _eval_predicate(
        self,
        predicate: Predicate | None,
        facts: Mapping[str, Any],
        gaps: list[str],
    ) -> str:
        """Evaluate one leaf predicate, recording what was missing.

        Recording the gap is not bookkeeping. ``ProofTree.gaps`` is what the
        rule system abstains on, so a gap that is not recorded here is a
        missing fact that the system will happily reason around -- returning
        "does_not_apply" for a limb nobody supplied because it treated the
        absent value as false. The field name is recorded, not the node id,
        because the field is the thing a user can go and establish.
        """
        if predicate is None:
            return "unknown"
        present, value = _resolve(facts, predicate.field)
        if not present:
            gaps.append(predicate.field)
            return "unknown"
        return "satisfied" if _compare(value, predicate) else "not_satisfied"

    def _observed(self, node: RuleNode, facts: Mapping[str, Any]) -> str | None:
        if node.kind is NodeKind.LEAF and node.predicate is not None:
            present, value = _resolve(facts, node.predicate.field)
            return repr(value) if present else None
        if node.kind is NodeKind.DELEGATE_BN:
            return None
        return None

    def _note(
        self,
        node: RuleNode,
        outcome: str,
        child_outcomes: list[tuple[str, Finding]],
    ) -> str | None:
        if outcome == "defeated":
            fired = [
                f.node_id
                for o, f in child_outcomes
                if o == "satisfied" and self._nodes[f.node_id].kind is NodeKind.EXCEPTION
            ]
            return f"defeated by exception(s): {', '.join(fired)}"
        if outcome == "unknown":
            missing = [
                self._nodes[f.node_id].predicate.field
                for o, f in child_outcomes
                if o == "unknown" and self._nodes[f.node_id].predicate is not None
            ]
            if missing:
                return f"undetermined: missing fact(s) {', '.join(sorted(set(missing)))}"
        return None


# --------------------------------------------------------------------------
# Predicate evaluation
# --------------------------------------------------------------------------


def _resolve(facts: Mapping[str, Any], path: str) -> tuple[bool, Any]:
    """Look up a dotted path in a nested mapping.

    Also tries a flattened spelling, because fact bases arrive from intake
    forms where "acquirer.is_foreign_person" may be the literal key.
    """
    if path in facts:
        return True, facts[path]
    current: Any = facts
    for part in path.split("."):
        if isinstance(current, Mapping) and part in current:
            current = current[part]
        else:
            return False, None
    return True, current


def _compare(value: Any, predicate: Predicate) -> bool:
    op = predicate.op
    target = predicate.value
    try:
        if op == "exists":
            return value is not None
        if op == "eq":
            return _loose_eq(value, target)
        if op == "ne":
            return not _loose_eq(value, target)
        if op == "in":
            return isinstance(target, list) and any(
                _loose_eq(value, t) for t in target
            )
        if op == "not_in":
            return isinstance(target, list) and not any(
                _loose_eq(value, t) for t in target
            )
        if op == "matches":
            return bool(re.search(str(target), str(value)))
        if op in {"lt", "le", "gt", "ge"}:
            left, right = float(value), float(target)  # type: ignore[arg-type]
            return {
                "lt": left < right,
                "le": left <= right,
                "gt": left > right,
                "ge": left >= right,
            }[op]
    except (TypeError, ValueError) as exc:
        raise RuleEvaluationError(
            f"predicate {predicate.field!r} {op} {target!r} could not be applied to "
            f"{value!r}: {exc}"
        ) from exc
    raise RuleEvaluationError(f"unknown operator {op!r}")


def _loose_eq(a: Any, b: Any) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return bool(a) == bool(b)
    if a is None or b is None:
        return a is b
    if isinstance(a, str) and isinstance(b, str):
        return a.strip().casefold() == b.strip().casefold()
    try:
        return float(a) == float(b)
    except (TypeError, ValueError):
        return a == b


def _conjunction(outcomes: list[str]) -> str:
    if not outcomes:
        return "satisfied"  # vacuous truth, and the validator flags empty nodes
    if "not_satisfied" in outcomes or "defeated" in outcomes:
        return "not_satisfied"
    if "unknown" in outcomes:
        return "unknown"
    return "satisfied"


def _disjunction(outcomes: list[str]) -> str:
    if not outcomes:
        return "not_satisfied"
    if "satisfied" in outcomes:
        return "satisfied"
    if "unknown" in outcomes:
        return "unknown"
    return "not_satisfied"


def _negate(outcome: str) -> str:
    return {
        "satisfied": "not_satisfied",
        "not_satisfied": "satisfied",
        "defeated": "satisfied",  # a defeated subtree is not satisfied
        "unknown": "unknown",
    }[outcome]


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


def validate_ruleset(ruleset: RuleSet) -> list[str]:
    """Structural and governance checks. Returns a list of problems.

    Governance checks are in here on purpose. A ruleset that is structurally
    valid but has unprovenanced leaves, or has been edited since approval, is
    not fit to be relied on, and the system should refuse to run it rather than
    produce a confident answer from an ungoverned artefact.
    """
    issues: list[str] = []
    ids = {n.id for n in ruleset.nodes}

    if ruleset.root not in ids:
        issues.append(f"root node {ruleset.root!r} is not defined")
    if len(ids) != len(ruleset.nodes):
        issues.append("duplicate node ids")

    for node in ruleset.nodes:
        for child in node.children:
            if child not in ids:
                issues.append(f"node {node.id!r} references missing child {child!r}")
        if node.kind is NodeKind.ALL_OF and not node.children:
            issues.append(f"all_of node {node.id!r} has no children")
        if node.kind is NodeKind.NOT and len(node.children) != 1:
            issues.append(f"NOT node {node.id!r} must have exactly one child")
        if node.kind is NodeKind.LEAF and not node.provenance:
            issues.append(
                f"leaf node {node.id!r} has no provenance: every factual test must "
                "point to the provision or authority that requires it"
            )
        if node.kind is NodeKind.EXCEPTION and not node.provenance:
            issues.append(f"exception node {node.id!r} has no provenance")

    # Exceptions must be children of exactly one parent, and that parent must
    # know about them (otherwise the exception is silently inert).
    parents: dict[str, list[str]] = {}
    for node in ruleset.nodes:
        for child in node.children:
            parents.setdefault(child, []).append(node.id)
    for node in ruleset.nodes:
        if node.kind is NodeKind.EXCEPTION:
            owners = parents.get(node.id, [])
            if len(owners) != 1:
                issues.append(
                    f"exception {node.id!r} is attached to {len(owners)} parents; "
                    "must be exactly one"
                )
            if node.exception_of and node.exception_of not in owners:
                issues.append(
                    f"exception {node.id!r} declares exception_of={node.exception_of!r} "
                    "but is not a child of that node"
                )

    for node in ruleset.nodes:
        if node.kind is NodeKind.DELEGATE_BN and not node.provenance:
            issues.append(f"delegate_bn node {node.id!r} has no provenance")

    # Reachability: unreachable nodes are usually a modelling mistake.
    reachable: set[str] = set()
    stack = [ruleset.root]
    while stack:
        current = stack.pop()
        if current in reachable or current not in ids:
            continue
        reachable.add(current)
        stack.extend(next(n for n in ruleset.nodes if n.id == current).children)
    for node in ruleset.nodes:
        if node.id not in reachable:
            issues.append(f"node {node.id!r} is unreachable from root")

    if ruleset.status == "approved" and ruleset.current_approval is None:
        issues.append(
            "ruleset is marked approved but no approval matches its current "
            "revision hash: it has been edited since sign-off"
        )
    return issues


def provenance_problems(
    ruleset: RuleSet,
    *,
    judgment_ids: Collection[str],
    provision_keys: Collection[str],
) -> list[str]:
    """Provenance that does not resolve is provenance that does not exist.

    :func:`validate_ruleset` checks that a leaf *has* provenance. It cannot check
    that the provenance *means* anything, because a ruleset does not carry a
    corpus -- and that gap is not academic. The first revision of the shipped
    ruleset cited every provision as
    ``statute:Foreign Acquisitions and Takeovers Act 1975 1975:18``: the corpus
    key with the year duplicated and a space in place of the colon. Structural
    validation passed it, the engine ran, and the entire statutory provenance of
    the only encoded rule terminated in a string that named nothing. That is the
    precise failure the project's invariant exists to prevent, wearing the
    mechanism that is supposed to prevent it.

    So the resolution step lives here, takes the corpus's key sets as arguments
    rather than a knowledge base (this module must stay runnable with no corpus
    loaded), and is called by whoever holds both. Unresolved provenance is
    reported, never repaired: a key that almost matches is still a key nobody
    checked.
    """
    problems: list[str] = []
    for node in ruleset.nodes:
        for ref in node.provenance:
            if ref.kind == "statute":
                if ref.provision_key not in provision_keys:
                    problems.append(
                        f"node {node.id!r} cites provision {ref.provision_key!r}, "
                        "which is not in the corpus"
                    )
            elif ref.kind == "judgment_paragraph":
                if ref.judgment_id not in judgment_ids:
                    problems.append(
                        f"node {node.id!r} cites judgment {ref.judgment_id!r}, "
                        "which is not in the corpus"
                    )
            elif ref.kind == "expert_statement" and not ref.proposition_id:
                problems.append(
                    f"node {node.id!r} cites an expert statement with no "
                    "proposition id: it names no source at all"
                )
    for key in ruleset.provision_keys:
        if key not in provision_keys:
            problems.append(
                f"ruleset declares provision {key!r}, which is not in the corpus"
            )
    return problems


def anchor_problems(
    ruleset: RuleSet,
    *,
    judgments: Mapping[str, Any],
) -> list[str]:
    """Provenance whose anchor does not hold.

    :func:`provenance_problems` asks whether a citation names a document the
    corpus holds. This asks the next question, which is the one that decides
    whether the citation is evidence: *is the node anchored to the words it
    claims?*

    Three ways it is not, and all three were live in this repository at once.

    A node may cite a paragraph the judgment does not contain. ``paragraph: 999``
    of a judgment that ends at [20] resolves to a document and to nothing inside
    it, so the key check passes and the pointer is dead.

    A node may cite a bare ``[42]`` in a judgment whose reasons are numbered
    separately by each judge. ``[42]`` then names as many paragraphs as there
    are authors, and ``AuthorityRef`` has no field in which to say whose.
    Resolving it arbitrarily is how a system ends up quoting a dissent as the
    majority, so it is reported as unresolvable rather than guessed at.

    And a node may quote words the paragraph does not contain. This is the
    narrow one and the dangerous one: it is invisible to every structural check,
    it survives a skim, and it is *nearly* right -- the shipped ruleset quoted a
    sentence of the Full Court with the source's comma replaced by a full stop,
    which reads as a complete sentence and is not one. A quotation that is not
    verbatim is not a quotation; it is a paraphrase wearing a citation, which is
    the failure this project exists to make impossible.

    Matching is the verifier's comparison (typographic folding, whitespace
    collapsing -- :func:`ratio.verify.grounding.normalise_quote`), not a fuzzy
    one. A paraphrase is not a near miss. It is a different thing.
    """
    # Imported here rather than at module scope so that the rule engine stays
    # importable without the verification package.
    from ratio.verify.grounding import normalise_quote

    problems: list[str] = []
    for node in ruleset.nodes:
        for ref in node.provenance:
            if ref.kind != "judgment_paragraph":
                continue
            judgment = judgments.get(ref.judgment_id) if ref.judgment_id else None
            if judgment is None:
                continue  # provenance_problems reports this; do not double-report
            if ref.paragraph is None:
                continue
            ambiguous = judgment.ambiguous_anchor(ref.paragraph)
            if ambiguous:
                problems.append(
                    f"node {node.id!r} cites paragraph [{ref.paragraph}] of "
                    f"{ref.judgment_id!r}, whose reasons are numbered separately by "
                    f"{len(ambiguous)} judges ({', '.join(str(a) for a in ambiguous)}); "
                    "the anchor names more than one paragraph and cannot be resolved"
                )
                continue
            paragraph = judgment.paragraph(ref.paragraph)
            if paragraph is None:
                problems.append(
                    f"node {node.id!r} cites paragraph [{ref.paragraph}] of "
                    f"{ref.judgment_id!r}, which that judgment does not contain"
                )
                continue
            if not ref.quote:
                continue
            if normalise_quote(ref.quote) not in normalise_quote(paragraph.text):
                problems.append(
                    f"node {node.id!r} quotes words that are not in paragraph "
                    f"[{ref.paragraph}] of {ref.judgment_id!r}: "
                    f"{_abbreviate(ref.quote)!r} does not appear in the judgment's "
                    "text for that paragraph"
                )
    return problems


def _abbreviate(text: str, limit: int = 72) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"
