<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { api, type Page } from '../api/client'
import type { Subscription, Task } from '../api/types'
import AppIcon from '../components/AppIcon.vue'
import { formatDateTime, formatRelativeTime, statusLabel, statusType, taskMessage, taskTypeLabels, triggerLabels } from '../utils/display'

const tasks = ref<Task[]>([])
const subscriptions = ref<Subscription[]>([])
const status = ref('')
const type = ref('')
const subscriptionId = ref<number | ''>('')
const page = ref(1)
const pageSize = ref(20)
const total = ref(0)
const loading = ref(false)
const autoRefresh = ref(true)
const selectedTask = ref<Task | null>(null)
const drawer = ref(false)
let timer: number | undefined

function subscriptionName(id: number | null) {
  if (!id) return '系统任务'
  return subscriptions.value.find((item) => item.id === id)?.name ?? `订阅 #${id}`
}
async function load(silent = false) {
  if (!silent) loading.value = true
  try {
    const params = new URLSearchParams({ page: String(page.value), page_size: String(pageSize.value) })
    if (status.value) params.set('status', status.value)
    if (type.value) params.set('type', type.value)
    if (subscriptionId.value) params.set('subscription_id', String(subscriptionId.value))
    const result = await api<Page<Task>>(`/tasks?${params}`)
    tasks.value = result.items
    total.value = result.total
    if (selectedTask.value) selectedTask.value = tasks.value.find((item) => item.id === selectedTask.value?.id) ?? selectedTask.value
  } catch (error) {
    if (!silent) ElMessage.error(error instanceof Error ? error.message : '任务记录加载失败')
  } finally {
    if (!silent) loading.value = false
  }
}
function applyFilters() { page.value = 1; void load() }
function clearFilters() {
  status.value = ''
  type.value = ''
  subscriptionId.value = ''
  applyFilters()
}
function openDetail(task: Task) {
  selectedTask.value = task
  drawer.value = true
}
function nextAttemptLabel(task: Task) {
  if (task.next_attempt_at) return formatDateTime(task.next_attempt_at)
  if (task.status === 'waiting_credential') return '等待凭证恢复'
  if (task.status === 'retry') return '等待重新调度'
  if (task.status === 'pending') return '等待执行'
  if (task.status === 'running' || task.status === 'cancel_requested') return '执行中'
  if (task.status === 'failed') return '已停止重试'
  if (task.status === 'cancelled') return '已取消'
  return '无需重试'
}
function startedAtLabel(task: Task) {
  if (task.started_at) return formatDateTime(task.started_at)
  if (task.status === 'pending') return '尚未开始'
  return '暂无执行记录'
}
function finishedAtLabel(task: Task) {
  if (task.finished_at) return formatDateTime(task.finished_at)
  if (task.status === 'running' || task.status === 'cancel_requested') return '尚未结束'
  if (task.status === 'pending') return '尚未开始'
  if (task.status === 'retry') return '等待下次尝试'
  if (task.status === 'waiting_credential') return '等待凭证恢复'
  return '—'
}
onMounted(async () => {
  try { subscriptions.value = (await api<Page<Subscription>>('/subscriptions?page_size=100')).items }
  catch { /* 任务列表仍可独立使用 */ }
  await load()
  timer = window.setInterval(() => { if (autoRefresh.value) void load(true) }, 10000)
})
onUnmounted(() => { if (timer) window.clearInterval(timer) })
</script>

<template>
  <section>
    <div class="page-heading">
      <div><h1>任务中心</h1><p>查看扫描、转存及自动重试的运行记录</p></div>
      <div class="page-actions">
        <span class="auto-refresh"><span :class="{ active: autoRefresh }"></span>自动刷新<el-switch v-model="autoRefresh" size="small" /></span>
        <el-button :loading="loading" @click="load()"><AppIcon name="refresh" />刷新</el-button>
      </div>
    </div>

    <div class="section-card">
      <div class="toolbar">
        <div class="toolbar-group task-filters">
          <el-select v-model="subscriptionId" clearable filterable placeholder="全部订阅" @change="applyFilters">
            <el-option v-for="item in subscriptions" :key="item.id" :label="item.name" :value="item.id" />
          </el-select>
          <el-select v-model="type" clearable placeholder="全部任务类型" @change="applyFilters">
            <el-option label="订阅扫描" value="scan" />
            <el-option label="文件转存" value="transfer" />
          </el-select>
          <el-select v-model="status" clearable placeholder="全部状态" @change="applyFilters">
            <el-option label="等待中" value="pending" />
            <el-option label="执行中" value="running" />
            <el-option label="成功" value="success" />
            <el-option label="失败" value="failed" />
          </el-select>
        </div>
        <el-button text @click="clearFilters">重置筛选</el-button>
      </div>

      <div class="table-wrap">
        <el-table v-loading="loading" :data="tasks" empty-text=" ">
          <el-table-column label="任务" min-width="210">
            <template #default="scope">
              <div class="task-cell">
                <span class="task-icon" :class="scope.row.type"><AppIcon :name="scope.row.type === 'scan' ? 'refresh' : 'cloud'" /></span>
                <div><strong class="cell-primary">{{ taskTypeLabels[scope.row.type] ?? scope.row.type }} #{{ scope.row.id }}</strong><span>{{ subscriptionName(scope.row.subscription_id) }} · {{ triggerLabels[scope.row.trigger_type] ?? scope.row.trigger_type }}</span></div>
              </div>
            </template>
          </el-table-column>
          <el-table-column label="状态" width="110"><template #default="scope"><el-tag :type="statusType(scope.row.status)">{{ statusLabel(scope.row.status) }}</el-tag></template></el-table-column>
          <el-table-column label="执行信息" min-width="260" show-overflow-tooltip><template #default="scope">{{ taskMessage(scope.row.message) }}</template></el-table-column>
          <el-table-column label="重试" width="90"><template #default="scope">{{ scope.row.retry_count }}/{{ scope.row.max_retries }}</template></el-table-column>
          <el-table-column label="创建时间" width="160"><template #default="scope"><el-tooltip :content="formatDateTime(scope.row.created_at)"><span>{{ formatRelativeTime(scope.row.created_at) }}</span></el-tooltip></template></el-table-column>
          <el-table-column label="操作" width="85" fixed="right"><template #default="scope"><el-button link type="primary" @click="openDetail(scope.row)">详情</el-button></template></el-table-column>
        </el-table>
        <div v-if="!loading && !tasks.length" class="empty-state">
          <span class="empty-state__icon"><AppIcon name="tasks" /></span>
          <h3>没有符合条件的任务</h3>
          <p>订阅扫描或文件转存开始后，运行信息会实时出现在这里。</p>
        </div>
      </div>
      <div class="table-footer">
        <span>共 {{ total }} 条记录</span>
        <el-pagination v-model:current-page="page" v-model:page-size="pageSize" layout="prev, pager, next" :total="total" @current-change="load()" />
      </div>
    </div>

    <el-drawer v-model="drawer" title="任务详情" size="min(460px, 92vw)">
      <div v-if="selectedTask" class="task-detail">
        <div class="detail-hero">
          <span class="task-icon large" :class="selectedTask.type"><AppIcon :name="selectedTask.type === 'scan' ? 'refresh' : 'cloud'" /></span>
          <div><h3>{{ taskTypeLabels[selectedTask.type] ?? selectedTask.type }} #{{ selectedTask.id }}</h3><p>{{ subscriptionName(selectedTask.subscription_id) }}</p></div>
          <el-tag :type="statusType(selectedTask.status)">{{ statusLabel(selectedTask.status) }}</el-tag>
        </div>
        <dl>
          <div><dt>触发方式</dt><dd>{{ triggerLabels[selectedTask.trigger_type] ?? selectedTask.trigger_type }}</dd></div>
          <div><dt>执行批次</dt><dd>{{ selectedTask.latest_run ? `第 ${selectedTask.latest_run.run_number} 次` : '尚未执行' }}</dd></div>
          <div><dt>重试次数</dt><dd>{{ selectedTask.retry_count }} / {{ selectedTask.max_retries }}</dd></div>
          <div><dt>创建时间</dt><dd>{{ formatDateTime(selectedTask.created_at) }}</dd></div>
          <div><dt>开始时间</dt><dd>{{ startedAtLabel(selectedTask) }}</dd></div>
          <div><dt>结束时间</dt><dd>{{ finishedAtLabel(selectedTask) }}</dd></div>
          <div><dt>下次尝试</dt><dd>{{ nextAttemptLabel(selectedTask) }}</dd></div>
          <div v-if="selectedTask.error_code"><dt>错误代码</dt><dd class="error-text">{{ selectedTask.error_code }}</dd></div>
        </dl>
        <div class="message-box"><span>执行信息</span><p>{{ taskMessage(selectedTask.message) }}</p></div>
      </div>
    </el-drawer>
  </section>
</template>

<style scoped>
.auto-refresh { display: inline-flex; align-items: center; gap: 8px; color: var(--text-secondary); font-size: 13px; }
.auto-refresh > span { width: 7px; height: 7px; border-radius: 50%; background: #c8ced8; }
.auto-refresh > span.active { background: var(--success); box-shadow: 0 0 0 4px rgb(34 197 94 / 12%); }
.task-filters { flex: 1; }
.task-filters .el-select { width: 180px; }
.task-cell { display: flex; align-items: center; gap: 11px; min-width: 0; }
.task-cell > div { min-width: 0; }
.task-cell span:last-child { display: block; margin-top: 4px; overflow: hidden; color: var(--text-muted); font-size: 12px; text-overflow: ellipsis; white-space: nowrap; }
.task-icon { display: grid; place-items: center; flex: 0 0 34px; width: 34px; height: 34px; border-radius: 10px; color: var(--primary); background: var(--primary-soft); }
.task-icon.transfer { color: #7c3aed; background: #f1eafe; }
.task-icon.large { width: 44px; height: 44px; flex-basis: 44px; border-radius: 13px; }
.detail-hero { display: flex; align-items: center; gap: 12px; padding-bottom: 20px; border-bottom: 1px solid var(--border-color); }
.detail-hero > div { flex: 1; min-width: 0; }
.detail-hero h3, .detail-hero p { margin: 0; }
.detail-hero h3 { margin-bottom: 4px; font-size: 17px; }
.detail-hero p { color: var(--text-secondary); font-size: 13px; }
.task-detail dl { margin: 18px 0; }
.task-detail dl > div { display: grid; grid-template-columns: 90px 1fr; gap: 14px; padding: 11px 0; border-bottom: 1px solid #f0f2f5; font-size: 13px; }
.task-detail dt { color: var(--text-muted); }
.task-detail dd { margin: 0; color: var(--text-primary); text-align: right; word-break: break-word; }
.message-box { padding: 15px; border-radius: 12px; background: #f7f9fc; }
.message-box span { color: var(--text-muted); font-size: 12px; }
.message-box p { margin: 8px 0 0; color: var(--text-primary); font-size: 13px; line-height: 1.7; white-space: pre-wrap; word-break: break-word; }
.error-text { color: var(--danger) !important; }
@media (max-width: 700px) {
  .task-filters, .task-filters .el-select { width: 100%; }
  .auto-refresh { display: none; }
}
</style>
