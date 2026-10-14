"""Resolving case *names* to authorities.

Judgments do not cite by neutral citation every time they refer to a case. They
say "Bramblewood was correctly decided and we would follow it", and the citation
appeared two paragraphs earlier. They say "I do not propose to follow Kingsford
Quay", and Kingsford Quay's citation is in the *previous* paragraph. That is how
legal prose is written: the citation establishes the identity once, and the name
carries it thereafter.

A citator that only follows citation tokens therefore misses exactly the
sentences in which courts say what they think of earlier decisions -- which is
most of them. The fixture corpus in this repository is built to expose this: the
Full Court's approval of Bramblewood, the Federal Circuit Court's refusal to
follow Kingsford Quay, and the primary judge's distinction of Bramblewood are all
expressed by name.

Building the name-to-authority map is done by mining the corpus rather than by
guessing from the case title, for a reason that shows up immediately in the
fixture: every judgment here is "X v Minister for Foreign Investment". A rule
that took the first token of the first party would work; a rule that took
"Minister" would map six different cases onto one authority. So:

  1. Candidate names are mined from the *titles*, restricted to the party names
     that are distinctive (a name appearing in several titles is not a name, it
     is boilerplate).
  2. The candidates are then tested against the corpus: a name is only bound to
     an authority if the corpus itself puts that name next to that authority's
     citation -- "the Full Court in Bramblewood [2019] FCAFC 77". Where the corpus
     never makes the connection, the name is left unresolved and reported.

Step 2 is what makes this safe. The alias table is evidenced by the corpus, not
asserted by a heuristic, so a name that maps to two authorities is reported as
ambiguous rather than resolved by taking whichever came first.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_GENERIC = frozenset(
    """
    minister commonwealth australia australian state states territory council
    commissioner court federal high full court chief executive director secretary
    attorney general the and or of for in re ex parte no pty ltd limited inc
    corporation company trust holdings group services investments investment
    properties resources proprietary
    """.split()
)

# Public-law corpora are dominated by proceedings against the Crown, and the
# Crown's party name is boilerplate: "Minister for Foreign Investment",
# "Commissioner of Taxation", "Secretary of the Department of ...". Every one of
# those would otherwise become a candidate "name" mapping to every judgment in
# the corpus at once. Listing the portfolio words here is unglamorous and is what
# makes the alias table usable on real Australian material.
_PORTFOLIO_WORDS = frozenset(
    """
    foreign immigration citizenship multicultural affairs aboriginal torres strait
    islander taxation treasurer treasury finance home defence infrastructure
    transport regional development agriculture water education employment
    workplace relations industry science communications arts environment energy
    health aged care social families housing veterans trade tourism
    """.split()
)

# Words that look like party names but never are, because they are how courts
# refer to themselves or to the parties generically.
_NOT_A_NAME = _GENERIC | _PORTFOLIO_WORDS | frozenset(
    {"appellant", "respondent", "applicant", "plaintiff", "defendant", "crown"}
)

_TOKEN_RE = re.compile(r"[A-Z][A-Za-z'’-]*")


@dataclass(frozen=True)
class NameMention:
    """One occurrence of a resolved case name in a passage of text."""

    name: str
    key: str
    span: tuple[int, int]


@dataclass
class AliasReport:
    resolved: dict[str, str] = field(default_factory=dict)
    # Names that the corpus could not tie to a single authority.
    ambiguous: dict[str, set[str]] = field(default_factory=dict)
    # Candidates seen in titles but never evidenced in the corpus text.
    unevidenced: set[str] = field(default_factory=set)

    def lines(self) -> list[str]:
        out = [f"case-name aliases resolved: {len(self.resolved)}"]
        for name, key in sorted(self.resolved.items()):
            out.append(f"  {name!r} -> {key}")
        for name, keys in sorted(self.ambiguous.items()):
            out.append(
                f"  {name!r} is AMBIGUOUS between {', '.join(sorted(keys))}; "
                "references by this name will not be resolved"
            )
        if self.unevidenced:
            out.append(
                "  title candidates that resolved to nothing (never evidenced "
                "beside a citation, or ambiguous): "
                + ", ".join(sorted(self.unevidenced))
            )
        return out


class CaseAliasResolver:
    """Maps short case names to canonical authority keys.

    Construction is two-pass and order-independent: mine candidates from titles,
    then confirm each candidate against the citation contexts in the corpus.
    """

    def __init__(self) -> None:
        self._resolved: dict[str, str] = {}
        self._ambiguous: dict[str, set[str]] = {}
        self._candidates: dict[str, set[str]] = {}
        # Names the corpus itself tied to a citation, with the authorities they
        # were tied to. Only these become aliases.
        self._evidenced: dict[str, set[str]] = {}
        self._pattern: re.Pattern[str] | None = None

    # -- pass 1: candidates from titles -------------------------------------

    @staticmethod
    def candidate_names(title: str) -> list[str]:
        """Distinctive party names in a case title, longest first.

        Generates unigrams, bigrams and trigrams over capitalised runs on each
        side of the "v", so that "Kingsford Quay" is available as well as
        "Kingsford", and drops anything generic or too short to be a name.
        """
        sides = re.split(r"\s+v\s+", title, maxsplit=1, flags=re.IGNORECASE)
        names: list[str] = []
        for side in sides:
            tokens = [
                t
                for t in _TOKEN_RE.findall(side)
                if t.lower() not in _NOT_A_NAME and len(t) >= 4
            ]
            for size in (3, 2, 1):
                for i in range(len(tokens) - size + 1):
                    ngram = " ".join(tokens[i : i + size])
                    names.append(ngram)
        # Longest first: "Kingsford Quay" must be preferred over "Kingsford".
        names.sort(key=lambda n: (-len(n), n))
        return names

    def add_title_candidates(self, title: str, key: str) -> None:
        for name in self.candidate_names(title):
            self._candidates.setdefault(name, set()).add(key)

    # -- pass 2: evidence from the corpus -----------------------------------

    def build_candidate_pattern(self) -> None:
        """Make the *candidate* names matchable, before they are confirmed.

        The evidence pass has to be able to see a candidate name in order to test
        it, so the matching pattern is built twice: once over the candidates, to
        find the evidence, and once over the survivors, to use the table. Leaving
        it until after confirmation -- which the first version of this module did
        -- means the evidence pass silently matches nothing and every candidate
        survives on its title alone, which is exactly the un-evidenced alias table
        the design is meant to avoid.
        """
        self._pattern = self._compile(sorted(self._candidates, key=lambda n: (-len(n), n)))

    @staticmethod
    def _compile(names: list[str]) -> re.Pattern[str] | None:
        if not names:
            return None
        # Longest alternative first, so "Kingsford Quay" wins over "Kingsford".
        return re.compile(r"\b(" + "|".join(re.escape(n) for n in names) + r")\b")

    def evidence_from_context(
        self, text: str, citation_span: tuple[int, int], key: str, *, window: int = 80
    ) -> list[str]:
        """Learn a name from "<name> ... [citation]" and return what was learned.

        The name must occur within ``window`` characters *before* the citation
        and inside the same sentence, which is the shape the corpus actually
        uses: "the Full Court in Bramblewood Nominees Pty Ltd v Minister for
        Foreign Investment [2019] FCAFC 77". A name occurring after the citation
        is not evidence, because "X [2019] FCAFC 77, approving Y" attaches the
        approval to Y, and treating the trailing name as the cited case would
        invert the reference.
        """
        learned: list[str] = []
        start = max(0, citation_span[0] - window)
        leading = text[start : citation_span[0]]
        # Cut at the last sentence boundary, so a name from the previous sentence
        # is not attributed to this citation.
        cut = max(leading.rfind("."), leading.rfind(";"), leading.rfind("\n"))
        if cut >= 0:
            leading = leading[cut + 1 :]
        for name in self._names_in(leading):
            candidates = self._candidates.get(name)
            if candidates is None:
                # A name the corpus uses that no title predicted is still usable
                # -- judgments abbreviate, and a party name need not be the first
                # words of the title -- but only if it is unique here.
                self._candidates[name] = {key}
                learned.append(name)
                self._evidenced.setdefault(name, set()).add(key)
                continue
            if key in candidates:
                learned.append(name)
                self._evidenced.setdefault(name, set()).add(key)
        return learned

    def confirm(self) -> AliasReport:
        """Freeze the alias table, reporting anything the corpus did not settle.

        Only names the corpus *evidenced* become aliases, together with the
        leading fragments of those names. The fragment rule exists because
        judgments are written that way: the first mention gives the full name with
        the citation, and every later mention says "Bramblewood". A fragment is
        admitted only where its key is unambiguous across every evidenced name
        that starts with it, so two cases sharing an opening word resolve to
        nothing rather than to whichever was seen first.
        """
        key_sets: dict[str, set[str]] = {}
        for name, keys in self._evidenced.items():
            tokens = name.split()
            for size in range(1, len(tokens) + 1):
                fragment = " ".join(tokens[:size])
                key_sets.setdefault(fragment, set()).update(keys)

        self._resolved = {}
        self._ambiguous = {}
        for name, keys in key_sets.items():
            if len(keys) == 1:
                self._resolved[name] = next(iter(keys))
            else:
                self._ambiguous[name] = set(keys)
        self._build_pattern()
        return AliasReport(
            resolved=dict(self._resolved),
            ambiguous={k: set(v) for k, v in self._ambiguous.items()},
            # Title candidates that became nothing at all: never evidenced beside
            # a citation, and not a fragment of one that was. Reported rather than
            # dropped quietly, because a case title the corpus never refers to by
            # name is a fact about the corpus.
            unevidenced=set(self._candidates)
            - set(self._resolved)
            - set(self._ambiguous),
        )

    # -- use ---------------------------------------------------------------

    def _build_pattern(self) -> None:
        self._pattern = self._compile(
            sorted(self._resolved, key=lambda n: (-len(n), n))
        )

    def names_in(self, text: str) -> list[NameMention]:
        """Resolved case-name mentions in ``text``, longest-match, in order."""
        return [
            NameMention(name=name, key=self._resolved[name], span=span)
            for name, span in self._match_names(text)
            if name in self._resolved
        ]

    def _match_names(self, text: str) -> list[tuple[str, tuple[int, int]]]:
        """Raw name matches, before any table lookup. Used by the evidence pass,
        which runs while the table is still being built."""
        if self._pattern is None:
            return []
        return [(m.group(0), (m.start(), m.end())) for m in self._pattern.finditer(text)]

    def _names_in(self, text: str) -> list[str]:
        return [name for name, _ in self._match_names(text)]

    @property
    def resolved(self) -> dict[str, str]:
        return dict(self._resolved)


def _overlaps(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return a[0] < b[1] and b[0] < a[1]
