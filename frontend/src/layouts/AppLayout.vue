<script setup lang="ts">
import { ElMessage } from 'element-plus'
import { reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import AppIcon from '../components/AppIcon.vue'
import { authState, changePassword, logout } from '../stores/auth'

const route = useRoute()
const router = useRouter()
const sidebarOpen = ref(false)
const passwordDialogOpen = ref(false)
const passwordSubmitting = ref(false)
const passwordForm = reactive({
  currentPassword: '',
  newPassword: '',
  confirmPassword: '',
})
const navItems = [
  { path: '/', label: '仪表盘', description: '运行概览', icon: 'dashboard' },
  { path: '/accounts', label: '云盘账号', description: '凭证与授权', icon: 'cloud' },
  { path: '/subscriptions', label: '分享订阅', description: '监控与转存', icon: 'subscription' },
  { path: '/files', label: '文件记录', description: '发现与结果', icon: 'files' },
  { path: '/tasks', label: '任务中心', description: '执行与诊断', icon: 'tasks' },
  { path: '/system', label: '系统设置', description: '版本与更新', icon: 'settings' },
]

async function signOut() {
  await logout()
  await router.push('/login')
}

function resetPasswordForm() {
  passwordForm.currentPassword = ''
  passwordForm.newPassword = ''
  passwordForm.confirmPassword = ''
}

async function submitPasswordChange() {
  if (!passwordForm.currentPassword || !passwordForm.newPassword || !passwordForm.confirmPassword) {
    ElMessage.warning('请完整填写三个密码字段')
    return
  }
  if (passwordForm.newPassword.length < 8) {
    ElMessage.warning('新密码至少需要 8 个字符')
    return
  }
  if (passwordForm.newPassword !== passwordForm.confirmPassword) {
    ElMessage.warning('两次输入的新密码不一致')
    return
  }

  passwordSubmitting.value = true
  try {
    await changePassword(
      passwordForm.currentPassword,
      passwordForm.newPassword,
      passwordForm.confirmPassword,
    )
    passwordDialogOpen.value = false
    ElMessage.success('密码已修改，请使用新密码重新登录')
    await router.push('/login')
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '密码修改失败')
  } finally {
    passwordSubmitting.value = false
  }
}

function navigate(path: string) {
  sidebarOpen.value = false
  router.push(path)
}
</script>

<template>
  <div class="app-shell" :class="{ 'sidebar-open': sidebarOpen }">
    <button class="sidebar-overlay" aria-label="关闭导航" @click="sidebarOpen = false" />
    <aside class="sidebar">
      <div class="brand">
        <span class="brand-mark"><img src="/mediasync-logo.svg" alt="" /></span>
        <div class="brand-copy"><strong>MediaSync</strong><small>影音云盘同步中心</small></div>
      </div>
      <div class="nav-caption">工作台</div>
      <nav class="side-nav" aria-label="主导航">
        <button
          v-for="item in navItems"
          :key="item.path"
          class="nav-item"
          :class="{ active: route.path === item.path }"
          @click="navigate(item.path)"
        >
          <span class="nav-icon"><AppIcon :name="item.icon" :size="19" /></span>
          <span><strong>{{ item.label }}</strong><small>{{ item.description }}</small></span>
          <AppIcon name="arrow" :size="15" class="nav-arrow" />
        </button>
      </nav>
      <div class="sidebar-safety">
        <AppIcon name="shield" :size="18" />
        <div><strong>请求防护运行中</strong><small>节流 · 退避 · 目录分批</small></div>
      </div>
      <div class="sidebar-footer">
        <span class="user-avatar">{{ (authState.username || 'A').slice(0, 1).toUpperCase() }}</span>
        <div class="user-copy"><strong>{{ authState.username }}</strong><small>管理员</small></div>
        <el-tooltip content="修改管理员密码" placement="top">
          <button class="icon-button dark" aria-label="修改管理员密码" @click="passwordDialogOpen = true"><AppIcon name="lock" :size="18" /></button>
        </el-tooltip>
        <el-tooltip content="退出登录" placement="top">
          <button class="icon-button dark" aria-label="退出登录" @click="signOut"><AppIcon name="logout" :size="18" /></button>
        </el-tooltip>
      </div>
    </aside>

    <main class="main-shell">
      <header class="topbar">
        <button class="mobile-menu" aria-label="打开导航" @click="sidebarOpen = true"><AppIcon name="menu" :size="22" /></button>
        <div><span class="topbar-kicker">MEDIASYNC</span><strong>{{ route.meta.title }}</strong></div>
        <div class="topbar-status"><i />服务在线</div>
      </header>
      <div class="main-content"><RouterView /></div>
    </main>

    <el-dialog
      v-model="passwordDialogOpen"
      title="修改管理员密码"
      width="min(460px, calc(100vw - 32px))"
      destroy-on-close
      @closed="resetPasswordForm"
    >
      <el-alert
        v-if="!authState.passwordChangeSupported"
        title="当前部署模式不支持在线修改"
        type="warning"
        :closable="false"
        show-icon
      >
        高级 Compose 部署请修改 .env 中的 ADMIN_PASSWORD，并重建 API 容器。
      </el-alert>
      <el-form v-else label-position="top" @submit.prevent="submitPasswordChange">
        <el-form-item label="当前密码">
          <el-input
            v-model="passwordForm.currentPassword"
            type="password"
            autocomplete="current-password"
            show-password
          />
        </el-form-item>
        <el-form-item label="新密码">
          <el-input
            v-model="passwordForm.newPassword"
            type="password"
            autocomplete="new-password"
            maxlength="128"
            show-password
          />
          <div class="form-tip">至少 8 个字符，推荐使用容易记忆的长密码短语。</div>
        </el-form-item>
        <el-form-item label="确认新密码">
          <el-input
            v-model="passwordForm.confirmPassword"
            type="password"
            autocomplete="new-password"
            maxlength="128"
            show-password
            @keyup.enter="submitPasswordChange"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="passwordDialogOpen = false">取消</el-button>
        <el-button
          v-if="authState.passwordChangeSupported"
          type="primary"
          :loading="passwordSubmitting"
          @click="submitPasswordChange"
        >
          修改并重新登录
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>
