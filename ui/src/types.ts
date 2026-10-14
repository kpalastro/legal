/**
 * The shapes the authoring service returns.
 *
 * These are transcribed from `ratio.authoring.service`, field for field and
 * name for name, including the ones nothing currently renders. That is
 * deliberate: the last defect this API had was a view that omitted a model field
 * (`approvals`), which meant the first save from a client silently deleted the
 * approval history. A type is not a schema and TypeScript cannot check the wire,
 * but a type that quietly disagrees with the service is the same mistake one
 * layer up -- so the rule here is that a field is added to this file only when it
 * exists in the payload, and never removed because nothing displays it.
 */

/** A refusal from the service. Arrives with HTTP 200; see `api.ts`. */
export interface Refusal {
  refused: true;
  reason: string;
  message: string;
  /** `detail` is flattened into the envelope by `Refused.to_payload`. */
  [detail: string]: unknown;
}

export function isRefusal(body: unknown): body is Refusal {
  return (
    typeof body === "object" &&
    body !== null &&
    (body as { refused?: unknown }).refused === true
  );
}

export type NodeKind = "all_of" | "any_of" | "leaf" | "exception" | "delegate_bn";

export type RefKind = "judgment_paragraph" | "statute" | "proposition";

export interface ProvenanceRef {
  kind: RefKind;
  judgment_id: string | null;
  paragraph: string | null;
  provision_key: string | null;
  proposition_id: string | null;
  quote: string | null;
  note: string | null;
}

export interface Predicate {
  field: string;
  op: string;
  value: string | number | boolean;
}

/** A node as a candidate payload: the model's own shape, what a save sends. */
export interface CandidateNode {
  id: string;
  kind: NodeKind;
  label: string;
  statement: string;
  children: string[];
  exception_of: string | null;
  predicate: Predicate | null;
  bn_query: string | null;
  bn_threshold: number | null;
  provenance: ProvenanceRef[];
}

export interface Approval {
  approver: string;
  role: string;
  approved_at: string;
  artefact_hash: string;
  note: string | null;
}

/**
 * The ruleset header as the service renders it, and as a client posts it back.
 *
 * Both at once, and that is the contract: the surface edits this object and
 * sends it, so anything present on the model needs to be present here. The
 * display-only fields (`is_approved`, `digest`, `current_hash`, `approval`,
 * `historical_approvals`) are extras the model ignores on the way back in.
 */
export interface RulesetView {
  id: string;
  title: string;
  question: string;
  domain: string;
  version: string;
  effective_from: string;
  effective_to: string | null;
  root: string;
  status: "draft" | "approved" | string;
  notes: string | null;
  provision_keys: string[];
  is_approved: boolean;
  /** The hash a signature on this revision must carry. */
  digest: string;
  /** The hash of the revision as it stands. Differs from `digest` unless approved. */
  current_hash: string;
  approval_count: number;
  approvals: Approval[];
  approval: Approval | null;
  historical_approvals: Omit<Approval, "note">[];
}

/** A node with the machine's reading of it attached. */
export interface NodeView {
  id: string;
  kind: NodeKind;
  label: string;
  statement: string;
  children: string[];
  exception_of: string | null;
  predicate: Predicate | null;
  bn_query: string | null;
  bn_threshold: number | null;
  provenance: ProvenanceRef[];
}

export type RefStatus =
  | "resolved"
  | "quote_verified"
  | "quote_not_in_paragraph"
  | "paragraph_missing"
  | "ambiguous_anchor"
  | "judgment_missing"
  | "provision_missing"
  | "names_no_source";

/** One reference, resolved against the corpus. */
export interface ProvenanceEntry {
  node: string;
  index: number;
  ref: ProvenanceRef;
  status: RefStatus;
  citation: string | null;
  anchor: string | null;
  heading: string | null;
  /** The paragraph or provision text, so the expert sees the words. */
  text: string | null;
  compiled_as_at: string | null;
  detail: string | null;
  authority_status?: string | null;
  authority_caveat?: string | null;
  synthetic?: boolean;
}

export interface JournalEntry {
  at: string;
  action: "save" | "approve";
  ruleset: string;
  editor?: string;
  approver?: string;
  role?: string;
  note?: string | null;
  before_digest?: string | null;
  after_digest?: string | null;
  hash?: string;
  invalidated_approval?: boolean;
  previous_status?: string | null;
  payload?: string;
}

export interface DiffLine {
  path: string;
  before: string | null;
  after: string | null;
}

export interface Diff {
  summary: string;
  changed: DiffLine[];
  added: DiffLine[];
  removed: DiffLine[];
}

/** Everything computable about a revision, saved or not. */
export interface RulesetPayload {
  ruleset: RulesetView;
  nodes: NodeView[];
  issues: string[];
  unresolved_provenance: string[];
  anchor_problems: string[];
  provenance: ProvenanceEntry[];
  diff_vs_signed: Diff | null;
  journal: JournalEntry[];
  source_path: string | null;
  origin_path: string | null;
  unsaved: boolean;
  /** Present on a save response. */
  wrote?: string;
  invalidated_approval?: boolean;
  warnings?: string[];
  /** Present on a sign response. */
  signed?: string;
}

export interface SignatureBlock {
  ruleset: string;
  version: string;
  current_status: string;
  /** The digest the signature will carry: the payload with status forced approved. */
  digest: string;
  issues: string[];
  signable: boolean;
  diff_vs_signed: Diff | null;
  /** The ready-to-paste approval block, as `ratio rules approve` prints it. */
  block: string;
}

export interface Finding {
  node: string;
  label: string;
  outcome: string;
  statement: string;
  observed: string | null;
  note: string | null;
  provenance: {
    kind: string;
    judgment: string | null;
    paragraph: string | null;
    provision_key: string | null;
  }[];
}

/** A conclusion, its derivation, and its gaps. Never a verdict. */
export interface PreviewResult {
  ruleset: string;
  version: string;
  status: string;
  is_approved: boolean;
  facts: Record<string, unknown>;
  conclusion: string;
  gaps: string[];
  defeaters_fired: string[];
  findings: Finding[];
}

export interface CoverageReport {
  total_cases: number;
  conclusion_counts: Record<string, number>;
  inert_leaves: string[];
  never_determined: string[];
  unexercised_defeaters: string[];
  unknown_fields: string[];
  is_healthy: boolean;
}

export interface ReviewSheet {
  ruleset: string;
  version: string;
  status: string;
  is_approved: boolean;
  coverage: CoverageReport;
  fields: string[];
  rows: {
    case_id: string;
    facts: Record<string, unknown>;
    conclusion: string;
    defeaters_fired: string[];
    gaps: string[];
  }[];
  shown: number;
  total: number;
}

export interface AuthorityParagraph {
  number: string;
  judge: string | null;
  /** `[7] (Halloran J)` -- scoped, never a bare `[7]`. */
  anchor: string;
  role: string;
}

export interface CorpusAuthority {
  key: string;
  id: string;
  title: string;
  neutral_citation: string;
  court: string;
  date_decided: string;
  synthetic: boolean;
  paragraphs: AuthorityParagraph[];
  /** Anchors this judgment cannot resolve alone. Never offer these as safe. */
  ambiguous_anchors: string[];
}

export interface ProvisionView {
  key: string;
  act_title: string;
  act_year: number;
  section: string;
  heading: string | null;
  jurisdiction: string;
  compiled_as_at: string;
  text: string;
}

/** A treatment edge: the machine's reading, with its evidence. */
export interface TreatmentEdge {
  source: string;
  target: string;
  treatment: string;
  confidence: number;
  via: string | null;
  quote: string | null;
  paragraph: string | null;
  needs_review: boolean;
}

export interface AuthorityStatus {
  key: string;
  title: string;
  status: string;
  /** Shown with the status or not at all. */
  caveat: string | null;
  /** Counted. This is why the status is what it is. */
  counted: TreatmentEdge[];
  /** Below threshold or bound via a name. Confirming makes it count. */
  advisory: TreatmentEdge[];
  /** A court that cannot bind. Confirming will NOT change the status. */
  hierarchy_blocked: TreatmentEdge[];
}

export interface CorpusSummary {
  judgments: number;
  provisions: number;
  paragraphs_indexed: number;
  synthetic: boolean;
  has_real_judgments: boolean;
  warnings: string[];
  store: {
    directory: string;
    origin: string | null;
    in_place: boolean;
    journal: string;
    rulesets: string[];
  };
}

export interface RulesetSummary {
  id: string;
  title: string;
  question: string;
  domain: string;
  version: string;
  status: string;
  is_approved: boolean;
  node_count: number;
  issue_count: number;
  edit_count: number;
}

/** What a save or a signature sends. */
export interface Candidate extends Record<string, unknown> {
  id: string;
  nodes: CandidateNode[];
  status: string;
}
