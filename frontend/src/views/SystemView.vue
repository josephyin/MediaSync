<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { api } from '../api/client'
import type { UpdateStatus } from '../api/types'
import AppIcon from '../components/AppIcon.vue'
import { formatDateTime } from '../utils/display'

const update = ref<UpdateStatus | null>(null)
const loading = ref(false)

const statusPresentation = computed(() => {
  if (!update.value) return { label: '读取中', type: 'info' as const }
  if (update.value.status === 'update_available') {
    return { label: '发现新版本', type: 'warning' as const }
  }
  if (update.value.status === 'current') {
    return { label: '已是最新版本', type: 'success' as const }
  }
  if (update.value.status === 'error') {
    return { label: '检查失败', type: 'danger' as const }
  }
  return { label: '尚未检查', type: 'info' as const }
})

const pullCommand = computed(() => {
  const image = update.value?.manual_upgrade.image
  return image ? `docker pull ${image}` : ''
})

async function loadStatus() {
  loading.value = true
  try {
    const status = await api<UpdateStatus>('/system/update')
    update.value = status
    if (status.status === 'not_checked') await checkForUpdates(false)
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '版本信息加载失败')
  } finally {
    loading.value = false
  }
}

async function checkForUpdates(showSuccess = true) {
  loading.value = true
  try {
    update.value = await api<UpdateStatus>('/system/update/check', { method: 'POST' })
    if (showSuccess && update.value.status === 'current') {
      ElMessage.success('当前已是最新版本')
    } else if (showSuccess && update.value.status === 'update_available') {
      ElMessage.success(`发现新版本 ${update.value.latest_release?.tag_name ?? ''}`)
    } else if (showSuccess && update.value.error_message) {
      ElMessage.warning(update.value.error_message)
    }
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '检查更新失败')
  } finally {
    loading.value = false
  }
}

async function copyPullCommand() {
  if (!pullCommand.value) return
  try {
    await navigator.clipboard.writeText(pullCommand.value)
    ElMessage.success('拉取命令已复制')
  } catch {
    ElMessage.warning('浏览器不允许自动复制，请手动选择命令')
  }
}

onMounted(loadStatus)
</script>

<template>
  <section>
    <div class="page-heading">
      <div>
        <h1>系统设置</h1>
        <p>查看当前版本、官方发布信息和安全升级指引</p>
      </div>
      <div class="page-actions">
        <el-button :loading="loading" @click="checkForUpdates()">
          <AppIcon name="refresh" :size="16" />
          检查更新
        </el-button>
      </div>
    </div>

    <el-alert
      v-if="update?.error_message"
      :title="update.stale ? '版本检查暂时失败，以下为上次成功结果' : '暂时无法检查新版本'"
      :description="update.error_message"
      type="warning"
      :closable="false"
      show-icon
      class="section-gap"
    />
    <el-alert
      v-if="update?.runtime_mode !== 'normal'"
      :title="update?.runtime_mode === 'draining' ? '更新任务排空中' : '候选版本验证模式'"
      :description="update?.runtime_mode === 'draining'
        ? 'Scheduler 已停止创建任务，Worker 将等待当前任务自然完成。'
        : 'Provider 副作用已关闭，仅保留健康检查、登录和更新状态查询。'"
      type="warning"
      :closable="false"
      show-icon
      class="section-gap"
    />

    <div class="version-grid" v-loading="loading && !update">
      <el-card shadow="never" class="version-card current-card">
        <div class="version-card__header">
          <span class="version-icon"><img src="/mediasync-logo.svg" alt="" /></span>
          <el-tag :type="statusPresentation.type" effect="light">
            {{ statusPresentation.label }}
          </el-tag>
        </div>
        <span class="card-eyebrow">当前运行版本</span>
        <strong class="version-number">{{ update?.current_version ?? '—' }}</strong>
        <div class="meta-line">
          <span>更新频道</span>
          <b>{{ update?.channel === 'rc' ? '候选版本（RC）' : '稳定版本' }}</b>
        </div>
        <div class="meta-line">
          <span>最近检查</span>
          <b>{{ formatDateTime(update?.checked_at) }}</b>
        </div>
      </el-card>

      <el-card shadow="never" class="version-card latest-card">
        <div class="version-card__header">
          <span class="release-icon"><AppIcon name="cloud" :size="22" /></span>
          <a
            v-if="update?.latest_release"
            :href="update.latest_release.release_url"
            target="_blank"
            rel="noopener noreferrer"
            class="release-link"
          >
            GitHub 发布页 <AppIcon name="external" :size="14" />
          </a>
        </div>
        <span class="card-eyebrow">官方最新版本</span>
        <strong class="version-number">
          {{ update?.latest_release?.tag_name ?? '等待检查' }}
        </strong>
        <div class="meta-line">
          <span>发布时间</span>
          <b>{{ formatDateTime(update?.latest_release?.published_at) }}</b>
        </div>
        <div class="meta-line">
          <span>版本类型</span>
          <b>{{ update?.latest_release?.prerelease ? '候选版本' : '稳定版本' }}</b>
        </div>
      </el-card>
    </div>

    <div class="system-grid">
      <el-card shadow="never" class="guide-card">
        <template #header>
          <div class="card-heading">
            <div>
              <h2>容器升级指引</h2>
              <p>现阶段请通过 NAS 容器管理器完成升级</p>
            </div>
            <el-tag type="info" effect="plain">手动升级</el-tag>
          </div>
        </template>
        <el-alert
          title="一键安装尚未启用"
          :description="update?.install_unavailable_reason"
          type="info"
          :closable="false"
          show-icon
        />
        <ol class="upgrade-steps">
          <li><span>1</span><div><strong>拉取新镜像</strong><p>使用下方官方镜像标签，避免来源不明的镜像。</p></div></li>
          <li><span>2</span><div><strong>停止并重建容器</strong><p>保持原有环境变量、端口映射和网络设置。</p></div></li>
          <li><span>3</span><div><strong>保留数据目录</strong><p>必须继续挂载原来的 {{ update?.manual_upgrade.data_path ?? '/data' }}，不要创建新的空目录。</p></div></li>
        </ol>
        <div class="command-box">
          <code>{{ pullCommand || '等待获取官方镜像信息' }}</code>
          <el-button text :disabled="!pullCommand" @click="copyPullCommand">
            <AppIcon name="copy" :size="15" />复制
          </el-button>
        </div>
        <p class="guide-note">{{ update?.manual_upgrade.message }}</p>
      </el-card>

      <el-card shadow="never" class="capability-card">
        <template #header>
          <div class="card-heading">
            <div>
              <h2>更新能力</h2>
              <p>当前部署可用的更新功能</p>
            </div>
          </div>
        </template>
        <div class="capability-row">
          <span class="capability-state success"><AppIcon name="shield" :size="17" /></span>
          <div><strong>官方版本检查</strong><p>已启用，结果会短时缓存以减少 GitHub 请求。</p></div>
          <el-tag type="success" effect="light">可用</el-tag>
        </div>
        <div class="capability-row">
          <span class="capability-state" :class="{ success: update?.docker_capability.reason_code === 'ready' }"><AppIcon name="settings" :size="17" /></span>
          <div><strong>Docker 环境</strong><p>{{ update?.docker_capability.message ?? '正在探测 Docker 更新能力' }}</p></div>
          <el-tag :type="update?.docker_capability.reason_code === 'ready' ? 'success' : 'info'" effect="light">
            {{ update?.docker_capability.reason_code === 'ready' ? '已识别' : '未就绪' }}
          </el-tag>
        </div>
        <div class="capability-row">
          <span class="capability-state"><AppIcon name="refresh" :size="17" /></span>
          <div><strong>界面一键更新</strong><p>将在后续独立阶段实现，并提供失败回滚保护。</p></div>
          <el-tag type="info" effect="light">规划中</el-tag>
        </div>
      </el-card>
    </div>

    <el-card v-if="update?.latest_release?.notes" shadow="never" class="release-notes">
      <template #header>
        <div class="card-heading">
          <div><h2>版本说明</h2><p>{{ update.latest_release.tag_name }} 官方发布摘要</p></div>
        </div>
      </template>
      <pre>{{ update.latest_release.notes }}</pre>
    </el-card>
  </section>
</template>

<style scoped>
.version-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 18px; margin-bottom: 18px; min-height: 250px; }
.version-card { overflow: hidden; }
.version-card :deep(.el-card__body) { min-height: 250px; padding: 24px; display: flex; flex-direction: column; }
.current-card { background: linear-gradient(145deg, #fff, #f7f7ff); }
.latest-card { background: linear-gradient(145deg, #fff, #f3f8ff); }
.version-card__header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 28px; }
.version-icon, .release-icon { width: 46px; height: 46px; display: grid; place-items: center; border-radius: 13px; }
.version-icon img { width: 46px; height: 46px; border-radius: inherit; }
.release-icon { color: #2563eb; background: #dbeafe; }
.card-eyebrow { color: #98a2b3; font-size: 11px; font-weight: 700; letter-spacing: .08em; }
.version-number { margin: 7px 0 20px; color: #101828; font-size: 30px; line-height: 1; letter-spacing: -.035em; }
.meta-line { padding: 9px 0; display: flex; align-items: center; justify-content: space-between; gap: 16px; border-top: 1px solid #edf0f4; font-size: 12px; }
.meta-line span { color: #667085; }
.meta-line b { color: #344054; text-align: right; }
.release-link { display: inline-flex; align-items: center; gap: 5px; color: #4f46e5; font-size: 12px; text-decoration: none; }
.release-link:hover { color: #3730a3; }
.system-grid { display: grid; grid-template-columns: minmax(0, 1.25fr) minmax(340px, .75fr); gap: 18px; margin-bottom: 18px; }
.card-heading { display: flex; align-items: center; justify-content: space-between; gap: 16px; }
.card-heading h2 { margin: 0 0 4px; color: #101828; font-size: 16px; }
.card-heading p { margin: 0; color: #98a2b3; font-size: 11px; }
.upgrade-steps { padding: 6px 0; margin: 17px 0; list-style: none; }
.upgrade-steps li { display: flex; align-items: flex-start; gap: 12px; padding: 10px 0; }
.upgrade-steps li > span { width: 25px; height: 25px; display: grid; place-items: center; flex: none; color: #4338ca; border-radius: 8px; background: #eef2ff; font-size: 11px; font-weight: 700; }
.upgrade-steps strong, .upgrade-steps p { display: block; }
.upgrade-steps strong { color: #344054; font-size: 13px; }
.upgrade-steps p { margin: 4px 0 0; color: #98a2b3; font-size: 11px; line-height: 1.6; }
.command-box { min-width: 0; padding: 10px 12px; display: flex; align-items: center; gap: 10px; border: 1px solid #d0d5dd; border-radius: 10px; background: #101828; }
.command-box code { min-width: 0; flex: 1; color: #d1fadf; overflow-x: auto; white-space: nowrap; font-size: 11px; }
.command-box .el-button { color: #c7d2fe; }
.guide-note { margin: 10px 0 0; color: #667085; font-size: 11px; }
.capability-row { display: grid; grid-template-columns: 36px minmax(0, 1fr) auto; gap: 11px; align-items: center; padding: 17px 0; border-bottom: 1px solid #edf0f4; }
.capability-row:last-child { border-bottom: 0; }
.capability-state { width: 34px; height: 34px; display: grid; place-items: center; color: #667085; border-radius: 9px; background: #f2f4f7; }
.capability-state.success { color: #067647; background: #dcfae6; }
.capability-row strong { color: #344054; font-size: 12px; }
.capability-row p { margin: 4px 0 0; color: #98a2b3; font-size: 10px; line-height: 1.5; }
.release-notes pre { margin: 0; color: #475467; white-space: pre-wrap; overflow-wrap: anywhere; font: 12px/1.75 ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; }
@media (max-width: 1100px) { .system-grid { grid-template-columns: 1fr; } }
@media (max-width: 720px) {
  .version-grid { grid-template-columns: 1fr; }
  .version-card :deep(.el-card__body) { min-height: 230px; }
  .capability-row { grid-template-columns: 36px minmax(0, 1fr); }
  .capability-row .el-tag { grid-column: 2; justify-self: start; }
}
</style>
