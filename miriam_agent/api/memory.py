"""Memory correction surface: let the user repair what Miriam remembers.

The agent loop is read-only by design, so none of these paths run inside
it. Each endpoint is authenticated (the bearer names the account) and
scoped to that user's own container tag — a caller can only rewrite their
own memory, never anyone else's. All endpoints fail open when Supermemory
is disabled: they report ``{"enabled": false}`` instead of breaking.

Endpoints:
* POST /memory/remember — store one explicit fact.
* POST /memory/correct — versioned correction of a stale fact.
* POST /memory/forget — dry-run preview by default; apply is bound to the
  previewed ids so the delete cannot drift.
* GET /memory/inferred — low-confidence derived facts awaiting review.
* POST /memory/inferred/{id}/review — approve | decline | undo.
* DELETE /memory — GDPR erasure of the caller's container.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from miriam_agent.api.dependencies import (
    get_current_user,
    get_supermemory_memory_dep,
)
from miriam_agent.database.models import User
from miriam_agent.integrations.supermemory_client import container_tag_for

logger = logging.getLogger(__name__)

router = APIRouter()


class RememberRequest(BaseModel):
    content: str = ""
    kind: str = "fact"
    is_static: bool = False


class CorrectRequest(BaseModel):
    new_content: str = ""
    memory_id: str | None = None
    query: str | None = None
    reason: str | None = None


class ForgetRequest(BaseModel):
    query: str = ""
    dry_run: bool = True
    reason: str | None = None


class ReviewRequest(BaseModel):
    action: str = ""


def _disabled() -> dict[str, Any]:
    return {"enabled": False, "detail": "memory is disabled"}


@router.post("/memory/remember")
async def remember_fact(
    body: RememberRequest,
    user: User = Depends(get_current_user),
    supermemory_memory: Any = Depends(get_supermemory_memory_dep),
) -> dict[str, Any]:
    content = (body.content or "").strip()
    if not content:
        return {"enabled": True, "stored": False, "detail": "content is required"}
    if supermemory_memory is None or not supermemory_memory.enabled:
        return _disabled()
    tag = container_tag_for(user.id)
    result = await supermemory_memory.remember_person_fact(
        tag, content, kind=(body.kind or "fact"), is_static=bool(body.is_static)
    )
    return {"enabled": True, "stored": result is not None}


@router.post("/memory/correct")
async def correct_fact(
    body: CorrectRequest,
    user: User = Depends(get_current_user),
    supermemory_memory: Any = Depends(get_supermemory_memory_dep),
) -> dict[str, Any]:
    new_content = (body.new_content or "").strip()
    if not new_content:
        return {"enabled": True, "updated": False, "detail": "new_content required"}
    if supermemory_memory is None or not supermemory_memory.enabled:
        return _disabled()
    tag = container_tag_for(user.id)
    result = await supermemory_memory.update(
        tag,
        new_content,
        memory_id=(body.memory_id or None),
        content=(body.query or None),
        metadata={"reason": body.reason or "user correction", "source": "miriam"},
    )
    return {"enabled": True, "updated": result is not None}


@router.post("/memory/forget")
async def forget_fact(
    body: ForgetRequest,
    user: User = Depends(get_current_user),
    supermemory_memory: Any = Depends(get_supermemory_memory_dep),
) -> dict[str, Any]:
    query = (body.query or "").strip()
    if not query:
        return {"enabled": True, "forgotten": False, "detail": "query is required"}
    if supermemory_memory is None or not supermemory_memory.enabled:
        return _disabled()
    tag = container_tag_for(user.id)
    if body.dry_run:
        preview = await supermemory_memory.forget(
            tag, query=query, dry_run=True, reason=body.reason
        )
        candidates = (preview or {}).get("candidates") or []
        return {
            "enabled": True,
            "dry_run": True,
            "candidates": len(candidates),
            "preview": preview,
        }
    applied = await supermemory_memory.forget_exact(
        tag, query, reason=body.reason
    )
    return {"enabled": True, "dry_run": False, "applied": applied}


@router.get("/memory/inferred")
async def list_inferred(
    user: User = Depends(get_current_user),
    supermemory_memory: Any = Depends(get_supermemory_memory_dep),
) -> dict[str, Any]:
    if supermemory_memory is None or not supermemory_memory.enabled:
        return {"enabled": False, "memories": []}
    tag = container_tag_for(user.id)
    memories = await supermemory_memory.list_inferred(tag)
    return {"enabled": True, "memories": memories, "total": len(memories)}


@router.post("/memory/inferred/{memory_id}/review")
async def review_inferred(
    memory_id: str,
    body: ReviewRequest,
    user: User = Depends(get_current_user),
    supermemory_memory: Any = Depends(get_supermemory_memory_dep),
) -> dict[str, Any]:
    action = (body.action or "").strip().lower()
    if action not in ("approve", "decline", "undo"):
        return {
            "enabled": True,
            "reviewed": False,
            "detail": "action must be approve|decline|undo",
        }
    if supermemory_memory is None or not supermemory_memory.enabled:
        return _disabled()
    tag = container_tag_for(user.id)
    result = await supermemory_memory.review_inferred(tag, memory_id, action)
    return {"enabled": True, "reviewed": result is not None, "action": action}


@router.delete("/memory")
async def erase_memory(
    user: User = Depends(get_current_user),
    supermemory_memory: Any = Depends(get_supermemory_memory_dep),
) -> dict[str, Any]:
    """GDPR erasure of the caller's graph container.

    Local audit rows stay (compliance); the semantic graph is deleted.
    """
    if supermemory_memory is None or not supermemory_memory.enabled:
        return _disabled()
    tag = container_tag_for(user.id)
    ok = await supermemory_memory.erase_user_data(tag)
    return {"enabled": True, "erased": bool(ok)}
