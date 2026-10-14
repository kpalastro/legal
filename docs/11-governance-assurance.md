# Governance and conformity

## What this document is

A conformity map, and a confession.

The conformity map is the easy half. A system that encodes legal judgement and
tells lawyers what to do with it sits inside a known set of management-system
standards and professional obligations, and it is worth writing down which
artefact in this repository answers which clause, because the alternative is a
set of claims about compliance that nobody can check.

The confession is the harder half, and it is the reason there is a "do not cite"
list at the end of this document. An earlier draft of this project cited several
instruments that do not exist — a confidentiality rule from the wrong number, a
NIST publication one number past the real one, a joint statement by two bodies
that have not issued one. None of them were invented maliciously; they were
invented by the ordinary mechanism by which a plausible-sounding citation
propagates. That is the same failure this whole repository exists to prevent, and
it happened in this repository's own documentation. So the list is here, in the
document that would otherwise be copied from, and it stays.

## What is actually built, and therefore what can be mapped

A conformity map that maps a control to an intention is a compliance-theatre
artefact. The rule adopted here is that an entry names either a file and a
function that exist, or the word **designed** and nothing more.

What exists and runs, as at this revision:

* a paragraph-anchored corpus loader that refuses to mix synthetic and real
  authorities (`src/ratio/ingest/fixtures.py`);
* a citation graph with two suppression gates and a three-bucket status record
  (`src/ratio/kb/graph.py`);
* span-exact quote verification (`src/ratio/verify/grounding.py`);
* a defeasible rule engine with governance validation (`src/ratio/rules/engine.py`);
* a credence network whose CPTs must agree with their elicitation records
  (`src/ratio/bn/network.py`);
* hash-bound expert approvals (`Approval.hash_of`, `RuleSet.current_approval`);
* an evaluation harness with abstention as a first-class outcome
  (`src/ratio/eval/`);
* an MCP service layer that names its refusals and carries the nearest thing the
  corpus does hold (`src/ratio/mcp_server/server.py`).

What does not exist: `ratio/obs/` (empty), `ratio/agents/` (empty), `ui/`
(empty), `infra/` (empty). Every checkpoint below that depends on those is
marked **designed**.

## ISO/IEC 42001:2023 — the AI management system

ISO/IEC 42001 is the management-system standard for organisations developing or
using AI. The relevant question for a codebase is not "are we certified" — that
is an organisational question — but "does the technical artefact give the
management system something true to point at". The Annex A controls, mapped:

**A.2 — Policies related to AI.** The written position of this system is this
`docs/` directory and the rulebook in `rulesets/`, and the rulebook is not just
a policy statement: it carries a `status` field, an `effective_from` date, and a
tuple of `Approval` records. A policy that can be edited without the edit being
visible is not a policy under this standard. The ruleset's approval hash is what
makes it one.

**A.3 — Internal organisation.** Roles are separated. The engine will not treat
a node as approved because a person wrote it; `Approval` carries an `approver`
and a `role` ("Partner, M&A", "Special Counsel, Foreign Investment") that are
distinct from the person who authored the encoding, and `Approval.hash_of`
covers the artefact rather than the act. The reviewer role is a separate job
again — see `docs/06-expert-authoring-surface.md` — because the person who
checks a treatment edge against the citing paragraph is not the person who
wrote the rule the edge feeds.

**A.4 — Resources for AI systems.** The resource that matters here is the
corpus, and its governing rule is negative: no unlicensed material. See the
AustLII section below. Positively, `SourceRef` on every judgment and every
provision records kind, URL, retrieval time, licence and a sha256 of the raw
source bytes, so a corpus can be reproduced and a licence question answered.

**A.5 — Assessing impacts of AI systems.** The impact that has been assessed is
not a general "risks of AI" assessment; it is the specific harm this system can
do, which is **fabricating authority and having it believed**. Every design
decision in this repository descends from it. The affected parties are
identified precisely: the lawyer who relies on the output, the client whose
matter it is, the court before which a submission built on it is filed, and the
person the law is being applied to. The fourth is the one that gets lost in
professional-services AI work, and it is the one with no relationship to the
system and no ability to contest its output.

**A.6 — System life cycle.** Versioned rulesets with `effective_from` and
`effective_to`; `ProofTree` records `ruleset_id` and `ruleset_version` on every
conclusion; `Approval.hash_of` binds the approval to one exact revision. Legal
point-in-time is not a nicety here — `graph.authority_status(..., as_at=...)`
exists because the question in a dispute is usually "was this good law when the
conduct occurred", not "is it good law now".

**A.7 — Data for AI systems.** Two rules, both enforced in code rather than
stated in a policy:

1. *No mixed corpora.* `load_corpus` refuses a corpus containing both synthetic
   and real authorities unless `allow_mixed=True` is passed explicitly, and the
   CLI documents that flag as "for test harnesses only; never for a corpus that
   will answer questions". A synthetic document must not be able to sit silently
   alongside a real one in a production index.
2. *Provenance or nothing.* `AuthorityRef` is narrow by construction: a
   paragraph of a judgment, a statutory provision, or an expert's own statement.
   There is no variant for "the model said so", and `validate_ruleset` reports
   any leaf or exception node that arrives without provenance.

**A.8 — Information for interested parties.** The system's reporting of its own
limits is the artefact: abstention is counted separately from error and never
folded into it (`ratio/eval/metrics.py`); a rule conclusion with a missing fact
returns `undetermined` with a gap list naming the *field a user can establish*;
a retrieval that cannot cover a question raises a refusal naming the nearest
thing the corpus does hold. All three are cases where the system tells its user
what it does not know, which is the only form of transparency that is worth
anything operationally.

**A.9 — Use of AI systems.** The posture is advisory and the human decides. The
evidence for that is structural: the credence layer returns a `PosteriorReport`
— a distribution, sensitivities, the authorities behind each factor and a
mandatory caveat — and there is no boolean in it. Every status carries the
sentences that produced it.

**A.10 — Third-party relationships.** The embedder is the third party, and the
seam is deliberate: `HybridRetriever` accepts an embedder and falls back to a
deterministic model-free path. The obligation that follows is that a third-party
embedder must be benchmarked on *this* corpus before it is allowed to affect a
ranking, because an embedder trained on general text will rank legal prose by
topical similarity, and topical similarity is the failure mode the coverage gate
exists to catch.

Certification-side: **ISO/IEC 42006:2025** sets out requirements for the bodies
that audit AI management systems. Nothing in this repository is written against
it, and it is named only so that the reader knows which document governs the
auditor rather than the audited.

## Risk management

**ISO/IEC 23894:2023** (AI risk management) and **ISO 31000** supply the risk
frame used in `docs/12-roadmap-and-risks.md`. Their contribution here is
methodological and worth one sentence: risk is expressed as consequence and
likelihood against an affected party, not as a list of technical defects. The
single highest-consequence risk in this system — a lawyer relying on a
fabricated or overruled authority — has a likelihood that is dominated by
*adoption* factors rather than model factors, which is why so much of this
design is about what the interface puts in front of a reader rather than about
the quality of the retrieval.

## NIST AI RMF 1.0 and the Generative AI Profile

**NIST AI RMF 1.0** organises around four functions: GOVERN, MAP, MEASURE,
MANAGE. **NIST AI 600-1**, the Generative AI Profile published alongside it,
adds roughly 212 suggested actions to those functions, identified in the form
`GOVERN 1.1`, `MEASURE 2.7` and so on. Most of them are about organisations and
are not a codebase's business. The five that are:

* **Provenance and citation of generated content** (`MAP` / `MEASURE`,
  provenance). Answered by `ratio/verify/grounding.py` and by the rule that a
  claim without a resolvable citation is not clean.
* **Confabulation measurement.** `NIST AI 600-1` treats confabulation as a
  quantity to be measured rather than a bug to be eliminated, which is the
  correct framing and is what the guardrail cases in `eval/tasks/` do: cases
  whose *expected* answer is a refusal, where answering correctly is scored as
  the failure.
* **Human oversight of generative output.** The `G1`–`G5` checkpoints below.
* **Pre-deployment testing and red-teaming**, specifically of the refusal path.
  A refusal path that has never been attacked is untested, and the retrieval gate
  tests in `tests/test_harness.py` are a first, small version of this: they assert
  both that the gate refuses when the corpus is silent *and* that it does not
  refuse when the corpus has something, because a gate tuned only against
  confabulations is satisfied by refusing everything.
* **Incident disclosure.** Designed, not built: `docs/09-observability-and-audit.md`
  describes the ledger that would make a disclosure possible, and `ratio/obs/` is
  empty.

## The Australian Voluntary AI Safety Standard

The Voluntary AI Safety Standard (VAISS) is guidance rather than law, and its
ten guardrails are the most directly applicable instrument in this document
because they were written for exactly this situation — an Australian
organisation deploying AI in a regulated domain with a duty of care to people
who are not its users.

| # | Guardrail | Where it lands here |
|---|---|---|
| 1 | Accountability at the highest level | `Approval.approver` and `.role`; the named signatory is a partner or special counsel, not a team |
| 2 | Risk management | The risk register and stop conditions in `docs/12-roadmap-and-risks.md` |
| 3 | Data governance | `SourceRef` licence and checksum fields; the corpus-provenance rule |
| 4 | Testing and monitoring | `ratio/eval/`, task suites as data, `pass_pow_k` for repeat-run reliability |
| 5 | Human oversight and intervention | G1–G5; designed UI surfaces for the review queues |
| 6 | Transparency and explainability | `ProofTree`, the three authority-status buckets, refusals that name their reason and the nearest thing the corpus holds |
| 7 | Contestability and redress | **Thin.** An expert can dispute a rule node and re-approve; there is no mechanism by which the *person the law is applied to* can contest an output, and there should be a written answer to that |
| 8 | Supply-chain and third-party risk | The embedder seam and its benchmarking obligation; designed only |
| 9 | Record-keeping and audit trails | Designed in `docs/09-observability-and-audit.md`; not implemented |
| 10 | Stakeholder engagement | **Thin.** The elicitation records name the experts consulted; no engagement with anyone outside the firm is recorded |

Guardrails 7 and 10 are where this design is weakest, and neither is a coding
problem. They are the two the next iteration should answer in writing.

## Professional ethical obligations

**APES 110** (the Code of Ethics for Professional Accountants, which Australian
professional-services firms map against where an engagement has an audit-adjacent
character). The sections that bear on a system like this:

* **110 (Integrity)**, **112 (Professional Competence and Due Care)**,
  **113 (Confidentiality)**, **114 (Professional Behaviour)** — the four that a
  system which retrieves privileged facts and produces a professional work
  product engages directly. Confidentiality is the operative one for the ledger:
  see "what must not go in it" in `docs/09-observability-and-audit.md`.
* **111 / 120 (Objectivity)**, and **120.6 A2**, which names **automation bias**
  as a threat to objectivity: the tendency of a person to favour a system's
  output over their own judgement. This is the single most important
  professional-ethics hook in the whole document, and it is the reason the design
  refuses to produce a verdict anywhere. A system that outputs a conclusion is a
  system that invites a competent practitioner to stop forming one.
* **R320.11 / R220.8** — the long-association and familiarity provisions, which
  apply by analogy to an encoded rulebook: a rule that has been in the artefact
  unchanged for years and has never been re-examined is a rule whose correctness
  is being assumed because it is familiar. The ruleset's `effective_from` date
  and the review sheet's coverage report are the mechanism against this.

**On the numbering.** The 2024 restructure of APES 110 moved several principles
and changed numbers; the sections above overlap (Objectivity appears under both
111 and 120 in the sources this project consulted), and this document is not
confident enough about the current compilation to resolve that overlap. The
substance is used and the numbering is given as consulted. Before any of this is
cited in a filed document or a submission, check it against the current text —
which is precisely the rule this repository exists to enforce, and it applies to
this document as much as to any answer the system gives.

## Audit-side standards

The **AUASB** publication *The Impact of AI on Auditors* (14 July 2025) is the
Australian auditing standard-setter's position on where AI changes audit
obligations. It is cited here as context rather than as a control this codebase
implements, because its audience is auditors and this system is not an audit
tool. What transfers is the documentation discipline:

* **ASQM 1**, paragraph 32(f) — engagement quality and the firm's responsibility
  for the systems it relies on. The analogue here is that a ruleset in `draft` is
  not a ruleset the firm may rely on, and the shipped ruleset is in `draft` today.
* **ASQM 2** — engagement quality reviews, by analogy the reviewer role
  separated from the authoring role.
* **ASA 230** — audit documentation. The direct analogue is the execution
  receipt in `docs/09-observability-and-audit.md`: sufficient record of what was
  done, with what, and on whose authority.
* **ASCR r 4.1.3**, **r 9 (Confidentiality)** and **r 37 (Supervision)**. r 9 is
  the one to keep in view: it constrains the audit ledger's contents, and r 37 is
  the reason a supervised junior's use of this system needs the reviewer surfaces
  to exist before deployment, not after. (r 4.1.3 is cited without a gloss here
  because this document is not confident enough about its subject matter to
  paraphrase it, and a wrong gloss is worse than no gloss.)

## The EU AI Act

The AI Act applies, amended. **Article 113** sets the application timeline, as
amended by **Regulation (EU) 2026/1744**, which changes when the Act's
obligations start to bite for the categories this system could fall into. The
relevant points for an Australian deployment:

* **Article 2(1)(c)** is the third-country hook. An Australian firm does not
  escape the Act by being Australian where the output of the system is used in
  the Union. The question to ask is not "where is the server" but "where is the
  output used".
* **Article 14** sets out what effective human oversight requires. Its five
  capabilities are, in substance: to understand the system's relevant capacities
  and limitations; to remain aware of automation bias; to interpret the output
  correctly; to decide not to use it, or to disregard, override or reverse it;
  and to intervene in or interrupt the system's operation. Every one of those is
  a UI and documentation obligation, not a model obligation — and the second of
  them is the same automation-bias concern APES 110 names at 120.6 A2, which is
  worth noticing because it means the Australian professional-ethics answer and
  the EU regulatory answer point at the same design decision.
* **Classification.** A legal-research tool is not automatically high-risk under
  Annex III. The deployer's use determines the classification, and a tool that
  is used to assess a person's legal position is in a different category from one
  used to find a paragraph. This is a reason to keep the system advisory and to
  keep the human decision visible in the interface.

## The human-in-the-loop checkpoints

Five, and it matters a great deal which of them are enforced by code and which
are only written down.

| | Checkpoint | Status |
|---|---|---|
| **G1** | **Corpus admission** — a document enters the knowledge base only with a licence, a checksum and a source record | Enforced: `SourceRef`, the mixed-corpus refusal in `load_corpus` |
| **G2** | **Citation-binding review** — a machine-proposed treatment edge is reviewed against the sentence that produced it before it may change an authority's status | Review queue exists (`advisory_evidence`, `!` hierarchy-blocked edges, `TreatmentEdge.needs_review`); **the confirm step has no UI**. `confirmed_by` is a field the code honours and nothing populates |
| **G3** | **Rule encoding sign-off** — the ruleset approval binds to a hash of the exact revision | **Enforced.** `validate_ruleset` reports a ruleset marked `approved` whose revision no longer matches any approval, and `RuleEngine.__init__` raises `RuleEvaluationError` on any validation issue, so such a ruleset cannot be evaluated at all |
| **G4** | **Elicitation sign-off** — every CPT row agrees with a record of what a named expert stated | Enforced: `CredenceEngine` refuses to load a network whose numbers differ from the elicitation records |
| **G5** | **Pre-release evaluation gate** — the suites pass, guardrail cases included, before a change ships | Harness and guardrail-inversion scoring exist; **no CI gate is wired**. Nothing currently stops a merge |

Two precisions that matter, because both are easy to overstate:

* The approval enforcement at G3 is a *consistency* control, not an *approval*
  control. A `draft` ruleset validates cleanly and runs — and reports
  `"approved": false` in its result, which the MCP tests assert. The system will
  evaluate an unapproved rule and tell you it did; it will not evaluate a rule
  that claims to be approved and is not.
* Nothing in the repository writes an approval. `ratio rules approve` prints the
  digest and a YAML block for a person to paste. That is deliberate: a tool that
  performs an expert's sign-off turns a governance control into a build step.

## Corpus acquisition: the AustLII prohibition

AustLII's terms of use prohibit the use of its content for artificial
intelligence training and embedding purposes. This project must therefore not
scrape AustLII, must not use AustLII content to build or fine-tune an embedder,
and must not ship an index derived from it. This is not a technical constraint
that can be engineered around; it is a licensing constraint, and it is the
reason `SourceKind` names AustLII alongside the other sources while the fixture
corpus is `SYNTHETIC` and the ingest adapter interface is the only thing
implemented. Any real deployment acquires judgments from a licensable or
first-party source, and answers the licensing question before the engineering
one — which is what `docs/02-judgment-knowledge-base.md` is about.

## The synthetic corpus

`data/fixtures/judgments.yaml` contains judgments written for this repository.
They use realistic citation formats and realistic statutory references, and the
cases, parties and reasoning are invented. Every document carries
`synthetic: true`, which travels through `ParagraphRef.synthetic` into retrieval
results, through `RatioService.corpus_summary()` into the MCP `ratio://corpus`
resource, and into every payload that could be shown to a user.

The rule is absolute: **nothing generated from the fixture corpus may be
presented as real Australian authority.** The loader refuses mixed corpora so
that a synthetic document cannot silently sit alongside a real one, and
`ratio://corpus` reports `synthetic: true` and `has_real_judgments: false` so
that a client cannot show these results without the flag. The MCP test file
opens by testing exactly this, because the server is the last place it can be
prevented.

## Do not cite

The following do not exist, and were cited in an earlier draft of this project.
They are listed so that they are not cited again, and so that anyone reading a
document that still contains them knows to stop.

* **`LPUL s 4.1.3`** — cited for a proposition about a solicitor's obligations.
  Do not rely on this reference.
* **`ASCR rule 41`** — cited for confidentiality. The confidentiality rule is
  **r 9**, which is cited correctly above.
* **`NIST AI 600-2`** — the Generative AI Profile is **NIST AI 600-1**.
* **A "Law Council of Australia and ABA joint statement on AI"** — no such joint
  statement exists. Referencing it invents a position that two professional
  bodies have not taken.
* **An AUASB publication titled "Consideration of AI in an Audit"** — the AUASB
  publication is *The Impact of AI on Auditors* (14 July 2025), cited above.
* **`APES 110 s 116`** — there is no section 116 in the list this project relies
  on.
* **`NIST AI RMF 2.0`** and **`NIST AI 700-1`** — neither exists. The AI RMF is
  version 1.0.

### Why this list is in the document

Because a conformity map that cites instruments which do not exist is worse than
no conformity map at all.

Without a map, a reader knows they have to do the work themselves. With a map,
they do not: the map's authority is transferred to everything in it, including
the entries that are wrong, and the wrong entries are the ones with the most
specific-looking citations. The failure compounds — a map is copied into a
policy, the policy is copied into an assurance report, and by the time anyone
checks the citation it is four documents away from the person who first typed
it. This is precisely the pathology of a fabricated authority in a legal
submission, and it happened here, in the documentation of a system built to make
it impossible.

The mitigation is the same one used everywhere else in this repository: every
citation in this document is either verifiable against the instrument itself, or
it is not here.
