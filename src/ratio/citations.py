"""Citation parsing and treatment classification.

Two jobs:

1. **Find and decompose references.** Turn the prose of a judgment into a list
   of typed references with character offsets preserved, so that anything
   downstream can point back at the exact bytes it relied on.

2. **Classify how a later court treated an earlier authority.** This is what
   separates a citator from a search index. The vocabulary is a closed set
   drawn from Australian practice (see docs/02-judgment-knowledge-base.md); the
   parser proposes, and a lawyer confirms.

Everything here is deterministic and testable. No model is involved. Where the
patterns fail, the reference lands as ``CitationKind.OTHER`` rather than being
guessed at -- an unparsed citation is a known unknown, whereas a
confidently-misparsed one is a silent error.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping

from ratio.model import Citation, CitationKind, Court, Jurisdiction, Treatment

# --------------------------------------------------------------------------
# Court codes
# --------------------------------------------------------------------------

COURT_CODES: dict[str, Court] = {
    "HCA": Court.HCA,
    "FCAFC": Court.FCAFC,
    "FCA": Court.FCA,
    "FedCFamC1A": Court.FedCFamC1A,
    "FedCFamC1F": Court.FedCFamC1F,
    "FedCFamC2G": Court.FedCFamC2G,
    "NSWCA": Court.NSWCA,
    "NSWSC": Court.NSWSC,
    "VSCA": Court.VSCA,
    "VSC": Court.VSC,
    "QCA": Court.QCA,
    "QSC": Court.QSC,
    "WACA": Court.WACA,
    "WASCA": Court.WASCA,
    "SASC": Court.SASC,
    "SASCA": Court.SASCA,
    "TASSC": Court.TASSC,
    "NTCA": Court.NTCA,
    "ACTCA": Court.ACTCA,
    "ART": Court.ART,
    "AAT": Court.AAT,
}

# Case-insensitive fallback, because "FedCFamC2G" is sometimes written
# "FEDCFAMC2G" in older transcripts and citation lists.
_COURT_CODES_CASEFOLD: dict[str, Court] = {k.casefold(): v for k, v in COURT_CODES.items()}


def resolve_court(code: str) -> Court | None:
    """Map a neutral-citation court code to the hierarchy, or None.

    Returning None rather than ``Court.OTHER`` matters: an unrecognised code is
    a parser gap, whereas ``OTHER`` is a court we know about and do not rank.
    Conflating them would hide new court codes entering the corpus.
    """
    if code in COURT_CODES:
        return COURT_CODES[code]
    return _COURT_CODES_CASEFOLD.get(code.casefold())


_JURIS_BY_COURT: dict[Court, Jurisdiction] = {
    Court.HCA: Jurisdiction.CTH,
    Court.FCAFC: Jurisdiction.CTH,
    Court.FCA: Jurisdiction.CTH,
    Court.ART: Jurisdiction.CTH,
    Court.NSWCA: Jurisdiction.NSW,
    Court.NSWSC: Jurisdiction.NSW,
    Court.VSCA: Jurisdiction.VIC,
    Court.VSC: Jurisdiction.VIC,
    Court.QCA: Jurisdiction.QLD,
    Court.QSC: Jurisdiction.QLD,
    Court.WACA: Jurisdiction.WA,
    Court.WASCA: Jurisdiction.WA,
    Court.TASSC: Jurisdiction.TAS,
    Court.NTCA: Jurisdiction.NT,
    Court.ACTCA: Jurisdiction.ACT,
}

# --------------------------------------------------------------------------
# Patterns
# --------------------------------------------------------------------------

# Neutral citation: [2026] FCA 123  /  [1992] HCA 23  /  [2025] FedCFamC2G 1314
#
# The court code is NOT all-caps: the Federal Circuit and Family Court of
# Australia publishes FedCFamC1A, FedCFamC1F and FedCFamC2G. Matching [A-Z]+
# here would silently drop every citation to that court, and dropped citations
# are invisible -- they do not fail, they just never appear, so the citator
# reports an authority as untroubled when it is not. Whitespace is tolerated
# because "[1992]  HCA  23" does occur in converted text.
_NEUTRAL_RE = re.compile(
    r"\[\s*(?P<year>(?:18|19|20)\d{2})\s*\]\s+"
    r"(?P<court>[A-Za-z][A-Za-z0-9]{1,11})\s+"
    r"(?P<number>\d{1,5})"
    r"(?:\s+(?:at|at\s+paras?\.?)\s+\[(?P<pin>\d{1,4})\])?",
)

# Report series recognised in Australian practice, authorised reports first.
_REPORTED_RE = re.compile(
    r"\(\s*(?P<year>(?:18|19|20)\d{2})\s*\)\s+"
    r"(?:(?P<volume>\d{1,4})\s+)?"
    r"(?P<series>CLR|FCR|ALR|ACSR|ALJR|FLR|NSWLR|VR|Qd\s?R|WAR|SASR|Tas\s?R|NTLR|ACTLR|IR|ATR)"
    r"\s+(?P<page>\d{1,5})",
)

# Authorised (most authoritative) report series, per AGLC4 r 2.2.2.
AUTHORISED_REPORTS: frozenset[str] = frozenset(
    {"CLR", "FCR", "NSWLR", "VR", "Qd R", "WAR", "SASR", "Tas R", "NTLR", "ACTLR"}
)


# Statutory reference:
#   Foreign Acquisitions and Takeovers Act 1975 (Cth) s 18
#   Corporations Act 2001, s 674(2)
#   s 50 of the Competition and Consumer Act 2010 (Cth)
_ACT_TITLE = r"(?P<act>[A-Z][A-Za-z'’,\-]*(?:\s+(?:and|of|the|for|on|to|[A-Z][A-Za-z'’\-]*))*)"
_STATUTE_FORWARD_RE = re.compile(
    _ACT_TITLE + r"\s+(?P<act_year>(?:18|19|20)\d{2})\s*"
    r"\((?:Cth|NSW|Vic|Qld|WA|SA|Tas|NT|ACT)\)\s*,?\s*"
    r"(?:ss?|sections?)\s*(?P<section>\d+[A-Za-z]*(?:\([0-9a-zA-Zivx]+\))*)",
)
_STATUTE_REVERSE_RE = re.compile(
    r"(?:ss?|sections?)\s*(?P<section>\d+[A-Za-z]*(?:\([0-9a-zA-Zivx]+\))*)\s+of\s+the\s+"
    + _ACT_TITLE
    + r"\s+(?P<act_year>(?:18|19|20)\d{2})",
)

# A pin cite following a citation: "at [42]", "at [42]-[45]", "at [42], [50]"
_PIN_RE = re.compile(r"\bat\s+\[(?P<first>\d{1,4})\]")

# --------------------------------------------------------------------------
# Treatment vocabulary
# --------------------------------------------------------------------------

# Order matters: longest / most specific first, so "not followed" is not
# swallowed by "followed" and "departed from" is not swallowed by "from".
_TREATMENT_PATTERNS: tuple[tuple[Treatment, tuple[str, ...]], ...] = (
    (
        Treatment.NOT_FOLLOWED,
        (r"\bnot\s+follow(?:ed|s)?\b", r"\brefus(?:ed|es)\s+to\s+follow\b"),
    ),
    (
        Treatment.OVERRULED,
        (
            r"\boverrul(?:ed|es|ing)\b",
            r"\bsupersed(?:ed|es)\b",
            r"\bno\s+longer\s+good\s+law\b",
        ),
    ),
    (
        Treatment.DEPARTED_FROM,
        (r"\bdepart(?:ed|s|ing)\s+from\b",),
    ),
    (
        Treatment.DOUBTED,
        (
            r"\bdoubt(?:ed|s|ful)\b",
            r"\bquestion(?:ed|s)\s+the\s+(?:correctness|authority)\b",
            r"\bdeclin(?:ed|es)\s+to\s+follow\b",
        ),
    ),
    (
        Treatment.DISTINGUISHED,
        (
            r"\bdistinguish(?:ed|es|able|ing)\b",
            r"\bconfined\s+to\s+its\s+own\s+facts\b",
        ),
    ),
    (
        Treatment.APPLIED,
        (r"\bappl(?:ied|ies|ying)\b",),
    ),
    (
        Treatment.FOLLOWED,
        (r"\bfollow(?:ed|s|ing)?\b",),
    ),
    (
        Treatment.APPROVED,
        (
            r"\bapprov(?:ed|es|ing)\b",
            r"\baffirm(?:ed|s)\b",
            r"\bwith\s+approval\b",
            r"\bendors(?:ed|es)\b",
        ),
    ),
    (
        Treatment.CONSIDERED,
        (r"\bconsider(?:ed|s|ing)\b",),
    ),
    (
        Treatment.REFERRED_TO,
        (
            r"\breferr?(?:ed|s|ing)\s+to\b",
            r"\bsee\s+(?:also|generally)\b",
            r"\bcited\b",
        ),
    ),
)

_TREATMENT_COMPILED: tuple[tuple[Treatment, tuple[re.Pattern[str], ...]], ...] = tuple(
    (t, tuple(re.compile(p, re.IGNORECASE) for p in pats))
    for t, pats in _TREATMENT_PATTERNS
)

# Signals that a negative-sounding phrase is actually being rejected by the
# citing court ("the respondent submitted that the case should be
# distinguished, but I do not accept that"). Cheap hedge against the most
# common false positive; the human confirmation step is the real safeguard.
_SUBMISSION_CUES = re.compile(
    r"\b(?:submitted|contended|argued|asserted|counsel\s+for|it\s+was\s+put)\b",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Negation.
#
# The single most dangerous error a citator can make is to read "I do not
# propose to follow Kingsford Quay" as *followed*. It inverts the meaning of the
# sentence and it does so in the direction that matters most: a court declining
# to follow an authority gets recorded as endorsing it. The earlier version of
# this module had patterns for "not followed" and "declined to follow" and
# nothing else, which covers the adjective ("was not followed") and misses the
# constructions English actually uses ("do not propose to follow", "is not
# prepared to follow", "should not be followed").
#
# Two mechanisms, because neither is sufficient alone:
#
#   1. explicit patterns for the idiomatic forms, which also lets the negated
#      phrase be reported to the reviewer in the words the judgment used;
#   2. a scope check on the clause preceding any matched phrase, which catches
#      the forms nobody thought to enumerate.
#
# Where a negated phrase has no defensible opposite in the vocabulary, the edge
# is *suppressed* rather than guessed at. "The Minister did not contend that the
# facts are distinguishable" says nothing about how any court treated the case,
# and there is no honest way to render it as an edge.
# ---------------------------------------------------------------------------

_NEGATED_PATTERNS: tuple[tuple[Treatment, tuple[str, ...]], ...] = (
    (
        Treatment.NOT_FOLLOWED,
        (
            r"\bnot\s+follow(?:ed|s|ing)?\b",
            r"\bnot\s+(?:prepared|willing|disposed|bound)\s+to\s+follow\b",
            r"\bnot\s+propos(?:e|ed|es|ing)\s+to\s+follow\b",
            r"\b(?:refus(?:e|ed|es|ing)|declin(?:e|ed|es|ing))\s+to\s+follow\b",
            r"\bshould\s+not\s+be\s+follow(?:ed)?\b",
            r"\bcannot\s+be\s+follow(?:ed)?\b",
            r"\bdid\s+not\s+follow\b",
            r"\bno\s+longer\s+follow(?:ed)?\b",
        ),
    ),
    (
        Treatment.DOUBTED,
        (
            r"\bnot\s+approv(?:ed|es)\b",
            r"\bdisapprov(?:ed|es)\b",
            r"\bquestion(?:ed|s)\s+whether\s+.*\bis\s+correct\b",
        ),
    ),
)

_NEGATED_COMPILED = tuple(
    (t, tuple(re.compile(p, re.IGNORECASE) for p in pats))
    for t, pats in _NEGATED_PATTERNS
)

# A negating cue in the clause immediately preceding a treatment phrase. Bounded
# lookback, and the clause is cut at a comma, semicolon, dash or sentence start,
# so that a "not" belonging to a different proposition does not reach the phrase.
_NEGATION_SCOPE_RE = re.compile(
    r"\b(?:not|never|no\s+longer|nor|neither|refus(?:e|ed|es|ing)|"
    r"declin(?:e|ed|es|ing)|unwilling|unprepared|cannot|can\s+not|"
    r"fail(?:ed|s|ing)?\s+to|unable\s+to|without|ought\s+not|should\s+not)\b",
    re.IGNORECASE,
)

_CLAUSE_BREAK_RE = re.compile(r"[,;:—–]|\band\b|\bbut\b|\bhowever\b|\balthough\b")

# What a negated positive treatment becomes. Anything not listed here has no
# defensible opposite, and the edge is dropped instead of guessed.
_NEGATION_MAP: dict[Treatment, Treatment | None] = {
    Treatment.FOLLOWED: Treatment.NOT_FOLLOWED,
    Treatment.APPLIED: Treatment.NOT_FOLLOWED,  # "declined to apply"
    Treatment.APPROVED: Treatment.DOUBTED,  # "did not approve"
    Treatment.DISTINGUISHED: None,  # "not distinguishable" is a statement about facts
    Treatment.CONSIDERED: None,  # "failed to consider" criticises the court, not the case
    Treatment.REFERRED_TO: None,
    Treatment.OVERRULED: None,
    Treatment.NOT_FOLLOWED: None,
    Treatment.DEPARTED_FROM: None,
    Treatment.DOUBTED: None,
}

# Treatment verbs that are not treatments. Legal prose is full of the passive and
# the infinitive, and both read as treatment verbs to a pattern matcher:
#
#   * "the association provisions are to be applied as a question of substance"
#     -- "to be applied" says how a rule is to be used, and no court is doing
#     anything to any authority;
#   * "a requirement to be followed in assessing X" -- same;
#   * "the principle follows from X" -- entailment, not treatment;
#   * "application was made for review" -- "applied for" is a procedural phrase
#     that has nothing to do with an authority.
#
# These are suppressed rather than recorded at low confidence, because a
# suppressed match is not a judgement call anybody has to revisit.
_NOT_A_TREATMENT_RE = re.compile(
    r"\b(?:to\s+be|is\s+to\s+be|are\s+to\s+be|was\s+to\s+be|were\s+to\s+be)\s+"
    r"(?:applied|followed)\b"
    r"|\bappl(?:ied|ies|ying)\s+for\b"
    r"|\bfollow(?:s|ed)?\s+from\b",
    re.IGNORECASE,
)

# An anaphoric subject immediately before the treatment phrase -- "that
# expression should not be followed", "this reasoning cannot be applied". The
# target of the treatment is then a noun phrase from an earlier sentence, not the
# nearest case name, and binding it to the nearest name is exactly the mistake
# this module exists to avoid. Detected and damped rather than resolved.
_ANAPHORA_RE = re.compile(
    r"\b(?:that|this|those|these|such)\s+"
    r"(?:expression|expressions|reasoning|observations|remarks|statement|"
    r"statements|passage|passages|view|approach|analysis|formulation|test|"
    r"dicta|conclusion|conclusions)\b|"
    r"\bthe\s+(?:reasoning|observations|remarks|passage|approach|analysis|"
    r"view|expression)\s+(?:in\s+that\s+respect|there|above|set\s+out)\b",
    re.IGNORECASE,
)

# How far back the negation scope reaches, and how far back an anaphor counts.
_NEGATION_LOOKBACK = 60


@dataclass(frozen=True)
class TreatmentCandidate:
    treatment: Treatment
    matched_phrase: str
    negated_by_submission: bool

    @property
    def confidence(self) -> float:
        """Heuristic only. Never used to auto-accept an edge."""
        if self.negated_by_submission:
            return 0.2
        if self.treatment in {Treatment.OVERRULED, Treatment.NOT_FOLLOWED}:
            return 0.7
        if self.treatment.is_positive:
            return 0.6
        return 0.4


@dataclass(frozen=True)
class TreatmentBinding:
    """One treatment phrase, bound to the one authority it governs."""

    treatment: Treatment
    phrase: str
    phrase_span: tuple[int, int]
    target_span: tuple[int, int]
    confidence: float
    negated_by_submission: bool
    # True when the phrase governs a noun phrase from an earlier sentence, so the
    # binding to the nearest authority is unreliable.
    anaphoric: bool = False

    def describe(self) -> str:
        note = " [anaphoric]" if self.anaphoric else ""
        return (
            f"{self.treatment.value} -> {self.target_span} "
            f'via "{self.phrase}" (conf {self.confidence:.2f}){note}'
        )


# --------------------------------------------------------------------------
# Extraction
# --------------------------------------------------------------------------


class CitationParser:
    """Stateless parser. Cheap to construct; safe to reuse."""

    def extract(
        self,
        text: str,
        *,
        paragraph_number: int | None = None,
        judge: str | None = None,
    ) -> list[Citation]:
        """All references in ``text``, in order of appearance.

        Offsets are into ``text``, not into the judgment, so the caller can add
        the paragraph's ``char_start`` to get judgment-level offsets. Each
        citation carries the paragraph anchor it was found in, because a
        treatment edge is only auditable if you can point at the paragraph that
        asserted it.
        """
        found: list[tuple[int, Citation]] = []

        for m in _NEUTRAL_RE.finditer(text):
            court_code = m.group("court")
            # Guard against bracketed numbers in ordinary prose ("[2024] the
            # 5 year period"). A real court code has an uppercase letter and at
            # least two characters; anything else is recorded as unresolved
            # rather than attributed to a court that does not exist.
            if len(court_code) < 2 or not any(c.isupper() for c in court_code):
                continue
            court = resolve_court(court_code)
            pin = int(m.group("pin")) if m.group("pin") else None
            found.append(
                (
                    m.start(),
                    Citation(
                        raw=m.group(0),
                        kind=CitationKind.NEUTRAL if court else CitationKind.OTHER,
                        court=court,
                        year=int(m.group("year")),
                        number=int(m.group("number")),
                        jurisdiction=_JURIS_BY_COURT.get(court) if court else None,
                        span_start=m.start(),
                        span_end=m.end(),
                        context=sentence_containing(text, m.start()),
                        paragraph=paragraph_number,
                        judge=judge,
                        pin_cite=pin,
                    ),
                )
            )

        for m in _REPORTED_RE.finditer(text):
            found.append(
                (
                    m.start(),
                    Citation(
                        raw=m.group(0),
                        kind=CitationKind.REPORTED,
                        year=int(m.group("year")),
                        span_start=m.start(),
                        span_end=m.end(),
                        context=sentence_containing(text, m.start()),
                        paragraph=paragraph_number,
                        judge=judge,
                    ),
                )
            )

        for m in _STATUTE_FORWARD_RE.finditer(text):
            found.append((m.start(), self._statute(m, text, paragraph_number, judge)))

        for m in _STATUTE_REVERSE_RE.finditer(text):
            found.append((m.start(), self._statute(m, text, paragraph_number, judge)))

        # Overlapping matches (a reported cite inside a neutral cite's context,
        # or a statute matched twice) are deduplicated by span, preferring the
        # longer match.
        found.sort(key=lambda pair: (pair[0], -(pair[1].span_end - pair[1].span_start)))
        deduped: list[Citation] = []
        occupied: list[tuple[int, int]] = []
        for _, cit in found:
            if any(_overlaps((cit.span_start, cit.span_end), o) for o in occupied):
                continue
            occupied.append((cit.span_start, cit.span_end))
            deduped.append(cit)

        return self._attribute_treatments(text, deduped)

    @staticmethod
    def _attribute_treatments(text: str, citations: list[Citation]) -> list[Citation]:
        """Attach each treatment phrase to the citation it actually governs.

        Sentence-level classification is not good enough, and this is the
        reason why: "The Court in X [2024] FCA 812 applied the test in Y [1992]
        HCA 23" contains one treatment verb and two citations. Attaching
        "applied" to both invents a treatment edge for Y, which then propagates
        through the citator and changes an authority's status on the strength of
        a classification nobody ever made. A citator that invents edges is worse
        than one that misses them.

        The binding itself lives in :meth:`bind_treatments`, because the same
        question arises for references made by case *name* rather than by
        citation token, and the two must not develop different rules.
        """
        if not citations:
            return []

        case_like = [
            i for i, c in enumerate(citations) if c.kind is not CitationKind.STATUTORY
        ]
        if not case_like:
            return citations

        targets = {i: (citations[i].span_start, citations[i].span_end) for i in case_like}
        bindings = CitationParser.bind_treatments(text, targets)

        return [
            citations[i].model_copy(
                update={
                    "treatment": bindings[i].treatment,
                    "treatment_phrase": bindings[i].phrase,
                    "treatment_phrase_span": bindings[i].phrase_span,
                    "treatment_confidence": bindings[i].confidence,
                }
            )
            if i in bindings
            else citations[i]
            for i in range(len(citations))
        ]

    @staticmethod
    def bind_treatments(
        text: str,
        targets: Mapping[int, tuple[int, int]],
    ) -> dict[int, TreatmentBinding]:
        """Bind treatment phrases to targets, one phrase to at most one target.

        ``targets`` maps an arbitrary identifier to a span in ``text``. The
        identifiers are opaque so that the same routine serves citations
        (identified by their index) and case-name mentions (identified by
        whatever the caller likes).

        The loop is over *phrases*, not over targets, and that is the whole
        point. If it were over targets, a sentence naming one case twice -- or a
        phrase governing only the first of two citations -- would attach the same
        phrase to everything in the sentence and invent edges. Each phrase is
        spent once, on the nearest target, and the distance is recorded in the
        confidence so the review queue knows how much the machine is guessing.
        """
        if not targets:
            return {}

        bindings: dict[int, TreatmentBinding] = {}
        # Only targets inside the sentence being scanned are eligible, and
        # sentence boundaries are recomputed per phrase so that a phrase cannot
        # reach across a full stop into the next proposition.
        for sent_start, sent_end in _sentence_bounds(text):
            sentence = text[sent_start:sent_end]
            negated = bool(_SUBMISSION_CUES.search(sentence))
            matches = _treatment_matches(sentence)
            if not matches:
                continue
            eligible = {
                key: span
                for key, span in targets.items()
                if sent_start <= span[0] and span[1] <= sent_end
            }
            if not eligible:
                continue
            for treatment, phrase_start, phrase_end in matches:
                phrase_abs = (sent_start + phrase_start, sent_start + phrase_end)
                best_key: int | None = None
                best_distance = 10**9
                for key, span in eligible.items():
                    distance = _gap_distance(phrase_abs, span)
                    if distance < best_distance:
                        best_distance = distance
                        best_key = key
                if best_key is None:
                    continue

                # Relative-clause inversion. In "..., which Kingsford Quay
                # adopted and approved", the treatment verb is performed *by*
                # Kingsford Quay *on* the authority the relative clause modifies,
                # so the nearer target is the subject and the further one is the
                # object. Proximity inverts the edge. Where an earlier target's
                # clause is a bare relative clause ("which <subject> <verb>"), the
                # antecedent governs: bind to it.
                #
                # The rule is deliberately narrow -- a bare "which" or "that"
                # immediately after the earlier target, not "in which" or "of
                # which". "..., in which the primary judge applied X" is the
                # ordinary case in which the subject follows and proximity is
                # right; treating every relative clause as an inversion would
                # break it.
                antecedent = _relative_clause_antecedent(
                    sentence, sent_start, phrase_abs, eligible
                )
                if antecedent is not None and antecedent != best_key:
                    best_key = antecedent
                    best_distance = _gap_distance(
                        phrase_abs, eligible[antecedent]
                    )

                anaphoric = is_anaphoric(sentence, phrase_start)
                confidence = _binding_confidence(best_distance, negated)
                if anaphoric:
                    # The phrase governs a noun phrase from an earlier sentence,
                    # so whichever target it bound to is probably not the one the
                    # court meant. Recorded at a confidence that keeps it out of
                    # the authority computation and in the review queue.
                    confidence = min(confidence, 0.3)
                existing = bindings.get(best_key)
                if existing is not None and existing.confidence >= confidence:
                    continue
                bindings[best_key] = TreatmentBinding(
                    treatment=treatment,
                    phrase=sentence[phrase_start:phrase_end],
                    phrase_span=phrase_abs,
                    target_span=eligible[best_key],
                    confidence=confidence,
                    negated_by_submission=negated,
                    anaphoric=anaphoric,
                )
        return bindings

    @staticmethod
    def _statute(
        m: re.Match[str],
        text: str,
        paragraph_number: int | None,
        judge: str | None,
    ) -> Citation:
        act = re.sub(r"\s+", " ", m.group("act")).strip()
        # Strip a leading article, which the greedy title pattern swallows.
        # "the Foreign Acquisitions and Takeovers Act 1975" and "Foreign
        # Acquisitions and Takeovers Act 1975" must produce the same key, or the
        # same Act appears twice in the graph and its treatment history splits.
        act = re.sub(r"^(?:the|a|an)\s+", "", act, flags=re.IGNORECASE)
        return Citation(
            raw=m.group(0),
            kind=CitationKind.STATUTORY,
            act_title=act,
            act_year=int(m.group("act_year")),
            section=m.group("section"),
            span_start=m.start(),
            span_end=m.end(),
            context=sentence_containing(text, m.start()),
            paragraph=paragraph_number,
            judge=judge,
        )


def classify_treatment(sentence: str) -> TreatmentCandidate | None:
    """Best-effort treatment classification of one citing sentence.

    Returns ``None`` when nothing matches -- deliberately, rather than
    defaulting to "considered". A missing edge is visibly missing; a wrong edge
    silently corrupts the citator.

    Prefer :meth:`CitationParser.extract` for anything that needs to know
    *which* citation a treatment applies to; this function answers only "does
    this sentence express a treatment, and which".
    """
    if not sentence.strip():
        return None
    negated = bool(_SUBMISSION_CUES.search(sentence))
    for treatment, patterns in _TREATMENT_COMPILED:
        for pattern in patterns:
            m = pattern.search(sentence)
            if m:
                return TreatmentCandidate(
                    treatment=treatment,
                    matched_phrase=m.group(0),
                    negated_by_submission=negated,
                )
    return None


def _treatment_matches(sentence: str) -> list[tuple[Treatment, int, int]]:
    """All treatment phrases in a sentence, as (treatment, start, end).

    Overlapping matches are resolved longest-first, so an explicit negated form
    ("not followed") is not also reported as its positive ("followed"). Phrases
    that survive are then put through the negation scope check.
    """
    hits: list[tuple[int, int, Treatment]] = []
    for treatment, patterns in _NEGATED_COMPILED:
        for pattern in patterns:
            for m in pattern.finditer(sentence):
                hits.append((m.start(), m.end(), treatment))
    for treatment, patterns in _TREATMENT_COMPILED:
        for pattern in patterns:
            for m in pattern.finditer(sentence):
                hits.append((m.start(), m.end(), treatment))
    hits.sort(key=lambda h: (h[0], -(h[1] - h[0])))
    chosen: list[tuple[Treatment, int, int]] = []
    occupied: list[tuple[int, int]] = []
    for start, end, treatment in hits:
        if any(_overlaps((start, end), o) for o in occupied):
            continue
        # Widen slightly for the suppression check so that "be applied" is caught
        # by the lookback in the suppression pattern rather than needing the match
        # itself to start at "to".
        context_start = max(0, start - 12)
        if _NOT_A_TREATMENT_RE.search(sentence[context_start:end]):
            occupied.append((start, end))
            continue
        occupied.append((start, end))
        chosen.append((treatment, start, end))

    resolved: list[tuple[Treatment, int, int]] = []
    for treatment, start, end in chosen:
        final = _apply_negation_scope(sentence, treatment, start)
        if final is not None:
            resolved.append((final, start, end))
    resolved.sort(key=lambda h: h[1])
    return resolved


def _apply_negation_scope(
    sentence: str, treatment: Treatment, start: int
) -> Treatment | None:
    """Flip or suppress a treatment phrase that sits inside a negation.

    Returns the treatment to record, or None when the negated form has no
    defensible opposite and the edge should not be created at all.
    """
    if treatment in {t for t, _ in _NEGATED_COMPILED}:
        return treatment  # already an explicit negated form
    prefix_start = max(0, start - _NEGATION_LOOKBACK)
    prefix = sentence[prefix_start:start]
    # Cut at the nearest clause boundary, so a negation in a preceding clause
    # does not govern this one.
    breaks = [m.end() for m in _CLAUSE_BREAK_RE.finditer(prefix)]
    if breaks:
        prefix = prefix[max(breaks) :]
    if not _NEGATION_SCOPE_RE.search(prefix):
        return treatment
    return _NEGATION_MAP.get(treatment, treatment)


def is_anaphoric(sentence: str, phrase_start: int) -> bool:
    """Whether the treatment's subject is a noun phrase from an earlier sentence."""
    prefix = sentence[max(0, phrase_start - _NEGATION_LOOKBACK) : phrase_start]
    return bool(_ANAPHORA_RE.search(prefix))


_BARE_RELATIVE_RE = re.compile(r"^\s*,?\s*(?:which|that)\s+\S")


def _relative_clause_antecedent(
    sentence: str,
    sent_start: int,
    phrase_abs: tuple[int, int],
    targets: Mapping[int, tuple[int, int]],
) -> int | None:
    """The target modified by a bare relative clause containing the phrase.

    Returns the identifier of the antecedent target, or None where the phrase is
    not inside such a clause. See the call site for why the pattern is anchored to
    a bare "which"/"that".

    ``targets`` holds paragraph-absolute spans while ``sentence`` is a slice of
    the paragraph, so the comparison is done in absolute offsets and only the
    slice for the regex is converted back. Mixing the two coordinate spaces is
    silent -- the offsets simply never line up and the rule never fires -- which is
    why the conversion is done in one place here.
    """
    for key, span in targets.items():
        if span[1] > phrase_abs[0]:
            continue
        between = sentence[span[1] - sent_start : phrase_abs[0] - sent_start]
        # The relative clause must be short enough to be the phrase's own clause.
        if len(between) > 60:
            continue
        if _BARE_RELATIVE_RE.match(between):
            return key
    return None


def _gap_distance(a: tuple[int, int], b: tuple[int, int]) -> int:
    """Character distance between two spans; 0 if they touch or overlap."""
    if a[1] <= b[0]:
        return b[0] - a[1]
    if b[1] <= a[0]:
        return a[0] - b[1]
    return 0


def _binding_confidence(distance: int, negated_by_submission: bool) -> float:
    """How much to trust a phrase-to-citation binding.

    Purely a way of ordering the human review queue. Nothing in the system
    treats a high-confidence machine edge as confirmed; only
    :attr:`ratio.model.TreatmentEdge.confirmed_by` does that.
    """
    if negated_by_submission:
        return 0.15
    if distance <= 40:
        return 0.7
    if distance <= 120:
        return 0.5
    return 0.3


_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z(\[])")


def _sentence_bounds(text: str) -> list[tuple[int, int]]:
    """Character spans of sentences.

    Abbreviation-tolerant enough for legal prose: we do not split after "s.",
    "Mr.", "No." or an internal citation, because we only split at sentence
    punctuation followed by whitespace and a capital or an opening bracket, and
    a citation never begins with a capital followed by whitespace in that way.
    """
    bounds: list[tuple[int, int]] = []
    start = 0
    for m in _SENTENCE_SPLIT_RE.finditer(text):
        bounds.append((start, m.start()))
        start = m.end()
    if start < len(text):
        bounds.append((start, len(text)))
    return bounds


def sentences_share(text: str, a: int, b: int) -> bool:
    """Whether two offsets fall in the same sentence.

    Used to decide whether a case-name mention and a citation token for the same
    authority are one reference or two. In the same sentence they are one mention
    of one authority, and keeping both would let a treatment phrase be spent twice;
    in different sentences they are separate mentions, and a phrase in the later
    sentence may well refer to the name and not to the token.
    """
    for start, end in _sentence_bounds(text):
        if start <= a < end:
            return start <= b < end
    return False


def sentence_containing(text: str, offset: int) -> str:
    """The sentence around ``offset``.

    Used as the evidence window a human reviews when confirming a treatment
    edge, and as stored context on every citation.
    """
    if not text:
        return ""
    for start, end in _sentence_bounds(text):
        if start <= offset < end:
            return text[start:end].strip()
    return text[-200:].strip()


def pin_cite(text: str, offset: int) -> int | None:
    """Nearest following "at [n]" after ``offset``, if any."""
    m = _PIN_RE.search(text, offset)
    if m and m.start() - offset < 60:
        return int(m.group("first"))
    return None


def _overlaps(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return a[0] < b[1] and b[0] < a[1]


def normalise_citation_key(
    court: Court | None, year: int | None, number: int | None
) -> str | None:
    if court and year and number:
        return f"{court.value}:{year}:{number}"
    return None
