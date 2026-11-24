import { BasePage } from '@zeppos/zml/base-page'
import { notify, cancel } from '@zos/notification'
import { Time } from '@zos/sensor'
import { createSysTimer, stopTimer } from '@zos/timer'
import { createSystemRequestRunner } from './system_request_runner'
import { cleanCommand, projectOf, buildApprovalActionParam } from '../common/format'

const TAG = '[Codex Watch Background]'
const POLL_INTERVAL_MS = 10000
const REQUEST_TIMEOUT = 15000
const ACTION_SERVICE = 'app-service/approval_action'
const REPLY_PAGE = 'page/reply'
const timeSensor = new Time()
const phoneRequest = createSystemRequestRunner({
  timeoutMs: REQUEST_TIMEOUT,
  createTimer: createSysTimer,
  stopTimer
})

let pollRunning = false
let destroyed = false
let pollTimerId = null
let bridgeConnected = false
const approvalNotifications = {}

function log(message) {
  console.log(TAG + ' ' + message)
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
    bridgeConnected = false
    log('service started')
    this.pollBridge()
    try {
      pollTimerId = createSysTimer(true, POLL_INTERVAL_MS, () => this.pollBridge())
    } catch (error) {
      log('poll timer unavailable: ' + String(error))
    }
    timeSensor.onPerMinute(() => {
      if (!destroyed) this.pollBridge()
    })
  },

  async pollBridge() {
    if (destroyed || pollRunning) return
    pollRunning = true
    try {
      const result = await phoneRequest.run(
        'bridge.poll',
        () => this.request({ method: 'bridge.poll', params: { limit: 20 } }, { timeout: REQUEST_TIMEOUT })
      )
      if (destroyed) return
      if (!result || result.ok !== true) throw new Error(result && result.error || 'invalid bridge response')
      if (!bridgeConnected) log('bridge polling active')
      bridgeConnected = true
      const notifications = result.notifications || []
      if (notifications.length) log('received notifications=' + notifications.length)
      for (const event of notifications) {
        if (destroyed) break
        try {
          if (!notifyEvent(event)) continue
          const ack = await phoneRequest.run(
            'bridge.notification.ack',
            () => this.request({
              method: 'bridge.notification.ack',
              params: { event_id: event.event_id }
            }, { timeout: REQUEST_TIMEOUT })
          )
          if (!ack || ack.ok !== true) throw new Error(ack && ack.error || 'notification acknowledgement failed')
          log('notification acknowledged event=' + String(event.event_id || ''))
        } catch (error) {
          log('delivery failed: ' + String(error))
        }
      }
      if (Object.keys(approvalNotifications).length) await this.checkStaleApprovals()
    } catch (error) {
      bridgeConnected = false
      log('poll failed: ' + String(error))
    } finally {
      pollRunning = false
    }
  },

  async checkStaleApprovals() {
    const result = await phoneRequest.run(
      'bridge.approval.list',
      () => this.request({
        method: 'bridge.approval.list',
        params: { status: 'pending', limit: 50 }
      }, { timeout: REQUEST_TIMEOUT })
    )
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
    if (pollTimerId !== null) {
      try { stopTimer(pollTimerId) } catch (error) {}
      pollTimerId = null
    }
  }
}))
