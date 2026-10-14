/**
 * Approval: the one control this surface deliberately makes inconvenient.
 *
 * `docs/06` is unambiguous about why. `ratio rules approve` prints the digest and
 * a ready-to-paste block, and then says "and set `status: approved`" -- it does
 * not do it. "The approval is the expert's act, and a tool that performs it for
 * them turns a governance control into a build step." A "Sign off" button that
 * writes the approval record would be the single most tempting and most damaging
 * thing here, because it converts a considered act into a click with the same
 * visual weight as saving a draft.
 *
 * So the flow is:
 *
 *   1. The expert states who they are and in what capacity. Both are required:
 *      an approval with no author is not a signature.
 *   2. They see the diff against the last signed revision. Docs/06: "A surface
 *      that shows the expert a diff of what changed since the last approval is
 *      doing the work that makes the sign-off meaningful."
 *   3. They see the digest -- the hash the signature will *carry*, which is the
 *      payload with `status` forced to approved.
 *   4. They type it back. Copying a hash is not reading one, and the value of the
 *      approval is that somebody looked at the artefact it binds. The service
 *      cannot check that the expert read the rules; it can check that they had
 *      the digest in front of them. That is this step.
 *
 * Nothing is written until step 4, and the digest is displayed before and after.
 */

import { useMemo, useState } from "react";

import type { Diff, SignatureBlock } from "../types";
import { Badge, Empty, Field, Panel, Text } from "./bits";

export function ApprovalPanel({
  block,
  busy,
  refusal,
  result,
  onSubmit,
}: {
  block: SignatureBlock | null;
  busy: boolean;
  refusal: { reason: string; message: string; expected?: unknown; given?: unknown } | null;
  result: { signed?: string; is_approved: boolean } | null;
  onSubmit: (body: {
    approver: string;
    role: string;
    note: string;
    confirm_digest: string;
  }) => void;
}) {
  const [approver, setApprover] = useState("");
  const [role, setRole] = useState("");
  const [note, setNote] = useState("");
  const [confirm, setConfirm] = useState("");

  const digest = block?.digest ?? "";
  const matches = useMemo(
    () => confirm.trim().toLowerCase() === digest.toLowerCase() && digest !== "",
    [confirm, digest],
  );

  if (!block) return <Empty>Computing what a signature would bind…</Empty>;

  if (result?.is_approved) {
    return (
      <Panel title="Signed">
        <p style={{ marginTop: 0 }}>
          This revision is approved, and the approval carries the hash of the revision as
          it stands. Edit anything below and it stops binding: the hash covers the whole
          payload, so an edited ruleset is reported as edited since sign-off and{" "}
          <span className="mono">RuleEngine</span> refuses to evaluate it at all.
        </p>
        <p className="conf">
          previous digest{" "}
          <span className="mono">{digest.slice(0, 16)}…</span> · written to{" "}
          <span className="mono">{result.signed ?? block.ruleset}</span>
        </p>
      </Panel>
    );
  }

  return (
    <div>
      <Panel title="Step 1 — who is signing, and in what capacity">
        <div className="grid-2">
          <Field label="Approver" hint="Required. An approval with no author is not a signature.">
            <Text value={approver} onChange={setApprover} placeholder="A. Expert" />
          </Field>
          <Field label="Capacity" hint={'Required. e.g. "Partner, M&A".'}>
            <Text value={role} onChange={setRole} placeholder="Special Counsel, Competition" />
          </Field>
        </div>
        <Field label="Note" hint="Recorded with the approval. Why you are satisfied.">
          <Text value={note} onChange={setNote} />
        </Field>
      </Panel>

      <Panel
        title="Step 2 — what changed since the last signed revision"
        tone={block.diff_vs_signed ? undefined : "warn"}
      >
        {block.diff_vs_signed ? (
          <DiffView diff={block.diff_vs_signed} />
        ) : (
          <p style={{ margin: 0 }}>
            No signed revision is on record, so there is nothing to diff against. This
            would be the first approval of this ruleset — which means every line below is
            new to the signature, and the whole ruleset is what is being signed.
          </p>
        )}
      </Panel>

      <Panel
        title={block.signable ? "Step 3 — the digest this signature will carry" : "Not signable"}
        tone={block.signable ? undefined : "defect"}
      >
        {!block.signable ? (
          <>
            <p style={{ marginTop: 0 }}>
              This revision does not validate. Signing it would produce an "approved"
              rulebook that the engine refuses to load — which is worse than a draft,
              because a draft is at least honestly unusable.
            </p>
            <ul className="problems">
              {block.issues.map((issue, index) => (
                <li key={index}>
                  <span className="problem-code">{issue}</span>
                </li>
              ))}
            </ul>
          </>
        ) : (
          <>
            <p style={{ marginTop: 0 }}>
              Not the hash of the ruleset as it stands. It is the hash of the ruleset{" "}
              <em>as it will stand once approved</em> — the payload with{" "}
              <span className="mono">status: approved</span>. Hashing the draft and then
              setting the status is a defect this project shipped for a while: the digest
              covered a value the act of signing changes, so no approval ever bound, and
              the resulting ruleset made the engine raise.
            </p>
            <p className="digest">{digest}</p>
            <p className="conf">
              version <span className="mono">{block.version}</span> · status now{" "}
              <span className="mono">{block.current_status}</span>
            </p>
          </>
        )}
      </Panel>

      <Panel title="Step 4 — type the digest back">
        <p style={{ marginTop: 0 }}>
          Copying a hash is not reading one. This is the friction that makes the act
          deliberate: the service cannot check that you read the rules, but it can check
          that you had the digest in front of you.
        </p>
        <Field label="Confirm the digest" hint="Paste or type it. Nothing is written until it matches.">
          <Text mono value={confirm} onChange={setConfirm} placeholder={digest} />
        </Field>
        <div className="row">
          <button
            type="button"
            className="act primary"
            disabled={busy || !matches || !block.signable || !approver.trim() || !role.trim()}
            onClick={() => onSubmit({ approver, role, note, confirm_digest: confirm })}
          >
            {busy ? "Recording…" : "Record the approval"}
          </button>
          {!matches && confirm !== "" ? (
            <Badge tone="stop">the digest does not match — nothing will be written</Badge>
          ) : null}
          {matches ? <Badge tone="ok">digest matches</Badge> : null}
        </div>
      </Panel>

      {block.block ? (
        <Panel title="The approval block">
          <p className="hint" style={{ marginTop: 0 }}>
            What <span className="mono">ratio rules approve</span> prints. It is here so
            that the same artefact can also be recorded by hand, in a file, through the
            CLI — the surface is not the only way to sign, and should not be.
          </p>
          <pre className="digest" style={{ margin: 0 }}>
            {block.block}
          </pre>
        </Panel>
      ) : null}

      {refusal ? (
        <Panel title="Refused" tone="defect">
          <p style={{ marginTop: 0 }}>{refusal.message}</p>
          <p className="conf">reason: {refusal.reason}</p>
          {refusal.expected ? (
            <p className="conf">
              the digest of this revision is{" "}
              <span className="mono">{String(refusal.expected)}</span>
            </p>
          ) : null}
        </Panel>
      ) : null}
    </div>
  );
}

function DiffView({ diff }: { diff: Diff }) {
  const total = diff.changed.length + diff.added.length + diff.removed.length;
  if (total === 0) {
    return (
      <p style={{ margin: 0 }}>
        This revision is identical to the last signed one. There is nothing to review —
        and nothing to renew, unless you are signing for the first time.
      </p>
    );
  }
  return (
    <>
      <p style={{ marginTop: 0, color: "var(--fg-muted)" }}>{diff.summary}</p>
      <table>
        <thead>
          <tr>
            <th>field</th>
            <th>signed</th>
            <th>now</th>
          </tr>
        </thead>
        <tbody>
          {diff.changed.map((line, index) => (
            <tr key={`c${index}`}>
              <td className="mono">{line.path}</td>
              <td className="diff-del">{line.before}</td>
              <td className="diff-add">{line.after}</td>
            </tr>
          ))}
          {diff.added.map((line, index) => (
            <tr key={`a${index}`}>
              <td className="mono">{line.path}</td>
              <td />
              <td className="diff-add">+ {line.after}</td>
            </tr>
          ))}
          {diff.removed.map((line, index) => (
            <tr key={`r${index}`}>
              <td className="mono">{line.path}</td>
              <td className="diff-del">− {line.before}</td>
              <td />
            </tr>
          ))}
        </tbody>
      </table>
    </>
  );
}
