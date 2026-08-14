<script setup lang="ts">
import { reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { useRoute, useRouter } from 'vue-router'
import AppIcon from '../components/AppIcon.vue'
import { login } from '../stores/auth'

const router = useRouter()
const route = useRoute()
const loading = ref(false)
const form = reactive({ username: 'admin', password: '' })

async function submit() {
  if (!form.username.trim() || !form.password) {
    ElMessage.warning('请输入管理员账号和密码')
    return
  }
  loading.value = true
  try {
    await login(form.username.trim(), form.password)
    const redirect = typeof route.query.redirect === 'string' ? route.query.redirect : '/'
    await router.replace(redirect.startsWith('/') && !redirect.startsWith('//') ? redirect : '/')
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '登录失败')
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="login-page">
    <section class="login-visual">
      <div class="brand"><span class="brand-mark"><img src="/mediasync-logo.svg" alt="" /></span><div class="brand-copy"><strong>MediaSync</strong><small>SELF-HOSTED MEDIA AUTOMATION</small></div></div>
      <div>
        <span class="eyebrow">YOUR MEDIA, ALWAYS IN SYNC</span>
        <h1>让家庭影音资源<br>自动抵达媒体库</h1>
        <p>持续监控分享内容、增量发现新资源并安全转存到个人云盘，为 OpenList、SmartStrm 和媒体服务器提供稳定的数据入口。</p>
      </div>
      <div class="login-flow"><span>资源分享</span>→<span>MediaSync</span>→<span>个人云盘</span>→<span>媒体库</span></div>
    </section>
    <section class="login-panel">
      <el-card class="login-card" shadow="never">
        <div class="login-heading">
          <span class="summary-icon"><AppIcon name="user" :size="20" /></span>
          <div><h2>欢迎回来</h2><p>登录 MediaSync 管理控制台</p></div>
        </div>
        <el-form label-position="top" @submit.prevent="submit">
          <el-form-item label="管理员账号"><el-input v-model="form.username" size="large" autocomplete="username" placeholder="请输入账号" /></el-form-item>
          <el-form-item label="密码"><el-input v-model="form.password" size="large" type="password" autocomplete="current-password" show-password placeholder="请输入密码" /></el-form-item>
          <el-button type="primary" native-type="submit" :loading="loading" class="full-width login-submit">登录控制台</el-button>
        </el-form>
      </el-card>
    </section>
  </div>
</template>
