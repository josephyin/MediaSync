import { afterEach, describe, expect, it, vi } from 'vitest'
import { api, setUnauthorizedHandler } from './client'

afterEach(() => {
  setUnauthorizedHandler(() => undefined)
  vi.unstubAllGlobals()
})

describe('api authentication handling', () => {
  it('notifies the application when a request returns 401', async () => {
    const onUnauthorized = vi.fn()
    setUnauthorizedHandler(onUnauthorized)
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ detail: 'Session expired' }), {
          status: 401,
          headers: { 'Content-Type': 'application/json' },
        }),
      ),
    )

    await expect(api('/tasks')).rejects.toMatchObject({
      message: 'Session expired',
      status: 401,
    })
    expect(onUnauthorized).toHaveBeenCalledOnce()
  })

  it('does not expire the session for other request errors', async () => {
    const onUnauthorized = vi.fn()
    setUnauthorizedHandler(onUnauthorized)
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ detail: 'Service unavailable' }), {
          status: 503,
          headers: { 'Content-Type': 'application/json' },
        }),
      ),
    )

    await expect(api('/tasks')).rejects.toMatchObject({ status: 503 })
    expect(onUnauthorized).not.toHaveBeenCalled()
  })
})
