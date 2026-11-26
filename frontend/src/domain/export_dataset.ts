/**
 * 数据导出领域规则（前端镜像）。
 *
 * 与后端 app/services/export_dataset.py 的放行门禁保持一致：
 * - 老路径 execute_export 只产生模拟导出（模拟不可放行）；
 * - 模拟结果、空清单回执、未受理实样、抽检未通过一律阻断；
 * - 真实放行后导出详情保留实际采用的样本身份与确认信息。
 */
import type { EntryRow } from '@/data/types'

export const RELEASE_BLOCKED_TEXT = '放行阻断'
export const RELEASE_SIMULATED_TEXT = '模拟不可放行'
export const RELEASE_NOT_REQUESTED_TEXT = '未申请放行'

export function isSimulatedExport(entry: EntryRow): boolean {
  return entry._simulated === true
}

export function isReleased(entry: EntryRow): boolean {
  return entry._released === true
}

export function isReleaseBlocked(entry: EntryRow): boolean {
  return entry['放行状态'] === RELEASE_BLOCKED_TEXT
}

/** 为什么此刻仍不能放行：直接复用后端留痕的阻断原因。 */
export function blockedReasons(entry: EntryRow): string[] {
  const reasons = entry._release_blocks
  return Array.isArray(reasons) ? reasons.map(String) : []
}

/** 实际放行的样本身份（sample_id），未放行时为空数组。 */
export function releasedSampleIds(entry: EntryRow): string[] {
  const ids = entry._released_sample_ids
  return Array.isArray(ids) ? ids.map(String) : []
}

/** 列表页统一的放行状态文案。 */
export function releaseGateText(entry: EntryRow): string {
  if (isReleased(entry)) return String(entry['放行状态'] ?? '已放行')
  if (isSimulatedExport(entry)) return RELEASE_SIMULATED_TEXT
  if (isReleaseBlocked(entry)) {
    const reasons = blockedReasons(entry)
    return reasons.length ? `${RELEASE_BLOCKED_TEXT}：${reasons[0]}` : RELEASE_BLOCKED_TEXT
  }
  return RELEASE_NOT_REQUESTED_TEXT
}
