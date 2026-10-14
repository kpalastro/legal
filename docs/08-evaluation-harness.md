# 08 — The evaluation harness

An eval is not a scoreboard. It is the mechanism by which a project's claims
about itself are made falsifiable — and in this project it has already earned
its place by finding faults that nothing else did.

This document describes what the harness measures, why it measures those things
rather than the standard defaults, and what the current numbers do and do not
support. The last section is the important one. A suite of six cases cannot
establish that a threshold is right; saying so is part of the harness's job.

---

## The cross-check that found two silent bugs

The clearest evidence in this repository that the approach works is not a
score. It is the credence layer.

`CredenceEngine.query` performs variable elimination. `query_by_enumeration`
brute-forces the same quantity by summing over every hidden assignment. They are
independent implementations of the same posterior, and
`tests/test_bn.py::test_variable_elimination_agrees_with_enumeration` asserts
they agree over every assignment of the network's root variables.

On its first run against the shipped network it failed. Two layout faults were
present:

- `_factors` reshaped a conditional probability table — written as one row per
  parent configuration, one column per state of the node — straight into the
  factor's scope shape, whose first axis is the node's *own* state. Both arrays
  have the same number of elements, so `np.reshape` succeeded and silently
  reinterpreted the numbers.
- `_multiply` did the same to its second operand, whose axes sit in a different
  order in the union scope than in its own.

Neither was caught by the network's `_validate()`, which checks that a CPT's rows
sum to one — a property both layouts preserve. Neither was visible in the
**prior**: at no evidence the two errors cancelled and the prior came out correct
to four decimal places. They showed only under conditional evidence, which is
the only case that matters. Given a foreign-government acquirer, the engine
reported `P(high concern) = 0.399` where the correct value is `0.236` — a 69%
relative overstatement of the highest-risk state, produced by a network whose
own validation passed.

`test_conditional_posterior_is_not_transposed_against_its_cpt` now pins both
values, and pins the prior alongside them, so that a future simplification of
either layout produces a failure naming the fault rather than a silent change in
a risk number a client might read.

The general lesson, and the reason this section opens the document: a numerical
fault that preserves every invariant the code checks is invisible to inspection
and to unit tests. It is visible only to a second, independent computation of the
same quantity. That is what the harness is for. Everything below is that idea
applied to the system as a whole.

## Abstention is a first-class outcome

The single biggest correction to the usual framing.

A legal system that says "not determinable on the facts given" when a required
fact is missing is behaving **correctly**. A system that guesses is not. If
abstentions are scored as errors, the harness pressures the system toward
guessing — which is exactly the failure mode the whole architecture exists to
prevent. So the harness reports:

- **answered accuracy** — correct over the cases it answered. Flatters;
  abstentions are excluded.
- **end-to-end accuracy** — correct over all cases. Punishes; abstentions count
  as wrong.
- **abstention rate**, reported separately and never folded into error.

One number cannot express both, and the truth is that both are needed to see the
tradeoff. `ABSTAIN` is spelled `"__abstain__"` rather than written as `None`, so
that it survives a round trip through YAML or JSON without acquiring a different
meaning.

**Macro-F1 is reported alongside accuracy as the diagnostic.** A system that
answers "not notifiable" every time scores well on a corpus where most
acquisitions are not notifiable. A large gap between accuracy and macro-F1 means
the score is coming from the majority class, and the gap is what makes that
visible.

There is one deliberate asymmetry in the per-label arithmetic. An abstention
counts against **recall** for the label the system declined to give — the system
did not find it — but never counts as a **false positive** for any label, because
it made no claim. Scoring abstention as a false positive would put precision and
recall in conflict and reward guessing. This is the one place in the metric where
a scoring decision encodes an ethical one, and it is a decision, not a default.

## Baselines travel with every score

An accuracy is meaningless without the floor it has to clear.

- `AbstainSystem` always declines. It is the floor, and an informative one: any
  system scoring below it is worse than useless, and any system whose *answered*
  accuracy falls below it has a serious problem.
- `MajoritySystem` always answers the most common expected label in the suite.

The majority baseline is **fitted on the suite it is scored on**, which flatters
it, and that is deliberate. The point of a baseline is to be generous. A system
that cannot beat a generously-fitted majority baseline has no demonstrated signal
at all — and most published benchmarks for legal classification are cleared by
exactly this baseline, a fact usually left out of the write-up.

## Guardrails fail the suite outright

Cases may be marked `expect_refusal`. On such a case, **answering correctly is
failing**. These are the cases where the only right behaviour is to refuse: a
claim citing a judgment that is not in the corpus, a fabricated quotation.

A guardrail failure does not contribute to a score. It fails the suite:

```python
@property
def passes(self) -> bool:
    return not self.guardrail_failures
```

A single case where the system failed to refuse a fabricated authority is not
offset by a hundred correct ones. Averaging it into an accuracy would let it be,
which is why it is not averaged. `tests/test_harness.py::test_guardrails_invert_the_scoring`
asserts the inversion directly, including that a system which returns a
plausible verdict on a guardrail case fails while one which returns
`__refused__` or abstains passes.

## Systems are scored only on the kinds of question they answer

Each system declares `kinds` — a frozenset naming the case kinds it has an
opinion about — and the harness scores it only on those.

This is not an optimisation. Scoring the verifier on "is this acquisition
notifiable?" produces a number, and the number is meaningless — but worse, it is
**not inert**. It enters the comparison against other systems, it produces
spurious guardrail failures for a system that never ran on those cases, and it
buries the signal the suite exists to carry. A system with no opinion on a case
has not failed it; it has not answered it, and the two must not be reported
alike.

Two related guards:

- A case whose `kind` no system handles is reported as **skipped**, and a suite
  with no matching cases renders `Not evaluated` rather than a clean sheet. A
  suite half of which was skipped is not a suite that passed.
- An unknown `kind` is **rejected at load**. A typo in a task file must announce
  itself rather than silently contributing nothing to every score in the file.

Comparisons use `mcnemar` on the **discordant pairs** — the cases where exactly
one system was right — because the cases where both were right or both wrong
carry no information about which is better. The exact binomial form is used
rather than the chi-square approximation, since the discordant count in a legal
evaluation is routinely below 25, where the approximation is not trustworthy.
`mcnemar` refuses to compare different case sets: it raises if lengths differ or
if case ids do not align, because the test is only valid on matched pairs.

## Confidence intervals: why the bootstrap was replaced

The headline intervals are **Clopper–Pearson exact** intervals, not percentile
bootstrap intervals. The reason is specific to this project rather than a
general preference for exact methods.

The percentile bootstrap has **zero width** when the observed proportion is 0 or
1: every resample of eight successes is eight successes. So an 8/8 suite reported
a 95% interval of `[1.000, 1.000]` — the most confident-looking result available,
produced by the smallest amount of evidence, in the artefact a reviewer is most
likely to quote. The exact interval for the same data is `[0.631, 1.000]`, which
is the same data telling the truth.

This matters more here than it would elsewhere because the entire design
position of the evaluation layer is that a number without its uncertainty, its
baseline and its denominator is not a number a reviewer can act on. A degenerate
interval defeats that from the inside, and it does so worst on the small,
hand-curated suites that are most likely to be quoted.

There is a trap in the implementation worth recording, because it was hit twice
during development. The lower and upper bounds require **opposite** comparisons:
the lower bound is the smallest `p` at which the upper tail has fallen to
`alpha/2`, and that tail *rises* with `p`; the upper bound is the largest `p` at
which the lower tail has fallen to `alpha/2`, and that tail *falls* with `p`.
Writing the two the same way round produces an interval that excludes the point
estimate — and it is invisible on the all-correct and all-wrong cases, which is
precisely what a small suite produces.
`test_the_exact_interval_is_symmetric_under_relabelling` asserts `CI(x, n)`
mirrors `CI(n-x, n)` for every `x` over several `n`, and that the interval always
contains the estimate. That is the property that catches the transposition.

The bootstrap is retained in `ratio/eval/metrics.py` for quantities that are not
proportions over cases, and `bootstrap_ci` remains seeded — an unseeded interval
is a number that moves when nothing changed, which destroys the only thing a
regression suite is for.

`pass_pow_k` is also available: the probability that `k` independent attempts all
succeed. It is the right summary for a system run repeatedly — a system right 90%
of the time is right five times in a row 59% of the time, and a lawyer who runs it
once per matter will eventually be the other 41%.

## The case study: a safety control that could not fire

This is the finding that shapes the rest of the design, and it is worth setting
out at length because it is a general failure, not a local one.

The retrieval wrapper originally abstained when the retriever's top score fell
below a threshold. The retriever returns a fused **reciprocal rank** score. RRF
keeps only the *ordering* of each ranker, so its output is a function of rank
alone: the top hit of a single ranker always scores `1/(60+1)`, whatever the
corpus contained.

The threshold was expressed in that unit. Every non-empty result scored ~0.0164
against a ~0.0115 threshold. The confabulation case — "what is the effect of an
exemption certificate on notifiability?", a question no judgment in the corpus
addresses — scored the **same 0.0164** as a case with a perfect answer. The gate
could not fire. It was reported as a safety control, and it was not one.

That is the general failure mode this document exists to name: **a control
measured in a unit that cannot vary is worse than no control, because it is
reported as one.** It has a passing test suite, a threshold constant, a comment
explaining what it protects against, and no effect whatsoever.
`test_the_rrf_score_cannot_be_the_basis_of_an_abstention` now asserts the
near-constancy directly — if a future change makes the fused score usable, that
test fails and says so.

### What replaced it

The gate now measures **how much of the question the corpus recognises.**

Each distinct query term is weighted by its IDF — its distinctiveness — and the
top hit's **coverage** is the share of that total weight it accounts for. The
reading is plain: "the best paragraph in the corpus uses 20% of the distinctive
vocabulary of your question" is a refusal; "it uses 61%" is an answer.

Unweighted term counts do not separate the two families. A lawyer who pads a
question with ordinary words — "what is the effect of ... on a notifiable
action" — raises unweighted coverage without the corpus having any more to say.
IDF weighting barely moves, which is the point.

An earlier version weighted a term occurring in *no* document at zero, since it
has no postings and therefore no IDF. That inverts the measurement: the less the
corpus had to say about a question, the better covered the question would look.
Terms the corpus does not contain are now weighted at `max_idf`, the weight a
term would carry if exactly one document contained it — the rarest thing that
does exist. `BM25Index.idf` and `BM25Index.max_idf` are public methods rather
than the gate reaching into `_idf`, because a gate with its own private
definition of IDF is a second definition that silently diverges from the first.
`test_a_term_the_corpus_never_contains_drives_coverage_down` asserts the
behaviour, and names this as the most likely way for the gate to be quietly
broken by a later "simplification".

### The calibration, stated honestly

The default threshold is 0.5. Against the current suite it separates the two
families cleanly — lowest positive **0.607**, highest refusal **0.206**, a margin
of **0.401**. `test_the_gate_separates_the_two_families_with_a_margin` asserts
both the separation and that the margin exceeds 0.15, so that closing it fails
loudly and someone re-derives the threshold rather than nudging it.

Two cautions, and they are why `min_coverage` is a dataclass field rather than a
module constant:

1. **Six cases is not a calibration.** The margin is the thing to watch, not the
   pass rate.
2. **Coverage bounds topical relevance only.** A paragraph can use every word of
   the question and still not answer it. Coverage is a **necessary** condition for
   answering, never a sufficient one.

The second caution is why the MCP search tool reports coverage but refuses to
report the fused score as anything resembling a confidence: coverage is the
quantity that was calibrated, and presenting a different quantity in the same
position would reintroduce the whole problem.

The gate is also tested as a **pair**, because its two claims are only meaningful
together: `test_the_gate_abstains_when_the_corpus_has_nothing` and
`test_the_gate_does_not_refuse_when_the_corpus_has_something`. A gate tuned to
refuse confabulations is trivially satisfied by refusing everything, and a
retriever that refuses everything is not a safe system, it is a broken one.

## What the suite cannot show

Taken from the LIMITATIONS block at the head of `eval/tasks/04_retrieval.yaml`,
and not softened here:

1. **Six cases, four of them positive.** This is a smoke test of the refusal
   behaviour, not a measurement of retrieval quality. The margin is what matters
   and it must be recomputed every time a case is added; nothing re-derives the
   threshold automatically.
2. **Only one of the four positive cases is hard** — the one that shares most of
   its vocabulary with the case it distinguishes. The others are cleared by term
   overlap alone, so a system scoring 4/4 here may still be doing nothing more
   than keyword matching.
3. **The suite cannot detect the pathology it is closest to**: preferring a lower
   court's summary of an authority over the authority itself. The `also_accept`
   entries in the suite acknowledge this case by case — a Full Court statement of
   a point is scored as acceptable — but nothing in the system implements
   authority-aware ranking, and no case would fail if it did.
4. **Retrieval does not index statutory provisions at all.** The index is built
   from judgments only. A correct answer that lives in a provision is not
   retrievable, and the two refusal cases below are refusal cases partly for that
   reason: the exemption certificate appears in the statute but no court in the
   corpus has construed it, so as the index stands there is nothing to find. That
   is the right answer here and the wrong architecture generally.

Point 3 is the sharpest of the four. It is the suite admitting that it does not
test the thing it is nearest to testing, and that no amount of adding cases to
*this* suite would fix it, because the fix is a ranking feature that does not
exist.

The current retrieval result, for the record: 4/6 correct, 2 abstentions, both
expected, **0 false answers**. Two of the four correct answers are correct via
`also_accept`.

## Harness tests versus task suites

The two are different instruments and the distinction is deliberate.

**`tests/test_harness.py` asserts properties of the metric.** That intervals are
symmetric under relabelling, that they are not degenerate, that guardrails invert
the scoring, that a system which raises is recorded as an abstention rather than
aborting the run, that an empty suite reports no interval rather than a perfect
one, that comparison refuses mismatched case sets, that the fused score is
near-constant. These are properties that must hold whatever the system's
accuracy happens to be. They are what stops the harness itself from quietly
becoming wrong — and a fault in the harness is worse than a fault in any
component, because it does not produce a wrong answer, it produces a wrong
*account* of how the system behaves, in the artefact a reviewer is most likely
to trust.

**`eval/tasks/*.yaml` measure the system.** They live outside the package so a
lawyer can add cases without touching code. Each case declares its own ground
truth, the authority it came from, and — where more than one conclusion is
defensible — the alternatives that also count. Cases with no `authority` are
surfaced by `TaskSuite.unanchored`, because a case whose expected answer is
somebody's opinion should be visible as such rather than scored silently.

## What is not built

- No cross-model or cross-version comparison. A comparison between two models
  uses the same machinery as any two systems, but none has been run.
- No adversarial or red-team suite for the refusal path. The guardrail cases test
  fabrication; they do not test a system under pressure to produce an answer.
- No test-set separation. Every suite is both development and evaluation data.
  With suites this small, a threshold tuned on the cases it is scored against is
  tuned on its test set, and the honest reading of the current numbers is as a
  regression check, not as a performance estimate.
- The `rule`, `verification` and `treatment` suites do not exercise the retrieval
  system, and the retrieval suite does not exercise the other three. No suite
  currently measures the system end to end.
