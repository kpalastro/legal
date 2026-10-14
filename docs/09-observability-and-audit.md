# Observability and the audit ledger

## Why this document exists, and why it is not about performance

Most systems treat tracing as a performance tool. Something is slow, you open a
trace, you find the span, you close the trace. That is not what this layer is for
here, and building it as though it were would produce a system that is well
instrumented and still unauditable.

The question a reviewer asks about a legal answer is not "was it slow". It is:

> On what authority, on whose sign-off, from which revision of the corpus and
> which revision of the ruleset, did the system say that — and what did it
> decline to say?

Every one of those clauses is a fact the system already holds at the moment it
answers, and every one of them is lost the moment the answer leaves the process
unless something writes it down deliberately. So observability here is an
accountability requirement that happens to be implemented with tracing
machinery, not a performance discipline that happens to produce useful records.

There is a second reason, specific to this corpus. The knowledge base changes.
Judgments are added, treatment edges are recomputed, a ruleset moves from
`draft` to `approved` under a new approval hash, a provision is re-compiled as
at a later date. An answer that was correct in March can be wrong in September
because a Full Court decision entered the corpus in June, and nothing in the
answer itself will show that. The trace is the only place where "which corpus
produced this" is recorded.

## The deterministic core has no model in it

This is the constraint that shapes the design, and it is easy to get wrong.

`ratio` is built so that the legal core — the knowledge base, BM25 retrieval,
the citation graph, the defeasible rule engine, the credence network, the
evaluator — runs with no model call and no network access at all. That is the
dependency posture recorded in `pyproject.toml`, and it is enforced by the way
the packages are written: `ratio/obs/` will import nothing from the model layer
to do its job, and nothing in the core imports the tracing layer to run.

The practical consequence for instrumentation is that **a model call is not
present on every request**, and a trace design that assumes one produces spans
with holes in exactly the runs that matter most. The deterministic path is the
path a reviewer is most likely to be asked to account for, because it is the
path whose output carries no model's name on it and therefore looks, wrongly,
like it needs no accounting.

So the trace has two kinds of span, and they are not the same shape:

* **deterministic spans**, which carry inputs, outputs and digests, and which
  will be present on every request;
* **model spans**, which follow the OpenTelemetry `gen_ai` semantic conventions,
  and which appear only where the agent layer (`ratio/agents/`) is in use.

The `gen_ai` conventions name the model facing attributes: `gen_ai.system`, the
operation under `gen_ai.operation.name`, the request under `gen_ai.request.model`
and `gen_ai.request.max_tokens`, the reply under `gen_ai.response.model` and
`gen_ai.response.finish_reasons`, and consumption under
`gen_ai.usage.input_tokens` and `gen_ai.usage.output_tokens`. Parts of that
convention set are still marked experimental, which is a reason to keep the
attribute names in one module rather than scattered through the code — the names
will move, and the ledger's *content* must not move with them.

One rule follows from the invariant, and it is not negotiable: **a model span
must never be the only record of an assertion.** Whatever a model contributed
has to appear in the deterministic trace as a verified claim — a quotation that
resolved to a span, a citation that resolved to an ingested judgment — or it has
not happened as far as the system is concerned.

## The span tree

The proposal, which is not yet implemented:

```
ratio.request                          one per user request
  case_id                              caller-supplied, or absent
  as_at                                point-in-time, echoed not inferred
  corpus.digest                        sha256 over the loaded corpus
  ruleset.id / .version / .approval_hash
  principal                            who asked

  ├── corpus.load                      documents read, parse warnings, unresolved cites
  ├── retrieve.search                  query, filters, coverage, n returned
  │     └── (no model span unless reranking is model-backed)
  ├── verify.claim                     per quote: resolved / grounded / failed
  ├── rules.evaluate                   conclusion, defeaters_fired, gaps
  │     └── rules.node.<id>            per node, for a proof rendered live in a UI
  ├── bn.query                         target, evidence, posterior, sensitivities
  └── gen_ai.*                         only where ratio/agents/ is in use
        agent.role                     planner | retriever | analyst | verifier
        agent.abstained                true when the role declined
```

Three of those attributes carry more weight than the rest.

`corpus.digest` is a digest over the loaded corpus rather than over the
directory it was read from. A file mtime and a path do not tell a reviewer what
the system read; a digest does, and it is what makes "this answer was correct as
at the corpus of 3 June" a checkable statement rather than a claim.

`ruleset.approval_hash` is the same hash `RuleSet.current_approval` compares
against. If it is absent, the answer was produced by an unapproved ruleset and
the span must say so rather than substituting the ruleset version, which will
still be `0.3.0` after somebody edits a predicate value.

`as_at` is echoed, never inferred. It is a request parameter throughout — the
CLI's `--as-at`, the `authority_status` tool's `as_at` argument — precisely so
that it cannot be defaulted to "now" at the point of logging, which is the one
way a point-in-time answer becomes impossible to reproduce.

It is also *not* uniform across the surface, and a ledger that assumes a
computation date on every record will be wrong about some of them. The MCP
`authority_status` **tool** takes `as_at`; the `ratio://authority/{key}/status`
**resource** takes no such argument and therefore always computes as at now,
reporting `as_at: null` in its payload. That is a defensible shape — a resource
URI names a thing, and a point-in-time status is not a different thing — but it
means an answer can reach a user from a path whose point-in-time basis is
"whenever it was asked", and the ledger has to record `null` honestly rather than
substituting the record's own timestamp, which would look like a computation date
and would be a different fact.

## The audit ledger

The trace tells you what happened during a request. The ledger is a different
artefact: a durable, append-only record of what the system asserted, intended to
be readable by someone who was not there and does not trust the process that
produced it.

### Append-only, and hash-chained

Each record carries the digest of the record before it, and its own digest
covers its content together with that predecessor digest. A record cannot be
edited without breaking every record after it, and a record cannot be deleted
without leaving a gap that the chain reports.

The alternative — a mutable table with an `updated_at` column, which is what a
service backed by a relational database naturally grows — is not merely weaker.
It moves the question from "what does the record say" to "what did the record say
before, and who changed it, and does the audit log of the audit table still
exist". For a ledger of legal advice that is a losing position, because the
party with the strongest incentive to alter a record is usually the party with
write access to the database.

The cost is real and should be stated rather than discovered:

* **Nothing is ever corrected in place.** A record that turns out to be wrong is
  superseded by a later record that says so. This means the ledger is not a
  source of truth about the law; it is a source of truth about what the system
  said.
* **The chain is a single sequence**, so writes serialise. A deployment that
  answers many requests concurrently needs either a serialising writer or a
  design that permits several chains — and several chains with a periodic
  anchor is a materially more complicated thing to reason about.
* **Retention is not free.** A ledger that grows forever is a liability of its
  own; see the retention note in `docs/10-aws-deployment-security.md`.

### What an execution receipt holds

One record per request that produced output:

| Field | Why it is there |
|---|---|
| `request_id` | Joins the ledger to the trace and to the caller |
| `principal` | An opaque id. Who asked, not who they are |
| `as_at` | The point-in-time basis, or `null` where the path had none — see above; never the record's own timestamp |
| `corpus.digest` | Which revision of the corpus answered |
| `ruleset.id`, `.version`, `.approval_hash` | Which encoding, signed by whom, and whether the signature still matches |
| `tools_called` | The sequence, with arguments — the service layer holds no per-client state, so this reconstructs the request |
| `evidence` | Citations and paragraph anchors returned, with the quotes that were verified |
| `abstention` | Present **only** when the system declined, naming the reason from the closed set and, where there is one, the missing fact field |
| `output_digest` | A digest of what was rendered to the user, so the ledger does not itself have to hold it |

The `evidence` field holds anchors, not judgement. `[2019] FCAFC 77 at [7]` and
the quote that resolved against it are facts about what the system read. Any
prose the system generated around them is not, and belongs behind
`output_digest`, not in the ledger.

### Abstentions are logged, with their reason

This is the requirement most likely to be quietly dropped, because it looks like
a feature rather than a control.

A ledger that records only answers cannot show that the system declined. It will
therefore show a system that always has something to say, and a reviewer
comparing it against a system that confabulates will see two systems that both
answer every question. The abstention path is the one this whole project is
organised around — the retrieval coverage gate, the rule engine's `undetermined`
with its gap list, the MCP `Refusal` — and if it is not in the ledger, the
ledger's most important number is missing.

So an abstention is a record like any other, carrying its reason. The reasons the
system can produce are a closed set, and the ledger should hold the value rather
than a free-text reconstruction of it:

* from retrieval — the coverage figure and threshold when the gate declined
  (`below_retrieval_coverage`), or `nothing_indexed_matches` when no paragraph
  contained any term of the query at all;
* from the rule engine — `undetermined` with the gap list naming the fact fields
  that were absent (`ProofTree.gaps`);
* from the MCP service layer — `authority_not_in_corpus`,
  `provision_not_in_corpus`, `unknown_ruleset`, `no_rulesets`,
  `verifier_unavailable`.

Those are already first-class values in the running code: `Refusal` in
`ratio/mcp_server/server.py` carries `reason` and a `detail` dict, and the
`gaps` field on `ProofTree` names fields rather than nodes. The ledger's job is
to preserve them rather than to log the call as a failure.

One asymmetry in that layer is worth recording, because a ledger built on the
wrong assumption about it will drop exactly the records it exists to keep. The
service layer **signals** a refusal by raising `Refusal`; it does not return
refusals as data. The conversion to a payload happens only at the transport
boundary, in `_resolve` inside `build_server`, so a direct caller — including the
tests — gets an exception. That is the right shape (the service layer is a plain
function library over the corpus and should behave like one), but it means an
instrumentation layer wrapped around `RatioService` must catch `Refusal`, not
inspect a return value. Instrumenting only the SDK adapter would leave every
refusal in the process unlogged, which is the precise failure described above.

### What must not go in it

* **No client-identifying material beyond an opaque principal id.** No names, no
  matter numbers, no counterparty. The ledger has to be readable by compliance
  without becoming a second copy of the client file.
* **No privileged facts.** The fact base passed to `apply_ruleset` is privileged
  material. The ledger records that a rule was evaluated, which ruleset revision
  was used, and the conclusion; it does not record the facts, which are
  recoverable from the matter file if a matter file is what a reviewer needs.
* **No model prompts or completions verbatim**, for the same reason, and because
  a model's context window will routinely contain material that came from
  somewhere the ledger has no licence to hold.

And the constraint that has to be said out loud before anyone builds this: **a
ledger of legal advice is itself discoverable.** An append-only record that the
system declined to answer a question, and why, and on what facts, is a document
that a court or a regulator can compel. That is an argument for keeping the
ledger small and its contents factual, and it is an argument against the tempting
design in which the ledger doubles as the application's debug log.

## What is not built

`ratio/obs/` is an **empty package**. The `otel` extra is declared in
`pyproject.toml`; nothing in `src/` imports OpenTelemetry, and there is no
tracer provider, no span emitter, no ledger writer and no hash chain. The file
`src/ratio/obs/__init__.py` is zero bytes. Everything above is a design.

Two smaller notes in the same register, because they are easy to misread from
the README's feature list:

* The `ProofTree.render()` docstring describes its output as "for CLI and for the
  audit ledger". The CLI half is real. There is no ledger to render into.
* The MCP service layer holds no per-client session state and no notion of "the
  current matter", which is what an honest ledger needs. That is a property it
  *happens to have*, not one the repository enforces: nothing here pins a
  protocol revision, no version string appears anywhere in `src/`, and
  `pyproject.toml` requires only `mcp>=1.2`. If a future revision of the protocol
  introduces server-side session state, nothing in this repository will notice,
  and the statelessness the ledger depends on would be a property of the deployed
  configuration rather than of the code. Worth recording as a standing risk
  rather than a current feature.
* Refusals reach the transport boundary as payloads, but only through `_resolve`,
  which is marked `# pragma: no cover` and is therefore untested. The reasons are
  already in the payloads; nothing logs them.

The order these should be built in, if it matters: the corpus digest and the
ruleset approval hash first, because they are the two facts that cannot be
reconstructed after the fact and the two the rest of the ledger is anchored to;
the span tree second; the hash chain third, since a chained ledger over records
whose content is wrong is worse than an unchained one over records that are
right.
