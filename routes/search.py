"""有界、只读的跨存档剧情检索路由。"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException

from core.search_service import (
    SearchError,
    SearchInputError,
    SearchLimitExceeded,
    SearchProjectNotFound,
    SearchService,
    empty_search_response,
    normalize_search_query,
    validate_search_limit,
    validate_search_scope,
)
from core.session_manager import get_session_store
from routes.common import _norm_project


router = APIRouter()


def get_search_service() -> SearchService:
    return SearchService(get_session_store())


def clear_search_services_for_testing() -> None:
    """兼容测试清理入口；服务不持有独立缓存。"""


@router.get("/api/search")
async def api_search(
    project: str = "默认项目",
    q: str = "",
    scope: str = "all",
    limit: str = "50",
):
    project = _norm_project(project)
    try:
        query = normalize_search_query(q)
        scope = validate_search_scope(scope)
        limit = validate_search_limit(limit)
        if not query:
            return empty_search_response(project, scope)
        return await asyncio.to_thread(
            get_search_service().search,
            project=project,
            q=query,
            scope=scope,
            limit=limit,
        )
    except SearchLimitExceeded as exc:
        raise HTTPException(413, detail=exc.as_detail()) from exc
    except SearchInputError as exc:
        raise HTTPException(400, detail=exc.as_detail()) from exc
    except SearchProjectNotFound as exc:
        raise HTTPException(404, detail=exc.as_detail()) from exc
    except SearchError as exc:
        raise HTTPException(500, detail=exc.as_detail()) from exc
