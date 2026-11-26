"""空样本受理与放行凭据端到端验收。

场景（单订单数据批次）：
1. 老调用方省略样本字段 → 仅模拟预览，不登记实样、不能放行。
2. 明确提交第一份空清单 → 实际样本数登记为 0、状态待补样、出空回执。
3. 实样在途未受理时空清单迟到重试（乱序到达）→ 只回原空清单结论。
4. 值班员从原抽检单登记补样，回执可沿原领取继续核验，不必等后续申请；
   此期间数据导出因无有效凭据仍不能放行。
5. 收到有效实样并通过抽检 → 导出详情可核对实际采用的样本与确认信息。
6. 空请求重送只回原空清单结论，不能把新样本的通过结果记到旧请求。
7. 并行再送一次相同有效实样 → 去重后真实样本数不变、放行身份固定、空单仍保留。
"""
from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from app.schemas import ReleaseRequestPayload, SampleSubmissionPayload
from app.store import store
from app.services import SERVICES
from app.routers import resource


@pytest.fixture()
def batch_context():
    """每个用例独立的一张抽检单 + 一张导出任务，避免共享内存仓库串数据。"""
    qc = SERVICES["quality_check"]
    ed = SERVICES["export_dataset"]
    suffix = len(store.rows("quality_check")) + len(store.rows("export_dataset"))
    check, _ = qc.create_entry({
        "任务编号": f"ACCEPT-QUAL-{suffix}",
        "数据批次": f"order_batch_{suffix}",
        "抽检比例": "100%",
        "通过标准": "0.8",
        "检测字段": "title,price",
    })
    export, _ = ed.create_entry({
        "任务编号": f"ACCEPT-EXP-{suffix}",
        "数据来源": f"order_batch_{suffix}",
        "导出格式": "CSV",
        "压缩方式": "none",
    })
    return {"qc": qc, "ed": ed, "check_id": check["id"], "export_id": export["id"],
            "batch": f"order_batch_{suffix}"}


def real_samples(mark: str = "A") -> list[dict]:
    return [
        {"sample_ref": f"{mark}-1", "title": f"商品{mark}1", "price": "19.90",
         "content": f"完整实样内容{mark}-1，字段齐全可核验"},
        {"sample_ref": f"{mark}-2", "title": f"商品{mark}2", "price": "88.00",
         "content": f"完整实样内容{mark}-2，字段齐全可核验"},
        {"sample_ref": f"{mark}-3", "title": f"商品{mark}3", "price": "126.50",
         "content": f"完整实样内容{mark}-3，字段齐全可核验"},
    ]


# ═══════════════════════════════════════════════════════════════
# 1. 老调用方省略样本字段：模拟预览保留但不冒充真实受理
# ═══════════════════════════════════════════════════════════════
class TestSimulatedPreviewLegacyCaller:
    def test_run_check_without_samples_is_only_preview(self, batch_context):
        qc = batch_context["qc"]
        cid = batch_context["check_id"]
        result = qc.run_check(cid)  # 老调用方：不传样本
        assert result["simulated"] is True
        assert result["status"] == "执行中"
        entry = qc.get_entry(cid)
        assert entry["_simulated"] is True
        # 关键修复：模拟条数不得显示成实际样本数
        assert entry["_actual_sample_count"] == 0
        assert entry["实际样本数"] == 0
        assert entry["_preview_sample_count"] >= 10
        assert "模拟预览" in entry["抽检结论"]
        # 模拟预览不是放行凭据
        evidence = qc.release_evidence(cid)
        assert evidence["valid"] is False
        assert any("模拟预览" in r for r in evidence["blocked_reasons"])

    def test_submit_samples_without_field_is_preview_too(self, batch_context):
        qc = batch_context["qc"]
        cid = batch_context["check_id"]
        receipt = qc.submit_samples(cid)  # samples 缺省
        assert receipt["simulated"] is True
        assert qc.get_entry(cid)["_actual_sample_count"] == 0
        assert qc.list_receipts(cid) == []  # 预览不出具受理回执


# ═══════════════════════════════════════════════════════════════
# 2. 明确空清单：实样数 0、待补样、空回执不可放行
# ═══════════════════════════════════════════════════════════════
class TestEmptyManifestAcceptance:
    def test_empty_manifest_registers_zero_and_awaits_samples(self, batch_context):
        qc = batch_context["qc"]
        cid = batch_context["check_id"]
        receipt = qc.submit_samples(cid, samples=[], request_no="REQ-EMPTY-1")
        assert receipt["kind"] == "empty_manifest"
        assert receipt["actual_sample_count"] == 0
        assert receipt["releasable"] is False
        entry = qc.get_entry(cid)
        assert entry["status"] == "待补样"
        assert entry["abnormal"] is True
        assert entry["实际样本数"] == 0
        assert entry["合格数量"] == 0
        assert "待补样" in entry["抽检结论"]
        assert entry["_simulated"] is False  # 空清单是真实受理，不是模拟

    def test_empty_receipt_cannot_release_export(self, batch_context):
        qc, ed = batch_context["qc"], batch_context["ed"]
        cid, eid = batch_context["check_id"], batch_context["export_id"]
        qc.submit_samples(cid, samples=[], request_no="REQ-EMPTY-1")

        result = ed.request_release(eid, cid)
        assert result["ok"] is False
        assert result["releasable"] is False
        detail = ed.release_detail(eid)
        assert detail["released"] is False
        assert detail["release_status_text"] == "放行阻断"
        assert any("空清单" in r or "实样" in r for r in detail["blocked_reasons"])
        # 阻断不产生可启动交付：任务仍在待执行
        assert ed.get_entry(eid)["status"] == "待执行"

    def test_simulated_export_cannot_become_delivery(self, batch_context):
        ed = batch_context["ed"]
        eid = batch_context["export_id"]
        ed.execute_export(eid)  # 老路径模拟导出
        detail = ed.release_detail(eid)
        assert detail["simulated"] is True
        assert detail["releasable"] is False
        assert detail["release_status_text"] == "模拟不可放行"
        assert any("模拟" in r for r in detail["blocked_reasons"])


# ═══════════════════════════════════════════════════════════════
# 3+6. 空清单迟到重试与幂等：只回原结论，新结果不记到旧请求
# ═══════════════════════════════════════════════════════════════
class TestEmptyManifestRetryIdempotency:
    def test_late_retry_returns_original_empty_conclusion(self, batch_context):
        qc = batch_context["qc"]
        cid = batch_context["check_id"]
        first = qc.submit_samples(cid, samples=[], request_no="REQ-EMPTY-1")

        # 实样在途、尚未受理：第一份空清单的迟到重试抵达（接收顺序不同于发送顺序）
        late = qc.submit_samples(cid, samples=[], request_no="REQ-EMPTY-1")
        assert late["replayed"] is True
        assert late["receipt_no"] == first["receipt_no"]
        assert late["kind"] == "empty_manifest"
        assert late["actual_sample_count"] == 0
        # 重放不重复登记回执
        assert len(qc.list_receipts(cid)) == 1

    def test_replay_keeps_empty_conclusion_after_real_samples_pass(self, batch_context):
        """旧空请求重送不能把新样本的通过结果记到旧请求上。"""
        qc = batch_context["qc"]
        cid = batch_context["check_id"]
        empty = qc.submit_samples(cid, samples=[], request_no="REQ-EMPTY-1")

        real = qc.submit_samples(cid, samples=real_samples("A"), request_no="REQ-REAL-1")
        assert real["passed"] is True
        assert qc.get_entry(cid)["status"] == "通过"

        replay = qc.submit_samples(cid, samples=[], request_no="REQ-EMPTY-1")
        assert replay["replayed"] is True
        assert replay["receipt_no"] == empty["receipt_no"]
        assert replay["kind"] == "empty_manifest"
        assert replay["releasable"] is False  # 旧请求结论保持空清单
        assert replay["passed"] is False
        # 空单仍保留在回执列表里，与实样回执并存
        kinds = [r["kind"] for r in qc.list_receipts(cid)]
        assert kinds == ["empty_manifest", "real_samples"]

    def test_get_receipt_returns_frozen_original(self, batch_context):
        qc = batch_context["qc"]
        cid = batch_context["check_id"]
        qc.submit_samples(cid, samples=[], request_no="REQ-EMPTY-1")
        qc.submit_samples(cid, samples=real_samples("A"), request_no="REQ-REAL-1")
        old = qc.get_receipt(cid, "REQ-EMPTY-1")
        assert old["kind"] == "empty_manifest"
        assert old["replayed"] is False  # 查询原回执不带重放标记
        new = qc.get_receipt(cid, "REQ-REAL-1")
        assert new["kind"] == "real_samples"
        assert new["passed"] is True


# ═══════════════════════════════════════════════════════════════
# 4. 值班员沿原抽检单登记补样；在途期间导出持续阻断
# ═══════════════════════════════════════════════════════════════
class TestReplenishOnOriginalCheckSheet:
    def test_replenish_continues_same_sheet_and_export_stays_blocked(self, batch_context):
        qc, ed = batch_context["qc"], batch_context["ed"]
        cid, eid = batch_context["check_id"], batch_context["export_id"]
        qc.submit_samples(cid, samples=[], request_no="REQ-EMPTY-1")

        # 实样在途未受理期间，任何放行尝试都被阻断且原因可读
        blocked = ed.request_release(eid, cid)
        assert blocked["ok"] is False
        reason_text = blocked["message"]
        assert "空清单" in reason_text or "实样" in reason_text

        # 值班员从原抽检单登记补样（不是新建抽检单/新申请）
        receipt = qc.submit_samples(cid, samples=real_samples("B"), request_no="REQ-SUPP-1")
        assert receipt["kind"] == "real_samples"
        # 同一张单上可查到空单与补样两笔受理，沿原领取继续核验
        receipts = qc.list_receipts(cid)
        assert [r["request_no"] for r in receipts] == ["REQ-EMPTY-1", "REQ-SUPP-1"]
        accepted = qc.accepted_samples(cid)
        assert len(accepted) == 3
        assert all(s["sample_id"].startswith(qc.get_entry(cid)["任务编号"]) for s in accepted)

        # 补样受理并通过后才允许放行
        evidence = qc.release_evidence(cid)
        assert evidence["valid"] is True
        assert evidence["actual_sample_count"] == 3
        assert evidence["blocked_reasons"] == []


# ═══════════════════════════════════════════════════════════════
# 5. 有效实样通过后：导出详情可核对样本身份与确认
# ═══════════════════════════════════════════════════════════════
class TestRealSampleRelease:
    def test_release_records_adopted_sample_identities(self, batch_context):
        qc, ed = batch_context["qc"], batch_context["ed"]
        cid, eid = batch_context["check_id"], batch_context["export_id"]
        qc.submit_samples(cid, samples=real_samples("C"), request_no="REQ-REAL-1")
        result = ed.request_release(eid, cid)
        assert result["ok"] is True
        detail = ed.release_detail(eid)
        assert detail["released"] is True
        assert detail["simulated"] is False
        assert detail["exported_rows"] == 3
        assert len(detail["released_sample_ids"]) == 3
        assert len(detail["released_fingerprints"]) == 3
        # 放行样本身份与抽检单受理身份逐一对齐
        accepted = qc.accepted_samples(cid)
        assert detail["released_sample_ids"] == [s["sample_id"] for s in accepted]
        assert detail["released_fingerprints"] == [s["fingerprint"] for s in accepted]
        # 确认信息可核对
        evidence = detail["evidence"]
        assert evidence["task_no"] == qc.get_entry(cid)["任务编号"]
        assert evidence["confirmed_at"] is not None
        assert qc.get_entry(cid)["_confirmed_at"] == evidence["confirmed_at"]

    def test_trigger_export_requires_real_evidence(self, batch_context):
        qc = batch_context["qc"]
        cid = batch_context["check_id"]
        qc.run_check(cid)  # 仅模拟预览
        assert qc.trigger_export_if_passed(cid, 999) is None
        qc.submit_samples(cid, samples=real_samples("D"), request_no="REQ-REAL-1")
        assert qc.trigger_export_if_passed(cid, 999) is not None
        assert any(t["name"] == "export_triggered" for t in store.metrics("quality_check"))


# ═══════════════════════════════════════════════════════════════
# 7. 并行重送相同有效实样：去重、身份固定、空单保留
# ═══════════════════════════════════════════════════════════════
class TestParallelDuplicateRealSamples:
    def test_parallel_identical_samples_dedup(self, batch_context):
        qc, ed = batch_context["qc"], batch_context["ed"]
        cid, eid = batch_context["check_id"], batch_context["export_id"]
        qc.submit_samples(cid, samples=[], request_no="REQ-EMPTY-1")
        first = qc.submit_samples(cid, samples=real_samples("E"), request_no="REQ-REAL-1")
        assert first["accepted_count"] == 3

        # 并行再送一次相同的有效实样（新请求号、内容完全相同）
        second = qc.submit_samples(cid, samples=real_samples("E"), request_no="REQ-REAL-2")
        assert second["accepted_count"] == 0       # 全部判重
        assert second["duplicate_count"] == 3
        assert second["passed"] is True            # 抽检结论仍为通过
        entry = qc.get_entry(cid)
        assert entry["_actual_sample_count"] == 3  # 真实样本数不膨胀
        assert entry["实际样本数"] == 3
        # 放行的样本身份固定为首次受理的三个
        ed.request_release(eid, cid)
        detail = ed.release_detail(eid)
        assert detail["released_sample_ids"] == first["accepted_sample_ids"]
        assert detail["exported_rows"] == 3
        # 空单仍保留
        kinds = [r["kind"] for r in qc.list_receipts(cid)]
        assert "empty_manifest" in kinds

    def test_replay_real_request_returns_original_receipt(self, batch_context):
        qc = batch_context["qc"]
        cid = batch_context["check_id"]
        first = qc.submit_samples(cid, samples=real_samples("F"), request_no="REQ-REAL-9")
        replay = qc.submit_samples(cid, samples=real_samples("F"), request_no="REQ-REAL-9")
        assert replay["replayed"] is True
        assert replay["receipt_no"] == first["receipt_no"]
        assert replay["accepted_sample_ids"] == first["accepted_sample_ids"]
        assert qc.get_entry(cid)["_actual_sample_count"] == 3

    def test_concurrent_identical_submissions_stay_deduped(self, batch_context):
        """多线程并行送相同实样：锁与指纹去重保证真实样本数不膨胀。"""
        import threading

        qc, ed = batch_context["qc"], batch_context["ed"]
        cid, eid = batch_context["check_id"], batch_context["export_id"]
        qc.submit_samples(cid, samples=[], request_no="REQ-EMPTY-1")
        errors: list[BaseException] = []

        def worker(seq: int) -> None:
            try:
                qc.submit_samples(cid, samples=real_samples("E"), request_no=f"REQ-PAR-{seq}")
            except BaseException as exc:  # noqa: BLE001 - 并发用例需要汇总线程异常
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        entry = qc.get_entry(cid)
        assert entry["_actual_sample_count"] == 3
        assert len(entry["_accepted_samples"]) == 3
        # 八个并行请求各自留回执，空单仍保留，放行身份固定
        assert len(qc.list_receipts(cid)) == 9
        result = ed.request_release(eid, cid)
        assert result["ok"] is True
        assert ed.release_detail(eid)["exported_rows"] == 3
        assert any(r["kind"] == "empty_manifest" for r in qc.list_receipts(cid))


# ═══════════════════════════════════════════════════════════════
# HTTP 路由层：受理/回执/放行接口契约
# ═══════════════════════════════════════════════════════════════
class TestHttpRoutes:
    def test_sample_and_release_routes(self, batch_context):
        qc, ed = batch_context["qc"], batch_context["ed"]
        cid, eid = batch_context["check_id"], batch_context["export_id"]

        # 空清单受理
        resp = resource.submit_samples(cid, SampleSubmissionPayload(samples=[], request_no="HTTP-1"))
        assert resp["ok"] is True
        assert resp["receipt"]["kind"] == "empty_manifest"

        # 空清单期间放行被阻断
        blocked = resource.request_release(eid, ReleaseRequestPayload(quality_check_id=cid))
        assert blocked["ok"] is False

        # 实样受理
        resource.submit_samples(cid, SampleSubmissionPayload(samples=real_samples("G"), request_no="HTTP-2"))
        granted = resource.request_release(eid, ReleaseRequestPayload(quality_check_id=cid))
        assert granted["ok"] is True
        assert len(granted["release_detail"]["released_sample_ids"]) == 3

        # 回执查询：旧空请求结论冻结
        receipts = resource.list_receipts(cid, request_no=None)
        assert len(receipts["items"]) == 2
        old = resource.list_receipts(cid, request_no="HTTP-1")
        assert old["receipt"]["kind"] == "empty_manifest"

        # 已受理样本明细与放行详情
        samples_resp = resource.list_accepted_samples(cid)
        assert samples_resp["actual_sample_count"] == 3
        detail = resource.release_detail(eid)
        assert detail["detail"]["released"] is True

    def test_routes_reject_unknown_entries(self, batch_context):
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc:
            resource.submit_samples(999999, SampleSubmissionPayload(samples=[]))
        assert exc.value.status_code == 404
        with pytest.raises(HTTPException) as exc:
            resource.request_release(999999, ReleaseRequestPayload(quality_check_id=1))
        assert exc.value.status_code == 404
