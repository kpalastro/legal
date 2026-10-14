# 01 — Architecture

## The shape of the thing

```
                          ┌──────────────────────────────────────────┐
   existing judgments ───▶ │  CORPUS      structure, anchors, cites   │
   legislation        ───▶ │  KB          citation graph, PIT store   │
                          └────────────────┬─────────────────────────┘
                                           │
                          ┌────────────────▼─────────────────────────┐
                          │  RETRIEVAL   BM25 ⊕ dense, filters,      │
                          │              rerank, authority status    │
                          └────────────────┬─────────────────────────┘
                                           │
        ┌──────────────────────────────────┼──────────────────────────────┐
        │                                  │                              │
┌───────▼────────┐              ┌──────────▼─────────┐          ┌─────────▼────────┐
│ RULES          │              │ CREDENCE           │          │ VERIFY           │
│ defeasible     │◀──delegate──▶│ Bayesian network,  │          │ span-exact       │
│ rule trees +   │              │ elicited CPTs,     │          │ grounding,       │
│ proof trees    │              │ sensitivity        │          │ treatment check  │
└───────┬────────┘              └──────────┬─────────┘          └─────────┬────────┘
        └──────────────────────────────────┼──────────────────────────────┘
                                           │
                          ┌────────────────▼─────────────────────────┐
                          │  AGENTS      planner/retriever/analyst/  │
                          │              adversarial verifier, MCP   │
                          └────────────────┬─────────────────────────┘
                                           │
        ┌──────────────────────────────────┼──────────────────────────────┐
        │                                  │                              │
┌───────▼────────┐              ┌──────────▼─────────┐          ┌─────────▼────────┐
│ EXPERT UI      │              │ EVAL + OBS         │          │ GOVERNANCE       │
│ inspect, edit, │              │ golden set,        │          │ ISO 42001 map,   │
│ test, approve  │              │ regression, OTel   │          │ audit ledger     │
└────────────────┘              └────────────────────┘          └──────────────────┘
```

The diagram is a lie in one respect worth naming: the arrows run downward, but
the dependencies do not. Verification is drawn below retrieval because it
receives retrieval's output, and that is a *data* flow. The import graph runs the
other way — retrieval imports the knowledge base, and the knowledge base imports
the verifier, so that a quote can only be grounded against text the retriever
could actually have returned. See "The one boundary that matters", below.

## Layers, and what each is allowed to know

**`model.py` — the domain model.** Knows nothing. Imports `pydantic` and
`enum`. Every other module speaks in these types. The invariant lives here
because this is the only place where it can be enforced structurally rather than
by convention: `Provenance` is a required field on the node kinds that assert
something about the law, so a rule with no provenance is not a rule that
*should* be rejected, it is a `ValidationError`.

**`citations.py` — parsing and treatment classification.** Knows nothing but
text. Deterministic, no model. Turns prose into typed references with character
offsets preserved, and classifies how a later court treated an earlier
authority. Where the patterns fail, the reference lands as `CitationKind.OTHER`
rather than being guessed at: an unparsed citation is a known unknown, a
confidently-misparsed one is a silent error.

**`ingest/` — structuring.** Knows text and the domain model. The only real
decision here is what a paragraph *is*, and the answer is dictated by how
judgments are cited: a paragraph runs from its `[n]` marker to the character
before the next one. Nothing splits on token counts, because a token window has
no relationship to a paragraph and therefore cannot be cited, checked, or quoted
back to a judge. Everything else in the module is defensive work against PDF and
web-extraction artefacts, and the parse *reports* what it did rather than
silently succeeding.

**`kb/` — the knowledge base.** Knows judgments, provisions and the domain
model. Builds three views over one corpus: the citation graph with treatment
edges, a paragraph index for retrieval, and a verifier bound to the same text.
Keeping them in one place is not tidiness. When retrieval and verification are
built from different snapshots, the verifier begins rejecting quotes from
documents the retriever is still serving, and the usual response is to loosen
the verifier — which is precisely backwards.

**`retrieve/` — search.** Knows the index. `hybrid.py` fuses a lexical ranker
with an optional dense one by reciprocal rank fusion and collapses to documents
after fusion; `query.py` decomposes a legal query into a target, an operation
and a temporal anchor using patterns rather than a model, and exposes the
decomposition on the object so a caller can see what was understood.

**`verify/` — the gatekeeper.** Knows the corpus. Small, deterministic,
fail-closed. Checks four things — existence, anchor, quote, treatment — and
refuses rather than annotating.

**`rules/` — the defeasible engine.** Knows rulesets and the domain model.
Evaluates rule trees to proof trees, with exceptions as a distinct node kind.

**`bn/` — credence networks.** Knows CPTs and elicitation records. Exact
inference only, two independent implementations cross-checked against each
other.

**`eval/` — the harness.** Knows task files and systems under test. Imports the
components in order to wrap them, which is the only place a layer is allowed to
depend on several others at once.

**`mcp_server/` — the boundary.** Knows everything, and is the only module
allowed to. It is an adapter; the behaviour it exposes lives in the service
layer and is tested there.

**`authoring/` — the second boundary, and the same shape.** A store, a service,
and two transports (HTTP, and the CLI's `ratio serve`). It defines its own
`Refused` rather than importing the MCP layer's `Refusal`: the two transports
share a *contract* — a refusal is data, returned with its reason and the nearest
thing the system does hold — and not an import. A surface that cannot render a
refusal is a surface that turns a governance control into an error dialog.

**`agents/`, `obs/` — empty or near-empty.** Designed in `docs/07` and `docs/09`,
not built.

## Dependency posture

`pyproject.toml` lists four runtime dependencies: `pydantic`, `networkx`,
`numpy`, `PyYAML`. Everything else is optional and extra-gated:

```
llm  → httpx          the agent layer's only egress
mcp  → mcp            the MCP SDK; the service layer runs without it
otel → opentelemetry-*  tracing; the ledger does not need it
api  → fastapi, uvicorn  the authoring surface's HTTP front door
dev  → pytest, pytest-asyncio
```

The reason is not minimalism for its own sake. A legal knowledge base that
requires a network call to answer *"is this still good law"* has a network call
in the path of an answer a court may have to review, and the call is a component
that can change under you between two runs of the same query. So BM25, the
citation parser, the Bayesian inference and the exact binomial statistics are
all implemented in-tree on `numpy` plus the standard library, and the
deterministic core runs with no network at all.

The dependency that *is* heavy, and is deliberately not taken, is an embedding
model. The published evidence on legal retrieval is unusually clear that this is
not the first thing to reach for — see `docs/03-retrieval-and-verification.md` —
and the offline default is lexical-only. That is a choice, not a limitation
being papered over, and when a dense ranker is added it goes behind the same
`DenseRanker` interface with a benchmark obligation attached.

## The one boundary that matters

The interesting architectural decision in this system is not the layering, it is
that **verification is bound to the same corpus snapshot as retrieval.**

It would be simpler to build the verifier as a standalone service over a live
citation database. It would also create the following failure, which is common
in practice and quiet: retrieval serves a paragraph from document A, the
verifier checks it against a database where A has been superseded, the quote
fails to ground, and the engineer "fixes" it by relaxing the verifier. Six months
later the verifier grounds quotes against text the retriever has never seen,
which means it can ground a quote in a paragraph the system would never cite —
and grounding becomes a rubber stamp.

Binding them means the failure cannot be fixed by loosening anything. If
retrieval and verification disagree, the corpus snapshot is inconsistent, and
that is a defect in the ingest, reported as one.

The second boundary decision is that **the MCP service layer is not the MCP
transport.** `mcp_server/server.py` builds a `RatioService` dataclass that
answers in plain dictionaries, and `build_server` adapts it to the SDK
lazily, inside the function, so that importing the module does not require the
SDK. The consequence is that the entire tool surface — payload shapes, refusals,
which contexts travel with which answer — is testable without the SDK installed,
which is what `tests/test_mcp.py` does.

## Failure modes, and how the architecture answers each

**Fabrication.** Answered structurally: the verifier checks existence, anchor,
quote and treatment; the retriever refuses below a coverage floor; the rule
engine abstains on gaps; the Bayesian layer is structurally incapable of
returning a verdict, only a distribution. There is no path in the deterministic
core that produces an authoritative-sounding output with nothing behind it.

**Silent drift between the encoding and the expert's belief.** Answered by
making the approval a hash over the artefact's canonical payload, so editing an
approved ruleset invalidates its approval rather than inheriting it, and by
generating cases from the ruleset so the expert adjudicates *outcomes* rather
than re-reading structure. `rules/generate_cases.py` reports inert leaves (a
leaf whose value never changes the conclusion) and unexercised exceptions,
neither of which is visible in a rendering of the rule tree.

**Stale authority.** Answered by computing status from treatment edges at a
point in time, and by carrying the evidence for the status rather than the
status alone.

**Confident wrong numbers.** Answered by reporting every rate with its
baseline, its denominator and its interval, and by treating abstention as a
category rather than a failure.

**Unreconstructable decisions.** Designed, not built: an append-only hash-chained
ledger in `obs/`. Without it, the answer to "why did the system say that in
March" is a guess.

## What is not built

`agents/`, `obs/` and `infra/` are empty directories. The MCP service
layer is built and tested; the agent roles that would call it are specified in
`docs/07-agent-orchestration-mcp.md`. The authoring surface in `ui/` is built and
served by `ratio serve`, and it sits on the authoring service in
`src/ratio/authoring/`; `docs/06-expert-authoring-surface.md` describes it and
names what it still cannot do — chiefly that the citation queue can be read and
not worked, that the service has no identity layer, and that the lawyer-facing
job (a) has no surface at all. The ruleset ships `status: draft` — there is a
real approval mechanism and nothing has been through it yet, which every output
reports.

## Repository map

```
src/ratio/
  model.py           domain model; the invariant lives here
  citations.py       neutral/reported/statutory citation parsing, treatment classification
  ingest/            structuring, source adapters, fixture loading
  kb/                stores and the citation graph
  retrieve/          BM25, dense interface, hybrid fusion, query understanding
  verify/            grounding and citation verification
  rules/             defeasible engine and ruleset loading
  authoring/         store, service and HTTP adapter behind the expert surface
  bn/                credence networks, exact inference, sensitivity
  agents/            orchestration and roles            [empty]
  mcp_server/        Model Context Protocol tools and resources
  obs/               tracing and the audit ledger       [empty]
docs/                the design, argument by argument
rulesets/            encoded legal tests, with provenance
data/fixtures/       synthetic judgments, clearly labelled
eval/                harness, metrics, statistics, task suites
ui/                  React + TypeScript expert authoring surface, served by `ratio serve`
infra/               AWS deployment skeleton               [empty]
```
