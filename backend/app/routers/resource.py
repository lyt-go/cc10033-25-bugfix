"""统一资源接口：查询、导出、登记和动作按模块 key 分派到领域服务。"""
from __future__ import annotations

import csv
import io
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.schemas import (
    ActionPayload,
    ActionResult,
    EntryPayload,
    PageResult,
    ReleaseRequestPayload,
    SampleSubmissionPayload,
)
from app.services import service_for
from app.services.export_dataset import ExportDatasetService
from app.services.quality_check import QualityCheckService

router = APIRouter(prefix="/api", tags=["业务资源"])


def require_service(module: str):
    service = service_for(module)
    if service is None:
        raise HTTPException(status_code=404, detail=f"未知业务模块：{module}")
    return service


@router.get("/{module}", response_model=PageResult[dict])
def list_entries(
    module: str,
    request: Request,
    keyword: str | None = Query(default=None),
    status: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> PageResult[dict]:
    service = require_service(module)
    filters = {
        key: value.strip()
        for key, value in request.query_params.items()
        if key in service.LIST_FIELDS and value.strip()
    }
    items, total = service.list_entries(
        filters=filters, keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/{module}/export")
def export_entries(
    module: str,
    request: Request,
    keyword: str | None = Query(default=None),
    status: str | None = Query(default=None),
) -> StreamingResponse:
    service = require_service(module)
    filters = {
        key: value.strip()
        for key, value in request.query_params.items()
        if key in service.LIST_FIELDS and value.strip()
    }
    items, _ = service.list_entries(
        filters=filters, keyword=keyword, status=status, page=1, size=1_000_000)
    output = io.StringIO()
    output.write("\ufeff")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["id", *service.LIST_FIELDS, "status"])
    for item in items:
        values = [item.get("id"), *(item.get(field, "") for field in service.LIST_FIELDS),
                  item.get("status", "")]
        writer.writerow([
            "'" + str(value) if str(value or "").startswith(("=", "+", "-", "@")) else value
            for value in values
        ])
    return StreamingResponse(
        iter([output.getvalue()]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{module}.csv"'},
    )


@router.get("/{module}/{entry_id}", response_model=dict)
def get_entry(module: str, entry_id: int) -> dict[str, Any]:
    service = require_service(module)
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"{service.ENTITY} {entry_id} 不存在或已归档")
    return entry


@router.post("/{module}", response_model=ActionResult)
def create_entry(module: str, payload: EntryPayload) -> ActionResult:
    service = require_service(module)
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message=f"{service.ENTITY}已登记", entry=entry)


@router.post("/{module}/{entry_id}/actions", response_model=ActionResult)
def run_action(module: str, entry_id: int, payload: ActionPayload) -> ActionResult:
    service = require_service(module)
    entry, message = service.run_action(entry_id, payload.action.strip())
    return ActionResult(ok=entry is not None, message=message, entry=entry)


# ── 质量抽检：样本清单受理（空清单 / 实样 / 模拟预览） ──────────────────
@router.post("/quality_check/{entry_id}/samples")
def submit_samples(entry_id: int, payload: SampleSubmissionPayload) -> dict[str, Any]:
    """受理 Schema 匹配侧送来的样本清单。

    省略 samples 字段为老调用方模拟预览；samples=[] 登记空清单（实样数 0、待补样）；
    非空清单按 request_no 幂等受理，重送只回原回执。
    """
    service = service_for("quality_check")
    if not isinstance(service, QualityCheckService):
        raise HTTPException(status_code=404, detail="样本受理仅支持质量抽检模块")
    receipt = service.submit_samples(entry_id, samples=payload.samples,
                                     request_no=payload.request_no)
    if receipt is None:
        raise HTTPException(status_code=404, detail=f"抽检任务 {entry_id} 不存在或已归档")
    return {"ok": True, "message": "样本清单已受理", "receipt": receipt}


@router.get("/quality_check/{entry_id}/receipts")
def list_receipts(entry_id: int, request_no: str | None = Query(default=None)) -> dict[str, Any]:
    """查询抽检单上的送样回执；带 request_no 时只回该请求的原回执结论。"""
    service = service_for("quality_check")
    if not isinstance(service, QualityCheckService):
        raise HTTPException(status_code=404, detail="回执查询仅支持质量抽检模块")
    if service.get_entry(entry_id) is None:
        raise HTTPException(status_code=404, detail=f"抽检任务 {entry_id} 不存在或已归档")
    if request_no is not None:
        receipt = service.get_receipt(entry_id, request_no)
        if receipt is None:
            raise HTTPException(status_code=404, detail=f"请求 {request_no} 暂无回执")
        return {"ok": True, "receipt": receipt}
    return {"ok": True, "items": service.list_receipts(entry_id)}


@router.get("/quality_check/{entry_id}/samples")
def list_accepted_samples(entry_id: int) -> dict[str, Any]:
    """查询已受理实样明细，供值班员沿原抽检单继续核验。"""
    service = service_for("quality_check")
    if not isinstance(service, QualityCheckService):
        raise HTTPException(status_code=404, detail="样本查询仅支持质量抽检模块")
    samples = service.accepted_samples(entry_id)
    if samples is None:
        raise HTTPException(status_code=404, detail=f"抽检任务 {entry_id} 不存在或已归档")
    evidence = service.release_evidence(entry_id)
    return {"ok": True, "actual_sample_count": len(samples),
            "items": samples, "evidence": evidence}


# ── 数据导出：真实放行凭据校验 ─────────────────────────────────────────
@router.post("/export_dataset/{entry_id}/release")
def request_release(entry_id: int, payload: ReleaseRequestPayload) -> dict[str, Any]:
    """凭质量抽检单申请放行；模拟结果与空清单回执一律阻断。"""
    service = service_for("export_dataset")
    if not isinstance(service, ExportDatasetService):
        raise HTTPException(status_code=404, detail="放行申请仅支持数据导出模块")
    if service.get_entry(entry_id) is None:
        raise HTTPException(status_code=404, detail=f"导出任务 {entry_id} 不存在或已归档")
    return service.request_release(entry_id, payload.quality_check_id)


@router.get("/export_dataset/{entry_id}/release")
def release_detail(entry_id: int) -> dict[str, Any]:
    """导出详情：实际采用的样本身份、抽检确认与阻断原因。"""
    service = service_for("export_dataset")
    if not isinstance(service, ExportDatasetService):
        raise HTTPException(status_code=404, detail="放行详情仅支持数据导出模块")
    detail = service.release_detail(entry_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"导出任务 {entry_id} 不存在或已归档")
    return {"ok": True, "detail": detail}
