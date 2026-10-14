"""Where an edited rule lives, and what is left of the edit.

The authoring surface is the only part of this system that writes to disk, which
makes it the only part where a mistake is not undone by reloading. Three
decisions follow, and they are the whole of this module.

**Edits land in a workspace, not in the shipped rulebook.** Opening the store on
``rulesets/`` copies the ``*.ruleset.yaml`` files into a working directory and
edits them there, so that a click in a browser cannot rewrite the file the
repository ships, the Quickstart uses, or the test suite asserts against. The
copy is not hidden -- the API reports the workspace path and the UI shows it in
the header -- because a surface that edits a file you did not mean to edit is
worse than one that refuses to edit at all. ``in_place=True`` edits the originals
and is the thing you pass deliberately.

**The file's header comment is preserved verbatim.** The comment block at the top
of ``firb_notifiable_action.ruleset.yaml`` is not decoration: it states why the
minority limb is an ``all_of`` and not an ``any_of``, which is the single most
important line in the file. A serialiser that dumps ``model_dump()`` over the
whole document deletes it on the first save, and the deletion is silent. So the
leading comment run is split off before parsing and re-emitted unchanged.

**Nothing is written without a journal entry.** Every save and every signature
appends a line to an append-only JSONL file beside the workspace, carrying the
edit's before and after digests, who made it, when, and why. A governed artefact
has to be able to say what it was and who changed it; the full audit ledger in
``docs/09-observability-and-audit.md`` is not built, and this is the local
precursor that costs almost nothing and closes the gap that matters first.

The journal is also what makes a diff against the *last signed revision*
possible. The ruleset on disk only ever holds the current revision; the approved
one is gone the moment it is edited, so the payload is journalled at signature
time and read back to show an expert exactly what they are being asked to
renew.
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from enum import Enum
from pathlib import Path
from typing import Any

import yaml

from ratio.ingest.fixtures import load_ruleset
from ratio.model import Approval, RuleSet
from ratio.rules.engine import validate_ruleset

RULESET_GLOB = "*.ruleset.yaml"
CREDENCE_GLOB = "*.credence.yaml"
JOURNAL_NAME = "edits.jsonl"
DEFAULT_WORKSPACE = ".ratio-workspace"

# A ruleset id becomes a filename, so it is constrained to what a filename can
# safely be. Without this, a candidate posted by a client with the id
# ``../../../../home/user/.bashrc`` would be written wherever it pointed: the id
# is caller-supplied and it is used as a path component, which is the entire
# definition of a path traversal.
_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}$")

# The order keys are written in. Not the model's declaration order -- the order
# the hand-written rulebook uses, so that a saved file diffs against the
# original in the places that changed rather than everywhere.
_RULESET_KEY_ORDER = (
    "id", "title", "question", "domain", "version", "effective_from",
    "effective_to", "root", "status", "notes", "provision_keys", "nodes",
    "approvals",
)
_NODE_KEY_ORDER = (
    "id", "kind", "exception_of", "label", "statement", "children", "predicate",
    "bn_query", "bn_threshold", "provenance",
)


class StoreError(RuntimeError):
    """The store cannot do what was asked, and the caller must not proceed."""


@dataclass
class StoredRuleset:
    ruleset: RuleSet
    path: Path
    origin: Path | None  # the file this one was copied from, in workspace mode
    issue_count: int = 0


@dataclass
class RulesetStore:
    """A directory of ``*.ruleset.yaml`` files, and a journal of what happened."""

    directory: Path
    origin: Path | None = None
    journal_path: Path = field(default_factory=lambda: Path(JOURNAL_NAME))
    in_place: bool = False
    entries: dict[str, StoredRuleset] = field(default_factory=dict)

    # -- construction ------------------------------------------------------

    @classmethod
    def open(
        cls,
        source: Path,
        *,
        workspace: Path | None = None,
        in_place: bool = False,
    ) -> RulesetStore:
        """Open ``source``, copying it into a workspace unless told otherwise."""
        source = Path(source)
        if not source.exists():
            raise StoreError(
                f"no ruleset directory at {source}. Point --rulesets at a directory "
                f"of {RULESET_GLOB} files (the default is rulesets/)."
            )

        if in_place:
            store = cls(directory=source, origin=None, in_place=True)
            store.journal_path = source / JOURNAL_NAME
        else:
            workspace = Path(workspace) if workspace else Path(DEFAULT_WORKSPACE)
            target = workspace / "rulesets"
            target.mkdir(parents=True, exist_ok=True)
            # Credence networks come too. They are not editable through this
            # surface, and copying them is what keeps the workspace a *corpus*
            # rather than a ruleset directory: a workspace missing its networks
            # loads without error and silently reports every delegate_bn node as
            # unknown, which looks exactly like a ruleset that is working.
            for pattern in (RULESET_GLOB, CREDENCE_GLOB):
                for path in sorted(source.glob(pattern)):
                    shutil.copy2(path, target / path.name)
            store = cls(directory=target, origin=source, in_place=False)
            store.journal_path = workspace / JOURNAL_NAME

        store.reload()
        return store

    # -- reading -----------------------------------------------------------

    def reload(self) -> None:
        self.entries = {}
        for path in sorted(self.directory.glob(RULESET_GLOB)):
            ruleset = load_ruleset(path)
            origin = (self.origin / path.name) if self.origin else None
            self.entries[ruleset.id] = StoredRuleset(
                ruleset=ruleset,
                path=path,
                origin=origin,
                issue_count=len(validate_ruleset(ruleset)),
            )

    def rulesets(self) -> list[RuleSet]:
        return [e.ruleset for e in self.entries.values()]

    def get(self, ruleset_id: str) -> StoredRuleset | None:
        return self.entries.get(ruleset_id)

    def path_for(self, ruleset_id: str):
        """Where a ruleset id would be written, creating the id's file if new."""
        entry = self.entries.get(ruleset_id)
        if entry is not None:
            return entry.path
        check_ruleset_id(ruleset_id)
        return self.directory / f"{ruleset_id}.ruleset.yaml"

    # -- writing -----------------------------------------------------------

    def write(self, ruleset: RuleSet) -> Path:
        """Serialise ``ruleset`` over its file, preserving the header comment."""
        path = self.path_for(ruleset.id)
        header = split_header(path.read_text(encoding="utf-8")) if path.exists() else None
        path.write_text(render_ruleset(ruleset, header=header), encoding="utf-8")
        return path

    def journal(self, entry: dict[str, Any]) -> None:
        """Append one record. Never rewrites, never truncates."""
        entry = {"at": datetime.now(UTC).isoformat(), **entry}
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        with self.journal_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, sort_keys=False) + "\n")

    def journal_entries(self, ruleset_id: str | None = None) -> list[dict[str, Any]]:
        if not self.journal_path.exists():
            return []
        out = []
        for line in self.journal_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                # A truncated final line is what a process killed mid-write
                # leaves. Dropping it is right: an entry that cannot be parsed
                # is not evidence, and refusing to show the rest of the journal
                # because of it would hide the entries that are.
                continue
            if ruleset_id is None or entry.get("ruleset") == ruleset_id:
                out.append(entry)
        return out

    def last_signed_payload(self, ruleset_id: str) -> dict[str, Any] | None:
        """The canonical payload of the most recent signed revision, if any."""
        for entry in reversed(self.journal_entries(ruleset_id)):
            if entry.get("action") == "approve" and entry.get("payload"):
                try:
                    return json.loads(entry["payload"])
                except json.JSONDecodeError:
                    return None
        return None

    def describe(self) -> dict[str, Any]:
        return {
            "directory": str(self.directory.resolve()),
            "origin": str(self.origin.resolve()) if self.origin else None,
            "in_place": self.in_place,
            "journal": str(self.journal_path.resolve()),
            "rulesets": sorted(self.entries),
        }


# --------------------------------------------------------------------------
# serialisation
# --------------------------------------------------------------------------


def check_ruleset_id(ruleset_id: str) -> None:
    if not _ID_PATTERN.match(ruleset_id or ""):
        raise StoreError(
            f"{ruleset_id!r} is not usable as a ruleset id: an id names a file, so "
            "it may contain only letters, digits, dot, dash and underscore, and "
            "must begin with a letter or digit"
        )


def split_header(text: str) -> str:
    """The leading run of comment and blank lines, verbatim.

    Returned with a trailing newline so it can be concatenated straight onto a
    dumped document. A file with no leading comment returns ``""``.
    """
    lines = text.splitlines(keepends=True)
    end = 0
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("#") or not stripped:
            end = index + 1
        else:
            break
    header = "".join(lines[:end])
    if not header:
        return ""
    return header.rstrip("\n") + "\n\n"


def render_ruleset(ruleset: RuleSet, *, header: str | None = None) -> str:
    """The YAML file for ``ruleset``, with ``header`` above it.

    Written in a canonical form rather than preserving the file's existing
    layout: prose fields become literal blocks, dates are written unquoted, and
    long scalars are not wrapped. Values survive exactly -- the round trip
    through this function and back through :func:`~ratio.ingest.fixtures.load_ruleset`
    is identical at the model level, and ``tests/test_authoring.py`` pins that --
    but the *text* differs, so the first save of a hand-formatted ruleset shows a
    formatting diff before it shows the edit. On the shipped FIRB ruleset that
    diff is about 120 lines, of which the edit is none.

    Two of those differences are worth naming, because they are not the same kind
    of thing.

    *Cosmetic.* Statements written as hand-wrapped ``>`` blocks come back as one
    long line, and provision keys written as ``"statute:...:18"`` come back
    unquoted (a plain scalar, because a colon is only a mapping indicator when a
    space follows it). Both reload to exactly the same value.

    *Lossy.* Comments interleaved among the nodes are deleted. The leading header
    run is preserved -- see :func:`split_header` -- but the block comments above
    each subtree, which say why that subtree is shaped the way it is, are inside
    the node list and a YAML emitter has nowhere to put them. This is the one
    difference that destroys something, and it is why the header says what it
    says: anything that must survive a save has to be a field, not a comment.

    That is the trade. None of the standard library's YAML support round-trips
    comments or scalar styles, and a formatting-preserving writer is a dependency;
    this repository runs on four of them, and spending a fifth to avoid a one-time
    diff in a file that is edited in one place is not the trade worth making. What
    matters is that the canonical form is *stable*: after the first save, changing
    one node's label is a one-line diff, which is what makes a rulebook reviewable.
    """
    if header is None:
        header = generated_header(ruleset)
    doc = _prune(ruleset.model_dump(mode="python", exclude_none=True))
    doc = _ordered(doc, _RULESET_KEY_ORDER)
    doc["nodes"] = [
        _ordered(node, _NODE_KEY_ORDER) for node in doc.get("nodes", [])
    ]
    body = yaml.dump(
        doc,
        Dumper=_Dumper,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
        # Effectively no wrapping. PyYAML's default width folds long plain
        # scalars across lines, which rewrites every quotation in a ruleset that
        # has one on a single line. A quote is one line of text and reads as one.
        width=10**6,
    )
    return header + body


class _Dumper(yaml.SafeDumper):
    """Emits a ruleset in the shape a person writes it in.

    The one thing that needs overriding is sequence indentation. PyYAML writes a
    block sequence flush with the key that introduces it::

        nodes:
        - id: significant_action

    and every hand-written rulebook writes it indented::

        nodes:
          - id: significant_action

    Semantically identical, and it is most of the file's lines -- so a save that
    changed one node's label rewrote the whole document, and the one line that
    actually changed was invisible in the diff. A rulebook whose edits cannot be
    reviewed is a rulebook whose edits are not reviewed.
    """

    def increase_indent(self, flow: bool = False, indentless: bool = False):
        return super().increase_indent(flow, False)


def _represent_str(dumper: yaml.SafeDumper, data: str):
    """Plain where it fits on a line, folded for one paragraph, literal beyond.

    Chosen to match what the rulebook already looks like, so that a save shows
    the edit rather than a change of notation. The three cases are not stylistic
    preference:

    * no newline -- plain. A quotation is one line of text and reads as one.
    * one paragraph ending in a newline -- folded ``>``, which is what the
      hand-written file uses for statements and notes.
    * anything else -- literal ``|``. Folding is *lossy* across a blank line:
      ``>`` collapses a paragraph break into a single newline, so a two-paragraph
      note would come back as a one-paragraph note. The literal style is the only
      one that returns exactly what it was given, and prose that survives a save
      unchanged matters more than prose that is prettily wrapped.
    """
    tag = "tag:yaml.org,2002:str"
    if "\n" not in data:
        return dumper.represent_scalar(tag, data)
    single = data.endswith("\n") and "\n" not in data[:-1]
    # Folding also strips significant leading and trailing whitespace, so it is
    # only safe when there is none to strip.
    if single and data == data.strip() + "\n":
        return dumper.represent_scalar(tag, data, style=">")
    return dumper.represent_scalar(tag, data, style="|")


def _represent_date(dumper: yaml.SafeDumper, data):
    # Unquoted. A quoted date is still a correct round trip, and it makes every
    # date line in the file look edited.
    return dumper.represent_scalar("tag:yaml.org,2002:timestamp", data.isoformat())


def _represent_datetime(dumper: yaml.SafeDumper, data):
    return dumper.represent_scalar("tag:yaml.org,2002:timestamp", data.isoformat())


def _represent_enum(dumper: yaml.SafeDumper, data):
    return dumper.represent_scalar("tag:yaml.org,2002:str", str(data.value))


_Dumper.add_representer(str, _represent_str)
_Dumper.add_representer(date, _represent_date)
_Dumper.add_representer(datetime, _represent_datetime)
_Dumper.add_multi_representer(Enum, _represent_enum)


def generated_header(ruleset: RuleSet) -> str:
    return (
        "# =============================================================================\n"
        f"# {ruleset.title}\n"
        "# =============================================================================\n"
        "#\n"
        "# Created in the RATIO authoring surface. Every node carries `provenance`: a\n"
        "# pointer to the statutory provision or the paragraph of an existing judgment\n"
        "# that requires it. `status: draft` until an expert signs this exact revision;\n"
        "# see docs/06-expert-authoring-surface.md.\n"
        "# =============================================================================\n\n"
    )


def _ordered(doc: dict[str, Any], order: tuple[str, ...]) -> dict[str, Any]:
    """Reorder ``doc``'s keys to ``order``, then whatever else it has.

    ``model_dump`` emits fields in declaration order, which is not the order the
    rulebook is written in. Writing the file in the rulebook's own order keeps a
    saved revision diffing against the original in the lines that changed.
    """
    out = {key: doc[key] for key in order if key in doc}
    out.update({key: value for key, value in doc.items() if key not in out})
    return out


# Keys whose empty string means "unset", as opposed to a meaningful empty value.
# ``predicate.value`` is deliberately absent: a predicate testing equality
# against the empty string is a predicate, and dropping its value would leave a
# node that validates and means something else.
_OPTIONAL_PROSE = frozenset(
    {"note", "notes", "quote", "proposition_id", "heading", "bn_query", "label", "statement"}
)


def _prune(value: Any, key: str | None = None) -> Any:
    """Drop absent values, so a leaf node does not grow a ``children: []``.

    ``exclude_none=True`` handles ``None`` and stops there. Every collection
    field in the model defaults to an empty tuple rather than ``None``, so a
    node with no children is dumped with ``children: []`` and a ruleset with no
    approvals with ``approvals: []``. Neither appears in the hand-written
    rulebook, so the first save adds a line to every leaf -- and a diff that
    grows a line per node for no reason is a diff a reviewer stops reading.
    """
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for name, item in value.items():
            if item is None:
                continue
            if isinstance(item, (list, tuple, dict)) and not item:
                continue
            if item == "" and name in _OPTIONAL_PROSE:
                continue
            out[name] = _prune(item, name)
        return out
    if isinstance(value, (list, tuple)):
        return [_prune(item, key) for item in value]
    return value


__all__ = [
    "DEFAULT_WORKSPACE",
    "JOURNAL_NAME",
    "RULESET_GLOB",
    "RulesetStore",
    "StoredRuleset",
    "StoreError",
    "generated_header",
    "render_ruleset",
    "split_header",
]
