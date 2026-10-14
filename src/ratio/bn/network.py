"""Discrete Bayesian networks with exact inference.

Used for the parts of the law that rules genuinely cannot reach: standards
phrased as "substantial", "reasonable", "contrary to the national interest".
For those, the honest technical artefact is not a boolean but a probability,
and the honest engineering question is not "what does the model predict" but
"where did every number in this model come from, and how much does the answer
move if an expert disagrees".

Design commitments (see docs/05-bayesian-layer.md):

* **Exact inference only.** Enumeration and variable elimination, both
  implemented here, both tested against each other. An approximate answer that
  cannot be reproduced is worse than no answer in a system a court might have
  to look at.
* **No black boxes.** A CPT row that cannot be traced to an :class:`Elicitation`
  record is a validation error, not a warning.
* **Posterior, never verdict.** :meth:`CredenceEngine.query` returns a
  distribution plus sensitivities. It is structurally incapable of returning
  "yes".
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass

import numpy as np

from ratio.model import (
    AuthorityRef,
    CredenceNetwork,
    CredenceNode,
    PosteriorReport,
    topological_order,
)


class CredenceError(ValueError):
    """Raised when a network cannot be evaluated as specified."""


@dataclass(frozen=True)
class Factor:
    """A function from an assignment of ``scope`` variables to a table.

    ``table`` has shape ``(len(states[v0]), ..., len(states[vk]))`` matching
    ``scope``. This is the only data structure in the engine.
    """

    scope: tuple[str, ...]
    table: np.ndarray


class CredenceEngine:
    """Compiled network. Construct once per network version, query many times."""

    def __init__(self, network: CredenceNetwork) -> None:
        self.network = network
        self._order = topological_order(network.nodes)
        self._nodes: dict[str, CredenceNode] = {n.id: n for n in network.nodes}
        self._state_index: dict[str, dict[str, int]] = {
            n.id: {s.name: i for i, s in enumerate(n.states)} for n in network.nodes
        }
        self._validate()

    # -- validation --------------------------------------------------------

    def _validate(self) -> None:
        if not self.network.nodes:
            raise CredenceError(f"network {self.network.id!r} has no nodes")
        for node in self.network.nodes:
            if not node.states:
                raise CredenceError(f"node {node.id!r} has no states")
            expected_rows = 1
            for parent in node.parents:
                expected_rows *= len(self._nodes[parent].states)
            if len(node.cpt) != expected_rows:
                raise CredenceError(
                    f"node {node.id!r} has {len(node.cpt)} CPT rows, expected "
                    f"{expected_rows} for parents {node.parents}"
                )
        # Every node's every row must trace to an elicited distribution, and the
        # numbers must match what was elicited.
        elicited = {e.key: e for e in self.network.elicitations}
        if not elicited:
            raise CredenceError(
                f"network {self.network.id!r} carries no elicitation provenance; "
                "every CPT row must trace to an expert record"
            )
        missing: list[str] = []
        mismatched: list[str] = []
        for node in self.network.nodes:
            state_names = [s.name for s in node.states]
            for row, config in enumerate(self._parent_configs(node)):
                record = elicited.get((node.id, config))
                label = f"{node.id}|{'/'.join(config) or '(root)'}"
                if record is None:
                    missing.append(label)
                    continue
                if set(record.distribution) != set(state_names):
                    mismatched.append(
                        f"{label}: record names states "
                        f"{sorted(record.distribution)}, node declares {sorted(state_names)}"
                    )
                    continue
                for column, state_name in enumerate(state_names):
                    stated = record.distribution[state_name]
                    if abs(stated - float(node.cpt[row][column])) > 1e-6:
                        mismatched.append(
                            f"{label}/{state_name}: CPT says "
                            f"{float(node.cpt[row][column]):.4f}, elicitation record "
                            f"says {stated:.4f} (elicited from {record.elicited_from})"
                        )
        if missing:
            raise CredenceError(
                f"network {self.network.id!r} has {len(missing)} CPT row(s) with no "
                f"elicitation record, e.g. {missing[:3]}"
            )
        if mismatched:
            raise CredenceError(
                f"network {self.network.id!r} has {len(mismatched)} CPT value(s) that "
                "do not match the record of what the expert said. A model may not be "
                "tuned away from its provenance:\n  - " + "\n  - ".join(mismatched[:5])
            )

    def _parent_configs(self, node: CredenceNode) -> list[tuple[str, ...]]:
        """Parent state assignments in CPT row order (first parent varies slowest)."""
        if not node.parents:
            return [()]
        state_names = [
            [s.name for s in self._nodes[p].states] for p in node.parents
        ]
        return [tuple(combo) for combo in itertools.product(*state_names)]

    # -- factors -----------------------------------------------------------

    def _factors(self) -> list[Factor]:
        factors: list[Factor] = []
        for node_id in self._order:
            node = self._nodes[node_id]
            scope = (node.id, *node.parents)
            # A CPT is stored the way a table of elicitations is written down:
            # one row per parent configuration, one column per state of this
            # node. A factor is stored the other way round -- its axes are its
            # scope, and its first scope entry is the node itself. Reshaping the
            # CPT straight to the scope's shape silently reinterprets the same
            # numbers in the wrong layout, because both have the right number of
            # elements. It does not raise, and ``_validate`` does not catch it
            # either, since that compares the CPT against the elicitations in the
            # CPT's own layout. The state axis has to be moved in front of the
            # parent axes first.
            states_first = np.moveaxis(np.asarray(node.cpt, dtype=float), 1, 0)
            table = states_first.reshape(
                tuple(len(self._nodes[v].states) for v in scope)
            )
            factors.append(Factor(scope=scope, table=table))
        return factors

    @staticmethod
    def _restrict(factor: Factor, evidence: dict[str, int]) -> Factor | None:
        """Slice a factor down to observed values in its scope."""
        if not any(v in evidence for v in factor.scope):
            return factor
        index: list[int | slice] = []
        for var in factor.scope:
            index.append(evidence[var] if var in evidence else slice(None))
        new_scope = tuple(v for v in factor.scope if v not in evidence)
        sliced = factor.table[tuple(index)]
        return Factor(scope=new_scope, table=np.atleast_1d(sliced).astype(float))

    @staticmethod
    def _multiply(a: Factor, b: Factor) -> Factor:
        scope = tuple(dict.fromkeys((*a.scope, *b.scope)))

        def align(factor: Factor) -> np.ndarray:
            """Put ``factor``'s table into ``scope`` axis order, then pad.

            The padding reshape alone is not enough, and the reason is easy to
            miss because it usually looks right. ``scope`` is built as the first
            operand's variables followed by the second's, so the second
            operand's axes are frequently in a *different* order here than in
            its own scope -- and reshaping a table into a permutation of its own
            shape does not fail, it silently transposes the data. So the axes are
            permuted into ``scope`` order first, and only then is the size-1
            padding applied for variables this factor does not mention.
            """
            table = np.asarray(factor.table, dtype=float)
            present = [v for v in scope if v in factor.scope]
            if present:
                table = table.transpose([factor.scope.index(v) for v in present])
            shape = [1] * len(scope)
            for var in present:
                shape[scope.index(var)] = factor.table.shape[factor.scope.index(var)]
            return table.reshape(tuple(shape))

        return Factor(scope=scope, table=(align(a) * align(b)).astype(float))

    @staticmethod
    def _sum_out(factor: Factor, var: str) -> Factor:
        axis = factor.scope.index(var)
        return Factor(
            scope=tuple(v for v in factor.scope if v != var),
            table=factor.table.sum(axis=axis).astype(float),
        )

    # -- inference ---------------------------------------------------------

    def query(
        self,
        evidence: dict[str, str] | None = None,
        *,
        target: str | None = None,
    ) -> dict[str, float]:
        """P(target | evidence) as a dict of state -> probability.

        Raises if evidence names an unknown variable or state, rather than
        silently ignoring it -- a dropped observation in a legal model is a
        wrong answer presented confidently.
        """
        evidence = evidence or {}
        target = target or self.network.target
        if target not in self._nodes:
            raise CredenceError(f"unknown target {target!r}")
        encoded = self._encode_evidence(evidence)
        if target in encoded:
            dist = {s.name: 0.0 for s in self._nodes[target].states}
            dist[self._nodes[target].states[encoded[target]].name] = 1.0
            return dist
        return self._variable_elimination(encoded, target)

    def _encode_evidence(self, evidence: dict[str, str]) -> dict[str, int]:
        encoded: dict[str, int] = {}
        for var, state in evidence.items():
            if var not in self._nodes:
                raise CredenceError(f"evidence names unknown variable {var!r}")
            index = self._state_index[var].get(state)
            if index is None:
                raise CredenceError(
                    f"variable {var!r} has no state {state!r}; "
                    f"known states: {sorted(self._state_index[var])}"
                )
            encoded[var] = index
        return encoded

    def _variable_elimination(
        self, evidence: dict[str, int], target: str
    ) -> dict[str, float]:
        factors: list[Factor] = []
        for f in self._factors():
            restricted = self._restrict(f, evidence)
            if restricted is not None:
                factors.append(restricted)

        hidden = [
            v
            for v in self._order
            if v != target and v not in evidence
        ]
        for var in hidden:
            involved = [f for f in factors if var in f.scope]
            if not involved:
                continue
            factors = [f for f in factors if var not in f.scope]
            product = involved[0]
            for other in involved[1:]:
                product = self._multiply(product, other)
            factors.append(self._sum_out(product, var))

        result = factors[0]
        for other in factors[1:]:
            result = self._multiply(result, other)
        table = np.asarray(result.table, dtype=float)
        if result.scope != (target,):
            raise CredenceError(
                f"elimination left scope {result.scope}, expected ({target!r},)"
            )
        total = float(table.sum())
        if total <= 0.0:
            raise CredenceError(
                "evidence has zero probability under this network; the CPTs are "
                "inconsistent with the observations"
            )
        normalised = table / total
        return {
            s.name: float(normalised[i]) for i, s in enumerate(self._nodes[target].states)
        }

    def query_by_enumeration(
        self,
        evidence: dict[str, str] | None = None,
        *,
        target: str | None = None,
    ) -> dict[str, float]:
        """Reference implementation, kept for cross-checking.

        Deliberately naive: brute-force sum over every hidden assignment. Slow
        and obviously correct. ``tests/test_bn.py`` asserts the two agree.
        """
        evidence = evidence or {}
        target = target or self.network.target
        encoded = self._encode_evidence(evidence)
        state_names = {n.id: [s.name for s in n.states] for n in self.network.nodes}
        counts = {n.id: len(n.states) for n in self.network.nodes}
        node_ids = list(self._order)

        hidden = [v for v in node_ids if v not in encoded]
        totals = np.zeros(counts[target])
        evidence_mass = 0.0

        for combo in itertools.product(*(range(counts[v]) for v in hidden)):
            assignment = dict(zip(hidden, combo, strict=True))
            assignment.update(encoded)
            joint = 1.0
            for node_id in node_ids:
                node = self._nodes[node_id]
                row = 0
                for parent in node.parents:
                    row = row * counts[parent] + assignment[parent]
                joint *= float(node.cpt[row][assignment[node_id]])
                if joint == 0.0:
                    break
            if joint == 0.0:
                continue
            totals[assignment[target]] += joint
            evidence_mass += joint

        if evidence_mass <= 0.0:
            raise CredenceError("zero-probability evidence in enumeration")
        return {
            name: float(totals[i] / evidence_mass) for i, name in enumerate(state_names[target])
        }

    def prior(self, var: str) -> dict[str, float]:
        return self.query({}, target=var)

    # -- reporting ---------------------------------------------------------

    def sensitivities(
        self,
        evidence: dict[str, str] | None = None,
        *,
        target: str | None = None,
        top_k: int = 6,
    ) -> list[tuple[str, float]]:
        """How much each observed factor is moving the answer.

        For every variable in the evidence, recompute the posterior with that
        variable unobserved and again with it set to each of its other states,
        and record the largest total-variation distance from the baseline.

        The output answers the question an expert actually has: "which of my
        inputs should I argue about?" Not "what is the number".
        """
        evidence = evidence or {}
        target = target or self.network.target
        baseline = np.array(
            [self.query(evidence, target=target)[s.name] for s in self._nodes[target].states]
        )
        scores: list[tuple[str, float]] = []
        for var in evidence:
            reduced = {k: v for k, v in evidence.items() if k != var}
            alternatives: list[np.ndarray] = []
            alternatives.append(
                np.array(
                    [
                        self.query(reduced, target=target)[s.name]
                        for s in self._nodes[target].states
                    ]
                )
            )
            for state in self._nodes[var].states:
                if state.name == evidence[var]:
                    continue
                alt = dict(reduced)
                alt[var] = state.name
                alternatives.append(
                    np.array(
                        [
                            self.query(alt, target=target)[s.name]
                            for s in self._nodes[target].states
                        ]
                    )
                )
            worst = max(0.5 * float(np.abs(a - baseline).sum()) for a in alternatives)
            scores.append((var, worst))
        scores.sort(key=lambda pair: (-pair[1], pair[0]))
        return scores[:top_k]

    def posterior_report(
        self,
        evidence: dict[str, str] | None = None,
        *,
        target: str | None = None,
        top_k: int = 6,
    ) -> PosteriorReport:
        """The posterior, its sensitivities, its provenance and its caveat.

        This exists so that no caller has to remember the caveat. ``query`` will
        happily hand back a float to anything that asks, and it should -- it is the
        arithmetic layer. But a probability from a legal model is not meaningful
        without the sentence saying what it is not, so the assembled artefact --
        the thing a lawyer, a UI or an agent actually displays -- is built here,
        with the caveat attached and the elicitation provenance attached, and there
        is no way to construct one without them.

        The ``authorities`` are the elicitation records themselves. They are the
        honest answer to "where did these numbers come from": not a judgment,
        because no judgment states a prior, but the named person who stated one,
        which is why they are recorded as ``expert_statement``.
        """
        evidence = evidence or {}
        target = target or self.network.target
        node = self._nodes.get(target)
        if node is None:
            raise CredenceError(f"unknown target {target!r}")

        distribution = self.query(evidence, target=target)
        sensitivities = (
            tuple(self.sensitivities(evidence, target=target, top_k=top_k))
            if evidence
            else ()
        )

        authorities: list[AuthorityRef] = []
        seen: set[tuple[str, str]] = set()
        # Every node that could bear on the target: the target itself and its
        # ancestors. A factor that has no path to the target cannot be moving the
        # answer, and listing its elicitation would misrepresent the provenance.
        for node_id in self._ancestors(target) | {target}:
            for record in self.network.elicitations:
                if record.node_id != node_id:
                    continue
                stamp = (record.elicited_from, record.role)
                if stamp in seen:
                    continue
                seen.add(stamp)
                authorities.append(
                    AuthorityRef(
                        kind="expert_statement",
                        note=(
                            f"{record.elicited_from} ({record.role}) on "
                            f"{record.node_id}, by {record.method}, "
                            f"{record.elicited_at.date().isoformat()}"
                        ),
                        quote=record.raw_response,
                    )
                )

        caveat_parts = [
            text.strip() for text in self.network.caveats.values() if text.strip()
        ]
        if not caveat_parts:
            # A network with no caveat is one whose author has not said what it
            # does not answer. Say so, rather than emitting a bare number.
            caveat_parts.append(
                "This network carries no interpretation caveat. The posterior is a "
                "summary of elicited expert judgement and is not a legal conclusion."
            )
        if not self.network.approvals:
            caveat_parts.append(
                "The network is unapproved: no expert has signed this revision, so "
                "it must not be relied on."
            )

        return PosteriorReport(
            network_id=self.network.id,
            network_version=self.network.version,
            target=target,
            target_label=node.label,
            distribution=distribution,
            evidence=dict(evidence),
            sensitivities=sensitivities,
            authorities=tuple(authorities),
            caveat="  ".join(caveat_parts),
        )

    def _ancestors(self, node_id: str) -> set[str]:
        """Every node with a directed path to ``node_id``, excluding it."""
        out: set[str] = set()
        frontier = [node_id]
        while frontier:
            current = frontier.pop()
            for parent in self._nodes[current].parents:
                if parent not in out:
                    out.add(parent)
                    frontier.append(parent)
        return out

    def describe(self) -> str:
        """Human-readable network summary, for the expert authoring surface."""
        lines = [
            f"Network: {self.network.title}  ({self.network.id} v{self.network.version})",
            f"Question: {self.network.question}",
            f"Target:   {self._nodes[self.network.target].label}",
            "",
        ]
        for node in self.network.nodes:
            lines.append(f"  {node.id:28s} [{node.role}]  {node.label}")
            if node.parents:
                lines.append(f"      <- {', '.join(node.parents)}")
        return "\n".join(lines)
