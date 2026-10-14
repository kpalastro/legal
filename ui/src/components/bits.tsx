/** Small shared pieces. Nothing here makes a decision about the law. */

import type { ReactNode } from "react";

import type { Tone } from "../status";

export function Badge({
  tone = "plain",
  children,
  title,
}: {
  tone?: Tone;
  children: ReactNode;
  title?: string;
}) {
  // `title` is offered for a machine-readable id, never for a caveat: docs/06 is
  // explicit that a caveat in a tooltip is a caveat nobody reads.
  return (
    <span className={`badge ${tone}`} title={title}>
      {children}
    </span>
  );
}

export function Mark({ tone, children }: { tone: Tone; children: ReactNode }) {
  return (
    <span className={`mark ${tone}`} aria-hidden="true">
      {children}
    </span>
  );
}

/**
 * A quotation, as evidence.
 *
 * Rendered with `white-space: pre-wrap` so the source's own line breaks survive.
 * Reflowing a quotation is a small edit to a document being relied on, and the
 * whole point of showing the paragraph is that the expert can compare it with
 * what the node claims.
 */
export function Quote({
  children,
  missing = false,
}: {
  children: ReactNode;
  missing?: boolean;
}) {
  return <blockquote className={`quote${missing ? " missing" : ""}`}>{children}</blockquote>;
}

export function Panel({
  title,
  tone,
  children,
}: {
  title: string;
  tone?: "defect" | "warn";
  children: ReactNode;
}) {
  return (
    <section className={`panel${tone ? ` ${tone}` : ""}`}>
      <h3>{title}</h3>
      {children}
    </section>
  );
}

export function Problems({ heading, items }: { heading: string; items: string[] }) {
  if (items.length === 0) return null;
  return (
    <Panel title={heading} tone={heading.toLowerCase().includes("defect") ? "defect" : "warn"}>
      <ul className="problems">
        {items.map((item, index) => (
          <li key={index}>
            <span className="problem-code">{item}</span>
          </li>
        ))}
      </ul>
    </Panel>
  );
}

export function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: ReactNode;
}) {
  return (
    <label className="field">
      <span className="label">{label}</span>
      {children}
      {hint ? <p className="hint">{hint}</p> : null}
    </label>
  );
}

export function Text({
  value,
  onChange,
  mono,
  placeholder,
}: {
  value: string;
  onChange: (next: string) => void;
  mono?: boolean;
  placeholder?: string;
}) {
  return (
    <input
      type="text"
      className={mono ? "mono" : undefined}
      value={value}
      placeholder={placeholder}
      onChange={(event) => onChange(event.target.value)}
    />
  );
}

export function Area({
  value,
  onChange,
  mono,
  rows,
  placeholder,
}: {
  value: string;
  onChange: (next: string) => void;
  mono?: boolean;
  rows?: number;
  placeholder?: string;
}) {
  return (
    <textarea
      className={mono ? "mono" : undefined}
      value={value}
      rows={rows}
      placeholder={placeholder}
      onChange={(event) => onChange(event.target.value)}
    />
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <p className="empty">{children}</p>;
}
