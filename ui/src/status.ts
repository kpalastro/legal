/**
 * The status vocabulary, and where each part of it comes from.
 *
 * All of it is already in the system, in two places, and neither is reinvented
 * here.
 *
 * The authority statuses and their icons are `status_report_lines` in
 * `ratio/kb/graph.py`. The three treatment buckets and their `-`, `?`, `!`
 * prefixes are the table in `docs/06-expert-authoring-surface.md`, which is
 * itself a description of what the CLI prints.
 *
 * The reason to keep them in one file rather than inline at each use is that the
 * distinction between the buckets *is* the design. `advisory` and
 * `hierarchy_blocked` look similar -- both are recorded and neither counts -- and
 * they are opposites in the only way that matters to a reviewer: confirming one
 * changes the status, and confirming the other cannot. Anywhere that renders
 * them, it must render which.
 */

import type { RefStatus } from "./types";

export type Tone = "ok" | "advisory" | "blocked" | "stop" | "unknown" | "plain";

/** `status_report_lines`: OK / LIM / WARN / STOP / ????. */
const AUTHORITY_STATUS: Record<string, { icon: string; tone: Tone; say: string }> = {
  good_law: {
    icon: "OK",
    tone: "ok",
    say: "No treatment recorded that diminishes this authority.",
  },
  distinguished: {
    icon: "LIM",
    tone: "advisory",
    say: "Distinguished on its facts. It still stands; it does not reach this case.",
  },
  questioned: {
    icon: "WARN",
    tone: "advisory",
    say: "Judicial or extra-judicial doubt has been recorded.",
  },
  overruled: {
    icon: "STOP",
    tone: "stop",
    say: "Overruled by a court that can bind it. Do not rely on it.",
  },
  superseded: {
    icon: "STOP",
    tone: "stop",
    say: "Superseded by statute or a later authority.",
  },
  uncertain: {
    icon: "????",
    tone: "unknown",
    say:
      "No treatment recorded -- which does not mean good law. It means the corpus " +
      "holds no evidence either way.",
  },
};

export function authorityStatus(status: string): {
  icon: string;
  tone: Tone;
  say: string;
} {
  return (
    AUTHORITY_STATUS[status] ?? {
      icon: "????",
      tone: "unknown",
      say: `Unrecognised status ${JSON.stringify(status)}. Treat it as unresolved.`,
    }
  );
}

/**
 * The three buckets, with the prefix the CLI prints.
 *
 * `canConfirm` is the point of the file. A reviewer looking at an advisory edge
 * can do something that changes the status; a reviewer looking at a
 * hierarchy-blocked edge cannot, and telling them otherwise would be the surface
 * offering a control that does not control anything.
 */
export type Bucket = "counted" | "advisory" | "blocked";

export const BUCKETS: Record<
  Bucket,
  { mark: string; tone: Tone; title: string; say: string; canConfirm: boolean }
> = {
  counted: {
    mark: "-",
    tone: "ok",
    title: "counted",
    say: "This is why the status is what it is.",
    canConfirm: false,
  },
  advisory: {
    mark: "?",
    tone: "advisory",
    title: "advisory — recorded, not counted",
    say: "Below the confidence threshold, or bound to a name rather than a citation.",
    canConfirm: true,
  },
  blocked: {
    mark: "!",
    tone: "blocked",
    title: "below this court — cannot bind",
    say:
      "Confirming this will not change the status. The gate is the hierarchy of " +
      "courts, not your confidence in the binding.",
    canConfirm: false,
  },
};

/**
 * Why an unresolved reference did not resolve.
 *
 * Each of these is a different thing an expert has to go and do, so each gets
 * its own sentence rather than a shared "problem".
 */
const REF_STATUS: Record<RefStatus, { tone: Tone; short: string; say: string }> = {
  resolved: {
    tone: "ok",
    short: "resolved",
    say: "The pointer resolves to a provision the corpus holds.",
  },
  quote_verified: {
    tone: "ok",
    short: "quotation verified",
    say: "The words quoted are in the paragraph, verbatim.",
  },
  quote_not_in_paragraph: {
    tone: "stop",
    short: "NOT IN THE PARAGRAPH",
    say:
      "The citation resolves and the words quoted are not in it. This is not a near " +
      "miss -- a quotation that is not verbatim is a paraphrase wearing a citation.",
  },
  paragraph_missing: {
    tone: "stop",
    short: "no such paragraph",
    say: "The judgment is in the corpus; the paragraph number is not in the judgment.",
  },
  ambiguous_anchor: {
    tone: "stop",
    short: "AMBIGUOUS ANCHOR",
    say:
      "The reasons are numbered separately by each judge, so this number names as " +
      "many paragraphs as there are authors. It cannot be resolved without saying " +
      "whose -- and guessing is how a system quotes a dissent as the majority.",
  },
  judgment_missing: {
    tone: "stop",
    short: "judgment not in corpus",
    say: "No judgment with this key is loaded.",
  },
  provision_missing: {
    tone: "stop",
    short: "provision not in corpus",
    say: "No provision with this key is loaded.",
  },
  names_no_source: {
    tone: "stop",
    short: "names no source",
    say: "The reference points at nothing at all.",
  },
};

export function refStatus(status: RefStatus) {
  return (
    REF_STATUS[status] ?? {
      tone: "unknown" as Tone,
      short: status,
      say: "An unrecognised status from the service.",
    }
  );
}

/** True for every status that means the pointer did not hold. */
export function isRefDefect(status: RefStatus): boolean {
  return !(status === "resolved" || status === "quote_verified");
}

/**
 * A conclusion is never a verdict, and `undetermined` is never a "no".
 *
 * Docs/06: "An interface that renders `undetermined` as a greyed-out 'No' has
 * converted a correctly abstaining system into a confident one."
 */
export function conclusionTone(conclusion: string): Tone {
  switch (conclusion) {
    case "applies":
      return "ok";
    case "does_not_apply":
      return "advisory";
    case "undetermined":
      return "unknown";
    default:
      return "unknown";
  }
}

export function conclusionWord(conclusion: string): string {
  switch (conclusion) {
    case "applies":
      return "the rule applies";
    case "does_not_apply":
      return "the rule does not apply";
    case "undetermined":
      return "undetermined — the encoding cannot conclude on these facts";
    default:
      return conclusion;
  }
}
