import { request } from './client'
import { moduleMeta } from '@/data/modules'
import type { ActionResult, EntryRow, OverviewResult, PageResult } from '@/data/types'

export { moduleMeta }

export async function listEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<PageResult> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}?${query}`)
  if (!response.ok) throw new Error('业务数据读取失败')
  return await response.json()
}

export async function createEntry(
  key: string,
  values: Record<string, string>,
): Promise<ActionResult> {
  const response = await request(`/api/${key}`, {
    method: 'POST',
    body: JSON.stringify({ values }),
  })
  return await response.json()
}

export async function runAction(
  key: string,
  id: number,
  action: string,
): Promise<ActionResult> {
  const response = await request(`/api/${key}/${id}/actions`, {
    method: 'POST',
    body: JSON.stringify({ action }),
  })
  return await response.json()
}

export async function downloadEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<void> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}/export?${query}`)
  if (!response.ok) throw new Error('业务数据导出失败')
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${key}.csv`
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}

export async function loadOverview(): Promise<OverviewResult> {
  const response = await request('/api/overview')
  if (!response.ok) throw new Error('运营概览读取失败')
  return await response.json()
}

// ── 质量抽检：样本受理（空清单 / 实样 / 模拟预览）与回执查询 ─────────────

export type SampleReceipt = {
  receipt_no: string
  request_no: string | null
  kind: 'empty_manifest' | 'real_samples'
  accepted_at: string
  actual_sample_count: number
  accepted_count: number
  duplicate_count: number
  passed: boolean
  releasable: boolean
  replayed: boolean
  conclusion: string
  accepted_sample_ids?: string[]
  score?: number
}

export async function submitSamples(
  entryId: number,
  payload: { samples?: Record<string, unknown>[] | null; request_no?: string },
): Promise<{ ok: boolean; message: string; receipt: SampleReceipt }> {
  const response = await request(`/api/quality_check/${entryId}/samples`, {
    method: 'POST',
    body: JSON.stringify(payload),
  })
  if (!response.ok) throw new Error('样本清单受理失败')
  return await response.json()
}

export async function listReceipts(
  entryId: number,
  requestNo?: string,
): Promise<{ ok: boolean; items?: SampleReceipt[]; receipt?: SampleReceipt }> {
  const query = requestNo ? `?request_no=${encodeURIComponent(requestNo)}` : ''
  const response = await request(`/api/quality_check/${entryId}/receipts${query}`)
  if (!response.ok) throw new Error('送样回执查询失败')
  return await response.json()
}

export type AcceptedSample = {
  sample_id: string
  fingerprint: string
  request_no: string | null
  payload: Record<string, unknown>
  accepted_at: string
}

export async function listAcceptedSamples(
  entryId: number,
): Promise<{ ok: boolean; actual_sample_count: number; items: AcceptedSample[]; evidence: unknown }> {
  const response = await request(`/api/quality_check/${entryId}/samples`)
  if (!response.ok) throw new Error('已受理样本查询失败')
  return await response.json()
}

// ── 数据导出：真实放行凭据校验与导出详情 ────────────────────────────────

export type ReleaseResult = {
  ok: boolean
  releasable: boolean
  message: string
  release_detail?: Record<string, unknown>
}

export async function requestRelease(
  entryId: number,
  qualityCheckId: number,
): Promise<ReleaseResult> {
  const response = await request(`/api/export_dataset/${entryId}/release`, {
    method: 'POST',
    body: JSON.stringify({ quality_check_id: qualityCheckId }),
  })
  if (!response.ok) throw new Error('放行申请失败')
  return await response.json()
}

export async function getReleaseDetail(
  entryId: number,
): Promise<{ ok: boolean; detail: Record<string, unknown> }> {
  const response = await request(`/api/export_dataset/${entryId}/release`)
  if (!response.ok) throw new Error('放行详情读取失败')
  return await response.json()
}
