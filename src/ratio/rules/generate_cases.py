"""Systematic case generation, and what it proves about a ruleset.

An expert reviewing an encoded rule by reading it will miss things. Everybody
does. The reviewer sees the structure, agrees with the structure, and stops --
which is why formalisation projects ship rules that behave differently from what
the reviewer believed they had approved.

The technique with the strongest track record for catching this is not reading
the rule, it is **generating cases from it and making the expert adjudicate the
outcomes**. Catala's differential test against OpenFisca found a live production
defect that way; concolic test generation over a benefits codebase produced
186,390 cases and found a rounding inconsistency between two backends. The
principle is the same at any scale: do not ask the expert whether the rule is
right, ask them whether *this case* came out right, a few hundred times.

This module generates the cases. Each leaf is given a small set of
discriminating values rather than an exhaustive domain, so the cross product
stays inside a reviewable size, and the resulting sheet is an incidence matrix:
one row per fact combination, one column per leaf, plus the conclusion the
engine reached and which defeater fired.

Two properties of the sheet do real work, and both are checks nobody performs by
reading:

* **Inert leaves.** A leaf whose value never changes the conclusion is either
  redundant or mis-wired. Either way it is a defect in the encoding, and it is
  invisible in a rendering of the rule tree.
* **Defeater coverage.** If no generated case fires a given exception, the
  exception has never been exercised, and its provenance has never been tested
  against anything.

Everything here is deterministic. It is the machinery an SME review queue runs
on, not a model.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Any

from ratio.model import NodeKind, Predicate, RuleNode, RuleSet
from ratio.rules.engine import CredenceResolver, RuleEngine


@dataclass(frozen=True)
class GeneratedCase:
    case_id: str
    facts: dict[str, Any]
    conclusion: str
    defeaters_fired: tuple[str, ...]
    gaps: tuple[str, ...]

    def row_label(self) -> str:
        return " | ".join(
            f"{k.split('.')[-1]}={_short(v)}" for k, v in sorted(self.facts.items())
        )


@dataclass
class CoverageReport:
    ruleset_id: str
    ruleset_version: str
    total_cases: int
    conclusion_counts: dict[str, int] = field(default_factory=dict)
    # Leaves whose value never changed the conclusion across all generated cases.
    inert_leaves: list[str] = field(default_factory=list)
    # Leaves that were never determinable, because no generated case supplied them.
    never_determined: list[str] = field(default_factory=list)
    # Exceptions that never fired in any generated case.
    unexercised_defeaters: list[str] = field(default_factory=list)
    unknown_fields: list[str] = field(default_factory=list)

    @property
    def is_healthy(self) -> bool:
        # A conclusion that never occurs is not itself a defect -- a well-formed
        # ruleset may be satisfiable in only one way. Inert leaves and
        # unexercised defeaters are, because they mean part of the encoding has
        # never been checked.
        return not self.inert_leaves and not self.unexercised_defeaters

    def summary(self) -> str:
        lines = [
            f"{self.total_cases} generated cases over ruleset "
            f"{self.ruleset_id} v{self.ruleset_version}",
            "conclusions: "
            + ", ".join(f"{k}={v}" for k, v in sorted(self.conclusion_counts.items())),
        ]
        if self.inert_leaves:
            lines.append(
                "INERT LEAVES (value never changed the outcome -- redundant or "
                "mis-wired): " + ", ".join(self.inert_leaves)
            )
        if self.unexercised_defeaters:
            lines.append(
                "UNEXERCISED DEFEATERS (never fired in any generated case): "
                + ", ".join(self.unexercised_defeaters)
            )
        if self.never_determined:
            lines.append("leaf values not generated: " + ", ".join(self.never_determined))
        if self.unknown_fields:
            lines.append(
                "fields left unset in some cases: " + ", ".join(self.unknown_fields)
            )
        return "\n".join(lines)


class CaseGenerator:
    """Enumerates fact combinations over a ruleset's leaves and evaluates them."""

    def __init__(self, engine: RuleEngine, *, max_cases: int = 4096) -> None:
        self.engine = engine
        self.ruleset: RuleSet = engine.ruleset
        self.max_cases = max_cases

    # -- value domains -----------------------------------------------------

    @staticmethod
    def value_domain(predicate: Predicate) -> list[Any]:
        """Discriminating values for one predicate.

        Boundary values for ordered comparisons, because that is where encoding
        errors live: an off-by-one in "at or above 20 per cent" is invisible at 5
        and at 40 and decisive at exactly 20.
        """
        op, value = predicate.op, predicate.value
        if op in {"eq", "ne"}:
            if isinstance(value, bool):
                return [True, False]
            if isinstance(value, (int, float)):
                return _numeric_boundary(float(value))
            return [value, "__not_this__"]
        if op in {"lt", "le", "gt", "ge"}:
            assert isinstance(value, (int, float))
            return sorted(_numeric_boundary(float(value)))
        if op == "in":
            items = list(value) if isinstance(value, list) else []
            return items[:3] + ["__not_in__"] if items else ["__not_in__"]
        if op == "not_in":
            items = list(value) if isinstance(value, list) else []
            return (items[:1] or ["__x__"]) + ["__not_in__"]
        if op == "exists":
            return [True, None]
        if op == "matches":
            return [str(value), "__no_match__"]
        return [value]

    # -- generation --------------------------------------------------------

    def _generatable_leaves(self) -> list[RuleNode]:
        """Every node carrying a fact predicate: leaves *and* exceptions.

        Exceptions have to be swept, and leaving them out is not a small omission.
        An exception whose predicate no generated case ever sets to its firing
        value never fires, and the coverage report then announces it as an
        unexercised defeater -- which is true, but the cause is the generator, not
        the ruleset. The reviewer is handed a defect they cannot act on, and the
        real question the sheet is meant to answer ("does an exemption certificate
        actually defeat notifiability in this encoding?") goes unasked.

        Ordering is by declaration in the ruleset, not by id, so that the column
        order of the review sheet matches the order the expert wrote the nodes in.
        """
        return [
            n
            for n in self.ruleset.nodes
            if n.kind in {NodeKind.LEAF, NodeKind.EXCEPTION} and n.predicate
        ]

    def generate(self) -> tuple[list[GeneratedCase], CoverageReport]:
        leaves = self._generatable_leaves()
        if not leaves:
            raise ValueError("ruleset has no leaf nodes to generate cases over")

        domains: list[list[Any]] = []
        for leaf in leaves:
            assert leaf.predicate is not None
            domains.append(self.value_domain(leaf.predicate))

        combinations = 1
        for domain in domains:
            combinations *= len(domain)

        report = CoverageReport(
            ruleset_id=self.ruleset.id,
            ruleset_version=self.ruleset.version,
            total_cases=0,
        )

        if combinations > self.max_cases:
            # Truncate the cross product rather than silently sampling randomly.
            # Random sampling would make the run irreproducible, and a review
            # sheet that differs between runs cannot be signed off on.
            report.unknown_fields.append(
                f"cross product of {combinations} exceeds max_cases="
                f"{self.max_cases}; truncated deterministically"
            )

        cases: list[GeneratedCase] = []
        for index, combo in enumerate(itertools.product(*domains)):
            if index >= self.max_cases:
                break
            facts: dict[str, Any] = {}
            for leaf, value in zip(leaves, combo, strict=True):
                assert leaf.predicate is not None
                if value == "__not_this__":
                    continue  # leave the field absent: exercises the unknown path
                facts[leaf.predicate.field] = value

            tree = self.engine.evaluate(facts)
            cases.append(
                GeneratedCase(
                    case_id=f"case-{index:05d}",
                    facts=facts,
                    conclusion=tree.conclusion,
                    defeaters_fired=tree.defeaters_fired,
                    gaps=tree.gaps,
                )
            )
            report.conclusion_counts[tree.conclusion] = (
                report.conclusion_counts.get(tree.conclusion, 0) + 1
            )

        report.total_cases = len(cases)
        self._analyse(leaves, cases, report)
        return cases, report

    def _analyse(
        self,
        leaves: list[RuleNode],
        cases: list[GeneratedCase],
        report: CoverageReport,
    ) -> None:
        for leaf in leaves:
            assert leaf.predicate is not None
            field_name = leaf.predicate.field
            outcomes_by_value: dict[str, set[str]] = {}
            seen_values = False
            for case in cases:
                if field_name not in case.facts:
                    continue
                seen_values = True
                key = repr(case.facts[field_name])
                outcomes_by_value.setdefault(key, set()).add(case.conclusion)
            if not seen_values:
                report.never_determined.append(field_name)
                continue
            # The leaf is inert if every value it took produced the same single
            # conclusion, and there was more than one value to try.
            distinct_outcomes = {o for s in outcomes_by_value.values() for o in s}
            if len(outcomes_by_value) > 1 and len(distinct_outcomes) == 1:
                report.inert_leaves.append(field_name)

        fired = {d for case in cases for d in case.defeaters_fired}
        for node in self.ruleset.nodes:
            if node.kind is NodeKind.EXCEPTION and node.id not in fired:
                report.unexercised_defeaters.append(node.id)

        gaps = {g for case in cases for g in case.gaps}
        report.unknown_fields.extend(sorted(gaps))

    # -- rendering ---------------------------------------------------------

    def render_review_sheet(
        self, cases: list[GeneratedCase], *, limit: int = 40
    ) -> str:
        """A markdown sheet an expert can annotate.

        The last column is deliberately empty: the artefact this produces is a
        document for a lawyer to disagree with, not a report of what the system
        concluded.
        """
        if not cases:
            return "(no cases generated)"
        fields = sorted({f for case in cases for f in case.facts})
        header = ["case", *fields, "engine conclusion", "defeaters", "SME agrees?"]
        lines = [
            "| " + " | ".join(header) + " |",
            "|" + "|".join("---" for _ in header) + "|",
        ]
        for case in cases[:limit]:
            cells = [case.case_id]
            for f in fields:
                cells.append(_short(case.facts.get(f, "-- absent --")))
            cells.append(case.conclusion)
            cells.append(", ".join(case.defeaters_fired) or "-")
            cells.append("")
            lines.append("| " + " | ".join(cells) + " |")
        if len(cases) > limit:
            lines.append(f"_(showing {limit} of {len(cases)} cases)_")
        return "\n".join(lines)


def _numeric_boundary(value: float) -> list[float]:
    """Values straddling a threshold, including the threshold itself."""
    step = 1.0 if value == int(value) else max(abs(value) * 0.1, 0.01)
    return [value - step, value, value + step]


def _short(value: Any) -> str:
    text = repr(value)
    return text if len(text) <= 18 else text[:15] + "..."


def generate_and_analyse(
    ruleset: RuleSet,
    *,
    credence: CredenceResolver | None = None,
    max_cases: int = 4096,
) -> tuple[list[GeneratedCase], CoverageReport]:
    engine = RuleEngine(ruleset, credence=credence)
    return CaseGenerator(engine, max_cases=max_cases).generate()
