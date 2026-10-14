"""``ratio`` -- the command line.

One entry point over every layer, so that the system is inspectable without a
notebook and operable without a service. That is not a convenience: a legal
knowledge system has to be explainable to the people accountable for it, and the
cheapest way to make something unexplainable is to make it only reachable through
a UI that renders a conclusion and hides the derivation. Every command here
prints its working.

Subcommands, and the question each answers:

  ingest      what is in the corpus, what failed to parse, what did not resolve
  authority   is this judgment still good law, and on what evidence
  ask         retrieve the paragraphs that bear on a question, with their status
  cases       generate the review sheet that proves the ruleset says what the
              expert meant
  eval        score the system against the task suites, abstentions included
  rules       list, validate and approve the encoded rulebook
  network     query the credence network for a posterior, never a verdict
  stats       one-line health of the knowledge base

Exit codes are meaningful, because this runs in CI: 0 clean, 1 the run completed
and found something wrong (a failed eval suite, a validation error, a coverage
defect), 2 the command could not run at all.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from ratio.ingest.fixtures import CorpusBundle, load_corpus
from ratio.kb.graph import status_report_lines
from ratio.kb.index import KnowledgeBase
from ratio.model import Court, RuleSet, Treatment
from ratio.retrieve.hybrid import HybridRetriever, RetrievalFilters
from ratio.retrieve.query import LegalQuery, QueryParser

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_FIXTURES = ROOT / "data" / "fixtures"
DEFAULT_RULESETS = ROOT / "rulesets"
DEFAULT_TASKS = ROOT / "eval" / "tasks"

EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_UNUSABLE = 2


# --------------------------------------------------------------------------
# Shared plumbing
# --------------------------------------------------------------------------


def _fail(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return EXIT_UNUSABLE


def _load(args: argparse.Namespace) -> tuple[CorpusBundle, KnowledgeBase]:
    bundle = load_corpus(
        Path(args.fixtures),
        rulesets_dir=Path(args.rulesets),
        allow_mixed=bool(getattr(args, "allow_mixed", False)),
        strict=False,
    )
    if bundle.problems:
        # Printed, not raised. A corpus that loads with complaints is still worth
        # looking at, and the complaints are the most useful thing in the output.
        for problem in bundle.problems:
            print(f"corpus problem: {problem}", file=sys.stderr)
    return bundle, KnowledgeBase.build(bundle)


def _ruleset(bundle: CorpusBundle, wanted: str | None) -> RuleSet | None:
    if not bundle.rulesets:
        return None
    if wanted is None:
        return bundle.rulesets[0]
    for ruleset in bundle.rulesets:
        if wanted in {ruleset.id, ruleset.domain, ruleset.id.split(".")[-1]}:
            return ruleset
    return None


# --------------------------------------------------------------------------
# ingest
# --------------------------------------------------------------------------


def cmd_ingest(args: argparse.Namespace) -> int:
    # An optional positional path, accepted because it is what people type.
    # Corpus loading is the one command whose subject is the filesystem, so
    # `ratio ingest data/fixtures` reads as the obvious thing to write even
    # though the path is otherwise a global flag. Rejecting it teaches the flag;
    # accepting it does not cost anything, because the two forms cannot disagree
    # — if both are given, that is a contradiction worth reporting rather than
    # silently resolving in favour of one.
    path = getattr(args, "path", None)
    if path:
        if args.fixtures != str(DEFAULT_FIXTURES):
            print(
                f"both a path argument ({path!r}) and --fixtures "
                f"({args.fixtures!r}) were given; use one or the other",
                file=sys.stderr,
            )
            return EXIT_UNUSABLE
        args.fixtures = path

    bundle, kb = _load(args)
    print(bundle.summary())
    print()
    print("knowledge base")
    for key, value in kb.stats().items():
        print(f"  {key:<18} {value}")

    if kb.warnings:
        print()
        print("parse warnings")
        for warning in kb.warnings:
            print(f"  - {warning}")

    if kb.alias_report is not None and args.aliases:
        print()
        print("case-name aliases")
        for line in kb.alias_report.lines():
            print(f"  {line}")

    unresolved = kb.graph.unresolved_citations()
    if unresolved:
        print()
        print(
            f"cited but not ingested ({len(unresolved)}) -- the graph is missing "
            "these, so any negative treatment they supply is missing too:"
        )
        for key in unresolved[:20]:
            print(f"  - {key}")
    return EXIT_OK


# --------------------------------------------------------------------------
# authority
# --------------------------------------------------------------------------


def cmd_authority(args: argparse.Namespace) -> int:
    _, kb = _load(args)

    as_at = date.fromisoformat(args.as_at) if args.as_at else None

    keys = []
    if args.key:
        # A lawyer types "Bramblewood", not "FCAFC:2019:77". Resolve the name and
        # report the resolution: taking a short name to mean a particular case is
        # an inference, and the reader has to be able to see which case it landed
        # on before they rely on the answer.
        key, alias = kb.resolve_authority(args.key)
        if key is None:
            print(f"???? {args.key}  is not a case in the corpus, and is not a "
                  "case name the corpus evidences")
            return EXIT_FINDINGS
        if alias:
            print(f"     {alias!r} resolved to {key}")
        keys = [key]
    else:
        keys = sorted(
            k for k in (j.canonical_key() for j in kb.judgments.values()) if k
        )

    problems = 0
    for key in keys:
        judgment = kb.judgment_for_key(key)
        if judgment is None:
            problems += 1
            print(f"???? {key}  not in the corpus")
            print()
            continue
        record = kb.graph.authority_status(
            key, as_at=as_at, confirmed_only=args.confirmed_only
        )
        for line in status_report_lines(record, judgment.title):
            print(line)

        if args.history:
            print("     what it relies on:")
            for target, edge in kb.graph.citations_made_by(key)[:10]:
                label = edge.treatment.value if edge else "(bare citation)"
                print(
                    f"       -> {target}  {label} at [{edge.citing_paragraph}]"
                    if edge
                    else f"       -> {target}  (bare citation)"
                )
        print()

    if as_at:
        print(f"(status computed as at {as_at.isoformat()})")
    return EXIT_FINDINGS if problems else EXIT_OK


# --------------------------------------------------------------------------
# ask
# --------------------------------------------------------------------------


def cmd_ask(args: argparse.Namespace) -> int:
    _, kb = _load(args)
    question = " ".join(args.question)

    parsed = QueryParser().parse(question)
    if args.json:
        print(json.dumps(_query_payload(parsed), indent=2))

    # The parsed intent sets the filters. A question that asks what binds the
    # forum should not be answered with first-instance decisions, and a question
    # asked as at a past date should not be answered with later cases.
    filters = _filters_for(parsed, args)
    retriever = HybridRetriever(kb)

    results = retriever.search(
        parsed.issue_text or question,
        filters=filters,
        limit=args.limit,
    )
    if not results:
        print("no paragraph in the corpus matches this question under the filters")
        print(f"  filters: {filters}")
        return EXIT_FINDINGS

    print(f"question: {question}")
    if parsed.intent.value != "unknown":
        print(f"read as:  {parsed.intent.value}")
    if parsed.is_point_in_time:
        print(f"as at:    {parsed.as_at}")
    print()

    for rank, result in enumerate(results, start=1):
        print(f"{rank:>2}. {result.ref.citation_label}")
        print(f"    {result.ref.heading or '(no heading)'}")
        if args.explain:
            print(f"    {result.explain()}")
        if result.authority_caveat:
            print(f"    ! {result.authority_caveat}")
        snippet = " ".join(result.ref.text.split())
        print(f"    {snippet[:args.width]}{'...' if len(snippet) > args.width else ''}")
        print()
    return EXIT_OK


def _filters_for(parsed: LegalQuery, args: argparse.Namespace) -> RetrievalFilters:
    courts = tuple(parsed.courts) or tuple(args.court or ())
    return RetrievalFilters(
        courts=courts,
        date_to=parsed.as_at or (date.fromisoformat(args.as_at) if args.as_at else None),
        judgments=tuple(args.judgment or ()),
        exclude_synthetic=args.exclude_synthetic,
        require_good_law=args.good_law,
    )


def _query_payload(parsed: LegalQuery) -> dict:
    return {
        "intent": parsed.intent.value,
        "issue_text": parsed.issue_text,
        "target_keys": list(parsed.target_keys),
        "raw_citations": list(parsed.raw_citations),
        "statute_refs": list(parsed.statute_refs),
        "courts": list(parsed.courts),
        "as_at": parsed.as_at.isoformat() if parsed.as_at else None,
        "is_point_in_time": parsed.is_point_in_time,
        "needs_graph": parsed.needs_graph,
    }


# --------------------------------------------------------------------------
# cases
# --------------------------------------------------------------------------


def cmd_cases(args: argparse.Namespace) -> int:
    from ratio.rules.generate_cases import generate_and_analyse

    bundle, _ = _load(args)
    ruleset = _ruleset(bundle, args.ruleset)
    if ruleset is None:
        return _fail(f"no ruleset matching {args.ruleset!r}")

    credence = _credence_resolver(bundle, ruleset)
    cases, coverage = generate_and_analyse(
        ruleset, credence=credence, max_cases=args.max_cases
    )

    print(coverage.summary())
    print()
    if args.out:
        sheet = _review_sheet(cases, coverage, ruleset, limit=args.limit)
        Path(args.out).write_text(sheet, encoding="utf-8")
        print(f"review sheet written to {args.out} ({len(cases)} cases)")
    else:
        print(_review_sheet(cases, coverage, ruleset, limit=args.limit))

    return EXIT_OK if coverage.is_healthy else EXIT_FINDINGS


def _review_sheet(cases, coverage, ruleset, *, limit: int) -> str:
    from ratio.rules.generate_cases import CaseGenerator

    # The generator's renderer is an instance method only because it takes the
    # limit as state in one path; the sheet itself depends on nothing else.
    generator = CaseGenerator.__new__(CaseGenerator)
    body = generator.render_review_sheet(cases, limit=limit)
    header = [
        f"# Rule review sheet -- {ruleset.title}",
        "",
        f"Ruleset `{ruleset.id}` v{ruleset.version}, status `{ruleset.status}`.",
        "",
        coverage.summary(),
        "",
        "Every row below is a case the system generated *from the encoding itself*, "
        "by walking its decision table. The empty column is the point: mark each row "
        "as agreeing or disagreeing with the conclusion, and the encoding has been "
        "checked against your reading of the law rather than against its own.",
        "",
    ]
    return "\n".join(header) + "\n" + body


def _credence_resolver(bundle: CorpusBundle, ruleset: RuleSet):
    """A resolver answering ``delegate_bn`` nodes from a credence network.

    Returns ``None`` when there is no network, which is the honest answer: the
    engine then reports the delegate as an unknown rather than inventing a number
    for it. A ``delegate_bn`` node with no network behind it is a modelling defect
    the coverage report should surface, not something to paper over with a
    default.
    """
    if not bundle.credence_engines:
        return None
    engine = bundle.credence_engines[0]

    def resolve(question: str, facts: dict) -> float:
        # The delegate is a statement of the credence question, not of a node id, so
        # it is resolved by matching the question text against the network's own
        # question. Falling back to the network's target keeps a single-target
        # network usable without restating its question at every delegate.
        target = engine.network.target
        for node in engine.network.nodes:
            if question.strip().lower() == node.question.strip().lower():
                target = node.id
                break
        try:
            distribution = engine.query({}, target=target)
        except Exception:  # noqa: BLE001
            # An unresolvable delegate must degrade to "unknown", never to a number.
            return float("nan")
        return float(distribution.get("yes", float("nan")))

    return resolve


# --------------------------------------------------------------------------
# eval
# --------------------------------------------------------------------------


def cmd_eval(args: argparse.Namespace) -> int:
    from ratio.eval.harness import compare, load_suites, run_suite
    from ratio.eval.systems import default_systems

    bundle, kb = _load(args)
    ruleset = _ruleset(bundle, args.ruleset)
    if ruleset is None:
        return _fail("no ruleset loaded; eval needs one to build the rule system")

    suites = load_suites(Path(args.tasks))
    systems = default_systems(kb, ruleset)

    selected = args.system or list(systems)
    unknown = [s for s in selected if s not in systems]
    if unknown:
        return _fail(
            f"unknown system(s) {', '.join(unknown)}; "
            f"available: {', '.join(systems)}"
        )

    failed = 0
    for suite in suites:
        results = {}
        for name in selected:
            result = run_suite(suite, systems[name])
            if not result.outcomes:
                # No cases of a kind this system answers. Printed as a single
                # line rather than a full report of zeros, which would read as a
                # catastrophic failure rather than as "not applicable".
                print(result.render())
                print()
                continue
            results[name] = result
            print(result.render())
            print()
            if not result.passes:
                failed += 1

        # Pairwise comparison, restricted to pairs that were measured on the same
        # cases. Two systems that answered different questions on this suite
        # cannot be compared here, and McNemar on disjoint case sets would produce
        # a p-value that means nothing while looking exactly like one that means
        # something. This is where comparing a retriever with a verifier stops
        # being a mistake a user can make.
        order = [n for n in selected if n in results]
        baseline_name = args.against or (order[0] if order else None)
        for name in order:
            if name == baseline_name:
                continue
            a, b = results[baseline_name], results[name]
            if {o.case_id for o in a.outcomes} != {o.case_id for o in b.outcomes}:
                continue
            print(compare(suite.id, a, b).render())
            print()

    print(
        f"{len(suites)} suite(s), {len(selected)} system(s); "
        f"{failed} suite/system combination(s) failed"
    )
    return EXIT_FINDINGS if failed else EXIT_OK


# --------------------------------------------------------------------------
# rules
# --------------------------------------------------------------------------


def cmd_rules(args: argparse.Namespace) -> int:
    bundle, kb = _load(args)
    action = args.action

    if action == "list":
        if not bundle.rulesets:
            print("no rulesets loaded")
            return EXIT_FINDINGS
        for ruleset in bundle.rulesets:
            flag = "approved" if ruleset.is_approved else ruleset.status
            print(
                f"{ruleset.id}  v{ruleset.version}  [{flag}]  "
                f"{len(ruleset.nodes)} nodes  {ruleset.title}"
            )
            if ruleset.is_approved:
                approval = ruleset.current_approval
                assert approval is not None
                print(
                    f"    approved by {approval.approver} ({approval.role}) "
                    f"at {approval.approved_at.isoformat()} "
                    f"hash {approval.artefact_hash[:12]}"
                )
            else:
                print(
                    "    NOT APPROVED for this revision. Any output derived from "
                    "it is unapproved and must not be relied on."
                )
        return EXIT_OK

    if action == "validate":
        from ratio.rules.engine import (
            anchor_problems,
            provenance_problems,
            validate_ruleset,
        )

        # Resolve provenance against the corpus as well as checking structure.
        # "Has provenance" and "has provenance that names something" are
        # different claims, and the first revision of the shipped ruleset passed
        # the first while failing the second on every single statutory node.
        #
        # And the third claim, which is the one that took longest to notice:
        # "names something" and "names something that says this" are different
        # too. A citation can resolve to a real paragraph and quote words that
        # are not in it, and that passes every check above. `anchor_problems`
        # asks the third question, and the shipped ruleset failed it -- a
        # sentence of the Full Court quoted with the source's comma replaced by
        # a full stop, which reads as a complete sentence and is not one.
        judgment_ids = set(kb.judgments)
        provision_keys = set(kb.provisions)

        problems = 0
        for ruleset in bundle.rulesets:
            issues = validate_ruleset(ruleset)
            issues += provenance_problems(
                ruleset,
                judgment_ids=judgment_ids,
                provision_keys=provision_keys,
            )
            issues += anchor_problems(ruleset, judgments=kb.judgments)
            if issues:
                problems += len(issues)
                print(f"{ruleset.id}: {len(issues)} issue(s)")
                for issue in issues:
                    print(f"  - {issue}")
            else:
                print(f"{ruleset.id}: ok")
        return EXIT_FINDINGS if problems else EXIT_OK

    if action == "approve":
        return _approve(args, bundle)

    return _fail(f"unknown rules action {action!r}")


def _approve(args: argparse.Namespace, bundle: CorpusBundle) -> int:
    ruleset = _ruleset(bundle, args.ruleset)
    if ruleset is None:
        return _fail(f"no ruleset matching {args.ruleset!r}")
    if not args.by or not args.role:
        return _fail("approval requires --by (name) and --role (capacity)")

    digest = ruleset.approval_digest
    print(f"ruleset:  {ruleset.id} v{ruleset.version}")
    print(f"digest:   {digest}")
    print()
    print("This digest covers the ruleset as it will stand once approved -- that is,")
    print("with `status: approved`. Edit any node, predicate value, provenance pointer,")
    print("version or status afterwards and the digest stops matching, at which point")
    print("the engine will refuse to run it as approved. Paste this block into the")
    print("ruleset's `approvals` list:")
    print()
    print("  approvals:")
    print(f"    - approver: {args.by!r}")
    print(f"      role: {args.role!r}")
    print(f"      approved_at: '{date.today().isoformat()}T00:00:00+00:00'")
    print(f"      artefact_hash: {digest}")
    print("      note: >-")
    print(f"        {args.note or 'Reviewed against the review sheet.'}")
    print()
    print("and set `status: approved`.")
    # Not written automatically. The approval is the expert's act, and a tool that
    # performs it for them turns a governance control into a build step.
    return EXIT_OK


# --------------------------------------------------------------------------
# network
# --------------------------------------------------------------------------


def cmd_network(args: argparse.Namespace) -> int:
    bundle, _ = _load(args)
    if not bundle.credence_engines:
        return _fail("no credence network loaded")

    engine = bundle.credence_engines[0]
    evidence = dict(kv.split("=", 1) for kv in args.evidence or [])

    try:
        report = engine.posterior_report(
            evidence, target=args.target, top_k=args.top_k
        )
    except Exception as exc:  # noqa: BLE001 - a bad evidence key is user error
        return _fail(str(exc))

    print(f"{report.network_id} v{report.network_version}")
    print(f"target: {report.target_label}  ({report.target})")
    if report.evidence:
        print("observed: " + ", ".join(f"{k}={v}" for k, v in report.evidence.items()))
    else:
        print("observed: (nothing -- this is the prior)")
    print()
    for state, probability in sorted(
        report.distribution.items(), key=lambda kv: -kv[1]
    ):
        bar = "#" * int(round(probability * 40))
        print(f"  {state:<20} {probability:6.4f}  {bar}")

    if report.sensitivities:
        print()
        print("what the answer is sensitive to (total-variation distance):")
        for variable, delta in report.sensitivities:
            print(f"  {variable:<32} {delta:+.4f}")

    if report.authorities:
        print()
        print("whose numbers these are:")
        for authority in report.authorities:
            print(f"  - {authority.note}")

    # The caveat is not optional decoration. It goes last so it is the line left on
    # screen, and it goes out unconditionally.
    print()
    print("caveat: " + report.caveat)
    return EXIT_OK


# --------------------------------------------------------------------------
# stats
# --------------------------------------------------------------------------


def cmd_stats(args: argparse.Namespace) -> int:
    bundle, kb = _load(args)
    stats = kb.stats()
    stats["rulesets"] = len(bundle.rulesets)
    stats["rulesets_approved"] = sum(1 for r in bundle.rulesets if r.is_approved)
    stats["networks"] = len(bundle.credence_engines)
    stats["corpus_problems"] = len(bundle.problems)

    for key, value in sorted(stats.items()):
        print(f"{key:<28} {value}")

    treatment_counts: dict[str, int] = {}
    for edges in kb.graph._edges.values():  # noqa: SLF001 - stats are a debug view
        for edge in edges:
            treatment_counts[edge.treatment.value] = (
                treatment_counts.get(edge.treatment.value, 0) + 1
            )
    if treatment_counts:
        print()
        print("treatment edges by class")
        for treatment in Treatment:
            count = treatment_counts.get(treatment.value, 0)
            if count:
                print(f"  {treatment.value:<20} {count}")

    courts: dict[str, int] = {}
    for judgment in kb.judgments.values():
        courts[judgment.court.value] = courts.get(judgment.court.value, 0) + 1
    if courts:
        print()
        print("judgments by court")
        for name, count in sorted(courts.items(), key=lambda kv: -_rank(kv[0])):
            print(f"  {name:<16} {count}   rank {_rank(name)}")
    return EXIT_OK


def _rank(court: str) -> int:
    try:
        return Court(court).rank
    except ValueError:
        return -1


def cmd_serve(args: argparse.Namespace) -> int:
    """Run the authoring surface.

    The FastAPI and uvicorn imports live behind the ``api`` extra, and the import
    error is the ordinary one: a person who runs a command and sees
    ``ModuleNotFoundError: No module named 'fastapi'`` learns nothing about what
    to do next.
    """
    try:
        from ratio.authoring.api import build_app
        from ratio.authoring.service import AuthoringService
    except ImportError as exc:  # pragma: no cover - requires the api extra
        print(
            "the authoring surface needs the api extra:\n"
            "  pip install 'ratio[api]'\n"
            f"(import failed: {exc})",
            file=sys.stderr,
        )
        return EXIT_UNUSABLE

    service = AuthoringService.build(
        args.fixtures,
        rulesets=args.rulesets,
        workspace=args.workspace,
        in_place=args.in_place,
    )
    described = service.store.describe()
    print(f"rulesets:    {described['directory']}")
    if described["origin"]:
        print(f"copied from: {described['origin']}  (edits do not touch it)")
    print(f"journal:     {described['journal']}")
    print(f"serving on   http://{args.host}:{args.port}")
    print()
    print("The shipped rulebook is a draft. Nothing here is legal advice.")

    ui_dist = ROOT / "ui" / "dist"
    app = build_app(service, ui_dist=ui_dist if ui_dist.is_dir() else None)
    if not ui_dist.is_dir():
        print()
        print(f"no built UI at {ui_dist}; serving the API only.")
        print("build it with:  cd ui && npm install && npm run build")
    try:
        import uvicorn
    except ImportError as exc:  # pragma: no cover - requires the api extra
        print(f"uvicorn is not installed: {exc}", file=sys.stderr)
        return EXIT_UNUSABLE
    uvicorn.run(app, host=args.host, port=args.port)
    return EXIT_OK


# --------------------------------------------------------------------------
# parser
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ratio",
        description="Citation-grounded knowledge base of judgments, with an "
        "executable encoding of the legal tests that reason over them.",
    )
    parser.add_argument(
        "--fixtures",
        default=str(DEFAULT_FIXTURES),
        help="directory holding judgments.yaml (default: data/fixtures)",
    )
    parser.add_argument(
        "--rulesets",
        default=str(DEFAULT_RULESETS),
        help="directory holding *.ruleset.yaml and *.credence.yaml",
    )
    parser.add_argument(
        "--allow-mixed",
        action="store_true",
        help="permit a corpus mixing synthetic and real authorities. For test "
        "harnesses only; never for a corpus that will answer questions.",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output where supported")

    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("ingest", help="load the corpus and report what it contains")
    p.add_argument(
        "path",
        nargs="?",
        help="corpus directory (default: --fixtures, i.e. data/fixtures). "
        "Accepted positionally because that is what people type.",
    )
    p.add_argument("--aliases", action="store_true", help="show the case-name table")
    p.set_defaults(func=cmd_ingest)

    p = sub.add_parser("authority", help="is this judgment still good law?")
    p.add_argument("key", nargs="?", help="neutral citation, e.g. FCAFC:2019:77")
    p.add_argument("--as-at", help="point-in-time status, ISO date")
    p.add_argument(
        "--confirmed-only",
        action="store_true",
        help="count only treatment a lawyer has signed off on",
    )
    p.add_argument("--history", action="store_true", help="also show what it relies on")
    p.set_defaults(func=cmd_authority)

    p = sub.add_parser("ask", help="retrieve paragraphs bearing on a question")
    p.add_argument("question", nargs="+")
    p.add_argument("--limit", type=int, default=8)
    p.add_argument("--court", action="append", help="restrict to a court code")
    p.add_argument("--judgment", action="append", help="restrict to a judgment id")
    p.add_argument("--as-at", help="ignore judgments decided after this date")
    p.add_argument("--explain", action="store_true", help="show ranking detail")
    p.add_argument("--width", type=int, default=280, help="snippet width")
    p.add_argument(
        "--exclude-synthetic",
        action="store_true",
        help="drop synthetic material, for a corpus that mixes it with real law",
    )
    p.add_argument(
        "--good-law",
        action="store_true",
        help="restrict to authorities currently assessed as good law",
    )
    p.set_defaults(func=cmd_ask)

    p = sub.add_parser(
        "cases",
        help="generate the decision-table review sheet for the ruleset",
    )
    p.add_argument("--ruleset", help="ruleset id, domain, or short name")
    p.add_argument("--max-cases", type=int, default=4096)
    p.add_argument("--limit", type=int, default=40)
    p.add_argument("--out", help="write the sheet to a file instead of stdout")
    p.set_defaults(func=cmd_cases)

    p = sub.add_parser("eval", help="score the system against the task suites")
    p.add_argument("--tasks", default=str(DEFAULT_TASKS))
    p.add_argument("--system", action="append", help="restrict to named system(s)")
    p.add_argument(
        "--against",
        help="system to compare the others against, on suites where both ran",
    )
    p.add_argument("--ruleset")
    p.set_defaults(func=cmd_eval)

    p = sub.add_parser("rules", help="list, validate or approve the rulebook")
    p.add_argument("action", choices=["list", "validate", "approve"])
    p.add_argument("--ruleset")
    p.add_argument("--by", help="approver name, for approve")
    p.add_argument("--role", help="approver capacity, for approve")
    p.add_argument("--note", help="approval note")
    p.set_defaults(func=cmd_rules)

    p = sub.add_parser("network", help="query the credence network")
    p.add_argument("--target", help="node to report on")
    p.add_argument(
        "--evidence",
        action="append",
        metavar="NODE=STATE",
        help="observed state, repeatable",
    )
    p.add_argument(
        "--top-k",
        type=int,
        default=6,
        help="how many sensitivities to report",
    )
    p.set_defaults(func=cmd_network)

    p = sub.add_parser("stats", help="one-screen health of the knowledge base")
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser(
        "serve",
        help="run the expert authoring surface over the rulebook",
        description=(
            "Serve the authoring surface. Edits land in a workspace copy unless "
            "--in-place is given, so pointing this at rulesets/ does not put the "
            "shipped rulebook behind a browser."
        ),
    )
    p.add_argument("--fixtures", type=Path, default=Path("data/fixtures"))
    p.add_argument("--rulesets", type=Path, default=Path("rulesets"))
    p.add_argument("--workspace", type=Path, default=None)
    p.add_argument(
        "--in-place",
        action="store_true",
        help="edit the ruleset directory itself, not a workspace copy",
    )
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.set_defaults(func=cmd_serve)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    # A corpus that will not load is an ordinary thing to get wrong -- the
    # commonest cause is running from a directory other than the project root, so
    # `data/fixtures` resolves somewhere that has no judgments.yaml in it. The
    # message already says which path it looked in; a traceback on top of that
    # buries the one useful sentence under twenty lines of stack.
    from ratio.ingest.fixtures import CorpusError

    try:
        return int(args.func(args))
    except CorpusError as exc:
        return _fail(f"{exc}\n  (paths are relative to the current directory: "
                     f"{Path.cwd()})")
    except FileNotFoundError as exc:
        return _fail(str(exc))
    except BrokenPipeError:  # piping into head(1)
        return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
