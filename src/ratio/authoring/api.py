"""A thin HTTP adapter over the authoring service.

Kept deliberately empty of judgement. Every route is a call to a method on
:class:`~ratio.authoring.service.AuthoringService` and a return of its result;
there is no validation here, no defaulting, no derived field. The reason is the
one the MCP adapter gives for the same split -- a transport is the easiest place
in a system like this to accidentally acquire a policy, and a policy that lives
here is invisible to every test that does not spin up a server.

Two choices are worth stating because they are visible to a client.

**A refusal is a 200.** When the service declines -- an unknown ruleset, a
revision that does not validate, a digest that was not confirmed -- the response
is ``200`` with ``{"refused": true, "reason": ..., "message": ...}`` and whatever
detail the reason carries. It is not a 4xx. A refusal is the system working: it
looked at the request and correctly declined it, and the client's job is to show
its user what is missing, not to retry. Returning an error status invites exactly
the retry loop the refusal exists to prevent, and it makes a governance control
indistinguishable from a malformed request. The same contract is what the MCP
layer returns, so a client written against one transport works against the other.

**Bodies are validated by the domain model, not by the transport.** Request
bodies arrive as plain JSON and are handed to ``RuleSet.model_validate``, which
is the same validator the corpus loader uses. FastAPI is not asked to describe
the ruleset schema, because a second description of it would be a second thing
to keep true.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ratio.authoring.service import AuthoringService, Refused, StoreError

# The Vite dev server's origin. Listed explicitly rather than as "*" because the
# authoring surface edits files on this machine.
DEV_ORIGINS = (
    "http://localhost:5173",
    "http://127.0.0.1:5173",
)


def build_app(service: AuthoringService, *, ui_dist: Path | None = None):
    """Wire the service onto FastAPI. Requires the ``api`` extra."""
    try:
        from fastapi import FastAPI, Request
        from fastapi.middleware.cors import CORSMiddleware
        from fastapi.responses import JSONResponse
    except ImportError as exc:  # pragma: no cover - requires the api extra
        raise SystemExit(
            "the api extra is not installed; run: pip install 'ratio[api]'"
        ) from exc

    app = FastAPI(
        title="RATIO authoring surface",
        version="0.1.0",
        description=(
            "Inspect, edit and sign the encoded rulebook. Every conclusion carries "
            "its ruleset version and approval state; the shipped ruleset is a draft."
        ),
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(DEV_ORIGINS),
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "OPTIONS"],
        allow_headers=["Content-Type"],
    )

    @app.exception_handler(Refused)
    async def _refused(_request: Request, exc: Refused):  # pragma: no cover
        return JSONResponse(status_code=200, content=exc.to_payload())

    @app.exception_handler(StoreError)
    async def _store_error(_request: Request, exc: StoreError):  # pragma: no cover
        return JSONResponse(
            status_code=200,
            content={
                "refused": True,
                "reason": "store_error",
                "message": str(exc),
            },
        )

    # -- the corpus ----------------------------------------------------

    @app.get("/api/health")
    def health() -> dict[str, Any]:  # pragma: no cover
        return {"ok": True, "store": service.store.describe()}

    @app.get("/api/corpus")
    def corpus() -> dict[str, Any]:
        return service.corpus()

    @app.get("/api/authorities")
    def authorities() -> dict[str, Any]:
        return service.authorities()

    @app.get("/api/authorities/{key:path}/status")
    def authority_status(key: str) -> dict[str, Any]:
        return service.authority_status(key)

    @app.get("/api/provisions")
    def provisions() -> dict[str, Any]:
        return service.provisions()

    # -- rulesets ------------------------------------------------------

    @app.get("/api/rulesets")
    def rulesets() -> dict[str, Any]:
        return service.list_rulesets()

    @app.get("/api/rulesets/{ruleset_id}")
    def ruleset(ruleset_id: str) -> dict[str, Any]:
        return service.ruleset(ruleset_id)

    @app.post("/api/rulesets/{ruleset_id}/candidate")
    def candidate(ruleset_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """The same payload as GET, computed over an unsaved revision."""
        return service.ruleset(ruleset_id, candidate=body.get("ruleset", body))

    @app.post("/api/rulesets/{ruleset_id}/preview")
    def preview(ruleset_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return service.preview(body.get("ruleset", {}), body.get("facts", {}) or {})

    @app.post("/api/rulesets/{ruleset_id}/signature")
    def signature(ruleset_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return service.signature_block(
            ruleset_id,
            candidate=body.get("ruleset"),
            approver=body.get("approver", "") or "",
            role=body.get("role", "") or "",
            note=body.get("note"),
        )

    @app.get("/api/rulesets/{ruleset_id}/review-sheet")
    def review_sheet(ruleset_id: str, limit: int = 40) -> dict[str, Any]:
        return service.review_sheet(ruleset_id, limit=limit)

    @app.put("/api/rulesets/{ruleset_id}")
    def save(ruleset_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return service.save(
            ruleset_id,
            body.get("ruleset", {}),
            editor=body.get("editor", "") or "",
            note=body.get("note"),
        )

    @app.post("/api/rulesets/{ruleset_id}/sign")
    def sign(ruleset_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return service.sign(
            ruleset_id,
            body.get("ruleset", {}),
            approver=body.get("approver", "") or "",
            role=body.get("role", "") or "",
            note=body.get("note"),
            confirm_digest=body.get("confirm_digest", "") or "",
        )

    if ui_dist is not None and Path(ui_dist).is_dir():
        _mount_ui(app, Path(ui_dist))

    return app


def _mount_ui(app, ui_dist: Path) -> None:  # pragma: no cover - needs a built UI
    """Serve a built bundle from the same process, so one command runs both.

    Two servers and a proxy is the right development arrangement and the wrong
    one for reading a rulebook, and the difference is a second terminal nobody
    starts. When ``ui/dist`` exists it is served here, after ``/api`` has been
    claimed, so the API keeps priority.
    """
    from fastapi.responses import FileResponse
    from fastapi.staticfiles import StaticFiles

    index = ui_dist / "index.html"
    app.mount("/assets", StaticFiles(directory=ui_dist / "assets"), name="assets")

    @app.get("/")
    def _index():  # pragma: no cover
        return FileResponse(index)


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - needs the extra
    import argparse

    parser = argparse.ArgumentParser(
        prog="ratio-authoring",
        description="The expert authoring surface over the encoded rulebook.",
    )
    parser.add_argument("--fixtures", type=Path, default=Path("data/fixtures"))
    parser.add_argument("--rulesets", type=Path, default=Path("rulesets"))
    parser.add_argument("--workspace", type=Path, default=None)
    parser.add_argument(
        "--in-place",
        action="store_true",
        help="edit the ruleset directory itself instead of a workspace copy",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)

    try:
        import uvicorn
    except ImportError as exc:
        raise SystemExit(
            "the api extra is not installed; run: pip install 'ratio[api]'"
        ) from exc

    service = AuthoringService.build(
        args.fixtures, rulesets=args.rulesets, workspace=args.workspace,
        in_place=args.in_place,
    )
    described = service.store.describe()
    print(f"rulesets:  {described['directory']}")
    if described["origin"]:
        print(f"copied from: {described['origin']}  (edits do not touch it)")
    print(f"journal:   {described['journal']}")
    print(f"serving on http://{args.host}:{args.port}")
    uvicorn.run(build_app(service, ui_dist=Path("ui/dist")), host=args.host, port=args.port)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = ["DEV_ORIGINS", "build_app", "main"]
