/**
 * The review sheet: the artefact an expert annotates to answer *how do I know the
 * rule says what I meant?*
 *
 * `docs/06`: "Walking the decision table and marking each row agree/disagree is a
 * finite, checkable task; reading the YAML is not."
 *
 * The three things the sheet is careful about, and all three are carried here
 * rather than re-decided:
 *
 *   - **The last column is the expert's, and it is empty.** The docstring in
 *     `generate_cases.py` says the artefact is "a document for a lawyer to
 *     disagree with, not a report of what the system concluded". A sheet that
 *     pre-fills the expected answer collects confirmation, not review. So this
 *     column starts blank and stays blank until a person fills it in, and it is
 *     not written back anywhere: the sheet is for reading.
 *   - **An inert leaf is a defect.** A leaf whose value never changes the
 *     conclusion is either redundant or mis-wired, and either way it is a node
 *     the expert believes is doing work and is not. `ratio cases` exits non-zero
 *     on an unhealthy coverage report, so the surface shows it the same way.
 *   - **An unexercised defeater is reported as a generator defect as well.** The
 *     generator can fail to exercise an exception because of a limitation in the
 *     generator rather than in the ruleset, and reporting it only as a ruleset
 *     defect "hands the reviewer a defect they cannot act on".
 */

import { useState } from "react";

import type { ReviewSheet as Sheet } from "../types";
import { conclusionTone, conclusionWord } from "../status";
import { Badge, Empty, Mark, Panel } from "./bits";

type Verdict = "agree" | "disagree" | "";

export function ReviewSheet({
  sheet,
  onReload,
}: {
  sheet: Sheet | null;
  onReload: (limit: number) => void;
}) {
  const [verdicts, setVerdicts] = useState<Record<string, Verdict>>({});
  const [shown, setShown] = useState(40);

  if (!sheet) return <Empty>Generating the decision table…</Empty>;

  const coverage = sheet.coverage;
  const marked = Object.values(verdicts).filter((v) => v !== "").length;

  return (
    <div style={{ padding: 16 }}>
      <p className="row" style={{ marginTop: 0 }}>
        <Badge tone={coverage.is_healthy ? "ok" : "stop"}>
          {coverage.is_healthy ? "coverage healthy" : "coverage NOT healthy"}
        </Badge>
        <span className="conf">
          {sheet.total} generated cases over {sheet.ruleset} v{sheet.version}
        </span>
        <span style={{ flex: 1 }} />
        <span className="conf">
          {marked} of {sheet.shown} rows marked by you
        </span>
      </p>

      {!coverage.is_healthy ? (
        <Panel
          title="This ruleset has a coverage defect — `ratio cases` would exit non-zero"
          tone="defect"
        >
          <p style={{ marginTop: 0 }}>
            A rulebook that passes every structural check and still does not do what the
            expert believes it does is the defect this whole surface exists to find. These
            are the ways the generator found it:
          </p>
          <CoverageList
            heading="Inert leaves — the value never changed the outcome"
            items={coverage.inert_leaves}
            say="Either redundant, or mis-wired. Either way it is a node you believe is doing work and is not."
          />
          <CoverageList
            heading="Unexercised defeaters — never fired in any generated case"
            items={coverage.unexercised_defeaters}
            say="This may be a limitation of the generator rather than of the ruleset. It is reported as both, because reporting it only as a ruleset defect hands you a defect you cannot act on."
          />
          <CoverageList
            heading="Never determined"
            items={coverage.never_determined}
            say="No combination of facts produced a conclusion through this node."
          />
          <CoverageList
            heading="Unknown fields"
            items={coverage.unknown_fields}
            say="Facts the generator had to invent a range for."
          />
        </Panel>
      ) : (
        <Panel title="Coverage">
          <p style={{ margin: 0 }}>
            Every leaf changed at least one outcome, every defeater fired at least once,
            and every combination concluded. There is no coverage defect in this revision.
          </p>
        </Panel>
      )}

      <p className="hint">
        The right-hand column is yours and is deliberately empty. The system's conclusion
        is on the left; whether the rule says what you meant is the question, and it is
        not one this system can answer for you. Nothing here is written back.
      </p>

      <div style={{ overflow: "auto", maxHeight: "60vh" }}>
        <table>
          <thead>
            <tr>
              {sheet.fields.map((field) => (
                <th key={field} className="mono">
                  {field}
                </th>
              ))}
              <th>conclusion</th>
              <th>defeaters fired</th>
              <th>unresolved gaps</th>
              <th>your verdict</th>
            </tr>
          </thead>
          <tbody>
            {sheet.rows.map((row) => {
              const tone = conclusionTone(row.conclusion);
              return (
                <tr key={row.case_id}>
                  {sheet.fields.map((field) => (
                    <td key={field} className="mono">
                      {formatFact(row.facts[field])}
                    </td>
                  ))}
                  <td>
                    <Mark tone={tone}>{tone === "unknown" ? "?" : "•"}</Mark>
                    {row.conclusion === "undetermined" ? (
                      <span className="undetermined">{conclusionWord(row.conclusion)}</span>
                    ) : (
                      conclusionWord(row.conclusion)
                    )}
                  </td>
                  <td className="mono">
                    {row.defeaters_fired.length === 0 ? (
                      <span style={{ color: "var(--fg-faint)" }}>—</span>
                    ) : (
                      row.defeaters_fired.join(", ")
                    )}
                  </td>
                  <td className="mono">
                    {row.gaps.length === 0 ? (
                      <span style={{ color: "var(--fg-faint)" }}>—</span>
                    ) : (
                      row.gaps.join(", ")
                    )}
                  </td>
                  <td className="verdict">
                    <div className="row">
                      <button
                        type="button"
                        className="act"
                        style={{ padding: "1px 7px" }}
                        aria-pressed={verdicts[row.case_id] === "agree"}
                        onClick={() =>
                          setVerdicts((v) => ({
                            ...v,
                            [row.case_id]: v[row.case_id] === "agree" ? "" : "agree",
                          }))
                        }
                      >
                        agree
                      </button>
                      <button
                        type="button"
                        className="act"
                        style={{ padding: "1px 7px" }}
                        aria-pressed={verdicts[row.case_id] === "disagree"}
                        onClick={() =>
                          setVerdicts((v) => ({
                            ...v,
                            [row.case_id]: v[row.case_id] === "disagree" ? "" : "disagree",
                          }))
                        }
                      >
                        disagree
                      </button>
                    </div>
                    {verdicts[row.case_id] === "disagree" ? (
                      <p className="conf" style={{ color: "var(--stop)", marginBottom: 0 }}>
                        the encoding says something the law does not
                      </p>
                    ) : null}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      {sheet.shown < sheet.total ? (
        <p className="row" style={{ marginTop: 12 }}>
          <span className="conf">
            showing {sheet.shown} of {sheet.total}
          </span>
          <button
            type="button"
            className="act"
            onClick={() => {
              const next = shown + 40;
              setShown(next);
              onReload(next);
            }}
          >
            show more
          </button>
        </p>
      ) : null}
    </div>
  );
}

function CoverageList({
  heading,
  items,
  say,
}: {
  heading: string;
  items: string[];
  say: string;
}) {
  if (items.length === 0) return null;
  return (
    <div style={{ marginBottom: 8 }}>
      <p style={{ margin: "0 0 2px" }}>
        <strong>{heading}</strong>
      </p>
      <p className="conf" style={{ margin: "0 0 4px" }}>
        {say}
      </p>
      <p className="mono" style={{ margin: 0, fontSize: 12 }}>
        {items.join(", ")}
      </p>
    </div>
  );
}

function formatFact(value: unknown): string {
  if (value === undefined || value === null) return "—";
  if (typeof value === "boolean") return value ? "yes" : "no";
  return String(value);
}
