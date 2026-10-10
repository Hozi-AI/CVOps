"""Throwaway server-rendered frame viewer at `/dataset`.

Renders raw `samples` rows (the output of `extract_frames`) grouped by data
source so the ingest pipeline's output is visible in a browser. Thumbnails link
to the full image; both are loaded straight from Garage via presigned URLs, so
the only authenticated request is the page itself.

This is deliberately minimal — no template engine, no model, no migration. The
React frontend replaces it later. A disabled "Send to CVAT" button per source
foreshadows the `human_review` gate step.
"""

from __future__ import annotations

import html
import uuid
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cvops_api.core.auth import get_viewer_user
from cvops_api.core.storage import get_storage, public_s3_endpoint
from cvops_api.db.session import get_session
from cvops_api.db.models.auth import User
from cvops_api.db.models.projects import Project
from cvops_api.db.models.samples import DataSource, Sample

router = APIRouter()

_DEFAULT_LIMIT = 200
_MAX_LIMIT = 500

_STYLE = """
body { font-family: system-ui, sans-serif; margin: 0; background: #fafafa; color: #1a1a1a; }
header.page { padding: 1rem 1.5rem; border-bottom: 1px solid #e2e2e2; background: #fff; }
header.page h1 { margin: 0; font-size: 1.25rem; }
main { padding: 1.5rem; }
section.source { margin-bottom: 2rem; }
section.source > h2 { font-size: 1rem; margin: 0 0 .25rem; }
.src-meta { color: #666; font-size: .85rem; margin-bottom: .75rem; display: flex; gap: .75rem; align-items: center; flex-wrap: wrap; }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(140px, 1fr)); gap: .5rem; }
.grid a { display: block; border: 1px solid #e2e2e2; border-radius: 4px; overflow: hidden; background: #fff; }
.grid img { display: block; width: 100%; height: 120px; object-fit: cover; }
button[disabled] { padding: .25rem .6rem; font-size: .8rem; border: 1px solid #ccc; border-radius: 4px; background: #f0f0f0; color: #999; cursor: not-allowed; }
.empty { color: #666; font-style: italic; }
.note { color: #b8860b; font-size: .85rem; margin-bottom: 1rem; }
ul.projects { list-style: none; padding: 0; }
ul.projects li { margin: .35rem 0; }
a { color: #1a5fb4; }
"""


def _page(title: str, body: str) -> str:
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{html.escape(title)}</title><style>{_STYLE}</style></head>"
        f"<body><header class='page'><h1>{html.escape(title)}</h1></header>"
        f"<main>{body}</main></body></html>"
    )


def _token_qs(token: str | None) -> str:
    """`&token=…` suffix to carry the viewer token through in-page links."""
    return f"&token={quote(token)}" if token else ""


@router.get("/dataset", response_class=HTMLResponse)
async def dataset_view(
    request: Request,
    project_id: uuid.UUID | None = Query(None),
    source_id: uuid.UUID | None = Query(None),
    limit: int = Query(_DEFAULT_LIMIT, ge=1, le=_MAX_LIMIT),
    token: str | None = Query(None),
    user: User = Depends(get_viewer_user),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    if project_id is None:
        return HTMLResponse(await _landing(session, user, token))
    return HTMLResponse(
        await _project_view(request, session, user, project_id, source_id, limit, token)
    )


async def _landing(session: AsyncSession, user: User, token: str | None) -> str:
    r = await session.execute(
        select(Project)
        .where(Project.org_id == user.org_id, Project.deleted_at == None)  # noqa: E711
        .order_by(Project.name)
    )
    projects = list(r.scalars().all())
    if not projects:
        return _page("Datasets", "<p class='empty'>No projects in your org yet.</p>")

    qs = _token_qs(token)
    items = "".join(
        f"<li><a href='/dataset?project_id={p.id}{qs}'>{html.escape(p.name)}</a></li>"
        for p in projects
    )
    return _page("Datasets", f"<ul class='projects'>{items}</ul>")


async def _project_view(
    request: Request,
    session: AsyncSession,
    user: User,
    project_id: uuid.UUID,
    source_id: uuid.UUID | None,
    limit: int,
    token: str | None,
) -> str:
    # Project scoping — mirrors routers/samples._check_project.
    pr = await session.execute(
        select(Project).where(
            Project.id == project_id,
            Project.org_id == user.org_id,
            Project.deleted_at == None,  # noqa: E711
        )
    )
    project = pr.scalar_one_or_none()
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")

    sq = select(Sample).where(Sample.project_id == project_id)
    if source_id is not None:
        sq = sq.where(Sample.source_id == source_id)
    sq = sq.order_by(Sample.source_id, Sample.frame_index, Sample.id).limit(limit + 1)
    sr = await session.execute(sq)
    samples = list(sr.scalars().all())
    capped = len(samples) > limit
    if capped:
        samples = samples[:limit]

    dr = await session.execute(select(DataSource).where(DataSource.project_id == project_id))
    sources = {ds.id: ds for ds in dr.scalars().all()}

    title = f"Dataset · {project.name}"
    if not samples:
        return _page(title, "<p class='empty'>No frames extracted for this project yet.</p>")

    storage = get_storage()
    endpoint = public_s3_endpoint(request.url.hostname)

    # Group samples by source, preserving the source_id/frame_index ordering.
    grouped: dict[uuid.UUID, list[Sample]] = {}
    for s in samples:
        grouped.setdefault(s.source_id, []).append(s)

    body_parts: list[str] = []
    if capped:
        body_parts.append(
            f"<p class='note'>Showing the first {limit} frames — "
            "refine by appending <code>&amp;source_id=…</code>.</p>"
        )

    for sid, group in grouped.items():
        ds = sources.get(sid)
        ds_type = html.escape(ds.type) if ds else "unknown"
        ds_status = html.escape(ds.status) if ds else "?"
        short_id = html.escape(str(sid)[:8])

        cells: list[str] = []
        for s in group:
            full_url = await storage.get_presigned_get(s.blob_hash, endpoint=endpoint)
            thumb_hash = s.thumbnail_hash or s.blob_hash
            thumb_url = await storage.get_presigned_get(thumb_hash, endpoint=endpoint)
            alt = f"frame {s.frame_index}" if s.frame_index is not None else "frame"
            cells.append(
                f"<a href='{html.escape(full_url)}' target='_blank' rel='noopener'>"
                f"<img src='{html.escape(thumb_url)}' alt='{html.escape(alt)}' "
                "loading='lazy'></a>"
            )

        body_parts.append(
            "<section class='source'>"
            f"<h2>{ds_type} · {short_id}</h2>"
            f"<div class='src-meta'><span>status: {ds_status}</span>"
            f"<span>{len(group)} frame(s)</span>"
            "<button disabled title='Coming with the human-review gate step'>"
            "Send to CVAT</button></div>"
            f"<div class='grid'>{''.join(cells)}</div>"
            "</section>"
        )

    return _page(title, "".join(body_parts))
