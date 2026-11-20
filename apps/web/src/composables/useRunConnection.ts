import { onUnmounted, ref, type Ref } from 'vue'
import { fetchEventSource } from '@microsoft/fetch-event-source'
import { api, type QueryRun } from '../api'
import { refreshToken } from '../auth'
import { mergeRunEvent, terminalStatus } from '../queryPresentation'

export function useRunConnection(run: Ref<QueryRun | null>) {
  const state = ref<'idle' | 'connecting' | 'live' | 'reconnecting'>('idle')
  const lastSignal = ref(0)
  let generation = 0
  let attempt: AbortController | null = null
  let wake: (() => void) | null = null

  function stop() {
    generation++
    attempt?.abort()
    attempt = null
    wake?.()
    wake = null
    state.value = 'idle'
    lastSignal.value = 0
  }

  async function start(id: string) {
    stop()
    const current = generation
    const active = () => current === generation && run.value?.id === id
    let pendingSnapshot: Promise<void> | null = null
    const snapshot = () => {
      if (pendingSnapshot) return pendingSnapshot
      pendingSnapshot = (async () => {
        if (!active()) return
        const { data } = await api.get<QueryRun>(`/runs/${id}`, { timeout: 10_000 })
        if (active() && data.events.length >= (run.value?.events.length || 0)) run.value = data
      })().finally(() => { pendingSnapshot = null })
      return pendingSnapshot
    }
    const watchdog = setInterval(() => {
      if (active() && lastSignal.value && Date.now() - lastSignal.value > 20_000) {
        state.value = 'reconnecting'
        attempt?.abort()
      }
    }, 1000)
    let retry = 0
    state.value = 'connecting'
    try {
      while (active() && !terminalStatus(run.value!.status)) {
        const controller = new AbortController()
        attempt = controller
        lastSignal.value = Date.now()
        let closed = false
        try {
          const token = await refreshToken()
          if (!active()) return
          await fetchEventSource(`/api/v1/runs/${id}/events?after=${run.value!.events.length}&heartbeat=true`, {
            headers: token ? { Authorization: `Bearer ${token}` } : {},
            signal: controller.signal,
            openWhenHidden: true,
            async onopen(response) {
              if (!response.ok || !response.headers.get('content-type')?.includes('text/event-stream')) throw new Error('stream unavailable')
              if (!active()) { controller.abort(); return }
              state.value = 'live'
              retry = 0
              lastSignal.value = Date.now()
              await snapshot()
            },
            onmessage(message) {
              if (!active() || controller.signal.aborted) return
              lastSignal.value = Date.now()
              state.value = 'live'
              if (message.event === 'stream.closed') { closed = true; controller.abort(); return }
              if (!message.data) return
              const event = JSON.parse(message.data)
              if (message.event === 'run.heartbeat') {
                if (event.cursor > run.value!.events.length || event.status !== run.value!.status) void snapshot().catch(() => {})
                return
              }
              const merged = mergeRunEvent(run.value!, Number(message.id), event)
              if (merged === 'gap' || merged === 'applied' && !message.event.startsWith('agent.')) void snapshot().catch(() => {})
            },
            onclose() { if (!closed && active()) throw new Error('stream disconnected') },
            onerror(error) { throw error },
          })
        } catch { /* Recover this run's snapshot; never resubmit its question. */ }
        if (!active()) return
        try {
          await snapshot()
          if (closed && active()) await snapshot()
        } catch { /* Retain the last known run. */ }
        if (!active() || terminalStatus(run.value!.status)) break
        state.value = 'reconnecting'
        await new Promise<void>(resolve => {
          const timer = setTimeout(() => { wake = null; resolve() }, Math.min(5000, 1000 * ++retry))
          wake = () => { clearTimeout(timer); resolve() }
        })
      }
    } finally {
      clearInterval(watchdog)
      if (active()) { state.value = 'idle'; attempt = null }
    }
  }

  onUnmounted(stop)
  return { state, lastSignal, start, stop }
}
