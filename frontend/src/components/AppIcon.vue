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
  settings: ['M12 15.5a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7Z', 'M19.4 15a1.7 1.7 0 0 0 .34 1.88l.06.06-2 3.46-.08-.02a1.7 1.7 0 0 0-1.8.65l-.44.76h-4l-.44-.76a1.7 1.7 0 0 0-1.8-.65l-.08.02-2-3.46.06-.06A1.7 1.7 0 0 0 7.6 15l-.4-.7a1.7 1.7 0 0 0-1.46-.85H5.6v-4h.14A1.7 1.7 0 0 0 7.2 8.6l.4-.7a1.7 1.7 0 0 0-.34-1.88l-.06-.06 2-3.46.08.02a1.7 1.7 0 0 0 1.8-.65l.44-.76h4l.44.76a1.7 1.7 0 0 0 1.8.65l.08-.02 2 3.46-.06.06A1.7 1.7 0 0 0 19.4 7.9l.4.7a1.7 1.7 0 0 0 1.46.85h.14v4h-.14a1.7 1.7 0 0 0-1.46.85l-.4.7Z'],
  external: ['M14 4h6v6', 'm20 4-9 9', 'M18 13v7H4V6h7'],
  copy: ['M8 8h12v12H8z', 'M4 16V4h12'],
  scan: ['M4 9V5a1 1 0 0 1 1-1h4', 'M15 4h4a1 1 0 0 1 1 1v4', 'M20 15v4a1 1 0 0 1-1 1h-4', 'M9 20H5a1 1 0 0 1-1-1v-4', 'M8 8h3v3H8zM14 8h2v2h-2zM8 14h2v2H8zM13 13h3v3h-3z'],
  browser: ['M3 5h18v14H3z', 'M3 9h18', 'M7 7h.01M10 7h.01'],
  key: ['M15 7a4 4 0 1 1-1.2 2.85L10 14H7v3H4v3H1v-4l9.15-9.15A4 4 0 0 1 15 7Z'],
  check: ['m5 12 4 4L19 6'],
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
