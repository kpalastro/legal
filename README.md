# RATIO

**A citation-grounded knowledge base of existing judgments, and an executable encoding of the legal judgement drawn from them.**

Built for Australian regulatory work — foreign investment (FIRB), continuous
disclosure, competition — in a regulated professional-services setting.

---

## The problem this is built around

Two failure modes dominate applied AI in legal work, and they are opposites.

**The first is fabrication.** A system produces a citation that does not exist,
or that exists but says the opposite of what it is cited for. This is not a
hypothetical: courts in several jurisdictions have now sanctioned practitioners
for filing submissions containing machine-generated citations to non-existent
authorities. A legal knowledge base whose retrieval is good but whose claims are
unverifiable is worse than no system, because it is fluent.

**The second is the opposite, and less discussed.** Systems that *are* careful
about retrieval still cannot answer the question a lawyer actually asks, which
is not "what documents mention these words" but "what is the rule, is it still
good law, and does it apply to my facts". That requires encoding legal
judgement — and every attempt to encode it either drowns the expert in
formalism, or quietly loses the exceptions, or produces a number nobody can
interrogate.

RATIO is an attempt to take both seriously at once.

## The invariant

Everything in this repository exists to enforce one rule:

> **Nothing is asserted about the law that is not anchored to a span of an
> existing judgment or a provision of a statute — and nothing is encoded as a
> machine-evaluable rule that does not carry a provenance chain back to the
> expert who approved it.**

Concretely, that means:

| Mechanism | Where | What it prevents |
|---|---|---|
| Paragraph-anchored corpus — never token-window chunking | `ratio/ingest/structure.py` | Cites that cannot be checked because the anchor was destroyed |
| Span-exact quote verification | `ratio/verify/grounding.py` | Fabricated or paraphrased quotes passing as quotes |
| Treatment edges + computed authority status | `ratio/kb/graph.py` | Relying on a case that a Full Court later overruled |
| Defeasible exceptions as a distinct node kind | `ratio/rules/engine.py` | Encoding "...unless..." as an AND-branch, which is a different rule |
| Elicitation records on every CPT cell | `ratio/bn/network.py` | Opinion laundered through arithmetic |
| Hash-bound expert approvals | `ratio/model.py` | Editing an approved rule and silently inheriting the approval |
| Trace + audit ledger | `ratio/obs/` | Being unable to reconstruct why the system said what it said |

## Architecture

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

## Quickstart

```bash
uv venv && uv pip install -e ".[dev]"

# The corpus and ruleset directories default to data/fixtures and rulesets;
# pass --fixtures / --rulesets before the subcommand to point elsewhere.
ratio ingest                      # what the corpus contains, and what is wrong with it
ratio stats                       # one-screen health check

# Is an authority still good law? Accepts a citation or a case name, and says
# which case it took a name to mean.
ratio authority "FCAFC:2019:77"
ratio authority "Bramblewood" --as-at 2021-06-01

# Retrieve paragraphs bearing on a question.
ratio ask "Is the acquisition of Example Holdings by a foreign government investor notifiable?"

# Generate the decision-table review sheet an SME adjudicates.
ratio cases --ruleset firb.notifiable_action --limit 40

# Score the system against the task suites (default: eval/tasks).
ratio eval

# The expert authoring surface: inspect, edit and sign the rulebook in a
# browser. Needs the api extra. Edits land in .ratio-workspace/, never in
# rulesets/, unless you pass --in-place.
uv pip install -e ".[api]"
cd ui && npm install && npm run build && cd ..
ratio serve                       # http://127.0.0.1:8000

# For frontend work, run the two separately; vite proxies /api to the service.
#   ratio serve
#   cd ui && npm run dev            # http://localhost:5173, hot reload
```

Everything above runs offline with no API key, and every command is exercised by
the test suite. The model-backed agent layer is opt-in (`.[llm]`) and is the
*only* part that would require network access.

The AWS deployment is a design, not a command — `infra/` is an empty directory
(see `docs/10-aws-deployment-security.md`).

## What runs today, and what does not

This repository is a **working scaffold plus a design**, not a production
system. Being precise about the line:

**Runs, tested:**
- Citation parsing and treatment classification over judgment prose
- Paragraph-anchored structuring of judgment text
- Citation graph construction, treatment propagation, authority-status computation
- BM25 retrieval with authority filters; deterministic hybrid fusion
- Span-exact quote verification and citation existence checks
- Defeasible rule engine with proof trees and structural/governance validation
- Bayesian network construction, exact inference (two independent implementations,
  cross-checked), and sensitivity analysis
- The eval harness: abstention as a first-class outcome, macro-F1, baselines,
  exact (Clopper–Pearson) confidence intervals, paired significance testing
- The MCP service layer: five resource templates and four tools, with
  refusals-as-data, tested without the SDK installed (`tests/test_mcp.py`)
- The expert authoring surface: the authoring store, service and HTTP layer
  (`ratio/authoring/`), tested without a web framework installed
  (`tests/test_authoring.py`) and with one (`tests/test_authoring_api.py`);
  the React surface in `ui/` sits on it

**Designed, specified, not built here:**
- Working the citation queue: nothing writes `AuthorityStatusRecord.confirmed_by`,
  so the authoring surface can read a treatment edge and cannot confirm one — its
  buttons are present and disabled, and say why (`docs/11`, checkpoint G2)
- The lawyer-facing question-answering surface (`docs/06`, job (a)); the
  authoring surface is not it, and deliberately shows a lawyer none of it
- An identity layer for the authoring service: an editor's name and an approver's
  name are free text today, which is adequate for one operator on loopback and is
  not adequate for anything else (`docs/10-aws-deployment-security.md`)
- OpenTelemetry tracing and the append-only audit ledger — `ratio/obs/` is an
  empty package; the design is `docs/09-observability-and-audit.md`
- Live corpus ingestion of any kind (adapter interface only — see
  `docs/02-judgment-knowledge-base.md` for licensing constraints that make this
  a legal question before it is an engineering one, and for why AustLII's terms
  prohibit using its content for AI training or embedding)
- Dense retrieval (interface + offline fallback; no embedding model bundled)
- AWS deployment — `infra/` is an empty directory; the design is
  `docs/10-aws-deployment-security.md`
- Model-backed agent roles — `ratio/agents/` is an empty package; the design is
  `docs/07-agent-orchestration-mcp.md`, and the default analyst is deterministic

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
  bn/                credence networks, exact inference, sensitivity
  agents/            orchestration and roles
  mcp_server/        Model Context Protocol tools and resources
  authoring/         the expert authoring surface: store, service, HTTP transport
  obs/               tracing and the audit ledger
docs/                the design, argument by argument
rulesets/            encoded legal tests, with provenance
data/fixtures/       synthetic judgments (clearly labelled — see below)
eval/                harness, metrics, statistics
eval/tasks/          the task suites `ratio eval` scores against
ui/                  React + TypeScript expert authoring surface (built by `npm run build`)
infra/               AWS deployment skeleton
```

## A note on the fixture corpus

`data/fixtures/` contains **synthetic judgments written for this repository**.
They use realistic citation *formats* and realistic statutory references, but
the cases, parties and reasoning are invented, and every document carries
`synthetic: true`, which the system surfaces in all output.

They exist so the architecture can be tested end to end offline. They must
never be presented as authority, and the fixtures deliberately do not resemble
any real proceeding. Before this system is used for anything, the fixtures are
replaced with licensed real sources; the loader refuses mixed corpora unless
explicitly overridden, so that a synthetic document cannot silently sit
alongside a real one in a production index.

## Design documents

| Doc | Subject |
|---|---|
| `docs/00-overview.md` | The problem, the approach, the bet being made |
| `docs/01-architecture.md` | Layering, boundaries, dependency posture, failure modes |
| `docs/02-judgment-knowledge-base.md` | Sources, document model, citation graph, precedent |
| `docs/03-retrieval-and-verification.md` | Why legal retrieval is different; grounding |
| `docs/04-rule-encoding.md` | Defeasibility, provenance, drift, point-in-time |
| `docs/05-bayesian-layer.md` | Probabilistic reasoning over legal standards |
| `docs/06-expert-authoring-surface.md` | Making the expert the author |
| `docs/07-agent-orchestration-mcp.md` | Roles, MCP tool surface, adversarial verification |
| `docs/08-evaluation-harness.md` | Making quality measurable and regressions detectable |
| `docs/09-observability-and-audit.md` | OTel traces, audit ledger |
| `docs/10-aws-deployment-security.md` | Deployment, identity, data handling |
| `docs/11-governance-assurance.md` | ISO 42001 and the Australian guardrails, mapped |
| `docs/12-roadmap-and-risks.md` | What I would build next, and what would make me stop |
