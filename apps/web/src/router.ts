import { createRouter, createWebHistory } from 'vue-router'
import WorkbenchView from './views/WorkbenchView.vue'
import KnowledgeView from './views/KnowledgeView.vue'
import OntologyView from './views/OntologyView.vue'
import DataSourcesView from './views/DataSourcesView.vue'
import SettingsView from './views/SettingsView.vue'

export default createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', redirect: '/workbench' },
    { path: '/workbench', component: WorkbenchView },
    { path: '/knowledge', component: KnowledgeView },
    { path: '/ontology', component: OntologyView },
    { path: '/datasources', component: DataSourcesView },
    { path: '/settings', component: SettingsView },
  ],
})

