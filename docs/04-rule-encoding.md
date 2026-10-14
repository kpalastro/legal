# 04 — Encoding legal judgement as rules

## The one unusual feature

Exceptions are a distinct node kind from conjuncts, and that is the whole point
of the module.

Most attempts to encode a legal test model "the Minister may exempt" or
"...unless the acquirer is a foreign government investor" as another AND-branch.
That encodes a *different rule* from the one in the Act. A defeater is not
required for the conclusion to hold; it is sufficient, on its own, to destroy it,
and it destroys it **even where every other element is made out**. Collapsing the
distinction produces a system that is confidently wrong in exactly the cases a
lawyer cares about — the unusual case, which is the case the lawyer is asking
about.

So the rule language has six node kinds:

```
all_of        every child must be satisfied
any_of        at least one child must be satisfied
not           the child must not be satisfied
leaf          a factual predicate over the case facts
delegate_bn   hand off to the probabilistic layer
exception     a defeater, not a conjunct
```

A node's normal children must all be satisfied; its `exception` children must
all be *not* satisfied. A firing exception marks the node `defeated`, and the
proof tree reports which one fired and why.

`Predicate` is kept deliberately primitive — comparisons only, no computation —
because a rule an expert cannot read is a rule an expert cannot approve, and
approval is the only thing standing between the encoding and the law.

## What a proof tree carries

`ProofTree` is the derivation, and it is an output rather than a debugging aid:

- `conclusion` — `applies`, `does_not_apply`, or `undetermined`;
- `findings` — the per-node outcome, so a reader can see which elements were
  made out and which were not;
- `defeaters_fired` — the ids of the exceptions that fired;
- `gaps` — **facts needed but absent**. Not "nodes that failed": the *fact
  fields* a user could go and establish. This distinction is the difference
  between a dead end and a task list. A user told "the analysis is undetermined
  because `action.is_exempt_action` was not supplied" has something to do; a
  user told "node n7 evaluated to unknown" has nothing.

That is why `defeaters_fired` names the *firing exception's own id* rather than
the parent. "The conclusion does not hold" is not actionable; "it does not hold
because the exemption-certificate exception fired" is.

## Abstention: the rule system refuses rather than guesses

The rule engine abstains on gaps. When a required fact is absent, the node
evaluates to `unknown`, and `unknown` propagates. A conclusion is only reached
when the facts support one.

This produces a behaviour that is worth stating because it looks like a bug to
someone expecting a classifier: **a missing fact can make an otherwise-satisfied
conclusion `unknown`.** That is correct. Consider a rule whose elements are all
made out and whose one exception — "unless an exemption certificate covers the
acquisition" — cannot be evaluated because nobody supplied
`action.covered_by_exemption_certificate`. The elements are satisfied. The
conclusion is nonetheless not safe to state, because the one thing that could
destroy it has not been *tried*, and "we did not check" is not "it did not fire".

An earlier version of `_combine` returned the satisfied base in that situation
and recorded the gap alongside. That is a real defect, and worth understanding
rather than filing as a bug fix, because it is a defect in the *shape* of the
output rather than in a computation. The gap would have been recorded either
way, and the caller that checks `gaps` would abstain either way. But the
conclusion travels further than the gap list does: it is what gets rendered,
logged, and quoted. It must not be the optimistic one. The rule now downgrades a
satisfied conclusion to `unknown` when any of its exceptions is unevaluated —
and only an otherwise-satisfied conclusion, since a conclusion that already fails
on its elements is not made worse by an untried defeater.

## Provenance is structural, not conventional

Every `leaf` and `exception` node carries `Provenance` — a reference to the
authority (a judgment paragraph or a statutory provision) that the node encodes.
This is enforced at the type level. A node kind that asserts something about the
law without provenance is not a node that *should* be rejected; it is a
`ValidationError`. `tests/test_rules.py` asserts that an unprovenanced leaf is
refused.

The join is the point. The knowledge base holds what the courts actually said;
the rule layer holds what we tell a machine to conclude; `Provenance` is the only
place the two meet, and it is the only place a reviewer needs to look to check
that a machine-evaluable rule is the law it claims to be.

## Approval, and why it is a hash

`Approval` is an expert's sign-off, carrying the approver, their role, the time
and an `artefact_hash`. `RuleSet.canonical_payload` is a deterministic
serialisation of everything except the approvals themselves, and
`current_approval` returns the approval whose hash matches that payload.

The consequence is the mechanism: **editing an approved ruleset invalidates its
approval rather than silently inheriting it.** A version bump alone does not do
this — a reviewer who changes an exception and forgets to bump the version would
otherwise ship an artefact whose stated approval belongs to a rule that no
longer exists. A hash cannot forget.

The approval is also deliberately *not* written by the CLI. `ratio rules approve`
prints the hash block for the expert to paste into the ruleset file, so the act
of approving is performed by the person who is accountable for it and is visible
in the artefact's history, rather than being a side effect of running a command.

**The shipped ruleset is `status: draft` and has no approval.** Every output that
depends on it says so — the MCP `apply_ruleset` payload carries
`approved: False`, and `tests/test_mcp.py` asserts that a complete fact base
determines the conclusion *and* reports `approved: False`. The gap between
"there is an approval mechanism" and "a rule has been approved" is exactly the
gap this design is about, and it is not hidden.

## Drift: how the encoding comes apart from the expert's belief

An expert reviewing an encoded rule by reading it will miss things. Everybody
does. The reviewer sees the structure, agrees with the structure, and stops —
which is why formalisation projects ship rules that behave differently from what
the reviewer believed they had approved.

The technique with the strongest track record is not reading the rule, it is
**generating cases from it and making the expert adjudicate the outcomes.**
Catala's differential test against OpenFisca found a live production defect that
way; concolic test generation over a benefits codebase produced 186,390 cases and
found a rounding inconsistency between two backends. The principle scales down
to a single rule: do not ask the expert whether the rule is right, ask them
whether *this case* came out right, a few hundred times.

`rules/generate_cases.py` generates the cases. Each leaf is given a small set of
discriminating values rather than an exhaustive domain, so the cross product
stays inside a reviewable size, and the sheet is an incidence matrix: one row per
fact combination, one column per leaf, plus the conclusion the engine reached and
which defeater fired.

Two properties of the sheet do real work, and both are checks nobody performs by
reading:

- **Inert leaves.** A leaf whose value never changes the conclusion is either
  redundant or mis-wired. Either way it is a defect in the encoding, and it is
  invisible in a rendering of the rule tree.
- **Defeater coverage.** If no generated case fires a given exception, the
  exception has never been exercised, and its provenance has never been tested
  against anything. The generator reports this as a defect in the *generator* as
  well as in the ruleset, because an unexercised exception can mean the
  exception is unreachable, or that the value space is too narrow to reach it.

Everything here is deterministic. It is the machinery an SME review queue runs
on, not a model.

## Point in time

`RuleSet` carries `effective_from` and `effective_to`, and evaluation takes an
`as_at` date. A rule encoding s 18 of the Foreign Acquisitions and Takeovers Act
as it stood before a 2021 amendment is a *different ruleset* from the one
encoding the provision after it, not a version flag on the same rule. Treating
them as one object with a date field is how a system ends up applying the current
test to a 2019 acquisition.

This is also why the ruleset version, its approval state and the `as_at` date
travel with every result. A conclusion without them cannot be checked.

## Structural validation

`validate_ruleset` refuses, rather than warns:

- a node with no provenance, where the kind asserts something about the law;
- an unreachable node (not in the root's transitive closure);
- a node that is its own ancestor — a cycle is a hard error, not a warning,
  because the alternative is a stack overflow inside an evaluation that a user is
  waiting on;
- an exception listed under two parents, which makes "which one fired"
  ambiguous;
- a ruleset marked `approved` whose `current_approval` no longer matches — it has
  been edited since sign-off.

An unapproved ruleset still runs. It is a draft an expert is testing, and
refusing to run it would make the authoring workflow impossible. It is not
*silent* about being unapproved, which is the part that matters. The approval
check therefore fires on a *previously approved* ruleset that has moved, not on a
draft; a draft reports `approved: false` in every result instead.

### Provenance has to resolve, and that is a different check

`validate_ruleset` is a pure function of the ruleset, and a ruleset does not carry
a corpus. So it can ask whether a node *has* provenance, and it cannot ask whether
the provenance **names anything** — which turned out to matter.

The first revision of the shipped ruleset cited every statutory node as
`statute:Foreign Acquisitions and Takeovers Act 1975 1975:18`: the corpus key with
the year duplicated and a space where the colon belongs. Every structural check
passed. The engine ran. `ratio rules validate` printed "ok". And the entire
statutory basis of the only encoded rule terminated in a string that pointed at
nothing — which is exactly the failure the invariant exists to prevent, wearing
the mechanism that is supposed to prevent it.

The resolution step therefore lives in `provenance_problems(ruleset, *,
judgment_ids, provision_keys)`, which takes the corpus's key sets as arguments
rather than a knowledge base so the rules module stays runnable with no corpus
loaded, and is called by the CLI (`ratio rules validate`, which now resolves
against the loaded corpus), by the MCP ruleset resource (as
`unresolved_provenance`), and by tests. Unresolved provenance is **reported, never
repaired**: a key that almost matches is still a key nobody checked.

## Known gaps

- **One ruleset ships**, `firb.notifiable_action`, covering the s 18 notifiable-
  action test and its exceptions. The continuous disclosure and competition
  domains named in the repository description are not encoded.
- **The second ruleset**, `firb_national_interest.credence.yaml`, is the
  probabilistic layer's network rather than a rule tree — see
  `docs/05-bayesian-layer.md`.
- **Delegation to the BN is a node kind, not a wired path.** `delegate_bn` exists
  in the model and the engine reports it; the engine does not itself call
  `CredenceEngine`, and joining the two is specified in `docs/05`.
