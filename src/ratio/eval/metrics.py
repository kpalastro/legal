"""Metrics for a system whose outputs are legal conclusions.

The choice of metrics here is not neutral, and the standard machine-learning
defaults are the wrong ones in ways that matter.

**Accuracy is the headline number and the least useful.** A system that answers
"not notifiable" every time scores well on a corpus where most acquisitions are
not notifiable, and it is worthless. So the harness reports accuracy *and*
macro-F1, and treats the gap between them as the diagnostic: a large gap means
the score is coming from the majority class.

**Abstention is a first-class outcome, not an error.** This is the single
biggest correction to the usual framing. A legal system that says "not
determinable on the facts given" when a required fact is missing is behaving
correctly; a system that guesses is not. If abstentions are scored as wrong, the
harness will pressure the system toward guessing, which is exactly the failure
mode the whole design exists to prevent. So:
  * abstention rate is reported separately and never folded into error;
  * there are two accuracies: *answered accuracy* (over cases it answered) and
    *coverage-weighted accuracy* (abstention counts as wrong). The first flatters,
    the second punishes, and the truth is that both are needed to see the tradeoff.

**A single number from a single run is not a result.** Every rate carries a
bootstrap confidence interval, because with 200 test cases an accuracy of 0.83
and one of 0.79 are the same measurement. And where two systems are compared on
the same cases, the comparison uses McNemar's test on the *discordant pairs* --
the cases where they differ -- rather than comparing two accuracies as if they
came from independent samples.

**Legal tasks admit multiple defensible answers.** A rule can be applied
correctly and produce a conclusion a competent practitioner would state
differently. Where the task file names alternatives, any of them counts.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Sequence

import numpy as np

# The sentinel a system emits when it will not answer. Spelled out rather than
# written as None so that it survives a round trip through YAML or JSON without
# acquiring a different meaning.
ABSTAIN = "__abstain__"


@dataclass
class Outcome:
    """One scored prediction."""

    case_id: str
    expected: str
    predicted: str
    alternatives: tuple[str, ...] = ()

    @property
    def abstained(self) -> bool:
        return self.predicted == ABSTAIN

    @property
    def correct(self) -> bool:
        if self.abstained:
            return False
        return self.predicted in (self.expected, *self.alternatives)

    @property
    def is_false_answer(self) -> bool:
        """Answered, and answered wrongly. The category that must be near zero."""
        return not self.abstained and not self.correct


@dataclass
class Report:
    n: int
    n_answered: int
    n_abstained: int
    n_correct: int
    n_false: int
    labels: list[str] = field(default_factory=list)
    per_label: dict[str, dict[str, float]] = field(default_factory=dict)

    @property
    def coverage(self) -> float:
        return self.n_answered / self.n if self.n else 0.0

    @property
    def abstention_rate(self) -> float:
        return self.n_abstained / self.n if self.n else 0.0

    @property
    def answered_accuracy(self) -> float:
        return self.n_correct / self.n_answered if self.n_answered else 0.0

    @property
    def end_to_end_accuracy(self) -> float:
        return self.n_correct / self.n if self.n else 0.0

    @property
    def false_answer_rate(self) -> float:
        return self.n_false / self.n if self.n else 0.0

    @property
    def macro_f1(self) -> float:
        if not self.per_label:
            return 0.0
        return sum(m["f1"] for m in self.per_label.values()) / len(self.per_label)

    def render(self) -> str:
        lines = [
            f"cases                 {self.n}",
            f"answered              {self.n_answered}  ({self.coverage:.1%})",
            f"abstained             {self.n_abstained}  ({self.abstention_rate:.1%})",
            f"correct               {self.n_correct}",
            f"false answers         {self.n_false}  ({self.false_answer_rate:.1%})",
            "",
            f"accuracy | answered   {self.answered_accuracy:.3f}   "
            "(flatters the system: abstentions are excluded)",
            f"accuracy | end-to-end {self.end_to_end_accuracy:.3f}   "
            "(punishes the system: abstentions count as wrong)",
            f"macro F1              {self.macro_f1:.3f}   "
            "(reported alongside accuracy; a large gap means the majority class "
            "is carrying the score)",
        ]
        if self.per_label:
            lines.append("")
            lines.append(f"{'label':<28}{'n':>5}{'prec':>8}{'rec':>8}{'f1':>8}")
            for label, m in sorted(self.per_label.items()):
                lines.append(
                    f"{label:<28}{int(m['support']):>5}{m['precision']:>8.2f}"
                    f"{m['recall']:>8.2f}{m['f1']:>8.2f}"
                )
        return "\n".join(lines)


def score(outcomes: Sequence[Outcome]) -> Report:
    """Aggregate outcomes. Deterministic: no sampling anywhere in here."""
    outcomes = list(outcomes)
    labels = sorted({o.expected for o in outcomes} | {o.predicted for o in outcomes})
    labels = [label for label in labels if label != ABSTAIN]

    per_label: dict[str, dict[str, float]] = {}
    for label in labels:
        tp = sum(1 for o in outcomes if o.expected == label and o.correct)
        fp = sum(
            1
            for o in outcomes
            if o.predicted == label and o.expected != label and not o.abstained
        )
        fn = sum(
            1
            for o in outcomes
            if o.expected == label and not o.correct
        )
        # Asymmetry, deliberately: an abstention counts against *recall* for the
        # label it declined to give (the system did not find it), but never counts
        # as a *false positive* for any label (it made no claim). Scoring
        # abstention as a false positive would put precision and recall in
        # conflict and reward guessing, which is the failure mode this harness
        # exists to detect.
        precision = tp / (tp + fp) if (tp + fp) else 0.0
        recall = tp / (tp + fn) if (tp + fn) else 0.0
        f1 = (
            2 * precision * recall / (precision + recall)
            if (precision + recall)
            else 0.0
        )
        per_label[label] = {
            "support": float(tp + fn),
            "precision": precision,
            "recall": recall,
            "f1": f1,
        }

    return Report(
        n=len(outcomes),
        n_answered=sum(1 for o in outcomes if not o.abstained),
        n_abstained=sum(1 for o in outcomes if o.abstained),
        n_correct=sum(1 for o in outcomes if o.correct),
        n_false=sum(1 for o in outcomes if o.is_false_answer),
        labels=labels,
        per_label=per_label,
    )


# -- statistics ------------------------------------------------------------


def _binom_cdf(k: int, n: int, p: float) -> float:
    """P(X <= k) for X ~ Binomial(n, p), summed exactly rather than approximated.

    n here is the number of cases in a task file -- tens, not thousands -- so the
    exact sum is cheap and there is no reason to reach for an approximation that
    would be wrong in exactly the small-sample regime this function exists to
    describe.
    """
    if k < 0:
        return 0.0
    if k >= n:
        return 1.0
    if p <= 0.0:
        return 1.0
    if p >= 1.0:
        return 0.0 if k < n else 1.0
    return sum(
        math.comb(n, i) * p**i * (1 - p) ** (n - i) for i in range(k + 1)
    )


def exact_ci(
    successes: int,
    n: int,
    *,
    confidence: float = 0.95,
) -> tuple[float, float]:
    """Clopper-Pearson exact interval for a proportion.

    Used instead of the bootstrap for the headline intervals, and the reason is
    specific to this project rather than a general preference for exact methods.
    The percentile bootstrap has **zero width** when the observed proportion is 0
    or 1: every resample of eight successes is eight successes, so an 8/8 suite
    reports a 95% interval of [1.000, 1.000]. On a task suite of six to fifteen
    cases -- which is what these are -- that reads as a precise result and is not
    one. The exact interval for 8/8 is [0.63, 1.00], which is the same data
    telling the truth.

    This matters more here than it would elsewhere because the entire design
    position of the evaluation layer is that a number without its uncertainty,
    its baseline and its denominator is not a number a reviewer can act on.
    A degenerate interval defeats that from the inside, and it does so worst on
    the small, hand-curated suites that are the most likely to be quoted.
    """
    if n <= 0:
        return (0.0, 0.0)
    alpha = 1 - confidence

    # Lower bound: the smallest p at which P(X >= successes) has fallen to
    # alpha/2. That tail rises with p, so it is *below* alpha/2 to the left of the
    # crossing.
    lower = 0.0
    if successes > 0:
        lo, hi = 0.0, 1.0
        for _ in range(200):
            mid = (lo + hi) / 2
            if 1.0 - _binom_cdf(successes - 1, n, mid) < alpha / 2:
                lo = mid
            else:
                hi = mid
        lower = (lo + hi) / 2

    # Upper bound: the largest p at which P(X <= successes) has fallen to
    # alpha/2. That tail falls with p, so it is *above* alpha/2 to the left of
    # the crossing -- the opposite comparison, and getting it the same way round
    # as the one above produces an interval that excludes the estimate.
    upper = 1.0
    if successes < n:
        lo, hi = 0.0, 1.0
        for _ in range(200):
            mid = (lo + hi) / 2
            if _binom_cdf(successes, n, mid) > alpha / 2:
                lo = mid
            else:
                hi = mid
        upper = (lo + hi) / 2
    return (lower, upper)


def bootstrap_ci(
    outcomes: Sequence[Outcome],
    *,
    metric: str = "end_to_end",
    n_resamples: int = 10_000,
    confidence: float = 0.95,
    seed: int = 20261014,
) -> tuple[float, float]:
    """Percentile bootstrap CI for a rate over cases.

    Seeded, so that a report regenerated from the same run is identical. An
    unseeded CI is a number that moves when nothing changed, which destroys the
    only thing a regression suite is for.
    """
    values = np.array([_metric_value(o, metric) for o in outcomes], dtype=float)
    if values.size == 0:
        return (0.0, 0.0)
    rng = random.Random(seed)
    n = values.size
    samples = np.empty(n_resamples, dtype=float)
    for i in range(n_resamples):
        idx = [rng.randrange(n) for _ in range(n)]
        samples[i] = values[idx].mean()
    lo = float(np.percentile(samples, (1 - confidence) / 2 * 100))
    hi = float(np.percentile(samples, (1 + confidence) / 2 * 100))
    return (lo, hi)


def _metric_value(outcome: Outcome, metric: str) -> float:
    if metric == "end_to_end":
        return 1.0 if outcome.correct else 0.0
    if metric == "false_answer":
        return 1.0 if outcome.is_false_answer else 0.0
    if metric == "abstention":
        return 1.0 if outcome.abstained else 0.0
    if metric == "answered":
        return 1.0 if (not outcome.abstained and outcome.correct) else 0.0
    raise ValueError(f"unknown metric {metric!r}")


def mcnemar(
    a: Sequence[Outcome], b: Sequence[Outcome]
) -> tuple[float, float, str]:
    """Compare two systems on the same cases. Returns (statistic, p, note).

    McNemar's test looks only at the *discordant* pairs -- cases where exactly one
    system was right -- because the cases where both were right, or both wrong,
    carry no information about which is better. The exact binomial form is used
    rather than the chi-square approximation: the discordant count in a legal
    evaluation is routinely below 25, where the approximation is not trustworthy.

    This is the difference between "system A scored 0.84 and system B scored 0.81"
    and "system A is better", and the two are routinely confused in evaluation
    write-ups.
    """
    if len(a) != len(b):
        raise ValueError("McNemar's test requires matched cases")
    b_only = 0  # A wrong, B right
    a_only = 0  # A right, B wrong
    for x, y in zip(a, b, strict=True):
        if x.case_id != y.case_id:
            raise ValueError(
                f"case ids do not align: {x.case_id!r} vs {y.case_id!r}. "
                "McNemar's test is only valid on matched pairs."
            )
        if x.correct and not y.correct:
            a_only += 1
        elif y.correct and not x.correct:
            b_only += 1

    n = a_only + b_only
    if n == 0:
        return (0.0, 1.0, "the two systems agreed on every case; no difference")
    # Exact two-sided binomial test with p=0.5 over the discordant pairs.
    k = min(a_only, b_only)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / (2**n)
    p = min(1.0, 2 * tail)
    note = (
        f"{a_only} case(s) where A alone was right, {b_only} where B alone was; "
        f"{n} discordant pair(s) out of {len(a)}"
    )
    return (float(k), p, note)


def pass_pow_k(pass_flags: Sequence[bool], k: int) -> float:
    """pass^k: the probability all k independent attempts succeed.

    The right summary for a system that is run repeatedly -- an agent retried
    after a transient failure, a pipeline rerun per matter. It falls off a cliff
    as k grows even when single-attempt accuracy looks respectable: a system that
    is right 90% of the time is right five times in a row 59% of the time, and a
    lawyer who runs it once per matter will eventually be the other 41%.
    """
    if not pass_flags:
        return 0.0
    rate = sum(1 for f in pass_flags if f) / len(pass_flags)
    return rate**k


def wilson_interval(successes: int, n: int, *, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval. Used where a closed form is preferable to a
    bootstrap -- reporting a rate on a small subset, for instance."""
    if n == 0:
        return (0.0, 0.0)
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - margin), min(1.0, centre + margin))
