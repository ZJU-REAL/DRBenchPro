"""FastAPI application exposing deterministic Wikipedia research tools."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException

from .models import (
    FindRequest,
    FindResponse,
    HealthResponse,
    ManifestResponse,
    OpenRequest,
    OpenResponse,
    SearchRequest,
    SearchResponse,
)
from .store import InvalidSourceId, SourceNotFound, WikipediaStore


def create_app(
    database_path: Path,
    expected_snapshot_id: Optional[str] = None,
    allow_incomplete: bool = False,
) -> FastAPI:
    """Create a search service bound to one immutable snapshot database."""

    store = WikipediaStore(
        database_path=database_path,
        expected_snapshot_id=expected_snapshot_id,
        allow_incomplete=allow_incomplete,
    )
    app = FastAPI(
        title="DRBenchPro Local Wikipedia",
        version="1.0.0",
        description="Offline search/open/find over a fixed Wikipedia snapshot.",
    )
    app.state.wikipedia_store = store

    @app.get("/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        return store.health()

    @app.get("/manifest", response_model=ManifestResponse)
    def manifest() -> ManifestResponse:
        return ManifestResponse(
            snapshot_id=store.snapshot_id,
            index_id=store.index_id,
            build_status=store.build_status,
            complete_snapshot=store.complete_snapshot,
            manifest=store.manifest(),
        )

    @app.post("/v1/search", response_model=SearchResponse)
    def search(request: SearchRequest) -> SearchResponse:
        if not request.query.strip():
            raise HTTPException(status_code=400, detail="query must not be blank")
        return store.search(request.query, request.top_k)

    @app.post("/v1/open", response_model=OpenResponse)
    def open_source(request: OpenRequest) -> OpenResponse:
        try:
            return store.open(request.source_id, request.cursor, request.max_chars)
        except InvalidSourceId as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except SourceNotFound as exc:
            raise HTTPException(status_code=404, detail="source_id not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/v1/find", response_model=FindResponse)
    def find(request: FindRequest) -> FindResponse:
        if not request.pattern.strip():
            raise HTTPException(status_code=400, detail="pattern must not be blank")
        try:
            return store.find(request.source_id, request.pattern, request.max_results)
        except InvalidSourceId as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except SourceNotFound as exc:
            raise HTTPException(status_code=404, detail="source_id not found") from exc

    return app
