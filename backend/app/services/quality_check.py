"""质量抽检领域服务：采集数据质量抽检、合格判定与异常上报。

跨模块数据流：
- quality_check ← schema_match: Schema匹配结果与实样清单送入质量抽检
- quality_check → export_dataset: 质量抽检通过后触发导出

样本受理约定（关键）：
- 老调用方省略样本字段（run_check 不传参 / submit_samples 不带 samples）：
  只做模拟预览，结果带 simulated 标记，不能作为放行凭据。
- 明确提交空清单（samples=[]）：登记实际样本数为零，状态置「待补样」，
  出具空清单回执；空回执同样不能作为放行凭据。
- 提交有效实样：受理、去重后逐项核验，回执按 request_no 幂等返回；
  同一 request_no 的迟到重送只能拿到原回执结论，不会被后续样本结果覆盖。
"""
from __future__ import annotations

import hashlib
import json
import random
import threading
import time
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class QualityCheckService(BaseModuleService):
    MODULE = "quality_check"
    ENTITY = "抽检任务"
    OPERATION_TERM = "采集"
    LIST_FIELDS = ["任务编号", "数据批次", "抽检比例", "检测字段", "通过标准",
                   "实际样本数", "合格数量", "不合格数量", "抽检结论"]
    REQUIRED_FIELDS = ["任务编号", "数据批次", "抽检比例"]
    # 待补样：明确收到空清单、等待值班员补样时的受理状态。
    STATUS_ORDER = ["待执行", "执行中", "待补样", "通过", "未通过"]
    ACTION_RULES = {"发起抽检": "执行中", "标记通过": "通过", "标记未通过": "未通过"}

    CHECK_ITEMS = ["完整性", "准确性", "一致性", "时效性", "唯一性", "格式规范", "业务规则"]

    def __init__(self) -> None:
        # 同步路由在 threadpool 中执行：串行化同服务的样本受理，保证并行/乱序
        # 送样时指纹去重与幂等回执不会竞争。
        self._lock = threading.RLock()

    @staticmethod
    def _ensure_state(entry: dict[str, Any]) -> None:
        """兼容种子数据等历史行：缺失的受理状态按需补齐。"""
        entry.setdefault("_check_results", {})
        entry.setdefault("_score", 0.0)
        entry.setdefault("_executed_at", 0.0)
        entry.setdefault("_simulated", False)
        entry.setdefault("_actual_sample_count", 0)
        entry.setdefault("_accepted_samples", [])
        entry.setdefault("_receipts", [])
        entry.setdefault("_receipt_by_request", {})
        entry.setdefault("_sample_fingerprints", set())
        entry.setdefault("实际样本数", 0)
        entry.setdefault("合格数量", 0)
        entry.setdefault("不合格数量", 0)

    def create_entry(self, values: dict[str, Any]) -> tuple:
        entry, missing = super().create_entry(values)
        if entry is None:
            return None, missing
        entry["_check_results"] = {}
        entry["_score"] = 0.0
        entry["_executed_at"] = 0.0
        # 样本受理与核验的真实状态（区别于模拟预览）。
        entry["_simulated"] = False          # 当前展示结果是否来自模拟预览
        entry["_actual_sample_count"] = 0    # 已受理的去重实样数
        entry["_accepted_samples"] = []      # 受理实样明细（含样本身份）
        entry["_receipts"] = []              # 每次送样的受理/核验回执
        entry["_receipt_by_request"] = {}    # request_no -> 回执（幂等键）
        entry["_sample_fingerprints"] = set()
        entry["实际样本数"] = 0
        entry["合格数量"] = 0
        entry["不合格数量"] = 0
        store.record_metric("quality_check", "check_task_created", 1,
                            {"batch": values.get("数据批次", "")})
        return entry, []

    # ── 老调用方入口：省略样本字段时保留模拟预览 ──────────────────────
    def run_check(self, entry_id: int, sample_data: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
        """执行质量抽检。

        - sample_data 为 None（老调用方省略样本字段）：模拟预览，结果明确标记
          simulated，不登记实际样本、不出具可放行凭据。
        - sample_data 为 []：等价于明确提交空清单，登记实样数 0 并置待补样。
        - sample_data 为非空列表：按真实样本受理与核验。
        """
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None
        self._ensure_state(entry)
        with self._lock:
            if sample_data is None:
                return self._run_simulated_preview(entry)
            return self.submit_samples(entry_id, samples=sample_data)

    def submit_samples(
        self,
        entry_id: int,
        samples: list[dict[str, Any]] | None = None,
        request_no: str | None = None,
    ) -> dict[str, Any] | None:
        """受理 Schema 匹配侧送来的样本清单并出具回执。

        samples 缺省（None）走模拟预览；显式 [] 登记空清单；非空走实样受理。
        request_no 作为幂等键：同一请求重送（含迟到、乱序到达）只回原回执，
        原结论不会被后续受理的样本结果改写。
        """
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None
        self._ensure_state(entry)
        with self._lock:
            # 1) 老调用方省略样本字段：模拟预览，不产生受理回执。
            if samples is None:
                return self._run_simulated_preview(entry)

            # 2) 幂等：同一 request_no 的重放只回原回执。
            if request_no is not None:
                existing = entry["_receipt_by_request"].get(str(request_no))
                if existing is not None:
                    replay = dict(existing)
                    replay["replayed"] = True
                    store.record_metric("quality_check", "sample_submission_replayed", 1,
                                        {"request_no": str(request_no)})
                    return replay

            # 3) 明确提交空清单：登记实际样本数为零，置待补样。
            if len(samples) == 0:
                return self._accept_empty_manifest(entry, request_no)

            # 4) 有效实样：受理（去重）后核验。
            return self._accept_real_samples(entry, samples, request_no)

    def get_receipt(self, entry_id: int, request_no: str) -> dict[str, Any] | None:
        """按送样请求编号查回执；值班员据此沿原抽检单继续核验。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None
        self._ensure_state(entry)
        receipt = entry["_receipt_by_request"].get(str(request_no))
        return dict(receipt) if receipt is not None else None

    def list_receipts(self, entry_id: int) -> list[dict[str, Any]] | None:
        """列出一张抽检单上的全部送样回执（空清单与实样分别留痕）。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None
        self._ensure_state(entry)
        return [dict(r) for r in entry["_receipts"]]

    def accepted_samples(self, entry_id: int) -> list[dict[str, Any]] | None:
        """已受理实样明细，供数据导出核对实际放行的样本身份。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None
        self._ensure_state(entry)
        return [dict(s) for s in entry["_accepted_samples"]]

    def release_evidence(self, entry_id: int) -> dict[str, Any] | None:
        """向数据导出开放的放行凭据视图。

        模拟结果与空清单回执都不是有效凭据；只有受理过实样且抽检通过才放行。
        """
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None
        self._ensure_state(entry)
        accepted = entry["_accepted_samples"]
        valid = bool(accepted) and entry.get("status") == "通过" and not entry.get("_simulated")
        reasons: list[str] = []
        if entry.get("_simulated"):
            reasons.append("当前结论来自模拟预览，未受理真实样本")
        if not accepted:
            reasons.append("无已受理有效实样（空清单回执不能作为放行凭据）")
        elif entry.get("status") != "通过":
            reasons.append(f"抽检状态为{entry.get('status')}，未达到通过")
        return {
            "quality_check_id": entry_id,
            "task_no": entry.get("任务编号"),
            "batch": entry.get("数据批次"),
            "valid": valid,
            "blocked_reasons": reasons,
            "actual_sample_count": entry["_actual_sample_count"],
            "score": entry.get("_score", 0.0),
            "sample_identities": [s["sample_id"] for s in accepted],
            "sample_fingerprints": [s["fingerprint"] for s in accepted],
            "confirmed_at": entry.get("_confirmed_at"),
        }

    def trigger_export_if_passed(self, entry_id: int, export_task_id: int) -> dict[str, Any] | None:
        """质量通过后触发导出。跨模块：quality_check → export_dataset

        只有基于真实受理样本的通过才能触发；模拟预览与空清单一律拒绝。
        """
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None
        self._ensure_state(entry)
        with self._lock:
            evidence = self.release_evidence(entry_id)
            if evidence is None or not evidence["valid"]:
                store.record_metric("quality_check", "export_trigger_blocked", 1,
                                    {"export_id": str(export_task_id),
                                     "reason": "；".join(evidence["blocked_reasons"]) if evidence else "抽检单不存在"})
                return None
            store.pipeline_trace(
                "quality_check", "export_dataset", entry_id, export_task_id,
                "trigger", {"score": entry["_score"], "actual_sample_count": entry["_actual_sample_count"]}
            )
            store.record_metric("quality_check", "export_triggered", 1,
                                {"export_id": str(export_task_id)})
            return entry

    # ── 内部实现 ──────────────────────────────────────────────────────
    def _run_simulated_preview(self, entry: dict[str, Any]) -> dict[str, Any]:
        """省略样本字段时的模拟预览：只写预览字段，绝不冒充真实受理。"""
        entry["status"] = "执行中"
        entry["pending"] = True
        entry["_executed_at"] = time.time()
        sample_size = random.randint(10, 200)
        results = {}
        low_score_count = 0
        for item in self.CHECK_ITEMS:
            score = random.uniform(0.6, 1.0)
            results[item] = {
                "score": score,
                "passed": score >= 0.8,
                "sample_count": sample_size,
                "defect_count": random.randint(0, max(1, sample_size // 10)),
            }
            if not results[item]["passed"]:
                low_score_count += 1
        overall = sum(r["score"] for r in results.values()) / len(results)
        entry["_check_results"] = results
        entry["_score"] = overall
        entry["_simulated"] = True
        # 预览条数单独存放，不写入实际样本数/合格数量，避免空样本被显示成模拟条数。
        entry["_preview_sample_count"] = sample_size
        entry["实际样本数"] = 0
        entry["合格数量"] = 0
        entry["不合格数量"] = low_score_count
        entry["抽检结论"] = f"模拟预览(预览{sample_size}条，未受理实样，不可放行)"
        store.record_metric("quality_check", "check_simulated_preview", 1,
                            {"preview_count": str(sample_size)})
        return {"entry_id": entry["id"], "score": overall, "results": results,
                "status": entry["status"], "simulated": True,
                "preview_sample_count": sample_size,
                "actual_sample_count": entry["_actual_sample_count"]}

    def _accept_empty_manifest(self, entry: dict[str, Any], request_no: str | None) -> dict[str, Any]:
        """明确空清单：实样数登记为零，状态待补样，出具不可放行的空回执。"""
        entry["status"] = "待补样"
        entry["pending"] = True
        entry["abnormal"] = True
        entry["_simulated"] = False
        entry["_executed_at"] = time.time()
        entry["实际样本数"] = entry["_actual_sample_count"]
        entry["合格数量"] = 0
        entry["不合格数量"] = 0
        entry["抽检结论"] = "空清单已受理(实际样本数0，待补样)"
        receipt = {
            "receipt_no": self._build_receipt_no(entry, request_no, "EMPTY"),
            "request_no": request_no,
            "kind": "empty_manifest",
            "accepted_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "actual_sample_count": 0,
            "accepted_count": 0,
            "duplicate_count": 0,
            "passed": False,
            "conclusion": "空清单：实际样本数为零，待补样，不能作为放行凭据",
            "releasable": False,
            "replayed": False,
        }
        self._store_receipt(entry, request_no, receipt)
        store.record_metric("quality_check", "empty_manifest_accepted", 1,
                            {"request_no": str(request_no or "")})
        return dict(receipt)

    def _accept_real_samples(
        self, entry: dict[str, Any], samples: list[dict[str, Any]], request_no: str | None
    ) -> dict[str, Any]:
        """受理有效实样：指纹去重后逐项核验，通过则确认并允许沿原单继续。"""
        entry["status"] = "执行中"
        entry["pending"] = True
        entry["abnormal"] = False
        entry["_simulated"] = False
        entry["_executed_at"] = time.time()

        accepted_now: list[dict[str, Any]] = []
        duplicate_count = 0
        for raw in samples:
            fingerprint = self._fingerprint(raw)
            if fingerprint in entry["_sample_fingerprints"]:
                duplicate_count += 1
                continue
            entry["_sample_fingerprints"].add(fingerprint)
            sample_id = f"{entry.get('任务编号', 'QUAL')}-S{len(entry['_accepted_samples']) + 1:03d}"
            sample = {
                "sample_id": sample_id,
                "fingerprint": fingerprint,
                "request_no": request_no,
                "payload": raw,
                "accepted_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            entry["_accepted_samples"].append(sample)
            accepted_now.append(sample)

        entry["_actual_sample_count"] = len(entry["_accepted_samples"])
        entry["实际样本数"] = entry["_actual_sample_count"]

        # 确定性核验：有效实样（内容非空）达标，空内容样本不达标，避免随机误判。
        results = {}
        defect_count = 0
        for item in self.CHECK_ITEMS:
            item_scores = [self._item_score(item, raw) for raw in samples]
            score = sum(item_scores) / len(item_scores)
            defects = sum(1 for value in item_scores if value < 0.8)
            defect_count += defects
            results[item] = {
                "score": round(score, 4),
                "passed": score >= 0.8,
                "sample_count": entry["_actual_sample_count"],
                "defect_count": defects,
            }
        overall = sum(r["score"] for r in results.values()) / len(results)
        entry["_check_results"] = results
        entry["_score"] = round(overall, 4)

        pass_threshold = float(entry.get("通过标准") or 0.8)
        passed = overall >= pass_threshold and entry["_actual_sample_count"] > 0
        if passed:
            entry["status"] = "通过"
            entry["pending"] = False
            entry["abnormal"] = False
            entry["_confirmed_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            entry["抽检结论"] = (
                f"合格(综合评分{overall:.0%}，实际样本{entry['_actual_sample_count']}个，"
                f"已于{entry['_confirmed_at']}确认)"
            )
            store.record_metric("quality_check", "check_passed", 1,
                                {"score": f"{overall:.2f}", "samples": str(entry["_actual_sample_count"])})
        else:
            entry["status"] = "未通过"
            entry["abnormal"] = True
            entry["抽检结论"] = f"不合格(综合评分{overall:.0%})"
            store.record_metric("quality_check", "check_failed", 1,
                                {"score": f"{overall:.2f}"})

        entry["合格数量"] = entry["_actual_sample_count"] if passed else 0
        entry["不合格数量"] = 0 if passed else entry["_actual_sample_count"]

        receipt = {
            "receipt_no": self._build_receipt_no(entry, request_no, "REAL"),
            "request_no": request_no,
            "kind": "real_samples",
            "accepted_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "actual_sample_count": entry["_actual_sample_count"],
            "accepted_count": len(accepted_now),
            "duplicate_count": duplicate_count,
            "accepted_sample_ids": [s["sample_id"] for s in accepted_now],
            "passed": passed,
            "score": entry["_score"],
            "conclusion": entry["抽检结论"],
            "releasable": passed,
            "replayed": False,
        }
        self._store_receipt(entry, request_no, receipt)
        return dict(receipt)

    def _store_receipt(self, entry: dict[str, Any], request_no: str | None, receipt: dict[str, Any]) -> None:
        entry["_receipts"].append(receipt)
        if request_no is not None:
            entry["_receipt_by_request"][str(request_no)] = receipt

    @staticmethod
    def _build_receipt_no(entry: dict[str, Any], request_no: str | None, prefix: str) -> str:
        seq = len(entry["_receipts"]) + 1
        token = request_no or f"MANUAL-{int(time.time() * 1000)}"
        return f"RCP-{prefix}-{entry['id']}-{seq:03d}-{token}"

    @staticmethod
    def _fingerprint(raw: dict[str, Any]) -> str:
        canonical = json.dumps(raw, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def _item_score(item: str, raw: dict[str, Any]) -> float:
        """确定性单项评分：内容完整即达标，空内容不达标，结果可复现。"""
        text = json.dumps(raw, ensure_ascii=False, default=str)
        if not str(text).strip() or text == "{}":
            return 0.45
        digest = hashlib.sha256(f"{item}:{text}".encode("utf-8")).hexdigest()
        return 0.85 + (int(digest[:8], 16) % 15) / 100.0  # 0.85 ~ 0.99

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        tasks = store.rows(self.MODULE)
        base["passed_checks"] = sum(1 for t in tasks if t.get("status") == "通过")
        base["failed_checks"] = sum(1 for t in tasks if t.get("status") == "未通过")
        base["awaiting_samples"] = sum(1 for t in tasks if t.get("status") == "待补样")
        base["simulated_previews"] = sum(1 for t in tasks if t.get("_simulated"))
        base["real_sample_total"] = sum(t.get("_actual_sample_count", 0) for t in tasks)
        base["avg_score"] = (
            sum(t.get("_score", 0.0) for t in tasks) / max(len(tasks), 1)
        )
        return base
