# The expert authoring surface

## Three jobs, and they are not one job

The failure mode of a professional tool is almost always the same: it flattens
several distinct jobs into a single box. Here that box would be a search field,
and it would be wrong for all three of the people who need to use this system.

**(a) The lawyer asking a question.** They want the paragraph that governs, the
status of the authority it came from, and to be told when the corpus does not
answer the question. They should never see a rule node, a CPT or a coverage
report. Their surface is the narrowest of the three and the one where a wrong
default costs the most.

**(b) The subject-matter expert authoring and signing off an encoded rule.**
They are doing something unusual: translating a legal test they hold in their
head into a structure a machine can evaluate, while remaining the person
accountable for it. Their surface has to show provenance and structure
simultaneously, because the defect they are hunting is a rule that is
*structurally* valid and *legally* wrong — the exception encoded as a conjunct,
the "and" that should have been an "or".

**(c) The reviewer working the treatment and citation queues.** They are not
authoring anything. They are looking at machine readings of judgment prose — a
treatment edge, a bound phrase, a name resolved to an authority — and deciding
whether each one is right. Their surface is a worklist, and its quality depends
on how much of the machine's reasoning travels with each item.

A single search box serving all three would be a search box that shows job (b)
and (c) material to job (a). The three surfaces share a corpus and a design
language and nothing else.

## The authoring surface: provenance at the moment of editing

The central requirement is one sentence: **the expert must be able to see the
provenance of every node in the tree they are editing, at the moment they edit
it.**

The reason is specific. `AuthorityRef` exists so that every leaf and every
exception in `rulesets/firb_notifiable_action.ruleset.yaml` points at either a
paragraph of a judgment or a provision of an Act. The structure of that file is
the interesting part:

```yaml
  - id: minority_control_indicia
    kind: all_of
    children:
      - board_nomination_rights
      - capacity_to_determine_policies
    provenance:
      - kind: judgment_paragraph
        judgment_id: au/fcafc/2019/77
        paragraph: 7
        quote: The power to nominate one of six directors is a minority power...
```

The comment above that block in the ruleset says it directly: the Full Court held
that a minority holding plus a minority board nomination right is *not*, without
more, a substantial interest, so the minority limb requires **both** board
representation **and** a capacity to determine policy. A ruleset written from the
statutory text alone would very likely have used `any_of` — and would have looked
entirely reasonable doing it. The provenance pointer is what lets an expert
checking the node see the words that force the `all_of`.

If the provenance is one click away in a side panel, the check happens. If it is
one click away in a *different view*, it does not. That is the design argument,
and it is why the surface is a tree-with-provenance rather than a table of nodes
plus a citation browser.

The corollary is that the surface must show the tree the expert wrote, in the
order they wrote it, not an alphabetised or normalised rendering. The
`CaseGenerator` already respects this — its leaf ordering is "by declaration in
the ruleset, not by id, so that the column order of the review sheet matches the
order the expert wrote the nodes in" — and the editor must do the same.

## Approval is an act over a hash, not a checkbox

The governance control is `Approval`:

```python
class Approval(BaseModel):
    approver: str
    role: str          # "Partner, M&A", "Special Counsel, Competition"
    approved_at: datetime
    artefact_hash: str
```

and the hash is taken over `RuleSet.canonical_payload()` — a deterministic
JSON serialisation with sorted keys, excluding the `approvals` list itself.
`RuleSet.current_approval` returns the approval whose `artefact_hash` matches
the *current* revision, or `None`. `RuleSet.is_approved` is true only when the
status is `approved` **and** such an approval exists.

The consequence is the property that matters: edit any node, any predicate
value, any provenance pointer or the version string after sign-off, and the hash
stops matching. The engine will not quietly inherit the approval. Worse, from the
editor's point of view — and better, from the firm's — `validate_ruleset`
reports "ruleset is marked approved but no approval matches its current revision
hash: it has been edited since sign-off", and `RuleEngine.__init__` raises
`RuleEvaluationError` on any validation issue, so the edited ruleset cannot be
evaluated at all until it is re-approved.

The design decision that follows for the UI is that **the interface must not
compute or write the approval.** `ratio rules approve` prints the digest and a
ready-to-paste block, and then says:

```
and set `status: approved`.
```

It does not do it. The comment in `cmd_approve` gives the reason: "The approval
is the expert's act, and a tool that performs it for them turns a governance
control into a build step." The UI must preserve that. A "Sign off" button that
writes the approval record is the single most tempting and most damaging thing
this surface could contain, because it converts a considered act into a click
with the same visual weight as saving a draft.

What the interface should do instead: show the expert the digest, let them copy
it, and — if it writes anything at all — write it as an explicit, separate,
confirmed action over a reviewed diff, with the hash displayed before and after.
A surface that shows the expert a diff of what changed since the last approval is
doing the work that makes the sign-off meaningful.

## The review sheet: evidence that the rule was exercised

`ratio cases` runs the decision-table generator over the ruleset and produces the
artefact an expert annotates. Its output is the coverage report followed by the
sheet:

```
N generated cases over ruleset firb.notifiable_action v0.3.0
conclusions: applies=…, does_not_apply=…, undetermined=…
INERT LEAVES (value never changed the outcome -- redundant or mis-wired): …
UNEXERCISED DEFEATERS (never fired in any generated case): …
```

Three things about this artefact are deliberate.

**An inert leaf is a defect.** A leaf whose value never changes the conclusion
across every generated fact combination is either redundant or mis-wired, and
either way it is a node the expert believes is doing work and is not. This is
what `CoverageReport.is_healthy` gates on, and `ratio cases` exits non-zero
because of it.

**An unexercised defeater is reported twice over.** The generator can fail to
exercise an exception because of a limitation in the generator rather than in
the ruleset. The comment in `generate_cases.py` is explicit that reporting it
only as a ruleset defect "hands the reviewer a defect they cannot act on" while
the real question — *does an exemption certificate actually defeat notifiability
in this encoding?* — goes unasked. So an unexercised exception is reported as a
generator defect as well, which is the honest attribution.

**The last column of the sheet is empty.** The docstring says it plainly: the
artefact is "a document for a lawyer to disagree with, not a report of what the
system concluded". A review sheet that pre-fills the expected answer is a
document that collects confirmation, not review.

This sheet is the answer to the question an expert will reasonably ask before
signing anything: *how do I know the rule says what I meant?* Walking the
decision table and marking each row agree/disagree is a finite, checkable task;
reading the YAML is not.

## The citation-binding review queue

The second queue is where machine readings of judgment prose are confirmed or
rejected. It exists because two gates in `ratio/kb/graph.py` deliberately *do
not act* on uncertain evidence, and evidence that is recorded and set aside has
to go somewhere a human will see it.

`AuthorityStatusRecord` carries three buckets, and the distinction between them
is the whole design:

| Bucket | Meaning | What a reviewer can do |
|---|---|---|
| `evidence` | Counted. This is why the status is what it is | Read the sentence that produced it |
| `advisory_evidence` | Below the confidence threshold, or bound to a name rather than a citation. Recorded, not counted | **Confirm it and it starts counting** |
| `hierarchy_blocked` | A diminishing treatment from a court that cannot bind the target | **Nothing.** Confirming it will not change the status — the gate is the hierarchy, not the reader's confidence in the binding |

That last row is the subtle one, and the CLI already renders it correctly. The
two buckets print differently, and the visual language is worth quoting because
it is the language the UI should adopt rather than reinvent:

```
       - followed by FCAFC:2020:1 at [12] (confirmed, conf 0.82)
         "I would follow the approach of the primary judge..."

       ? doubted by FCA:2021:300 at [4] (ADVISORY, not counted, conf 0.35, via name_reference)
         "the reasoning in that decision is difficult to reconcile with..."

       ! not_followed by FedCFamC2G:2022:11 at [9] (BELOW THIS COURT, cannot bind, conf 0.71)
         "...I do not propose to follow it."
```

The conventions to carry across:

* `-` counted, `?` advisory and not counted, `!` hierarchy-blocked and incapable
  of counting.
* The `via` field is always shown on an advisory edge. A treatment attached to a
  neutral citation is stronger evidence than one attached to a case *name*
  resolved through the corpus-derived alias table, and a lawyer reviewing the
  edge is entitled to know which they are looking at. This is also why
  `TreatmentEdge.needs_review` fires on `via == "name_reference"` regardless of
  confidence.
* The quote travels with the edge, always, and never summarised. The quote is
  not decoration; it is the evidence. `_edge_quote` appends the reason a binding
  is doubtful in brackets when there is one — the phrase governing a noun phrase
  from an earlier sentence, or appearing in a party's submission rather than a
  holding — so the reviewer sees *why* the machine was unsure rather than
  inferring it from a confidence float.
* The status itself is rendered with the icon set from `status_report_lines`:
  `OK` good law, `LIM` distinguished on facts, `WARN` questioned, `STOP`
  overruled or superseded, `????` uncertain. `????` is not a placeholder for an
  error; it is the honest rendering of "no treatment recorded, which does not
  mean good law".

Every status also carries a caveat, and the caveat must be shown with the status
or not at all:

```
Treatment was recorded for this authority, but none of it is capable of moving
its status -- it came from a court that cannot bind this one, or below the
confidence threshold. Read the notes below; they are evidence about how the
authority is received, even though they are not evidence that it has been
overruled.
```

That wording is the whole point of `recorded_any` in `_classify_status`. Without
it the system would say "no treatment recorded" while holding the treatment, and
"no evidence either way" is a lie told in the system's most reassuring register.

## What every answer shown to a user must carry

Binding, whatever surface renders it:

1. **The paragraph anchor**, scoped by judge where the judgment has more than one
   author — `[7] (Halloran J)`. Not `[7]` alone: in a three-judge Full Court, "at
   [42]" names three different paragraphs.
2. **The authority status and its caveat** for the judgment the paragraph came
   from. The MCP `search` tool returns `authority_caveat` on every result for
   exactly this reason — a client must not be able to render a paragraph without
   the warning about the judgment it came from.
3. **The ruleset version and approval state**, on anything derived from a rule.
   The MCP `apply_rules` result carries `"approved": false` today, and the test
   suite asserts it.
4. **An abstention rendered as an abstention, with the missing fact named.** The
   retrieval gate's refusal names the coverage figure and the nearest paragraph
   the corpus does hold; a rule that cannot conclude returns `undetermined` with
   a `gaps` list naming the *field a user can establish* (`action.is_exempt_action`,
   not the node that needed it). A bare "I cannot answer" is not acceptable,
   because it leaves the user with nothing to do.

The fourth item is the one that gets designed away by accident. An interface that
renders `undetermined` as a greyed-out "No" has converted a correctly abstaining
system into a confident one.

**Today, the shipped ruleset is `draft`.** `rulesets/firb_notifiable_action.ruleset.yaml`
is version `0.3.0` with `status: draft`, and `ratio rules list` prints:

```
NOT APPROVED for this revision. Any output derived from it is unapproved and
must not be relied on.
```

The UI must show "not approved" wherever it shows a rule conclusion, and must not
soften it into an amber dot. Every rule conclusion this system can currently
produce is unapproved, and the interface's job is to make that impossible to
miss — which is also what makes the approval flow meaningful once it is used.

## Accessibility, and the liability posture

Accessibility here is mostly a rendering discipline, since the content is text:
the anchor, the quote and the caveat must survive at every zoom level and in a
screen reader, which argues against the common pattern of putting the caveat in a
tooltip. A caveat in a tooltip is a caveat nobody reads.

The posture is simpler than the design: **the tool advises, the lawyer decides.**
Three consequences the interface has to respect:

* No verdict anywhere. The credence layer returns a `PosteriorReport` — a
  distribution, the sensitivities driving it, the authorities behind each factor,
  and a mandatory caveat — and there is no boolean in it. The rule engine returns
  a conclusion *with its proof tree and its defeats*. Neither is a yes/no, and the
  UI must not add one.
* The proof tree is not an advanced view. `ProofTree.render()` exists so the
  derivation can be shown; if the expert-facing surface hides it behind a
  disclosure triangle that nobody opens, the system has become unexplainable in
  the ordinary use case, which is the failure the CLI was written to avoid.
* An unapproved artefact says so on every screen that shows its output.

## What is not built

`ui/` is an **empty directory**. There is no React project, no TypeScript, no
components. Everything above is a design for a surface that does not exist.

What does exist, and is what a UI would sit on: the CLI prints all of this in
plain text today. `ratio authority Bramblewood` resolves the case name, prints
the resolution so the reader can see which case the name was taken to mean, then
prints the status, the caveat and the three buckets. `ratio cases` writes the
review sheet. `ratio rules approve` prints the digest block. The MCP service
layer returns the same data as JSON.

The genuine gaps, in the order they would need closing:

* **Nothing writes `confirmed_by`.** The field is honoured throughout the graph
  and the verifier; no code path sets it. The review queue therefore cannot
  actually be worked, only read. This is checkpoint G2 in
  `docs/11-governance-assurance.md`, and it is the most consequential
  unimplemented piece of the governance story.
* **No approval has been written.** Both shipped artefacts — the ruleset and the
  credence network — are `status: draft`. The approval mechanism is tested
  (`tests/test_rules.py` pins the edited-after-approval behaviour) and unused.
* **No CI gate.** `ratio eval` exits non-zero on a guardrail failure and
  `ratio cases` exits non-zero on an unhealthy coverage report, so the hooks
  exist, but nothing in the repository invokes them on a change.
* **No accessibility work at all**, because there is no interface to make
  accessible.
