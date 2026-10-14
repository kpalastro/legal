/**
 * The citation-binding review queue: the second job, and a different one.
 *
 * `docs/06` is explicit that the three surfaces "share a corpus and a design
 * language and nothing else". This one is a worklist, not an editor: a reviewer
 * is looking at machine readings of judgment prose and deciding whether each is
 * right.
 *
 * It exists because two gates in `ratio/kb/graph.py` deliberately *do not act* on
 * uncertain evidence, and evidence that is recorded and set aside has to go
 * somewhere a human will see it. The three buckets are the whole design, and the
 * distinction between the last two is the subtle one:
 *
 *   - counted: this is why the status is what it is;
 *   - advisory: recorded, not counted. **Confirming it makes it count.**
 *   - hierarchy-blocked: a court that cannot bind. **Confirming it cannot change
 *     the status**, because the gate is the hierarchy of courts and not the
 *     reader's confidence in the binding.
 *
 * The `via` field is always shown on an advisory edge. A treatment attached to a
 * neutral citation is stronger evidence than one attached to a case *name*
 * resolved through the corpus-derived alias table, and a lawyer reviewing the edge
 * is entitled to know which they are looking at. It is also why `needs_review`
 * fires on `via == "name_reference"` regardless of confidence.
 *
 * One thing this surface still cannot do: write `confirmed_by`. The field is
 * honoured throughout the graph and the verifier and no code path sets it, so the
 * queue can be read and not worked. `docs/11` calls that checkpoint G2 and calls
 * it the most consequential unimplemented piece of the governance story. The
 * buttons below are present and disabled, and say why, because a reviewer who
 * clicks one and is told nothing has learned less than a reviewer who can see
 * that the control is not connected yet.
 */

import type { AuthorityStatus, TreatmentEdge } from "../types";
import { BUCKETS, authorityStatus } from "../status";
import { Badge, Empty, Mark, Panel } from "./bits";

export function AuthorityQueue({
  authority,
  corpus,
  onSelect,
  busy,
}: {
  authority: AuthorityStatus | null;
  corpus: { id: string; title: string; key: string }[];
  onSelect: (id: string) => void;
  busy: boolean;
}) {
  return (
    <div style={{ padding: 16, display: "grid", gridTemplateColumns: "260px 1fr", gap: 16 }}>
      <div>
        <p className="col-head">
          <span>Authorities</span>
          <span>{corpus.length}</span>
        </p>
        <ul className="tree">
          {corpus.map((item) => (
            <li key={item.id} className="tree-node">
              <button
                type="button"
                className="tree-button"
                aria-current={authority?.key === item.key}
                onClick={() => onSelect(item.id)}
              >
                {item.title}
                <span className="id">{item.key}</span>
              </button>
            </li>
          ))}
        </ul>
      </div>

      <div>
        {busy ? (
          <Empty>Reading the treatment record…</Empty>
        ) : !authority ? (
          <Empty>Pick an authority to see how the corpus holds it.</Empty>
        ) : (
          <AuthorityDetail authority={authority} />
        )}
      </div>
    </div>
  );
}

function AuthorityDetail({ authority }: { authority: AuthorityStatus }) {
  const status = authorityStatus(authority.status);
  const total =
    authority.counted.length + authority.advisory.length + authority.hierarchy_blocked.length;

  return (
    <div>
      <h2 style={{ margin: "0 0 4px", fontSize: 18 }}>{authority.title}</h2>
      <p className="prov-head" style={{ marginTop: 0 }}>
        <Mark tone={status.tone}>{status.icon}</Mark>
        <span>{authority.status}</span>
        <span className="conf">{total} treatment edges recorded</span>
      </p>

      <Panel title="What this status means">
        <p style={{ marginTop: 0 }}>{status.say}</p>
        {/*
          The caveat is shown with the status or not at all. Docs/06 gives the
          wording and says why: without it the system would say "no treatment
          recorded" while holding the treatment, and "no evidence either way" is a
          lie told in the system's most reassuring register.
        */}
        {authority.caveat ? <p className="caveat">{authority.caveat}</p> : null}
      </Panel>

      {total === 0 ? (
        <Panel title="No treatment recorded" tone="warn">
          <p style={{ margin: 0 }}>
            The corpus holds no treatment edge for this authority. That is not the same as
            good law — it means there is no evidence either way, and the status above says
            so in those words rather than showing a reassuring green tick.
          </p>
        </Panel>
      ) : null}

      <Bucket
        bucket="counted"
        edges={authority.counted}
        emptySay="No counted treatment. Nothing in the corpus is capable of moving this status."
      />
      <Bucket bucket="advisory" edges={authority.advisory} emptySay="No advisory edges." />
      <Bucket
        bucket="blocked"
        edges={authority.hierarchy_blocked}
        emptySay="No treatment from a court that cannot bind this one."
      />
    </div>
  );
}

function Bucket({
  bucket,
  edges,
  emptySay,
}: {
  bucket: "counted" | "advisory" | "blocked";
  edges: TreatmentEdge[];
  emptySay: string;
}) {
  const spec = BUCKETS[bucket];
  if (edges.length === 0) {
    return (
      <Panel title={`${spec.mark} ${spec.title}`}>
        <p style={{ margin: 0, color: "var(--fg-faint)" }}>
          {emptySay}
        </p>
      </Panel>
    );
  }
  return (
    <Panel title={`${spec.mark} ${spec.title}`} tone={bucket === "blocked" ? "warn" : undefined}>
      <p className="hint" style={{ marginTop: 0 }}>
        {spec.say}
      </p>
      {edges.map((edge, index) => (
        <Edge key={index} edge={edge} bucket={bucket} />
      ))}
    </Panel>
  );
}

function Edge({
  edge,
  bucket,
}: {
  edge: TreatmentEdge;
  bucket: "counted" | "advisory" | "blocked";
}) {
  const spec = BUCKETS[bucket];
  return (
    <div className="prov-block" style={{ paddingBottom: 10, borderBottom: "1px solid var(--line)" }}>
      <p className="prov-head">
        <Mark tone={spec.tone}>{spec.mark}</Mark>
        <span>
          {edge.treatment} by <span className="mono">{edge.source}</span>
          {edge.paragraph ? <span className="mono"> at [{edge.paragraph}]</span> : null}
        </span>
        <span className="conf">conf {edge.confidence.toFixed(2)}</span>
        {/*
          Always shown on an advisory edge. A treatment bound to a case *name*
          resolved through the alias table is weaker evidence than one bound to a
          neutral citation, and the reviewer is entitled to know which.
        */}
        {bucket === "advisory" && edge.via ? (
          <Badge tone={edge.via === "name_reference" ? "advisory" : "plain"}>
            via {edge.via}
          </Badge>
        ) : null}
        {edge.needs_review ? <Badge tone="advisory">needs review</Badge> : null}
      </p>

      {edge.quote ? (
        // The quote travels with the edge, always, and never summarised. It is
        // not decoration; it is the evidence.
        <blockquote className="quote">{edge.quote}</blockquote>
      ) : (
        <p className="conf">no quotation recorded for this edge</p>
      )}

      <div className="row">
        <button type="button" className="act" disabled title="Not connected yet — see below">
          confirm
        </button>
        <button type="button" className="act" disabled title="Not connected yet — see below">
          reject
        </button>
        {!spec.canConfirm ? (
          <span className="conf">
            confirming this cannot change the status — the gate is the hierarchy of courts
          </span>
        ) : null}
      </div>
    </div>
  );
}

export function QueueNotWired() {
  return (
    <Panel title="This queue cannot be worked yet — and that is the honest state" tone="warn">
      <p style={{ marginTop: 0 }}>
        The buttons above are disabled because nothing in the system writes{" "}
        <span className="mono">confirmed_by</span>. The field is honoured throughout the
        authority graph and the verifier, and no code path sets it, so this queue can be
        read and not worked. Reviewing an edge here would produce a confirmation that does
        not persist and does not change any status.
      </p>
      <p style={{ marginBottom: 0 }} className="conf">
        Recorded as checkpoint G2 in docs/11-governance-assurance.md, which calls it the
        most consequential unimplemented piece of the governance story. The surface shows
        the control and the reason it does not work, rather than showing nothing and
        leaving a reviewer to conclude the queue is empty.
      </p>
    </Panel>
  );
}
