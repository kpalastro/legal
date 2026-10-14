"""Query understanding: what is this lawyer actually asking for?

A legal query is not a bag of words, and treating it as one throws away the parts
that are most informative. "Is Bramblewood [2019] FCAFC 77 still good law as at
2021?" contains three separable instructions:

  * a *target* -- the judgment cited by neutral citation, which must be resolved
    by exact match, never by similarity;
  * an *operation* -- "still good law", which selects a different code path
    entirely (the citation graph, not the index);
  * a *temporal anchor* -- "as at 2021", which makes the answer a point-in-time
    query and which is meaningless to a retriever.

This module does that decomposition with patterns, not with a model. It is
deliberately a parser rather than a classifier: a lawyer can read the regexes and
tell you where they are wrong, which is not true of a fine-tuned intent head. And
it means the whole pipeline stays runnable with no network.

The decomposition is exposed on the object rather than consumed invisibly, so a
caller can see what was understood and override it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum

from ratio.citations import CitationParser, resolve_court
from ratio.model import CitationKind

class QueryIntent(str, Enum):
    """What the caller wants done. Each value selects a different code path."""

    FIND_AUTHORITY = "find_authority"  # locate material on a point
    CHECK_AUTHORITY = "check_authority"  # is this still good law, how treated
    TRACE_PRINCIPLE = "trace_principle"  # how has this proposition developed
    APPLY_RULE = "apply_rule"  # run a ruleset over facts
    UNKNOWN = "unknown"


# Ordered most-specific first: "still good law" must win over "good law" alone,
# and "overruled" must win over the generic "authority".
_INTENT_CUES: tuple[tuple[QueryIntent, tuple[str, ...]], ...] = (
    (
        QueryIntent.CHECK_AUTHORITY,
        (
            "still good law",
            "good law",
            "been overruled",
            "overruled",
            "been reversed",
            "been doubted",
            "been distinguished",
            "how has .* been treated",
            "treatment of",
            "is .* still",
            "authority status",
            "has .* been applied",
        ),
    ),
    (
        QueryIntent.TRACE_PRINCIPLE,
        (
            "developed",
            "evolution of",
            "history of",
            "how has the test",
            "line of authority",
            "subsequent cases",
            "judicial treatment",
        ),
    ),
    (
        QueryIntent.APPLY_RULE,
        ("is this notifiable", "does the ruleset", "apply the ruleset", "run the rule"),
    ),
    (
        QueryIntent.FIND_AUTHORITY,
        (
            "what is the test",
            "what does .* say",
            "cases about",
            "authority for",
            "find case",
            "what are the elements",
        ),
    ),
)


@dataclass
class LegalQuery:
    """A parsed query: what was asked, and what was understood."""

    raw: str
    intent: QueryIntent = QueryIntent.UNKNOWN
    # Neutral citations appearing in the query, canonicalised where possible.
    target_keys: list[str] = field(default_factory=list)
    raw_citations: list[str] = field(default_factory=list)
    statute_refs: list[str] = field(default_factory=list)
    courts: list[str] = field(default_factory=list)
    as_at: date | None = None
    # The residual free text, with citations and stop-phrases removed. This is
    # what goes to the retriever.
    issue_text: str = ""
    # Anything the caller should know about how the query was read.
    notes: list[str] = field(default_factory=list)

    @property
    def is_point_in_time(self) -> bool:
        return self.as_at is not None

    @property
    def needs_graph(self) -> bool:
        """Whether answering requires the citation graph rather than the index."""
        return self.intent is QueryIntent.CHECK_AUTHORITY

    def explain(self) -> str:
        lines = [f'intent: {self.intent.value}   query: "{self.raw}"']
        if self.target_keys:
            lines.append("  target authorities: " + ", ".join(self.target_keys))
        if self.statute_refs:
            lines.append("  provisions: " + ", ".join(self.statute_refs))
        if self.courts:
            lines.append("  courts: " + ", ".join(self.courts))
        if self.as_at:
            lines.append(f"  as at: {self.as_at.isoformat()}")
        lines.append(f"  issue text: {self.issue_text!r}")
        for note in self.notes:
            lines.append(f"  note: {note}")
        return "\n".join(lines)


class QueryParser:
    """Turns a natural-language legal question into a :class:`LegalQuery`."""

    def __init__(self) -> None:
        self.citations = CitationParser()

    def parse(self, raw: str) -> LegalQuery:
        query = LegalQuery(raw=raw)

        for match in self.citations.extract(raw):
            if match.kind is CitationKind.STATUTORY:
                if match.target_key:
                    query.statute_refs.append(match.target_key)
                continue
            query.raw_citations.append(match.raw)
            if match.target_key:
                query.target_keys.append(match.target_key)
            elif match.court:
                # A citation-shaped string whose year or number is malformed. It
                # is reported, not silently dropped: a lawyer who typed a neutral
                # citation we cannot parse has probably mistyped it, and telling
                # them so is more useful than quietly answering a different
                # question.
                query.notes.append(
                    f"{match.raw!r} looks like a citation but could not be "
                    "canonicalised; check the year and number"
                )

        query.courts = self._courts(raw)
        query.as_at = self._as_at(raw)
        query.intent = self._intent(raw)
        query.issue_text = self._issue_text(raw, query)

        if query.intent is QueryIntent.CHECK_AUTHORITY and not query.target_keys:
            query.notes.append(
                "the query asks about the status of an authority but names no "
                "resolvable citation; a status question without a target cannot "
                "be answered"
            )
        return query

    # -- components --------------------------------------------------------

    @staticmethod
    def _courts(raw: str) -> list[str]:
        found: list[str] = []
        for token in raw.replace(",", " ").split():
            court = resolve_court(token.strip("().[]?;:"))
            if court is not None and court.value not in found:
                found.append(court.value)
        return found

    @staticmethod
    def _as_at(raw: str) -> date | None:
        """Extract a temporal anchor.

        "as at 2021" means the end of 2021 for the purposes of asking what the law
        was during that year, which is the reading a lawyer intends when they say
        the law "as at 2021". Being explicit about the convention matters, because
        the other reading -- 1 January -- would silently exclude authorities the
        person expected to see.
        """
        import re

        lowered = raw.lower()
        marker = None
        for cue in ("as at", "as of", "at the date of", "in "):
            if cue in lowered:
                marker = cue
                break
        if marker is None:
            return None
        tail = lowered.split(marker, 1)[1]
        iso = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", tail)
        if iso:
            return date.fromisoformat(iso.group(0))
        year = re.search(r"\b(19|20)\d{2}\b", tail)
        if year:
            return date(int(year.group(0)), 12, 31)
        month_year = re.search(r"\b([a-z]{3,9})\s+((?:19|20)\d{2})\b", tail)
        if month_year:
            return date(int(month_year.group(2)), 12, 31)
        return None

    @staticmethod
    def _intent(raw: str) -> QueryIntent:
        import re

        lowered = raw.lower()
        for intent, cues in _INTENT_CUES:
            for cue in cues:
                if re.search(cue, lowered):
                    return intent
        return QueryIntent.UNKNOWN

    def _issue_text(self, raw: str, query: LegalQuery) -> str:
        """Strip the citation apparatus, leaving the substantive question."""
        text = raw
        for match in query.raw_citations:
            text = text.replace(match, " ")
        import re

        # Drop the framing clauses that carry no retrieval signal and that, worse,
        # pull the ranking toward judgments about procedure rather than substance.
        text = re.sub(
            r"\b(as at|as of|at the date of|still good law|good law|"
            r"is there any authority|what is the test for|case law on)\b",
            " ",
            text,
            flags=re.IGNORECASE,
        )
        text = re.sub(r"\s+", " ", text).strip(" ?.,")
        return text or raw
