<script setup lang="ts">
import { computed } from 'vue'

const props = withDefaults(defineProps<{
  provider: string
  size?: number
}>(), {
  size: 42,
})

const providers: Record<string, { name: string; src: string; fallback: string }> = {
  aliyundrive: { name: '阿里云盘', src: '/providers/aliyundrive.jpg', fallback: '阿里' },
  quark: { name: '夸克网盘', src: '/providers/quark.jpg', fallback: '夸克' },
  pan123: { name: '123 云盘', src: '/providers/pan123.jpg', fallback: '123' },
  baidu: { name: '百度网盘', src: '/providers/baidu.jpg', fallback: '百度' },
}

const providerInfo = computed(() => providers[props.provider])
const fallback = computed(() => providers[props.provider]?.fallback || props.provider.slice(0, 3) || '?')
const logoStyle = computed(() => ({ '--provider-logo-size': `${props.size}px` }))
</script>

<template>
  <span class="provider-logo" :style="logoStyle">
    <img
      v-if="providerInfo"
      :src="providerInfo.src"
      :alt="`${providerInfo.name} Logo`"
      draggable="false"
    />
    <span v-else>{{ fallback }}</span>
  </span>
</template>

<style scoped>
.provider-logo {
  display: grid;
  place-items: center;
  flex: 0 0 var(--provider-logo-size);
  width: var(--provider-logo-size);
  height: var(--provider-logo-size);
  overflow: hidden;
  color: #fff;
  border: 1px solid rgb(16 24 40 / 7%);
  border-radius: calc(var(--provider-logo-size) * .29);
  background: linear-gradient(135deg, #6c5ce7, #2898ff);
  box-shadow: 0 7px 16px rgb(31 45 81 / 14%);
  font-size: calc(var(--provider-logo-size) * .25);
  font-weight: 800;
}

.provider-logo img {
  display: block;
  width: 100%;
  height: 100%;
  object-fit: cover;
  user-select: none;
}
</style>
