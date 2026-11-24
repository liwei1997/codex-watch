import { BaseSideService } from '@zeppos/zml/base-side'
import { BRIDGE_URL, BRIDGE_TOKEN } from './bridge-config'

const BASE_URL = BRIDGE_URL.replace(/\/+$/, '')
const TIMEOUT = 10000

function bodyOf(value) {
  if (!value) return {}
  if (typeof value !== 'string') return value
  try { return JSON.parse(value) } catch (error) { return { raw: value } }
}

async function request(service, path, method = 'get', payload) {
  const response = await service.fetch({
    url: BASE_URL + path,
    method,
    headers: {
      Authorization: 'Bearer ' + BRIDGE_TOKEN,
      'Content-Type': 'application/json',
      Accept: 'application/json'
    },
    body: payload ? JSON.stringify(payload) : undefined,
    timeout: TIMEOUT
  })
  const data = bodyOf(response && response.body)
  if (response && response.status >= 400) {
    throw new Error(data.error || 'Bridge HTTP ' + response.status)
  }
  return data
}

AppSideService(BaseSideService({
  onInit() {
    console.log('[Codex Watch] Side Service initialized')
  },

  async onRequest(req, res) {
    const params = req.params || {}
    try {
      if (req.method === 'bridge.version') {
        res(null, { ok: true, js: '120-20261124', methods: [
          'bridge.version', 'bridge.poll', 'bridge.approval.list',
          'bridge.approval.decision', 'bridge.thread.reply'
        ] })
        return
      }
      if (BASE_URL.includes('YOUR_UBUNTU') || BRIDGE_TOKEN.startsWith('PASTE_')) {
        throw new Error('Configure BRIDGE_URL and BRIDGE_TOKEN')
      }
      if (req.method === 'bridge.poll') {
        const limit = Math.max(1, Math.min(50, Number(params.limit) || 20))
        const data = await request(this, '/v1/notifications?limit=' + limit)
        res(null, data)
        return
      }
      if (req.method === 'bridge.approval.list') {
        const status = encodeURIComponent(params.status || 'pending')
        const limit = Math.max(1, Math.min(50, Number(params.limit) || 20))
        res(null, await request(this, '/v1/approvals?status=' + status + '&limit=' + limit))
        return
      }
      if (req.method === 'bridge.approval.decision') {
        const id = String(params.approval_id || '')
        const decision = String(params.decision || '').toLowerCase()
        if (!id || !['accept', 'decline'].includes(decision)) {
          throw new Error('Invalid approval decision')
        }
        res(null, await request(
          this,
          '/v1/approvals/' + encodeURIComponent(id) + '/decision',
          'post',
          { decision }
        ))
        return
      }
      if (req.method === 'bridge.notification.ack') {
        if (!params.event_id) throw new Error('event_id required')
        res(null, await request(this, '/v1/notifications/ack', 'post', {
          event_id: String(params.event_id)
        }))
        return
      }
      if (req.method === 'bridge.thread.reply') {
        const threadId = String(params.thread_id || '')
        const text = String(params.text || '').trim()
        if (!threadId || !text) throw new Error('thread_id and text required')
        res(null, await request(this, '/v1/threads/reply', 'post', {
          thread_id: threadId,
          text
        }))
        return
      }
      res('Unknown method: ' + req.method)
    } catch (error) {
      console.error('[Codex Watch] Request failed: ' + String(error))
      res(null, { ok: false, error: error.message || String(error) })
    }
  },

  onCall(data) {
    console.log('[Codex Watch] Watch call: ' + JSON.stringify(data))
  }
}))
