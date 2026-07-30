<script setup lang="ts">
defineProps<{ name: string; size?: number }>()

const paths: Record<string, string[]> = {
  dashboard: ['M4 13h6V4H4v9Zm0 7h6v-4H4v4Zm10 0h6v-9h-6v9Zm0-16v4h6V4h-6Z'],
  cloud: ['M7 18h11a4 4 0 0 0 .6-7.96A6 6 0 0 0 7.35 8.1 5 5 0 0 0 7 18Z'],
  subscription: ['M5 5h14v14H5z', 'm8 4-4 3 4 3V9Z'],
  files: ['M6 3h8l4 4v14H6z', 'M14 3v5h5', 'M9 13h6M9 17h6'],
  tasks: ['M9 5h11M9 12h11M9 19h11', 'M4 5h.01M4 12h.01M4 19h.01'],
  menu: ['M4 6h16M4 12h16M4 18h16'],
  plus: ['M12 5v14M5 12h14'],
  refresh: ['M20 6v5h-5', 'M19 11a7 7 0 1 0 1 5'],
  search: ['m20 20-4.4-4.4', 'M18 11a7 7 0 1 1-14 0 7 7 0 0 1 14 0Z'],
  shield: ['M12 3 5 6v5c0 4.6 2.8 8.4 7 10 4.2-1.6 7-5.4 7-10V6l-7-3Z', 'm9 12 2 2 4-4'],
  logout: ['M10 5H5v14h5', 'M14 8l4 4-4 4M18 12H9'],
  user: ['M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8Z', 'M4 21a8 8 0 0 1 16 0'],
  lock: ['M7 11V8a5 5 0 0 1 10 0v3', 'M5 11h14v10H5z', 'M12 15v2'],
  arrow: ['m9 18 6-6-6-6'],
  folder: ['M3 6h7l2 2h9v11H3z'],
  database: ['M4 6c0-2 16-2 16 0s-16 2-16 0Z', 'M4 6v6c0 2 16 2 16 0V6', 'M4 12v6c0 2 16 2 16 0v-6'],
}
</script>

<template>
  <svg
    class="app-icon"
    :width="size ?? 20"
    :height="size ?? 20"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    stroke-width="1.8"
    stroke-linecap="round"
    stroke-linejoin="round"
    aria-hidden="true"
  >
    <path v-for="path in paths[name] ?? paths.files" :key="path" :d="path" />
  </svg>
</template>
