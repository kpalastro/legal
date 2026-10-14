# 02 — The judgment knowledge base

## Why paragraphs are the unit of citation

A judgment is cited by paragraph number. `Bramblewood at [42]` is a complete
reference; a passage in a token window is not, because the window is an artefact
of a text processing pipeline that no judge has ever seen and no court can look
up. Everything downstream of that observation follows:

- A paragraph runs from its `[n]` marker to the character before the next one.
  `ingest/structure.py` splits on markers and on nothing else.
- Character offsets into the judgment text are preserved through parsing, so
  anything that relies on a reference can point back at the exact bytes it
  relied on, not at a re-derived string that may have been normalised.
- A judgment carries not just paragraphs but *whose* reasons each paragraph
  holds. Most judgments that matter procedurally have more than one set of
  reasons — a plurality, a concurrence, a dissent — and a quote from a dissent
  cited as the court's holding is a defect that a paragraph number alone will not
  catch. `ParagraphRef` carries the judge, and the verifier checks that the judge
  is known where the judgment has more than one.

The structuring is defensive because real judgment text is dirty by the time it
reaches a machine. Markers repeat in footnotes, appear inside block quotations,
appear in catchwords, and appear at the start of a line as part of a list. A
structurer that quietly mangles them produces a corpus that looks fine and cites
wrong. So `structure.py` reports what it did — counts of markers found, markers
rejected and why — rather than returning a list and a shrug.

## Sources, and the licensing question that precedes the engineering

`ingest/fixtures.py` is the seam a real deployment replaces. The rest of the
system only ever sees `ratio.model.Judgment` and `ratio.model.StatutoryProvision`
objects, so swapping the synthetic corpus for licensed real sources is one more
loader and no changes to the KB, retrieval, rules or verification layers. That
seam is real and deliberate: `SourceKind` includes `AUSTLII`, `FEDERAL_COURT`,
`HIGH_COURT`, `JADE`, `MANUAL` and `SYNTHETIC`, and only `SYNTHETIC` has a
working loader.

**The obvious free source cannot lawfully be used for this purpose.** AustLII's
terms of use prohibit the use of its content for AI training and embedding
purposes. This project therefore does not scrape AustLII, does not use its
content to build or fine-tune an embedder, and does not ingest it into the
corpus. The `SourceKind.AUSTLII` enum member exists because the source is real
and a deployment might have a separate licence to it; the presence of the
constant is not a licence, and no code path here uses it. Corpus acquisition for
anything beyond the fixtures is a licensing question first — AustLII, Jade, the
Federal Court's own published judgments and the High Court's are each governed
differently — and only then an engineering question about formats and
incremental updates.

The same discipline runs the other way. `data/fixtures/judgments.yaml` is
invented material: realistic citation *formats*, realistic statutory references,
invented cases, parties and reasoning, every document carrying `synthetic: true`.
The loader **refuses to build a corpus that mixes synthetic and real authorities
unless explicitly overridden**, because the failure mode — a fixture silently
retrieved alongside real law — produces fluent, confident, wrong output rather
than an error. The current corpus reports `synthetic: true` and
`has_real_judgments: false` through the MCP corpus resource, and a test asserts it.

## The citation graph

A corpus of judgments is not a knowledge base until it can answer two questions:

1. **What does this authority rest on, and what rests on it?**
2. **Is it still good law?**

The second is where naive systems fail most expensively. Retrieval returns the
case that supports the argument, and nobody notices that a Full Court
disapproved of it two years ago. So authority status is computed from treatment
edges and *always* carries the evidence that produced it, so a lawyer can read
the sentence that did the damage.

The treatment vocabulary is a closed set drawn from Australian practice:
`overruled`, `not_followed`, `departed_from`, `doubted`, `distinguished`,
`considered`, `referred_to`, `approved`, `applied`, `followed`. `is_negative`
partitions them and drives the status computation. The fixture corpus carries
eight treatment edges across those classes; the edges themselves are derived
from prose by `citations.py`, which proposes and does not decide.

### Names, not just citations

Judgments do not cite by neutral citation every time they refer to a case. They
say "Bramblewood was correctly decided and we would follow it", and the citation
appeared two paragraphs earlier. That is how legal prose is written: the
citation establishes identity once, and the name carries it thereafter. A
citator that follows only citation tokens therefore misses exactly the sentences
in which courts say what they think of earlier decisions — which is most of
them.

The alias table is therefore **mined from the corpus rather than guessed from
the case title**, and the reason is visible immediately in the fixture: every
judgment here is "X v Minister for Foreign Investment". A rule taking the first
token of the first party would work; a rule taking "Minister" would map six
different cases onto one authority. So names are mined from titles restricted to
distinctive party names, and a candidate is only bound to an authority if the
corpus itself puts that name next to that authority's citation — "the Full Court
in Bramblewood [2019] FCAFC 77". Where the corpus never makes the connection, the
name is left unresolved and reported. That second step is what makes the table
safe: it is evidenced by the corpus rather than asserted by a heuristic, and a
name that maps to two authorities is reported as ambiguous rather than resolved
by taking whichever came first.

### Resolving a name a lawyer types

`KnowledgeBase.resolve_authority` takes a case name or a key and returns
`(key, alias_used)`. It exists because legal work is conducted in short case
names — no practitioner says "FCAFC:2019:77" — so a system that accepts only
neutral citations is unusable in the way that matters, and one that accepts
names must say *which case* it took a name to mean. The CLI reports the
resolution (`'Bramblewood' resolved to FCAFC:2019:77`) before it reports the
status, because taking a short name to mean a particular case is an inference
and the reader has to be able to see where it landed. An exact key always wins
over an alias, so a corpus containing a case actually named "Yarrow" cannot be
shadowed by an alias.

## Authority status, and the two gates

`AuthorityStatus` is computed, never authored: `good_law`,
`distinguished_on_facts`, `questioned`, `overruled`, `superseded_by_statute`,
`uncertain`. Three rules govern the computation, and each exists because the
obvious alternative is wrong.

**Hierarchy is asymmetric.** A treatment edge counts for only as much as the
citing court's position supports. A single judge of the Federal Court cannot
overrule the High Court; the graph encodes that, and a first-instance
disagreement downgrades an authority to *questioned* rather than *overruled*.
The asymmetry is the point: **only diminishing treatment from a subordinate
court is blocked.** Positive treatment from a subordinate court is counted,
because a lower court following a decision cannot downgrade anything, and
suppressing it would throw away real evidence that the authority is being
applied.

**Reading is symmetric.** A separate gate withholds treatment whose
classification confidence is below `ADVISORY_CONFIDENCE = 0.5`. It is symmetric
in both directions — a low-confidence *positive* edge is suppressed as readily
as a low-confidence negative one — because it asks about the *reading* of the
sentence, not about the court. If we are not confident we understood what the
citing judgment said, we are not confident in either direction.

**Nothing is discarded silently.** Suppressed treatment lands in one of three
buckets on `AuthorityStatusRecord`: `evidence` (counted),
`advisory_evidence` (suppressed on confidence), `hierarchy_blocked` (suppressed
on hierarchy). All three are reported, with the quote and the citing judgment.
The CLI's visual language encodes it — `?` for advisory, `!` for
hierarchy-blocked — and a test asserts that suppressed treatment never reports as
*absence*: a record with a hierarchy-blocked overruling does not say "no
treatment recorded", because that would be a different and false statement.

The result of both gates being open on an authority with no treatment at all is
`uncertain`, not `good_law`. Absence of evidence of overruling is not evidence of
good law, and a system that conflates them is the reason practitioners do not
trust citators.

## Point in time

Every query carries an `as_at` date, and the answer echoes it. A status computed
as at a date that is not in the answer is a status nobody can check. Treatment
edges have effective dates, so a 2021 status excludes a 2022 disapproval, and a
test asserts exactly that.

Statutory provisions are modelled separately, and their point-in-time problem is
harder than case law's: a provision can be amended, and the version in force at
the relevant date is the one that governs. `StatutoryProvision` carries
`effective_from`/`effective_to`, and `AuthorityStatus.SUPERSEDED_BY_STATUTE` is
the edge from the case graph into the statutory graph.

## Known gap

**Retrieval does not index statutory provisions at all.** `_build_index`
iterates judgments only, so a provision is resolvable by key and readable as a
resource, but is not retrievable by query. The corpus holds seven provisions and
they are not reachable by search. This is recorded in the header of
`eval/tasks/04_retrieval.yaml` and asserted as a documented limitation, rather
than left for someone to discover from a disappointing result.
