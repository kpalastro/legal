# 05 — The probabilistic layer

## Why a probability, and why it is not a cop-out

A rule engine can say whether an acquisition is notifiable, because s 18 of the
Foreign Acquisitions and Takeovers Act states a test with elements. It cannot say
whether an acquisition is *contrary to the national interest*, because the
statute does not define that phrase and the Minister's satisfaction is not
reviewable in the way a rule's elements are. Neither can it answer "is this
shareholding **substantial**" where the statute says substantial and the case law
gives you factors rather than a line.

For those questions the honest technical artefact is not a boolean. It is a
probability — and the honest engineering questions are not "what does the model
predict" but:

1. **Where did every number in this model come from?**
2. **How much does the answer move if an expert disagrees with one of them?**

Most Bayesian-network tooling answers neither, which is why probabilistic
reasoning has a bad reputation in regulated settings: it looks like a machine for
turning opinion into a figure that outranks the opinion.

The design commitments in `bn/network.py` exist to answer both.

## Commitment 1: exact inference only

Enumeration and variable elimination, both implemented in-tree, both tested
against each other. No sampling anywhere.

An approximate answer that cannot be reproduced is worse than no answer in a
system a court might have to look at. A posterior of 0.62 that shifts to 0.58 on
re-run is not a measurement, it is noise with a decimal point, and a lawyer who
notices will stop using the tool — correctly. Exactness also makes the
regression suites meaningful: the harness re-runs systems and compares outcomes,
and an inference routine with stochastic output would make every comparison a
coin flip.

Disk and dependency constraints made this an in-tree implementation rather than a
library (see `docs/01-architecture.md`, "Dependency posture"), and the
cross-check makes that a feature rather than a compromise.

## Commitment 2: no black boxes

A CPT row that cannot be traced to an `Elicitation` record is a **validation
error, not a warning**.

`Elicitation` holds one record per parent configuration, holding the whole
distribution, because that is how elicitation actually happens — you ask an
expert "given these features, how likely is each outcome", not "what is the third
component of a vector". The important property is that the CPT row and the
elicitation record must *agree*: `CredenceEngine` refuses to load a network whose
numbers differ from what the named expert stated.

That is what stops a number being quietly tuned during model development and
keeping the provenance of the original workshop. A number in a legal model that
cannot be traced to a person and a date is an opinion wearing a percentage sign,
and it will be quoted as though it were a measurement.

## Commitment 3: posterior, never verdict

`CredenceEngine.query` returns a distribution plus sensitivities. It is
structurally incapable of returning "yes".

This is not a stylistic preference. The output of this layer has to be
un-usable as a conclusion on its own, because the question it answers — "how
strongly do the factors point toward X" — is not the question a lawyer is asking.
The layer's job is to make the weight of the soft factors visible; the decision
is the rules layer's and ultimately the practitioner's.

## The bug the cross-check found, and why it is in this document

The two inference implementations are cross-checked over **every** single-root
evidence set in `tests/test_bn.py`. On first run the cross-check failed.

The cause was two silent layout faults in the CPT-to-factor conversion, and the
shape of the failure is the reason this section exists.

**A CPT is stored the way a table of elicitations is written down:** one row per
parent configuration, one column per state of the node. **A factor is stored the
other way round:** its axes are its scope, and the first scope entry is the node
itself. Reshaping a CPT straight into the scope's shape reinterprets the same
numbers in the wrong layout — and both arrays have exactly the right number of
elements, so it does not raise. `_validate()` did not catch it. Nothing caught it.

The fix is `_factors` moving the state axis in front of the parent axes before
reshaping:

```python
states_first = np.moveaxis(np.asarray(node.cpt, dtype=float), 1, 0)
table = states_first.reshape(tuple(len(self._nodes[v].states) for v in scope))
```

The second fault was in `_multiply`. Multiplying two factors requires aligning
each to the union of their scopes, and the naive version reshaped each table into
a permutation of its own shape — which, again, does not fail, it silently
transposes the data. The correct order is to *permute* the axes into union-scope
order first, and only then apply the size-1 padding for the variables a factor
does not mention.

The measured effect: P(high | foreign government) was 0.399 where it should be
0.236. The prior came out coincidentally correct, which is why nothing looked
wrong.

Three things are worth taking from this, and they generalise past this codebase:

- **The fault was invisible to the component's own validation.** Validation
  checks that the numbers are *present* and *normalised*; it cannot check that
  they are in the right *position*. A self-consistency check is not an
  correctness check, and the gap between them is where this class of bug lives.
- **Two independent implementations of the same specification caught it.** Not
  two tests of one implementation, which would have shared the assumption. The
  cross-check is only worth anything insofar as the two paths do not share code.
- **The cross-check space must be asserted non-empty.** A test that iterates over
  all single-root evidence sets passes trivially if the set is empty. There is a
  test for that too.

Both faults are pinned as a regression test in `tests/test_bn.py`, with the
original values.

## Sensitivity, not just posterior

Every `posterior_report` carries a caveat and the authorities behind the
network, and the engine exposes sensitivity analysis: how much the posterior
moves when a elicited cell moves within the range the expert gave.

This is what makes the elicited numbers arguable rather than authoritative. An
expert who disagrees with a cell can ask what difference it makes, and the answer
is a number rather than a debate. In many cases the honest answer is "almost
none", which is worth knowing before spending a workshop on it.

## The delegate seam

`NodeKind.DELEGATE_BN` exists so a rule tree can hand a sub-question to the
probabilistic layer. The engine models the node kind and reports it in the proof
tree; it does not itself call `CredenceEngine`, because the rules layer must stay
runnable with no numpy and the BN layer must stay runnable with no ruleset. The
join is a caller's job — see `docs/07-agent-orchestration-mcp.md` — and it is the
point at which the proof tree starts carrying a distribution as well as a
finding.

That seam is the least-finished part of the design. The two halves are built and
tested independently; the contract between them is specified and not exercised
end to end.

## What is not built

- **One network ships**, `firb_national_interest.credence.yaml`, covering the
  national-interest factors. It is a worked example of the elicitation format and
  the validation discipline, not a validated model.
- **No elicitation has been conducted.** The records in the shipped network are
  illustrative and say so. Nothing here has been through checkpoint G4 in
  `docs/11-governance-assurance.md`.
- **The delegate seam is not wired** (above).
- **Continuous variables are not supported.** Nodes are discrete and finite; a
  holding percentage that matters to the analysis is discretised into bands by
  whoever encodes the network, and that discretisation is itself an elicitation.
