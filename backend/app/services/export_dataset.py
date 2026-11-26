"""数据导出领域服务：采集数据批量导出、格式转换与交付路径。

跨模块数据流：
- export_dataset ← quality_check: 质量抽检通过后触发导出

放行凭据约定（关键）：
- execute_export 的老调用方路径只产生模拟导出，结果带 simulated 标记且
  「放行状态=模拟不可放行」，不会生成可启动的真实交付。
- request_release 必须指回一张质量抽检单，并以其 release_evidence 为唯一凭据：
  模拟预览、空清单回执、未受理实样或抽检未通过一律阻断，阻断原因留痕。
- 真实放行后记录实际采用的样本身份（sample_id/指纹），导出详情可逐项核对。
"""
from __future__ import annotations

import random
import threading
import time
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class ExportDatasetService(BaseModuleService):
    MODULE = "export_dataset"
    ENTITY = "导出任务"
    OPERATION_TERM = "采集"
    LIST_FIELDS = ["任务编号", "数据来源", "导出格式", "字段筛选", "行数范围",
                   "压缩方式", "文件路径", "导出时间", "放行状态"]
    REQUIRED_FIELDS = ["任务编号", "数据来源", "导出格式"]
    STATUS_ORDER = ["待执行", "导出中", "已完成", "已失败"]
    ACTION_RULES = {"发起导出": "导出中", "标记完成": "已完成", "标记失败": "已失败"}

    SUPPORTED_FORMATS = ["CSV", "JSON", "Parquet", "Excel", "XML", "Avro"]
    COMPRESSION = ["none", "gzip", "snappy", "lz4", "zstd"]

    def __init__(self) -> None:
        # 放行申请可能并发到达：串行化导出状态变更，保证交付与凭据一一对应。
        self._lock = threading.RLock()

    @staticmethod
    def _ensure_state(entry: dict[str, Any]) -> None:
        """兼容种子数据等历史行：缺失的放行状态按需补齐。"""
        entry.setdefault("_export_progress", 0.0)
        entry.setdefault("_exported_rows", 0)
        entry.setdefault("_export_errors", [])
        entry.setdefault("_start_ts", 0)
        entry.setdefault("_simulated", False)
        entry.setdefault("_releasable", False)
        entry.setdefault("_released", False)
        entry.setdefault("_release_blocks", [])
        entry.setdefault("_release_evidence", None)
        entry.setdefault("_released_sample_ids", [])
        entry.setdefault("_released_fingerprints", [])
        entry.setdefault("放行状态", "未申请放行")

    def create_entry(self, values: dict[str, Any]) -> tuple:
        entry, missing = super().create_entry(values)
        if entry is None:
            return None, missing
        entry["_export_progress"] = 0.0
        entry["_exported_rows"] = 0
        entry["_export_errors"] = []
        entry["_start_ts"] = 0
        # 放行凭据与真实交付状态。
        entry["_simulated"] = False
        entry["_releasable"] = False
        entry["_released"] = False
        entry["_release_blocks"] = []
        entry["_release_evidence"] = None
        entry["_released_sample_ids"] = []
        entry["_released_fingerprints"] = []
        entry["放行状态"] = "未申请放行"
        store.record_metric("export_dataset", "export_task_created", 1,
                            {"format": values.get("导出格式", ""),
                             "source": values.get("数据来源", "")})
        return entry, []

    def execute_export(self, entry_id: int, data: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
        """执行数据导出（老调用方路径：模拟导出，不得作为放行凭据）。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None
        self._ensure_state(entry)
        with self._lock:
            entry["status"] = "导出中"
            entry["_start_ts"] = time.time()
            fmt = entry.get("导出格式", "CSV")
            if fmt not in self.SUPPORTED_FORMATS:
                entry["status"] = "已失败"
                entry["_export_errors"].append(f"不支持的格式：{fmt}")
                entry["abnormal"] = True
                return entry
            # 模拟导出：行数来自模拟值，明确标记不可放行，避免多出一笔可启动交付。
            self._simulate_export()
            row_count = data if isinstance(data, int) else random.randint(100, 100000)
            entry["_exported_rows"] = row_count if isinstance(row_count, int) else len(data) if data else 0
            entry["行数范围"] = f"1-{entry['_exported_rows']}"
            entry["文件路径"] = f"/exports/{entry.get('任务编号', 'TASK')}.{fmt.lower()}"
            compression = entry.get("压缩方式", "none")
            if compression != "none":
                entry["文件路径"] += f".{compression}"
            entry["导出时间"] = time.strftime("%Y-%m-%d %H:%M:%S")
            entry["status"] = "已完成"
            entry["pending"] = False
            entry["_export_progress"] = 100.0
            entry["_simulated"] = True
            entry["_releasable"] = False
            entry["_released"] = False
            entry["放行状态"] = "模拟不可放行"
            block = "模拟导出无真实抽检凭据，不能作为放行凭据"
            if block not in entry["_release_blocks"]:
                entry["_release_blocks"].append(block)
            store.record_metric("export_dataset", "export_completed_simulated", entry["_exported_rows"],
                                {"format": fmt, "compression": compression})
            return entry

    def request_release(self, entry_id: int, quality_check_id: int) -> dict[str, Any]:
        """凭质量抽检单申请真实放行；凭据无效时阻断并留痕，不产生交付。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "releasable": False,
                    "message": f"导出任务 {entry_id} 不存在或已归档"}
        self._ensure_state(entry)
        with self._lock:
            from app.services import service_for
            qc = service_for("quality_check")
            evidence = qc.release_evidence(quality_check_id) if qc is not None else None
            if evidence is None:
                return self._block_release(entry, f"质量抽检单 {quality_check_id} 不存在")
            if not evidence.get("valid"):
                return self._block_release(entry, "；".join(evidence.get("blocked_reasons") or ["凭据无效"]),
                                           evidence=evidence)

            fmt = entry.get("导出格式", "CSV")
            if fmt not in self.SUPPORTED_FORMATS:
                return self._block_release(entry, f"不支持的导出格式：{fmt}", evidence=evidence)

            # 凭据有效：执行真实交付，样本身份与抽检确认一并固化到导出详情。
            entry["status"] = "导出中"
            entry["_start_ts"] = time.time()
            self._simulate_export()
            sample_count = int(evidence.get("actual_sample_count", 0))
            entry["_exported_rows"] = sample_count
            entry["行数范围"] = f"1-{sample_count}"
            entry["文件路径"] = (
                f"/exports/{entry.get('任务编号', 'TASK')}.{fmt.lower()}"
                + (f".{entry.get('压缩方式', 'none')}" if entry.get("压缩方式", "none") != "none" else "")
            )
            entry["导出时间"] = time.strftime("%Y-%m-%d %H:%M:%S")
            entry["status"] = "已完成"
            entry["pending"] = False
            entry["abnormal"] = False
            entry["_export_progress"] = 100.0
            entry["_simulated"] = False
            entry["_releasable"] = True
            entry["_released"] = True
            entry["_release_evidence"] = evidence
            entry["_released_sample_ids"] = list(evidence.get("sample_identities", []))
            entry["_released_fingerprints"] = list(evidence.get("sample_fingerprints", []))
            entry["放行状态"] = f"已放行({sample_count}个实样)"
            store.pipeline_trace(
                "quality_check", "export_dataset", quality_check_id, entry_id,
                "release", {"actual_sample_count": sample_count,
                            "sample_ids": entry["_released_sample_ids"]}
            )
            store.record_metric("export_dataset", "release_granted", sample_count,
                                {"quality_check_id": str(quality_check_id)})
        return {
            "ok": True,
            "releasable": True,
            "message": f"已凭抽检单 {evidence.get('task_no')} 放行 {sample_count} 个实样",
            "release_detail": self.release_detail(entry_id),
        }

    def release_detail(self, entry_id: int) -> dict[str, Any] | None:
        """导出详情：核对实际采用的样本、确认信息与阻断原因。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None
        self._ensure_state(entry)
        return {
            "export_id": entry_id,
            "task_no": entry.get("任务编号"),
            "status": entry.get("status"),
            "simulated": entry.get("_simulated", False),
            "releasable": entry.get("_releasable", False),
            "released": entry.get("_released", False),
            "release_status_text": entry.get("放行状态"),
            "exported_rows": entry.get("_exported_rows", 0),
            "blocked_reasons": list(entry.get("_release_blocks", [])),
            "evidence": entry.get("_release_evidence"),
            "released_sample_ids": list(entry.get("_released_sample_ids", [])),
            "released_fingerprints": list(entry.get("_released_fingerprints", [])),
            "file_path": entry.get("文件路径"),
            "exported_at": entry.get("导出时间"),
        }

    def export_with_quality_check(self, entry_id: int, quality_check_id: int,
                                  passed: bool) -> dict[str, Any] | None:
        """质量抽检联动导出。跨模块：quality_check → export_dataset

        老签名保留：显式 passed=False 仍置失败；passed=True 时改走真实放行
        凭据校验，模拟通过/空清单无法再启动交付。
        """
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None
        self._ensure_state(entry)
        if not passed:
            entry["status"] = "已失败"
            entry["abnormal"] = True
            return entry
        result = self.request_release(entry_id, quality_check_id)
        if not result.get("ok"):
            # 凭据不足：任务保持待执行并记录阻断，不生成可启动交付。
            entry["status"] = "待执行"
            entry["pending"] = True
        store.pipeline_trace(
            "quality_check", "export_dataset", quality_check_id, entry_id,
            "quality_passed", {"action": "request_release", "ok": bool(result.get("ok"))}
        )
        return entry

    def format_to_bytes(self, entry_id: int) -> dict[str, Any] | None:
        """模拟格式转换，返回文件大小。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None
        self._ensure_state(entry)
        sizes = {"CSV": 1.2, "JSON": 1.5, "Parquet": 0.6, "Excel": 2.0, "XML": 2.5, "Avro": 0.4}
        row_factor = entry.get("_exported_rows", 1000) / 1000
        size_mb = sizes.get(entry.get("导出格式", "CSV"), 1.0) * row_factor
        return {"format": entry.get("导出格式"), "rows": entry.get("_exported_rows"),
                "size_mb": round(size_mb, 2), "compression": entry.get("压缩方式", "none")}

    def _block_release(self, entry: dict[str, Any], reason: str,
                       evidence: dict[str, Any] | None = None) -> dict[str, Any]:
        entry["_releasable"] = False
        entry["_released"] = False
        entry["放行状态"] = "放行阻断"
        entry["_release_evidence"] = evidence
        if reason not in entry["_release_blocks"]:
            entry["_release_blocks"].append(reason)
        store.record_metric("export_dataset", "release_blocked", 1, {"reason": reason})
        return {"ok": False, "releasable": False, "message": f"放行阻断：{reason}",
                "release_detail": self.release_detail(entry["id"])}

    def _simulate_export(self) -> None:
        time.sleep(random.uniform(0.05, 0.2))

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        tasks = store.rows(self.MODULE)
        base["completed_exports"] = sum(1 for t in tasks if t.get("status") == "已完成")
        base["released_exports"] = sum(1 for t in tasks if t.get("_released"))
        base["blocked_exports"] = sum(1 for t in tasks if t.get("放行状态") == "放行阻断")
        base["simulated_exports"] = sum(1 for t in tasks if t.get("_simulated"))
        base["total_rows_exported"] = sum(t.get("_exported_rows", 0) for t in tasks)
        return base
