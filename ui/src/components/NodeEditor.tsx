/**
 * The editor. It writes to nothing.
 *
 * Every change here goes into an in-memory candidate and is posted to
 * `/candidate`, which computes the same payload `GET` does -- issues, unresolved
 * provenance, anchors, the diff against the last signed revision -- without
 * writing a byte. Saving is a separate, named action, and it is the only one.
 *
 * The structural controls are arranged around the defect the surface is for. A
 * node's kind is not a free choice between four equal options: choosing `any_of`
 * where the law requires `all_of` is the mistake that a ruleset written from the
 * statutory text alone makes, and it is invisible in the output. So the choice is
 * presented with what it means, and choosing `exception` is presented as what it
 * is -- not a conjunct, but something that defeats the conclusion.
 */

import type { CandidateNode, NodeKind, NodeView, Predicate, ProvenanceRef } from "../types";
import { Area, Badge, Field, Panel, Text } from "./bits";

const KIND_NOTE: Record<NodeKind, string> = {
  all_of:
    "Every conjunct must hold. Use this when the authorities require both limbs — " +
    "the mistake this surface exists to catch is an `all_of` written as an `any_of`.",
  any_of: "At least one limb must hold. Check the provenance before choosing this.",
  leaf: "Tested directly against the facts. Requires a predicate.",
  exception:
    "A defeater. It does not need to be true for its parent to hold, and if it fires " +
    "it defeats the conclusion. Do not encode it as a sibling conjunct: that encodes " +
    "a different rule.",
  delegate_bn: "Delegates to a credence network. The tree cannot conclude without it.",
};

const OPS = ["eq", "ne", "in", "not_in", "gt", "gte", "lt", "lte", "exists", "not_exists"];

const REF_KINDS: ProvenanceRef["kind"][] = [
  "judgment_paragraph",
  "statute",
  "proposition",
];

export function NodeEditor({
  node,
  nodes,
  onChange,
  disabled,
}: {
  node: NodeView;
  /** Every node, so children and `exception_of` can be picked from what exists. */
  nodes: NodeView[];
  onChange: (next: CandidateNode) => void;
  disabled: boolean;
}) {
  // `NodeView` and `CandidateNode` are the same shape by construction -- the
  // service builds the view from the model field for field -- so the edit is a
  // copy rather than a conversion. If they ever diverge, this is where it shows.
  const candidate: CandidateNode = { ...node };

  function patch(changes: Partial<CandidateNode>) {
    onChange({ ...candidate, ...changes });
  }

  function patchRef(index: number, changes: Partial<ProvenanceRef>) {
    patch({
      provenance: candidate.provenance.map((ref, at) =>
        at === index ? { ...ref, ...changes } : ref,
      ),
    });
  }

  const childCandidates = nodes.filter(
    (other) => other.id !== node.id && other.kind !== "exception",
  );

  return (
    <div>
      <div className="row" style={{ marginBottom: 10 }}>
        <Badge tone="plain">{node.id}</Badge>
        <Badge tone={node.kind === "exception" ? "blocked" : "plain"}>{node.kind}</Badge>
        <span className="spacer" style={{ flex: 1 }} />
      </div>

      <Field label="Label" hint="What a reader of the review sheet sees.">
        <Text
          value={node.label}
          onChange={(label) => patch({ label })}
          placeholder="Notifiable action"
        />
      </Field>

      <Field
        label="Statement"
        hint="The node in words, as the expert would state the test. Shown in the proof tree."
      >
        <Area
          value={node.statement}
          onChange={(statement) => patch({ statement })}
          rows={3}
        />
      </Field>

      <Field label="Kind" hint={KIND_NOTE[node.kind]}>
        <select
          value={node.kind}
          disabled={disabled}
          onChange={(event) => {
            const kind = event.target.value as NodeKind;
            patch({
              kind,
              // An exception is not a conjunct of the parent it defeats. Moving
              // to `exception` without setting `exception_of` leaves a node that
              // validates and is attached to nothing.
              exception_of: kind === "exception" ? (node.exception_of ?? parentOf(node)) : null,
              predicate: kind === "leaf" || kind === "exception" ? node.predicate : null,
            });
          }}
        >
          {(Object.keys(KIND_NOTE) as NodeKind[]).map((kind) => (
            <option key={kind} value={kind}>
              {kind}
            </option>
          ))}
        </select>
      </Field>

      {node.kind === "exception" ? (
        <Panel title="This node is a defeater">
          <p className="hint" style={{ margin: 0 }}>
            It defeats <span className="mono">{node.exception_of ?? "—"}</span> when it
            fires. Its own conjuncts, if it has any, must all hold for it to fire.
          </p>
        </Panel>
      ) : null}

      {(node.kind === "leaf" || node.kind === "exception") && (
        <PredicateEditor
          predicate={node.predicate}
          onChange={(predicate) => patch({ predicate })}
        />
      )}

      {node.kind === "delegate_bn" ? (
        <div className="grid-2">
          <Field label="Credence question" hint="The network node this delegates to.">
            <Text
              mono
              value={node.bn_query ?? ""}
              onChange={(bn_query) => patch({ bn_query: bn_query || null })}
              placeholder="firb.national_interest_concern"
            />
          </Field>
          <Field label="Threshold" hint="Conclusion fires at or above this posterior.">
            <input
              type="number"
              step="0.01"
              min="0"
              max="1"
              value={node.bn_threshold ?? ""}
              onChange={(event) =>
                patch({
                  bn_threshold:
                    event.target.value === "" ? null : Number(event.target.value),
                })
              }
            />
          </Field>
        </div>
      ) : null}

      {node.kind !== "leaf" ? (
        <ChildrenEditor
          node={node}
          candidates={childCandidates}
          onChange={(children) => patch({ children })}
        />
      ) : null}

      <ProvenanceEditor
        provenance={node.provenance}
        onPatch={patchRef}
        onAdd={() =>
          patch({
            provenance: [
              ...candidate.provenance,
              {
                kind: "judgment_paragraph",
                judgment_id: null,
                paragraph: null,
                provision_key: null,
                proposition_id: null,
                quote: null,
                note: null,
              },
            ],
          })
        }
        onRemove={(index) =>
          patch({ provenance: candidate.provenance.filter((_, at) => at !== index) })
        }
      />
    </div>
  );
}

/** A node's parent by declaration, if it has exactly one. Used only as a default. */
function parentOf(node: NodeView): string | null {
  return node.exception_of;
}

function PredicateEditor({
  predicate,
  onChange,
}: {
  predicate: Predicate | null;
  onChange: (next: Predicate | null) => void;
}) {
  if (predicate === null) {
    return (
      <Panel title="Predicate">
        <p className="hint" style={{ margin: "0 0 8px" }}>
          This node is a leaf with no predicate. It cannot be evaluated, and{" "}
          <span className="mono">validate_ruleset</span> will refuse the revision.
        </p>
        <button
          type="button"
          className="act"
          onClick={() => onChange({ field: "", op: "eq", value: "" })}
        >
          Add a predicate
        </button>
      </Panel>
    );
  }
  return (
    <div className="grid-3">
      <Field label="Fact field" hint="The field a user can establish.">
        <Text
          mono
          value={predicate.field}
          onChange={(field) => onChange({ ...predicate, field })}
          placeholder="action.is_exempt_action"
        />
      </Field>
      <Field label="Operator">
        <select
          value={predicate.op}
          onChange={(event) => onChange({ ...predicate, op: event.target.value })}
        >
          {OPS.map((op) => (
            <option key={op} value={op}>
              {op}
            </option>
          ))}
        </select>
      </Field>
      <Field label="Value" hint="JSON: true, 20, or [1, 2] for `in`.">
        <Text
          mono
          value={String(predicate.value ?? "")}
          onChange={(raw) => onChange({ ...predicate, value: parseValue(raw) })}
        />
      </Field>
    </div>
  );
}

function parseValue(raw: string): string | number | boolean {
  const trimmed = raw.trim();
  if (trimmed === "true") return true;
  if (trimmed === "false") return false;
  if (trimmed !== "" && !Number.isNaN(Number(trimmed))) return Number(trimmed);
  return raw;
}

function ChildrenEditor({
  node,
  candidates,
  onChange,
}: {
  node: NodeView;
  candidates: NodeView[];
  onChange: (children: string[]) => void;
}) {
  return (
    <Panel
      title={
        node.kind === "all_of"
          ? "Conjuncts — every one must hold"
          : node.kind === "any_of"
            ? "Limbs — one is enough"
            : "Children"
      }
    >
      {node.children.length === 0 ? (
        <p className="hint" style={{ margin: "0 0 8px" }}>
          No children. A node of kind <span className="mono">{node.kind}</span> with none
          cannot be evaluated.
        </p>
      ) : (
        <ol style={{ margin: "0 0 8px", paddingLeft: 20 }}>
          {node.children.map((id, index) => (
            <li key={`${id}-${index}`} style={{ marginBottom: 4 }}>
              <span className="mono" style={{ fontSize: 12 }}>
                {id}
              </span>{" "}
              <button
                type="button"
                className="act"
                style={{ padding: "1px 6px" }}
                aria-label={`remove ${id}`}
                onClick={() =>
                  onChange(node.children.filter((_, at) => at !== index))
                }
              >
                remove
              </button>{" "}
              {index > 0 ? (
                <button
                  type="button"
                  className="act"
                  style={{ padding: "1px 6px" }}
                  aria-label={`move ${id} up`}
                  onClick={() => onChange(swap(node.children, index, index - 1))}
                >
                  ↑
                </button>
              ) : null}{" "}
              {index < node.children.length - 1 ? (
                <button
                  type="button"
                  className="act"
                  style={{ padding: "1px 6px" }}
                  aria-label={`move ${id} down`}
                  onClick={() => onChange(swap(node.children, index, index + 1))}
                >
                  ↓
                </button>
              ) : null}
            </li>
          ))}
        </ol>
      )}
      <label className="field" style={{ margin: 0 }}>
        <span className="label">Add a child</span>
        <select
          value=""
          onChange={(event) => {
            if (event.target.value) onChange([...node.children, event.target.value]);
          }}
        >
          <option value="">— pick a node —</option>
          {candidates
            .filter((other) => !node.children.includes(other.id))
            .map((other) => (
              <option key={other.id} value={other.id}>
                {other.label} ({other.id})
              </option>
            ))}
        </select>
      </label>
      <p className="hint" style={{ marginBottom: 0 }}>
        Order is the order the expert wrote. It is what the review sheet's column order
        follows, so it is not cosmetic.
      </p>
    </Panel>
  );
}

function swap(items: string[], a: number, b: number): string[] {
  const next = [...items];
  const first = next[a];
  const second = next[b];
  if (first === undefined || second === undefined) return items;
  next[a] = second;
  next[b] = first;
  return next;
}

function ProvenanceEditor({
  provenance,
  onPatch,
  onAdd,
  onRemove,
}: {
  provenance: ProvenanceRef[];
  onPatch: (index: number, changes: Partial<ProvenanceRef>) => void;
  onAdd: () => void;
  onRemove: (index: number) => void;
}) {
  return (
    <Panel title="Provenance — the authority that requires this node">
      <p className="hint">
        Every node points at a paragraph of a judgment or a provision of an Act. The
        engine refuses to load a node without one, and a quotation that is not verbatim
        is not a quotation — it is a paraphrase wearing a citation.
      </p>
      {provenance.length === 0 ? (
        <p className="hint">
          This node names no authority. That is the thing this system exists to prevent.
        </p>
      ) : null}
      {provenance.map((ref, index) => (
        <div key={index} className="panel" style={{ background: "var(--bg-sunken)" }}>
          <div className="row" style={{ marginBottom: 8 }}>
            <Badge tone="plain">reference {index + 1}</Badge>
            <span style={{ flex: 1 }} />
            <button
              type="button"
              className="act danger"
              style={{ padding: "1px 8px" }}
              onClick={() => onRemove(index)}
            >
              remove
            </button>
          </div>
          <div className="grid-3">
            <Field label="Kind">
              <select
                value={ref.kind}
                onChange={(event) =>
                  onPatch(index, { kind: event.target.value as ProvenanceRef["kind"] })
                }
              >
                {REF_KINDS.map((kind) => (
                  <option key={kind} value={kind}>
                    {kind}
                  </option>
                ))}
              </select>
            </Field>
            {ref.kind === "judgment_paragraph" ? (
              <>
                <Field label="Judgment id" hint="As the corpus holds it, e.g. au/fcafc/2019/77.">
                  <Text
                    mono
                    value={ref.judgment_id ?? ""}
                    onChange={(judgment_id) => onPatch(index, { judgment_id: judgment_id || null })}
                  />
                </Field>
                <Field
                  label="Paragraph"
                  hint="The bare number; the anchor is scoped by judge when it has to be."
                >
                  <Text
                    mono
                    value={ref.paragraph ?? ""}
                    onChange={(paragraph) => onPatch(index, { paragraph: paragraph || null })}
                  />
                </Field>
              </>
            ) : ref.kind === "statute" ? (
              <Field label="Provision key">
                <Text
                  mono
                  value={ref.provision_key ?? ""}
                  onChange={(provision_key) =>
                    onPatch(index, { provision_key: provision_key || null })
                  }
                />
              </Field>
            ) : (
              <Field label="Proposition id">
                <Text
                  mono
                  value={ref.proposition_id ?? ""}
                  onChange={(proposition_id) =>
                    onPatch(index, { proposition_id: proposition_id || null })
                  }
                />
              </Field>
            )}
          </div>
          <Field
            label="Quotation"
            hint={
              ref.kind === "judgment_paragraph"
                ? "Verbatim, exactly as the paragraph reads. The check is exact: typographic shape is folded, punctuation is not."
                : "Optional for a provision. Quote it if the words are doing work."
            }
          >
            <Area
              mono
              rows={3}
              value={ref.quote ?? ""}
              onChange={(quote) => onPatch(index, { quote: quote || null })}
              placeholder="The power to nominate one of six directors is a minority power..."
            />
          </Field>
          <Field label="Note" hint="Why this authority, in the expert's words. Kept in the file.">
            <Area
              rows={2}
              value={ref.note ?? ""}
              onChange={(note) => onPatch(index, { note: note || null })}
            />
          </Field>
        </div>
      ))}
      <button type="button" className="act" onClick={onAdd}>
        Add a reference
      </button>
    </Panel>
  );
}
