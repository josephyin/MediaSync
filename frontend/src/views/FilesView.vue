<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api, type Page } from '../api/client'
import type { CloudFile, Subscription } from '../api/types'
import AppIcon from '../components/AppIcon.vue'
import { formatDateTime, formatFileSize, formatRelativeTime, statusLabel, statusType, taskMessage } from '../utils/display'

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
const bulkRetrying = ref(false)
const selectedFile = ref<CloudFile | null>(null)
const detailVisible = ref(false)
const selection = ref<CloudFile[]>([])
const downloadFiles = ref<{id: number}[]>([])
const selectingAll = ref(false)
const fileTable = ref<{ clearSelection: () => void }>()
async function selectAllDownloadable() {
  selectingAll.value = true
  try {
    const params = filteredParams(status.value)
    const result = await api<{file_ids: number[]; count: number}>(`/downloads/selection?${params}`)
    if (!result.count) { ElMessage.info('当前筛选范围没有可下载的已转存 PikPak 文件'); return }
    await openDownload(result.file_ids.map(id => ({id})))
  } catch (error) { ElMessage.error(error instanceof Error ? error.message : '跨页选择失败') }
  finally { selectingAll.value = false }
}
const downloadVisible = ref(false)
const downloadDirectory = ref('')
const downloadRoot = ref('')
const downloadAvailable = ref(false)
const downloading = ref(false)
const locationVisible = ref(false)
const locationPath = ref('')
const locationParent = ref('/')
const locationEntries = ref<{ name: string; path: string }[]>([])
const locationBusy = ref(false)
const locationTruncated = ref(false)
async function browseLocation(path: string) {
  locationBusy.value = true
  try {
    const result = await api<{ path: string; parent: string; directories: {name: string; path: string}[]; truncated: boolean }>(`/downloads/directories?path=${encodeURIComponent(path)}`)
    locationPath.value = result.path
    locationParent.value = result.parent
    locationEntries.value = result.directories
    locationTruncated.value = result.truncated
  } catch (error) { ElMessage.error(error instanceof Error ? error.message : '目录加载失败') }
  finally { locationBusy.value = false }
}
function openLocationRow(row: {path: string}) { void browseLocation(row.path) }
async function chooseLocation() {
  locationPath.value = downloadAvailable.value ? downloadRoot.value : '/'
  locationVisible.value = true
  await browseLocation(locationPath.value)
}
async function saveLocation() {
  locationBusy.value = true
  try {
    const result = await api<{ root: string; available: boolean }>('/downloads/config', {
      method: 'PUT', body: JSON.stringify({root: locationPath.value}),
    })
    downloadRoot.value = result.root
    downloadAvailable.value = result.available
    locationVisible.value = false
    ElMessage.success('下载位置已保存，无需重启')
  } catch (error) { ElMessage.error(error instanceof Error ? error.message : '保存失败') }
  finally { locationBusy.value = false }
}
function downloadable(file: CloudFile) {
  return file.status === 'saved' && file.item_type === 'file' && !!file.target_file_id
    && subscriptions.value.find(s => s.id === file.subscription_id)?.provider === 'pikpak'
}
async function openDownload(items: {id: number}[]) {
  downloadFiles.value = items
  try {
    const config = await api<{ root: string; available: boolean }>('/downloads/config')
    downloadRoot.value = config.root
    downloadAvailable.value = config.available
    downloadVisible.value = true
  } catch (error) { ElMessage.error(error instanceof Error ? error.message : '下载配置加载失败') }
}
async function submitDownload() {
  downloading.value = true
  try {
    const result = await api<{ task_ids: number[] }>('/downloads', {
      method: 'POST', body: JSON.stringify({ file_ids: downloadFiles.value.map(f => f.id), root: downloadRoot.value, directory: downloadDirectory.value }),
    })
    ElMessage.success(`已加入 ${result.task_ids.length} 个下载任务，请到任务中心查看进度`)
    downloadVisible.value = false
  } catch (error) { ElMessage.error(error instanceof Error ? error.message : '下载提交失败') }
  finally { downloading.value = false }
}


function subscriptionName(id: number) {
  return subscriptions.value.find((item) => item.id === id)?.name ?? `订阅 #${id}`
}
function openDetail(file: CloudFile) {
  selectedFile.value = file
  detailVisible.value = true
}
function resultDescription(file: CloudFile) {
  if (file.status === 'saved') return '转存完成'
  if (file.status === 'saving') return '正在转存到目标云盘'
  if (file.status === 'failed') return file.last_error ? taskMessage(file.last_error) : '转存失败，可重新尝试'
  return '等待转存任务处理'
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
function applyFilters() { fileTable.value?.clearSelection(); selection.value = []; page.value = 1; void load() }
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
    if (selectedFile.value?.id === id) {
      selectedFile.value = files.value.find((item) => item.id === id) ?? selectedFile.value
    }
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '重试提交失败')
  } finally {
    retryingId.value = null
  }
}
interface BulkRetryResult {
  matched_count: number
  enqueued_count: number
  skipped_count: number
}
function filteredParams(statusFilter?: string) {
  const params = new URLSearchParams()
  if (statusFilter) params.set('status', statusFilter)
  if (query.value.trim()) params.set('query', query.value.trim())
  if (subscriptionId.value) params.set('subscription_id', String(subscriptionId.value))
  return params
}
async function retryFailed() {
  bulkRetrying.value = true
  try {
    const previewParams = filteredParams('failed')
    previewParams.set('page', '1')
    previewParams.set('page_size', '1')
    const preview = await api<Page<CloudFile>>(`/files?${previewParams}`)
    if (!preview.total) {
      ElMessage.info('当前筛选范围内没有失败文件')
      return
    }
    await ElMessageBox.confirm(
      `将把当前筛选范围内的 ${preview.total} 个失败文件重新加入转存队列，是否继续？`,
      '批量重新尝试',
      { type: 'warning', confirmButtonText: '全部重新尝试', cancelButtonText: '取消' },
    )
    const result = await api<BulkRetryResult>(`/files/retry-failed?${filteredParams()}`, { method: 'POST' })
    const skipped = result.skipped_count ? `，跳过 ${result.skipped_count} 个已有任务` : ''
    ElMessage.success(`已将 ${result.enqueued_count} 个文件加入重试队列${skipped}`)
    page.value = 1
    await load()
  } catch (error) {
    if (error === 'cancel' || error === 'close') return
    ElMessage.error(error instanceof Error ? error.message : '批量重试提交失败')
  } finally {
    bulkRetrying.value = false
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
        <div class="toolbar-actions">
          <el-button :loading="selectingAll" @click="selectAllDownloadable">跨页全选下载</el-button>
          <el-button :disabled="!selection.length" @click="openDownload(selection)">下载选中（{{ selection.length }}）</el-button>
          <el-button v-if="!status || status === 'failed'" type="primary" :loading="bulkRetrying" @click="retryFailed">
            批量重试失败文件
          </el-button>
          <el-button text @click="clearFilters">重置筛选</el-button>
        </div>
      </div>

      <div class="table-wrap">
        <el-table ref="fileTable" row-key="id" v-loading="loading" :data="files" empty-text=" " @selection-change="selection = $event">
          <el-table-column type="selection" :reserve-selection="true" width="45" :selectable="downloadable" />
          <el-table-column label="文件信息" min-width="320">
            <template #default="scope">
              <div class="file-cell">
                <span class="file-icon"><AppIcon name="files" /></span>
                <div>
                  <button class="file-link" type="button" @click="openDetail(scope.row)">{{ scope.row.filename }}</button>
                  <el-tooltip :content="scope.row.relative_path">
                    <span>{{ scope.row.relative_path }}</span>
                  </el-tooltip>
                </div>
              </div>
            </template>
          </el-table-column>
          <el-table-column label="所属订阅" min-width="150"><template #default="scope">{{ subscriptionName(scope.row.subscription_id) }}</template></el-table-column>
          <el-table-column label="大小" width="105"><template #default="scope"><span class="nowrap">{{ formatFileSize(scope.row.size) }}</span></template></el-table-column>
          <el-table-column label="处理结果" min-width="230">
            <template #default="scope">
              <div class="result-cell">
                <div class="result-cell__header">
                  <el-button v-if="downloadable(scope.row)" link type="primary" @click="openDownload([scope.row])">下载到本地</el-button>
                  <el-tag :type="statusType(scope.row.status)">{{ statusLabel(scope.row.status) }}</el-tag>
                  <el-button
                    v-if="scope.row.status === 'failed'"
                    link
                    type="primary"
                    :loading="retryingId === scope.row.id"
                    @click="retry(scope.row.id)"
                  >
                    重新尝试
                  </el-button>
                </div>
                <el-tooltip v-if="scope.row.status === 'failed'" :content="resultDescription(scope.row)">
                  <span class="result-message error-text">{{ resultDescription(scope.row) }}</span>
                </el-tooltip>
                <span v-else class="result-message">{{ resultDescription(scope.row) }}</span>
                <span v-if="scope.row.status === 'saved'" class="result-time">
                  完成于 {{ formatDateTime(scope.row.saved_at) }}
                </span>
              </div>
            </template>
          </el-table-column>
          <el-table-column label="目标位置" min-width="250">
            <template #default="scope">
              <el-tooltip v-if="scope.row.target_path" :content="scope.row.target_path">
                <span class="target-path monospace">{{ scope.row.target_path }}</span>
              </el-tooltip>
              <span v-else class="muted">—</span>
            </template>
          </el-table-column>
          <el-table-column label="发现时间" width="175">
            <template #default="scope">
              <span class="date-time">{{ formatDateTime(scope.row.first_seen_at) }}</span>
              <span class="subtext">{{ formatRelativeTime(scope.row.first_seen_at) }}</span>
            </template>
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

    <el-drawer v-model="detailVisible" title="文件详情" size="min(520px, 92vw)">
      <template v-if="selectedFile">
        <div class="detail-heading">
          <span class="file-icon"><AppIcon name="files" /></span>
          <div>
            <strong>{{ selectedFile.filename }}</strong>
            <span>{{ subscriptionName(selectedFile.subscription_id) }}</span>
          </div>
        </div>

        <el-descriptions :column="1" border class="file-details">
          <el-descriptions-item label="处理状态">
            <el-tag :type="statusType(selectedFile.status)">{{ statusLabel(selectedFile.status) }}</el-tag>
          </el-descriptions-item>
          <el-descriptions-item label="分享内路径">
            <span class="detail-value monospace">{{ selectedFile.relative_path }}</span>
          </el-descriptions-item>
          <el-descriptions-item label="文件大小">{{ formatFileSize(selectedFile.size) }}</el-descriptions-item>
          <el-descriptions-item label="目标位置">
            <span class="detail-value monospace">{{ selectedFile.target_path || '—' }}</span>
          </el-descriptions-item>
          <el-descriptions-item label="首次发现">{{ formatDateTime(selectedFile.first_seen_at) }}</el-descriptions-item>
          <el-descriptions-item label="最近发现">{{ formatDateTime(selectedFile.last_seen_at) }}</el-descriptions-item>
          <el-descriptions-item label="转存完成">{{ formatDateTime(selectedFile.saved_at) }}</el-descriptions-item>
        </el-descriptions>

        <el-alert
          v-if="selectedFile.last_error"
          class="detail-error"
          title="最近一次错误"
          :description="taskMessage(selectedFile.last_error)"
          type="error"
          :closable="false"
          show-icon
        />

        <el-button
          v-if="selectedFile.status === 'failed'"
          class="detail-retry"
          type="primary"
          :loading="retryingId === selectedFile.id"
          @click="retry(selectedFile.id)"
        >
          重新尝试转存
        </el-button>
      </template>
    </el-drawer>
    <el-dialog v-model="downloadVisible" title="下载到本地 / 已挂载的 NAS" width="min(560px, 92vw)">
      <p>已选 {{ downloadFiles.length }} 个文件（包含其他页面），保留原目录结构。</p>
      <p>下载位置：<code>{{ downloadRoot }}</code></p>
      <el-button @click="chooseLocation">选择下载目录</el-button>
      <p class="muted">浏览的是运行 MediaSync 的机器，支持已挂载的 NAS 目录。</p>
      <el-alert v-if="!downloadAvailable" type="warning" :closable="false" title="目录尚不存在，请选择其他目录，或先在服务器上挂载 NAS。" />
      <el-form label-position="top">
        <el-form-item label="根目录内的子目录（可留空）">
          <el-input v-model="downloadDirectory" placeholder="例如：电影 / 本次下载" />
        </el-form-item>
      </el-form>
      <p class="muted">不覆盖已有文件。中断保留进度，重试可断点继续；内容校验失败时重新下载。</p>
      <template #footer>
        <el-button @click="downloadVisible = false">取消</el-button>
        <el-button type="primary" :loading="downloading" :disabled="!downloadAvailable" @click="submitDownload">加入下载队列</el-button>
      </template>
    </el-dialog>
    <el-dialog v-model="locationVisible" title="选择下载目录" width="min(620px, 94vw)" append-to-body>
      <p>选择现有文件夹作为默认下载位置。Docker 中只能看到已映射的目录。</p>
      <el-input v-model="locationPath" placeholder="输入绝对路径，例如 /Volumes/NAS/Media 或 /downloads" @keyup.enter="browseLocation(locationPath)">
        <template #append><el-button :disabled="locationBusy" @click="browseLocation(locationPath)">打开</el-button></template>
      </el-input>
      <el-button style="margin-top: 12px" :disabled="locationBusy || locationPath === locationParent" @click="browseLocation(locationParent)">上一级</el-button>
      <div v-loading="locationBusy" style="max-height: 320px; overflow: auto; margin-top: 12px">
        <el-table :data="locationEntries" empty-text="此目录没有可浏览的子目录" @row-dblclick="openLocationRow">
          <el-table-column label="文件夹">
            <template #default="scope"><el-button link type="primary" @click="browseLocation(scope.row.path)">{{ scope.row.name }}</el-button></template>
          </el-table-column>
        </el-table>
      </div>
      <p v-if="locationTruncated">仅显示前 1,000 个目录，也可在上方直接输入路径。</p>
      <template #footer>
        <el-button @click="locationVisible = false">取消</el-button>
        <el-button type="primary" :loading="locationBusy" @click="saveLocation">使用此目录</el-button>
      </template>
    </el-dialog>
  </section>
</template>

<style scoped>
.file-filters { flex: 1; }
.toolbar-actions { display: flex; align-items: center; }
.file-filters .el-input { width: min(320px, 30vw); }
.file-filters .el-select { width: 170px; }
.file-cell { display: flex; align-items: center; gap: 11px; min-width: 0; }
.file-cell > div { min-width: 0; }
.file-cell span:last-child { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.file-cell span:last-child, .subtext { margin-top: 4px; color: var(--text-muted); font-size: 12px; }
.file-icon { display: grid; place-items: center; flex: 0 0 34px; width: 34px; height: 34px; border-radius: 10px; color: var(--primary); background: var(--primary-soft); }
.file-link { display: block; max-width: 100%; padding: 0; overflow: hidden; color: #1d2939; cursor: pointer; font-size: 13px; font-weight: 600; text-align: left; text-overflow: ellipsis; white-space: nowrap; background: none; }
.file-link:hover { color: var(--primary); }
.result-cell { display: grid; gap: 5px; min-width: 0; }
.result-cell__header { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.result-message { overflow: hidden; color: var(--text-secondary); font-size: 12px; text-overflow: ellipsis; white-space: nowrap; }
.result-time { color: var(--text-muted); font-size: 12px; white-space: nowrap; }
.error-text { color: var(--danger); }
.target-path { display: block; overflow: hidden; color: #475467; text-overflow: ellipsis; white-space: nowrap; }
.date-time { display: block; color: #344054; font-size: 12px; white-space: nowrap; }
.nowrap { white-space: nowrap; }
.muted { color: var(--text-muted); }
.detail-heading { display: flex; align-items: center; gap: 12px; margin: 0 0 22px; }
.detail-heading > div { min-width: 0; }
.detail-heading strong, .detail-heading span { display: block; overflow-wrap: anywhere; }
.detail-heading strong { color: #101828; font-size: 16px; }
.detail-heading span { margin-top: 4px; color: var(--text-muted); font-size: 12px; }
.file-details { width: 100%; }
.detail-value { color: #475467; overflow-wrap: anywhere; word-break: break-word; }
.detail-error { margin-top: 18px; }
.detail-retry { width: 100%; margin-top: 18px; }
@media (max-width: 820px) {
  .file-filters, .file-filters .el-input, .file-filters .el-select { width: 100%; }
  .toolbar-actions { width: 100%; justify-content: flex-end; }
}
</style>
