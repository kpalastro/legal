# 12 — Roadmap and risks

## How to read this

Everything in this repository is a working scaffold plus a design, and the
honest accounting of which is which is in `README.md` and repeated at the foot of
each document. This document is the part that says what I would do next, in what
order, and what would make me stop.

The ordering is not by effort. It is by **what would change a decision** — what
would tell us something we do not currently know, or close a gap that
currently makes a claim unsupportable.

## The five things that are actually open

### 1. Approve the shipped ruleset, or stop claiming the model works

`firb.notifiable_action` is `status: draft` with no approval. The approval
mechanism is built and tested — `Approval.hash_of` over
`RuleSet.canonical_payload`, with `current_approval` returning nothing when the
payload has moved — and no expert has used it.

This matters more than its size suggests, because the entire argument of the
project is *"an expert can inspect, edit and govern the rule the machine
applies"*, and that argument is currently untested against a real expert. Until
someone signs a rule off, the mechanism is a hypothesis. The work is not
engineering: it is a workshop, a case-generation sheet from `ratio cases`, and an
SME who is willing to adjudicate a few hundred generated fact combinations and
find out whether the encoding is what they thought it was.

The likely outcome is that it is not, in at least one place. That is the point.

### 2. Replace the synthetic corpus with a licensed real one

The fixture corpus is seven invented judgments, and the system is honest about it
everywhere. It is also the ceiling on what any result here can mean: a retrieval
margin measured over six synthetic cases constrains nothing about real
Australian law.

The engineering is one loader — `ingest/fixtures.py` is the seam and the KB,
retrieval, rules and verification layers never see anything but the domain model.
The hard part is upstream and it is not technical: **AustLII's terms prohibit use
of its content for AI training and embedding purposes**, so the corpus has to
come from a source that licenses it, and the source determines the ingest
contract. Doing this properly means, in order: settle the licence; write the
loader against the licence's terms rather than against what the HTML happens to
make easy; re-derive the treatment classifier's patterns against real prose
rather than the fixture's; and re-calibrate the coverage gate, which is currently
calibrated on six cases and would be meaningful on six hundred.

I would not do this before (1). A real corpus makes the ruleset's gaps harder to
see, because the system will start finding real authority for the wrong reasons.

### 3. Close the delegate seam

The rules layer and the probabilistic layer are both built and tested and do not
call each other. `NodeKind.DELEGATE_BN` is modelled and reported, and the call is
not made. Until it is, the system cannot answer the questions it was built for —
"is this substantial", "is this contrary to the national interest" — end to end;
it can only answer them in separate commands.

This is the smallest of the five and the most visible, because it is the join
between the two halves of the "both rules and Bayesian" decision.

### 4. The agent layer, the ledger, and the remaining surfaces

`agents/`, `obs/` and `infra/` are empty directories with designs:
`docs/07-agent-orchestration-mcp.md`, `docs/09-observability-and-audit.md` and
`docs/10-aws-deployment-security.md`. The expert authoring surface from
`docs/06-expert-authoring-surface.md` is **built** — the interface in `ui/`, the
service in `src/ratio/authoring/`, served by `ratio serve` — and what remains of
`docs/06` is its job (a), the lawyer asking a question, which is a design.

Of these, the one I would build first is **not** the agent layer. It is the
**audit ledger**. The reasoning is the same as the reasoning for the harness: a
system that cannot account for its own outputs cannot be governed, and the ledger
is what makes the account possible. It is also the cheapest — a hash chain over
append-only records is a small amount of code against a large amount of
accountability. The agent layer is the one I would build last, because it is the
only component that can fabricate, and adding it before the ledger exists means
adding the fabrication risk without the means to reconstruct it afterwards.

### 5. Index the statutory provisions

Retrieval iterates judgments only. The corpus holds seven provisions, they are
resolvable by key and readable as an MCP resource, and no query reaches them.
This is a small gap with a large false-negative surface: a lawyer asking about s
18 gets case law about s 18 and not s 18.

It is listed here rather than buried because it is recorded as a limitation in
the task suite's own header (`eval/tasks/04_retrieval.yaml`), which means the
suite reports it as a known gap rather than as a retrieval failure. Keeping that
distinction is the thing to protect.

## Risk register

Framed against ISO 31000 and ISO/IEC 23894; the governance mapping is in
`docs/11-governance-assurance.md`. Likelihood and impact are the ordinary
three-point scale, and the treatment column says what actually reduces the risk
rather than what would be reassuring to write down.

| # | Risk | L | I | Treatment |
|---|---|---|---|---|
| R1 | A fabricated or unsupported authority reaches a client, and the system's fluency is what made it convincing | M | H | Span-exact verification with no partial pass; `expect_refusal` guardrail cases that fail a suite outright; the agent layer kept off the critical path; abstention as a first-class outcome. Residual: the verifier can only check authority *we hold*, so a real case we have not ingested is reported `unverifiable` — which a hurried reader may take as a pass. |
| R2 | The encoding drifts from what the expert believed they approved | H | H | Approval as a hash over the canonical payload, so editing invalidates; case generation with inert-leaf and defeater-coverage reporting; the SME adjudicates outcomes rather than reading structure. Residual: the shipped ruleset is unapproved, so this is untested against a real expert. |
| R3 | The system is relied on as advice rather than as an input to advice | M | H | Every output carries anchors, authority status and ruleset approval state; the ruleset reports `approved: false`; the advisory posture is stated in the UI design. Residual: a caveat that is always present is a caveat nobody reads, and no design fixes that. |
| R4 | A stale authority is cited because status was computed at the wrong point in time | M | H | `as_at` on every query, echoed in every answer; treatment edges dated; exclusion of post-date treatment asserted in tests. Residual: provisions are not point-in-time indexed by the retriever at all. |
| R5 | The eval suites give false comfort — small, hand-curated, and easy to pass | H | M | Exact binomial intervals rather than the degenerate bootstrap; baselines reported; limitations written into the task file itself; family margin asserted rather than pass rate. Residual: six cases. Nothing in the harness can fix six cases. |
| R6 | Synthetic fixture text is mistaken for real authority | L | H | `synthetic: true` on every document, surfaced in the MCP corpus resource and asserted by a test; the loader refuses mixed corpora unless explicitly overridden. Residual: a downstream consumer that strips metadata. |
| R7 | Licensing: content used for AI purposes without the right to | M | H | AustLII is not scraped and not used for embedding or training; only the fixture corpus is ingested; real acquisition is treated as a licensing question before an engineering one. Residual: the `SourceKind.AUSTLII` constant exists, which could be misread as a licence — it is documented not to be one. |
| R8 | Numeric fault in the probabilistic layer, silently producing confident wrong numbers | M | H | Two independent exact-inference implementations cross-checked over all single-root evidence sets; the cross-check space asserted non-empty; the layout faults found this way pinned as regression tests. Residual: the cross-check covers single-root evidence sets, and the network is small. |
| R9 | Dependency or supply-chain compromise, including through a data source | L | H | Four pinned runtime dependencies; the deterministic core runs with no network; no embedding model bundled; egress is deniable rather than monitored in the intended deployment. Residual: `infra/` is a skeleton, so none of this is enforced in an actual deployment. |
| R10 | Confidential matter data reaches a model provider, or is used to improve a third-party model | M | H | The deterministic core is model-free; the agent layer is opt-in and separately egressed; the corpus must not be sent wholesale as context. Residual: not built, and the control is a design statement until it is. |
| R11 | Key-person risk: the encoding is only legible to the person who wrote it | M | M | Rulesets, networks and task suites are declarative files outside the package; provenance is on every asserting node; the authoring surface is designed for the domain expert rather than the engineer. Residual: the authoring surface is not built, so today "legible" means "legible in YAML". |

## What would make me stop

A roadmap that only lists what to build next is a sales document. These are the
findings that would end the project, and I would rather name them now than
discover them as sunk cost.

**If the expert-adjudication loop in (1) shows that the encoding cannot be kept
faithful.** If a domain expert, working through generated cases over a real
ruleset, disagrees with the engine's outcomes at a rate that cannot be driven
down by editing the rule, then the premise fails: the rule language cannot
express the law at the fidelity the use case needs. That is a falsifiable outcome
and the machinery to produce it already exists.

**If the retrieval margin does not survive a real corpus.** The coverage gate is
calibrated on six synthetic judgments with a 0.40 margin. If, on a licensed
corpus, the distributions of coverage for answerable and unanswerable questions
overlap, then IDF-weighted query coverage is not measuring what it is supposed to
measure, and the abstention gate goes back to being a control that cannot fire —
this time with more confidence behind it.

**If a fabricated authority escapes into a filed document.** Not a demo, not a
test: a client matter. The guardrails are designed so this requires a failure of
verification *and* of the human review gate, and the honest response would be to
stop and re-examine the assumption that a system like this can be safely
supervised by practitioners under time pressure. The evidence from *Mata* and
*JNE24* is that practitioners do not reliably catch machine-generated citations,
and a design that assumes they will is a design built on a known-false premise.

**If the licensing position cannot be settled.** Without a corpus that may
lawfully be used for this purpose there is no product, only a demonstration. The
fixture corpus is honest about being a fixture, and it cannot become anything
else by engineering effort.

## Sequencing, in one line

Approve a real rule with a real expert → licence and ingest a real corpus →
re-calibrate the gate and the treatment classifier against real prose → close the
delegate seam → build the audit ledger → then, last, the agent layer and the
surfaces. Each step is a decision point, and the first one is the only one that
can be taken this week.
