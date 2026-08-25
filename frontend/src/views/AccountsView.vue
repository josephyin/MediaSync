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

const openDialog = ref(false)
const openLoading = ref(false)
const openAccount = ref<CloudAccount | null>(null)
const aliyunHostedTokenUrls = {
  alistgo: 'https://api.alistgo.com/alist/ali_open/token',
  openlist: 'https://api.oplist.org.cn/alicloud/renewapi',
} as const
const quarkOpenListTokenUrl = 'https://api.oplist.org/quarkyun/renewapi'
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
const qrForm = reactive<{ account_id: number | null; name: string }>({ account_id: null, name: '' })
let qrTimer: number | undefined
let qrSessionId = ''

const activeCount = computed(() => accounts.value.filter((item) => item.status === 'active').length)
const openCount = computed(() => accounts.value.filter((item) => item.open_auth_mode).length)
const enabledProviders = computed(() => providers.value.filter((provider) => provider.enabled))
const aliyunEnabled = computed(() => findProvider(providers.value, 'aliyundrive')?.enabled === true)

function providerInfo(providerId: string) { return findProvider(providers.value, providerId) }
function isAliyun(account: CloudAccount) { return account.provider === 'aliyundrive' }
function supportsOpenApi(account: CloudAccount) { return ['aliyundrive', 'quark'].includes(account.provider) }
function openProviderName(account: CloudAccount | null) {
  return account ? `${providerName(providers.value, account.provider)} OpenAPI` : 'OpenAPI'
}
function defaultOpenMode(account: CloudAccount) {
  return account.provider === 'quark' ? 'openlist' : 'alistgo'
}
function defaultOpenTokenUrl(account: CloudAccount, mode: 'alistgo' | 'openlist' | 'custom') {
  if (account.provider === 'quark') return quarkOpenListTokenUrl
  return mode === 'custom' ? '' : aliyunHostedTokenUrls[mode]
}
const accountCredentialLabel = computed(() => {
  const suffix = editingId.value ? '（留空表示不修改）' : ''
  return form.provider === 'quark' ? `Cookie${suffix}` : `Refresh Token${suffix}`
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
  editingId.value = null
  Object.assign(form, {
    provider: enabledProviders.value[0]?.id ?? '',
    name: '',
    refresh_token: '',
  })
  dialog.value = true
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
    await api(`/cloud-accounts/${openAccount.value.id}/open-credential/verify`, { method: 'POST' })
    ElMessage.success('OpenAPI 已绑定并校验成功')
    openDialog.value = false
  } catch (error) {
    const message = error instanceof Error ? error.message : 'OpenAPI 绑定失败'
    ElMessage.error(configured ? `配置已保存，但校验失败：${message}` : message)
  } finally {
    openLoading.value = false
    await load()
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
  if (qrTimer) window.clearInterval(qrTimer)
  qrTimer = undefined
}
function openQr(account?: CloudAccount) {
  stopQrPolling()
  qrSessionId = ''
  qrImage.value = ''
  qrStatus.value = account ? '生成二维码后使用阿里云盘 App 扫码' : '请填写账号名称并生成二维码'
  Object.assign(qrForm, { account_id: account?.id ?? null, name: account?.name ?? '' })
  qrDialog.value = true
}
async function pollQr() {
  if (!qrSessionId) return
  try {
    const result = await api<QrStatus>(`/aliyundrive/qr-login/${qrSessionId}`)
    if (result.status === 'scanned') qrStatus.value = '已扫码，请在手机上确认登录'
    if (result.status === 'waiting') qrStatus.value = '等待扫码…'
    if (result.status === 'expired') {
      qrStatus.value = '二维码已过期，请重新生成'
      stopQrPolling()
    }
    if (result.status === 'confirmed') {
      stopQrPolling()
      qrStatus.value = '登录成功，账号已保存'
      ElMessage.success('阿里云盘登录成功')
      await load()
      window.setTimeout(() => { qrDialog.value = false }, 600)
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
    const result = await api<QrStart>('/aliyundrive/qr-login/start', {
      method: 'POST',
      body: JSON.stringify({ account_id: qrForm.account_id, name: qrForm.name.trim() }),
    })
    qrSessionId = result.session_id
    qrImage.value = result.qr_code_data_url
    qrStatus.value = '请使用阿里云盘 App 扫码并确认'
    qrTimer = window.setInterval(pollQr, 2000)
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
      <div><h1>云盘账号</h1><p>管理接收资源的账号，并按需绑定 OpenAPI 识别多种盘类型</p></div>
      <div class="page-actions">
        <el-button @click="openAdd"><AppIcon name="plus" />手动添加</el-button>
        <el-button v-if="aliyunEnabled" type="primary" @click="openQr()"><AppIcon name="cloud" />阿里云盘扫码添加</el-button>
      </div>
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
            <div><span class="open-mark">O</span><div><strong>{{ providerName(providers, account.provider) }} OpenAPI</strong><p>{{ account.open_auth_mode ? `${openModeLabels[account.open_auth_mode]} 授权` : account.provider === 'quark' ? '可选，用于 OpenAPI 账号盘和目录能力' : '可选，用于识别默认盘、资源库与备份盘' }}</p></div></div>
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
                <el-dropdown-item v-if="isAliyun(account)" @click="openQr(account)">重新扫码登录</el-dropdown-item>
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
        <p>推荐使用阿里云盘 App 扫码登录，MediaSync 会自动保存兼容的私有 token。</p>
        <el-button v-if="aliyunEnabled" type="primary" @click="openQr()">扫码添加第一个账号</el-button>
        <el-button v-else-if="enabledProviders.length" type="primary" @click="openAdd">手动添加第一个账号</el-button>
      </div>
    </div>

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
        <el-form-item :label="accountCredentialLabel"><el-input v-model="form.refresh_token" type="password" show-password /></el-form-item>
      </el-form>
      <template #footer><el-button @click="dialog = false">取消</el-button><el-button type="primary" :loading="loading" @click="save">保存</el-button></template>
    </el-dialog>

    <el-dialog v-model="openDialog" :title="`绑定 ${openProviderName(openAccount)}`" width="min(560px, calc(100vw - 32px))">
      <el-alert v-if="openAccount?.provider === 'aliyundrive'" title="私有 token 继续负责分享监控和转存；Open token 只用于识别默认盘、资源库和备份盘。校验时会核对两边是否为同一账号。" type="info" :closable="false" class="open-alert" />
      <el-alert v-else title="Cookie 私有接口负责分享读取；OpenList Open token 负责账号盘和目录。当前私有接口没有稳定 user_id，系统会分别校验两套凭证，但无法自动证明属于同一账号，请确认授权的是同一个夸克账号。" type="warning" :closable="false" class="open-alert" />
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
        <el-alert v-if="openForm.mode === 'openlist'" type="info" :closable="false" class="open-alert">
          <template #title>请在 OpenList APIPages 选择“{{ openAccount?.provider === 'quark' ? '夸克网盘 (OAuth2) 验证登录' : '阿里云盘 (OAuth2) 扫码登录' }}”获取专用 token，并在下方选择同一节点：<el-link href="https://api.oplist.org.cn" target="_blank" type="primary">国内站</el-link> · <el-link href="https://api.oplist.org" target="_blank" type="primary">全球站</el-link></template>
        </el-alert>
        <el-form-item label="Open Refresh Token（编辑时留空表示不修改）"><el-input v-model="openForm.refresh_token" type="password" show-password /></el-form-item>
        <el-form-item v-if="openForm.mode === 'alistgo'" label="托管 Token URL"><el-input v-model="openForm.token_url" /></el-form-item>
        <el-form-item v-else-if="openForm.mode === 'openlist'" label="OpenList 刷新节点">
          <el-select v-model="openForm.token_url" filterable allow-create default-first-option class="full-width">
            <template v-if="openAccount?.provider === 'quark'">
              <el-option label="OpenList 全球站" value="https://api.oplist.org/quarkyun/renewapi" />
              <el-option label="OpenList 国内站" value="https://api-cn.oplist.org/quarkyun/renewapi" />
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

    <el-dialog v-model="qrDialog" title="阿里云盘扫码登录" width="min(430px, calc(100vw - 32px))" @closed="stopQrPolling">
      <el-form label-position="top"><el-form-item label="账号名称"><el-input v-model="qrForm.name" :disabled="!!qrForm.account_id" /></el-form-item></el-form>
      <div class="qr-login">
        <img v-if="qrImage" :src="qrImage" alt="阿里云盘登录二维码" class="qr-image" />
        <el-empty v-else :image-size="70" description="尚未生成二维码" />
        <p>{{ qrStatus }}</p>
      </div>
      <template #footer><el-button @click="qrDialog = false">关闭</el-button><el-button type="primary" :loading="qrLoading" @click="generateQr">{{ qrImage ? '重新生成' : '生成二维码' }}</el-button></template>
    </el-dialog>
  </section>
</template>

<style scoped>
.account-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 18px; min-height: 180px; }
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
@media (max-width: 1100px) { .account-grid { grid-template-columns: 1fr; } }
@media (max-width: 600px) {
  .account-card { padding: 18px; }
  .account-facts { grid-template-columns: 1fr 1fr; }
  .account-facts > div:nth-child(2) { grid-column: 1 / -1; grid-row: 2; }
  .account-actions { flex-wrap: wrap; }
  .account-actions .el-button { flex: 1; }
}
</style>
