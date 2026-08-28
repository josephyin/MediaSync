<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api, type Page } from '../api/client'
import type { CloudAccount, ProviderInfo, SystemInfo } from '../api/types'
import AppIcon from '../components/AppIcon.vue'
import { formatDateTime, formatRelativeTime, openModeLabels, statusLabel, statusType } from '../utils/display'
import { findProvider, providerAvailabilityLabel, providerMark, providerName, supportsCapabilities } from '../utils/providers'

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
  },
  quark: {
    provider: 'quark',
    title: '使用夸克 App 扫码',
    description: '登录成功后由 MediaSync 自动接收并安全保存登录凭证。',
    action: '开始扫码',
    state: 'available',
    icon: 'scan',
    privatePurpose: '读取分享内容',
    openApi: 'required',
    openApiPurpose: '浏览账号盘、查重并自动创建目录',
  },
  pan123: {
    provider: 'pan123',
    title: '扫码登录 123 云盘',
    description: '自动完成网页会话登录，不再需要查找 authorToken。',
    action: '开始扫码',
    state: 'available',
    icon: 'scan',
    privatePurpose: '读取分享、浏览账号盘并执行转存',
    openApi: 'none',
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
const openForm = reactive({
  mode: 'alistgo' as 'alistgo' | 'openlist' | 'custom',
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
function supportsOpenApi(account: CloudAccount) { return ['aliyundrive', 'quark', 'baidu'].includes(account.provider) }
function openProviderName(account: CloudAccount | null) {
  return account ? `${providerName(providers.value, account.provider)} OpenAPI` : 'OpenAPI'
}
function defaultOpenMode(account: CloudAccount) {
  return ['quark', 'baidu'].includes(account.provider) ? 'openlist' : 'alistgo'
}
function defaultOpenTokenUrl(account: CloudAccount, mode: 'alistgo' | 'openlist' | 'custom') {
  if (account.provider === 'quark') return quarkOpenListTokenUrl
  if (account.provider === 'baidu') return baiduOpenListTokenUrl
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
  const mode = account.open_auth_mode ?? defaultOpenMode(account)
  Object.assign(openForm, {
    mode,
    refresh_token: '',
    token_url: account.open_token_url ?? defaultOpenTokenUrl(account, mode),
    client_id: account.open_client_id ?? '',
    client_secret: '',
  })
  openDialog.value = true
}
function openModeChanged(mode: 'alistgo' | 'openlist' | 'custom') {
  if (openAccount.value && mode !== 'custom') {
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
    if (openForm.mode !== 'custom') body.token_url = openForm.token_url.trim()
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
      const needsRequiredOpenApi = ['quark', 'baidu'].includes(qrProviderId.value) && result.account && !result.account.open_auth_mode
      ElMessage.success(needsRequiredOpenApi ? `${qrProviderLabel.value}私有登录成功，请继续绑定 OpenAPI` : `${qrProviderLabel.value}登录成功`)
      window.setTimeout(() => {
        qrDialog.value = false
        if (needsRequiredOpenApi && result.account) openOpenApi(result.account)
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
          <div class="account-brand"><span class="account-logo">{{ providerMark(providers, account.provider) }}</span><div><h3>{{ account.name }}</h3><p>{{ account.account_identity || '账号信息待校验' }}</p></div></div>
          <el-tag :type="statusType(account.status)" effect="light">{{ statusLabel(account.status) }}</el-tag>
        </header>

        <div class="account-facts">
          <div><span>服务商</span><strong>{{ providerName(providers, account.provider) }}</strong></div>
          <div><span>默认 Drive ID</span><strong class="monospace">{{ account.default_drive_id || '待获取' }}</strong></div>
          <div><span>最近校验</span><el-tooltip :content="formatDateTime(account.last_verified_at)"><strong>{{ formatRelativeTime(account.last_verified_at) }}</strong></el-tooltip></div>
        </div>

        <div v-if="supportsOpenApi(account)" class="open-panel" :class="{ connected: account.open_auth_mode }">
          <div class="open-panel__title">
            <div><span class="open-mark">O</span><div><strong>{{ providerName(providers, account.provider) }} OpenAPI</strong><p>{{ account.open_auth_mode ? `${openModeLabels[account.open_auth_mode]} 授权` : account.provider === 'aliyundrive' ? '可选，用于识别默认盘、资源库与备份盘' : '用于账号盘浏览、查重和目录能力' }}</p></div></div>
            <el-tag v-if="account.open_status" size="small" :type="statusType(account.open_status)">{{ statusLabel(account.open_status) }}</el-tag>
            <span v-else class="muted">未绑定</span>
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
          <span class="provider-choice__logo" :data-provider="provider.id">{{ providerMark(providers, provider.id) }}</span>
          <span class="provider-choice__copy"><strong>{{ provider.name }}</strong><small>{{ authorizationSummary(provider.id) }}</small></span>
          <span class="auth-count" :class="{ double: guidedLoginOptions[provider.id]?.openApi !== 'none' }">{{ authorizationCountLabel(provider.id) }}</span>
          <AppIcon name="arrow" :size="17" />
        </button>
      </div>

      <div v-else class="login-methods">
        <div class="provider-selected">
          <span class="provider-choice__logo" :data-provider="selectedProviderId">{{ providerMark(providers, selectedProviderId) }}</span>
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
          <div class="authorization-wait"><AppIcon name="lock" :size="16" /><span>先完成私有登录，账号创建后继续授权</span></div>
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

    <el-dialog v-model="openDialog" :title="`绑定 ${openProviderName(openAccount)}`" width="min(560px, calc(100vw - 32px))">
      <el-alert v-if="openAccount?.provider === 'aliyundrive'" title="私有 token 继续负责分享监控和转存；Open token 只用于识别默认盘、资源库和备份盘。校验时会核对两边是否为同一账号。" type="info" :closable="false" class="open-alert" />
      <el-alert v-else-if="openAccount?.provider === 'quark'" title="Cookie 私有接口负责分享读取；OpenList Open token 负责账号盘和目录。当前私有接口没有稳定 user_id，系统会分别校验两套凭证，但无法自动证明属于同一账号，请确认授权的是同一个夸克账号。" type="warning" :closable="false" class="open-alert" />
      <el-alert v-else title="BDUSS Cookie 负责分享读取和转存；OpenList Open token 负责账号盘浏览、查重和自动建目录。系统会校验两套凭证是否属于同一百度账号。" type="info" :closable="false" class="open-alert" />
      <el-form label-position="top">
        <el-form-item v-if="openAccount?.provider === 'aliyundrive'" label="授权方式">
          <el-radio-group v-model="openForm.mode" @change="openModeChanged">
            <el-radio value="alistgo">AListGo 托管刷新</el-radio>
            <el-radio value="openlist">OpenList APIPages</el-radio>
            <el-radio value="custom">自有 OpenAPI 应用</el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item v-else label="授权方式"><el-tag>OpenList APIPages</el-tag></el-form-item>
        <el-alert v-if="openForm.mode !== 'custom'" title="托管模式会把 Open refresh token 发送到下面的 Token URL。不同服务签发的 token 不能混用，请只使用你信任的服务。" type="warning" :closable="false" class="open-alert" />
        <div v-if="openForm.mode === 'openlist' && openAccount?.provider === 'baidu'" class="openlist-assistant">
          <div class="assistant-title"><AppIcon name="browser" :size="18" /><div><strong>OpenList 百度授权助手</strong><span>OpenList 当前不会把授权结果自动回传给 MediaSync，因此还需要一次复制；无需打开开发者工具。</span></div></div>
          <ol>
            <li><span>1</span><div>打开与你选择的刷新节点相同的授权站点：<el-link :href="openListAuthorizationPage" target="_blank" type="primary">打开 OpenList Token 工具</el-link></div></li>
            <li><span>2</span><div>选择“百度网盘 (OAuth2) 验证登录”，勾选“使用 OpenList 提供的参数”，完成百度授权。</div></li>
            <li><span>3</span><div>复制页面中的 Refresh Token，回到这里点击读取。</div></li>
          </ol>
          <el-button plain type="primary" @click="importOpenTokenFromClipboard"><AppIcon name="copy" :size="15" />从剪贴板读取 Refresh Token</el-button>
        </div>
        <el-alert v-else-if="openForm.mode === 'openlist'" type="info" :closable="false" class="open-alert">
          <template #title>请在 OpenList APIPages 选择“{{ openAccount?.provider === 'quark' ? '夸克网盘 (OAuth2) 验证登录' : openAccount?.provider === 'baidu' ? '百度网盘 验证登录' : '阿里云盘 (OAuth2) 扫码登录' }}”获取专用 token，并在下方选择同一节点：<el-link href="https://api.oplist.org.cn" target="_blank" type="primary">国内站</el-link> · <el-link href="https://api.oplist.org" target="_blank" type="primary">全球站</el-link></template>
        </el-alert>
        <el-form-item label="Open Refresh Token（编辑时留空表示不修改）"><el-input v-model="openForm.refresh_token" type="password" show-password /></el-form-item>
        <el-form-item v-if="openForm.mode === 'alistgo'" label="托管 Token URL"><el-input v-model="openForm.token_url" /></el-form-item>
        <el-form-item v-else-if="openForm.mode === 'openlist'" label="OpenList 刷新节点">
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
          <div class="form-tip">自建 APIPages 可以直接粘贴对应 driver 的完整 HTTPS /renewapi 地址。</div>
        </el-form-item>
        <template v-if="openForm.mode === 'custom' || openAccount?.provider === 'quark'">
          <el-form-item :label="openAccount?.provider === 'quark' ? 'AppID（必填）' : 'Client ID'"><el-input v-model="openForm.client_id" /></el-form-item>
          <el-form-item :label="openAccount?.provider === 'quark' ? 'SignKey（新增时必填，编辑时留空表示不修改）' : 'Client Secret（编辑时留空表示不修改）'"><el-input v-model="openForm.client_secret" type="password" show-password /></el-form-item>
        </template>
        <el-alert v-if="openAccount?.open_last_error" :title="openAccount.open_last_error" type="error" :closable="false" />
      </el-form>
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
.account-logo { display: grid; place-items: center; width: 42px; height: 42px; border-radius: 13px; color: #fff; background: linear-gradient(135deg, #6c5ce7, #2898ff); font-size: 13px; font-weight: 800; box-shadow: 0 8px 18px rgb(78 92 224 / 24%); }
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
.open-panel__title > div { gap: 10px; min-width: 0; }
.open-panel__title strong { display: block; margin-bottom: 2px; font-size: 13px; }
.open-mark { display: grid; place-items: center; flex: 0 0 30px; width: 30px; height: 30px; border-radius: 9px; color: #fff; background: #2f80ed; font-size: 12px; font-weight: 800; }
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
.provider-choice__logo { display: grid; place-items: center; width: 46px; height: 46px; color: #fff; border-radius: 13px; background: linear-gradient(135deg, #6c5ce7, #2898ff); font-size: 12px; font-weight: 800; }
.provider-choice__logo[data-provider="quark"] { background: linear-gradient(135deg, #ff6a00, #ff9b31); }
.provider-choice__logo[data-provider="pan123"] { background: linear-gradient(135deg, #1687ff, #15b5ff); }
.provider-choice__logo[data-provider="baidu"] { background: linear-gradient(135deg, #315efb, #7657ff); }
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
  .provider-choice { grid-template-columns: 42px minmax(0, 1fr) auto 16px; }
  .provider-choice__logo { width: 42px; height: 42px; }
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
}
</style>
