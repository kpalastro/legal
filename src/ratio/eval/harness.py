"""The evaluation harness: task files in, scored results out.

Two commitments distinguish this from an ad-hoc test script.

**Every metric carries its baseline.** An accuracy of 0.78 means nothing until
you know what a system that answers the majority label every time would have
scored, and what a system that abstains on everything would have scored. Both
baselines are computed by the harness and printed alongside, so that a number
which fails to beat them is visible as such. Most published benchmarks for legal
classification are cleared by a majority-class baseline, and the fact is usually
left out.

**Abstention is a category, not a failure.** The harness counts abstentions
separately and reports two accuracies, because a system that abstains on the hard
third and is right on the rest is behaving *better* than one that guesses
confidently, and any single-number score will say the opposite.

Task files live outside the package (``eval/tasks/*.yaml``) so that a lawyer can
add cases without touching code. Each case declares its own ground truth, the
authority it came from, and -- where more than one conclusion is defensible --
the alternatives that also count.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import yaml

from ratio.eval.metrics import (
    ABSTAIN,
    Outcome,
    Report,
    exact_ci,
    mcnemar,
    pass_pow_k,
    score,
)

CASE_KINDS = ("rule", "retrieval", "verification", "treatment")


@dataclass
class CaseSpec:
    """One evaluation case, with its provenance."""

    id: str
    kind: str
    prompt: str
    expected: str
    alternatives: tuple[str, ...] = ()
    facts: dict[str, Any] = field(default_factory=dict)
    query: str | None = None
    claim: dict[str, Any] = field(default_factory=dict)
    as_at: str | None = None
    # The authority that fixes the ground truth. A case with no authority is a
    # case whose expected answer is somebody's opinion, and the harness says so
    # rather than scoring it silently.
    authority: str | None = None
    notes: str | None = None
    # Set on cases where the only correct behaviour is to refuse.
    expect_refusal: bool = False


@dataclass
class TaskSuite:
    id: str
    title: str
    domain: str
    version: str
    cases: list[CaseSpec] = field(default_factory=list)
    source: Path | None = None

    @property
    def unanchored(self) -> list[CaseSpec]:
        """Cases with no authority behind their expected answer."""
        return [c for c in self.cases if not c.authority]

    @classmethod
    def load(cls, path: Path) -> TaskSuite:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        suite = cls(
            id=raw["id"],
            title=raw["title"],
            domain=raw.get("domain", ""),
            version=str(raw.get("version", "0")),
            source=path,
        )
        for entry in raw.get("cases", []):
            kind = entry.get("kind", "rule")
            if kind not in CASE_KINDS:
                raise ValueError(
                    f"{path.name}: case {entry.get('id')!r} has unknown kind "
                    f"{kind!r}; expected one of {', '.join(CASE_KINDS)}"
                )
            suite.cases.append(
                CaseSpec(
                    id=str(entry["id"]),
                    kind=kind,
                    prompt=entry.get("prompt", ""),
                    expected=str(entry["expected"]),
                    alternatives=tuple(str(a) for a in entry.get("also_accept", ())),
                    facts=entry.get("facts", {}) or {},
                    query=entry.get("query"),
                    claim=entry.get("claim", {}) or {},
                    as_at=entry.get("as_at"),
                    authority=entry.get("authority"),
                    notes=entry.get("notes"),
                    expect_refusal=bool(entry.get("expect_refusal", False)),
                )
            )
        return suite

    @classmethod
    def load_dir(cls, directory: Path) -> list[TaskSuite]:
        return [cls.load(p) for p in sorted(directory.glob("*.yaml"))]


class SystemUnderTest(Protocol):
    """Anything that can answer a case. A stub is a system; so is a model.

    ``kinds`` names the case kinds this system has an opinion about. It is part
    of the contract rather than a convenience: without it, every system gets
    scored on every case, and the verifier's score on "is this a notifiable
    action" is a number with no meaning that nonetheless flows into comparisons
    and guardrail counts.
    """

    name: str
    kinds: frozenset[str]

    def predict(self, case: CaseSpec) -> str:
        ...


# -- baselines -------------------------------------------------------------


class AbstainSystem:
    """Always declines. The floor, and a surprisingly informative one: any system
    scoring below it is worse than useless, and any system whose *answered*
    accuracy is below it has a serious problem."""

    name = "baseline:abstain-always"

    def predict(self, case: CaseSpec) -> str:
        return ABSTAIN


class MajoritySystem:
    """Always answers the most common expected label in the suite.

    Fitted on the suite it is scored on, which flatters it and is deliberate:
    the point of a baseline is to be *generous*. A system that cannot beat a
    generously-fitted majority baseline has no demonstrated signal at all.
    """

    name = "baseline:majority-class"

    def __init__(self, suites: list[TaskSuite]) -> None:
        counts: Counter[str] = Counter()
        for suite in suites:
            counts.update(c.expected for c in suite.cases)
        self.label = counts.most_common(1)[0][0] if counts else ABSTAIN

    def predict(self, case: CaseSpec) -> str:
        return self.label


# -- results ---------------------------------------------------------------


@dataclass
class SuiteResult:
    suite_id: str
    system_name: str
    report: Report
    outcomes: list[Outcome]
    ci: tuple[float, float] = (0.0, 0.0)
    false_answer_ci: tuple[float, float] = (0.0, 0.0)
    # Cases where the system answered and the expected behaviour was refusal, or
    # vice versa. These are the guardrail failures, and they are listed by id
    # because they must be read individually.
    guardrail_failures: list[str] = field(default_factory=list)
    # Cases in the suite this system does not answer at all, because they ask a
    # kind of question it is not built for. Reported, because a suite half of
    # which was skipped is not a suite that passed.
    skipped: int = 0

    @property
    def passes(self) -> bool:
        """Whether the suite passes. Guardrail failures always fail the suite:
        a single case where the system failed to refuse a fabricated authority
        is not offset by a hundred correct ones."""
        return not self.guardrail_failures

    def render(self) -> str:
        if not self.outcomes:
            return (
                f"=== {self.suite_id}  /  {self.system_name} ===\n"
                f"no cases of a kind this system answers "
                f"({self.skipped} skipped). Not evaluated."
            )
        lines = [
            f"=== {self.suite_id}  /  {self.system_name} ===",
            self.report.render(),
            "",
            f"end-to-end accuracy 95% CI  [{self.ci[0]:.3f}, {self.ci[1]:.3f}]",
            f"false-answer rate 95% CI    [{self.false_answer_ci[0]:.3f}, "
            f"{self.false_answer_ci[1]:.3f}]",
        ]
        if self.skipped:
            lines.append(
                f"{self.skipped} case(s) skipped: not a kind this system answers"
            )
        if self.guardrail_failures:
            lines.append("")
            lines.append(
                f"GUARDRAIL FAILURES ({len(self.guardrail_failures)}): "
                + ", ".join(self.guardrail_failures)
            )
        return "\n".join(lines)


def run_suite(
    suite: TaskSuite,
    system: SystemUnderTest,
    *,
    bootstrap: bool = True,
) -> SuiteResult:
    """Score one system against the cases of ``suite`` it is able to answer.

    The filtering is not an optimisation. A case kind names the *question* it
    asks, and each system answers one kind of question. Scoring the verifier on
    "was this acquisition notifiable?" produces a number, and the number is
    meaningless -- but worse, it is not inert. It enters the comparison against
    other systems, it produces spurious "guardrail failures" for a system that
    never ran on those cases, and it buries the signal that a suite exists to
    carry. A system with no opinion on a case has not failed it; it has not
    answered it, and the two must not be reported alike.

    A case whose kind no system handles is reported as skipped, so that a task
    file with a typo in `kind` announces itself rather than silently contributing
    nothing.
    """
    handled = getattr(system, "kinds", None)
    cases = [c for c in suite.cases if handled is None or c.kind in handled]
    skipped = len(suite.cases) - len(cases)

    outcomes: list[Outcome] = []
    guardrail_failures: list[str] = []

    for case in cases:
        try:
            prediction = system.predict(case)
        except Exception as exc:  # noqa: BLE001
            # A system that raises has not answered. It is recorded as an
            # abstention with the exception named, never as a crash that aborts
            # the run -- one bad case must not be able to suppress the rest of
            # the report.
            prediction = ABSTAIN
            case.notes = f"{case.notes or ''} [system raised {type(exc).__name__}: {exc}]"

        outcome = Outcome(
            case_id=case.id,
            expected=case.expected,
            predicted=prediction,
            alternatives=case.alternatives,
        )
        outcomes.append(outcome)

        if case.expect_refusal:
            # The correct answer here is a refusal, so a clean answer is the
            # failure. This is inverted from ordinary scoring on purpose.
            refused = prediction == ABSTAIN or prediction == "__refused__"
            if not refused:
                guardrail_failures.append(case.id)

    if not cases:
        # Nothing to score. Reported, not silently returned as a perfect score:
        # a system that abstains from a suite entirely is not a system with a
        # 100% record, and a zero-case suite must never render as one.
        return SuiteResult(
            suite_id=suite.id,
            system_name=system.name,
            report=score([]),
            outcomes=[],
            guardrail_failures=[],
            skipped=skipped,
        )

    report = score(outcomes)
    result = SuiteResult(
        suite_id=suite.id,
        system_name=system.name,
        report=report,
        outcomes=outcomes,
        guardrail_failures=guardrail_failures,
        skipped=skipped,
    )
    if bootstrap:
        # Exact binomial intervals, not the percentile bootstrap. On a suite of
        # six to fifteen cases the bootstrap has zero width at 0 and 1, so a
        # suite that passes everything reports a 95% interval of [1.000, 1.000]
        # -- the most precise-looking result available, produced by the smallest
        # amount of evidence. ``exact_ci`` reports the same data honestly. The
        # bootstrap is kept in ``ratio.eval.metrics`` for statistics where the
        # quantity is not a proportion over cases.
        n = len(outcomes)
        result.ci = exact_ci(report.n_correct, n)
        result.false_answer_ci = exact_ci(report.n_false, n)
    return result


@dataclass
class Comparison:
    suite_id: str
    a: str
    b: str
    statistic: float
    p_value: float
    note: str

    @property
    def significant(self) -> bool:
        return self.p_value < 0.05

    def render(self) -> str:
        verdict = "significant" if self.significant else "not significant"
        return (
            f"{self.a} vs {self.b} on {self.suite_id}: p={self.p_value:.4f} "
            f"({verdict} at 0.05)\n  {self.note}"
        )


def compare(
    suite_id: str, result_a: SuiteResult, result_b: SuiteResult
) -> Comparison:
    statistic, p, note = mcnemar(result_a.outcomes, result_b.outcomes)
    return Comparison(
        suite_id=suite_id,
        a=result_a.system_name,
        b=result_b.system_name,
        statistic=statistic,
        p_value=p,
        note=note,
    )


def load_suites(directory: Path) -> list[TaskSuite]:
    if not directory.exists():
        raise FileNotFoundError(
            f"no task directory at {directory}. The evaluation suite is data, "
            "not code; point --tasks at a directory containing *.yaml suites."
        )
    suites = TaskSuite.load_dir(directory)
    if not suites:
        raise FileNotFoundError(f"{directory} contains no *.yaml task suites")
    return suites


__all__ = [
    "ABSTAIN",
    "AbstainSystem",
    "CaseSpec",
    "Comparison",
    "MajoritySystem",
    "SuiteResult",
    "SystemUnderTest",
    "TaskSuite",
    "compare",
    "exact_ci",
    "load_suites",
    "pass_pow_k",
    "run_suite",
]
