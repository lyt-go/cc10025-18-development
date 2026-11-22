<template>
  <section class="page" data-module="schema_rollback">
    <header class="page-head">
      <div>
        <h2>Schema回滚</h2>
        <p class="page-desc">
          上线回滚前导出快照片段与依赖字段关系，打包成可下载的 Schema 回滚包；
          发布人员导入回滚包生成审批，用候选方案试读实际从表后再提交确认。
        </p>
      </div>
    </header>

    <div class="stat-row">
      <article class="stat-card">
        <span class="stat-label">回滚审批总数</span>
        <strong class="stat-value">{{ approvals.length }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">待确认</span>
        <strong class="stat-value">{{ approvals.filter((a) => a.status === '待确认').length }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">已拦截</span>
        <strong class="stat-value">{{ approvals.filter((a) => a.status === '已拦截').length }}</strong>
      </article>
    </div>

    <section class="panel">
      <h3>生成 Schema 回滚包</h3>
      <form class="filter-bar" @submit.prevent="generatePackage">
        <label class="filter-item">
          <span>主表资产</span>
          <input v-model="assetId" data-testid="rollback-asset" placeholder="如 DWD_ORDER_MASTER" />
        </label>
        <button class="btn primary" data-testid="build-package" type="submit" :disabled="building">
          {{ building ? '生成中…' : '生成回滚包' }}
        </button>
        <button class="btn" data-testid="download-package" type="button" :disabled="!pkg" @click="downloadPackage">
          下载回滚包文件
        </button>
        <button class="btn" data-testid="import-package" type="button" :disabled="!pkg" @click="importCurrent">
          导入该回滚包
        </button>
        <label class="btn ghost file-btn">
          从文件导入
          <input type="file" accept=".json" data-testid="import-file" @change="importFromFile" />
        </label>
      </form>

      <table v-if="pkg" class="data-table" data-testid="package-fields">
        <thead>
          <tr>
            <th>目标字段</th>
            <th>旧列名</th>
            <th>新列名</th>
            <th>对应快照</th>
            <th>同值依据</th>
            <th>可执行目标</th>
            <th>受影响从表</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="field in pkg.fields" :key="field.field">
            <td>{{ field.field }}</td>
            <td>{{ field.old_name }}</td>
            <td>{{ field.new_name }}</td>
            <td>{{ field.snapshot_id }}{{ field.snapshot_verified ? '' : '（缺失）' }}</td>
            <td>{{ field.same_value_evidence ? '有（可复查）' : '无' }}</td>
            <td>{{ field.executable ? '是' : '否' }}</td>
            <td>{{ field.affected_tables.join('、') || '—' }}</td>
          </tr>
        </tbody>
      </table>
    </section>

    <section class="panel">
      <h3>回滚审批</h3>
      <table class="data-table">
        <thead>
          <tr>
            <th>审批编号</th>
            <th>关联资产</th>
            <th>目标字段</th>
            <th>受影响表</th>
            <th>数据版本</th>
            <th>试读结果</th>
            <th>当前状态</th>
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in approvals" :key="String(row.id)" :data-testid="`approval-${row.id}`" :data-status="String(row.status)">
            <td>{{ row['审批编号'] }}</td>
            <td>{{ row['关联资产'] }}</td>
            <td>{{ row['目标字段'] }}</td>
            <td>{{ row['受影响表'] }}</td>
            <td>{{ row['数据版本'] || '—' }}</td>
            <td>{{ row['试读结果'] || '—' }}</td>
            <td>
              {{ row.status }}
              <span v-if="row['拦截原因']" class="error-text block">{{ row['拦截原因'] }}</span>
            </td>
            <td class="row-actions">
              <button
                v-if="row.status === '待试读' || row.status === '待确认'"
                class="link"
                type="button"
                :data-testid="`trial-${row.id}`"
                @click="trialRead(Number(row.id))"
              >
                试读从表
              </button>
              <button
                v-if="row.status === '待确认'"
                class="link"
                type="button"
                :data-testid="`submit-${row.id}`"
                @click="submitApproval(Number(row.id))"
              >
                提交审批
              </button>
            </td>
          </tr>
          <tr v-if="!approvals.length">
            <td colspan="8" class="empty-state">暂无回滚审批，请先生成并导入 Schema 回滚包</td>
          </tr>
        </tbody>
      </table>

      <div v-if="trialDetail.length" class="panel" data-testid="trial-detail">
        <h3>试读返回结果（数据版本 {{ trialDetail[0]?.data_version }}）</h3>
        <table class="data-table">
          <thead>
            <tr>
              <th>从表</th>
              <th>请求列</th>
              <th>保留当前列名</th>
              <th>返回行数</th>
              <th>实际返回的值</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="item in trialDetail" :key="item.table">
              <td>{{ item.table }}</td>
              <td>{{ item.requested_columns.join('、') }}</td>
              <td>{{ item.retained_current_names ? '是' : `否（缺失 ${item.missing_columns.join('、')}）` }}</td>
              <td>{{ item.row_count }}</td>
              <td><code>{{ JSON.stringify(item.returned_rows) }}</code></td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <footer class="page-foot">
      <span>共 {{ approvals.length }} 条回滚审批</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
      <span v-else-if="notice" class="notice-text">{{ notice }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import {
  buildRollbackPackage,
  downloadRollbackPackage,
  importRollbackPackage,
  listRollbackApprovals,
  submitRollbackApproval,
  trialReadApproval,
} from '@/api/backend-service'
import type { RollbackApproval, RollbackPackage, TrialReadResult } from '@/data/types'

const assetId = ref('DWD_ORDER_MASTER')
const building = ref(false)
const pkg = ref<RollbackPackage | null>(null)
const approvals = ref<RollbackApproval[]>([])
const trialDetail = ref<TrialReadResult[]>([])
const errorMessage = ref('')
const notice = ref('')

async function generatePackage() {
  errorMessage.value = ''
  notice.value = ''
  building.value = true
  try {
    pkg.value = await buildRollbackPackage(assetId.value.trim())
    notice.value = `回滚包 ${pkg.value.package_id} 已生成`
  } catch (error) {
    pkg.value = null
    errorMessage.value = error instanceof Error ? error.message : '回滚包生成失败'
  } finally {
    building.value = false
  }
}

async function downloadPackage() {
  errorMessage.value = ''
  try {
    await downloadRollbackPackage(assetId.value.trim())
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '回滚包下载失败'
  }
}

async function importCurrent() {
  if (!pkg.value) return
  await doImport(pkg.value)
}

async function importFromFile(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  try {
    const parsed = JSON.parse(await file.text()) as RollbackPackage
    await doImport(parsed)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '回滚包文件解析失败'
  } finally {
    input.value = ''
  }
}

async function doImport(pack: RollbackPackage) {
  errorMessage.value = ''
  notice.value = ''
  try {
    const result = await importRollbackPackage(pack)
    if (!result.ok) throw new Error(result.message)
    notice.value = result.message
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '回滚包导入失败'
  }
}

async function trialRead(id: number) {
  errorMessage.value = ''
  notice.value = ''
  try {
    const result = await trialReadApproval(id)
    if (!result.ok) throw new Error(result.message)
    notice.value = result.message
    trialDetail.value = (result.entry?.trial_results as TrialReadResult[] | undefined) ?? []
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '试读失败'
  }
}

async function submitApproval(id: number) {
  errorMessage.value = ''
  notice.value = ''
  try {
    const result = await submitRollbackApproval(id)
    if (!result.ok) throw new Error(result.message)
    notice.value = result.message
    trialDetail.value = []
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '提交失败'
    await reload()
  }
}

async function reload() {
  try {
    const payload = await listRollbackApprovals()
    approvals.value = payload.items
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '回滚审批读取失败'
  }
}

onMounted(reload)
</script>

<style scoped>
.panel {
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 12px;
  margin-bottom: 12px;
}
.panel h3 {
  margin: 0 0 10px;
  font-size: 14px;
}
.file-btn {
  display: inline-flex;
  align-items: center;
  cursor: pointer;
}
.file-btn input[type='file'] {
  display: none;
}
.block {
  display: block;
  font-size: 12px;
}
.notice-text {
  color: #067647;
}
code {
  font-size: 12px;
  white-space: pre-wrap;
}
</style>
