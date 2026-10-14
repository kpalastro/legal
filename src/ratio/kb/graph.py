"""The citation graph and authority status.

A judgment corpus is not a knowledge base until you can ask the two questions
that make precedent work:

  1. **What does this authority rest on, and what rests on it?** (the graph)
  2. **Is it still good law?** (treatment propagation)

The second question is where naive systems fail most expensively. Retrieval
returns the case that supports your argument; nobody notices that a Full Court
disapproved of it two years ago. So authority status is computed here, from
treatment edges, and *always* carries the evidence that produced it, so a lawyer
can look at the sentence that did the damage.

One subtlety that is easy to miss and expensive to get wrong: a treatment edge
only counts for as much as the citing court's position in the hierarchy
supports. A single judge of the Federal Court cannot overrule the High Court.
The graph encodes that, so a first-instance disagreement downgrades an
authority to "questioned" rather than "overruled".
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime

import networkx as nx

from ratio.model import (
    AuthorityStatus,
    AuthorityStatusRecord,
    Court,
    Judgment,
    Treatment,
    TreatmentEdge,
)

# Below this, a treatment edge is recorded but does not move an authority's
# status. Set at 0.5 because that is where the binder stops claiming a direct,
# in-sentence, unambiguous binding: 0.7 is a phrase bound within 40 characters of
# the authority, 0.5 within 120, and below that the phrase was bound at long range,
# inside a party's submission, to a name rather than a citation, or to a target it
# may not govern.
#
# The number matters less than the policy: an unreviewed machine reading must not
# be able to say an authority has been overruled. A citator that under-reports a
# negative treatment produces a footnote a lawyer will find on their own; one that
# over-reports invents a proposition that no court ever expressed, and does it in
# the register of authority.
#
# This gate applies in both directions, unlike the hierarchy gate above, and the
# reason is worth stating because it looks like an inconsistency. The hierarchy
# gate asks a question about the *court* -- who has power to move this law -- and
# that question only arises for downward movement. This gate asks a question
# about the *reading* -- is the phrase that produced this edge actually about this
# authority -- and a misread edge is misread whichever way it points.
ADVISORY_CONFIDENCE = 0.5


class CitationGraph:
    """Directed multigraph over authorities.
    Node keys are :attr:`ratio.model.Citation.target_key` values -- either
    ``"FCA:2024:812"`` for a judgment or ``"statute:Corporations Act 2001:674"``
    for a provision. Edges point from the citing judgment to the cited
    authority and carry a :class:`TreatmentEdge`.
    """

    def __init__(self) -> None:
        self.graph = nx.MultiDiGraph()
        self._judgments: dict[str, Judgment] = {}
        self._edges: dict[str, list[TreatmentEdge]] = defaultdict(list)
        self._known_keys: set[str] = set()

    # -- construction ------------------------------------------------------

    def register_judgment(self, judgment: Judgment) -> str:
        """Add the judgment as a node and record its metadata."""
        key = _judgment_key(judgment)
        self._judgments[key] = judgment
        self.graph.add_node(
            key,
            kind="judgment",
            title=judgment.title,
            court=judgment.court.value,
            date=judgment.date_decided.isoformat(),
            synthetic=judgment.synthetic,
        )
        self._known_keys.add(key)
        return key

    def register_statute(self, provision_key: str, title: str) -> None:
        self.graph.add_node(provision_key, kind="statute", title=title)
        self._known_keys.add(provision_key)

    def add_treatment(self, edge: TreatmentEdge) -> None:
        """Record a treatment edge.

        The citing judgment must already be registered; the cited authority may
        not be, and in that case the node is created as a *stub* (``resolved:
        False``). Stubs are the honest representation of a citation to something
        we have not ingested. They must never be silently treated as authority.
        """
        source_key = edge.citing_judgment
        if source_key not in self._judgments:
            raise KeyError(
                f"citing judgment {source_key!r} is not registered in the graph"
            )
        if edge.cited_key not in self.graph:
            self.graph.add_node(edge.cited_key, kind="unknown", resolved=False)
        self.graph.add_edge(
            source_key,
            edge.cited_key,
            treatment=edge.treatment.value,
            paragraph=edge.citing_paragraph,
            quote=edge.quote,
            confidence=edge.confidence,
            confirmed_by=edge.confirmed_by,
            via=edge.via,
        )
        self._edges[edge.cited_key].append(edge)

    def add_reference(self, citing_key: str, cited_key: str, paragraph: int) -> None:
        """A bare citation with no detected treatment.

        Kept separate from treatment edges: the graph should still show that the
        court cited the authority, even where we could not classify how.
        """
        if cited_key not in self.graph:
            self.graph.add_node(cited_key, kind="unknown", resolved=False)
        self.graph.add_edge(citing_key, cited_key, treatment=None, paragraph=paragraph)

    # -- queries -----------------------------------------------------------

    def judgment(self, key: str) -> Judgment | None:
        return self._judgments.get(key)

    def is_resolved(self, key: str) -> bool:
        return key in self._judgments

    def citations_made_by(self, key: str) -> list[tuple[str, TreatmentEdge | None]]:
        """Outgoing references: what this authority relies on."""
        out: list[tuple[str, TreatmentEdge | None]] = []
        for _, target, data in self.graph.out_edges(key, data=True):
            treatment = data.get("treatment")
            edge = (
                TreatmentEdge(
                    citing_judgment=key,
                    citing_paragraph=data.get("paragraph", 0),
                    cited_key=target,
                    treatment=Treatment(treatment),
                    quote=data.get("quote", ""),
                    confidence=data.get("confidence", 0.5),
                    confirmed_by=data.get("confirmed_by"),
                )
                if treatment
                else None
            )
            out.append((target, edge))
        return out

    def citing_authorities(self, key: str, *, confirmed_only: bool = False):
        """Incoming references: what relies on this authority."""
        for source, _, data in self.graph.in_edges(key, data=True):
            if data.get("treatment") is None:
                continue
            if confirmed_only and not data.get("confirmed_by"):
                continue
            yield source, data

    def authority_status(
        self,
        key: str,
        *,
        as_at: date | None = None,
        confirmed_only: bool = False,
    ) -> AuthorityStatusRecord:
        """Compute whether an authority is still good law.

        ``as_at`` gives point-in-time status: "was this good law when the
        transaction occurred", which is the question that actually matters in a
        dispute about conduct two years ago. Only treatment by judgments decided
        on or before that date is considered.

        ``confirmed_only`` restricts to treatment edges a lawyer has signed off
        on -- appropriate for any output a client will see.

        Two gates, and both are deliberate:

        * **The hierarchy gate.** A court can only bind downwards. A *diminishing*
          treatment by a subordinate court -- a refusal to follow, a doubt, a
          distinction -- is recorded and shown but cannot, on its own, take an
          authority out of the law. This is the difference between a citator and a
          tally: a single-judge Federal Circuit decision declining to follow a
          Full Court judgment does not overrule it, and a system that let it would
          give a wrong answer about the state of the law in exactly the cases where
          the stakes are highest. The gate is deliberately asymmetric -- it binds
          only treatment that moves the law downwards. Positive treatment from a
          subordinate court is considered, because it cannot downgrade anything and
          is genuine evidence that the authority is still being applied.

        * **The confidence gate.** An edge is *advisory* below
          :data:`ADVISORY_CONFIDENCE`, and advisory edges never drive the computed
          status. A phrase bound at long range, a phrase in a party's submission,
          a phrase whose grammatical target is a noun phrase from an earlier
          sentence, or a phrase bound to a case name rather than a citation token
          -- each of these can be wrong in a way no amount of code review will
          catch, and each is a plausible mechanism for branding good law as
          overruled. They are surfaced in the review queue instead, with the words
          that caused them, so a lawyer decides.
        """
        target_court = self._court_of(key)
        considered: list[TreatmentEdge] = []
        advisory: list[TreatmentEdge] = []
        blocked: list[TreatmentEdge] = []

        for source_key, data in self.citing_authorities(key, confirmed_only=confirmed_only):
            citing_court = self._court_of(source_key)
            citing_date = self._date_of(source_key)
            if as_at is not None and citing_date is not None and citing_date > as_at:
                continue
            edge = TreatmentEdge(
                citing_judgment=source_key,
                citing_paragraph=data.get("paragraph", 0),
                cited_key=key,
                treatment=Treatment(data["treatment"]),
                quote=data.get("quote", ""),
                confidence=data.get("confidence", 0.5),
                confirmed_by=data.get("confirmed_by"),
                via=data.get("via") or "citation_token",
            )
            # Hierarchy gate, applied only to treatment that *diminishes* an
            # authority. A subordinate court's refusal to follow, doubt or
            # distinction is an attempt to move the law, and it cannot: the
            # authority stands until a court at or above the target says
            # otherwise. That is the gate's whole purpose.
            #
            # It does not extend to positive treatment. A first-instance judgment
            # that applies or approves a Full Court decision is not attempting to
            # move anything -- it is evidence, of exactly the kind a citator
            # exists to collect, that the decision is still being used. Blocking
            # it would suppress information while protecting nothing, because a
            # positive edge can only ever support a GOOD_LAW finding; and it would
            # leave the reader of a High Court authority with no record at all of
            # the decisions applying it.
            if edge.treatment.is_negative and not _can_bind(citing_court, target_court):
                blocked.append(edge)
                continue
            if edge.confidence < ADVISORY_CONFIDENCE and not edge.confirmed_by:
                advisory.append(edge)
                continue
            considered.append(edge)

        status, caveat = _classify_status(
            considered,
            target_court,
            recorded_any=bool(considered or advisory or blocked),
        )
        if advisory:
            caveat = (
                caveat
                + f"  {len(advisory)} unreviewed treatment(s) are below the "
                "confidence threshold and are not reflected in this status; see "
                "the review queue."
            )
        if blocked:
            caveat = (
                caveat
                + f"  {len(blocked)} treatment(s) were recorded from courts that "
                "cannot bind this one and do not affect the status; see the "
                "hierarchy note below."
            )
        return AuthorityStatusRecord(
            target_key=key,
            status=status,
            evidence=tuple(considered),
            advisory_evidence=tuple(advisory),
            hierarchy_blocked=tuple(blocked),
            computed_at=datetime.now(),
            caveat=caveat,
        )

    def most_cited(self, *, limit: int = 10) -> list[tuple[str, int]]:
        """Authorities by incoming citation count -- a crude but useful signal."""
        counts = [
            (node, self.graph.in_degree(node))
            for node in self.graph.nodes
            if self.graph.nodes[node].get("kind") == "judgment"
        ]
        counts.sort(key=lambda pair: (-pair[1], pair[0]))
        return counts[:limit]

    def unresolved_citations(self) -> list[str]:
        """Cited authorities we have not ingested.

        A standing worklist. A corpus whose unresolved rate is high is a corpus
        whose citator cannot be trusted, because the graph is missing the cases
        that would supply the negative treatment.
        """
        return sorted(
            node
            for node in self.graph.nodes
            if self.graph.nodes[node].get("kind") == "unknown"
        )

    def precedent_chain(self, key: str, *, depth: int = 3) -> list[list[str]]:
        """Shortest citation paths back to apex authorities (High Court).

        Answers "what is the chain of reasoning from the High Court to this
        first-instance decision", which is how a junior lawyer would be told to
        check the point.
        """
        apex = {
            node
            for node in self.graph.nodes
            if self.graph.nodes[node].get("court") == Court.HCA.value
        }
        chains: list[list[str]] = []
        for target in apex:
            try:
                for path in nx.all_simple_paths(self.graph, key, target, cutoff=depth):
                    chains.append(path)
            except nx.NetworkXNoPath:
                continue
        chains.sort(key=len)
        return chains

    # -- internals ---------------------------------------------------------

    def _court_of(self, key: str) -> Court | None:
        judgment = self._judgments.get(key)
        return judgment.court if judgment else None

    def _date_of(self, key: str) -> date | None:
        judgment = self._judgments.get(key)
        return judgment.date_decided if judgment else None

    def stats(self) -> dict[str, int]:
        resolved = sum(
            1 for n in self.graph.nodes if self.graph.nodes[n].get("kind") == "judgment"
        )
        return {
            "nodes": self.graph.number_of_nodes(),
            "edges": self.graph.number_of_edges(),
            "judgments": resolved,
            "unresolved": len(self.unresolved_citations()),
            "treatment_edges": sum(len(v) for v in self._edges.values()),
        }


# --------------------------------------------------------------------------
# Status computation
# --------------------------------------------------------------------------


def _judgment_key(judgment: Judgment) -> str:
    """Derive the graph key from a judgment's neutral citation.

    Delegates to :meth:`Judgment.canonical_key` rather than re-deriving the key
    here. There was previously a second regular expression in this function, and
    it was narrower than the one in the model -- ``[A-Z]{2,6}`` against
    ``[A-Za-z][A-Za-z0-9]*``. The effect was that every judgment of the Federal
    Circuit and Family Court (whose code is ten characters) registered under its
    internal id while every citation *to* it resolved to the canonical key, so
    the judgment was permanently a stub and its treatment of other authorities
    was invisible. Two implementations of one identity is the defect; the fix is
    to have one.
    """
    return judgment.canonical_key() or judgment.id


def _can_bind(citing: Court | None, target: Court | None) -> bool:
    """Can a treatment by ``citing`` alter the authority of ``target``?

    True when the citing court is at or above the target in the hierarchy.
    Where the citing court is unknown, we are conservative and say no: an
    unranked court should not be allowed to silently downgrade an authority.
    """
    if citing is None or target is None:
        return False
    return citing.rank >= target.rank


def _classify_status(
    edges: list[TreatmentEdge],
    target_court: Court | None,
    *,
    recorded_any: bool = False,
) -> tuple[AuthorityStatus, str]:
    """Reduce treatment edges to one status plus a caveat for the user.

    ``recorded_any`` says whether *any* treatment was recorded for this authority,
    including edges the gates suppressed. It changes only the wording of the
    empty case, and it needs to: a court whose only recorded treatment came from
    below it is in a different position from one nobody has cited, and telling a
    lawyer "we have no evidence either way" when we are holding the evidence
    would be a lie told in the system's most reassuring register.
    """
    if not edges:
        if recorded_any:
            return (
                AuthorityStatus.UNCERTAIN,
                "Treatment was recorded for this authority, but none of it is "
                "capable of moving its status -- it came from a court that cannot "
                "bind this one, or below the confidence threshold. Read the notes "
                "below; they are evidence about how the authority is received, "
                "even though they are not evidence that it has been overruled.",
            )
        return (
            AuthorityStatus.UNCERTAIN,
            "No treatment recorded in the ingested corpus. This does not mean the "
            "authority is good law -- it means we have no evidence either way, and "
            "the corpus may simply not contain the relevant later decisions.",
        )

    def has(*treatments: Treatment) -> list[TreatmentEdge]:
        return [e for e in edges if e.treatment in treatments]

    overruling = has(Treatment.OVERRULED, Treatment.DEPARTED_FROM)
    if overruling:
        strongest = max(overruling, key=lambda e: e.confidence)
        return (
            AuthorityStatus.OVERRULED,
            f"Treated as overruled or departed from in {strongest.citing_judgment} "
            f"at [{strongest.citing_paragraph}]. Do not rely on it without reading "
            "that decision.",
        )

    not_followed = has(Treatment.NOT_FOLLOWED)
    doubted = has(Treatment.DOUBTED)
    if not_followed or doubted:
        edge = max(not_followed + doubted, key=lambda e: e.confidence)
        return (
            AuthorityStatus.QUESTIONED,
            f"Authority questioned in {edge.citing_judgment} at "
            f"[{edge.citing_paragraph}]. The proposition may still hold; check "
            "whether it has since been upheld.",
        )

    distinguished = has(Treatment.DISTINGUISHED)
    positives = has(Treatment.APPROVED, Treatment.APPLIED, Treatment.FOLLOWED)
    if distinguished and not positives:
        edge = max(distinguished, key=lambda e: e.confidence)
        return (
            AuthorityStatus.DISTINGUISHED_ON_FACTS,
            f"Distinguished in {edge.citing_judgment} at [{edge.citing_paragraph}]. "
            "The legal proposition stands, but its application is confined on "
            "facts materially like those of the later case.",
        )

    if positives:
        edge = max(positives, key=lambda e: e.confidence)
        return (
            AuthorityStatus.GOOD_LAW,
            f"Applied or approved in {edge.citing_judgment} at "
            f"[{edge.citing_paragraph}]. Treatment within the ingested corpus is "
            "consistent with the authority remaining good law.",
        )

    return (
        AuthorityStatus.UNCERTAIN,
        "Cited only in neutral terms; no treatment has been classified. Read the "
        "citing passages before relying on the authority.",
    )


def status_report_lines(record: AuthorityStatusRecord, title: str) -> list[str]:
    """Formatting shared by the CLI and the MCP server."""
    icon = {
        AuthorityStatus.GOOD_LAW: "OK  ",
        AuthorityStatus.DISTINGUISHED_ON_FACTS: "LIM ",
        AuthorityStatus.QUESTIONED: "WARN",
        AuthorityStatus.OVERRULED: "STOP",
        AuthorityStatus.SUPERSEDED_BY_STATUTE: "STOP",
        AuthorityStatus.UNCERTAIN: "????",
    }[record.status]
    lines = [
        f"{icon} {record.target_key}  {title}",
        f"     status: {record.status.value}",
        f"     {record.caveat}",
    ]
    for edge in record.evidence[:5]:
        verified = "confirmed" if edge.confirmed_by else "unconfirmed"
        lines.append(
            f"       - {edge.treatment.value} by {edge.citing_judgment} "
            f"at [{edge.citing_paragraph}] ({verified}, conf {edge.confidence:.2f})"
        )
        if edge.quote:
            lines.append(f"         \"{edge.quote[:150]}\"")

    # Advisory edges are printed even though they did not move the status. Holding
    # them out of the computation is right; holding them out of the *report* would
    # hide the one thing a reviewer most needs to see -- that the machine formed a
    # negative reading of this authority and we chose not to act on it. Rendered
    # below the fold so it cannot be mistaken for the basis of the status above.
    for edge in record.advisory_evidence[:5]:
        lines.append(
            f"       ? {edge.treatment.value} by {edge.citing_judgment} "
            f"at [{edge.citing_paragraph}] (ADVISORY, not counted, "
            f"conf {edge.confidence:.2f}, via {edge.via})"
        )
        if edge.quote:
            lines.append(f"         \"{edge.quote[:150]}\"")
    if len(record.advisory_evidence) > 5:
        lines.append(
            f"       ? ... {len(record.advisory_evidence) - 5} further advisory "
            "edge(s) in the review queue"
        )

    # Hierarchy-blocked edges: recorded, shown, and incapable of changing the
    # status even if a reviewer confirms them. Rendered with the citing court's
    # rank made explicit, because the reason they do not count is not obvious
    # from the edge itself and readers will otherwise assume a bug.
    for edge in record.hierarchy_blocked[:5]:
        lines.append(
            f"       ! {edge.treatment.value} by {edge.citing_judgment} "
            f"at [{edge.citing_paragraph}] (BELOW THIS COURT, cannot bind, "
            f"conf {edge.confidence:.2f})"
        )
        if edge.quote:
            lines.append(f"         \"{edge.quote[:150]}\"")
    if len(record.hierarchy_blocked) > 5:
        lines.append(
            f"       ! ... {len(record.hierarchy_blocked) - 5} further "
            "subordinate-court treatment(s)"
        )
    return lines
