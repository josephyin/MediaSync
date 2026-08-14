import { createApp } from 'vue'
import ElementPlus from 'element-plus'
import 'element-plus/dist/index.css'
import './styles.css'
import App from './App.vue'
import { setUnauthorizedHandler } from './api/client'
import router from './router'
import { clearAuthState } from './stores/auth'

setUnauthorizedHandler(() => {
  clearAuthState()
  const currentRoute = router.currentRoute.value
  if (currentRoute.path !== '/login') {
    void router.replace({ path: '/login', query: { redirect: currentRoute.fullPath } })
  }
})

createApp(App).use(ElementPlus).use(router).mount('#app')
