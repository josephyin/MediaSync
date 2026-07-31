export const statusLabels: Record<string, string> = {
  active: '正常',
  disabled: '已停用',
  pending: '等待中',
  running: '执行中',
  retry: '等待重试',
  waiting_credential: '等待凭证',
  cancel_requested: '正在取消',
  cancelled: '已取消',
  scanning: '扫描中',
  success: '成功',
  failed: '失败',
  error: '异常',
  discovered: '已发现',
  saving: '转存中',
  saved: '已转存',
}

export function statusLabel(value: string | null | undefined) {
  return value ? (statusLabels[value] ?? value) : '—'
}

export function statusType(value: string | null | undefined) {
  if (['active', 'success', 'saved'].includes(value ?? '')) return 'success'
  if (['failed', 'error'].includes(value ?? '')) return 'danger'
  if (['pending', 'running', 'retry', 'waiting_credential', 'cancel_requested', 'scanning', 'saving'].includes(value ?? '')) return 'warning'
  return 'info'
}

const isoDateTimeWithoutTimezone =
  /^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?$/

export function parseApiDateTime(value: string | null | undefined) {
  if (!value) return null
  const trimmed = value.trim()
  // SQLite 会丢失 timezone=True 的偏移信息；MediaSync 后端时间统一按 UTC 写入。
  // 对无时区的 API 时间补回 UTC 标识，再交给浏览器转换为用户本地时区。
  const normalized = isoDateTimeWithoutTimezone.test(trimmed)
    ? `${trimmed.replace(' ', 'T')}Z`
    : trimmed
  const date = new Date(normalized)
  return Number.isNaN(date.getTime()) ? null : date
}

export function formatDateTime(value: string | null | undefined) {
  if (!value) return '—'
  const date = parseApiDateTime(value)
  if (!date) return value
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit',
    hour12: false,
  }).format(date)
}

export function formatRelativeTime(value: string | null | undefined) {
  if (!value) return '暂无记录'
  const date = parseApiDateTime(value)
  if (!date) return value
  const seconds = Math.round((date.getTime() - Date.now()) / 1000)
  const formatter = new Intl.RelativeTimeFormat('zh-CN', { numeric: 'auto' })
  if (Math.abs(seconds) < 60) return formatter.format(seconds, 'second')
  const minutes = Math.round(seconds / 60)
  if (Math.abs(minutes) < 60) return formatter.format(minutes, 'minute')
  const hours = Math.round(minutes / 60)
  if (Math.abs(hours) < 24) return formatter.format(hours, 'hour')
  return formatter.format(Math.round(hours / 24), 'day')
}

export function formatFileSize(value: number | null | undefined) {
  if (value == null) return '—'
  if (value < 1024) return `${value} B`
  if (value < 1024 ** 2) return `${(value / 1024).toFixed(1)} KB`
  if (value < 1024 ** 3) return `${(value / 1024 ** 2).toFixed(1)} MB`
  if (value < 1024 ** 4) return `${(value / 1024 ** 3).toFixed(2)} GB`
  return `${(value / 1024 ** 4).toFixed(2)} TB`
}

export function scheduleLabel(value: string) {
  const match = /^interval:(\d+)([mh])$/.exec(value)
  if (!match) return value
  return match[2] === 'm' ? `每 ${match[1]} 分钟` : `每 ${match[1]} 小时`
}

export const taskTypeLabels: Record<string, string> = {
  scan: '订阅扫描',
  transfer: '文件转存',
}

export const triggerLabels: Record<string, string> = {
  scheduled: '定时',
  manual: '手动',
  retry: '重试',
}

export const driveTypeLabels: Record<string, string> = {
  default: '默认盘',
  resource: '资源库',
  backup: '备份盘',
  custom: '自定义盘',
}

export const openModeLabels: Record<string, string> = {
  alistgo: 'AListGo',
  openlist: 'OpenList',
  custom: '自有应用',
}

export function taskMessage(value: string | null | undefined) {
  if (!value) return '—'
  if (value.startsWith('Saved to ')) return `已转存至 ${value.slice('Saved to '.length)}`
  const discovered = /^Discovered (\d+) new items$/.exec(value)
  if (discovered) return `扫描完成：发现 ${discovered[1]} 个新增项目`
  if (value === 'Recovered after application restart') return '应用重启后已恢复，等待重新执行'
  return value
}
