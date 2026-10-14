/**
 * The provenance rail: the node's authority, visible while it is being edited.
 *
 * This is the surface's central requirement, and `docs/06` gives the reason in
 * one sentence -- "If the provenance is one click away in a side panel, the check
 * happens. If it is one click away in a *different view*, it does not."
 *
 * So this column is not a disclosure, a modal or a tab. It is beside the editor
 * at all times, and it shows the words.
 *
 * What it shows, and why each part is here:
 *
 *   - the paragraph text itself, verbatim and unreflowed, so the expert compares
 *     the quotation against the source rather than against their memory of it;
 *   - whether the quotation is in that paragraph, checked rather than asserted;
 *   - the anchor, scoped by judge where the judgment has several authors;
 *   - the authority's status *and its caveat*. The caveat is in the flow of the
 *     page, never in a tooltip: "a caveat in a tooltip is a caveat nobody reads";
 *   - a loud marker when the material is synthetic, because the fixture corpus
 *     must never be read as genuine authority.
 */

import type { NodeView, ProvenanceEntry } from "../types";
import { authorityStatus, refStatus, isRefDefect } from "../status";
import { Badge, Empty, Mark, Panel, Quote } from "./bits";

export function ProvenanceRail({
  node,
  provenance,
  synthetic,
}: {
  node: NodeView | null;
  provenance: ProvenanceEntry[];
  synthetic: boolean;
}) {
  if (!node) {
    return <Empty>Select a node to see the authority behind it.</Empty>;
  }

  const mine = provenance.filter((entry) => entry.node === node.id);
  const unresolved = mine.filter((entry) => isRefDefect(entry.status));
  const missing = node.children.filter((id) => id === node.id);

  return (
    <div>
      <p className="col-head">
        <span>Provenance</span>
        <span>
          {mine.length} reference{mine.length === 1 ? "" : "s"}
          {unresolved.length > 0 ? (
            <>
              {" · "}
              <span style={{ color: "var(--stop)" }}>{unresolved.length} not holding</span>
            </>
          ) : null}
        </span>
      </p>

      {mine.length === 0 ? (
        <Panel title="No authority" tone="defect">
          <p style={{ margin: 0 }}>
            This node names no judgment paragraph and no provision. It is a rule the
            system cannot trace to any source, which is the one thing this encoding
            exists to make impossible. The engine will refuse to load it.
          </p>
        </Panel>
      ) : null}

      {missing.length > 0 ? (
        <Panel title="A child does not exist" tone="defect">
          <p style={{ margin: 0 }}>
            This node lists a child that is not in the ruleset:{" "}
            <span className="mono">{missing.join(", ")}</span>. A pointer to a node that
            is not there cannot be evaluated.
          </p>
        </Panel>
      ) : null}

      {mine.map((entry) => (
        <RefBlock key={`${entry.node}-${entry.index}`} entry={entry} synthetic={synthetic} />
      ))}
    </div>
  );
}

function RefBlock({ entry, synthetic }: { entry: ProvenanceEntry; synthetic: boolean }) {
  const resolved = refStatus(entry.status);
  const tone = resolved.tone;
  const defective = isRefDefect(entry.status);
  const authority = entry.authority_status ? authorityStatus(entry.authority_status) : null;

  return (
    <div className="prov-block">
      <p className="prov-head">
        <Mark tone={tone}>{defective ? "!" : "✓"}</Mark>
        <Badge tone={tone}>{resolved.short}</Badge>
        {entry.citation ? <span>{entry.citation}</span> : null}
      </p>

      <p style={{ margin: "4px 0 0", fontSize: 13 }}>
        {entry.anchor ? (
          <span className="mono" style={{ color: "var(--accent)" }}>
            {entry.anchor}
          </span>
        ) : (
          <span className="mono">{entry.ref.kind}</span>
        )}
        {entry.heading ? <span style={{ color: "var(--fg-muted)" }}> — {entry.heading}</span> : null}
      </p>

      {entry.compiled_as_at ? (
        <p className="conf">
          provision compiled as at {entry.compiled_as_at}; the law may have moved since
        </p>
      ) : null}

      {defective ? (
        <p className="caveat" style={{ borderLeftColor: "var(--stop)", color: "#ffd9da" }}>
          {resolved.say}
        </p>
      ) : null}

      {entry.detail ? (
        <p className="conf" style={{ marginTop: 4 }}>
          {entry.detail}
        </p>
      ) : null}

      {entry.ref.quote ? (
        <>
          <p className="conf" style={{ marginTop: 8 }}>
            the node quotes:
          </p>
          <Quote missing={defective}>{entry.ref.quote}</Quote>
        </>
      ) : (
        <p className="conf" style={{ marginTop: 8 }}>
          no quotation — this node points at the source without relying on its words
        </p>
      )}

      {entry.text ? (
        <>
          <p className="conf" style={{ marginTop: 10 }}>
            {entry.ref.kind === "judgment_paragraph"
              ? "the paragraph reads:"
              : "the provision reads:"}
          </p>
          <Quote>{entry.text}</Quote>
        </>
      ) : null}

      {synthetic || entry.synthetic ? (
        <p className="caveat" style={{ borderLeftColor: "var(--unknown)", color: "#d8d2ff" }}>
          This is fixture material. It was written to exercise the system, it is not a
          real judgment or a real provision, and nothing here may be cited.
        </p>
      ) : null}

      {authority ? (
        <div style={{ marginTop: 8 }}>
          <p className="prov-head">
            <Mark tone={authority.tone}>{authority.icon}</Mark>
            <span>{authority.say}</span>
          </p>
          {entry.authority_caveat ? (
            <p className="caveat">{entry.authority_caveat}</p>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
