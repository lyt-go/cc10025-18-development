/** 纯前端数据层的公共类型：与全栈版后端返回的结构保持一致，换回后端时页面不用改。 */

export type EntryRow = {
  id: number
  status: string
  pending: boolean
  abnormal: boolean
  [field: string]: string | number | boolean
}

export type ModuleMeta = {
  key: string
  name: string
  entity: string
  desc: string
  fields: string[]
  statuses: string[]
  actions: string[]
  actionTargets: Record<string, string>
  metrics: string[]
}

export type PageResult = {
  items: EntryRow[]
  total: number
  page: number
  size: number
}

export type ActionResult = {
  ok: boolean
  message: string
  entry?: Record<string, unknown>
}

export type RollbackField = {
  field: string
  old_name: string
  new_name: string
  snapshot_id: string
  snapshot_verified: boolean
  same_value_evidence: Record<string, unknown> | null
  executable: boolean
  affected_tables: string[]
}

export type RollbackPackage = {
  package_id: string
  package_type: string
  generated_at: string
  asset_id: string
  table: string
  snapshots: Record<string, unknown>[]
  fields: RollbackField[]
}

export type TrialReadResult = {
  table: string
  requested_columns: string[]
  retained_current_names: boolean
  missing_columns: string[]
  data_version: string
  row_count: number
  returned_rows: Record<string, unknown>[]
}

export type RollbackApproval = EntryRow & {
  trial_results?: TrialReadResult[]
}

export type OverviewResult = {
  cards: { label: string; value: number }[]
  modules: { name: string; created: number; pending: number; abnormal: number }[]
}
