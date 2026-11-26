"""通用接口契约；模块字段由领域服务持有，不再为每个模块复制一份模型类。"""
from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class PageResult(BaseModel, Generic[T]):
    items: list[T]
    total: int
    page: int = 1
    size: int = 20


class ActionResult(BaseModel):
    ok: bool
    message: str
    entry: dict[str, Any] | None = None


class EntryPayload(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict)
    remark: str | None = None


class ActionPayload(BaseModel):
    action: str
    remark: str | None = None


class SampleSubmissionPayload(BaseModel):
    """Schema匹配侧送来的样本清单；samples 缺省与显式空清单语义不同。

    - 不带 samples 字段：老调用方的模拟预览。
    - samples=[]：明确提交空清单，登记实际样本数为零。
    - request_no：送样请求编号，作为幂等键，重送同一请求只回原回执。
    """

    samples: list[dict[str, Any]] | None = None
    request_no: str | None = None
    remark: str | None = None


class ReleaseRequestPayload(BaseModel):
    """数据导出放行请求，必须指回一张质量抽检单作为凭据来源。"""

    quality_check_id: int
