# 00 — Overview

## The problem, stated as two failures rather than one

Applied AI in legal work fails in two directions, and the literature is heavily
weighted toward the first.

**It fabricates.** *Mata v Avianca* (S.D.N.Y. 2023) produced six non-existent
decisions with invented quotes, docket numbers and judges, in a filed
submission; sanctions followed. Stanford RegLab's preregistered audit of the
leading commercial legal research tools found hallucination rates between
roughly 17% and 33%, against vendors' claims of being hallucination-free. In
Australia, *JNE24 v Minister for Immigration and Citizenship* [2025] FedCFamC2G
1314 saw a practitioner personally pay costs after citing four non-existent
authorities — and the court's point was sharper than "check your citations":
confirming a case is not fictitious is *not enough*, because a real case cited
for a proposition it does not support is the same defect wearing a valid
citation. Existence checking is necessary and nowhere near sufficient.

**It is precise but useless.** The second failure gets less attention because it
does not produce a sanctionable event. A legal research tool can retrieve the
right five documents and still fail the question, because the question a lawyer
asks is not "what documents mention these words" but "what is the rule, is it
still good law, and does it apply to my facts". Answering that requires
*encoding* legal judgement, and every attempt to do so lands in one of three
ditches: the encoding drowns the expert in formalism so the expert disengages
and the encoding drifts from what they believed they approved; the encoding
quietly loses the exceptions, so it is confidently wrong in exactly the cases
that matter; or it produces a number — 0.73 — that no lawyer can interrogate and
no court can review.

## The invariant

Everything in this repository exists to enforce one sentence:

> **Nothing is asserted about the law that is not anchored to a span of an
> existing judgment or a provision of a statute — and nothing is encoded as a
> machine-evaluable rule that does not carry a provenance chain back to the
> expert who approved it.**

The invariant is deliberately two-sided. Anchoring alone gives you a citator
with no reasoning: every claim traceable, nothing concluded. Encoding alone
gives you a rules engine with no accountability: conclusions whose authority
cannot be inspected. The system is only interesting where the two meet, and the
domain model in `src/ratio/model.py` is written so that they cannot come apart.
`Provenance` is not decoration there; it is the join between what the courts
actually said and what we tell a machine to conclude.

The invariant has teeth in specific, testable places:

| Mechanism | Where | The failure it makes impossible |
|---|---|---|
| Paragraph-anchored corpus — never token-window chunking | `ingest/structure.py` | A citation that cannot be checked because the anchor was destroyed |
| Span-exact quote verification | `verify/grounding.py` | A paraphrase passing as a quotation |
| Treatment edges with a computed authority status | `kb/graph.py` | Relying on a case a Full Court later overruled |
| EXCEPTION as a distinct node kind from a conjunct | `rules/engine.py` | Encoding "...unless..." as an AND-branch, which is a *different rule* |
| An `Elicitation` record behind every CPT cell | `bn/network.py` | Opinion laundered through arithmetic into a number with no author |
| Hash-bound expert approvals | `model.py` | Editing an approved rule and silently inheriting the approval |
| Appended, hash-chained audit ledger | `obs/` (designed, not built) | Being unable to reconstruct why the system said what it said |

## The wager

The design makes a specific bet, and it is worth stating because it is the thing
a reviewer should argue with.

**The bet is that encoding legal judgement is worth doing at all, if and only if
the encoding is inspectable by the expert who owns the rule.** The alternative
positions are both respectable and both rejected here. One is *retrieval only* —
build a very good search engine and let the lawyer do the reasoning. That is
safe, and it leaves the second failure untouched: the tool never tells you the
rule, so it never tells you the rule has an exception you did not think of. The
other is *end-to-end model reasoning* — let a capable model read the cases and
answer. That is fluent, and it is fluent in exactly the way that produces
`JNE24`: the failure is not that the model cannot read, it is that nothing in
the pipeline is *structurally* incapable of inventing authority.

So the system is built with a deterministic core — citation parsing, the
citation graph, BM25 retrieval, span-exact verification, the defeasible rule
engine, exact Bayesian inference, the evaluation harness — that runs with no
model and no network at all. The model-backed agent layer is opt-in
(`.[llm]`), and it is the only component that can fabricate, which is precisely
why it is the only component kept off the critical path.

## What the reader should look at

If you are assessing this work rather than reading it end to end, three things
carry most of the weight.

**The evaluation harness found real bugs.** Not bugs in the tests — bugs in the
system, which the system's own validation had passed. The variable-elimination
implementation and the enumeration implementation are written independently and
cross-checked over every single-root evidence set; on first run the cross-check
failed, and the cause was two silent layout faults in the CPT-to-factor
conversion. A CPT is stored one row per parent configuration with one column per
state, and a factor's axes are its scope with the node's own state axis first.
Both arrays have the right number of elements, so reshaping one into the other
does not raise, `_validate()` does not catch it, and the prior came out
coincidentally correct. What was wrong was a conditional: P(high | foreign
government) was 0.399 where it should have been 0.236. `tests/test_bn.py` pins
this as a regression test. See `docs/05-bayesian-layer.md`.

**A safety control that could not fire was found and replaced.** The retrieval
abstention gate was originally a threshold on the retriever's fused score. RRF
keeps only the *ordering* of each ranker, so the top hit of a single ranker
scores 1/(k+1) = 0.0164 whatever the corpus contained — the confabulation case
and a case with a perfect answer scored identically, and the threshold could
essentially never fire. A control that cannot fire is worse than no control,
because it is reported as one. It is now IDF-weighted query coverage. See
`docs/03-retrieval-and-verification.md` and `docs/08-evaluation-harness.md`.

**Confidence intervals were degenerate.** The percentile bootstrap has zero
width when the observed proportion is 0 or 1, so a suite that passed everything
reported a 95% interval of [1.000, 1.000] — the most confident-looking result
available, produced by the least evidence, in the artefact a reviewer is most
likely to quote. The exact interval for the same data is [0.631, 1.000]. See
`docs/08-evaluation-harness.md`.

**The provenance mechanism had a hole in it.** The first revision of the shipped
ruleset cited every statutory node as `statute:Foreign Acquisitions and Takeovers
Act 1975 1975:18` — the corpus key with the year duplicated and a space in place
of the colon. Structural validation passed it, because validation can ask whether
a node *has* provenance and cannot ask whether the provenance names anything. So
the entire statutory basis of the only encoded rule pointed at a string that
resolved to nothing, and `ratio rules validate` printed "ok". See
`docs/04-rule-encoding.md`.

**The quotation was not a quotation.** The fix above closed the hole one level
too early. "Names something" and "names something that says this" are different
claims, and only the second is evidence. The shipped ruleset quoted a sentence of
the Full Court as ending in a full stop; the source continues with a comma and
goes on to say something further, so the quotation ended the court's sentence for
it — which changes the grammar of the sentence being cited, and hides that the
court said more. `validate_ruleset` cannot see this (the node has provenance),
`provenance_problems` cannot see it (the key resolves to a real judgment and a
real paragraph, `au/fcafc/2022/205` at [3]), and a skim cannot see it, because it
is *nearly* right. `ratio.rules.engine.anchor_problems` now asks the third
question, and `ratio rules validate` fails on it. See
`docs/04-rule-encoding.md`.

**The documented approval flow could not produce a valid approval.** The one that
matters most, because following the printed instructions exactly made the rulebook
unevaluatable. `canonical_payload()` covers `status`, and signing off is precisely
the act of changing `status` from `draft` to `approved` — so a digest taken over
the draft, as `ratio rules approve` told the reader to do, could never match the
revision afterwards. The resulting file reported `is_approved: false`, carried an
approval whose hash matched nothing, and failed `validate_ruleset` with "it has
been edited since sign-off" — which makes `RuleEngine.__init__` raise, so the
rulebook could not be evaluated at all. `RuleSet.approval_digest` is the hash the
revision will carry *once approved*, which is the hash a signature has to name.
See `docs/06-expert-authoring-surface.md`.

Each of these is a case of the same thing: the components were plausible,
self-consistent, and wrong, and the only reason they were caught is that
something independent was asked to disagree. That is the argument for the
harness, and it is why the harness is not a scoreboard. It is also the argument
for reading the rest of this documentation as a set of claims that have been
tested rather than a set of intentions.

## What is deliberately not here

- **No live corpus ingestion.** `ingest/fixtures.py` is the seam, and it is
  deliberately the only place a real deployment has to change. What a real
  ingest involves is a licensing question before it is an engineering one — see
  `docs/02-judgment-knowledge-base.md`, including why the obvious free source
  cannot lawfully be used for this purpose.
- **No dense retrieval by default.** Published evidence on legal retrieval runs
  against reaching for embeddings first (see `docs/03-retrieval-and-verification.md`);
  the offline default is lexical, and a dense ranker is an opt-in interface.
- **No autonomous advice.** The system advises; the lawyer decides. Every
  output carries its anchors, its authority status and its ruleset approval
  state, and the shipped ruleset is `status: draft` today, which every output
  says out loud.
- **No claim to a measured accuracy.** The task suites are hand-curated and
  small — six to fifteen cases — and `eval/tasks/04_retrieval.yaml` says so at
  the top of the file, in the artefact itself rather than in a caveat section a
  reader might skip.

## The documents

| Doc | Subject |
|---|---|
| `00-overview.md` | This. The problem, the invariant, the wager |
| `01-architecture.md` | Layers, boundaries, dependency posture, failure modes |
| `02-judgment-knowledge-base.md` | Sources, document model, citation graph, precedent |
| `03-retrieval-and-verification.md` | Why legal retrieval is different; grounding |
| `04-rule-encoding.md` | Defeasibility, provenance, drift, point-in-time |
| `05-bayesian-layer.md` | Probabilistic reasoning over legal standards |
| `06-expert-authoring-surface.md` | Making the expert the author |
| `07-agent-orchestration-mcp.md` | Roles, MCP tool surface, adversarial verification |
| `08-evaluation-harness.md` | Making quality measurable and regressions detectable |
| `09-observability-and-audit.md` | OTel traces, audit ledger |
| `10-aws-deployment-security.md` | Deployment, identity, data handling |
| `11-governance-assurance.md` | ISO 42001 and the Australian guardrails, mapped |
| `12-roadmap-and-risks.md` | What to build next, and what would make me stop |
