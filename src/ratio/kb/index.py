"""The knowledge base: corpus in, queryable structure out.

This module is the join between the corpus layer (judgments, provisions) and
everything that reasons over them. It builds, from a list of judgments:

  * the **citation graph**, with treatment edges derived from the prose;
  * a **paragraph index** for retrieval, carrying the metadata that filters
    need (court, jurisdiction, date, whose reasons);
  * the **verifier**, bound to the same corpus, so that a quote can only be
    grounded against the text the retriever actually returned.

Keeping those three views over one corpus in one place matters. When retrieval
and verification are built from different snapshots, the verifier will start
rejecting quotes from documents the retriever is still serving, and the usual
response is to loosen the verifier -- which is precisely backwards.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from ratio.citations import CitationParser, sentences_share
from ratio.ingest.fixtures import CorpusBundle
from ratio.kb.aliases import AliasReport, CaseAliasResolver
from ratio.kb.graph import CitationGraph
from ratio.model import (
    Citation,
    CitationKind,
    Judgment,
    StatutoryProvision,
    TreatmentEdge,
)
from ratio.retrieve.bm25 import BM25Index
from ratio.verify.grounding import Verifier


@dataclass
class ParagraphRef:
    """A retrievable unit: one paragraph, with the metadata filters need."""

    doc_id: str  # "{judgment_id}#{judge}#{number}"
    judgment_id: str
    neutral_citation: str
    paragraph: int
    judge: str | None
    reasons_role: str
    court: str
    jurisdiction: str
    date_decided: str
    heading: str | None
    text: str
    synthetic: bool

    @property
    def anchor(self) -> str:
        suffix = f" ({self.judge})" if self.judge else ""
        return f"[{self.paragraph}]{suffix}"

    @property
    def citation_label(self) -> str:
        return f"{self.neutral_citation} at {self.anchor}"


@dataclass
class KnowledgeBase:
    judgments: dict[str, Judgment] = field(default_factory=dict)
    provisions: dict[str, StatutoryProvision] = field(default_factory=dict)
    graph: CitationGraph = field(default_factory=CitationGraph)
    index: BM25Index = field(default_factory=BM25Index)
    refs: dict[str, ParagraphRef] = field(default_factory=dict)
    verifier: Verifier | None = None
    unresolved_authorities: set[str] = field(default_factory=set)
    # Which short case names the corpus evidenced, and what they resolved to.
    aliases: dict[str, str] = field(default_factory=dict)
    alias_report: AliasReport | None = None
    warnings: list[str] = field(default_factory=list)

    # -- construction ------------------------------------------------------

    @classmethod
    def build(cls, bundle: CorpusBundle) -> KnowledgeBase:
        kb = cls()
        parser = CitationParser()

        for provision in bundle.provisions:
            kb.provisions[provision.key] = provision
            kb.graph.register_statute(
                provision.key, f"{provision.act_title} s {provision.section}"
            )

        for judgment in bundle.judgments:
            kb.judgments[judgment.id] = judgment
            kb.graph.register_judgment(judgment)
            report = bundle.parse_reports.get(judgment.id)
            if report is not None and not report.is_clean:
                kb.warnings.append(
                    f"{judgment.neutral_citation}: parse warnings ({report.summary()})"
                )

        kb._build_index()
        aliases = kb._mine_case_names(parser)
        kb._build_edges(parser, aliases)
        kb.verifier = Verifier(kb.judgments, graph=kb.graph)
        kb.unresolved_authorities = set(kb.graph.unresolved_citations())
        return kb

    # -- stage 1: the case-name alias table --------------------------------

    def _mine_case_names(self, parser: CitationParser) -> CaseAliasResolver:
        """Settle the name-to-authority table before any edge is built.

        Two passes over the corpus, in this order because the second depends on
        the first:

          1. candidates come from case titles, restricted to distinctive party
             names -- every judgment in the fixture corpus is "X v Minister for
             Foreign Investment", so a rule that took the first party's first
             token regardless would work here and map six cases onto "Minister"
             elsewhere;
          2. candidates are then *evidenced* against the corpus: a name is bound
             to an authority only where the corpus itself puts the name next to
             that authority's citation, as in "the Full Court in Bramblewood
             Nominees Pty Ltd v Minister for Foreign Investment [2019] FCAFC 77".

        Nothing survives step 2 on the strength of step 1 alone. A name the corpus
        never ties to a citation is reported as unevidenced and resolves to
        nothing, because a name that has not been shown to denote an authority is
        just a proper noun.
        """
        resolver = CaseAliasResolver()
        for judgment in self.judgments.values():
            key = judgment.canonical_key()
            if key:
                resolver.add_title_candidates(judgment.title, key)

        # The evidence pass needs the candidate names before they are confirmed.
        resolver.build_candidate_pattern()

        for judgment in self.judgments.values():
            for paragraph in judgment.paragraphs:
                for citation in parser.extract(paragraph.text):
                    if citation.kind is CitationKind.STATUTORY or not citation.target_key:
                        continue
                    resolver.evidence_from_context(
                        paragraph.text,
                        (citation.span_start, citation.span_end),
                        citation.target_key,
                    )

        self.alias_report = resolver.confirm()
        self.aliases = resolver.resolved
        return resolver

    # -- stage 2: edges ----------------------------------------------------

    def _build_edges(self, parser: CitationParser, aliases: CaseAliasResolver) -> None:
        """One binding per paragraph, over citations *and* case names together.

        The union matters. Binding citations first and names afterwards gives
        every treatment phrase two chances to be spent -- once on a distant
        citation token, once on a nearby name -- and the phrase is then attached
        to both. That is the invented-edge failure arriving through the back door.
        A sentence such as "the reasoning of the High Court in Yarrow [2016] HCA
        12 is not capable of being confined ..., and I do not propose to follow
        Kingsford Quay" contains one treatment phrase and two candidate targets;
        it must produce one edge, to the nearer of them.

        Each paragraph is processed once. A judgment citing itself is skipped: it
        is the source of nothing, and a self-loop would make the authority status
        computation consider the judgment's own opinion of itself.
        """
        for judgment in self.judgments.values():
            citing_key = judgment.canonical_key()
            if not citing_key:
                continue
            for paragraph in judgment.paragraphs:
                text = paragraph.text
                citations = parser.extract(
                    text,
                    paragraph_number=paragraph.number,
                    judge=paragraph.judge,
                )

                targets: dict[int, tuple[int, int]] = {}
                citation_by_index: dict[int, Citation] = {}
                next_id = 0
                for citation in citations:
                    targets[next_id] = (citation.span_start, citation.span_end)
                    citation_by_index[next_id] = citation
                    next_id += 1

                name_by_index: dict[int, object] = {}
                for mention in aliases.names_in(text):
                    if any(
                        _spans_overlap(mention.span, span) for span in targets.values()
                    ):
                        continue  # already covered by a citation token
                    # A name and a citation token for the same authority in the same
                    # sentence are one reference, not two. Keeping both would give a
                    # treatment phrase two targets for one authority, and the phrase
                    # would be bound to the name -- losing the harder evidence and the
                    # confidence that goes with it. The name is dropped in favour of
                    # the token, never the other way round.
                    if any(
                        citation_by_index[i].target_key == mention.key
                        and sentences_share(text, citation_by_index[i].span_start, mention.span[0])
                        for i in citation_by_index
                    ):
                        continue
                    targets[next_id] = mention.span
                    name_by_index[next_id] = mention
                    next_id += 1

                bindings = CitationParser.bind_treatments(text, targets)

                # Records with no treatment, and treatment records. A bare
                # citation is still worth recording: the graph should show that
                # the court referred to the authority even where we could not
                # classify how.
                seen: set[tuple[str, str | None]] = set()
                for index in range(next_id):
                    binding = bindings.get(index)
                    if index in citation_by_index:
                        citation = citation_by_index[index]
                        cited = citation.target_key
                        if cited is None or cited == citing_key:
                            continue
                    else:
                        mention = name_by_index[index]
                        cited = mention.key  # type: ignore[attr-defined]
                        if cited == citing_key:
                            continue
                    dedupe = (cited, binding.treatment.value if binding else None)
                    if dedupe in seen:
                        continue
                    seen.add(dedupe)

                    if binding is None:
                        self.graph.add_reference(
                            citing_key, cited, paragraph.number
                        )
                        continue
                    via = (
                        "citation_token"
                        if index in citation_by_index
                        else "name_reference"
                    )
                    confidence = binding.confidence
                    if via == "name_reference":
                        # Damped: the phrase was bound to a name, and the step from
                        # name to authority is an inference even when the corpus
                        # supports it.
                        confidence = round(confidence * 0.8, 3)
                    self.graph.add_treatment(
                        TreatmentEdge(
                            citing_judgment=citing_key,
                            citing_paragraph=paragraph.number,
                            cited_key=cited,
                            treatment=binding.treatment,
                            quote=self._edge_quote(binding),
                            confidence=confidence,
                            via=via,
                        )
                    )

    @staticmethod
    def _edge_quote(binding) -> str:
        """The words that evidence the edge, plus why the binding is doubtful.

        A reviewer looking at an edge needs to see the phrase that drove the
        classification. Where the classification was made despite something
        working against it, they need to see what that was, in the record, rather
        than having it inferred from a confidence float.
        """
        notes = []
        if binding.anaphoric:
            notes.append(
                "the phrase governs a noun phrase from an earlier sentence, so "
                "the authority bound here may not be the one the court meant"
            )
        if binding.negated_by_submission:
            notes.append("the phrase appears in a party's submission, not a holding")
        if not notes:
            return binding.phrase
        return f'{binding.phrase}  [{" / ".join(notes)}]'

    def _build_index(self) -> None:
        docs: list[tuple[str, str, dict[str, str]]] = []
        for judgment in self.judgments.values():
            for paragraph in judgment.paragraphs:
                ref = ParagraphRef(
                    doc_id=_doc_id(judgment, paragraph.judge, paragraph.number),
                    judgment_id=judgment.id,
                    neutral_citation=judgment.neutral_citation,
                    paragraph=paragraph.number,
                    judge=paragraph.judge,
                    reasons_role=paragraph.reasons_role.value,
                    court=judgment.court.value,
                    jurisdiction=judgment.jurisdiction.value,
                    date_decided=judgment.date_decided.isoformat(),
                    heading=paragraph.heading,
                    text=paragraph.text,
                    synthetic=judgment.synthetic,
                )
                self.refs[ref.doc_id] = ref
                # Index the catchwords alongside the paragraph: they are the
                # court's own characterisation of the issues, and a query using
                # legal terms of art matches them far better than it matches the
                # body text.
                boosted = " ".join(
                    [paragraph.text, " ".join(judgment.catchwords)]
                )
                docs.append(
                    (
                        ref.doc_id,
                        boosted,
                        {
                            "court": ref.court,
                            "jurisdiction": ref.jurisdiction,
                            "date": ref.date_decided,
                            "judgment": ref.judgment_id,
                            "judge": ref.judge or "",
                        },
                    )
                )
        self.index.add_many(docs)

    # -- queries -----------------------------------------------------------

    def judgment_for_key(self, key: str) -> Judgment | None:
        for judgment in self.judgments.values():
            if judgment.canonical_key() == key:
                return judgment
        return None

    def resolve_authority(self, name: str) -> tuple[str | None, str | None]:
        """Resolve a case name or authority key to a canonical key.

        Returns ``(key, alias_used)``. Legal work is conducted in short case
        names -- no practitioner says "FCAFC:2019:77" -- so a system that only
        accepts neutral citations is unusable in the way that matters, and one
        that accepts names must say which case it took a name to mean. A name
        resolved silently is a name the reader cannot check, and mistaking one
        case for another is the failure mode this whole project is organised
        around.

        An exact key always wins over an alias, so that a corpus containing a
        case actually named "Yarrow" cannot be shadowed by an alias.
        """
        if self.judgment_for_key(name) is not None:
            return name, None
        key = self.aliases.get(name)
        if key is not None and self.judgment_for_key(key) is not None:
            return key, name
        # A case-insensitive fallback, reported as an alias so the caller still
        # shows what was matched.
        folded = name.strip().casefold()
        for alias, candidate in self.aliases.items():
            if alias.casefold() == folded and self.judgment_for_key(candidate):
                return candidate, alias
        return None, None

    def paragraph_context(self, ref: ParagraphRef, window: int = 2) -> str:
        """Expand a retrieved paragraph to its neighbours.

        Parent-document retrieval, in the retrieval literature's terms. A bare
        paragraph often omits the words that establish what the point is; a
        window of surrounding paragraphs restores it. We expand *after* ranking,
        so the expansion cannot distort the ranking.
        """
        judgment = self.judgments[ref.judgment_id]
        siblings = [p for p in judgment.paragraphs if p.judge == ref.judge]
        numbers = [p.number for p in siblings]
        if ref.paragraph not in numbers:
            return ref.text
        position = numbers.index(ref.paragraph)
        lo = max(0, position - window)
        hi = min(len(siblings), position + window + 1)
        return "\n\n".join(p.text for p in siblings[lo:hi])

    def authority_caveat(self, key: str, *, as_at: date | None = None) -> str:
        record = self.graph.authority_status(key, as_at=as_at)
        return record.caveat

    def stats(self) -> dict[str, object]:
        return {
            **self.graph.stats(),
            "paragraphs": len(self.refs),
            "provisions": len(self.provisions),
            "warnings": len(self.warnings),
        }


def _doc_id(judgment: Judgment, judge: str | None, number: int) -> str:
    return f"{judgment.id}#{judge or '_'}#{number}"


def _spans_overlap(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return a[0] < b[1] and b[0] < a[1]
