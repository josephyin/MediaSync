<script setup lang="ts">
import { ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import AppIcon from '../components/AppIcon.vue'
import { authState, logout } from '../stores/auth'

const route = useRoute()
const router = useRouter()
const sidebarOpen = ref(false)
const navItems = [
  { path: '/', label: '仪表盘', description: '运行概览', icon: 'dashboard' },
  { path: '/accounts', label: '云盘账号', description: '凭证与授权', icon: 'cloud' },
  { path: '/subscriptions', label: '分享订阅', description: '监控与转存', icon: 'subscription' },
  { path: '/files', label: '文件记录', description: '发现与结果', icon: 'files' },
  { path: '/tasks', label: '任务中心', description: '执行与诊断', icon: 'tasks' },
]

async function signOut() {
  await logout()
  await router.push('/login')
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
  </div>
</template>
