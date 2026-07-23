<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { useRouter } from 'vue-router'
import { api } from '../api/client'
import AppIcon from '../components/AppIcon.vue'
import { formatDateTime, formatRelativeTime, statusLabel, statusType, taskMessage, taskTypeLabels } from '../utils/display'

interface RecentTask { id: number; type: string; status: string; message: string | null; subscription_name: string | null; created_at: string }
interface DashboardSummary {
  subscriptions: number; active_subscriptions: number; pending_files: number; saved_files: number
  saved_today: number; failed_tasks: number; running_tasks: number
  last_scanned_at: string | null; last_full_scanned_at: string | null; next_scan_at: string | null
  folder_checkpoints: number
  request_guard: { interval_seconds: number; jitter_seconds: number; max_retries: number; schedule_jitter_seconds: number; folder_scan_batch_size: number; full_scan_interval_hours: number }
  recent_tasks: RecentTask[]
}
const router = useRouter()
const summary = reactive<DashboardSummary>({
  subscriptions: 0, active_subscriptions: 0, pending_files: 0, saved_files: 0,
  saved_today: 0, failed_tasks: 0, running_tasks: 0, last_scanned_at: null, last_full_scanned_at: null, next_scan_at: null,
  folder_checkpoints: 0,
  request_guard: { interval_seconds: 0, jitter_seconds: 0, max_retries: 0, schedule_jitter_seconds: 0, folder_scan_batch_size: 0, full_scan_interval_hours: 0 }, recent_tasks: [],
})
const loading = ref(false)
const healthy = computed(() => summary.failed_tasks === 0)
let timer: number | undefined

async function load(showError = false) {
  loading.value = true
  try {
    Object.assign(summary, await api<DashboardSummary>('/dashboard/summary'))
  } catch (error) {
    if (showError) ElMessage.error(error instanceof Error ? error.message : '仪表盘加载失败')
  } finally {
    loading.value = false
  }
}
onMounted(() => { load(); timer = window.setInterval(() => load(), 15000) })
onUnmounted(() => { if (timer) window.clearInterval(timer) })
</script>

<template>
  <section>
    <div class="dashboard-hero">
      <div class="hero-copy">
        <span class="eyebrow">MEDIA AUTOMATION CONTROL CENTER</span>
        <h1>家庭影音同步中心</h1>
        <p>从资源分享发现到个人云盘转存，所有同步状态一目了然。</p>
        <div class="hero-actions">
          <el-button type="primary" color="#fff" @click="router.push('/subscriptions')"><AppIcon name="subscription" :size="16" />管理订阅</el-button>
          <el-button plain @click="router.push('/tasks')">查看任务日志</el-button>
        </div>
      </div>
      <div class="hero-state">
        <div class="health-pill" :class="{ warning: !healthy }"><i />{{ healthy ? '服务运行正常' : `${summary.failed_tasks} 个任务需要处理` }}</div>
        <small>数据每 15 秒自动刷新</small>
      </div>
    </div>

    <div class="stats-grid">
      <el-card shadow="never" class="stat-card">
        <div class="stat-head"><span>启用订阅</span><i class="stat-icon indigo"><AppIcon name="subscription" :size="18" /></i></div>
        <strong>{{ summary.active_subscriptions }}<small>/ {{ summary.subscriptions }}</small></strong><em>正在定时监控</em>
      </el-card>
      <el-card shadow="never" class="stat-card">
        <div class="stat-head"><span>等待转存</span><i class="stat-icon amber"><AppIcon name="cloud" :size="18" /></i></div>
        <strong>{{ summary.pending_files }}</strong><em>{{ summary.running_tasks }} 个任务在队列中</em>
      </el-card>
      <el-card shadow="never" class="stat-card">
        <div class="stat-head"><span>累计转存</span><i class="stat-icon green"><AppIcon name="files" :size="18" /></i></div>
        <strong>{{ summary.saved_files }}</strong><em>今日新增 {{ summary.saved_today }}</em>
      </el-card>
      <el-card shadow="never" class="stat-card">
        <div class="stat-head"><span>失败任务</span><i class="stat-icon red"><AppIcon name="tasks" :size="18" /></i></div>
        <strong :class="{ danger: summary.failed_tasks }">{{ summary.failed_tasks }}</strong><em>{{ summary.failed_tasks ? '请尽快查看任务日志' : '暂无需要处理的异常' }}</em>
      </el-card>
    </div>

    <div class="dashboard-grid">
      <el-card shadow="never" class="activity-card" v-loading="loading">
        <template #header><div class="card-header"><div><h2>最近任务</h2><p>扫描与转存的最新执行结果</p></div><el-button link type="primary" @click="load(true)"><AppIcon name="refresh" :size="15" />刷新</el-button></div></template>
        <div class="table-wrap">
          <el-table :data="summary.recent_tasks" height="348" empty-text=" ">
            <el-table-column label="任务" width="110"><template #default="scope"><span class="task-kind">{{ taskTypeLabels[scope.row.type] ?? scope.row.type }}</span></template></el-table-column>
            <el-table-column prop="subscription_name" label="订阅" min-width="120" />
            <el-table-column label="状态" width="90"><template #default="scope"><el-tag size="small" effect="light" :type="statusType(scope.row.status)">{{ statusLabel(scope.row.status) }}</el-tag></template></el-table-column>
            <el-table-column label="执行结果" min-width="260" show-overflow-tooltip><template #default="scope">{{ taskMessage(scope.row.message) }}</template></el-table-column>
            <el-table-column label="时间" width="120"><template #default="scope"><el-tooltip :content="formatDateTime(scope.row.created_at)">{{ formatRelativeTime(scope.row.created_at) }}</el-tooltip></template></el-table-column>
          </el-table>
        </div>
        <div v-if="!summary.recent_tasks.length && !loading" class="empty-state"><AppIcon name="tasks" :size="30" /><h3>还没有任务记录</h3><p>添加订阅并开始扫描后，执行记录会显示在这里。</p></div>
      </el-card>
      <div class="side-stack">
        <el-card shadow="never" class="schedule-card">
          <div class="mini-card-title"><span class="summary-icon"><AppIcon name="tasks" :size="17" /></span><div><h2>扫描计划</h2><p>订阅轮询与完整校验</p></div></div>
          <div class="schedule-row"><span>下次扫描</span><strong>{{ formatRelativeTime(summary.next_scan_at) }}</strong></div>
          <div class="schedule-row"><span>最近轮询</span><strong>{{ formatRelativeTime(summary.last_scanned_at) }}</strong></div>
          <div class="schedule-row"><span>完整校验</span><strong>{{ formatRelativeTime(summary.last_full_scanned_at) }}</strong></div>
        </el-card>
        <el-card shadow="never" class="guard-card">
          <div class="mini-card-title"><span class="summary-icon guard"><AppIcon name="shield" :size="18" /></span><div><h2>API 请求防护</h2><p>节流、目录分批、周期校验</p></div></div>
          <div class="guard-stats"><span><b>{{ summary.request_guard.interval_seconds }}s</b>最小间隔</span><span><b>{{ summary.request_guard.folder_scan_batch_size }}</b>每轮目录</span><span><b>{{ summary.request_guard.full_scan_interval_hours }}h</b>完整校验</span></div>
          <div class="checkpoint-line"><span>目录检查点</span><b>{{ summary.folder_checkpoints }}</b></div>
        </el-card>
      </div>
    </div>

    <el-card shadow="never" class="flow-card">
      <div class="card-header"><div><h2>自动化链路</h2><p>MediaSync 负责分享监控与安全转存</p></div></div>
      <div class="flow">
        <span><i>01</i>资源分享</span><b>→</b><span><i>02</i>MediaSync</span><b>→</b><span><i>03</i>个人云盘</span><b>→</b><span><i>04</i>STRM / 媒体库</span>
      </div>
    </el-card>
  </section>
</template>

<style scoped>
.dashboard-hero { min-height: 188px; padding: 32px 36px; margin-bottom: 20px; border-radius: 18px; color: #fff; position: relative; overflow: hidden; background: radial-gradient(circle at 84% 5%, rgba(139,92,246,.92), transparent 34%), radial-gradient(circle at 58% 130%, rgba(59,130,246,.45), transparent 42%), linear-gradient(135deg, #172554, #312e81 58%, #4338ca); display: flex; align-items: center; justify-content: space-between; box-shadow: 0 18px 42px rgba(49,46,129,.16); }
.dashboard-hero::after { content: ""; position: absolute; width: 260px; height: 260px; right: -70px; bottom: -170px; border: 1px solid rgba(255,255,255,.18); border-radius: 50%; box-shadow: 0 0 0 35px rgba(255,255,255,.035), 0 0 0 70px rgba(255,255,255,.025); }
.hero-copy, .hero-state { position: relative; z-index: 1; }
.dashboard-hero h1 { margin: 8px 0; font-size: 31px; letter-spacing: -.04em; color: #fff; }
.dashboard-hero p { margin: 0; color: #dbeafe; font-size: 13px; }
.eyebrow { font-size: 10px; font-weight: 700; letter-spacing: .15em; color: #bfdbfe; }
.hero-actions { display: flex; gap: 10px; margin-top: 24px; }
.hero-actions .el-button { border-color: rgba(255,255,255,.24); }
.hero-state { display: grid; justify-items: end; gap: 10px; }
.hero-state small { color: #c7d2fe; font-size: 10px; }
.health-pill { display: flex; align-items: center; gap: 9px; padding: 10px 15px; border: 1px solid rgba(255,255,255,.22); border-radius: 999px; background: rgba(255,255,255,.12); backdrop-filter: blur(8px); }
.health-pill i { width: 8px; height: 8px; border-radius: 50%; background: #6ee7b7; box-shadow: 0 0 0 5px rgba(110,231,183,.15); }
.health-pill.warning i { background: #fda4af; }
.stat-card :deep(.el-card__body) { padding: 19px; }
.stat-head { display: flex; align-items: center; justify-content: space-between; }
.stat-icon { width: 34px; height: 34px; display: grid; place-items: center; border-radius: 9px; }
.stat-icon.indigo { color: #4f46e5; background: #eef2ff; }.stat-icon.amber { color: #d97706; background: #fffbeb; }.stat-icon.green { color: #039855; background: #ecfdf3; }.stat-icon.red { color: #d92d20; background: #fef3f2; }
.stat-card em { font-size: 11px; color: #98a2b3; font-style: normal; }
.stat-card strong small { margin-left: 5px; font-size: 14px; color: #98a2b3; font-weight: 500; }
.dashboard-grid { display: grid; grid-template-columns: minmax(0, 1.7fr) minmax(320px, .72fr); gap: 18px; margin-bottom: 18px; }
.side-stack { display: grid; gap: 18px; }
.card-header { display: flex; align-items: center; justify-content: space-between; }
.card-header h2, .schedule-card h2, .guard-card h2 { margin: 0 0 4px; font-size: 16px; color: #101828; }
.card-header p, .mini-card-title p { margin: 0; color: #98a2b3; font-size: 11px; }
.mini-card-title { display: flex; gap: 11px; align-items: center; margin-bottom: 13px; }
.summary-icon.guard { color: #039855; background: #dcfae6; }
.schedule-row { display: flex; justify-content: space-between; gap: 12px; padding: 13px 0; border-bottom: 1px solid #f0f2f5; }
.schedule-row:last-child { border-bottom: 0; }
.schedule-row span { color: #667085; font-size: 12px; }
.schedule-row strong { color: #344054; font-size: 12px; }
.guard-card { background: linear-gradient(145deg, #f5fdf8, #ecfdf3); border-color: #c7eed7; }
.guard-stats { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; margin-top: 18px; }
.guard-stats span { color: #667085; font-size: 9px; }
.guard-stats b { display: block; margin-bottom: 4px; color: #067647; font-size: 16px; }
.checkpoint-line { margin-top: 16px; padding-top: 13px; display: flex; align-items: center; justify-content: space-between; border-top: 1px solid #c7eed7; color: #47705a; font-size: 11px; }
.checkpoint-line b { color: #067647; font-size: 16px; }
.task-kind { font-size: 12px; color: #475467; font-weight: 500; }
.flow span { display: flex; align-items: center; gap: 9px; }
.flow i { font-size: 9px; font-style: normal; color: #6172f3; }
.empty-state .app-icon { margin: 0 auto; color: #c7cdd6; }
@media (max-width: 1250px) { .dashboard-grid { grid-template-columns: 1fr; } .side-stack { grid-template-columns: 1fr 1fr; } }
@media (max-width: 700px) {
  .dashboard-hero { padding: 26px 22px; align-items: flex-start; flex-direction: column; gap: 24px; }
  .hero-state { justify-items: start; }
  .side-stack { grid-template-columns: 1fr; }
  .activity-card :deep(.el-table__header th:nth-child(n+4)),
  .activity-card :deep(.el-table__body td:nth-child(n+4)) { display: none; }
}
</style>
