import { request } from './client'
import { moduleMeta } from '@/data/modules'
import type { ActionResult, EntryRow, OverviewResult, PageResult, RollbackApproval, RollbackPackage } from '@/data/types'

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

// ---------- Schema 回滚包 ----------

export async function buildRollbackPackage(assetId: string): Promise<RollbackPackage> {
  const response = await request(`/api/rollback/package/${encodeURIComponent(assetId)}`)
  if (!response.ok) {
    const detail = await response.json().catch(() => null)
    throw new Error(detail?.detail ?? '回滚包生成失败')
  }
  return await response.json()
}

export async function downloadRollbackPackage(assetId: string): Promise<void> {
  const response = await request(`/api/rollback/package/${encodeURIComponent(assetId)}?download=1`)
  if (!response.ok) throw new Error('回滚包下载失败')
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `schema_rollback_${assetId}.json`
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}

export async function importRollbackPackage(pkg: RollbackPackage): Promise<ActionResult> {
  const response = await request('/api/rollback/import', {
    method: 'POST',
    body: JSON.stringify({ package: pkg }),
  })
  return await response.json()
}

export async function listRollbackApprovals(): Promise<{ items: RollbackApproval[]; total: number }> {
  const response = await request('/api/rollback/approvals')
  if (!response.ok) throw new Error('回滚审批读取失败')
  return await response.json()
}

export async function trialReadApproval(id: number): Promise<ActionResult> {
  const response = await request(`/api/rollback/approvals/${id}/trial-read`, { method: 'POST' })
  return await response.json()
}

export async function submitRollbackApproval(id: number): Promise<ActionResult> {
  const response = await request(`/api/rollback/approvals/${id}/submit`, { method: 'POST' })
  return await response.json()
}
