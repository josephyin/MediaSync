import type { ProviderInfo } from '../api/types'

export const SUBSCRIPTION_CAPABILITIES = [
  'share_browse',
  'folder_browse',
  'share_save',
] as const

export function findProvider(providers: ProviderInfo[], providerId: string): ProviderInfo | undefined {
  return providers.find((provider) => provider.id === providerId)
}

export function providerName(providers: ProviderInfo[], providerId: string): string {
  return findProvider(providers, providerId)?.name ?? providerId
}

export function providerMark(providers: ProviderInfo[], providerId: string): string {
  const name = providerName(providers, providerId)
  if (providerId === 'aliyundrive') return 'Ali'
  if (providerId === 'quark') return '夸'
  return name.slice(0, 3)
}

export function supportsCapabilities(
  provider: ProviderInfo | undefined,
  capabilities: readonly string[],
): boolean {
  return Boolean(
    provider?.enabled
    && capabilities.every((capability) => provider.capabilities.includes(capability)),
  )
}

export function supportsSubscriptions(provider: ProviderInfo | undefined): boolean {
  return supportsCapabilities(provider, SUBSCRIPTION_CAPABILITIES)
}

export function providerAvailabilityLabel(provider: ProviderInfo): string {
  if (!provider.enabled) return '尚未启用'
  if (provider.status === 'experimental') return '实验性'
  if (provider.status === 'partial') return '部分能力'
  return '可用'
}
