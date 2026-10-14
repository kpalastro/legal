# 07 — Orchestration, agents, and the MCP surface

This document describes the agent layer and the Model Context Protocol surface
that a client talks to. It is written in two registers on purpose: the MCP
service layer in `ratio/mcp_server/server.py` is **built and tested**, and the
orchestration described in the first half of this document is **designed and not
built**. The line between them is marked explicitly wherever it occurs, because
the two are easy to conflate when the design is written down well enough.

---

## The roles, and why the verifier is not the analyst

The intended decomposition is four roles.

**The planner** decides what the question is. In legal work this is not a
formality. "Is this acquisition notifiable?" is not one question; it decomposes
into a rule application over facts the user has not supplied, a retrieval
question about the leading authority on one limb of the test, and a
point-in-time question about whether that authority is still good law. The
planner's job is to produce that decomposition, and — more importantly — to
notice when the question cannot be decomposed because a fact is missing, which
is the point at which the whole run should terminate in a refusal rather than
proceed.

**The retriever** finds the paragraphs that bear on each sub-question. It is a
thin wrapper over `HybridRetriever`, and its most consequential behaviour is the
abstention gate described in doc 08 — a retriever that always returns its best
hit is confabulation with a search index in front of it.

**The analyst** reads what the retriever found and states what follows. In this
project the default analyst is **deterministic and model-free**: it is the
defeasible rule engine and the credence network, applied to the fact base. See
"the deterministic core is the default analyst" below for why that is a design
position rather than an implementation convenience.

**The adversarial verifier** takes a claim, the quotations it rests on and the
citations it names, and checks each quotation against the paragraph cited. It
does not evaluate whether the claim is legally correct. It answers one question:
*do these words appear in that span, and does that citation resolve to a
judgment in this corpus?*

The verifier being a **separate role** rather than a second pass by the analyst
is the load-bearing design decision in this section. A model asked to check its
own work checks it against the same context that produced the error. If the
analyst reasoned its way to "Yarrow requires an enforceable arrangement", a
self-check runs over the reasoning that produced the mistake and finds it
coherent — because it was. The verifier must therefore receive the claim, the
quotes and the citations as **inputs**, with no access to the reasoning that
produced them, so that the only operation available to it is comparison of a
quotation against a span of text. That is a mechanical operation, and
`ratio/verify/grounding.py` performs it without a model at all.

The asymmetry is worth stating plainly: a verifier that is given the analyst's
reasoning can only confirm it; a verifier that is given only the outputs can
contradict it.

## Abstention is a first-class agent outcome

An agent loop with no abstention path will always produce an answer, because
producing an answer is what a loop does. Given a tool surface, a step budget and
an instruction to respond, a model will respond. If the corpus does not contain
the answer, the response will be assembled from whatever the corpus does
contain, and it will read exactly like a response assembled from the answer.

So the orchestrator must be able to terminate in a refusal that carries a
**reason from a small closed set**. The closed set matters: a free-text refusal
is not machine-checkable, cannot be aggregated over a suite of runs, and cannot
be logged as a category. A refusal that says "I could not answer" is a shrug; a
refusal that says `below_retrieval_coverage` with the coverage figure attached
is a finding.

The reason set is defined by the service layer, which is built. `Refusal` in
`ratio/mcp_server/server.py` is raised with one of the following `reason`
values — this list is the complete set in the code, and an agent layer may add
to it but must not invent reasons the service cannot produce:

| Reason | Raised when |
|---|---|
| `authority_not_in_corpus` | The client named a judgment, or a case name, that the corpus neither holds nor evidences |
| `provision_not_in_corpus` | The client named a statutory provision the corpus does not hold |
| `below_retrieval_coverage` | The best-matching paragraph accounts for too little of the question's distinctive vocabulary |
| `nothing_indexed_matches` | No indexed paragraph contains any term of the query at all |
| `unknown_ruleset` | The client named a ruleset that is not loaded; the result lists the ones that are |
| `no_rulesets` | The service holds no rulesets, so there is nothing to apply |
| `ruleset_failed_validation` | The ruleset failed validation and will not be applied — in practice, a rule edited after an expert signed it off. The validation issues travel verbatim in the detail |
| `verifier_unavailable` | The knowledge base was built without a verifier, so nothing can be checked |

`ruleset_failed_validation` is worth calling out because it was the one case
that had *no* refusal, and it is the case where a refusal matters most. A ruleset
edited after sign-off makes `RuleEngine` raise, and that exception used to escape
`apply_rules` and reach a model client as a transport failure — so the single
message a client most needs to relay ("the rule you are about to rely on has been
altered since it was approved") was the one it could not read. It now travels as
a refusal like any other, with the validation issues attached.

Two of these are about *inputs* rather than about the corpus — a named authority
that does not exist, a named ruleset that does not exist. They are refusals
rather than errors for the reason given in the next section: the caller should
be told, not crashed.

## The deterministic core is the default analyst

The rules engine, the credence network, the retriever and the verifier are all
model-free, and all are covered by tests. A model in the loop must add something
measurable or it is a liability, and the measurement has to be against the
harness in doc 08 — a model-backed analyst that cannot be shown to beat the
deterministic one on the task suites is a component that has been added because
it seemed natural rather than because it helped.

The reason to hold this line firmly in a legal system specifically: **the model
is the only component in the architecture that can fabricate.** The rule engine
can be wrong, and the credence network can be wrong, but neither can produce a
citation to a case that does not exist. Every other component's failure mode is
an incorrect answer over material that is really there; the model's failure mode
is material that is not there at all, presented in the register of authority.
That is the failure courts have sanctioned practitioners for.

A model belongs in the loop where the task is genuinely open-ended — reading an
unstructured fact pattern and proposing a fact base, for instance, where the
output is then checked by the verifier and applied by the engine. It does not
belong where a deterministic implementation exists and is tested.

## The MCP surface

### The protocol revision, and statelessness

The service targets the stateless revision of the protocol (`2026-07-28`), in
which no session state may be assumed between calls. This is a design
commitment, not something the repository enforces: `pyproject.toml` requires
only `mcp>=1.2` and nothing pins a revision. What *is* enforced is the property
that makes it possible — `RatioService` holds a corpus and a set of rulesets and
nothing else. It has no notion of "the current matter", no conversation history
and no per-client cache. Every call that depends on context takes that context
as an argument.

The reason is the audit's, not the protocol's. A legal answer is only meaningful
relative to a date and a fact base. A system that remembers "the case" produces
answers whose provenance cannot be reconstructed from the call that produced
them. In a deployment that logs every tool call — which doc 09 specifies — that
is the difference between an auditable transcript and a pile of unattributable
output. So `search` takes `as_at`, `courts`, `binding_only` and `require_good_law`
as explicit arguments, `authority_status` takes `as_at`, `apply_rules` takes the
whole fact base, and **every result echoes the context it was computed under**.

### Resource-first, tool-second

The split is not arbitrary and it is not about capability. A tool is a *verb the
model chooses*; a resource is a *thing the model can look at*. This corpus is a
body of authority — it is read, not operated. So the judgment text, the treatment
history, the provision text and the encoded ruleset are all exposed as
resources, addressable by a URI naming exactly one object.

The five resource templates, with what each returns:

| URI | Returns |
|---|---|
| `ratio://corpus` | Counts of judgments, provisions and indexed paragraphs; the loaded rulesets and networks; case names the corpus resolves; loader warnings. Carries `synthetic` and `has_real_judgments` at the top level, unconditionally. |
| `ratio://judgment/{key}` | One judgment as an ordered list of paragraphs, each with judge, number, `reasons_role` and text. The paragraph is the unit because the paragraph is the anchor every assertion in the system points at. |
| `ratio://provision/{key}` | One provision: act title, section, text. |
| `ratio://authority/{key}/status` | The authority status of a judgment, the caveat that qualifies it, and the three treatment buckets. |
| `ratio://ruleset/{id}` | The encoded rule: version, approval state, every node with its kind, predicate and provenance. |

A client that can read these five can answer most questions about *what the
corpus says* without invoking a tool at all. That is intended: it means the
cheapest thing for a model to do is also the safest thing, because reading a
resource cannot produce a fabricated citation.

The four tools exist only where something genuinely needs computing:

| Tool | Computes | Refuses when |
|---|---|---|
| `search_authorities` | Ranks paragraphs bearing on a query, with the coverage of each | The corpus does not address the query (`below_retrieval_coverage`), or nothing matches any term (`nothing_indexed_matches`) |
| `verify_claim` | Checks a claim's quotations and citations against the corpus | No verifier is configured (`verifier_unavailable`) |
| `apply_ruleset` | Applies an encoded rule to a fact base | The named ruleset is unknown (`unknown_ruleset`) or none is loaded (`no_rulesets`) |
| `authority_status` | Whether an authority is still good law, optionally as at a date | The authority is not in the corpus (`authority_not_in_corpus`) |

Note what `search_authorities` returns and what it withholds. Each result
carries its `coverage` and its `authority_caveat`, but **not** its fused score.
The fused score is not a confidence and cannot be read as one — see doc 08 for
why it is near-constant by construction. Coverage is the quantity that was
actually calibrated, so coverage is the quantity that is reported. Returning a
score that looks like a probability and is not one would be the transport
reintroducing exactly the confusion the retrieval gate exists to remove.

`verify_claim` similarly reports no confidence. A quotation either resolves to
the cited span or it does not.

### Refusal as data

`_resolve` in the adapter turns a `Refusal` into a result payload rather than a
protocol error:

```json
{"refused": true, "reason": "below_retrieval_coverage",
 "message": "...", "query": "...", "coverage": 0.26,
 "threshold": 0.5, "nearest": "..."}
```

This is the same argument as `Refusal`'s docstring, generalised: reporting a
refusal as a failure of the *call* is misleading in the same way reporting an
abstention as a wrong answer is. The system did the right thing and the
transport called it a fault.

The practical consequence is about what a model client can do with each shape.
A transport error surfaces to the model as a failure it may retry, swallow, or
work around — and a model working around a failed tool call is a model about to
answer from its own priors, which for a legal question means from memory, which
means potentially from a case that does not exist. A payload with
`refused: true` and a reason is something the client can *report*. The model can
tell its user "the corpus does not address this question; the nearest material
is X at 26% coverage". That is a useful answer. It is also the only shape in
which the refusal survives contact with a chat interface.

### The three buckets must reach the client

`authority_status` returns the status **and** the three buckets that produced
it:

- `evidence` — treatment that was counted;
- `advisory_evidence` — treatment recorded but below the confidence threshold,
  so it did not move the status;
- `hierarchy_blocked` — treatment recorded but from a court that cannot diminish
  this authority, so it was not allowed to.

Every edge in every bucket carries the quote that produced it, the judgment it
came from and the paragraph number.

A status alone is a conclusion the client cannot audit. "This is good law" is
not checkable; "this is good law, and here are the two decisions that support
it, the one recorded instance of doubt that we did not count because it comes
from a single judge of an inferior court, and the words that produced each" is
checkable, and it is also the thing a lawyer needs in order to disagree. A
system that silently dropped the suppressed treatment would present a
contested authority as settled — and would do so in the same output shape as an
authority about which nothing was ever said. That is the failure mode: not a
wrong answer, but an inability to distinguish *we found nothing* from *we found
something and set it aside*.

The CLI's rendering conventions carry the same distinction visually — `?` for
advisory treatment, `!` for hierarchy-blocked — and doc 06 covers the review
workflow built on these buckets.

## What is not built

**`ratio/agents/` is an empty package.** There is no planner, no orchestrator, no
role definitions, no step budget and no termination condition. What exists is
the service layer those roles would call: `RatioService` in
`ratio/mcp_server/server.py`, with the eight refusal reasons above, the five
resource templates and the four tools, all covered by `tests/test_mcp.py`.

Concretely, what is missing between the design above and a running agent layer:

- an orchestrator with an explicit refusal path and a step budget;
- role prompts and their interfaces, for the model-backed variants of the
  planner and the fact-extraction step;
- the wiring that turns a `Refusal` payload into a terminating orchestrator state
  rather than a tool result the model may retry;
- an evaluation of a model-backed analyst against the deterministic one, which is
  the measurement that would justify putting a model in the loop at all.

The MCP adapter itself (`build_server`) is written but not exercised by the test
suite, because it requires the `mcp` extra. The SDK import is inside the function
so that the deterministic core, the evaluator and the tests run without it —
which is the repository's dependency posture, and a top-level import there would
quietly reverse it. `RatioService` is where the behaviour lives, and
`RatioService` is tested.
