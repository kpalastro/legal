"""Evaluation harness, metrics, and the systems under test.

Deliberately separate from the components it measures. An evaluator that shares
code with the thing being evaluated will eventually be adjusted until it agrees
with it.
"""

from ratio.eval.harness import (
    ABSTAIN,
    AbstainSystem,
    CaseSpec,
    Comparison,
    MajoritySystem,
    SuiteResult,
    SystemUnderTest,
    TaskSuite,
    compare,
    load_suites,
    run_suite,
)
from ratio.eval.metrics import (
    Outcome,
    Report,
    bootstrap_ci,
    mcnemar,
    pass_pow_k,
    score,
    wilson_interval,
)

__all__ = [
    "ABSTAIN",
    "AbstainSystem",
    "CaseSpec",
    "Comparison",
    "MajoritySystem",
    "Outcome",
    "Report",
    "SuiteResult",
    "SystemUnderTest",
    "TaskSuite",
    "bootstrap_ci",
    "compare",
    "load_suites",
    "mcnemar",
    "pass_pow_k",
    "run_suite",
    "score",
    "wilson_interval",
]
