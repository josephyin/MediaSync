<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api, type Page } from '../api/client'
import type { CloudAccount, ProviderInfo, SystemInfo } from '../api/types'
import AppIcon from '../components/AppIcon.vue'
import ProviderLogo from '../components/ProviderLogo.vue'
import { formatDateTime, formatRelativeTime, openModeLabels, statusLabel, statusType } from '../utils/display'
import { findProvider, providerAvailabilityLabel, providerName, supportsCapabilities } from '../utils/providers'

interface QrStart { session_id: string; qr_code_data_url: string; expires_in: number }
interface QrStatus { status: 'waiting' | 'scanned' | 'confirmed' | 'expired'; account?: CloudAccount }

const accounts = ref<CloudAccount[]>([])
const providers = ref<ProviderInfo[]>([])
const pageLoading = ref(false)
const verifyingId = ref<number | null>(null)
const openVerifyingId = ref<number | null>(null)
const dialog = ref(false)
const loading = ref(false)
const editingId = ref<number | null>(null)
const form = reactive({ provider: 'aliyundrive', name: '', refresh_token: '' })

const addDialog = ref(false)
const addStep = ref<1 | 2>(1)
const selectedProviderId = ref('')

type GuidedLoginState = 'available' | 'building'
type OpenApiRequirement = 'required' | 'optional' | 'none'
interface GuidedLoginOption {
  provider: string
  title: string
  description: string
  action: string
  state: GuidedLoginState
  icon: 'scan' | 'browser'
  privatePurpose: string
  openApi: OpenApiRequirement
  openApiPurpose?: string
  openApiModes?: string
}

const guidedLoginOptions: Record<string, GuidedLoginOption> = {
  aliyundrive: {
    provider: 'aliyundrive',
    title: '使用阿里云盘 App 扫码',
    description: '无需复制 Token，扫码确认后自动完成账号添加。',
    action: '开始扫码',
    state: 'available',
    icon: 'scan',
    privatePurpose: '读取分享并执行转存',
    openApi: 'optional',
    openApiPurpose: '识别默认盘、资源库与备份盘',
    openApiModes: 'AListGo、OpenList 或自有应用',
  },
  quark: {
    provider: 'quark',
    title: '使用夸克 App 扫码',
    description: '登录成功后由 MediaSync 自动接收并安全保存登录凭证。',
    action: '开始扫码',
    state: 'available',
    icon: 'scan',
    privatePurpose: '读取分享、浏览账号盘、查重、建目录并执行转存',
    openApi: 'optional',
    openApiPurpose: '备用的官方账号盘访问通道',
    openApiModes: 'OpenList（需要 AppID 和 SignKey）',
  },
  pan123: {
    provider: 'pan123',
    title: '扫码登录 123 云盘',
    description: '自动完成网页会话登录，不再需要查找 authorToken。',
    action: '开始扫码',
    state: 'available',
    icon: 'scan',
    privatePurpose: '读取分享、浏览账号盘并执行转存',
    openApi: 'optional',
    openApiPurpose: '高级方式：使用自己的开放平台应用访问账号盘',
    openApiModes: '仅自有开放平台应用',
  },
  baidu: {
    provider: 'baidu',
    title: '使用百度网盘 App 扫码',
    description: '自动完成分享会话登录，不再需要从浏览器查找 BDUSS。',
    action: '开始扫码',
    state: 'available',
    icon: 'scan',
    privatePurpose: '读取分享并执行转存',
    openApi: 'required',
    openApiPurpose: '浏览账号盘、查重并自动创建目录',
    openApiModes: 'AListGo、OpenList 或自有应用',
  },
}

const openDialog = ref(false)
const openLoading = ref(false)
const openAccount = ref<CloudAccount | null>(null)
const aliyunHostedTokenUrls = {
  alistgo: 'https://api.alistgo.com/alist/ali_open/token',
  openlist: 'https://api.oplist.org.cn/alicloud/renewapi',
} as const
const quarkOpenListTokenUrl = 'https://api.oplist.org/quarkyun/renewapi'
const baiduOpenListTokenUrl = 'https://api.oplist.org/baiduyun/renewapi'
const baiduAlistGoAuthorizeUrl = 'https://openapi.baidu.com/oauth/2.0/authorize?response_type=code&client_id=hq9yQ9w9kR4YHj1kyYafLygVocobh7Sf&redirect_uri=https%3A%2F%2Falistgo.com%2Ftool%2Fbaidu%2Fcallback&scope=basic%2Cnetdisk&qrcode=1'
const aliyunAlistGoAuthorizeUrl = 'https://alistgo.com/zh/tool/aliyundrive/request.html'
type OpenAuthMode = 'alistgo' | 'openlist' | 'custom'
interface OpenModeOption {
  value: OpenAuthMode
  label: string
  description: string
  recommended?: boolean
}
const openForm = reactive({
  mode: 'alistgo' as OpenAuthMode,
  refresh_token: '',
  token_url: aliyunHostedTokenUrls.alistgo as string,
  client_id: '',
  client_secret: '',
})

const qrDialog = ref(false)
const qrLoading = ref(false)
const qrImage = ref('')
const qrStatus = ref('请填写账号名称并生成二维码')
type QrProviderId = 'aliyundrive' | 'quark' | 'pan123' | 'baidu'
const qrProviderId = ref<QrProviderId>('aliyundrive')
const qrForm = reactive<{ account_id: number | null; name: string }>({ account_id: null, name: '' })
let qrTimer: number | undefined
let qrSessionId = ''
let qrPollDelay = 2000

const activeCount = computed(() => accounts.value.filter((item) => item.status === 'active').length)
const openCount = computed(() => accounts.value.filter((item) => item.open_auth_mode).length)
const enabledProviders = computed(() => providers.value.filter((provider) => provider.enabled))
const selectedProvider = computed(() => providerInfo(selectedProviderId.value))
const selectedGuidedOption = computed(() => guidedLoginOptions[selectedProviderId.value])
const selectedAuthorizationSummary = computed(() => {
  if (selectedGuidedOption.value?.openApi === 'required') return '完整接入需要完成 2 项授权'
  if (selectedGuidedOption.value?.openApi === 'optional') return '1 项必需授权，另有 1 项可选增强'
  return '完整接入只需完成 1 项登录'
})
const qrProviderLabel = computed(() => providerName(providers.value, qrProviderId.value))
const openListAuthorizationPage = computed(() => openForm.token_url.includes('api-cn.oplist.org') ? 'https://api-cn.oplist.org' : 'https://api.oplist.org')
const availableOpenModes = computed<OpenModeOption[]>(() => {
  if (openAccount.value?.provider === 'quark') return [{
    value: 'openlist',
    label: 'OpenList APIPages',
    description: '使用 OpenList 获取 Refresh Token，同时填写匹配的 AppID 和 SignKey。',
  }]
  if (openAccount.value?.provider === 'pan123') return [
    {
      value: 'custom',
      label: '自有 OpenAPI 应用',
      description: '使用你在 123 开放平台申请的 Client ID 和 Client Secret。',
    },
  ]
  return [
    {
      value: 'alistgo',
      label: 'AListGo 授权',
      description: '使用 AListGo 公开应用获取 Refresh Token，步骤最少。',
      recommended: true,
    },
    {
      value: 'openlist',
      label: 'OpenList APIPages',
      description: '使用 OpenList 授权工具，可选择公开或自建刷新节点。',
    },
    {
      value: 'custom',
      label: '自有 OpenAPI 应用',
      description: '使用自己的 Client ID、Client Secret 和对应 Refresh Token。',
    },
  ]
})
const openProviderGuide = computed(() => {
  switch (openAccount.value?.provider) {
    case 'aliyundrive':
      return {
        requirement: '可选增强',
        tagType: 'info' as const,
        privateCoverage: '分享监控与转存',
        openCoverage: '识别默认盘、资源库和备份盘',
        verification: '保存时会校验私有登录与 OpenAPI 是否属于同一账号。',
      }
    case 'quark':
      return {
        requirement: '可跳过',
        tagType: 'info' as const,
        privateCoverage: '分享读取、账号盘浏览、查重、建目录与转存',
        openCoverage: '备用的官方账号盘访问通道',
        verification: '扫码 Cookie 已覆盖完整流程，没有 AppID/SignKey 时无需绑定。',
      }
    case 'pan123':
      return {
        requirement: '高级选项',
        tagType: 'info' as const,
        privateCoverage: '分享读取、账号盘浏览、建目录与转存',
        openCoverage: '使用自有开放平台应用访问账号盘',
        verification: '普通用户使用扫码登录即可；这里只面向已有开放平台应用的用户。',
      }
    default:
      return {
        requirement: '完整功能所需',
        tagType: 'warning' as const,
        privateCoverage: '分享读取与转存',
        openCoverage: '账号盘浏览、查重与自动创建目录',
        verification: '保存时会校验 BDUSS 与 OpenAPI 是否属于同一百度账号。',
      }
  }
})
const selectedOpenMode = computed(() => availableOpenModes.value.find((item) => item.value === openForm.mode))
const openListDriverName = computed(() => {
  if (openAccount.value?.provider === 'quark') return '夸克网盘 (OAuth2) 验证登录'
  if (openAccount.value?.provider === 'baidu') return '百度网盘 验证登录'
  return '阿里云盘 (OAuth2) 扫码登录'
})
const alistGoAuthorizationUrl = computed(() => openAccount.value?.provider === 'baidu' ? baiduAlistGoAuthorizeUrl : aliyunAlistGoAuthorizeUrl)
const hostedCredentialNotice = computed(() => {
  if (openForm.mode === 'custom') return '凭证只会发送给对应网盘的官方 OpenAPI。'
  if (openForm.mode === 'alistgo' && openAccount.value?.provider === 'baidu') {
    return 'Refresh Token 由 AListGo 公开应用签发，MediaSync 使用同一组公开应用参数在本机续期。'
  }
  return 'Refresh Token 会发送到所选托管刷新服务；不同服务签发的 Token 不能混用。'
})
const qrAppLabel = computed(() => {
  if (qrProviderId.value === 'quark') return '夸克 App'
  if (qrProviderId.value === 'pan123') return '微信或 123 云盘 App'
  if (qrProviderId.value === 'baidu') return '百度网盘 App'
  return '阿里云盘 App'
})

function authorizationSummary(providerId: string) {
  const option = guidedLoginOptions[providerId]
  if (!option) return '查看可用登录方式'
  if (option.openApi === 'required') return '需要私有登录 + OpenAPI'
  if (option.openApi === 'optional') return '私有登录必需 · OpenAPI 可选'
  return '仅需完成账号登录'
}
function authorizationCountLabel(providerId: string) {
  const requirement = guidedLoginOptions[providerId]?.openApi
  if (requirement === 'required') return '2 项必需'
  if (requirement === 'optional') return '1 必需 + 1 可选'
  return '1 项必需'
}

function providerInfo(providerId: string) { return findProvider(providers.value, providerId) }
function supportsQrLogin(account: CloudAccount) { return ['aliyundrive', 'quark', 'pan123', 'baidu'].includes(account.provider) }
function supportsOpenApi(account: CloudAccount) { return ['aliyundrive', 'quark', 'pan123', 'baidu'].includes(account.provider) }
function openProviderName(account: CloudAccount | null) {
  return account ? `${providerName(providers.value, account.provider)} OpenAPI` : 'OpenAPI'
}
function defaultOpenMode(account: CloudAccount) {
  if (account.provider === 'quark') return 'openlist'
  if (account.provider === 'pan123') return 'custom'
  return 'alistgo'
}
function defaultOpenTokenUrl(account: CloudAccount, mode: OpenAuthMode) {
  if (account.provider === 'quark') return quarkOpenListTokenUrl
  if (account.provider === 'pan123') return ''
  if (account.provider === 'baidu') return mode === 'openlist' ? baiduOpenListTokenUrl : ''
  return mode === 'custom' ? '' : aliyunHostedTokenUrls[mode]
}
const accountCredentialLabel = computed(() => {
  const suffix = editingId.value ? '（留空表示不修改）' : ''
  if (['quark', 'baidu'].includes(form.provider)) return `Cookie${suffix}`
  if (form.provider === 'pan123') return `Access Token${suffix}`
  return `Refresh Token${suffix}`
})
function canVerify(account: CloudAccount) {
  return supportsCapabilities(providerInfo(account.provider), ['account_verify'])
}

async function load() {
  pageLoading.value = true
  try {
    const [accountPage, system] = await Promise.all([
      api<Page<CloudAccount>>('/cloud-accounts?page_size=100'),
      api<SystemInfo>('/system/info'),
    ])
    accounts.value = accountPage.items
    providers.value = system.providers
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '云盘账号加载失败')
  } finally {
    pageLoading.value = false
  }
}
function openAdd() {
  addStep.value = 1
  selectedProviderId.value = enabledProviders.value[0]?.id ?? ''
  addDialog.value = true
}
function selectProvider(providerId: string) {
  selectedProviderId.value = providerId
  addStep.value = 2
}
function backToProviders() {
  addStep.value = 1
}
function openManualAdd(providerId = selectedProviderId.value) {
  editingId.value = null
  Object.assign(form, {
    provider: providerId || enabledProviders.value[0]?.id || '',
    name: '',
    refresh_token: '',
  })
  addDialog.value = false
  dialog.value = true
}
function startGuidedLogin() {
  const option = selectedGuidedOption.value
  if (!option || option.state !== 'available') return
  addDialog.value = false
  if (['aliyundrive', 'quark', 'pan123', 'baidu'].includes(option.provider)) {
    openQr(undefined, option.provider as QrProviderId)
  }
}
function openEdit(account: CloudAccount) {
  editingId.value = account.id
  Object.assign(form, { provider: account.provider, name: account.name, refresh_token: '' })
  dialog.value = true
}
async function save() {
  const selectedProvider = providerInfo(form.provider)
  if (!selectedProvider?.enabled) {
    ElMessage.warning('所选 Provider 尚未启用')
    return
  }
  loading.value = true
  try {
    if (editingId.value) {
      const body: { name: string; refresh_token?: string } = { name: form.name }
      if (form.refresh_token.trim()) body.refresh_token = form.refresh_token.trim()
      await api(`/cloud-accounts/${editingId.value}`, { method: 'PATCH', body: JSON.stringify(body) })
      ElMessage.success('账号已更新')
    } else {
      await api('/cloud-accounts', { method: 'POST', body: JSON.stringify({ ...form, refresh_token: form.refresh_token.trim() }) })
      ElMessage.success('账号已添加')
    }
    dialog.value = false
    await load()
  } catch (error) { ElMessage.error(error instanceof Error ? error.message : '保存失败') }
  finally { loading.value = false }
}
async function verify(id: number) {
  verifyingId.value = id
  try { await api(`/cloud-accounts/${id}/verify`, { method: 'POST' }); ElMessage.success('校验成功') }
  catch (error) { ElMessage.error(error instanceof Error ? error.message : '校验失败') }
  finally { verifyingId.value = null }
  await load()
}
async function remove(id: number) {
  try {
    await ElMessageBox.confirm('确定删除这个云盘账号吗？', '确认删除', { type: 'warning', confirmButtonText: '确认删除', cancelButtonText: '取消' })
    await api(`/cloud-accounts/${id}`, { method: 'DELETE' })
    ElMessage.success('账号已删除')
    await load()
  } catch (error) {
    if (error === 'cancel' || error === 'close') return
    ElMessage.error(error instanceof Error ? error.message : '删除失败')
  }
}

function openOpenApi(account: CloudAccount) {
  openAccount.value = account
  const mode = account.provider === 'pan123' ? 'custom' : (account.open_auth_mode ?? defaultOpenMode(account))
  Object.assign(openForm, {
    mode,
    refresh_token: '',
    token_url: account.open_token_url ?? defaultOpenTokenUrl(account, mode),
    client_id: account.open_client_id ?? '',
    client_secret: '',
  })
  openDialog.value = true
}
function openModeChanged(mode: OpenAuthMode) {
  if (openAccount.value) {
    openForm.token_url = defaultOpenTokenUrl(openAccount.value, mode)
  }
}
async function saveOpenApi() {
  if (!openAccount.value) return
  openLoading.value = true
  let configured = false
  try {
    const body: Record<string, string> = { mode: openForm.mode }
    if (openForm.refresh_token.trim()) body.refresh_token = openForm.refresh_token.trim()
    if (openForm.mode !== 'custom' && openForm.token_url.trim()) body.token_url = openForm.token_url.trim()
    if (openForm.mode === 'custom' || openAccount.value.provider === 'quark') {
      if (openForm.client_id.trim()) body.client_id = openForm.client_id.trim()
      if (openForm.client_secret.trim()) body.client_secret = openForm.client_secret.trim()
    }
    await api(`/cloud-accounts/${openAccount.value.id}/open-credential`, { method: 'PUT', body: JSON.stringify(body) })
    configured = true
    // Hosted brokers can rotate refresh tokens during verification. Clear the
    // pasted value so a retry cannot overwrite the newly persisted token.
    openForm.refresh_token = ''
    await api(`/cloud-accounts/${openAccount.value.id}/open-credential/verify`, { method: 'POST' })
    ElMessage.success('OpenAPI 已绑定并校验成功')
    openDialog.value = false
  } catch (error) {
    const message = error instanceof Error ? error.message : 'OpenAPI 绑定失败'
    ElMessage.error(configured ? `配置已保存，但校验失败：${message}` : message)
  } finally {
    openLoading.value = false
    await load()
    if (openAccount.value) {
      openAccount.value = accounts.value.find((item) => item.id === openAccount.value?.id) ?? openAccount.value
    }
  }
}
async function importOpenTokenFromClipboard() {
  try {
    const value = (await navigator.clipboard.readText()).trim()
    if (!value) { ElMessage.warning('剪贴板中没有可导入的 Token'); return }
    openForm.refresh_token = value
    ElMessage.success('已从剪贴板读取 Open Refresh Token')
  } catch {
    ElMessage.warning('浏览器未允许读取剪贴板，请直接粘贴到 Token 输入框')
  }
}
async function verifyOpenApi(account: CloudAccount) {
  openVerifyingId.value = account.id
  try {
    await api(`/cloud-accounts/${account.id}/open-credential/verify`, { method: 'POST' })
    ElMessage.success('OpenAPI 校验成功')
  } catch (error) { ElMessage.error(error instanceof Error ? error.message : 'OpenAPI 校验失败') }
  finally { openVerifyingId.value = null }
  await load()
}
async function unbindOpenApi() {
  if (!openAccount.value) return
  try {
    await ElMessageBox.confirm('解绑只会删除 MediaSync 中保存的 OpenAPI 凭证，不影响私有接口账号。是否继续？', '解绑 OpenAPI', { type: 'warning', confirmButtonText: '确认解绑', cancelButtonText: '取消' })
    await api(`/cloud-accounts/${openAccount.value.id}/open-credential`, { method: 'DELETE' })
    ElMessage.success('OpenAPI 已解绑')
    openDialog.value = false
    await load()
  } catch (error) {
    if (error === 'cancel' || error === 'close') return
    ElMessage.error(error instanceof Error ? error.message : '解绑失败')
  }
}

function stopQrPolling() {
  if (qrTimer) window.clearTimeout(qrTimer)
  qrTimer = undefined
  qrPollDelay = 2000
}
function scheduleQrPolling(delay = qrPollDelay) {
  if (qrTimer) window.clearTimeout(qrTimer)
  qrPollDelay = delay
  qrTimer = window.setTimeout(() => { void pollQr() }, delay)
}
function openQr(account?: CloudAccount, providerId?: QrProviderId) {
  stopQrPolling()
  qrSessionId = ''
  qrImage.value = ''
  qrProviderId.value = account && ['aliyundrive', 'quark', 'pan123', 'baidu'].includes(account.provider)
    ? account.provider as QrProviderId
    : (providerId ?? 'aliyundrive')
  qrStatus.value = account ? `生成二维码后使用${qrAppLabel.value}扫码` : '请填写账号名称并生成二维码'
  Object.assign(qrForm, { account_id: account?.id ?? null, name: account?.name ?? '' })
  qrDialog.value = true
}
async function pollQr() {
  if (!qrSessionId) return
  const pollingSessionId = qrSessionId
  try {
    const result = await api<QrStatus>(`/${qrProviderId.value}/qr-login/${pollingSessionId}`)
    if (pollingSessionId !== qrSessionId) return
    if (result.status === 'scanned') {
      qrStatus.value = '已扫码，请在手机上确认登录'
      scheduleQrPolling(qrProviderId.value === 'pan123' ? 500 : 2000)
    }
    if (result.status === 'waiting') {
      qrStatus.value = '等待扫码…'
      scheduleQrPolling(2000)
    }
    if (result.status === 'expired') {
      qrStatus.value = '二维码已过期，请重新生成'
      stopQrPolling()
    }
    if (result.status === 'confirmed') {
      stopQrPolling()
      qrStatus.value = '登录成功，账号已保存'
      await load()
      const shouldOfferOpenApi = result.account && supportsOpenApi(result.account) && !result.account.open_auth_mode && !['quark', 'pan123'].includes(qrProviderId.value)
      ElMessage.success(shouldOfferOpenApi ? `${qrProviderLabel.value}私有登录成功，请选择 OpenAPI 授权方式` : `${qrProviderLabel.value}登录成功`)
      window.setTimeout(() => {
        qrDialog.value = false
        if (shouldOfferOpenApi && result.account) openOpenApi(result.account)
      }, 600)
    }
  } catch (error) {
    stopQrPolling()
    qrStatus.value = error instanceof Error ? error.message : '扫码状态查询失败'
    ElMessage.error(qrStatus.value)
  }
}
async function generateQr() {
  if (!qrForm.name.trim()) { ElMessage.warning('请填写账号名称'); return }
  qrLoading.value = true
  stopQrPolling()
  try {
    const result = await api<QrStart>(`/${qrProviderId.value}/qr-login/start`, {
      method: 'POST',
      body: JSON.stringify({ account_id: qrForm.account_id, name: qrForm.name.trim() }),
    })
    qrSessionId = result.session_id
    qrImage.value = result.qr_code_data_url
    qrStatus.value = `请使用${qrAppLabel.value}扫码并确认`
    scheduleQrPolling(2000)
  } catch (error) {
    qrStatus.value = error instanceof Error ? error.message : '二维码生成失败'
    ElMessage.error(qrStatus.value)
  } finally { qrLoading.value = false }
}

onMounted(load)
onUnmounted(stopQrPolling)
</script>

<template>
  <section>
    <div class="page-heading">
      <div><h1>云盘账号</h1><p>扫码或网页授权即可添加账号，MediaSync 会自动处理所需凭证</p></div>
      <div class="page-actions">
        <el-button type="primary" @click="openAdd"><AppIcon name="plus" />添加云盘账号</el-button>
      </div>
    </div>

    <div class="connection-banner">
      <span class="connection-banner__icon"><AppIcon name="scan" :size="22" /></span>
      <div><strong>推荐使用扫码或网页授权</strong><p>不需要打开开发者工具，也不需要复制 Cookie 或 Token。手工凭证仅作为高级备用方式。</p></div>
      <el-button text type="primary" @click="openAdd">选择云盘<AppIcon name="arrow" :size="15" /></el-button>
    </div>

    <div class="summary-strip">
      <div class="summary-item"><span class="summary-icon"><AppIcon name="cloud" /></span><div><strong>{{ accounts.length }}</strong><span>云盘账号</span></div></div>
      <div class="summary-item"><span class="summary-icon success"><AppIcon name="shield" /></span><div><strong>{{ activeCount }}</strong><span>私有接口正常</span></div></div>
      <div class="summary-item"><span class="summary-icon purple"><AppIcon name="database" /></span><div><strong>{{ openCount }}</strong><span>已绑定 OpenAPI</span></div></div>
    </div>

    <div v-loading="pageLoading" class="account-grid">
      <article v-for="account in accounts" :key="account.id" class="account-card">
        <header class="account-card__header">
          <div class="account-brand"><ProviderLogo class="account-logo" :provider="account.provider" :size="42" /><div><h3>{{ account.name }}</h3><p>{{ account.account_identity || '账号信息待校验' }}</p></div></div>
          <el-tag :type="statusType(account.status)" effect="light">{{ statusLabel(account.status) }}</el-tag>
        </header>

        <div class="account-facts">
          <div><span>服务商</span><strong>{{ providerName(providers, account.provider) }}</strong></div>
          <div><span>默认 Drive ID</span><strong class="monospace">{{ account.default_drive_id || '待获取' }}</strong></div>
          <div><span>最近校验</span><el-tooltip :content="formatDateTime(account.last_verified_at)"><strong>{{ formatRelativeTime(account.last_verified_at) }}</strong></el-tooltip></div>
        </div>

        <div v-if="supportsOpenApi(account)" class="open-panel" :class="{ connected: account.open_auth_mode }">
          <div class="open-panel__title">
            <div><ProviderLogo :provider="account.provider" :size="30" /><div><strong>{{ providerName(providers, account.provider) }} OpenAPI</strong><p>{{ account.provider === 'pan123' && account.open_auth_mode === 'openlist' ? '公共 OpenList 已停用，请改用自有应用或解绑' : account.open_auth_mode ? `${openModeLabels[account.open_auth_mode]} 授权` : account.provider === 'aliyundrive' ? '可选，用于识别默认盘、资源库与备份盘' : account.provider === 'quark' ? '可选；Cookie 已覆盖浏览、查重、建目录和转存' : '用于账号盘浏览、查重和目录能力' }}</p></div></div>
            <div class="open-panel__action">
              <el-tag v-if="account.open_status" size="small" :type="statusType(account.open_status)">{{ statusLabel(account.open_status) }}</el-tag>
              <span v-else class="muted">未绑定</span>
              <el-button text type="primary" size="small" @click="openOpenApi(account)">{{ account.open_auth_mode ? '编辑' : '选择授权方式' }}</el-button>
            </div>
          </div>
          <p v-if="account.open_account_identity" class="open-identity">{{ account.open_account_identity }}</p>
          <p v-if="account.last_error || account.open_last_error" class="account-error">{{ account.open_last_error || account.last_error }}</p>
        </div>

        <footer class="account-actions">
          <el-button type="primary" plain :disabled="!canVerify(account)" :loading="verifyingId === account.id" @click="verify(account.id)">校验账号</el-button>
          <el-button @click="openEdit(account)">编辑账号</el-button>
          <el-dropdown trigger="click">
            <el-button>更多<AppIcon name="arrow" /></el-button>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item v-if="supportsQrLogin(account)" @click="openQr(account)">重新扫码登录</el-dropdown-item>
                <el-dropdown-item v-if="supportsOpenApi(account)" @click="openOpenApi(account)">{{ account.open_auth_mode ? '编辑 OpenAPI' : '绑定 OpenAPI' }}</el-dropdown-item>
                <el-dropdown-item v-if="supportsOpenApi(account) && account.open_auth_mode" :disabled="openVerifyingId === account.id" @click="verifyOpenApi(account)">校验 OpenAPI</el-dropdown-item>
                <el-dropdown-item divided class="danger-item" @click="remove(account.id)">删除账号</el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </footer>
      </article>
      <div v-if="!pageLoading && !accounts.length" class="empty-state account-empty">
        <span class="empty-state__icon"><AppIcon name="cloud" /></span>
        <h3>还没有云盘账号</h3>
        <p>选择云盘后，MediaSync 会优先提供最简单、安全的登录方式。</p>
        <el-button v-if="enabledProviders.length" type="primary" @click="openAdd">添加第一个账号</el-button>
      </div>
    </div>

    <el-dialog v-model="addDialog" width="min(720px, calc(100vw - 32px))" class="add-account-dialog" :show-close="true">
      <template #header>
        <div class="add-dialog-heading">
          <button v-if="addStep === 2" class="back-button" type="button" aria-label="返回选择云盘" @click="backToProviders"><AppIcon name="arrow" :size="18" /></button>
          <div><span>添加云盘账号</span><small>{{ addStep === 1 ? '先选择你要连接的云盘' : `连接 ${selectedProvider?.name ?? ''}` }}</small></div>
          <div class="step-dots"><i :class="{ active: addStep === 1 }">1</i><b></b><i :class="{ active: addStep === 2 }">2</i></div>
        </div>
      </template>

      <div v-if="addStep === 1" class="provider-picker">
        <button v-for="provider in enabledProviders" :key="provider.id" class="provider-choice" type="button" @click="selectProvider(provider.id)">
          <ProviderLogo class="provider-choice__logo" :provider="provider.id" :size="46" />
          <span class="provider-choice__copy"><strong>{{ provider.name }}</strong><small>{{ authorizationSummary(provider.id) }}</small></span>
          <span class="auth-count" :class="{ double: guidedLoginOptions[provider.id]?.openApi !== 'none' }">{{ authorizationCountLabel(provider.id) }}</span>
          <AppIcon name="arrow" :size="17" />
        </button>
      </div>

      <div v-else class="login-methods">
        <div class="provider-selected">
          <ProviderLogo class="provider-choice__logo" :provider="selectedProviderId" :size="46" />
          <div><strong>{{ selectedProvider?.name }}</strong><small>{{ selectedAuthorizationSummary }}</small></div>
        </div>

        <div v-if="selectedGuidedOption" class="authorization-section active">
          <div class="authorization-heading">
            <span class="authorization-number">1</span>
            <div><strong>私有登录</strong><small>{{ selectedGuidedOption.privatePurpose }}</small></div>
            <el-tag size="small" type="danger" effect="plain">必需</el-tag>
          </div>

          <button class="login-method recommended" :class="{ disabled: selectedGuidedOption.state !== 'available' }" type="button" :disabled="selectedGuidedOption.state !== 'available'" @click="startGuidedLogin">
            <span class="method-icon"><AppIcon :name="selectedGuidedOption.icon" :size="25" /></span>
            <span class="method-copy"><span class="method-eyebrow">推荐方式</span><strong>{{ selectedGuidedOption.title }}</strong><small>{{ selectedGuidedOption.description }}</small></span>
            <span class="method-action">{{ selectedGuidedOption.action }}<AppIcon v-if="selectedGuidedOption.state === 'available'" name="arrow" :size="16" /></span>
          </button>

          <div v-if="selectedGuidedOption.state === 'building'" class="building-note">
            <AppIcon name="settings" :size="17" />
            <span><strong>自动登录正在接入</strong>当前版本仍可使用已有凭证完成这一项。</span>
          </div>

          <button class="manual-link" type="button" @click="openManualAdd()"><AppIcon name="key" :size="15" />使用已有 Cookie 或 Token<AppIcon name="arrow" :size="14" /></button>
        </div>

        <div v-if="selectedGuidedOption?.openApi !== 'none'" class="authorization-section pending">
          <div class="authorization-heading">
            <span class="authorization-number">2</span>
            <div><strong>OpenAPI 授权</strong><small>{{ selectedGuidedOption?.openApiPurpose }}</small></div>
            <el-tag size="small" :type="selectedGuidedOption?.openApi === 'required' ? 'danger' : 'info'" effect="plain">{{ selectedGuidedOption?.openApi === 'required' ? '必需' : '可选' }}</el-tag>
          </div>
          <div class="authorization-wait"><AppIcon name="lock" :size="16" /><span>先完成私有登录，账号创建后选择授权方式<span v-if="selectedGuidedOption?.openApiModes">：{{ selectedGuidedOption.openApiModes }}</span></span></div>
        </div>

        <div class="privacy-note"><AppIcon name="shield" :size="16" /><span>登录凭证只保存在你的 MediaSync 中，不会发送到 MediaSync 官方服务器。</span></div>
      </div>
    </el-dialog>

    <el-dialog v-model="dialog" :title="editingId ? '编辑云盘账号' : '手动添加账号'" width="min(480px, calc(100vw - 32px))">
      <el-form label-position="top">
        <el-form-item label="Provider">
          <el-select v-model="form.provider" class="full-width" :disabled="!!editingId">
            <el-option
              v-for="provider in providers"
              :key="provider.id"
              :label="`${provider.name}（${providerAvailabilityLabel(provider)}）`"
              :value="provider.id"
              :disabled="!provider.enabled"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="账号名称"><el-input v-model="form.name" placeholder="例如：家庭影音盘" /></el-form-item>
        <el-form-item :label="accountCredentialLabel">
          <el-input v-model="form.refresh_token" type="password" show-password />
          <div v-if="form.provider === 'pan123'" class="form-tip">填写 123 云盘 Web 端 Local Storage 中的 authorToken；凭证会加密保存，请勿发送到聊天或日志。</div>
          <div v-if="form.provider === 'baidu'" class="form-tip">填写百度网盘 Web Cookie 中的 BDUSS；凭证会加密保存，请勿发送到聊天或日志。</div>
        </el-form-item>
      </el-form>
      <template #footer><el-button @click="dialog = false">取消</el-button><el-button type="primary" :loading="loading" @click="save">保存</el-button></template>
    </el-dialog>

    <el-dialog v-model="openDialog" :title="`绑定 ${openProviderName(openAccount)}`" width="min(680px, calc(100vw - 32px))" class="openapi-dialog">
      <div class="openapi-overview">
        <div class="openapi-overview__heading">
          <ProviderLogo :provider="openAccount?.provider ?? ''" :size="34" />
          <div><strong>{{ openProviderName(openAccount) }}</strong><span>{{ openProviderGuide.verification }}</span></div>
          <el-tag size="small" :type="openProviderGuide.tagType" effect="plain">{{ openProviderGuide.requirement }}</el-tag>
        </div>
        <div class="openapi-coverage">
          <div><span>私有登录负责</span><strong>{{ openProviderGuide.privateCoverage }}</strong></div>
          <AppIcon name="arrow" :size="15" />
          <div><span>OpenAPI 补充</span><strong>{{ openProviderGuide.openCoverage }}</strong></div>
        </div>
      </div>

      <div class="openapi-section">
        <div class="openapi-section__title"><i>1</i><div><strong>选择授权来源</strong><span>只显示这个网盘能够长期使用的方式</span></div></div>
        <el-radio-group v-model="openForm.mode" class="open-mode-grid" @change="openModeChanged">
          <el-radio v-for="mode in availableOpenModes" :key="mode.value" :value="mode.value" class="open-mode-card" :class="{ selected: openForm.mode === mode.value }">
            <span class="open-mode-card__copy">
              <span class="open-mode-card__heading"><strong>{{ mode.label }}</strong><em v-if="mode.recommended">推荐</em></span>
              <small>{{ mode.description }}</small>
            </span>
          </el-radio>
        </el-radio-group>
      </div>

      <div class="openapi-section">
        <div class="openapi-section__title"><i>2</i><div><strong>获取授权凭证</strong><span>{{ selectedOpenMode?.label }}</span></div></div>
        <div v-if="openForm.mode === 'alistgo'" class="openlist-assistant unified-assistant">
          <div class="assistant-title"><AppIcon name="browser" :size="18" /><div><strong>通过 AListGo 获取 Refresh Token</strong><span>无需打开开发者工具，授权后只需复制一次 Token。</span></div></div>
          <ol>
            <li><span>1</span><div><el-link :href="alistGoAuthorizationUrl" target="_blank" type="primary">打开 {{ openAccount?.provider === 'baidu' ? '百度 OAuth 授权' : 'AListGo 授权工具' }}</el-link>并完成账号授权。</div></li>
            <li><span>2</span><div>复制授权结果中的 Refresh Token，回到这里读取或粘贴。</div></li>
          </ol>
          <el-button plain type="primary" @click="importOpenTokenFromClipboard"><AppIcon name="copy" :size="15" />从剪贴板读取 Refresh Token</el-button>
        </div>
        <div v-else-if="openForm.mode === 'openlist'" class="openlist-assistant unified-assistant">
          <div class="assistant-title"><AppIcon name="browser" :size="18" /><div><strong>通过 OpenList APIPages 获取 Refresh Token</strong><span>授权工具和刷新节点必须选择同一站点。</span></div></div>
          <ol>
            <li><span>1</span><div><el-link :href="openListAuthorizationPage" target="_blank" type="primary">打开 OpenList Token 工具</el-link>，选择“{{ openListDriverName }}”。</div></li>
            <li><span>2</span><div>完成授权并复制 Refresh Token<span v-if="openAccount?.provider === 'quark'">；同时保留页面使用的 AppID 和 SignKey</span>。</div></li>
            <li><span>3</span><div>回到这里选择同一刷新节点，然后读取或粘贴 Token。</div></li>
          </ol>
          <el-button plain type="primary" @click="importOpenTokenFromClipboard"><AppIcon name="copy" :size="15" />从剪贴板读取 Refresh Token</el-button>
        </div>
        <div v-else class="openlist-assistant unified-assistant custom-assistant">
          <div class="assistant-title"><AppIcon name="key" :size="18" /><div><strong>使用自己的开放平台应用</strong><span>{{ openAccount?.provider === 'pan123' ? '填写开放平台提供的 Client ID 和 Client Secret，无需 Refresh Token。' : '使用同一个应用完成 OAuth 授权，并准备对应的 Refresh Token、Client ID 和 Client Secret。' }}</span></div></div>
        </div>
        <div class="openapi-security-note" :class="{ hosted: openForm.mode !== 'custom' }"><AppIcon :name="openForm.mode === 'custom' ? 'shield' : 'external'" :size="15" /><span>{{ hostedCredentialNotice }}</span></div>
      </div>

      <div class="openapi-section">
        <div class="openapi-section__title"><i>3</i><div><strong>填写并校验</strong><span>敏感字段会加密保存；编辑时留空表示不修改</span></div></div>
        <el-form label-position="top" class="openapi-form">
          <el-form-item v-if="!(openAccount?.provider === 'pan123' && openForm.mode === 'custom')" label="Refresh Token">
            <el-input v-model="openForm.refresh_token" type="password" show-password placeholder="粘贴授权工具返回的 Refresh Token" />
          </el-form-item>
          <el-form-item v-if="openForm.mode === 'openlist'" label="OpenList 刷新节点">
            <el-select v-model="openForm.token_url" filterable allow-create default-first-option class="full-width">
              <template v-if="openAccount?.provider === 'quark'">
                <el-option label="OpenList 全球站" value="https://api.oplist.org/quarkyun/renewapi" />
                <el-option label="OpenList 国内站" value="https://api-cn.oplist.org/quarkyun/renewapi" />
              </template>
              <template v-else-if="openAccount?.provider === 'baidu'">
                <el-option label="OpenList 全球站" value="https://api.oplist.org/baiduyun/renewapi" />
                <el-option label="OpenList 国内站" value="https://api-cn.oplist.org/baiduyun/renewapi" />
              </template>
              <template v-else>
                <el-option label="OpenList 国内站" value="https://api.oplist.org.cn/alicloud/renewapi" />
                <el-option label="OpenList 全球站" value="https://api.oplist.org/alicloud/renewapi" />
              </template>
            </el-select>
            <div class="form-tip">自建 APIPages 可以粘贴对应 driver 的完整 HTTPS /renewapi 地址。</div>
          </el-form-item>
          <template v-if="openForm.mode === 'custom' || openAccount?.provider === 'quark'">
            <el-form-item :label="openAccount?.provider === 'quark' ? 'AppID' : 'Client ID'"><el-input v-model="openForm.client_id" /></el-form-item>
            <el-form-item :label="openAccount?.provider === 'quark' ? 'SignKey' : 'Client Secret'"><el-input v-model="openForm.client_secret" type="password" show-password /></el-form-item>
          </template>
          <el-collapse v-if="openForm.mode === 'alistgo' && openForm.token_url" class="openapi-advanced">
            <el-collapse-item title="高级设置" name="advanced"><el-form-item label="托管 Token URL"><el-input v-model="openForm.token_url" /></el-form-item></el-collapse-item>
          </el-collapse>
          <el-alert v-if="openAccount?.open_last_error" :title="openAccount.open_last_error" type="error" :closable="false" />
        </el-form>
      </div>
      <template #footer><el-button v-if="openAccount?.open_auth_mode" type="danger" plain @click="unbindOpenApi">解绑</el-button><el-button @click="openDialog = false">取消</el-button><el-button type="primary" :loading="openLoading" @click="saveOpenApi">保存并校验</el-button></template>
    </el-dialog>

    <el-dialog v-model="qrDialog" :title="`${qrProviderLabel}扫码登录`" width="min(430px, calc(100vw - 32px))" @closed="stopQrPolling">
      <el-form label-position="top"><el-form-item label="账号名称"><el-input v-model="qrForm.name" :disabled="!!qrForm.account_id" /></el-form-item></el-form>
      <div class="qr-login">
        <img v-if="qrImage" :src="qrImage" :alt="`${qrProviderLabel}登录二维码`" class="qr-image" />
        <el-empty v-else :image-size="70" description="尚未生成二维码" />
        <p>{{ qrStatus }}</p>
      </div>
      <template #footer><el-button @click="qrDialog = false">关闭</el-button><el-button type="primary" :loading="qrLoading" @click="generateQr">{{ qrImage ? '重新生成' : '生成二维码' }}</el-button></template>
    </el-dialog>
  </section>
</template>

<style scoped>
.account-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 18px; min-height: 180px; }
.connection-banner { display: flex; align-items: center; gap: 13px; margin-bottom: 18px; padding: 14px 16px; border: 1px solid #d9e4ff; border-radius: 14px; background: linear-gradient(100deg, #f5f7ff, #fff); }
.connection-banner__icon { display: grid; place-items: center; width: 42px; height: 42px; flex: 0 0 42px; color: #4f46e5; border-radius: 12px; background: #e8eaff; }
.connection-banner > div { min-width: 0; flex: 1; }
.connection-banner strong { display: block; color: #25316d; font-size: 13px; }
.connection-banner p { margin: 3px 0 0; color: #6b769e; font-size: 12px; line-height: 1.45; }
.connection-banner .el-button { gap: 4px; }
.account-card { display: flex; flex-direction: column; gap: 18px; padding: 22px; border: 1px solid var(--border-color); border-radius: 18px; background: var(--surface); box-shadow: var(--shadow-sm); }
.account-card__header, .account-brand, .open-panel__title, .open-panel__title > div, .account-actions { display: flex; align-items: center; }
.account-card__header { justify-content: space-between; gap: 16px; }
.account-brand { gap: 12px; min-width: 0; }
.account-brand h3 { margin: 0 0 4px; font-size: 17px; }
.account-brand p, .open-panel p { margin: 0; color: var(--text-secondary); font-size: 13px; }
.account-facts { display: grid; grid-template-columns: .8fr 1.35fr 1fr; gap: 12px; }
.account-facts > div { min-width: 0; }
.account-facts span, .account-facts strong { display: block; }
.account-facts span { margin-bottom: 5px; color: var(--text-muted); font-size: 12px; }
.account-facts strong { overflow: hidden; color: var(--text-primary); font-size: 13px; text-overflow: ellipsis; white-space: nowrap; }
.open-panel { padding: 14px; border: 1px solid var(--border-color); border-radius: 14px; background: #f8fafc; }
.open-panel.connected { border-color: #d9e9ff; background: #f5f9ff; }
.open-panel__title { justify-content: space-between; gap: 12px; }
.open-panel__action { display: flex; align-items: center; gap: 6px; flex: 0 0 auto; }
.open-panel__title > div { gap: 10px; min-width: 0; }
.open-panel__title strong { display: block; margin-bottom: 2px; font-size: 13px; }
.open-identity { margin-top: 10px !important; padding-top: 10px; border-top: 1px dashed #d8e2ef; }
.account-error { margin-top: 10px !important; color: var(--danger) !important; line-height: 1.5; }
.account-actions { margin-top: auto; gap: 8px; padding-top: 2px; border-top: 1px solid #f0f2f5; }
.account-actions .el-button { margin: 0; }
.account-actions .app-icon { margin-left: 5px; width: 14px; }
.muted { color: var(--text-muted); font-size: 12px; }
.account-empty { grid-column: 1 / -1; }
.qr-login { min-height: 250px; text-align: center; }
.qr-image { width: 220px; height: 220px; background: #fff; border-radius: 8px; }
.qr-login p { color: var(--el-text-color-secondary); }
.open-alert { margin-bottom: 18px; }
.openapi-overview { display: grid; gap: 14px; margin-bottom: 18px; padding: 16px; border: 1px solid #dce3ff; border-radius: 15px; background: linear-gradient(105deg, #f7f8ff, #fff); }
.openapi-overview__heading { display: grid; grid-template-columns: 34px minmax(0, 1fr) auto; align-items: center; gap: 11px; }
.openapi-overview__heading > div { min-width: 0; }
.openapi-overview__heading strong, .openapi-overview__heading span { display: block; }
.openapi-overview__heading strong { color: #1d2939; font-size: 14px; }
.openapi-overview__heading span { margin-top: 3px; color: #667085; font-size: 11px; line-height: 1.5; }
.openapi-coverage { display: grid; grid-template-columns: minmax(0, 1fr) 20px minmax(0, 1fr); align-items: center; gap: 8px; }
.openapi-coverage > div { min-width: 0; padding: 10px 12px; border-radius: 10px; background: rgb(255 255 255 / 82%); }
.openapi-coverage span, .openapi-coverage strong { display: block; }
.openapi-coverage span { color: #98a2b3; font-size: 10px; }
.openapi-coverage strong { margin-top: 3px; color: #344054; font-size: 11px; line-height: 1.45; }
.openapi-coverage > .app-icon { color: #98a2b3; }
.openapi-section { display: grid; gap: 13px; padding: 18px 0; border-top: 1px solid #eef0f4; }
.openapi-section__title { display: grid; grid-template-columns: 28px minmax(0, 1fr); align-items: center; gap: 10px; }
.openapi-section__title i { display: grid; place-items: center; width: 28px; height: 28px; color: #fff; border-radius: 50%; background: #4f46e5; font-size: 11px; font-style: normal; font-weight: 800; }
.openapi-section__title strong, .openapi-section__title span { display: block; }
.openapi-section__title strong { color: #1d2939; font-size: 13px; }
.openapi-section__title span { margin-top: 2px; color: #98a2b3; font-size: 10px; }
.open-mode-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; width: 100%; }
:deep(.open-mode-card.el-radio) { display: flex; align-items: flex-start; width: 100%; height: auto; margin: 0; padding: 13px; white-space: normal; border: 1px solid #e4e7ec; border-radius: 12px; background: #fff; transition: .16s ease; }
:deep(.open-mode-card.el-radio:hover) { border-color: #a5b4fc; }
:deep(.open-mode-card.el-radio.selected) { border-color: #818cf8; background: #f7f7ff; box-shadow: 0 5px 16px rgb(79 70 229 / 8%); }
:deep(.open-mode-card .el-radio__input) { margin-top: 2px; }
:deep(.open-mode-card .el-radio__label) { min-width: 0; padding-left: 8px; color: inherit; white-space: normal; }
.open-mode-card__copy, .open-mode-card__heading, .open-mode-card__copy small { display: block; }
.open-mode-card__heading { display: flex; align-items: center; gap: 5px; color: #344054; }
.open-mode-card__heading strong { font-size: 12px; }
.open-mode-card__heading em { padding: 2px 5px; color: #4338ca; border-radius: 999px; background: #e9eaff; font-size: 8px; font-style: normal; font-weight: 700; }
.open-mode-card__copy small { margin-top: 5px; color: #98a2b3; font-size: 9px; line-height: 1.45; }
.unified-assistant { margin: 0; }
.custom-assistant { padding-bottom: 15px; }
.openapi-security-note { display: flex; align-items: flex-start; gap: 8px; padding: 10px 12px; color: #3f6b57; border-radius: 10px; background: #f1f8f4; font-size: 10px; line-height: 1.55; }
.openapi-security-note.hosted { color: #8a6116; background: #fff8e8; }
.openapi-security-note .app-icon { flex: 0 0 auto; margin-top: 1px; }
.openapi-form { padding-left: 38px; }
.openapi-advanced { border: 0; }
:deep(.openapi-advanced .el-collapse-item__header) { height: 36px; color: #667085; font-size: 11px; border: 0; }
:deep(.openapi-advanced .el-collapse-item__wrap) { border: 0; }
:deep(.openapi-advanced .el-collapse-item__content) { padding-bottom: 0; }
.openlist-assistant { display: grid; gap: 13px; margin-bottom: 18px; padding: 15px; border: 1px solid #c7d2fe; border-radius: 13px; background: #f8f9ff; }
.assistant-title { display: flex; align-items: flex-start; gap: 9px; color: #4338ca; }
.assistant-title > div { min-width: 0; }
.assistant-title strong, .assistant-title span { display: block; }
.assistant-title strong { font-size: 13px; }
.assistant-title span { margin-top: 4px; color: #667085; font-size: 11px; line-height: 1.5; }
.openlist-assistant ol { display: grid; gap: 9px; margin: 0; padding: 0; list-style: none; }
.openlist-assistant li { display: grid; grid-template-columns: 23px minmax(0, 1fr); align-items: start; gap: 8px; color: #475467; font-size: 11px; line-height: 1.55; }
.openlist-assistant li > span { display: grid; place-items: center; width: 23px; height: 23px; color: #4f46e5; border-radius: 50%; background: #e7eaff; font-size: 10px; font-weight: 800; }
.openlist-assistant .el-button { justify-self: start; gap: 6px; }
.add-dialog-heading { display: flex; align-items: center; gap: 12px; padding-right: 30px; }
.add-dialog-heading > div:nth-child(2) { min-width: 0; flex: 1; }
.add-dialog-heading span, .add-dialog-heading small { display: block; }
.add-dialog-heading span { color: #101828; font-size: 18px; font-weight: 700; }
.add-dialog-heading small { margin-top: 4px; color: #98a2b3; font-size: 12px; font-weight: 400; }
.back-button { display: grid; place-items: center; width: 34px; height: 34px; color: #475467; cursor: pointer; border-radius: 9px; background: #f2f4f7; transform: rotate(180deg); }
.step-dots { display: flex; align-items: center; }
.step-dots i { display: grid; place-items: center; width: 24px; height: 24px; color: #98a2b3; border-radius: 50%; background: #f2f4f7; font-size: 11px; font-style: normal; font-weight: 700; }
.step-dots i.active { color: #fff; background: #4f46e5; }
.step-dots b { width: 22px; height: 2px; background: #e4e7ec; }
.provider-picker { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; padding: 4px 0 8px; }
.provider-choice { display: grid; grid-template-columns: 46px minmax(0, 1fr) auto 18px; align-items: center; gap: 12px; min-height: 78px; padding: 14px; text-align: left; cursor: pointer; color: #344054; border: 1px solid #e4e7ec; border-radius: 14px; background: #fff; transition: .18s ease; }
.provider-choice:hover { border-color: #a5b4fc; box-shadow: 0 9px 24px rgba(79,70,229,.08); transform: translateY(-1px); }
.provider-choice__copy, .provider-selected > div { min-width: 0; }
.provider-choice__copy strong, .provider-choice__copy small, .provider-selected strong, .provider-selected small { display: block; }
.provider-choice__copy strong, .provider-selected strong { color: #1d2939; font-size: 14px; }
.provider-choice__copy small, .provider-selected small { margin-top: 4px; color: #98a2b3; font-size: 11px; }
.auth-count { padding: 4px 8px; color: #475467; border-radius: 999px; background: #f2f4f7; font-size: 10px; font-weight: 700; white-space: nowrap; }
.auth-count.double { color: #4338ca; background: #eef2ff; }
.login-methods { display: grid; gap: 13px; }
.provider-selected { display: flex; align-items: center; gap: 12px; margin-bottom: 3px; }
.authorization-section { display: grid; gap: 11px; padding: 15px; border: 1px solid #e4e7ec; border-radius: 15px; background: #fff; }
.authorization-section.active { border-color: #c7d2fe; background: #fafbff; }
.authorization-section.pending { background: #fafafa; }
.authorization-heading { display: grid; grid-template-columns: 28px minmax(0, 1fr) auto; align-items: center; gap: 10px; }
.authorization-heading > div { min-width: 0; }
.authorization-heading strong, .authorization-heading small { display: block; }
.authorization-heading strong { color: #1d2939; font-size: 13px; }
.authorization-heading small { margin-top: 3px; color: #98a2b3; font-size: 11px; }
.authorization-number { display: grid; place-items: center; width: 28px; height: 28px; color: #fff; border-radius: 50%; background: #4f46e5; font-size: 11px; font-weight: 800; }
.authorization-section.pending .authorization-number { color: #667085; background: #e4e7ec; }
.login-method { width: 100%; display: grid; grid-template-columns: 48px minmax(0, 1fr) auto; gap: 14px; align-items: center; padding: 18px; text-align: left; cursor: pointer; color: #344054; border: 1px solid #e4e7ec; border-radius: 15px; background: #fff; }
.login-method.recommended { border-color: #bdc7ff; background: linear-gradient(105deg, #f5f6ff, #fff); box-shadow: 0 8px 24px rgba(79,70,229,.07); }
.login-method:hover:not(.disabled) { border-color: #818cf8; }
.login-method.disabled { cursor: not-allowed; opacity: .75; }
.method-icon { display: grid; place-items: center; width: 48px; height: 48px; color: #4f46e5; border-radius: 13px; background: #e9ebff; }
.login-method.manual .method-icon { color: #667085; background: #f2f4f7; }
.method-copy { min-width: 0; }
.method-copy > * { display: block; }
.method-eyebrow { margin-bottom: 4px; color: #4f46e5; font-size: 10px; font-weight: 700; letter-spacing: .06em; }
.method-copy strong { color: #1d2939; font-size: 15px; }
.method-copy small { margin-top: 5px; color: #667085; font-size: 12px; line-height: 1.5; }
.method-action { display: flex; align-items: center; gap: 4px; color: #4f46e5; font-size: 12px; font-weight: 600; white-space: nowrap; }
.building-note { display: flex; align-items: flex-start; gap: 9px; padding: 11px 13px; color: #7a5a13; border: 1px solid #f4dfaa; border-radius: 11px; background: #fffaeb; font-size: 11px; line-height: 1.55; }
.building-note .app-icon { margin-top: 1px; }
.building-note strong { margin-right: 6px; }
.manual-link { justify-self: start; display: flex; align-items: center; gap: 6px; padding: 3px 2px; color: #667085; cursor: pointer; background: transparent; font-size: 11px; }
.manual-link:hover { color: #4f46e5; }
.authorization-wait { display: flex; align-items: center; gap: 8px; padding: 11px 12px; color: #98a2b3; border-radius: 10px; background: #f2f4f7; font-size: 11px; }
.privacy-note { display: flex; align-items: center; justify-content: center; gap: 7px; padding-top: 3px; color: #98a2b3; font-size: 11px; }
@media (max-width: 1100px) { .account-grid { grid-template-columns: 1fr; } }
@media (max-width: 600px) {
  .connection-banner { align-items: flex-start; }
  .connection-banner .el-button { display: none; }
  .provider-picker { grid-template-columns: 1fr; }
  .provider-choice { grid-template-columns: 46px minmax(0, 1fr) auto 16px; }
  .authorization-section { padding: 13px; }
  .authorization-heading { grid-template-columns: 26px minmax(0, 1fr) auto; }
  .authorization-number { width: 26px; height: 26px; }
  .login-method { grid-template-columns: 42px minmax(0, 1fr); padding: 15px; }
  .method-icon { width: 42px; height: 42px; }
  .method-action { grid-column: 2; }
  .step-dots { display: none; }
  .account-card { padding: 18px; }
  .account-facts { grid-template-columns: 1fr 1fr; }
  .account-facts > div:nth-child(2) { grid-column: 1 / -1; grid-row: 2; }
  .account-actions { flex-wrap: wrap; }
  .account-actions .el-button { flex: 1; }
  .openapi-coverage { grid-template-columns: 1fr; }
  .openapi-coverage > .app-icon { display: none; }
  .open-mode-grid { grid-template-columns: 1fr; }
  .openapi-form { padding-left: 0; }
}
</style>
