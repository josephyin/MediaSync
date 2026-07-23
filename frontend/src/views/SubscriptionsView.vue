<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api, type Page } from '../api/client'
import type { CloudAccount, DriveInfo, FolderItem, Subscription } from '../api/types'
import AppIcon from '../components/AppIcon.vue'
import { driveTypeLabels, formatDateTime, formatRelativeTime, scheduleLabel, statusLabel, statusType } from '../utils/display'

const subscriptions = ref<Subscription[]>([])
const accounts = ref<CloudAccount[]>([])
const pageLoading = ref(false)
const scanningId = ref<number | null>(null)
const togglingId = ref<number | null>(null)
const query = ref('')
const stateFilter = ref('')
const dialog = ref(false)
const saving = ref(false)
const editingId = ref<number | null>(null)
const form = reactive({ name: '', cloud_account_id: 0, provider: 'aliyundrive', share_url: '', share_password: '', target_drive_id: '', target_drive_type: 'default', target_path: '/Media', schedule: 'interval:30m', initial_sync_mode: 'all', enabled: true })
const drives = ref<DriveInfo[]>([])
const drivesLoading = ref(false)

const folderDialog = ref(false)
const folderLoading = ref(false)
const folders = ref<FolderItem[]>([])
const currentPath = ref('/')
const breadcrumbs = computed(() => {
  const result = [{ name: '根目录', path: '/' }]
  let path = ''
  for (const part of currentPath.value.split('/').filter(Boolean)) {
    path += `/${part}`
    result.push({ name: part, path })
  }
  return result
})
const selectedDrive = computed(() => drives.value.find((item) => item.id === form.target_drive_id))
const filteredSubscriptions = computed(() => {
  const keyword = query.value.trim().toLowerCase()
  return subscriptions.value.filter((item) => {
    const stateMatched = !stateFilter.value || (stateFilter.value === 'enabled' ? item.enabled : !item.enabled)
    const keywordMatched = !keyword || [item.name, item.share_url, item.target_path].some((value) => value.toLowerCase().includes(keyword))
    return stateMatched && keywordMatched
  })
})
const enabledCount = computed(() => subscriptions.value.filter((item) => item.enabled).length)
const healthyCount = computed(() => subscriptions.value.filter((item) => item.status === 'active').length)

async function load() {
  pageLoading.value = true
  try {
    const [subs, accts] = await Promise.all([api<Page<Subscription>>('/subscriptions?page_size=100'), api<Page<CloudAccount>>('/cloud-accounts?page_size=100')])
    subscriptions.value = subs.items
    accounts.value = accts.items
    if (!form.cloud_account_id && accounts.value.length) form.cloud_account_id = accounts.value[0].id
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '订阅列表加载失败')
  } finally {
    pageLoading.value = false
  }
}
function accountName(id: number) { return accounts.value.find((item) => item.id === id)?.name ?? `账号 #${id}` }
function resetForm() {
  Object.assign(form, {
    name: '', cloud_account_id: accounts.value[0]?.id ?? 0, provider: 'aliyundrive',
    share_url: '', share_password: '', target_drive_id: '', target_drive_type: 'default',
    target_path: '/Media', schedule: 'interval:30m', initial_sync_mode: 'all', enabled: true,
  })
}
async function saveSubscription() {
  if (!form.name.trim() || !form.target_drive_id || !form.target_path.trim()) {
    ElMessage.warning('请完整填写名称、目标盘和目标目录')
    return
  }
  saving.value = true
  try {
    const matchedDrive = drives.value.find((item) => item.id === form.target_drive_id)
    form.target_drive_type = matchedDrive?.type ?? 'custom'
    if (editingId.value) {
      await api(`/subscriptions/${editingId.value}`, {
        method: 'PATCH',
        body: JSON.stringify({
          name: form.name.trim(), target_drive_id: form.target_drive_id,
          target_drive_type: form.target_drive_type, target_path: form.target_path.trim(),
          schedule: form.schedule, initial_sync_mode: form.initial_sync_mode, enabled: form.enabled,
        }),
      })
    } else {
      await api('/subscriptions', { method: 'POST', body: JSON.stringify({ ...form, name: form.name.trim(), share_password: form.share_password || null }) })
    }
    dialog.value = false
    ElMessage.success(editingId.value ? '订阅已更新' : '订阅已添加')
    await load()
  } catch (error) { ElMessage.error(error instanceof Error ? error.message : (editingId.value ? '更新失败' : '添加失败')) }
  finally { saving.value = false }
}
async function scan(id: number, full = false) {
  try {
    if (full) await ElMessageBox.confirm('完整校验会递归读取全部分享目录，API 请求会明显增加。是否继续？', '完整校验', { type: 'warning', confirmButtonText: '继续校验', cancelButtonText: '取消' })
    scanningId.value = id
    await api(`/subscriptions/${id}/scan${full ? '?full=true' : ''}`, { method: 'POST' })
    ElMessage.success(full ? '完整校验任务已提交' : '增量轮询任务已提交')
  } catch (error) {
    if (error === 'cancel' || error === 'close') return
    ElMessage.error(error instanceof Error ? error.message : '扫描任务提交失败')
  } finally { scanningId.value = null }
}
async function toggle(row: Subscription) {
  togglingId.value = row.id
  try {
    await api(`/subscriptions/${row.id}`, { method: 'PATCH', body: JSON.stringify({ enabled: !row.enabled }) })
    ElMessage.success(row.enabled ? '订阅已停用' : '订阅已启用')
    await load()
  } catch (error) { ElMessage.error(error instanceof Error ? error.message : '状态更新失败') }
  finally { togglingId.value = null }
}
async function remove(id: number) {
  try {
    await ElMessageBox.confirm('删除订阅不会删除目标云盘文件。是否继续？', '删除订阅', { type: 'warning', confirmButtonText: '确认删除', cancelButtonText: '取消' })
    await api(`/subscriptions/${id}`, { method: 'DELETE' })
    ElMessage.success('订阅已删除')
    await load()
  } catch (error) {
    if (error === 'cancel' || error === 'close') return
    ElMessage.error(error instanceof Error ? error.message : '删除失败')
  }
}

function childPath(name: string) {
  return currentPath.value === '/' ? `/${name}` : `${currentPath.value}/${name}`
}
async function loadFolders(path: string) {
  if (!form.cloud_account_id) { ElMessage.warning('请先选择目标云盘'); return }
  if (!form.target_drive_id) { ElMessage.warning('请先选择目标盘'); return }
  folderLoading.value = true
  try {
    folders.value = await api<FolderItem[]>(`/cloud-accounts/${form.cloud_account_id}/folders?path=${encodeURIComponent(path)}&drive_id=${encodeURIComponent(form.target_drive_id)}`)
    currentPath.value = path
  } catch (error) { ElMessage.error(error instanceof Error ? error.message : '读取云盘目录失败') }
  finally { folderLoading.value = false }
}
async function openFolderPicker() {
  if (!form.target_drive_id) { ElMessage.warning('请先选择目标盘'); return }
  folderDialog.value = true
  await loadFolders('/')
}
async function enterFolder(folder: FolderItem) { await loadFolders(childPath(folder.name)) }
function selectCurrentFolder() {
  form.target_path = currentPath.value
  folderDialog.value = false
}
async function loadDrives() {
  if (!form.cloud_account_id) return
  drivesLoading.value = true
  try {
    drives.value = await api<DriveInfo[]>(`/cloud-accounts/${form.cloud_account_id}/drives`)
    if (!drives.value.some((item) => item.id === form.target_drive_id)) {
      form.target_drive_id = drives.value[0]?.id ?? ''
    }
  } catch (error) {
    drives.value = []
    ElMessage.error(error instanceof Error ? error.message : '读取盘类型失败')
  } finally { drivesLoading.value = false }
}
async function accountChanged() {
  form.target_path = '/'
  form.target_drive_id = ''
  await loadDrives()
}
async function openAddDialog() {
  editingId.value = null
  resetForm()
  dialog.value = true
  await loadDrives()
}
async function openEditDialog(row: Subscription) {
  editingId.value = row.id
  Object.assign(form, {
    name: row.name, cloud_account_id: row.cloud_account_id, provider: row.provider,
    share_url: row.share_url, share_password: '', target_drive_id: row.target_drive_id ?? '',
    target_drive_type: row.target_drive_type ?? 'custom', target_path: row.target_path,
    schedule: row.schedule, initial_sync_mode: row.initial_sync_mode, enabled: row.enabled,
  })
  dialog.value = true
  await loadDrives()
}

onMounted(load)
</script>

<template>
  <section>
    <div class="page-heading">
      <div><h1>分享订阅</h1><p>按计划增量检查分享目录，只为新内容创建转存任务</p></div>
      <el-button type="primary" :disabled="!accounts.length" @click="openAddDialog"><AppIcon name="plus" />添加订阅</el-button>
    </div>
    <el-alert v-if="!accounts.length" title="请先添加一个云盘账号" type="warning" :closable="false" class="section-gap" />

    <div class="summary-strip">
      <div class="summary-item"><span class="summary-icon"><AppIcon name="subscription" /></span><div><strong>{{ subscriptions.length }}</strong><span>全部订阅</span></div></div>
      <div class="summary-item"><span class="summary-icon success"><AppIcon name="shield" /></span><div><strong>{{ enabledCount }}</strong><span>监控中</span></div></div>
      <div class="summary-item"><span class="summary-icon purple"><AppIcon name="refresh" /></span><div><strong>{{ healthyCount }}</strong><span>最近运行正常</span></div></div>
    </div>

    <div class="section-card">
      <div class="toolbar">
        <div class="toolbar-group toolbar-search">
          <el-input v-model="query" clearable placeholder="搜索名称、分享链接或目标目录">
            <template #prefix><AppIcon name="search" /></template>
          </el-input>
          <el-select v-model="stateFilter" clearable placeholder="全部启用状态">
            <el-option label="监控中" value="enabled" />
            <el-option label="已停用" value="disabled" />
          </el-select>
        </div>
        <el-button :loading="pageLoading" @click="load"><AppIcon name="refresh" />刷新</el-button>
      </div>
      <div class="table-wrap">
        <el-table v-loading="pageLoading" :data="filteredSubscriptions" empty-text=" ">
          <el-table-column label="订阅" min-width="230">
            <template #default="scope">
              <div class="subscription-cell">
                <span class="subscription-icon"><AppIcon name="subscription" /></span>
                <div><strong class="cell-primary">{{ scope.row.name }}</strong><span>{{ accountName(scope.row.cloud_account_id) }}</span></div>
              </div>
            </template>
          </el-table-column>
          <el-table-column label="保存位置" min-width="220">
            <template #default="scope">
              <div class="target-cell"><el-tag size="small" effect="plain">{{ driveTypeLabels[scope.row.target_drive_type || 'custom'] }}</el-tag><span class="monospace">{{ scope.row.target_path }}</span></div>
            </template>
          </el-table-column>
          <el-table-column label="检查计划" width="140"><template #default="scope"><strong>{{ scheduleLabel(scope.row.schedule) }}</strong><span class="subtext">{{ scope.row.initial_sync_mode === 'future_only' ? '仅监控新增' : '首次同步全部' }}</span></template></el-table-column>
          <el-table-column label="运行状态" width="125"><template #default="scope"><el-tag :type="statusType(scope.row.status)">{{ scope.row.enabled ? statusLabel(scope.row.status) : '已停用' }}</el-tag></template></el-table-column>
          <el-table-column label="最近扫描" width="150"><template #default="scope"><el-tooltip :content="formatDateTime(scope.row.last_scanned_at)"><span>{{ formatRelativeTime(scope.row.last_scanned_at) }}</span></el-tooltip></template></el-table-column>
          <el-table-column label="操作" width="250" fixed="right">
            <template #default="scope">
              <el-button link type="primary" :loading="scanningId === scope.row.id" @click="scan(scope.row.id)">立即轮询</el-button>
              <el-button link @click="openEditDialog(scope.row)">编辑</el-button>
              <el-dropdown trigger="click">
                <el-button link>更多<AppIcon name="arrow" /></el-button>
                <template #dropdown>
                  <el-dropdown-menu>
                    <el-dropdown-item @click="scan(scope.row.id, true)">完整校验</el-dropdown-item>
                    <el-dropdown-item :disabled="togglingId === scope.row.id" @click="toggle(scope.row)">{{ scope.row.enabled ? '停用订阅' : '启用订阅' }}</el-dropdown-item>
                    <el-dropdown-item divided class="danger-item" @click="remove(scope.row.id)">删除订阅</el-dropdown-item>
                  </el-dropdown-menu>
                </template>
              </el-dropdown>
            </template>
          </el-table-column>
        </el-table>
        <div v-if="!pageLoading && !filteredSubscriptions.length" class="empty-state">
          <span class="empty-state__icon"><AppIcon name="subscription" /></span>
          <h3>{{ subscriptions.length ? '没有符合条件的订阅' : '还没有分享订阅' }}</h3>
          <p>{{ subscriptions.length ? '调整筛选条件后再试试。' : '添加分享链接后，MediaSync 会按计划检查并转存新增内容。' }}</p>
          <el-button v-if="!subscriptions.length && accounts.length" type="primary" @click="openAddDialog">添加第一个订阅</el-button>
        </div>
      </div>
    </div>

    <el-dialog v-model="dialog" :title="editingId ? '编辑分享订阅' : '添加分享订阅'" width="min(680px, calc(100vw - 32px))"><el-form label-position="top">
      <div class="form-section-title">资源来源</div>
      <el-form-item label="订阅名称"><el-input v-model="form.name" placeholder="例如：某剧集持续更新" /></el-form-item>
      <el-form-item label="分享链接"><el-input v-model="form.share_url" :disabled="!!editingId" placeholder="粘贴阿里云盘分享链接" /></el-form-item>
      <el-form-item v-if="!editingId" label="分享密码（可选）"><el-input v-model="form.share_password" /></el-form-item>
      <div class="form-section-title">保存位置</div>
      <div class="form-grid">
        <el-form-item label="目标云盘"><el-select v-model="form.cloud_account_id" class="full-width" :disabled="!!editingId" @change="accountChanged"><el-option v-for="item in accounts" :key="item.id" :label="item.name" :value="item.id" /></el-select></el-form-item>
        <el-form-item label="目标盘">
        <el-select v-model="form.target_drive_id" class="full-width" filterable allow-create default-first-option :loading="drivesLoading" placeholder="选择盘类型，或粘贴 Drive ID" @change="form.target_path = '/'">
          <el-option v-for="item in drives" :key="item.id" :label="`${item.name}（${driveTypeLabels[item.type]}）`" :value="item.id" />
        </el-select>
        </el-form-item>
      </div>
      <div class="form-tip drive-tip">私有接口未返回资源库时，可粘贴从 OpenList 获取的 Drive ID。</div>
      <el-form-item label="目标目录">
        <el-input v-model="form.target_path"><template #append><el-button @click="openFolderPicker"><AppIcon name="folder" />选择目录</el-button></template></el-input>
      </el-form-item>
      <div class="form-section-title">检查策略</div>
      <div class="form-grid">
        <el-form-item label="检查周期"><el-select v-model="form.schedule" class="full-width"><el-option label="每 15 分钟" value="interval:15m" /><el-option label="每 30 分钟" value="interval:30m" /><el-option label="每 1 小时" value="interval:1h" /><el-option label="每 3 小时" value="interval:3h" /><el-option label="每 6 小时" value="interval:6h" /><el-option label="每 12 小时" value="interval:12h" /></el-select></el-form-item>
        <el-form-item label="首次同步"><el-radio-group v-model="form.initial_sync_mode"><el-radio value="all">转存全部</el-radio><el-radio value="future_only">只监控新增</el-radio></el-radio-group></el-form-item>
      </div>
      <el-alert title="最短周期限制为 15 分钟，实际执行会自动随机错峰；日常轮询使用增量游标，不会每次完整遍历。" type="info" :closable="false" />
      <el-form-item v-if="editingId" label="启用状态"><el-switch v-model="form.enabled" active-text="启用" inactive-text="停用" /></el-form-item>
      <el-alert v-if="editingId" title="修改目标盘或目录只影响后续转存，不会移动已经保存的文件。" type="info" :closable="false" />
    </el-form><template #footer><el-button @click="dialog = false">取消</el-button><el-button type="primary" :loading="saving" @click="saveSubscription">{{ editingId ? '保存修改' : '创建订阅' }}</el-button></template></el-dialog>

    <el-dialog v-model="folderDialog" title="选择目标云盘目录" width="min(560px, calc(100vw - 32px))">
      <el-alert :title="selectedDrive ? `当前浏览：${selectedDrive.name}（${selectedDrive.type}）` : '当前浏览：自定义 Drive ID'" type="info" :closable="false" class="folder-alert" />
      <el-breadcrumb separator="/" class="folder-breadcrumb">
        <el-breadcrumb-item v-for="item in breadcrumbs" :key="item.path"><el-link :underline="false" @click="loadFolders(item.path)">{{ item.name }}</el-link></el-breadcrumb-item>
      </el-breadcrumb>
      <el-table v-loading="folderLoading" :data="folders" height="300" empty-text=" " @row-dblclick="enterFolder">
        <el-table-column label="目录"><template #default="scope"><el-button link type="primary" @click="enterFolder(scope.row)">📁 {{ scope.row.name }}</el-button></template></el-table-column>
        <el-table-column label="更新时间" width="170"><template #default="scope">{{ formatDateTime(scope.row.updated_at) }}</template></el-table-column>
      </el-table>
      <el-empty v-if="!folderLoading && !folders.length" description="当前目录没有子目录" />
      <template #footer><span class="selected-path">当前：{{ currentPath }}</span><el-button @click="folderDialog = false">取消</el-button><el-button type="primary" @click="selectCurrentFolder">选择当前目录</el-button></template>
    </el-dialog>
  </section>
</template>

<style scoped>
.toolbar-search { flex: 1; }
.toolbar-search .el-input { max-width: 380px; }
.toolbar-search .el-select { width: 160px; }
.subscription-cell, .target-cell { display: flex; align-items: center; gap: 10px; min-width: 0; }
.subscription-cell > div { min-width: 0; }
.subscription-cell span:last-child, .subtext { display: block; margin-top: 3px; color: var(--text-muted); font-size: 12px; }
.subscription-icon { display: grid; place-items: center; flex: 0 0 34px; width: 34px; height: 34px; border-radius: 10px; color: var(--primary); background: var(--primary-soft); }
.target-cell .monospace { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.form-section-title { margin: 8px 0 16px; padding-bottom: 9px; border-bottom: 1px solid var(--border-color); color: var(--text-primary); font-size: 14px; font-weight: 700; }
.form-section-title:not(:first-child) { margin-top: 22px; }
.form-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
.drive-tip { margin: -12px 0 18px; }
.folder-alert { margin-bottom: 18px; }
.folder-breadcrumb { margin-bottom: 14px; }
.selected-path { float: left; line-height: 32px; color: var(--el-text-color-secondary); }
.form-tip { margin-top: 6px; color: var(--el-text-color-secondary); font-size: 12px; line-height: 18px; }
@media (max-width: 700px) {
  .toolbar-search, .toolbar-search .el-input, .toolbar-search .el-select { width: 100%; max-width: none; }
  .form-grid { grid-template-columns: 1fr; gap: 0; }
}
</style>
