"""Schema 回滚专用接口：回滚包导出下载、回滚审批导入、从表试读与提交确认。"""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.schemas import ActionResult
from app.services import service_for

router = APIRouter(prefix="/api/rollback", tags=["Schema回滚"])


class ImportPayload(BaseModel):
    package: dict[str, Any] = Field(default_factory=dict)


def _snapshot_service():
    service = service_for("version_snapshot")
    if service is None:
        raise HTTPException(status_code=404, detail="版本快照模块未就绪")
    return service


def _registry_service():
    service = service_for("schema_registry")
    if service is None:
        raise HTTPException(status_code=404, detail="Schema注册模块未就绪")
    return service


def _public(entry: dict[str, Any] | None) -> dict[str, Any] | None:
    """审批条目对外输出时去掉内部计算字段。"""
    if entry is None:
        return None
    return {k: v for k, v in entry.items() if not k.startswith("_")}


@router.get("/package/{asset_id}")
def build_rollback_package(asset_id: str, download: bool = False) -> Any:
    """导出某主表资产的 Schema 回滚包（快照片段 + 依赖字段关系）。"""
    package, message = _snapshot_service().build_rollback_package(asset_id)
    if package is None:
        raise HTTPException(status_code=404, detail=message)
    if download:
        return Response(
            content=json.dumps(package, ensure_ascii=False, indent=2),
            media_type="application/json; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="schema_rollback_{asset_id}.json"'
            },
        )
    return package


@router.post("/import", response_model=ActionResult)
def import_rollback_package(payload: ImportPayload) -> ActionResult:
    """Schema 注册从 Schema 回滚包读取目标字段和受影响的数据表，生成回滚审批。"""
    entry, message = _registry_service().import_rollback_package(payload.package)
    return ActionResult(ok=entry is not None, message=message, entry=_public(entry))


@router.get("/approvals")
def list_rollback_approvals() -> dict[str, Any]:
    items = _registry_service().list_rollback_approvals()
    return {"items": items, "total": len(items)}


@router.post("/approvals/{approval_id}/trial-read", response_model=ActionResult)
def trial_read_approval(approval_id: int) -> ActionResult:
    """用候选方案试读实际从表，审批记录试读所用的数据版本和返回结果。"""
    entry, message, done = _registry_service().trial_read_rollback(approval_id)
    public = _public(entry)
    if public is not None and entry.get("_trial_results"):
        public["trial_results"] = entry["_trial_results"]
    return ActionResult(ok=done, message=message, entry=public)


@router.post("/approvals/{approval_id}/submit", response_model=ActionResult)
def submit_approval(approval_id: int) -> ActionResult:
    """提交回滚审批；确认之前识别已变更的依赖，无依据或依赖漂移的旧方案一律拦截。"""
    entry, message, confirmed = _registry_service().submit_rollback_approval(approval_id)
    return ActionResult(ok=confirmed, message=message, entry=_public(entry))
