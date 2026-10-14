/**
 * The shell: which ruleset, which tab, and the two banners that must never leave
 * the screen.
 *
 * The banners are the point of this file. `docs/06`: "The UI must show 'not
 * approved' wherever it shows a rule conclusion, and must not soften it into an
 * amber dot. Every rule conclusion this system can currently produce is
 * unapproved, and the interface's job is to make that impossible to miss — which
 * is also what makes the approval flow meaningful once it is used."
 *
 * So a draft ruleset carries a red banner in words, on every tab, for as long as
 * it is selected. And the synthetic-corpus banner is not dismissible at all: the
 * fixture material must never be readable as Australian law, and a dismissible
 * warning about fabricated authority is a warning that gets dismissed.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { api } from "./api";
import type {
  AuthorityStatus,
  Candidate,
  CandidateNode,
  CorpusAuthority,
  CorpusSummary,
  NodeView,
  PreviewResult,
  RulesetPayload,
  RulesetSummary,
  SignatureBlock,
} from "./types";
import type { Refusal } from "./types";
import { isRefusal } from "./types";
import { isRefDefect } from "./status";
import { ApprovalPanel } from "./components/ApprovalPanel";
import { AuthorityQueue, QueueNotWired } from "./components/AuthorityQueue";
import { NodeEditor } from "./components/NodeEditor";
import { NodeTree } from "./components/NodeTree";
import { PreviewPanel } from "./components/PreviewPanel";
import { ProvenanceRail } from "./components/ProvenanceRail";
import { ReviewSheet } from "./components/ReviewSheet";
import { Badge, Empty, Panel, Problems } from "./components/bits";

type Tab = "inspect" | "preview" | "sheet" | "approve" | "queue";

const TABS: { id: Tab; label: string; job: string }[] = [
  { id: "inspect", label: "Inspect & edit", job: "authoring" },
  { id: "preview", label: "Preview", job: "authoring" },
  { id: "sheet", label: "Review sheet", job: "authoring" },
  { id: "approve", label: "Approval", job: "authoring" },
  { id: "queue", label: "Citation queue", job: "review" },
];

export default function App() {
  const [corpus, setCorpus] = useState<CorpusSummary | null>(null);
  const [summaries, setSummaries] = useState<RulesetSummary[]>([]);
  const [rulesetId, setRulesetId] = useState<string>("");
  const [candidate, setCandidate] = useState<Candidate | null>(null);
  const [checked, setChecked] = useState<RulesetPayload | null>(null);
  const [selected, setSelected] = useState<string>("");
  const [tab, setTab] = useState<Tab>("inspect");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [dirty, setDirty] = useState(false);

  const [sheet, setSheet] = useState<import("./types").ReviewSheet | null>(null);
  const [signature, setSignature] = useState<SignatureBlock | null>(null);
  const [signResult, setSignResult] = useState<{ signed?: string; is_approved: boolean } | null>(
    null,
  );
  const [saveRefusal, setSaveRefusal] = useState<Refusal | null>(null);
  const [signRefusal, setSignRefusal] = useState<Refusal | null>(null);
  const [preview, setPreview] = useState<PreviewResult | null>(null);

  const [reloadToken, setReloadToken] = useState(0);
  const [authorities, setAuthorities] = useState<CorpusAuthority[]>([]);
  const [status, setStatus] = useState<AuthorityStatus | null>(null);
  const [fatal, setFatal] = useState<string | null>(null);

  // -- load ---------------------------------------------------------------

  useEffect(() => {
    (async () => {
      try {
        const [c, r] = await Promise.all([api.corpus(), api.rulesets()]);
        if (isRefusal(c) || isRefusal(r)) {
          setFatal("the service refused the corpus request");
          return;
        }
        setCorpus(c);
        setSummaries(r.rulesets);
        const first = r.rulesets[0];
        if (first) setRulesetId(first.id);
        else setFatal("no rulesets are loaded");
      } catch (cause) {
        setFatal(cause instanceof Error ? cause.message : String(cause));
      }
    })();
  }, []);

  useEffect(() => {
    if (!rulesetId) return;
    (async () => {
      setBusy(true);
      setError(null);
      const body = await api.ruleset(rulesetId);
      setBusy(false);
      if (isRefusal(body)) {
        setError(body.message);
        return;
      }
      setChecked(body);
      setCandidate(toCandidate(body));
      setSignature(null);
      setSignResult(null);
      setSaveRefusal(null);
      setSignRefusal(null);
      setDirty(false);
      const root = body.nodes.find((n) => n.id === body.ruleset.root) ?? body.nodes[0];
      setSelected(root?.id ?? "");
    })().catch((cause) => {
      setBusy(false);
      setError(cause instanceof Error ? cause.message : String(cause));
    });
  }, [rulesetId, reloadToken]);

  useEffect(() => {
    (async () => {
      const body = await api.authorities();
      if (!isRefusal(body)) setAuthorities(body.authorities);
    })().catch(() => undefined);
  }, []);

  // -- live candidate checking -------------------------------------------

  const timer = useRef<number | undefined>(undefined);
  const check = useCallback(
    (next: Candidate) => {
      window.clearTimeout(timer.current);
      timer.current = window.setTimeout(async () => {
        const body = await api.candidate(String(next.id), next);
        if (isRefusal(body)) {
          setSaveRefusal(body);
          return;
        }
        setChecked(body);
        setSaveRefusal(null);
      }, 260);
    },
    [],
  );

  // -- derived -----------------------------------------------------------

  const view = checked?.ruleset ?? null;
  const approvalStale = Boolean(view && !view.is_approved && view.approval_count > 0);

  const flaggedNodeIds = useMemo(() => {
    const ids = new Set<string>();
    for (const entry of checked?.provenance ?? []) {
      if (isRefDefect(entry.status)) ids.add(entry.node);
    }
    return ids;
  }, [checked]);

  const factFields = useMemo(() => {
    const fields = new Set<string>();
    for (const node of checked?.nodes ?? []) {
      if (node.predicate?.field) fields.add(node.predicate.field);
    }
    return [...fields].sort();
  }, [checked]);

  const selectedNode: NodeView | null =
    checked?.nodes.find((node) => node.id === selected) ?? null;

  // -- mutations ---------------------------------------------------------

  function editNode(next: CandidateNode) {
    if (!candidate) return;
    const replaced = {
      ...candidate,
      nodes: candidate.nodes.map((node) => (node.id === next.id ? next : node)),
    };
    commit(replaced);
  }

  function commit(next: Candidate) {
    // Editing an approved ruleset demotes it, in the open. The service will not
    // do this silently -- a service that quietly changes a governance field to
    // make a write succeed is the failure mode this whole surface is arranged
    // against -- so the editor does it explicitly and the journal records it.
    const demoted =
      view?.status === "approved" ? { ...next, status: "draft" as const } : next;
    setCandidate(demoted);
    setDirty(true);
    setSignResult(null);
    check(demoted);
  }

  async function save(editor: string, note: string) {
    if (!candidate) return;
    setBusy(true);
    const body = await api.save(String(candidate.id), candidate, editor, note || undefined);
    setBusy(false);
    if (isRefusal(body)) {
      setSaveRefusal(body);
      return;
    }
    setSaveRefusal(null);
    setChecked(body);
    setCandidate(toCandidate(body));
    setDirty(false);
    setSignResult(null);
    setSignature(null);
    const r = await api.rulesets();
    if (!isRefusal(r)) setSummaries(r.rulesets);
  }

  async function loadSignature() {
    if (!candidate) return;
    setBusy(true);
    const body = await api.signature(String(candidate.id), candidate, {
      approver: "",
      role: "",
    });
    setBusy(false);
    if (isRefusal(body)) {
      setSignRefusal(body);
      return;
    }
    setSignature(body);
  }

  async function sign(body: {
    approver: string;
    role: string;
    note: string;
    confirm_digest: string;
  }) {
    if (!candidate) return;
    setBusy(true);
    const response = await api.sign(String(candidate.id), candidate, {
      approver: body.approver,
      role: body.role,
      note: body.note || undefined,
      confirm_digest: body.confirm_digest,
    });
    setBusy(false);
    if (isRefusal(response)) {
      setSignRefusal(response);
      return;
    }
    setSignRefusal(null);
    setChecked(response);
    setCandidate(toCandidate(response));
    setSignResult({ signed: response.signed, is_approved: response.ruleset.is_approved });
    setDirty(false);
    const r = await api.rulesets();
    if (!isRefusal(r)) setSummaries(r.rulesets);
  }

  async function loadSheet(limit = 40) {
    if (!rulesetId) return;
    const body = await api.reviewSheet(rulesetId, limit);
    if (!isRefusal(body)) setSheet(body);
  }

  async function runPreview(facts: Record<string, unknown>) {
    if (!candidate) return;
    setBusy(true);
    const body = await api.preview(String(candidate.id), candidate, facts);
    setBusy(false);
    if (isRefusal(body)) {
      setError(body.message);
      return;
    }
    setPreview(body);
  }

  useEffect(() => {
    if (tab === "sheet" && !sheet) void loadSheet();
    if (tab === "approve" && !signature) void loadSignature();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tab]);

  // -- render ------------------------------------------------------------

  if (fatal) {
    return (
      <div className="error-box">
        <h2 style={{ marginTop: 0 }}>The authoring service is not reachable</h2>
        <p>{fatal}</p>
        <p className="conf">
          Start it with <span className="mono">ratio serve</span>, or{" "}
          <span className="mono">uvicorn ratio.authoring.api:build_app</span> from a
          script. This surface has no fixture of its own: every conclusion it shows is the
          service's.
        </p>
      </div>
    );
  }

  return (
    <div className="shell">
      <header className="topbar">
        <h1>RATIO · expert authoring surface</h1>
        <select
          value={rulesetId}
          onChange={(event) => setRulesetId(event.target.value)}
          aria-label="ruleset"
        >
          {summaries.map((item) => (
            <option key={item.id} value={item.id}>
              {item.title} — v{item.version} · {item.is_approved ? "approved" : "draft"}
            </option>
          ))}
        </select>
        {view ? (
          <>
            <Badge tone={view.is_approved ? "ok" : "stop"}>
              {view.is_approved ? "approved" : "DRAFT"}
            </Badge>
            <Badge tone="plain">v{view.version}</Badge>
            <Badge tone="plain">{checked?.nodes.length ?? 0} nodes</Badge>
          </>
        ) : null}
        {dirty ? <Badge tone="advisory">unsaved changes</Badge> : null}
        <span className="spacer" />
        <span className="store-line" title={corpus?.store.directory}>
          {corpus
            ? corpus.store.in_place
              ? `editing ${corpus.store.directory} IN PLACE`
              : `editing a copy · ${corpus.store.directory}`
            : ""}
        </span>
      </header>

      {/*
        Not dismissible, and it does not matter which tab is open. The corpus is
        invented for development, and a surface that lets this be dismissed is a
        surface that lets fabricated authority be read as law.
      */}
      {corpus?.synthetic ? (
        <div className="banner synthetic" role="status">
          <strong>Fixture corpus</strong>
          <span>
            Every judgment and provision here is synthetic — written to exercise the
            system, not taken from any court or Act. Nothing on this screen may be cited,
            and no conclusion from it is legal advice.
          </span>
        </div>
      ) : null}

      {view && !view.is_approved ? (
        <div className="banner unapproved" role="alert">
          <strong>Not approved</strong>
          <span>
            This revision carries no valid expert approval. Every rule conclusion it
            produces — the review sheet, the preview, anything derived — is unapproved and
            must not be relied on.
          </span>
        </div>
      ) : null}

      {approvalStale ? (
        <div className="banner unapproved" role="alert">
          <strong>Approval invalidated</strong>
          <span>
            This ruleset was approved and has been edited since. The approval names a hash
            of the revision it was made over, and that revision is gone. The engine refuses
            to evaluate it at all until it is re-approved.
          </span>
        </div>
      ) : null}

      <nav className="tabs" role="tablist">
        {TABS.map((item) => (
          <button
            key={item.id}
            role="tab"
            aria-selected={tab === item.id}
            onClick={() => setTab(item.id)}
          >
            {item.label}
            {item.id === "inspect" && flaggedNodeIds.size > 0 ? (
              <span className="badge stop">{flaggedNodeIds.size}</span>
            ) : null}
            {item.id === "sheet" && sheet && !sheet.coverage.is_healthy ? (
              <span className="badge stop">!</span>
            ) : null}
          </button>
        ))}
      </nav>

      {error ? (
        <div className="banner note" role="status">
          <strong>Refused</strong>
          <span>{error}</span>
        </div>
      ) : null}

      <div className="body">
        {tab === "inspect" ? (
          <div className="columns">
            <div className="col">
              <p className="col-head">
                <span>Tree — declaration order</span>
              </p>
              {checked ? (
                <NodeTree
                  nodes={checked.nodes}
                  selected={selected}
                  onSelect={setSelected}
                  flaggedNodeIds={flaggedNodeIds}
                />
              ) : (
                <Empty>Loading…</Empty>
              )}
            </div>

            <div className="col">
              <p className="col-head">
                <span>
                  Editor — <span className="mono">{selected}</span>
                </span>
              </p>
              {selectedNode && candidate ? (
                <NodeEditor
                  node={selectedNode}
                  nodes={candidate.nodes}
                  onChange={editNode}
                  disabled={busy}
                />
              ) : (
                <Empty>Pick a node.</Empty>
              )}
            </div>

            <div className="col">
              <ProvenanceRail
                node={selectedNode}
                provenance={checked?.provenance ?? []}
                synthetic={Boolean(corpus?.synthetic)}
              />
            </div>
          </div>
        ) : null}

        {tab === "preview" ? (
          <PreviewPanel
            nodes={checked?.nodes ?? []}
            result={preview}
            busy={busy}
            fields={factFields}
            onRun={runPreview}
          />
        ) : null}

        {tab === "sheet" ? <ReviewSheet sheet={sheet} onReload={loadSheet} /> : null}

        {tab === "approve" ? (
          <div style={{ padding: 16, maxWidth: 900 }}>
            <ApprovalPanel
              block={signature}
              busy={busy}
              refusal={signRefusal}
              result={signResult}
              onSubmit={sign}
            />
          </div>
        ) : null}

        {tab === "queue" ? (
          <>
            <div style={{ padding: "12px 16px 0" }}>
              <QueueNotWired />
            </div>
            <AuthorityQueue
              authority={status}
              corpus={authorities.map((a) => ({
                id: a.id,
                title: `${a.title} (${a.neutral_citation})`,
                key: a.key,
              }))}
              busy={busy}
              onSelect={async (id) => {
                setBusy(true);
                const body = await api.authorityStatus(id);
                setBusy(false);
                if (!isRefusal(body)) setStatus(body);
              }}
            />
          </>
        ) : null}
      </div>

      {/*
        The save bar. It is not on the inspect tab alone: a change made on any tab
        belongs to the same candidate, and a save control that is only visible on
        the tab where the change was made is a control people lose.
      */}
      {candidate ? (
        <SaveBar
          dirty={dirty}
          busy={busy}
          checked={checked}
          refusal={saveRefusal}
          approvalStale={approvalStale}
          onSave={save}
          onDiscard={() => setReloadToken((n) => n + 1)}
        />
      ) : null}
    </div>
  );
}

/** The payload the model wants, assembled from what the service returned. */
function toCandidate(body: RulesetPayload): Candidate {
  return { ...body.ruleset, nodes: body.nodes } as unknown as Candidate;
}

function SaveBar({
  dirty,
  busy,
  checked,
  refusal,
  approvalStale,
  onSave,
  onDiscard,
}: {
  dirty: boolean;
  busy: boolean;
  checked: RulesetPayload | null;
  refusal: Refusal | null;
  approvalStale: boolean;
  onSave: (editor: string, note: string) => void;
  onDiscard: () => void;
}) {
  const [editor, setEditor] = useState("");
  const [note, setNote] = useState("");

  const issues = checked?.issues ?? [];
  const provenance = checked?.unresolved_provenance ?? [];
  const anchors = checked?.anchor_problems ?? [];

  return (
    <footer
      style={{
        borderTop: "1px solid var(--line)",
        background: "var(--bg-raised)",
        padding: 10,
        maxHeight: "38vh",
        overflow: "auto",
        flex: "0 0 auto",
      }}
    >
      <div className="row" style={{ marginBottom: 8 }}>
        <input
          type="text"
          value={editor}
          onChange={(event) => setEditor(event.target.value)}
          placeholder="your name — the journal records who changed this rulebook"
          style={{ maxWidth: 340 }}
          aria-label="editor"
        />
        <input
          type="text"
          value={note}
          onChange={(event) => setNote(event.target.value)}
          placeholder="why (recorded with the edit)"
          style={{ flex: 1, minWidth: 180 }}
          aria-label="note"
        />
        <button
          type="button"
          className="act primary"
          disabled={busy || !dirty || !editor.trim() || issues.length > 0}
          onClick={() => onSave(editor, note)}
        >
          {busy ? "Saving…" : "Save revision"}
        </button>
        <button type="button" className="act" disabled={!dirty} onClick={onDiscard}>
          Discard
        </button>
      </div>

      <div className="row">
        <Badge tone={issues.length === 0 ? "ok" : "stop"}>
          {issues.length === 0
            ? "structurally valid"
            : `${issues.length} structural — save blocked`}
        </Badge>
        <Badge tone={provenance.length === 0 ? "ok" : "advisory"}>
          {provenance.length === 0
            ? "every pointer resolves"
            : `${provenance.length} unresolved`}
        </Badge>
        <Badge tone={anchors.length === 0 ? "ok" : "stop"}>
          {anchors.length === 0 ? "every quotation verified" : `${anchors.length} NOT anchored`}
        </Badge>
        {approvalStale ? (
          <Badge tone="stop">editing this will demote it to draft</Badge>
        ) : null}
        {checked?.diff_vs_signed ? (
          <span className="conf">{checked.diff_vs_signed.summary}</span>
        ) : null}
      </div>

      <div style={{ marginTop: 8 }}>
        <Problems heading="Structural defects — these block a save" items={issues} />
        <Problems
          heading="Quotations that are not in the paragraph they cite"
          items={anchors}
        />
        <Problems
          heading="Provenance that does not resolve — you may still save this"
          items={provenance}
        />
        {refusal ? (
          <Panel title="Refused" tone="defect">
            <p style={{ marginTop: 0 }}>{refusal.message}</p>
            <p className="conf">reason: {refusal.reason}</p>
            {refusalIssues(refusal).length > 0 ? (
              <ul className="problems">
                {refusalIssues(refusal).map((issue, index) => (
                  <li key={index}>
                    <span className="problem-code">{issue}</span>
                  </li>
                ))}
              </ul>
            ) : null}
          </Panel>
        ) : null}
      </div>
    </footer>
  );
}

/**
 * The `issues` list a refusal carries, if it carries one.
 *
 * `Refused.to_payload` flattens `detail` into the envelope, so a refusal about a
 * ruleset that does not validate has `issues` at the top level and one about a
 * digest does not. Checked rather than asserted, because an assertion here would
 * be a crash on the refusal path -- the path where the surface most needs to stay
 * on screen and explain itself.
 */
function refusalIssues(refusal: Refusal): string[] {
  const issues = (refusal as { issues?: unknown }).issues;
  return Array.isArray(issues) ? issues.filter((i): i is string => typeof i === "string") : [];
}
