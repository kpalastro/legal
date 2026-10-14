/**
 * Preview: the derivation, not the answer.
 *
 * `docs/06`: "The proof tree is not an advanced view. `ProofTree.render()` exists
 * so the derivation can be shown; if the expert-facing surface hides it behind a
 * disclosure triangle that nobody opens, the system has become unexplainable in
 * the ordinary use case."
 *
 * So there is no disclosure triangle. Every finding is on the page, in tree
 * order, with its outcome, its statement, what was observed and -- where the node
 * carries one -- the authority that requires it.
 *
 * And there is no verdict. The conclusion is rendered as one of three words, and
 * `undetermined` is rendered as an abstention with the gaps named, because "a
 * bare 'I cannot answer' is not acceptable, because it leaves the user with
 * nothing to do" -- and because an interface that draws `undetermined` as a
 * greyed-out "No" has converted a correctly abstaining system into a confident
 * one.
 */

import { useState } from "react";

import type { NodeView, PreviewResult } from "../types";
import { conclusionTone, conclusionWord } from "../status";
import { Badge, Field, Mark, Panel, Text } from "./bits";

const OUTCOME_TONE: Record<string, "ok" | "stop" | "unknown" | "plain"> = {
  satisfied: "ok",
  defeated: "stop",
  not_satisfied: "plain",
  unknown: "unknown",
  undetermined: "unknown",
};

export function PreviewPanel({
  nodes,
  result,
  busy,
  fields,
  onRun,
}: {
  nodes: NodeView[];
  result: PreviewResult | null;
  busy: boolean;
  /** The fact fields the ruleset's leaves test, so facts can be entered. */
  fields: string[];
  onRun: (facts: Record<string, unknown>) => void;
}) {
  const [draft, setDraft] = useState<Record<string, string>>({});

  return (
    <div style={{ padding: 16 }}>
      <Panel title="Facts">
        <p className="hint" style={{ marginTop: 0 }}>
          Every field a leaf in this ruleset tests. Leave one blank and the conclusion
          will come back <span className="undetermined">undetermined</span> with that field
          named as the gap — which is the honest answer, not a failure.
        </p>
        <div className="grid-3">
          {fields.map((field) => (
            <Field key={field} label={field}>
              <Text
                mono
                value={draft[field] ?? ""}
                onChange={(value) => setDraft((d) => ({ ...d, [field]: value }))}
                placeholder="yes / no / 20"
              />
            </Field>
          ))}
        </div>
        <button
          type="button"
          className="act primary"
          disabled={busy}
          onClick={() => onRun(parseFacts(draft))}
        >
          {busy ? "Evaluating…" : "Evaluate against these facts"}
        </button>
      </Panel>

      {!result ? (
        <p className="empty">
          No evaluation yet. The conclusion will arrive with its whole derivation.
        </p>
      ) : (
        <>
          <Panel title="Conclusion">
            <p style={{ marginTop: 0, fontSize: 16 }}>
              <Mark tone={conclusionTone(result.conclusion)}>
                {result.conclusion === "undetermined" ? "?" : "•"}
              </Mark>{" "}
              {result.conclusion === "undetermined" ? (
                <strong className="undetermined">{conclusionWord(result.conclusion)}</strong>
              ) : (
                <strong>{conclusionWord(result.conclusion)}</strong>
              )}
            </p>
            <p className="conf">
              {result.ruleset} v{result.version} ·{" "}
              {result.is_approved ? (
                <span style={{ color: "var(--ok)" }}>approved</span>
              ) : (
                <span style={{ color: "var(--stop)" }}>
                  NOT APPROVED — this output is unapproved and must not be relied on
                </span>
              )}
            </p>

            {result.defeaters_fired.length > 0 ? (
              <p className="caveat" style={{ borderLeftColor: "var(--blocked)" }}>
                Defeated by{" "}
                <span className="mono">{result.defeaters_fired.join(", ")}</span>. Any one
                defeater firing defeats the conclusion, however completely the elements
                are made out.
              </p>
            ) : null}

            {result.gaps.length > 0 ? (
              <>
                <p style={{ marginBottom: 4 }}>
                  <strong>Undetermined because these facts are not established:</strong>
                </p>
                <ul className="problems">
                  {result.gaps.map((gap) => (
                    <li key={gap}>
                      <span className="problem-code">{gap}</span>
                    </li>
                  ))}
                </ul>
                <p className="conf">
                  These are fields a user can establish, named rather than implied. A bare
                  "I cannot answer" would leave you with nothing to do.
                </p>
              </>
            ) : null}
          </Panel>

          <Panel title="Derivation — every node the engine visited">
            <p className="hint" style={{ marginTop: 0 }}>
              In tree order. This is not an advanced view: it is what you are checking.
            </p>
            <ul className="tree">
              {result.findings.map((finding) => {
                const node = nodes.find((n) => n.id === finding.node);
                const tone = OUTCOME_TONE[finding.outcome] ?? "unknown";
                return (
                  <li key={finding.node} className="tree-node">
                    <div className="row">
                      <Mark tone={tone}>
                        {tone === "ok" ? "✓" : tone === "stop" ? "✗" : "·"}
                      </Mark>
                      <Badge tone={tone}>{finding.outcome}</Badge>
                      <span className="mono" style={{ fontSize: 12 }}>
                        {finding.node}
                      </span>
                    </div>
                    <p style={{ margin: "2px 0 0 22px" }}>{finding.statement}</p>
                    {finding.observed ? (
                      <p className="conf" style={{ margin: "0 0 0 22px" }}>
                        observed: {finding.observed}
                      </p>
                    ) : null}
                    {finding.note ? (
                      <p className="conf" style={{ margin: "0 0 0 22px" }}>
                        {finding.note}
                      </p>
                    ) : null}
                    {node && node.kind === "exception" ? (
                      <p className="conf" style={{ margin: "2px 0 0 22px" }}>
                        a defeater of <span className="mono">{node.exception_of}</span>
                      </p>
                    ) : null}
                    {finding.provenance.length > 0 ? (
                      <p className="conf" style={{ margin: "0 0 0 22px" }}>
                        authority:{" "}
                        {finding.provenance
                          .map(
                            (p) =>
                              p.judgment
                                ? `${p.judgment} [${p.paragraph}]`
                                : (p.provision_key ?? p.kind),
                          )
                          .join("; ")}
                      </p>
                    ) : null}
                  </li>
                );
              })}
            </ul>
          </Panel>
        </>
      )}
    </div>
  );
}

/** `yes`/`no` become booleans, numerics become numbers, everything else a string. */
function parseFacts(draft: Record<string, string>): Record<string, unknown> {
  const facts: Record<string, unknown> = {};
  for (const [field, raw] of Object.entries(draft)) {
    const value = raw.trim();
    if (value === "") continue;
    if (value === "yes" || value === "true") facts[field] = true;
    else if (value === "no" || value === "false") facts[field] = false;
    else if (!Number.isNaN(Number(value))) facts[field] = Number(value);
    else facts[field] = value;
  }
  return facts;
}
