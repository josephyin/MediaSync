import { reactive } from 'vue'
import { api } from '../api/client'

interface AuthStatus {
  authenticated: boolean
  username?: string | null
  password_change_supported: boolean
}

export const authState = reactive({
  checked: false,
  authenticated: false,
  username: '',
  passwordChangeSupported: false,
})

export function clearAuthState(): void {
  authState.checked = true
  authState.authenticated = false
  authState.username = ''
  authState.passwordChangeSupported = false
}

export async function checkAuth(): Promise<boolean> {
  const result = await api<AuthStatus>('/auth/status')
  authState.checked = true
  authState.authenticated = result.authenticated
  authState.username = result.username || ''
  authState.passwordChangeSupported = result.password_change_supported
  return result.authenticated
}

export async function login(username: string, password: string): Promise<void> {
  const result = await api<AuthStatus>('/auth/login', {
    method: 'POST',
    body: JSON.stringify({ username, password }),
  })
  authState.authenticated = result.authenticated
  authState.username = result.username || username
  authState.passwordChangeSupported = result.password_change_supported
}

export async function logout(): Promise<void> {
  await api('/auth/logout', { method: 'POST' })
  clearAuthState()
}

export async function changePassword(
  currentPassword: string,
  newPassword: string,
  confirmPassword: string,
): Promise<void> {
  await api('/auth/password', {
    method: 'POST',
    body: JSON.stringify({
      current_password: currentPassword,
      new_password: newPassword,
      confirm_password: confirmPassword,
    }),
  })
  clearAuthState()
}
