# 03 — Retrieval and verification

Two questions, and they are not the same question:

- **Retrieval** asks *which paragraphs bear on this?* Its failure is returning
  something plausible and wrong.
- **Verification** asks *does this claimed authority actually say this?* Its
  failure is passing something that does not, which is the defect that gets
  practitioners sanctioned.

Everything upstream of verification is machinery for producing claims. It is the
only place that decides whether a claim is allowed out, and it is deliberately
small, deterministic and fail-closed.

## Why legal retrieval is not general retrieval

The published evidence is unusually clear, and it runs against the default
instinct to reach for embeddings first:

- On **CLERC** (1.84M US case-law passages), **BM25 outperformed zero-shot dense
  retrieval.** A fine-tuned domain encoder beat both — but only a *fine-tuned*
  one, which is a project, not a dependency.
- **Legal queries are term-of-art queries.** "Special share", "prescribed
  corporation", "substantial lessening of competition" have no useful
  distributional neighbourhood; a general-purpose encoder blurs them into the
  surrounding vocabulary, which is exactly the vocabulary that does not matter.
- **Citations and section numbers are exact-match objects.** No embedding
  represents the difference between s 52 and s 52A.
- **Document-level retrieval mismatch** — retrieving a passage from the wrong
  document — is severe in long-document corpora, and summarisation-augmented
  chunking roughly halves it.

So the architecture is: lexical backbone, dense as an opt-in signal, reciprocal
rank fusion to combine them, and a document-level collapse applied *after*
fusion. The collapse runs late deliberately — applied early it would distort
both rankers; applied on the fused list it only removes near-duplicate passages
from the same judgment once the ranking has been decided.

The offline default is lexical-only. Everything else in this repository — the
rules, the networks, the evaluator — runs with no model and no network, and
retrieval should not be the one component that secretly needs one.

## Query understanding

> "Is Bramblewood [2019] FCAFC 77 still good law as at 2021?"

That query contains three separable instructions:

- a **target** — the judgment cited by neutral citation, which must be resolved
  by exact match, never by similarity;
- an **operation** — "still good law", which selects a different code path
  entirely: the citation graph, not the index;
- a **temporal anchor** — "as at 2021", which makes the answer a point-in-time
  query and is meaningless to a retriever.

`retrieve/query.py` does that decomposition with patterns, not with a model. It
is deliberately a *parser* rather than a classifier: a lawyer can read the
regexes and tell you where they are wrong, which is not true of a fine-tuned
intent head. It also keeps the whole pipeline runnable with no network. The
decomposition is exposed on the object rather than consumed invisibly, so a
caller can see what was understood and override it.

## The abstention gate, and a control that could not fire

A retrieval system that always returns its top hit is not a safe system, because
the question *"does the corpus contain anything bearing on this?"* has a
different answer from *"which paragraph is closest?"* — and only the first
answer licenses an assertion.

The first version of this gate was a threshold on the retriever's own fused
score. It could not work, and the reason is structural rather than a matter of
tuning. **Reciprocal rank fusion discards scores and keeps only rank order.**
The top hit of any single ranker scores 1/(k+1); with k=60 that is 0.0164, for
every query, whatever the corpus contained. The confabulation case and a case
with a perfect answer scored *identically*. The threshold sat below that constant
and therefore could essentially never fire.

That is worse than having no gate, because the gate was reported as a safety
control. What is now measured is **IDF-weighted query coverage**: the share of
the query's IDF mass that the matched paragraph accounts for.

```
coverage(q) = Σ_{t ∈ terms(q)} w(t) · [t ∈ matched]  /  Σ_{t ∈ terms(q)} w(t)
```

Three properties of that definition are load-bearing:

**Weighting is by IDF, not by count.** The confabulation case — a question about
exemption certificates, asked of a corpus that does not discuss them — matched a
judgment about structured interests. It matched on "action" and "notifiable",
which are precisely the two words in the query that carry no information. An
unweighted term-overlap measure scores that match highly; IDF-weighted coverage
scores it low, because it asks *how much of what this question is made of does
the corpus recognise at all*.

**A term the corpus has never seen weighs `max_idf`, not zero.** Its IDF is
undefined — it has no postings — and the obvious default of zero inverts the
measurement: the less the corpus had to say about a question, the *better*
covered the question would look. This is the single most likely way for the gate
to be quietly broken by a later "simplification", so it is documented in
`BM25Index.max_idf` and asserted in `tests/test_harness.py`.

**Coverage is necessary and never sufficient.** It measures whether the corpus
recognises the question, not whether the top paragraph answers it. Passing the
gate licenses looking; it does not license asserting.

### Calibration, honestly

Measured over `eval/tasks/04_retrieval.yaml`: the lowest coverage among cases
with a correct answer is **0.607**; the highest among cases that should be
refused is **0.206**. The threshold is 0.5, and the test asserts a margin greater
than 0.15 between the families — not a pass rate.

Six cases is not a calibration and the suite says so in its own header. What the
suite *can* do is show how much room there is before adding a case changes the
decision. If a new case closes that gap, the suite fails loudly and someone
re-derives the threshold rather than discovering the problem in production.

## Verification: four checks, no partial pass

The verifier exists to prevent a specific failure, and it is not "cannot find
authority". It is finding the wrong authority, or inventing one, and stating it
with the same fluency as everything else.

*JNE24 v Minister for Immigration and Citizenship* [2025] FedCFamC2G 1314 is the
design brief. A practitioner personally paid costs after citing four non-existent
authorities, and the court's point was that checking a case is not fictitious is
**not enough** — the practitioner must confirm it supports the proposition. An
existence check is necessary and nowhere near sufficient. So the verifier checks
four things:

1. **Existence.** The cited authority is in the corpus. A citation to something
   we have not ingested is reported as such, never as verified. The distinction
   between "we hold this and it is overruled" and "we do not hold this" matters:
   `verdict` returns `unverifiable` for the latter, and `unverifiable` is not
   `supported`.
2. **Anchor.** The paragraph exists, and the judge is known where the judgment
   has more than one set of reasons. A quote from a dissent attributed to the
   court is a defect that an existence check cannot see.
3. **Quote.** The claimed words occur in the claimed paragraph, and the report
   says whether the character offsets were right, right only after
   normalisation, or wrong. That third distinction is what separates "you quoted
   it" from "you quoted it and your offset points somewhere else", which matters
   when the citation is machine-generated.
4. **Treatment.** The authority has not been overruled, and the caller is told if
   it has been questioned or distinguished. Verification is where the citation
   graph and the retrieval path meet: a perfectly grounded quote from an
   overruled case is still not safe to rely on.

`VerificationReport.is_clean` is the gate, and it is False if any citation failed
to resolve, any quote failed to ground, or any authority relied on is overruled.
**There is no partial pass and no confidence threshold.** A claim is either
supported by authority we hold and can point at, or it is not. An uncited claim
is not a clean claim — nothing in it is false, and nothing in it has been
verified, and those are different things.

Anything that fails is refused, not annotated. The orchestrator is written to
drop unsupported claims rather than soften them, because the alternative — 
rendering a claim with a caveat — is the failure mode the whole module exists to
prevent.

## The seam between the two

Verification is bound to the **same corpus snapshot** as retrieval, and this is
the most important structural decision in this document.

Building the verifier as a standalone service over a live citation database would
be simpler, and it creates a failure that is common, quiet, and self-reinforcing:
retrieval serves a paragraph from document A, the verifier checks it against a
database where A has since been superseded, the quote fails to ground, and the
engineer fixes it by relaxing the verifier. Six months later the verifier grounds
quotes against text the retriever has never seen, and grounding is a rubber
stamp.

Binding them removes that escape. If retrieval and verification disagree, the
corpus snapshot is internally inconsistent, and that is an ingest defect reported
as one.

## Known gaps

- **Dense retrieval is an interface, not a working ranker.** No embedding model
  is bundled, and adding one carries a benchmark obligation against the lexical
  baseline before it is enabled by default.
- **Statutory provisions are not indexed by the retriever** (see
  `docs/02-judgment-knowledge-base.md`).
- **Coverage is calibrated on six cases**, which is a margin, not a measurement.
- **The gate cannot detect preferring a lower court's summary of an authority
  over the authority itself.** That is a relevance judgement about legal weight,
  and coverage is blind to it. It is listed among the suite's limitations rather
  than left implicit.
