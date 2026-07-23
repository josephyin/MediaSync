<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { api, type Page } from '../api/client'
import type { CloudFile, Subscription } from '../api/types'
import AppIcon from '../components/AppIcon.vue'
import { formatDateTime, formatFileSize, formatRelativeTime, statusLabel, statusType } from '../utils/display'

const files = ref<CloudFile[]>([])
const subscriptions = ref<Subscription[]>([])
const status = ref('')
const query = ref('')
const subscriptionId = ref<number | ''>('')
const page = ref(1)
const pageSize = ref(20)
const total = ref(0)
const loading = ref(false)
const retryingId = ref<number | null>(null)

function subscriptionName(id: number) {
  return subscriptions.value.find((item) => item.id === id)?.name ?? `订阅 #${id}`
}
async function load() {
  loading.value = true
  try {
    const params = new URLSearchParams({ page: String(page.value), page_size: String(pageSize.value) })
    if (status.value) params.set('status', status.value)
    if (query.value.trim()) params.set('query', query.value.trim())
    if (subscriptionId.value) params.set('subscription_id', String(subscriptionId.value))
    const result = await api<Page<CloudFile>>(`/files?${params}`)
    files.value = result.items
    total.value = result.total
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '文件记录加载失败')
  } finally {
    loading.value = false
  }
}
function applyFilters() { page.value = 1; void load() }
function clearFilters() {
  query.value = ''
  status.value = ''
  subscriptionId.value = ''
  applyFilters()
}
async function retry(id: number) {
  retryingId.value = id
  try {
    await api(`/files/${id}/retry`, { method: 'POST' })
    ElMessage.success('已加入重试队列')
    await load()
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '重试提交失败')
  } finally {
    retryingId.value = null
  }
}
onMounted(async () => {
  try { subscriptions.value = (await api<Page<Subscription>>('/subscriptions?page_size=100')).items }
  catch { /* 文件列表仍可独立使用 */ }
  await load()
})
</script>

<template>
  <section>
    <div class="page-heading">
      <div><h1>文件记录</h1><p>追踪每个文件从发现到转存完成的全过程</p></div>
      <el-button :loading="loading" @click="load"><AppIcon name="refresh" />刷新</el-button>
    </div>

    <div class="section-card">
      <div class="toolbar">
        <div class="toolbar-group file-filters">
          <el-input v-model="query" clearable placeholder="搜索文件名或路径" @keyup.enter="applyFilters">
            <template #prefix><AppIcon name="search" /></template>
          </el-input>
          <el-select v-model="subscriptionId" clearable filterable placeholder="全部订阅" @change="applyFilters">
            <el-option v-for="item in subscriptions" :key="item.id" :label="item.name" :value="item.id" />
          </el-select>
          <el-select v-model="status" clearable placeholder="全部状态" @change="applyFilters">
            <el-option label="待处理" value="pending" />
            <el-option label="转存中" value="saving" />
            <el-option label="已转存" value="saved" />
            <el-option label="失败" value="failed" />
          </el-select>
          <el-button type="primary" plain @click="applyFilters">查询</el-button>
        </div>
        <el-button text @click="clearFilters">重置筛选</el-button>
      </div>

      <div class="table-wrap">
        <el-table v-loading="loading" :data="files" empty-text=" ">
          <el-table-column label="文件" min-width="300">
            <template #default="scope">
              <div class="file-cell">
                <span class="file-icon"><AppIcon name="files" /></span>
                <div><strong class="cell-primary">{{ scope.row.filename }}</strong><span>{{ scope.row.relative_path }}</span></div>
              </div>
            </template>
          </el-table-column>
          <el-table-column label="所属订阅" min-width="150"><template #default="scope">{{ subscriptionName(scope.row.subscription_id) }}</template></el-table-column>
          <el-table-column label="大小" width="110"><template #default="scope">{{ formatFileSize(scope.row.size) }}</template></el-table-column>
          <el-table-column label="状态" width="110"><template #default="scope"><el-tag :type="statusType(scope.row.status)">{{ statusLabel(scope.row.status) }}</el-tag></template></el-table-column>
          <el-table-column label="保存结果" min-width="190">
            <template #default="scope">
              <template v-if="scope.row.status === 'saved'"><strong class="cell-primary monospace">{{ scope.row.target_path || '已保存' }}</strong><el-tooltip :content="formatDateTime(scope.row.saved_at)"><span class="subtext">{{ formatRelativeTime(scope.row.saved_at) }}</span></el-tooltip></template>
              <el-tooltip v-else-if="scope.row.last_error" :content="scope.row.last_error"><span class="error-text">{{ scope.row.last_error }}</span></el-tooltip>
              <span v-else class="muted">等待处理</span>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="90" fixed="right">
            <template #default="scope"><el-button v-if="scope.row.status === 'failed'" link type="primary" :loading="retryingId === scope.row.id" @click="retry(scope.row.id)">重试</el-button></template>
          </el-table-column>
        </el-table>
        <div v-if="!loading && !files.length" class="empty-state">
          <span class="empty-state__icon"><AppIcon name="files" /></span>
          <h3>{{ total ? '当前页没有记录' : '没有符合条件的文件记录' }}</h3>
          <p>新文件被订阅发现后，会在这里显示扫描和转存状态。</p>
        </div>
      </div>

      <div class="table-footer">
        <span>共 {{ total }} 条记录</span>
        <el-pagination v-model:current-page="page" v-model:page-size="pageSize" layout="prev, pager, next" :total="total" @current-change="load" />
      </div>
    </div>
  </section>
</template>

<style scoped>
.file-filters { flex: 1; }
.file-filters .el-input { width: min(320px, 30vw); }
.file-filters .el-select { width: 170px; }
.file-cell { display: flex; align-items: center; gap: 11px; min-width: 0; }
.file-cell > div { min-width: 0; }
.file-cell strong, .file-cell span:last-child { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.file-cell span:last-child, .subtext { margin-top: 4px; color: var(--text-muted); font-size: 12px; }
.file-icon { display: grid; place-items: center; flex: 0 0 34px; width: 34px; height: 34px; border-radius: 10px; color: var(--primary); background: var(--primary-soft); }
.error-text { display: block; overflow: hidden; color: var(--danger); font-size: 13px; text-overflow: ellipsis; white-space: nowrap; }
.muted { color: var(--text-muted); }
@media (max-width: 820px) {
  .file-filters, .file-filters .el-input, .file-filters .el-select { width: 100%; }
}
</style>
