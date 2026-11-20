import { createApp } from 'vue'
import { createPinia } from 'pinia'
import TDesign from 'tdesign-vue-next'
import 'tdesign-vue-next/es/style/index.css'
import './styles/theme.css'
import App from './App.vue'
import router from './router'
import { initAuth } from './auth'

async function bootstrap() {
  await initAuth()
  const application = createApp(App).use(createPinia()).use(router).use(TDesign)
  await router.isReady()
  const route = router.currentRoute.value
  const cleanQuery = { ...route.query }
  for (const key of ['code', 'state', 'session_state', 'iss']) delete cleanQuery[key]
  if (Object.keys(cleanQuery).length !== Object.keys(route.query).length || /\b(code|state|session_state|iss)=/.test(route.hash)) {
    await router.replace({ path: route.path, query: cleanQuery, hash: '' })
  }
  application.mount('#app')
}

bootstrap().catch(error => {
  document.querySelector<HTMLDivElement>('#app')!.innerHTML = `
    <div style="font-family:sans-serif;padding:48px;color:#942c35">
      <h2>身份服务连接失败</h2><p>${String(error)}</p>
    </div>`
})

