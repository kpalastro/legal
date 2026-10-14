"""Fixture corpus loading.

This is the seam that a real deployment replaces. The rest of the system only
ever sees :class:`ratio.model.Judgment` and :class:`ratio.model.StatutoryProvision`
objects, so swapping the synthetic fixtures for licensed real sources means
writing one more loader, not touching the KB, retrieval, rules or verification
layers. See docs/02-judgment-knowledge-base.md for what a real ingest involves
and why the obvious free sources cannot lawfully be used for it.

The loader refuses to build a corpus that mixes synthetic and real authorities
unless explicitly told to, because the failure mode -- a fixture silently
retrieved alongside real law -- is one that produces fluent, confident, wrong
output rather than an error.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import yaml

from ratio.bn.network import CredenceEngine
from ratio.ingest.structure import JudgmentStructurer, StructureReport
from ratio.model import (
    Judgment,
    Jurisdiction,
    ReasonsRole,
    RuleSet,
    SourceKind,
    SourceRef,
    StatutoryProvision,
)
from ratio.rules.engine import validate_ruleset


class CorpusError(RuntimeError):
    pass


@dataclass
class CorpusBundle:
    judgments: list[Judgment] = field(default_factory=list)
    provisions: list[StatutoryProvision] = field(default_factory=list)
    rulesets: list[RuleSet] = field(default_factory=list)
    credence_engines: list[CredenceEngine] = field(default_factory=list)
    parse_reports: dict[str, StructureReport] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)

    @property
    def has_synthetic(self) -> bool:
        return any(j.synthetic for j in self.judgments)

    @property
    def has_real(self) -> bool:
        return any(not j.synthetic for j in self.judgments)

    def summary(self) -> str:
        lines = [
            f"judgments:   {len(self.judgments)}",
            f"provisions:  {len(self.provisions)}",
            f"rulesets:    {len(self.rulesets)}",
            f"networks:    {len(self.credence_engines)}",
        ]
        dirty = [k for k, r in self.parse_reports.items() if not r.is_clean]
        if dirty:
            lines.append(f"parse warnings: {len(dirty)} document(s): {', '.join(dirty)}")
        if self.problems:
            lines.append(f"problems: {len(self.problems)}")
            lines.extend(f"  - {p}" for p in self.problems)
        return "\n".join(lines)


# --------------------------------------------------------------------------
# Judgments
# --------------------------------------------------------------------------


def parse_judgment_document(doc: dict, structurer: JudgmentStructurer) -> tuple[
    Judgment, StructureReport
]:
    """Build a :class:`Judgment` from one fixture document.

    Each set of reasons is structured independently, which is the point: the
    High Court fixture has two judges whose paragraph numbering overlaps, so a
    bare "[1]" in that judgment is ambiguous and the model must say so.
    """
    paragraphs = []
    orders: list[str] = []
    combined_report = StructureReport()

    reasons_docs = doc.get("reasons") or []
    for reason in reasons_docs:
        judge = reason.get("judge")
        role = ReasonsRole(reason.get("role", "unknown"))
        structured, reason_orders, report = structurer.structure(
            reason.get("text", ""),
            default_judge=judge,
            default_role=role,
        )
        # Override the structurer's inference with the declared role, which for
        # fixtures is authoritative and in production comes from an expert or
        # from comparing the orders.
        for sp in structured:
            sp.reasons_role = role
            sp.judge = judge
        paragraphs.extend(structurer.to_model_paragraphs(structured))
        orders.extend(reason_orders)

        combined_report.paragraph_count += report.paragraph_count
        combined_report.headings.extend(report.headings)
        combined_report.orphan_markers.extend(report.orphan_markers)
        if judge and judge not in combined_report.judges:
            combined_report.judges.append(judge)

    orders.extend(doc.get("orders") or [])

    # Recompute offsets across the concatenated reasons, so that a quote's
    # offsets resolve against Judgment.full_text.
    paragraphs = _reindex_offsets(paragraphs)

    judgment = Judgment(
        id=doc["id"],
        neutral_citation=doc["neutral_citation"],
        title=doc["title"],
        court=doc["court"],
        jurisdiction=Jurisdiction(doc["jurisdiction"]),
        date_decided=_as_date(doc["date_decided"]),
        judges=tuple(combined_report.judges) or tuple(doc.get("judges") or ()),
        catchwords=tuple(doc.get("catchwords") or ()),
        headnote=doc.get("headnote"),
        paragraphs=tuple(paragraphs),
        orders=tuple(orders),
        source=SourceRef(
            kind=SourceKind(doc.get("source", {}).get("kind", "synthetic")),
            url=doc.get("source", {}).get("url"),
            licence=doc.get("source", {}).get("licence"),
        ),
        synthetic=bool(doc.get("synthetic", False)),
    )
    return judgment, combined_report


def _reindex_offsets(paragraphs):
    """Assign char offsets consistent with Judgment.full_text's joining."""
    from ratio.model import Paragraph

    out = []
    cursor = 0
    for p in paragraphs:
        end = cursor + len(p.text)
        out.append(
            Paragraph(
                number=p.number,
                text=p.text,
                char_start=cursor,
                char_end=end,
                heading=p.heading,
                is_indented_quote=p.is_indented_quote,
                judge=p.judge,
                reasons_role=p.reasons_role,
            )
        )
        cursor = end + 2  # "\n\n"
    return out


# --------------------------------------------------------------------------
# Provisions, rulesets, networks
# --------------------------------------------------------------------------


def parse_provision(doc: dict) -> StatutoryProvision:
    return StatutoryProvision(
        act_title=doc["act_title"],
        act_year=int(doc["act_year"]),
        jurisdiction=Jurisdiction(doc["jurisdiction"]),
        section=str(doc["section"]),
        heading=doc.get("heading"),
        text=doc["text"],
        compiled_as_at=_as_date(doc["compiled_as_at"]),
        source=SourceRef(
            kind=SourceKind(doc.get("source", {}).get("kind", "synthetic")),
            licence=doc.get("source", {}).get("licence"),
        ),
    )


def load_ruleset(path: Path) -> RuleSet:
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    return RuleSet.model_validate(doc)


def load_credence_network(path: Path) -> CredenceEngine:
    from ratio.model import CredenceNetwork

    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    doc = _repair_yaml_booleans(doc)
    return CredenceEngine(CredenceNetwork.model_validate(doc))


def _repair_yaml_booleans(doc: dict) -> dict:
    """Undo YAML's treatment of bare ``yes``/``no``/``on``/``off`` as booleans.

    A legal expert authoring a network will write state names as words. In YAML
    1.1, the unquoted word ``no`` parses as the boolean False, so a factor whose
    states are yes/no silently becomes a factor whose states are
    True/False -- and the failure surfaces as a type error about booleans, which
    tells the expert nothing about what they did wrong.

    Rather than requiring the expert to know this, we map the booleans back to
    the words they were almost certainly meant to be. This is safe because a
    boolean in a state name or evidence value is never what the author intended:
    the only thing it can correctly mean is the word.
    """
    words = {True: "yes", False: "no"}

    for elicitation in doc.get("elicitations") or []:
        if isinstance(elicitation.get("distribution"), dict):
            elicitation["distribution"] = {
                words.get(k, k): v for k, v in elicitation["distribution"].items()
            }
        config = elicitation.get("parent_config")
        if isinstance(config, list):
            elicitation["parent_config"] = [words.get(v, v) for v in config]

    for node in doc.get("nodes") or []:
        if isinstance(node.get("parents"), list):
            node["parents"] = [words.get(v, v) for v in node["parents"]]
        for state in node.get("states") or []:
            if "name" in state:
                state["name"] = words.get(state["name"], state["name"])
    return doc


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def load_corpus(
    fixtures_dir: Path,
    *,
    rulesets_dir: Path | None = None,
    allow_mixed: bool = False,
    strict: bool = True,
) -> CorpusBundle:
    """Load the fixture corpus, its rulesets and its credence networks."""
    bundle = CorpusBundle()
    structurer = JudgmentStructurer()

    judgments_path = fixtures_dir / "judgments.yaml"
    if not judgments_path.exists():
        raise CorpusError(f"no judgments.yaml in {fixtures_dir}")

    doc = yaml.safe_load(judgments_path.read_text(encoding="utf-8"))

    for jdoc in doc.get("judgments") or []:
        judgment, report = parse_judgment_document(jdoc, structurer)
        bundle.judgments.append(judgment)
        bundle.parse_reports[judgment.id] = report

    for pdoc in doc.get("provisions") or []:
        bundle.provisions.append(parse_provision(pdoc))

    if bundle.has_synthetic and bundle.has_real and not allow_mixed:
        raise CorpusError(
            "refusing to mix synthetic fixtures with real authorities. Pass "
            "allow_mixed=True only for test harnesses, never for a corpus that "
            "will answer questions."
        )

    rulesets_dir = rulesets_dir or Path(__file__).resolve().parents[3] / "rulesets"
    if rulesets_dir.exists():
        for path in sorted(rulesets_dir.glob("*.ruleset.yaml")):
            ruleset = load_ruleset(path)
            issues = validate_ruleset(ruleset)
            if issues:
                if strict:
                    raise CorpusError(
                        f"{path.name} failed validation:\n  - " + "\n  - ".join(issues)
                    )
                bundle.problems.extend(f"{path.name}: {i}" for i in issues)
            bundle.rulesets.append(ruleset)
        for path in sorted(rulesets_dir.glob("*.credence.yaml")):
            try:
                bundle.credence_engines.append(load_credence_network(path))
            except Exception as exc:  # noqa: BLE001 - surfaced in the report
                if strict:
                    raise CorpusError(f"{path.name} could not be loaded: {exc}") from exc
                bundle.problems.append(f"{path.name}: {exc}")

    return bundle


def _as_date(value: object) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))
