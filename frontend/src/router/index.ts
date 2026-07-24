import { createRouter, createWebHistory } from 'vue-router'
import AppLayout from '../layouts/AppLayout.vue'
import { authState, checkAuth } from '../stores/auth'

const LoginView = () => import('../views/LoginView.vue')
const DashboardView = () => import('../views/DashboardView.vue')
const AccountsView = () => import('../views/AccountsView.vue')
const SubscriptionsView = () => import('../views/SubscriptionsView.vue')
const FilesView = () => import('../views/FilesView.vue')
const TasksView = () => import('../views/TasksView.vue')

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/login', component: LoginView, meta: { public: true, title: '登录' } },
    {
      path: '/',
      component: AppLayout,
      children: [
        { path: '', component: DashboardView, meta: { title: '仪表盘' } },
        { path: 'accounts', component: AccountsView, meta: { title: '云盘账号' } },
        { path: 'subscriptions', component: SubscriptionsView, meta: { title: '分享订阅' } },
        { path: 'files', component: FilesView, meta: { title: '文件记录' } },
        { path: 'tasks', component: TasksView, meta: { title: '任务中心' } },
      ],
    },
  ],
})

router.beforeEach(async (to) => {
  if (!authState.checked) {
    try {
      await checkAuth()
    } catch {
      authState.checked = true
    }
  }
  if (!to.meta.public && !authState.authenticated) return '/login'
  if (to.path === '/login' && authState.authenticated) return '/'
})

router.afterEach((to) => {
  document.title = to.meta.title ? `${String(to.meta.title)} · MediaSync` : 'MediaSync'
})

export default router
