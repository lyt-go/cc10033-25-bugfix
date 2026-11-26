/**
 * 质量抽检领域规则（前端镜像）。
 *
 * 与后端 app/services/quality_check.py 的受理语义保持一致：
 * - 省略样本字段只是模拟预览，不能作为放行凭据；
 * - 显式空清单登记实际样本数 0，状态为「待补样」；
 * - 只有受理过有效实样且抽检通过才具备放行凭据。
 */
import type { EntryRow } from '@/data/types'
import type { SampleReceipt } from '@/api/backend-service'

/** 明确空清单后的受理状态。 */
export const AWAITING_SAMPLE_STATUS = '待补样'

/** 模拟预览（老调用方省略样本字段）。 */
export function isSimulatedPreview(entry: EntryRow): boolean {
  return entry._simulated === true
}

/** 空清单已受理、等待值班员补样。 */
export function isAwaitingSamples(entry: EntryRow): boolean {
  return entry.status === AWAITING_SAMPLE_STATUS
}

/** 已受理的去重实样数；模拟预览与空清单都为 0。 */
export function actualSampleCount(entry: EntryRow): number {
  return Number(entry._actual_sample_count ?? entry['实际样本数'] ?? 0)
}

/** 是否具备数据导出放行凭据：真实实样 + 抽检通过 + 非模拟。 */
export function hasReleaseEvidence(entry: EntryRow): boolean {
  return (
    !isSimulatedPreview(entry) &&
    entry.status === '通过' &&
    actualSampleCount(entry) > 0
  )
}

/** 列表/详情页统一的样本状态文案。 */
export function sampleStateText(entry: EntryRow): string {
  if (isAwaitingSamples(entry)) return '待补样（空清单已受理，实际样本数 0）'
  if (isSimulatedPreview(entry)) {
    return `模拟预览（预览 ${entry._preview_sample_count ?? 0} 条，未受理实样，不可放行）`
  }
  if (hasReleaseEvidence(entry)) return `已受理实样 ${actualSampleCount(entry)} 个，抽检通过可放行`
  if (actualSampleCount(entry) > 0) return `已受理实样 ${actualSampleCount(entry)} 个，待核验`
  return '尚未受理样本'
}

export function isEmptyManifestReceipt(receipt: SampleReceipt): boolean {
  return receipt.kind === 'empty_manifest'
}

export function isRealSampleReceipt(receipt: SampleReceipt): boolean {
  return receipt.kind === 'real_samples'
}

/** 同一请求重送时后端只回原回执，前端据此提示「结论未变更」。 */
export function isReplayedReceipt(receipt: SampleReceipt): boolean {
  return receipt.replayed === true
}

/** 空回执永远不能作为放行凭据，即使后来的实样已通过。 */
export function receiptReleasable(receipt: SampleReceipt): boolean {
  return receipt.releasable === true && receipt.kind === 'real_samples'
}
