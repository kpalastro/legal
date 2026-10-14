"""Domain model.

The whole system is built around one invariant, and this module is where it is
enforced:

    Nothing is asserted about the law that is not anchored to a span of an
    existing judgment or a provision of a statute, and nothing is encoded as a
    machine-evaluable rule that does not carry a provenance chain back to the
    expert who approved it.

Every type below exists to make that traceable. ``Provenance`` is not
decoration -- it is the join between the knowledge base (what the courts
actually said) and the reasoning layer (what we tell a machine to conclude).

See docs/02-judgment-knowledge-base.md for why paragraphs are the unit of
citation, and docs/04-rule-encoding.md for the rule/provenance design.
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime
from enum import Enum
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

# --------------------------------------------------------------------------
# Enumerations
# --------------------------------------------------------------------------


class Court(str, Enum):
    """Court hierarchy. ``rank`` drives authority filtering in retrieval.

    Court codes are what appear in the neutral citation, so they are the codes
    the High Court, Federal Court and state courts actually publish. Note that
    these are *not* all-caps: the Federal Circuit and Family Court of Australia
    uses ``FedCFamC1A`` and ``FedCFamC2G``. A parser that assumes ``[A-Z]+``
    silently drops every citation to those courts.
    """

    HCA = "HCA"  # High Court of Australia
    FCAFC = "FCAFC"  # Full Court, Federal Court
    FCA = "FCA"  # Federal Court (single judge)
    FedCFamC1A = "FedCFamC1A"  # FCFCOA (Div 1) Appellate
    FedCFamC1F = "FedCFamC1F"  # FCFCOA (Div 1)
    FedCFamC2G = "FedCFamC2G"  # FCFCOA (Div 2)
    NSWCA = "NSWCA"
    NSWSC = "NSWSC"
    VSCA = "VSCA"
    VSC = "VSC"
    QCA = "QCA"
    QSC = "QSC"
    WACA = "WACA"
    WASCA = "WASCA"
    SASC = "SASC"
    SASCA = "SASCA"
    TASSC = "TASSC"
    NTCA = "NTCA"
    ACTCA = "ACTCA"
    ART = "ART"  # Administrative Review Tribunal (replaced the AAT in 2024)
    AAT = "AAT"  # Administrative Appeals Tribunal (historical)
    OTHER = "OTHER"

    @property
    def rank(self) -> int:
        """Higher binds harder. Used to rank and to filter authorities."""
        return _COURT_RANK[self]


_COURT_RANK: dict[Court, int] = {
    Court.HCA: 100,
    Court.FCAFC: 80,
    Court.FedCFamC1A: 72,
    Court.NSWCA: 70,
    Court.VSCA: 70,
    Court.QCA: 70,
    Court.WACA: 70,
    Court.WASCA: 70,
    Court.SASCA: 70,
    Court.ACTCA: 65,
    Court.NTCA: 65,
    Court.TASSC: 65,
    Court.FCA: 60,
    Court.FedCFamC1F: 60,
    Court.NSWSC: 55,
    Court.VSC: 55,
    Court.QSC: 55,
    Court.SASC: 55,
    Court.FedCFamC2G: 35,
    Court.ART: 30,
    Court.AAT: 30,
    Court.OTHER: 10,
}


class Jurisdiction(str, Enum):
    CTH = "Cth"
    NSW = "NSW"
    VIC = "Vic"
    QLD = "Qld"
    WA = "WA"
    SA = "SA"
    TAS = "Tas"
    NT = "NT"
    ACT = "ACT"
    NZ = "NZ"
    UK = "UK"
    OTHER = "Other"


class Treatment(str, Enum):
    """How a later judgment treated an earlier one.

    This is the difference between a search engine and a citator. Retrieving a
    case that supports you is worthless if a Full Court later overruled it.
    Ordering here is roughly by force; ``is_negative`` drives the authority
    status computation in :mod:`ratio.kb.graph`.
    """

    OVERRULED = "overruled"
    NOT_FOLLOWED = "not_followed"
    DEPARTED_FROM = "departed_from"
    DOUBTED = "doubted"
    DISTINGUISHED = "distinguished"
    CONSIDERED = "considered"
    REFERRED_TO = "referred_to"
    APPROVED = "approved"
    APPLIED = "applied"
    FOLLOWED = "followed"

    @property
    def is_negative(self) -> bool:
        return self in {
            Treatment.OVERRULED,
            Treatment.NOT_FOLLOWED,
            Treatment.DEPARTED_FROM,
            Treatment.DOUBTED,
            Treatment.DISTINGUISHED,
        }

    @property
    def is_positive(self) -> bool:
        return self in {
            Treatment.APPROVED,
            Treatment.APPLIED,
            Treatment.FOLLOWED,
        }


class AuthorityStatus(str, Enum):
    """Computed, not authored. The output of the treatment graph."""

    GOOD_LAW = "good_law"
    DISTINGUISHED_ON_FACTS = "distinguished_on_facts"
    QUESTIONED = "questioned"
    OVERRULED = "overruled"
    SUPERSEDED_BY_STATUTE = "superseded_by_statute"
    UNCERTAIN = "uncertain"  # no treatment edges, or conflicting edges


class SourceKind(str, Enum):
    AUSTLII = "austlii"
    FEDERAL_COURT = "federal_court"
    HIGH_COURT = "high_court"
    JADE = "jade"
    MANUAL = "manual"
    SYNTHETIC = "synthetic"


# --------------------------------------------------------------------------
# Corpus layer: judgments and their structure
# --------------------------------------------------------------------------


class SourceRef(BaseModel):
    """Where a document came from, and under what licence.

    Kept on every document because a regulated system has to be able to say
    where each byte came from and when. See docs/11-governance-assurance.md.
    """

    model_config = ConfigDict(frozen=True)

    kind: SourceKind
    url: str | None = None
    retrieved_at: datetime | None = None
    licence: str | None = None
    checksum: str | None = None  # sha256 of the raw source bytes

    @staticmethod
    def checksum_of(raw: bytes) -> str:
        return hashlib.sha256(raw).hexdigest()


class ReasonsRole(str, Enum):
    """A judge's relationship to the disposition.

    Recorded because precedent weight differs: a proposition in a dissent is
    not authority, and a proposition in a concurrence may rest on narrower
    reasoning than the majority's. A system that extracts a "ratio" without
    knowing which reasons it came from will eventually quote a dissent as law.
    """

    SOLE = "sole"  # single-judge decision
    MAJORITY = "majority"
    CONCURRING = "concurring"
    DISSENT = "dissent"
    UNKNOWN = "unknown"


class Paragraph(BaseModel):
    """The unit of citation.

    Australian judgments are numbered paragraph by paragraph and every
    practitioner cites by that number ("at [42]"). Chunking a judgment into
    fixed-size token windows -- the default in generic RAG -- destroys the
    anchor that makes a citation checkable, so we never do it. See
    docs/02-judgment-knowledge-base.md.

    The anchor is scoped by judge, because separate reasons each restart
    numbering at [1]. In a three-judge Full Court, "at [42]" names three
    different paragraphs depending on whose reasons are meant. A paragraph
    identity of ``(judgment, judge, number)`` is therefore the only correct one,
    and :meth:`Judgment.ambiguous_anchor` exists to catch the case where a
    citation we have parsed does not say whose reasons it means.
    """

    model_config = ConfigDict(frozen=True)

    number: int
    text: str
    # Character offsets into the normalised full text of the judgment. These are
    # what makes span verification possible: a quote is only accepted if it
    # occurs at the claimed offsets.
    char_start: int
    char_end: int
    heading: str | None = None  # nearest preceding section heading, if any
    is_indented_quote: bool = False  # block quotation of another source
    # Whose reasons this paragraph belongs to. None means the judgment is
    # single-authored and unambiguous.
    judge: str | None = None
    reasons_role: ReasonsRole = ReasonsRole.UNKNOWN

    @property
    def anchor(self) -> str:
        return f"[{self.number}]"

    @property
    def scoped_anchor(self) -> str:
        """Unambiguous anchor, e.g. ``[42] (Bell J)``."""
        if self.judge is None or self.reasons_role is ReasonsRole.SOLE:
            return f"[{self.number}]"
        return f"[{self.number}] ({self.judge})"

    @property
    def identity(self) -> tuple[str | None, int]:
        return (self.judge, self.number)


class ParallelCitation(BaseModel):
    """An alternative citation form for the same judgment.

    Australian practice (AGLC4 r 2.2.7) is to cite one version only, the most
    authoritative report. The knowledge base must nonetheless store every form
    and collapse them to a single canonical case, because otherwise the same
    case enters the citation graph as several distinct nodes and its treatment
    history fragments -- which is exactly how a citator ends up reporting
    "good law" for a case that has been overruled under a different report
    citation.
    """

    model_config = ConfigDict(frozen=True)

    raw: str
    kind: CitationKind
    is_authorised_report: bool = False  # CLR, FCR, NSWLR, VR, Qd R, ...


class CitationKind(str, Enum):
    NEUTRAL = "neutral"  # [2026] FCA 123
    REPORTED = "reported"  # (2026) 300 FCR 1 / (2026) 345 ALR 12
    STATUTORY = "statutory"  # Foreign Acquisitions and Takeovers Act 1975 (Cth) s 18
    OTHER = "other"  # unreported, transcript, extrinsic material


class Citation(BaseModel):
    """A reference found in a judgment's text, with its position preserved."""

    model_config = ConfigDict(frozen=True)

    raw: str
    kind: CitationKind
    # Parsed components, when the pattern is recognised.
    court: Court | None = None
    year: int | None = None
    number: int | None = None
    jurisdiction: Jurisdiction | None = None
    # For statutory references.
    act_title: str | None = None
    act_year: int | None = None
    section: str | None = None
    # Position within the containing paragraph.
    span_start: int
    span_end: int
    treatment: Treatment | None = None
    # The exact phrase that drove the classification, and how far it was from
    # this citation. Kept so an expert reviewing a proposed treatment edge sees
    # the words that caused it, not just the label.
    treatment_phrase: str | None = None
    treatment_phrase_span: tuple[int, int] | None = None
    treatment_confidence: float | None = None
    context: str = ""  # the sentence containing the reference
    # The paragraph this reference appears in, and whose reasons it belongs to.
    paragraph: int | None = None
    judge: str | None = None
    # A pinpoint to a paragraph of the *cited* judgment: "[2020] HCA 5 at [42]".
    pin_cite: int | None = None

    @property
    def target_key(self) -> str | None:
        """Stable key for graph resolution, or None if unresolvable.

        Returning None is a first-class outcome: an unresolvable reference is
        recorded as such and never silently dropped, because the unresolved set
        is exactly the set of authorities whose treatment we cannot see.
        """
        if (
            self.kind is CitationKind.NEUTRAL
            and self.year
            and self.number
            and self.court
        ):
            return f"{self.court.value}:{self.year}:{self.number}"
        if (
            self.kind is CitationKind.STATUTORY
            and self.act_title
            and self.act_year
            and self.section
        ):
            return f"statute:{self.act_title} {self.act_year}:{self.section}"
        return None

    @property
    def is_resolvable(self) -> bool:
        return self.target_key is not None


class Judgment(BaseModel):
    """A judgment, structured but not interpreted.

    No field here is an inference about the law. Structure yes; meaning no.
    Meaning is added by the reasoning layer, with provenance.
    """

    model_config = ConfigDict(frozen=True)

    id: str  # e.g. "au/fca/2026/00123"
    neutral_citation: str
    parallel_citations: tuple[ParallelCitation, ...] = ()
    title: str
    court: Court
    jurisdiction: Jurisdiction
    date_decided: date
    judges: tuple[str, ...] = ()
    catchwords: tuple[str, ...] = ()
    headnote: str | None = None
    paragraphs: tuple[Paragraph, ...] = ()
    orders: tuple[str, ...] = ()
    source: SourceRef
    synthetic: bool = False  # fixtures are flagged, and surfaced in every output

    @property
    def paragraph_numbers(self) -> set[int]:
        return {p.number for p in self.paragraphs}

    def paragraph(self, number: int, judge: str | None = None) -> Paragraph | None:
        """Resolve a paragraph anchor.

        With ``judge`` given, the lookup is unambiguous. Without it, we return
        the paragraph only when doing so is safe -- see
        :meth:`ambiguous_anchor`.
        """
        matches = [p for p in self.paragraphs if p.number == number]
        if judge is not None:
            return next((p for p in matches if p.judge == judge), None)
        if not matches:
            return None
        if len(matches) > 1:
            return None  # refuse to guess between judges' reasons
        return matches[0]

    def ambiguous_anchor(self, number: int) -> tuple[str | None, ...] | None:
        """Judges whose reasons contain ``number``, or None if unambiguous.

        A non-None return is a warning to the caller: this anchor cannot be
        resolved without knowing whose reasons are meant, and resolving it
        arbitrarily is how a system cites a dissent as though it were the
        majority.
        """
        judges = tuple(p.judge for p in self.paragraphs if p.number == number)
        return judges if len(judges) > 1 else None

    @property
    def full_text(self) -> str:
        return "\n\n".join(p.text for p in self.paragraphs)

    def span_is_exact(
        self,
        number: int,
        start: int,
        end: int,
        quote: str,
        *,
        judge: str | None = None,
    ) -> bool:
        """Is ``quote`` genuinely present at the claimed offsets?

        This is the check that makes hallucinated citations structurally
        impossible to emit: a claimed quote either matches the source bytes at
        the claimed position or it does not.
        """
        para = self.paragraph(number, judge)
        if para is None:
            return False
        return para.text[start:end] == quote

    def canonical_key(self) -> str | None:
        """Graph key derived from the neutral citation."""
        import re

        m = re.match(
            r"\[\s*(\d{4})\s*\]\s*([A-Za-z][A-Za-z0-9]*)\s+(\d{1,5})",
            self.neutral_citation,
        )
        if m:
            return f"{m.group(2)}:{m.group(1)}:{m.group(3)}"
        return None



class StatutoryProvision(BaseModel):
    """A provision of an Act, at a point in time.

    Point-in-time matters: an encoded rule is only valid against the compiled
    text that was in force when it was approved. See docs/04-rule-encoding.md
    on drift.
    """

    model_config = ConfigDict(frozen=True)

    act_title: str
    act_year: int
    jurisdiction: Jurisdiction
    section: str
    heading: str | None = None
    text: str
    compiled_as_at: date  # "as at" date of the compilation relied on
    source: SourceRef

    @property
    def key(self) -> str:
        return f"statute:{self.act_title}:{self.section}"


# --------------------------------------------------------------------------
# Precedent layer: what a judgment means for other judgments
# --------------------------------------------------------------------------


class TreatmentEdge(BaseModel):
    """A later court's treatment of an earlier authority.

    Derived by parsing the citing sentence (see :mod:`ratio.citations`), then
    reviewable by a lawyer. The ``confirmed_by`` field is the escape hatch: a
    machine-proposed treatment edge is advisory until a human signs it.
    """

    model_config = ConfigDict(frozen=True)

    citing_judgment: str  # judgment id
    citing_paragraph: int
    cited_key: str  # Citation.target_key
    treatment: Treatment
    quote: str  # the sentence evidencing the treatment
    confidence: float = Field(ge=0.0, le=1.0, default=0.5)
    confirmed_by: str | None = None  # SME identifier, if reviewed
    # How the edge was found. A treatment attached to a neutral citation is
    # stronger evidence than one attached to a case *name* resolved through the
    # corpus-derived alias table, and a lawyer reviewing the edge is entitled to
    # know which they are looking at.
    via: Literal["citation_token", "name_reference"] = "citation_token"

    @property
    def is_negative(self) -> bool:
        return self.treatment.is_negative

    @property
    def needs_review(self) -> bool:
        """Whether a human should look at this before it changes a status.

        Two independent reasons: the phrase was weak or bound at a distance, or
        the edge rests on a resolved name rather than a citation.
        """
        return self.confidence < 0.5 or self.via == "name_reference"


class AuthorityStatusRecord(BaseModel):
    """The computed answer to "is this still good law?"."""

    target_key: str
    status: AuthorityStatus
    evidence: tuple[TreatmentEdge, ...] = ()
    # Treatment edges that were recorded but whose confidence is below the
    # advisory threshold, so they did not affect ``status``. Kept because they are
    # exactly what a reviewing lawyer needs to see: the machine's uncertain
    # readings, in the words that produced them.
    advisory_evidence: tuple[TreatmentEdge, ...] = ()
    # Treatment edges that were recorded but which the citing court has no power
    # to give effect to -- a subordinate court purporting to overrule or doubt a
    # superior one. These did not affect ``status`` and, unlike the advisory
    # bucket, reviewing them will not change that: the gate is the hierarchy, not
    # the reader's confidence in the binding.
    #
    # They are kept for a different reason. A subordinate court's refusal to
    # follow a decision is real information -- it tells a lawyer that the
    # authority will be contested in that court, and it is the first thing to
    # check when the point comes up in that jurisdiction. Reporting "no treatment
    # recorded" in such a case is not a conservative answer, it is a wrong one:
    # the corpus contains the treatment and we chose not to show it.
    hierarchy_blocked: tuple[TreatmentEdge, ...] = ()
    computed_at: datetime
    # What the system will tell a user to do about it.
    caveat: str | None = None


class RatioProposition(BaseModel):
    """A proposition of law extracted from a judgment, as ratio or obiter.

    This is the pivot between the knowledge base and the rulebook. A rule node
    cites one or more of these; each of these cites a paragraph span; the
    paragraph span is verifiable. Whether the proposition is *ratio* (binding)
    or *obiter* (persuasive) is a legal judgement, so it is an SME field, not
    something the system infers on its own -- it may only propose.
    """

    id: str
    judgment_id: str
    paragraph: int
    quote: str
    span_start: int
    span_end: int
    statement: str  # plain-English restatement
    kind: Literal["ratio", "obiter", "proposed_ratio", "proposed_obiter"] = (
        "proposed_ratio"
    )
    confirmed_by: str | None = None
    confirmed_at: datetime | None = None
    issue_tags: tuple[str, ...] = ()

    @property
    def is_confirmed(self) -> bool:
        return self.confirmed_by is not None and self.kind in {"ratio", "obiter"}


# --------------------------------------------------------------------------
# Reasoning layer: rules, provenance, credence
# --------------------------------------------------------------------------


class AuthorityRef(BaseModel):
    """A pointer from an encoded artefact to the authority that justifies it.

    Deliberately narrow: either a paragraph of a judgment, a statutory
    provision, or an expert's own statement. There is no "because the model
    said so".
    """

    model_config = ConfigDict(frozen=True)

    kind: Literal["judgment_paragraph", "statute", "expert_statement"]
    judgment_id: str | None = None
    paragraph: int | None = None
    provision_key: str | None = None
    proposition_id: str | None = None
    quote: str | None = None
    note: str | None = None

    @model_validator(mode="after")
    def _check_shape(self) -> Self:
        if self.kind == "judgment_paragraph" and not (
            self.judgment_id and self.paragraph is not None
        ):
            raise ValueError("judgment_paragraph ref requires judgment_id and paragraph")
        if self.kind == "statute" and not self.provision_key:
            raise ValueError("statute ref requires provision_key")
        return self


class Approval(BaseModel):
    """An expert's sign-off on an encoded artefact.

    The signature is a hash over the artefact's canonical form, so that editing
    the artefact after approval invalidates the approval rather than silently
    inheriting it. This is the mechanism behind "experts can inspect and govern
    the systems they use".
    """

    approver: str
    role: str  # e.g. "Partner, M&A", "Special Counsel, Competition"
    approved_at: datetime
    artefact_hash: str
    note: str | None = None

    @staticmethod
    def hash_of(payload: str) -> str:
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class NodeKind(str, Enum):
    ALL_OF = "all_of"
    ANY_OF = "any_of"
    NOT = "not"
    LEAF = "leaf"  # a factual predicate over case facts
    DELEGATE_BN = "delegate_bn"  # hand off to the probabilistic layer
    EXCEPTION = "exception"  # a defeater, not a conjunct


class Predicate(BaseModel):
    """A test against the fact base. Kept deliberately primitive.

    Only comparisons, no computation: a rule an expert cannot read is a rule an
    expert cannot govern.
    """

    field: str
    op: Literal["eq", "ne", "lt", "le", "gt", "ge", "in", "not_in", "exists", "matches"]
    value: str | float | int | bool | list[str] | None = None


class RuleNode(BaseModel):
    """One node of a defeasible rule tree.

    Defeasibility is expressed by EXCEPTION children: they do not need to be
    true for the rule to apply, but if one *is* true and fires, it defeats the
    conclusion. This is how legal rules actually behave ("...unless the
    Minister is satisfied that..."), and encoding exceptions as ordinary
    conjuncts is the most common way computational-law projects encode
    something that is not the law.
    """

    id: str
    kind: NodeKind
    label: str  # short label for the expert UI
    statement: str  # plain-English proposition
    children: tuple[str, ...] = ()  # node ids
    predicate: Predicate | None = None
    bn_query: str | None = None  # for DELEGATE_BN: which credence question
    bn_threshold: float | None = None
    provenance: tuple[AuthorityRef, ...] = ()
    # Exceptions are checked after the normal children, and a firing exception
    # defeats the conclusion regardless of the normal children.
    exception_of: str | None = None

    @model_validator(mode="after")
    def _check_shape(self) -> Self:
        if self.kind is NodeKind.LEAF and self.predicate is None:
            raise ValueError(f"leaf node {self.id!r} requires a predicate")
        if self.kind is NodeKind.DELEGATE_BN and not self.bn_query:
            raise ValueError(f"delegate_bn node {self.id!r} requires bn_query")
        # An exception is either a predicate in its own right -- the common case,
        # "...unless the Minister has granted an exemption certificate" -- or a
        # subtree, where the defeating condition has several elements.
        if self.kind is NodeKind.EXCEPTION and self.predicate is None and not self.children:
            raise ValueError(
                f"exception node {self.id!r} requires either a predicate or children"
            )
        if (
            self.kind not in {NodeKind.LEAF, NodeKind.EXCEPTION}
            and not self.children
        ):
            raise ValueError(f"node {self.id!r} of kind {self.kind} requires children")
        return self


class RuleSet(BaseModel):
    """A versioned, expert-approved encoding of a legal test."""

    id: str
    title: str
    question: str  # the legal question this answers
    domain: str  # e.g. "firb", "continuous_disclosure"
    version: str
    effective_from: date
    effective_to: date | None = None
    root: str  # node id
    nodes: tuple[RuleNode, ...] = ()
    approvals: tuple[Approval, ...] = ()
    provision_keys: tuple[str, ...] = ()
    status: Literal["draft", "in_review", "approved", "retired"] = "draft"
    notes: str | None = None

    def node(self, node_id: str) -> RuleNode | None:
        for n in self.nodes:
            if n.id == node_id:
                return n
        return None

    def canonical_payload(self) -> str:
        """Deterministic serialisation used for approval hashing."""
        payload = self.model_dump(
            mode="json",
            exclude={"approvals"},
        )
        return _canonical_json(payload)

    @property
    def current_approval(self) -> Approval | None:
        """The approval that matches this exact revision, if any."""
        h = Approval.hash_of(self.canonical_payload())
        for a in self.approvals:
            if a.artefact_hash == h:
                return a
        return None

    @property
    def approval_digest(self) -> str:
        """The hash an approval of this revision has to carry.

        *Not* the hash of the revision as it stands. ``canonical_payload``
        covers ``status``, and signing off is precisely the act of changing
        ``status`` from ``draft`` to ``approved`` -- so a digest taken before
        that change can never match the revision afterwards. An approval stamped
        with one makes :attr:`is_approved` false, and makes
        :func:`ratio.rules.engine.validate_ruleset` report the ruleset as edited
        since sign-off, which in turn makes :class:`~ratio.rules.engine.RuleEngine`
        refuse to load it.

        That was the shipped behaviour: ``ratio rules approve`` printed a digest
        taken over the revision as it stood and then instructed the reader to set
        ``status: approved``, and doing exactly that produced a rulebook that
        could not be evaluated at all. The failure is invisible until someone
        actually approves something, and nothing had.

        An expert can only be shown the digest their signature will be checked
        against, so it has to be computed over the payload this revision will
        have once it is approved.
        """
        approved = self.model_copy(update={"status": "approved"})
        return Approval.hash_of(approved.canonical_payload())

    @property
    def is_approved(self) -> bool:
        return self.status == "approved" and self.current_approval is not None


class CredenceState(BaseModel):
    """A named state of a discrete variable in the credence network."""

    name: str
    label: str
    # Free-text anchor for what this state means in law, and where it comes from.
    definition: str | None = None


class CredenceNode(BaseModel):
    """A discrete variable, usually a legal factor or an intermediate finding."""

    id: str
    label: str
    question: str  # phrased for an expert, not for a statistician
    states: tuple[CredenceState, ...]
    parents: tuple[str, ...] = ()
    # Rows are parent configurations in the order produced by itertools.product
    # over parent states; columns are this node's states. Flattened for JSON.
    cpt: tuple[tuple[float, ...], ...] = ()
    role: Literal["factor", "intermediate", "target"] = "factor"

    @model_validator(mode="after")
    def _check_cpt(self) -> Self:
        if self.cpt:
            for row in self.cpt:
                if len(row) != len(self.states):
                    raise ValueError(
                        f"cpt row width {len(row)} != {len(self.states)} states on {self.id!r}"
                    )
                if abs(sum(row) - 1.0) > 1e-6:
                    raise ValueError(f"cpt row on {self.id!r} does not sum to 1")
        return self


class Elicitation(BaseModel):
    """Provenance for one elicited distribution: who said what.

    One record per parent configuration, holding the whole distribution, because
    that is how elicitation actually happens -- you ask an expert "given these
    features, how likely is each outcome", not "what is the third component of
    a vector".

    The important property is that the CPT row and the elicitation record must
    *agree*: :class:`ratio.bn.network.CredenceEngine` refuses to load a network
    whose numbers differ from what the named expert stated. This is what stops a
    number being quietly tuned during model development and keeping the
    provenance of the original workshop. A number in a legal model that cannot be
    traced to a named expert's stated view is not evidence; it is an opinion
    laundered through arithmetic.
    """

    node_id: str
    # Parent states in the order the node declares its parents. Empty for a root
    # node.
    parent_config: tuple[str, ...] = ()
    distribution: dict[str, float]
    method: Literal[
        "direct_probability",
        "likelihood_ratio",
        "frequency",
        "expert_consensus",
        "fit_from_cases",
    ]
    elicited_from: str
    role: str
    elicited_at: datetime
    raw_response: str
    rationale: str | None = None

    @property
    def key(self) -> tuple[str, tuple[str, ...]]:
        return (self.node_id, self.parent_config)



class CredenceNetwork(BaseModel):
    """A Bayesian network over legal factors, for standards text cannot settle.

    Used where a rule genuinely cannot go: "substantial lessening of
    competition", "contrary to the national interest", "reasonably
    practicable". The design rule is that this layer never outputs a verdict --
    it outputs a posterior, the sensitivities driving it, and the authorities
    behind each factor, so a lawyer can disagree with the inputs rather than
    argue with a black box. See docs/05-bayesian-layer.md.
    """

    id: str
    title: str
    question: str
    domain: str
    version: str
    nodes: tuple[CredenceNode, ...] = ()
    target: str  # node id whose posterior we report
    elicitations: tuple[Elicitation, ...] = ()
    approvals: tuple[Approval, ...] = ()
    provision_keys: tuple[str, ...] = ()
    status: Literal["draft", "in_review", "approved", "retired"] = "draft"
    # Text that must be shown wherever a posterior from this network is shown.
    # Not optional in spirit: a number from a legal model without its
    # interpretation caveat attached is a liability.
    caveats: dict[str, str] = Field(default_factory=dict)

    def node(self, node_id: str) -> CredenceNode | None:
        for n in self.nodes:
            if n.id == node_id:
                return n
        return None

    @model_validator(mode="after")
    def _check_acyclic_and_parents(self) -> Self:
        ids = [n.id for n in self.nodes]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate node ids in credence network")
        known = set(ids)
        for n in self.nodes:
            missing = [p for p in n.parents if p not in known]
            if missing:
                raise ValueError(f"node {n.id!r} has unknown parents {missing}")
        if ids and self.target not in known:
            raise ValueError(f"target {self.target!r} is not a node")
        _topological_order(self.nodes)  # raises on cycles
        return self


# --------------------------------------------------------------------------
# Output layer
# --------------------------------------------------------------------------


class GroundedQuote(BaseModel):
    """A quote that has survived span verification.

    Constructed only by :mod:`ratio.verify.grounding`. If a model produces a
    quote, it enters the system as an unverified claim and is upgraded to a
    GroundedQuote only by exact match.
    """

    model_config = ConfigDict(frozen=True)

    judgment_id: str
    neutral_citation: str
    paragraph: int
    quote: str
    span_start: int
    span_end: int
    verified_at: datetime


class Finding(BaseModel):
    """One step of a proof, from the rule engine."""

    node_id: str
    label: str
    outcome: Literal["satisfied", "not_satisfied", "defeated", "unknown"]
    statement: str
    predicate: Predicate | None = None
    observed: str | None = None  # what the fact base actually had
    children: tuple[str, ...] = ()
    provenance: tuple[AuthorityRef, ...] = ()
    note: str | None = None


class ProofTree(BaseModel):
    """The whole derivation, for display and for audit."""

    ruleset_id: str
    ruleset_version: str
    conclusion: Literal["applies", "does_not_apply", "undetermined"]
    findings: tuple[Finding, ...] = ()
    defeaters_fired: tuple[str, ...] = ()
    gaps: tuple[str, ...] = ()  # facts needed but absent
    generated_at: datetime

    def render(self, indent: int = 0) -> str:
        """Plain-text proof, for CLI and for the audit ledger."""
        glyph = {
            "satisfied": "[+]",
            "not_satisfied": "[-]",
            "defeated": "[x]",
            "unknown": "[?]",
        }
        lines: list[str] = []
        pad = "  " * indent
        for f in self.findings:
            lines.append(f"{pad}{glyph[f.outcome]} {f.label}: {f.statement}")
            if f.observed is not None:
                lines.append(f"{pad}    observed: {f.observed}")
            if f.note:
                lines.append(f"{pad}    note: {f.note}")
        for g in self.gaps:
            lines.append(f"{pad}    gap: {g}")
        return "\n".join(lines)


class PosteriorReport(BaseModel):
    """What the credence layer returns. Never a yes/no."""

    network_id: str
    network_version: str
    target: str
    target_label: str
    distribution: dict[str, float]
    evidence: dict[str, str]
    # Ranked influence of each observed factor on the target, one at a time.
    sensitivities: tuple[tuple[str, float], ...] = ()
    authorities: tuple[AuthorityRef, ...] = ()
    caveat: str


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _canonical_json(value: object) -> str:
    import json

    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _topological_order(nodes: tuple[CredenceNode, ...]) -> list[str]:
    """Kahn's algorithm; raises ValueError on a cycle."""
    by_id = {n.id: n for n in nodes}
    indegree = {n.id: 0 for n in nodes}
    for n in nodes:
        for p in n.parents:
            indegree[n.id] += 1
    queue = [i for i, d in indegree.items() if d == 0]
    order: list[str] = []
    while queue:
        current = queue.pop(0)
        order.append(current)
        for n in nodes:
            if current in n.parents:
                indegree[n.id] -= 1
                if indegree[n.id] == 0:
                    queue.append(n.id)
    if len(order) != len(by_id):
        raise ValueError("credence network contains a cycle")
    return order


def topological_order(nodes: tuple[CredenceNode, ...]) -> list[str]:
    """Public wrapper used by the inference engine."""
    return _topological_order(nodes)


# A citation span extracted from prose, used across verify/ and retrieve/.
AnnotatedSpan = Annotated[tuple[int, int], Field(description="char offsets [start, end)")]
