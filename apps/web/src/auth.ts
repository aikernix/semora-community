import Keycloak from 'keycloak-js'
import { reactive } from 'vue'

declare global {
  interface Window {
    __SEMORA_CONFIG__?: {
      authRequired?: boolean
      keycloakUrl?: string
      keycloakRealm?: string
      keycloakClientId?: string
    }
  }
}

const runtimeConfig = window.__SEMORA_CONFIG__ ?? {}
export const authRequired = runtimeConfig.authRequired
  ?? (import.meta.env.VITE_AUTH_REQUIRED ?? 'false') === 'true'

export const keycloak = new Keycloak({
  url: runtimeConfig.keycloakUrl ?? import.meta.env.VITE_KEYCLOAK_URL ?? 'http://localhost:8088',
  realm: runtimeConfig.keycloakRealm ?? import.meta.env.VITE_KEYCLOAK_REALM ?? 'semora',
  clientId: runtimeConfig.keycloakClientId ?? import.meta.env.VITE_KEYCLOAK_CLIENT_ID ?? 'semora-web',
})

export const authState = reactive({
  ready: false,
  authenticated: false,
  username: '演示分析员',
  roles: ['business_user', 'analyst'] as string[],
})

export function hasRole(...roles: string[]) {
  return !authRequired || roles.some(role => authState.roles.includes(role))
}

export async function initAuth() {
  if (!authRequired) {
    authState.ready = true
    authState.authenticated = true
    return
  }
  const authenticated = await keycloak.init({
    onLoad: 'login-required',
    pkceMethod: 'S256',
    checkLoginIframe: false,
    flow: 'standard',
    responseMode: 'query',
  })
  authState.authenticated = authenticated
  authState.username = keycloak.tokenParsed?.preferred_username || keycloak.tokenParsed?.name || '已登录用户'
  authState.roles = keycloak.realmAccess?.roles || []
  authState.ready = true
}

export async function refreshToken() {
  if (!authRequired || !keycloak.authenticated) return undefined
  await keycloak.updateToken(30)
  return keycloak.token
}

export function logout() {
  if (authRequired) {
    void keycloak.logout({ redirectUri: window.location.origin })
  }
}
