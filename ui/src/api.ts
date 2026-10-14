/**
 * The HTTP client, and the one decision it makes.
 *
 * **A refusal is not an error.** The service answers a declined request with
 * `200` and `{"refused": true, "reason": ...}`. So this module never converts a
 * refusal into a thrown exception, and never retries one. The caller receives the
 * payload and renders it. A refusal is the system working -- it looked at the
 * request and correctly declined -- and a client that treats it as a fault will
 * either retry it or swallow it, and both are wrong.
 *
 * A genuinely failed request (the service is not running, the network is down)
 * does throw, because that is a different thing and the surface should say so
 * rather than render an empty screen.
 */

import { isRefusal, type Refusal } from "./types";

export class ServiceUnavailable extends Error {}

async function request<T>(path: string, init?: RequestInit): Promise<T | Refusal> {
  let response: Response;
  try {
    response = await fetch(path, {
      ...init,
      headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
    });
  } catch (cause) {
    throw new ServiceUnavailable(
      `the authoring service did not answer at ${path}. Is it running? ` +
        `Start it with \`ratio serve\`.`,
      { cause },
    );
  }

  let body: unknown;
  try {
    body = await response.json();
  } catch (cause) {
    throw new ServiceUnavailable(
      `${path} returned ${response.status} and no JSON body`,
      { cause },
    );
  }

  if (isRefusal(body)) return body;
  if (!response.ok) {
    throw new ServiceUnavailable(`${path} returned ${response.status}`);
  }
  return body as T;
}

export const api = {
  health: () => request<{ ok: boolean; store: Record<string, unknown> }>("/api/health"),

  corpus: () => request<import("./types").CorpusSummary>("/api/corpus"),

  authorities: () =>
    request<{ authorities: import("./types").CorpusAuthority[] }>("/api/authorities"),

  authorityStatus: (key: string) =>
    request<import("./types").AuthorityStatus>(
      `/api/authorities/${encodeURIComponent(key)}/status`,
    ),

  provisions: () =>
    request<{ provisions: import("./types").ProvisionView[] }>("/api/provisions"),

  rulesets: () =>
    request<{ rulesets: import("./types").RulesetSummary[] }>("/api/rulesets"),

  ruleset: (id: string) =>
    request<import("./types").RulesetPayload>(`/api/rulesets/${encodeURIComponent(id)}`),

  /**
   * Everything about an unsaved revision, computed without writing anything.
   * The editor calls this on every keystroke, debounced.
   */
  candidate: (id: string, ruleset: unknown) =>
    request<import("./types").RulesetPayload>(
      `/api/rulesets/${encodeURIComponent(id)}/candidate`,
      { method: "POST", body: JSON.stringify({ ruleset }) },
    ),

  preview: (id: string, ruleset: unknown, facts: Record<string, unknown>) =>
    request<import("./types").PreviewResult>(
      `/api/rulesets/${encodeURIComponent(id)}/preview`,
      { method: "POST", body: JSON.stringify({ ruleset, facts }) },
    ),

  signature: (
    id: string,
    ruleset: unknown,
    who: { approver: string; role: string; note?: string },
  ) =>
    request<import("./types").SignatureBlock>(
      `/api/rulesets/${encodeURIComponent(id)}/signature`,
      {
        method: "POST",
        body: JSON.stringify({ ruleset, ...who }),
      },
    ),

  reviewSheet: (id: string, limit = 40) =>
    request<import("./types").ReviewSheet>(
      `/api/rulesets/${encodeURIComponent(id)}/review-sheet?limit=${limit}`,
    ),

  save: (id: string, ruleset: unknown, editor: string, note?: string) =>
    request<import("./types").RulesetPayload>(
      `/api/rulesets/${encodeURIComponent(id)}`,
      { method: "PUT", body: JSON.stringify({ ruleset, editor, note }) },
    ),

  sign: (
    id: string,
    ruleset: unknown,
    body: {
      approver: string;
      role: string;
      note?: string;
      confirm_digest: string;
    },
  ) =>
    request<import("./types").RulesetPayload>(
      `/api/rulesets/${encodeURIComponent(id)}/sign`,
      { method: "POST", body: JSON.stringify({ ruleset, ...body }) },
    ),
};
