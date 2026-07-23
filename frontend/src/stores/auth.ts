import { reactive } from 'vue'
import { api } from '../api/client'

interface AuthStatus {
  authenticated: boolean
  username?: string | null
}

export const authState = reactive({
  checked: false,
  authenticated: false,
  username: '',
})

export async function checkAuth(): Promise<boolean> {
  const result = await api<AuthStatus>('/auth/status')
  authState.checked = true
  authState.authenticated = result.authenticated
  authState.username = result.username || ''
  return result.authenticated
}

export async function login(username: string, password: string): Promise<void> {
  const result = await api<AuthStatus>('/auth/login', {
    method: 'POST',
    body: JSON.stringify({ username, password }),
  })
  authState.authenticated = result.authenticated
  authState.username = result.username || username
}

export async function logout(): Promise<void> {
  await api('/auth/logout', { method: 'POST' })
  authState.authenticated = false
  authState.username = ''
}
