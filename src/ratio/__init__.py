"""RATIO -- a citation-grounded knowledge base of existing judgments, with an
executable encoding of legal judgement on top of it.

Layering, outermost to innermost:

    corpus      judgments, structured but not interpreted   (model, citations, ingest)
    kb          the citation graph and point-in-time store  (kb)
    retrieval   hybrid search with authority filters        (retrieve)
    verification everything asserted is checked             (verify)
    reasoning   defeasible rules + credence networks        (rules, bn)
    agents      orchestration over MCP tools                (agents, mcp_server)
    assurance   traces, eval, audit ledger                  (obs, eval)

The design rule that holds the whole thing together: a claim that cannot be
traced to a span of an existing judgment, or a statutory provision, or a named
expert's approval, does not leave the system.
"""

from ratio.model import (
    Approval,
    AuthorityRef,
    AuthorityStatus,
    Citation,
    CitationKind,
    Court,
    CredenceNetwork,
    Finding,
    GroundedQuote,
    Judgment,
    Jurisdiction,
    Paragraph,
    ProofTree,
    RatioProposition,
    RuleSet,
    StatutoryProvision,
    Treatment,
)

__version__ = "0.1.0"

__all__ = [
    "Approval",
    "AuthorityRef",
    "AuthorityStatus",
    "Citation",
    "CitationKind",
    "Court",
    "CredenceNetwork",
    "Finding",
    "GroundedQuote",
    "Judgment",
    "Jurisdiction",
    "Paragraph",
    "ProofTree",
    "RatioProposition",
    "RuleSet",
    "StatutoryProvision",
    "Treatment",
    "__version__",
]
