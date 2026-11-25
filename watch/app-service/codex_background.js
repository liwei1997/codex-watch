import { BasePage } from '@zeppos/zml/base-page'
import { connectStatus } from '@zos/ble'
import { notify, cancel } from '@zos/notification'
import { createSysTimer, stopTimer } from '@zos/timer'
import { createSystemRequestRunner } from './system_request_runner'
import { isBleConnected } from '../common/phone_link'
import { cleanCommand, projectOf, buildApprovalActionParam } from '../common/format'

const TAG = '[Codex Watch Background]'
const POLL_INTERVAL_MS = 10000
const REQUEST_TIMEOUT = 15000
const ACTION_SERVICE = 'app-service/approval_action'
const REPLY_PAGE = 'page/reply'
const phoneRequest = createSystemRequestRunner({
  timeoutMs: REQUEST_TIMEOUT,
  createTimer: createSysTimer,
  stopTimer
})

let pollRunning = false
let pollAgain = false
let destroyed = false
let pollTimerId = null
let bridgeConnected = false
let connectionState = 'unknown'
const approvalNotifications = {}

function log(message) {
  console.log(TAG + ' ' + message)
}

function setConnectionState(next) {
  if (connectionState === next) return
  connectionState = next
  log('connection=' + next)
}

function notifyEvent(event) {
  const payload = event.payload || {}
  const approval = event.type === 'approval_request'
  const id = String(approval ? payload.approval_id || '' : '')
  const title = approval
    ? 'APPROVAL · ' + projectOf(payload.cwd)
    : String(event.title || 'CODEX')
  const content = approval
    ? cleanCommand(payload.command) || event.body || 'Approval required'
    : String(event.body || 'Codex turn completed').replace(/\s+/g, ' ').trim().slice(0, 160)
  const actions = approval && id ? [
    { text: 'APPROVE', file: ACTION_SERVICE, param: buildApprovalActionParam(id, 'accept') },
    { text: 'REJECT', file: ACTION_SERVICE, param: buildApprovalActionParam(id, 'decline') }
  ] : [{
    text: 'REPLY',
    file: REPLY_PAGE,
    param: JSON.stringify({
      thread_id: String(payload.thread_id || ''),
      project: projectOf(payload.cwd),
      event_id: String(event.event_id || '')
    })
  }]
  const notificationId = notify({
    title: title.slice(0, 60),
    content: content.slice(0, 160),
    actions,
    vibrate: approval ? 1 : 0
  })
  if (!notificationId) {
    log('notification rejected event=' + String(event.event_id || ''))
    return false
  }
  log('notification sent event=' + String(event.event_id || '') + ' id=' + notificationId)
  if (approval && id) approvalNotifications[id] = notificationId
  return true
}

AppService(BasePage({
  state: {},
  build() {},

  onInit() {
    destroyed = false
    pollRunning = false
    pollAgain = false
    bridgeConnected = false
    connectionState = 'unknown'
    setConnectionState(isBleConnected(connectStatus) ? 'ble-connected' : 'ble-disconnected')
    log('service started')
    if (isBleConnected(connectStatus)) this.pollBridge()
    try {
      pollTimerId = createSysTimer(true, POLL_INTERVAL_MS, () => this.pollBridge())
    } catch (error) {
      log('poll timer unavailable: ' + String(error))
    }
  },

  onBleChanged(connected) {
    if (connected !== true) {
      bridgeConnected = false
      setConnectionState('ble-disconnected')
      return
    }
    setConnectionState('ble-connected')
    this.pollBridge(true)
  },

  requestPhone(method, params) {
    if (!isBleConnected(connectStatus)) {
      const error = new Error('BLE disconnected')
      error.code = 2
      return Promise.reject(error)
    }
    return phoneRequest.run(
      method,
      () => this.request({ method, params }, { timeout: REQUEST_TIMEOUT })
    )
  },

  async pollBridge(force = false) {
    if (destroyed) return
    if (pollRunning) {
      if (force) pollAgain = true
      return
    }
    if (!isBleConnected(connectStatus)) {
      bridgeConnected = false
      setConnectionState('ble-disconnected')
      return
    }

    pollRunning = true
    try {
      const result = await this.requestPhone('bridge.poll', { limit: 20 })
      if (destroyed) return
      if (!result || result.ok !== true) throw new Error(result && result.error || 'invalid bridge response')
      if (!bridgeConnected) log('bridge polling active')
      bridgeConnected = true
      setConnectionState('connected')
      const notifications = result.notifications || []
      if (notifications.length) log('received notifications=' + notifications.length)
      for (const event of notifications) {
        if (destroyed) break
        try {
          if (!notifyEvent(event)) continue
          const ack = await this.requestPhone('bridge.notification.ack', {
            event_id: event.event_id
          })
          if (!ack || ack.ok !== true) throw new Error(ack && ack.error || 'notification acknowledgement failed')
          log('notification acknowledged event=' + String(event.event_id || ''))
        } catch (error) {
          log('delivery failed: ' + String(error))
          if (!isBleConnected(connectStatus)) break
        }
      }
      if (Object.keys(approvalNotifications).length) await this.checkStaleApprovals()
    } catch (error) {
      bridgeConnected = false
      if (!isBleConnected(connectStatus) || Number(error && error.code) === 2) {
        setConnectionState('ble-disconnected')
      } else if ([1, 3].includes(Number(error && error.code))) {
        setConnectionState('side-service-waiting')
      } else if (Number(error && error.code) === 4) {
        setConnectionState('phone-link-timeout')
      } else {
        setConnectionState('bridge-offline')
      }
      log('poll failed: ' + String(error))
    } finally {
      pollRunning = false
      if (pollAgain && !destroyed) {
        pollAgain = false
        this.pollBridge()
      }
    }
  },

  async checkStaleApprovals() {
    const result = await this.requestPhone('bridge.approval.list', {
      status: 'pending',
      limit: 50
    })
    if (destroyed || !result || result.ok !== true) return
    const pending = new Set((result.approvals || []).map(item => String(item.approval_id)))
    Object.keys(approvalNotifications).forEach(id => {
      if (pending.has(id)) return
      cancel([approvalNotifications[id]])
      delete approvalNotifications[id]
    })
  },

  onDestroy() {
    log('service stopped')
    destroyed = true
    pollAgain = false
    if (pollTimerId !== null) {
      try { stopTimer(pollTimerId) } catch (error) {}
      pollTimerId = null
    }
  }
}))
