import { describe, expect, it } from 'vitest'
import type { ProviderInfo } from '../api/types'
import {
  findProvider,
  providerAvailabilityLabel,
  providerMark,
  providerName,
  supportsCapabilities,
  supportsSubscriptions,
} from './providers'

const providers: ProviderInfo[] = [
  {
    id: 'aliyundrive',
    name: 'Aliyun Drive',
    enabled: true,
    status: 'experimental',
    capabilities: ['account_verify', 'share_browse', 'folder_browse', 'share_save'],
  },
  {
    id: 'quark',
    name: 'Quark Drive',
    enabled: true,
    status: 'experimental',
    capabilities: ['account_verify', 'share_browse', 'folder_browse', 'folder_create', 'share_save'],
  },
  {
    id: '115',
    name: '115',
    enabled: false,
    capabilities: [],
  },
]

describe('provider presentation', () => {
  it('uses backend metadata and keeps a safe fallback for historical rows', () => {
    expect(findProvider(providers, 'aliyundrive')?.enabled).toBe(true)
    expect(providerName(providers, 'quark')).toBe('Quark Drive')
    expect(providerName(providers, 'unknown')).toBe('unknown')
    expect(providerMark(providers, 'aliyundrive')).toBe('Ali')
    expect(providerMark(providers, 'quark')).toBe('夸')
  })

  it('requires both enabled state and every requested capability', () => {
    expect(supportsCapabilities(providers[0], ['account_verify'])).toBe(true)
    expect(supportsSubscriptions(providers[0])).toBe(true)
    expect(supportsCapabilities(providers[0], ['folder_create'])).toBe(false)
    expect(supportsSubscriptions(providers[1])).toBe(true)
    expect(supportsSubscriptions(providers[2])).toBe(false)
    expect(supportsSubscriptions(undefined)).toBe(false)
  })

  it('labels disabled and experimental providers explicitly', () => {
    expect(providerAvailabilityLabel(providers[0])).toBe('实验性')
    expect(providerAvailabilityLabel(providers[1])).toBe('实验性')
    expect(providerAvailabilityLabel(providers[2])).toBe('尚未启用')
  })
})
