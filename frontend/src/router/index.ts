import { createRouter, createWebHistory } from 'vue-router'

import Overview from '@/views/Overview.vue'
import RollbackView from '@/views/RollbackView.vue'
import WorkbenchView from '@/views/WorkbenchView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'overview', component: Overview },
    { path: '/schema_rollback', name: 'schema_rollback', component: RollbackView },
    { path: '/:moduleKey', name: 'module', component: WorkbenchView, props: true },
  ],
})

export default router
