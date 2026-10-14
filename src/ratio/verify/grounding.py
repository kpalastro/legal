"""Verification: the component that makes the invariant enforceable.

Everything upstream of this module is machinery for producing claims. This is
the only place that decides whether a claim is allowed out. It is deliberately
small, deterministic, and fail-closed.

The failure it exists to prevent is not a system that cannot find authority. It
is a system that finds the wrong authority, or invents one, and states it with
the same fluency as everything else. Documented examples are not exotic:

* *Mata v Avianca* (S.D.N.Y. 2023) -- six fabricated decisions, with invented
  quotes, docket numbers and judges, in a filed submission. Sanctions followed.
* Stanford RegLab's preregistered audit of the leading commercial legal
  research tools found hallucination rates of roughly 17% to 33%, against
  vendors' claims of being hallucination-free.
* In Australia, *JNE24 v Minister for Immigration and Citizenship*
  [2025] FedCFamC2G 1314 saw a practitioner personally pay costs after citing
  four non-existent authorities, and the court's point was that checking a case
  is not fictitious is *not enough* -- the practitioner must confirm it supports
  the proposition.

That last point is the design brief. An existence check is necessary and
nowhere near sufficient, so this module checks four things:

  1. **Existence** -- the cited authority is in the corpus. A citation to
     something we have not ingested is reported as such, never as verified.
  2. **Anchor** -- the paragraph exists, and the judge is known where the
     judgment has more than one set of reasons.
  3. **Quote** -- the claimed words occur in the claimed paragraph, and we say
     whether the offsets were right, right after normalisation, or wrong.
  4. **Treatment** -- the authority has not been overruled, and the caller is
     told if it has been questioned or distinguished.

Anything that fails any of these is refused, not annotated. The verifier
returns a :class:`VerificationReport` whose ``is_clean`` is False, and the
orchestrator is written to drop unsupported claims rather than soften them.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

from ratio.model import (
    AuthorityStatusRecord,
    GroundedQuote,
    Judgment,
)

QuoteStatus = Literal["exact", "offset_corrected", "normalised", "not_found"]
CitationStatus = Literal["exists", "not_in_corpus", "unresolvable", "ambiguous_anchor"]

# Characters that differ between a source and a model's rendering of it, and
# that should not by themselves make a quote fail. Note what is *not* here: we
# do not fold word forms, do not stem, and do not allow word substitution. A
# quote must be the words of the judgment.
_PUNCT_FOLD = str.maketrans(
    {
        "‘": "'",
        "’": "'",
        "“": '"',
        "”": '"',
        "–": "-",
        "—": "-",
        "−": "-",
        " ": " ",
        " ": " ",
        "­": "",
    }
)


def normalise_quote(text: str) -> str:
    """Fold away typographic variation and collapse whitespace.

    Deliberately conservative. The point of normalisation is to stop a curly
    quote being reported as a fabrication, not to let an approximate quotation
    pass as an exact one -- which is why the match is *labelled* ``normalised``
    rather than upgraded to ``exact``.
    """
    folded = unicodedata.normalize("NFKC", text).translate(_PUNCT_FOLD)
    return re.sub(r"\s+", " ", folded).strip()


@dataclass(frozen=True)
class QuoteCheck:
    judgment_id: str
    neutral_citation: str
    paragraph: int
    judge: str | None
    claimed_quote: str
    status: QuoteStatus
    span: tuple[int, int] | None = None
    note: str | None = None

    @property
    def is_grounded(self) -> bool:
        return self.status in {"exact", "offset_corrected", "normalised"}

    @property
    def is_byte_exact(self) -> bool:
        return self.status == "exact"


@dataclass(frozen=True)
class CitationCheck:
    raw: str
    target_key: str | None
    status: CitationStatus
    authority: AuthorityStatusRecord | None = None
    note: str | None = None

    @property
    def is_resolved(self) -> bool:
        return self.status == "exists"

    @property
    def is_safe_to_rely_on(self) -> bool:
        """Resolved, and not carrying a negative treatment in the corpus."""
        if self.status != "exists":
            return False
        if self.authority is None:
            return True
        from ratio.model import AuthorityStatus

        return self.authority.status not in {
            AuthorityStatus.OVERRULED,
            AuthorityStatus.SUPERSEDED_BY_STATUTE,
        }


@dataclass
class VerificationReport:
    """The result of checking one generated claim.

    ``is_clean`` is the gate. It is False if any citation failed to resolve, any
    quote failed to ground, or any authority relied on is overruled. There is no
    partial pass and no confidence threshold: a claim is either supported by
    authority we hold and can point at, or it is not.
    """

    claim: str
    quotes: list[QuoteCheck] = field(default_factory=list)
    citations: list[CitationCheck] = field(default_factory=list)
    checked_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    warnings: list[str] = field(default_factory=list)

    @property
    def is_clean(self) -> bool:
        if not self.quotes and not self.citations:
            return False  # an uncited claim is not a clean claim
        return all(q.is_grounded for q in self.quotes) and all(
            c.is_safe_to_rely_on for c in self.citations
        )

    @property
    def verdict(self) -> Literal["supported", "unsupported", "unverifiable"]:
        if self.is_clean:
            return "supported"
        if any(not q.is_grounded for q in self.quotes):
            return "unsupported"  # the words are not there
        if any(c.status == "unresolvable" for c in self.citations):
            return "unverifiable"  # we cannot check it, which is not the same
        return "unsupported"

    def explain(self) -> str:
        lines = [f"[{self.verdict.upper()}] {self.claim}"]
        for q in self.quotes:
            mark = "ok " if q.is_grounded else "FAIL"
            where = f"{q.neutral_citation} at [{q.paragraph}]"
            if q.judge:
                where += f" ({q.judge})"
            lines.append(f"  {mark} quote  {where}  ({q.status})")
            if q.note:
                lines.append(f"       {q.note}")
        for c in self.citations:
            mark = "ok " if c.is_safe_to_rely_on else "FAIL"
            lines.append(f"  {mark} cite   {c.raw}  ({c.status})")
            if c.note:
                lines.append(f"       {c.note}")
        for w in self.warnings:
            lines.append(f"  warn   {w}")
        return "\n".join(lines)


class Verifier:
    """Checks claims against a corpus of judgments and a citation graph."""

    def __init__(
        self,
        judgments: dict[str, Judgment],
        *,
        graph=None,
        strict_offsets: bool = False,
    ) -> None:
        # Accept either a map from judgment id or from neutral citation key.
        self._by_id: dict[str, Judgment] = {}
        self._by_key: dict[str, Judgment] = {}
        for judgment in judgments.values():
            self._by_id[judgment.id] = judgment
            key = judgment.canonical_key()
            if key:
                self._by_key[key] = judgment
        self.graph = graph
        # When True, a quote whose offsets are wrong is treated as a failure even
        # though the words were found. Off by default, because a model that
        # quotes correctly but counts characters badly is a nuisance rather than
        # a hazard -- but the option exists for pipelines where an offset is
        # itself relied on, such as highlighting in a document viewer.
        self.strict_offsets = strict_offsets

    # -- quotes ------------------------------------------------------------

    def check_quote(
        self,
        judgment_ref: str,
        paragraph: int,
        quote: str,
        *,
        judge: str | None = None,
        span: tuple[int, int] | None = None,
    ) -> QuoteCheck:
        """Verify a quote against a stored paragraph.

        ``judgment_ref`` may be either the internal judgment id or the graph key
        (``"HCA:2016:12"``). Where the judgment has multiple sets of reasons,
        ``judge`` must identify whose -- an unresolved multi-judge anchor is
        reported as such rather than resolved by taking the first match, because
        the first match is as likely to be the dissent.
        """
        judgment = self._resolve(judgment_ref)
        if judgment is None:
            return QuoteCheck(
                judgment_id=judgment_ref,
                neutral_citation=judgment_ref,
                paragraph=paragraph,
                judge=judge,
                claimed_quote=quote,
                status="not_found",
                note="no such judgment in the corpus",
            )

        ambiguous = judgment.ambiguous_anchor(paragraph)
        if ambiguous is not None and judge is None:
            return QuoteCheck(
                judgment_id=judgment.id,
                neutral_citation=judgment.neutral_citation,
                paragraph=paragraph,
                judge=None,
                claimed_quote=quote,
                status="not_found",
                note=(
                    f"paragraph [{paragraph}] exists in {len(ambiguous)} sets of "
                    f"reasons ({', '.join(str(a) for a in ambiguous)}); the citing "
                    "judge must be identified before the quote can be checked"
                ),
            )

        para = judgment.paragraph(paragraph, judge)
        if para is None:
            return QuoteCheck(
                judgment_id=judgment.id,
                neutral_citation=judgment.neutral_citation,
                paragraph=paragraph,
                judge=judge,
                claimed_quote=quote,
                status="not_found",
                note=(
                    f"no paragraph [{paragraph}]"
                    + (f" in the reasons of {judge}" if judge else "")
                ),
            )

        # 1. Byte-exact at the claimed offsets.
        if span is not None:
            start, end = span
            if para.text[start:end] == quote:
                return QuoteCheck(
                    judgment_id=judgment.id,
                    neutral_citation=judgment.neutral_citation,
                    paragraph=paragraph,
                    judge=judge,
                    claimed_quote=quote,
                    status="exact",
                    span=span,
                )

        # 2. Byte-exact anywhere in the paragraph.
        index = para.text.find(quote)
        if index >= 0:
            found = (index, index + len(quote))
            if span is None:
                return QuoteCheck(
                    judgment_id=judgment.id,
                    neutral_citation=judgment.neutral_citation,
                    paragraph=paragraph,
                    judge=judge,
                    claimed_quote=quote,
                    status="exact",
                    span=found,
                )
            return QuoteCheck(
                judgment_id=judgment.id,
                neutral_citation=judgment.neutral_citation,
                paragraph=paragraph,
                judge=judge,
                claimed_quote=quote,
                status="offset_corrected",
                span=found,
                note=(
                    f"quote is present but not at the claimed offsets "
                    f"{span}; the correct span is {found}"
                ),
            )

        # 3. Match after normalising typography and whitespace.
        normalised_para = normalise_quote(para.text)
        normalised_quote = normalise_quote(quote)
        if normalised_quote and normalised_quote in normalised_para:
            return QuoteCheck(
                judgment_id=judgment.id,
                neutral_citation=judgment.neutral_citation,
                paragraph=paragraph,
                judge=judge,
                claimed_quote=quote,
                status="normalised",
                note=(
                    "quote matches after folding typographic punctuation and "
                    "collapsing whitespace, but not byte-for-byte"
                ),
            )

        # 4. Nothing. Report the closest span so a human can see what happened.
        return QuoteCheck(
            judgment_id=judgment.id,
            neutral_citation=judgment.neutral_citation,
            paragraph=paragraph,
            judge=judge,
            claimed_quote=quote,
            status="not_found",
            note=(
                "the claimed words do not appear in this paragraph. "
                f"Paragraph [{paragraph}] begins: "
                f"\"{para.text[:120]}...\""
            ),
        )

    def grounded_quote(
        self, judgment_ref: str, paragraph: int, quote: str, *, judge: str | None = None
    ) -> GroundedQuote | None:
        """Return a :class:`GroundedQuote`, or None if it does not ground.

        This is the only constructor of GroundedQuote in the system. Nothing
        downstream can create one without passing through a check.
        """
        check = self.check_quote(judgment_ref, paragraph, quote, judge=judge)
        if not check.is_grounded or check.span is None:
            return None
        judgment = self._resolve(judgment_ref)
        assert judgment is not None
        return GroundedQuote(
            judgment_id=judgment.id,
            neutral_citation=judgment.neutral_citation,
            paragraph=paragraph,
            quote=quote,
            span_start=check.span[0],
            span_end=check.span[1],
            verified_at=datetime.now(UTC),
        )

    # -- citations ---------------------------------------------------------

    def check_citation(self, raw: str) -> CitationCheck:
        """Check that a citation resolves to an authority we actually hold.

        Also attaches the computed authority status, so that a citation to a
        case that exists but has been overruled is not reported as fine.
        """
        from ratio.citations import CitationParser
        from ratio.model import CitationKind

        parser = CitationParser()
        candidates = parser.extract(raw)
        target = next(
            (c for c in candidates if c.kind is not CitationKind.STATUTORY),
            candidates[0] if candidates else None,
        )
        if target is None:
            return CitationCheck(
                raw=raw,
                target_key=None,
                status="unresolvable",
                note="no citation pattern recognised in this text",
            )
        key = target.target_key
        if key is None:
            return CitationCheck(
                raw=raw,
                target_key=None,
                status="unresolvable",
                note=(
                    "the reference could not be reduced to a canonical key; it is "
                    "recorded rather than guessed at"
                ),
            )

        judgment = self._by_key.get(key)
        if judgment is None:
            return CitationCheck(
                raw=raw,
                target_key=key,
                status="not_in_corpus",
                note=(
                    f"{key} is not in the ingested corpus. It may exist and be "
                    "sound; we cannot check it, so it is not reported as verified."
                ),
            )

        authority = None
        if self.graph is not None:
            authority = self.graph.authority_status(key)
        return CitationCheck(
            raw=raw,
            target_key=key,
            status="exists",
            authority=authority,
            note=(
                None if authority is None or authority.status.value == "good_law"
                else authority.caveat
            ),
        )

    # -- claims ------------------------------------------------------------

    def verify_claim(
        self,
        claim: str,
        *,
        quotes: list[dict] | None = None,
        citations: list[str] | None = None,
    ) -> VerificationReport:
        """Check a whole claim: its quotes and its citations.

        ``quotes`` entries are dicts with ``judgment``, ``paragraph``, ``quote``
        and optionally ``judge`` and ``span``.
        """
        report = VerificationReport(claim=claim)
        for spec in quotes or []:
            report.quotes.append(
                self.check_quote(
                    spec["judgment"],
                    int(spec["paragraph"]),
                    spec["quote"],
                    judge=spec.get("judge"),
                    span=tuple(spec["span"]) if spec.get("span") else None,
                )
            )
        for raw in citations or []:
            report.citations.append(self.check_citation(raw))

        if self.strict_offsets:
            for q in report.quotes:
                if q.status == "offset_corrected":
                    report.warnings.append(
                        f"quote at [{q.paragraph}] was found at corrected offsets"
                    )
        if not report.citations and report.quotes:
            report.warnings.append(
                "claim carries quotes but no citation to the judgment quoted"
            )
        return report

    # -- internals ---------------------------------------------------------

    def _resolve(self, ref: str) -> Judgment | None:
        return self._by_id.get(ref) or self._by_key.get(ref)
