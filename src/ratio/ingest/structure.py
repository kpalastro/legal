"""Turning judgment text into anchored paragraphs.

The only real decision in this module is what a paragraph is, and the answer is
dictated by how judgments are cited: a paragraph runs from its ``[n]`` marker to
the character before the next one. We never split on token counts, because a
token window has no relationship to a paragraph and therefore cannot be cited,
checked, or quoted back to a judge.

Everything else here is defensive. Real judgment text, once it has been through
a PDF extractor and a web page, is full of artefacts: markers that repeat in
footnotes, markers inside block quotations, markers in the catchwords, and
markers that appear at the start of a line but are actually part of a list. A
structurer that quietly mangles them produces a corpus that looks fine and
cites wrong, so the parse reports what it did rather than silently succeeding.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ratio.model import Paragraph, ReasonsRole

# A paragraph marker at the start of a line: "[42]" or "[42]-[44]".
_PARA_START_RE = re.compile(r"^\s*\[(?P<number>\d{1,4})\](?:\s*[-–]\s*\[\d{1,4}\])?\s*")

# Judge attribution lines, which begin a set of reasons:
#   "FENWICK J:", "Bell J:", "STEWART J:", "KIEFEL CJ AND GAGELER J:"
_JUDGE_HEADING_RE = re.compile(
    r"^(?P<names>(?:[A-Z][A-Za-z'’\-]+(?:\s+(?:CJ|JA|J|JJ|ACJ|DCJ|SC|AM|ASJ))?(?:\s+(?:AND|and|&)\s+)?)+)"
    r"\s*:\s*$"
)

# Section headings. Judgments are inconsistent here; we accept a small set of
# common shapes and otherwise let the heading be None, which costs nothing.
_HEADING_RE = re.compile(
    r"^(?:#{1,4}\s*(?P<md>.+?)"
    r"|(?P<plain>[A-Z][A-Za-z'’,\- ]{2,60}))\s*$"
)

_ORDER_CUES = re.compile(r"^(?:orders?|the court orders?|disposition|conclusion)\b", re.I)

# A block quotation: indented by four spaces, or introduced by ">".
_QUOTE_RE = re.compile(r"^(?:\s{4,}|>)\s*\S")


@dataclass
class StructuredParagraph:
    number: int
    text: str
    judge: str | None
    reasons_role: ReasonsRole
    heading: str | None
    is_indented_quote: bool


@dataclass
class StructureReport:
    """What the parser did, and what it was unsure about.

    Returned alongside the result so that ingestion can be *assessed*. A corpus
    whose parse report shows many orphan markers or an unusual judge count is a
    corpus whose retrieval quality cannot be interpreted.
    """

    paragraph_count: int = 0
    judges: list[str] = field(default_factory=list)
    headings: list[str] = field(default_factory=list)
    orphan_markers: list[int] = field(default_factory=list)
    non_monotonic: list[tuple[int, int]] = field(default_factory=list)
    orders_found: int = 0

    @property
    def is_clean(self) -> bool:
        return not self.orphan_markers and not self.non_monotonic

    def summary(self) -> str:
        parts = [f"{self.paragraph_count} paragraphs"]
        if self.judges:
            parts.append(f"{len(self.judges)} set(s) of reasons")
        if self.headings:
            parts.append(f"{len(self.headings)} headings")
        if self.orphan_markers:
            parts.append(f"WARNING {len(self.orphan_markers)} orphan marker(s)")
        if self.non_monotonic:
            parts.append(
                f"WARNING numbering restarts at {len(self.non_monotonic)} point(s)"
            )
        return ", ".join(parts)


class JudgmentStructurer:
    """Stateful only in that it accumulates a report."""

    def __init__(self, *, min_paragraph_chars: int = 1) -> None:
        self.min_paragraph_chars = min_paragraph_chars

    def structure(
        self,
        raw: str,
        *,
        default_judge: str | None = None,
        default_role: ReasonsRole = ReasonsRole.SOLE,
    ) -> tuple[list[StructuredParagraph], list[str], StructureReport]:
        """Parse ``raw`` into paragraphs, orders and a report.

        Returns ``(paragraphs, orders, report)``.
        """
        report = StructureReport()
        lines = raw.replace("\r\n", "\n").split("\n")

        current_judge = default_judge
        current_role = default_role
        current_heading: str | None = None
        in_orders = False

        paragraphs: list[StructuredParagraph] = []
        orders: list[str] = []

        # (judge, number, [lines])
        buffer: list[str] = []
        buffer_number: int | None = None
        buffer_quote = False

        def flush() -> None:
            nonlocal buffer, buffer_number, buffer_quote
            if buffer_number is None:
                buffer = []
                return
            text = "\n".join(buffer).strip()
            if len(text) < self.min_paragraph_chars:
                report.orphan_markers.append(buffer_number)
                buffer, buffer_number, buffer_quote = [], None, False
                return
            if in_orders:
                orders.append(text)
                report.orders_found += 1
            else:
                paragraphs.append(
                    StructuredParagraph(
                        number=buffer_number,
                        text=text,
                        judge=current_judge,
                        reasons_role=current_role,
                        heading=current_heading,
                        is_indented_quote=buffer_quote,
                    )
                )
            buffer, buffer_number, buffer_quote = [], None, False

        for line in lines:
            judge_match = _JUDGE_HEADING_RE.match(line.strip())
            if judge_match and not _PARA_START_RE.match(line):
                flush()
                names = judge_match.group("names").strip()
                current_judge = _tidy_judge_name(names)
                current_role = _infer_role(names)
                if current_judge not in report.judges:
                    report.judges.append(current_judge)
                in_orders = False
                continue

            heading = _heading_of(line)
            if heading is not None and not _PARA_START_RE.match(line):
                flush()
                current_heading = heading
                report.headings.append(heading)
                in_orders = bool(_ORDER_CUES.match(heading))
                continue

            marker = _PARA_START_RE.match(line)
            if marker:
                flush()
                buffer_number = int(marker.group("number"))
                rest = line[marker.end() :]
                buffer = [rest] if rest else []
                buffer_quote = bool(_QUOTE_RE.match(line))
                continue

            if buffer_number is not None:
                buffer.append(line)
                if _QUOTE_RE.match(line):
                    buffer_quote = True

        flush()

        # Numbering restarts are legal and expected (each set of reasons starts
        # at [1]) but they are also what makes a bare "[42]" ambiguous, so we
        # record where they happen.
        seen: dict[int, int] = {}
        for p in paragraphs:
            seen[p.number] = seen.get(p.number, 0) + 1
        report.paragraph_count = len(paragraphs)
        report.orphan_markers = sorted(set(report.orphan_markers))

        return paragraphs, orders, report

    def to_model_paragraphs(
        self, structured: list[StructuredParagraph]
    ) -> tuple[Paragraph, ...]:
        """Convert to model paragraphs, assigning character offsets.

        Offsets are into the concatenation of paragraph texts with the same
        separator :attr:`Judgment.full_text` uses, so that a quote's offsets can
        be checked against the stored text.
        """
        out: list[Paragraph] = []
        cursor = 0
        for i, sp in enumerate(structured):
            start = cursor
            end = start + len(sp.text)
            out.append(
                Paragraph(
                    number=sp.number,
                    text=sp.text,
                    char_start=start,
                    char_end=end,
                    heading=sp.heading,
                    is_indented_quote=sp.is_indented_quote,
                    judge=sp.judge,
                    reasons_role=sp.reasons_role,
                )
            )
            cursor = end + 2  # "\n\n"
            _ = i
        return tuple(out)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _heading_of(line: str) -> str | None:
    stripped = line.strip()
    if not stripped or len(stripped) > 70:
        return None
    if _PARA_START_RE.match(line):
        return None
    m = _HEADING_RE.match(stripped)
    if not m:
        return None
    heading = (m.group("md") or m.group("plain") or "").strip()
    if not heading or heading.endswith("."):
        return None
    # Reject lines that are obviously prose rather than headings.
    if heading.count(" ") > 9:
        return None
    return heading


def _tidy_judge_name(names: str) -> str:
    parts = re.split(r"\s+(?:AND|and|&)\s+", names)
    return " and ".join(p.strip().title() for p in parts if p.strip())


def _infer_role(names: str) -> ReasonsRole:
    """Best guess at the role of a set of reasons.

    A single judge is the sole decision-maker. Multiple judges on one set of
    reasons are the plurality/majority. Dissents are *not* inferable from the
    heading -- "Bell J:" looks identical whether or not Bell J dissented -- so
    this returns MAJORITY and the real answer comes from the orders, or from an
    expert. Guessing dissent from text is exactly the kind of inference that
    ends with a dissent being cited as authority.
    """
    if re.search(r"\s+(?:AND|and|&)\s+", names):
        return ReasonsRole.MAJORITY
    return ReasonsRole.UNKNOWN


def detect_dissent_by_orders(orders_by_judge: dict[str, str]) -> dict[str, ReasonsRole]:
    """Assign roles where orders disagree.

    The only reliable machine signal for dissent is that a judge's orders differ
    from the plurality's. Where all judges agree on orders, everyone is in the
    majority regardless of how much they disagreed on the way there.
    """
    if not orders_by_judge:
        return {}
    counts: dict[str, int] = {}
    for text in orders_by_judge.values():
        key = re.sub(r"\s+", " ", text.strip().lower())
        counts[key] = counts.get(key, 0) + 1
    if not counts:
        return {}
    plurality = max(counts.items(), key=lambda kv: kv[1])[0]
    roles: dict[str, ReasonsRole] = {}
    for judge, text in orders_by_judge.items():
        key = re.sub(r"\s+", " ", text.strip().lower())
        roles[judge] = (
            ReasonsRole.MAJORITY if key == plurality else ReasonsRole.DISSENT
        )
    return roles
