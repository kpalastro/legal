# 10 — Deployment and security

This document describes how the system is intended to be deployed, what the
security posture is and why, and — in the final section — exactly how much of it
exists. `infra/` is currently an empty directory. Nothing in this document has
been applied to any account.

---

## What is actually sensitive here

The two halves of this system have opposite risk profiles, and conflating them
produces the wrong controls on both.

**The corpus plane is low-sensitivity and high-integrity.** It holds published
judgments and statute. Nobody's privacy is engaged by a judgment of the Full
Court. But a corrupted corpus produces **confident wrong answers**: if a
paragraph is truncated, if a treatment edge is lost, if a judgment from the
wrong court is filed under the wrong key, the system will not degrade — it will
answer, in the register of authority, over material that has been silently
altered. The controls this implies are integrity controls: content digests,
reproducible ingestion, and a load path that refuses rather than repairs.

**The matter plane is high-sensitivity and moderate-integrity.** The facts a
lawyer types into `apply_rules` are a client's privileged material. So is the
fact pattern behind a `search_authorities` query — the query itself can disclose
the matter. The controls this implies are confidentiality controls: encryption,
access control, and a hard boundary on egress.

**The audit ledger is a record of legal work.** It is not sensitive in the way
matter data is, but it is discoverable, it is cumulative, and it is the thing
that lets the firm reconstruct what it advised and on what basis. Doc 09
specifies the ledger; this document specifies where it lives and why.

One consequence worth stating early: **the query log is matter data.** A
deployment that treats tool-call logs as operational telemetry and ships them to
a general observability vendor has exported privileged information to a third
party. The tracing and ledger stores belong inside the same trust boundary as the
matter data, not in a shared telemetry account.

## The deployment skeleton

The shape below is the intended target. It is a design; see the last section.

### Compute

ECS on Fargate runs the MCP service. Fargate rather than EC2 because the service
is stateless by design (doc 07) — there is no local state to preserve, no
instance to patch, and no reason for a long-lived host. Tasks run with a
read-only root filesystem, a non-root user, and no writable volumes.

The service is stateless, which makes horizontal scaling trivial and removes an
entire class of problem: there is no session affinity to configure, no cache to
invalidate, and no "which instance holds this matter" question to answer, because
no instance holds anything.

### Storage

- **Knowledge base**: managed PostgreSQL with pgvector, in a private subnet, not
  publicly addressable. The KB is derived data — it can be rebuilt from the corpus
  — so its backup posture is about recovery time, not about preservation.
- **Corpus**: S3, versioned, with the content digest recorded per object and
  verified on ingestion. The corpus is the authoritative artefact; the KB is a
  function of it.
- **Audit ledger**: S3 with **Object Lock in compliance mode** over a retention
  window. This is the control that makes "append-only" true rather than
  aspirational: it is enforced by the storage service against every principal
  including the account root, rather than by the application declining to issue
  a delete. A hash chain (doc 09) detects tampering; Object Lock prevents it.
  Both are needed — the chain proves a record was not altered, and the lock makes
  alteration impossible in the first place.

### Keys

KMS customer-managed keys, separate per plane. Separate keys for the corpus, the
matter database, and the ledger, because a single key means a single blast radius
and a single rotation event. Key policies grant decrypt to the task role and
nothing else; no human principal holds standing decrypt rights on the matter key,
because a human with standing decrypt rights is a human whose compromise is
unbounded.

### Network

The service runs in a private subnet with **no NAT gateway and no egress**.

This is the control most worth arguing for, because it is the one usually left
out. The deterministic core — KB, retrieval, rules, BN, verification, evaluation —
has no legitimate reason to reach the internet. Not "should not"; has no reason.
So egress can be **denied** rather than monitored, and denial is a control that
cannot be defeated by a novel exfiltration technique, whereas monitoring is a
control that can.

The practical effect is that a compromised dependency or a prompt-injected
component in this service has nowhere to send what it takes.

The model-backed agent layer is the exception, and it must be a **separate
service with a separate egress path**, separately logged and separately audited.
Folding it into the core service would mean opening egress for the deterministic
core, which is exactly the trade this design refuses. Doc 07 already argues that
the model is the only component that can fabricate; this makes it also the only
component that can exfiltrate, which is the correct thing for it to be.

## Identity

Short-lived credentials via OIDC to an IAM role. No long-lived access keys in the
repository, in environment variables, or in a task definition. A task assumes a
role at start and the credentials expire with the task.

The identity requirement that matters most is the one the audit ledger depends
on. **The principal id recorded in the ledger must be an identity the deployment
actually authenticated, not a field the caller fills in.** A ledger entry that
says `principal: "jsmith"` because the request body said so is not an audit
record; it is a claim. The ledger's value in an incident — see below — rests
entirely on the principal being something the platform can vouch for. If the
service cannot derive the principal from a verified token, it must record that it
could not, rather than record what it was told.

Authorisation is per-matter. A caller who can read matter A must not be able to
read matter B by changing an identifier in a request, and the service must derive
the authorisation decision from the authenticated identity rather than from the
request body — the same failure as the principal, one step further in.

## Data handling

Encryption in transit (TLS to every endpoint, including the database and the
model provider) and at rest (KMS, as above). That is table stakes and is stated
for completeness rather than as a claim of maturity.

The **corpus digest is not a secret.** It should be published with every answer:
"this answer was produced from corpus revision `abc123` and ruleset
`firb.notifiable_action` v0.3.0". That is what makes an answer reproducible, and
what lets a reviewer establish six months later whether the system would still
say the same thing. A digest of public judgments discloses nothing.

**The matter data is.** Retention is per-matter and driven by the firm's
obligations, not by an operations team's preference for a shorter window.
Deletion must be possible — a client can require it — and the ledger is the
awkward case, because a ledger that can be edited on request is not a ledger.
The honest position is that this is an unresolved tension to be settled with the
firm's risk function rather than an engineering decision, and it is recorded as
an open question in doc 12.

## The model-provider path

When the agent layer runs, matter text and retrieved paragraphs leave the VPC.
Three constraints:

1. **The corpus must not be sent wholesale as context.** A provider that receives
   the entire corpus as a prompt has received a copy of the corpus and, in
   practice, has received it repeatedly. Retrieval exists to send the three
   paragraphs that bear on the question, and sending more is both a cost and a
   disclosure.
2. **No privileged matter text may be used to train or improve a third-party
   model.** This must be a contractual term with the provider, verified rather
   than assumed, and it must cover retention for abuse monitoring as well as
   training — a provider that retains prompts for 30 days has retained the
   client's matter for 30 days.
3. **AustLII content must not be used for AI training or embedding purposes.**
   AustLII's terms prohibit it. The same discipline the licence imposes on the
   corpus applies to matter data going to a provider: a legal knowledge base whose
   ingestion ignores the terms of its sources is not a compliant system with a
   licensing footnote, it is a system built on material it was not entitled to
   use.

The deterministic core runs with none of this. That is a deliberate property, and
it is why the default analyst (doc 07) is model-free: a deployment that never
enables the `llm` extra has no model-provider path at all, no egress, and no
disclosure surface beyond its own VPC. That should be the default for
deployments that can live with it.

## Supply chain

Standard practice — pinned dependencies, a committed lockfile, a generated SBOM,
automated vulnerability scanning in CI — with one addition specific to this
system. **A legal knowledge base's dependency risk includes its data sources,
not just its packages.** A compromised transitive dependency is a known shape
with known mitigations. A judgment corpus acquired from a source whose terms
prohibit the use it is being put to is a risk that no scanner will flag, that no
lockfile will pin, and that surfaces as a licensing dispute or a takedown request
after the system is in production and its outputs are in client files.

The mitigation is process, not tooling: every corpus source is recorded with its
licence and the permitted uses, and the ingestion path refuses a source with no
licence record. That field does not exist in the codebase today.

## Incident response

An incident in this system has a different shape from an outage, and the runbook
has to reflect it.

**An incorrect legal conclusion that reached a client is not resolved by fixing
the code.** It may need to be corrected with the client. It may need to be
corrected with a court. It may have propagated into a filed document, an advice,
a board paper, or a transaction that has already closed. The response therefore
has a step that an outage does not: **identifying every other matter where the
same fault produced the same answer**, so that the correction reaches all of
them.

That step is what the audit ledger is for. Given a fault, the questions are: from
which corpus revision and which ruleset revision did this output come; which
other requests ran against those revisions; which of them touched the affected
topic; and what did they return. A ledger that records the answer but not the
corpus digest and the ruleset revision cannot answer any of those, and the
investigation becomes a manual review of every matter the firm has run through
the system.

This is the reason doc 09 puts the corpus digest, the ruleset approval hash and
the abstention reason in every ledger record. They look like operational
metadata. They are the incident-response capability.

A related case: the ledger must record **abstentions** as abstentions. A ledger
that records only answers cannot show that the system declined to answer a
question, and "the system refused this and the lawyer advised anyway" is a very
different fact pattern from "the system answered this". Both are legitimate; only
one of them is currently recorded.

## What is not built

`infra/` is an empty directory. There is no Terraform, no CloudFormation, no
Helm chart, no container definition.

Specifically, none of the following exists anywhere in this repository:

- any IAM role, policy, trust relationship or OIDC provider configuration;
- any KMS key or key policy;
- any VPC, subnet, security group, route table or VPC endpoint configuration;
- any S3 bucket, lifecycle rule or Object Lock configuration;
- any database, schema, parameter group or backup policy;
- any ECS cluster, task definition or service;
- any CI pipeline, image build, or deployment script;
- any SIEM, log destination, alert rule or on-call integration for the audit
  ledger — so an implementation would have to build the detection for ledger
  writes as well as the ledger.

There is also no secret-management story, no key-rotation runbook, no
vulnerability-disclosure process, no incident-response runbook of the shape
described above, and no DPIA or security assessment. This document is the design
that a security review would assess; it is not evidence that any of it has been
assessed, built, or run.

The one thing that can be said with confidence is that the *application* is
deployable without change: `RatioService` is stateless and takes its corpus path
as a startup argument, the deterministic core has no network dependency, and the
SDK import for the transport is inside a function so the image does not need the
`mcp` extra to run the core. The gap is everything around it.
