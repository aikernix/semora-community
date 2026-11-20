<script setup lang="ts">
import { computed } from 'vue'
import { DataBaseIcon } from 'tdesign-icons-vue-next'

const props = defineProps<{ type?: string; branded?: boolean }>()
const product = computed(() => {
  const type = props.type?.toLowerCase().replace(/[\s_-]/g, '')
  if (type === 'postgres') return 'postgresql'
  if (type === 'mssql' || type === 'microsoftsqlserver') return 'sqlserver'
  return type
})
</script>

<template>
  <span class="database-icon" aria-hidden="true">
    <svg v-if="product === 'duckdb'" viewBox="0 0 24 24">
      <circle cx="12" cy="12" r="11" fill="#171717" />
      <circle cx="9" cy="12" r="4" fill="#ffdf22" />
      <rect x="15" y="10.5" width="3" height="3" fill="#ffdf22" />
    </svg>
    <svg v-else-if="product === 'postgresql'" viewBox="0 0 24 24">
      <path d="M6.6 3C2.8 1.7 1 4.3 2 9.4l1.6 5.1c.8 2.6 2.8 2.9 4.4 1l2 .5v3.6c0 2 1.1 3 2.7 3 2.2 0 3.2-1.3 3.5-3.5l.3-2.4c2.1.3 4.2-.5 5.1-2.6-1.4.5-2.5.5-3.3.1 1.4-2.6 2.1-5.5 2.3-8 .2-3.6-3.9-5-7.6-3.3-2.2-.8-4.1-.7-6.4.1z" fill="#336791" stroke="#254862" stroke-width="1.1" />
      <path d="M6.6 4C4 3.3 3.1 5.2 3.5 8.2L5 14c.5 1.6 1.2 1.7 2.3.4m6.8-10.6c2.8-1.1 5.6-.4 5.1 3.1-.3 2.8-1.2 5.2-2.6 7.3m-7.2-.5c-1.5.3-2.5-.4-2.5-2.4V8.8c0-2.7 1.7-4.5 4.2-4.5 2.9 0 4.6 1.7 4.6 4.7v2.2c0 1.8-.4 3.1-1.1 4.6l-.5 3c-.2 1.5-.6 2.2-1.5 2.2-1 0-1.3-.7-1.3-1.9v-5.7m-4 2.2c1.3 1.1 2.5 1.2 3.7.4m5.2-.3c1 .4 2.1.2 3.1-.3M8.9 8.7h.2m4.5 0h.2" fill="none" stroke="#fff" stroke-width=".85" stroke-linecap="round" stroke-linejoin="round" />
    </svg>
    <svg v-else-if="product === 'mysql' && branded" viewBox="0 0 44 44">
      <path d="M37 27c-1.2-1.5-3.3-2.9-6-4l4-1-3.8-3c-2.2-3.6-4-6.5-7.1-9.1-2.4-2.1-5.5-3.1-7.7-3.3-1.8-1.3-3.4-2-4.1-1.2-.6 1.1 1.3 3.2 2.8 4.4-.1 2.4 1 4.4 2.4 6.1-1.2 2.6-1.1 4.7.4 6.5l1.1-3.5c.7 1.6 1.4 2.5 2.6 3.4" fill="none" stroke="#006b9a" stroke-width="1.35" stroke-linecap="round" stroke-linejoin="round" />
      <circle cx="18.4" cy="10.5" r=".75" fill="#006b9a" />
      <text x="22" y="36" text-anchor="middle" font-size="10" font-weight="650" font-family="system-ui, sans-serif" fill="#006b9a">My<tspan fill="#bd6509">SQL</tspan></text>
    </svg>
    <svg v-else-if="product === 'mysql' || product === 'mariadb'" viewBox="0 0 24 24">
      <path d="M22 21c-1.1-1.4-2.7-2.4-4.8-3.2l3.5-.8-3.1-2.4c-1.4-2.1-2.9-5-5.5-7.5-2.5-2.5-5.4-3-7-3.2C3.8 2.8 2.2 2 1.8 2.8c-.4 1 1.2 2.5 2.3 3.6 0 2 .8 3.6 2 5-1.2 2.1-.9 4.6.4 6.1l.8-3.1c.7 1.6 1.3 2.5 2.4 3.2" fill="none" stroke="#006b9a" stroke-width="1.35" stroke-linecap="round" stroke-linejoin="round" />
      <circle cx="6.6" cy="6.5" r=".65" fill="#006b9a" />
    </svg>
    <svg v-else-if="product === 'oracle'" viewBox="0 0 24 24">
      <rect x="2" y="6" width="20" height="12" rx="6" fill="none" stroke="#c74634" stroke-width="3" />
    </svg>
    <svg v-else-if="product === 'sqlserver'" viewBox="0 0 24 24">
      <path d="M4 20 20 16 14 4 6 2l4 9-6 9zm0 0 10-16m-4 7 10 5m-10-5 7-1m-13 10 13-10" fill="none" stroke="#b42c36" stroke-width="1.3" stroke-linejoin="round" />
    </svg>
    <svg v-else-if="product === 'oceanbase'" viewBox="0 0 24 24">
      <path d="M21 8a9 9 0 1 0-2 10M3 13c5-5 11 5 18-1" fill="none" stroke="#1763c6" stroke-width="3" stroke-linecap="round" />
    </svg>
    <svg v-else-if="product === 'goldendb'" viewBox="0 0 24 24">
      <path d="m12 2 10 6-10 6L2 8zm-9 11 9 5 9-5m-18 4 9 5 9-5" fill="none" stroke="#a96a0c" stroke-width="2" stroke-linejoin="round" />
    </svg>
    <svg v-else-if="product === 'vastbase'" viewBox="0 0 24 24">
      <path d="m4 5 8 15 8-15m-11 0 3 6 3-6" fill="none" stroke="#167c94" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" />
    </svg>
    <DataBaseIcon v-else />
  </span>
</template>

<style scoped>
.database-icon {
  display: inline-grid;
  place-items: center;
  flex: none;
  width: var(--database-icon-size, 36px);
  height: var(--database-icon-size, 36px);
  color: #1958a6;
}
.database-icon > svg { width: 100%; height: 100%; }
</style>
