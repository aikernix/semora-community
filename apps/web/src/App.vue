<script setup lang="ts">
import { computed, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  BrowseIcon, BookIcon, RelationIcon, DataBaseIcon,
  SettingIcon, ChevronLeftIcon, ChevronRightIcon,
} from 'tdesign-icons-vue-next'
import { authRequired, authState, hasRole, logout } from './auth'

const route = useRoute()
const router = useRouter()
const collapsed = ref(false)
const items = computed(() => [
  { path: '/workbench', label: '查询工作台', icon: BrowseIcon, visible: hasRole('business_user', 'analyst', 'admin') },
  { path: '/knowledge', label: '知识库', icon: BookIcon, visible: hasRole('business_user', 'analyst', 'data_steward', 'admin') },
  { path: '/ontology', label: '本体工作室', icon: RelationIcon, visible: hasRole('analyst', 'data_steward', 'admin') },
  { path: '/datasources', label: '数据源', icon: DataBaseIcon, visible: hasRole('analyst', 'data_steward', 'admin') },
  { path: '/settings', label: '模型与设置', icon: SettingIcon, visible: hasRole('admin') },
].filter(item => item.visible))
const roleLabel = computed(() => authState.roles.filter(role => ['analyst', 'approver', 'auditor', 'data_steward', 'admin'].includes(role)).join(' · ') || '业务用户')
const active = computed(() => route.path)
function navigate(path: string) { router.push(path) }
</script>

<template>
  <div class="app-shell" :class="{ 'management-shell': active !== '/workbench' }">
    <aside class="global-nav" :class="{ collapsed }">
      <div class="brand" @click="navigate('/workbench')">
        <svg class="brand-mark" viewBox="0 0 32 32" aria-hidden="true">
          <path fill="currentColor" d="M29 3c-7 0-15 2-20 6-6 5-4 10 5 11l3-4c-5-1-6-2-3-4 3-2 8-3 12-3zM3 29c7 0 15-2 20-6 6-5 4-10-5-11l-3 4c5 1 6 2 3 4-3 2-8 3-12 3z" />
        </svg>
        <span v-if="!collapsed" class="brand-word">Semora</span>
      </div>
      <button type="button" class="collapse-button" :aria-label="collapsed ? '展开导航' : '收起导航'" :title="collapsed ? '展开导航' : '收起导航'" :aria-expanded="!collapsed" aria-controls="primary-navigation" @click="collapsed = !collapsed">
        <ChevronRightIcon v-if="collapsed" /><ChevronLeftIcon v-else />
      </button>
      <nav id="primary-navigation" aria-label="主导航">
        <button v-for="item in items" :key="item.path" :class="{ active: active === item.path }" :title="item.label" :aria-label="item.label" :aria-current="active === item.path ? 'page' : undefined" @click="navigate(item.path)">
          <component :is="item.icon" size="22px" />
          <span v-if="!collapsed">{{ item.label }}</span>
        </button>
      </nav>
      <div v-if="authRequired && authState.authenticated" class="nav-footer">
        <button class="user" title="退出登录" @click="logout"><t-avatar size="34px">{{ authState.username.slice(0, 1).toUpperCase() }}</t-avatar><div v-if="!collapsed"><strong>{{ authState.username }}</strong><small>{{ roleLabel }}</small></div></button>
      </div>
    </aside>
    <main class="route-view"><router-view /></main>
  </div>
</template>

