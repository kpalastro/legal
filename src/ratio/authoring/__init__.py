"""The expert authoring surface: a service, and a transport over it.

Three modules, and the split between them is the same split the MCP layer makes,
for the same reason.

``store``
    Where an edited rule lives on disk, how the file it came from is protected,
    and what record is left of the edit. This is the only module in the
    repository that writes to the rulebook.

``service``
    Every authoring operation, as a plain function over JSON-serialisable data --
    inspecting a ruleset, validating a *candidate* that has not been saved,
    previewing it against facts, diffing it against the last revision an expert
    signed, saving it, and recording a signature. It imports no web framework, so
    it runs and is tested with nothing installed beyond the core dependencies.

``api``
    A thin HTTP adapter. It contains no policy of its own: if a rule about what
    may be said appears there rather than in ``service``, it is in the wrong
    place, and the tests will not see it. A transport is the easiest place in a
    system like this to accidentally reimplement a control.

See docs/06-expert-authoring-surface.md for what this surface is for, and in
particular for the one control it deliberately does *not* make convenient.
"""

from ratio.authoring.service import AuthoringService
from ratio.authoring.store import RulesetStore, StoreError

__all__ = ["AuthoringService", "RulesetStore", "StoreError"]
