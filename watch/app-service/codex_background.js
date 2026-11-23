import { BasePage } from '@zeppos/zml/base-page'
import { notify, cancel } from '@zos/notification'
import { Time } from '@zos/sensor'
import { createSysTimer, stopTimer } from '@zos/timer'

import { createSystemRequestRunner } from './system_request_runner'

import {
  cleanCommand,
  projectOf,
  buildApprovalActionParam
} from '../common/format'

// ============================================================
// Codex Watch - Background Message Service
//
// Zepp OS 4.0
// Amazfit T-Rex 3 Pro
//
// Watch App Service
//   -> ZML
//   -> iPhone Side Service
//   -> Cloudflare Tunnel
//   -> Ubuntu Bridge
//
// Polls every 10 seconds while the Zepp OS background service is running.
//
//   * approval_request events become system notifications
//     with APPROVE / REJECT action buttons that launch
//     app-service/approval_action.
//   * other events become plain notifications.
//   * when a tracked approval no longer shows as pending,
//     its notification is cancelled (stale-approval cleanup).
// ============================================================

const TAG = '[Codex Watch Background]'

const POLL_LIMIT = 5
const POLL_INTERVAL_MS = 10_000
const MAX_BODY_LENGTH = 160
const MAX_APPROVAL_TITLE = 40
const MAX_APPROVAL_CONTENT = 120
const PHONE_REQUEST_TIMEOUT = 15000

const ACTION_SERVICE = 'app-service/approval_action'

const timeSensor = new Time()

// ZML's built-in request timeout uses a regular setTimeout, which is not
// available in App Service. Guard every phone request with Zepp OS's system
// timer so one lost BLE response cannot block all later polling cycles.
const phoneRequest = createSystemRequestRunner({
  timeoutMs: PHONE_REQUEST_TIMEOUT,
  createTimer: createSysTimer,
  stopTimer
})

let pollRunning = false
let destroyed = false
let minuteCount = 0
let pollTimerId = null

// Prevent repeated notifications in the current service instance
// when the notification succeeded but the ACK failed.
const notifiedEvents = {}

// approval_id -> { event_id, notification_id } for approval
// notifications that may still need to be cancelled.
const approvalNotifications = {}

function log(message) {
  console.log(TAG + ' ' + message)
}

function errorText(error) {
  if (error && error.message) {
    return String(error.message)
  }

  return String(error)
}

function cut(text, max) {
  const s = String(text === null || text === undefined ? '' : text)

  if (s.length <= max) {
    return s
  }

  return s.slice(0, max - 3) + '...'
}

function buildNotificationContent(event) {
  const type = String(event.type || 'event')

  const machine = String(event.machine_id || 'unknown')

  const body = String(event.body || '')
    .replace(/\s+/g, ' ')
    .trim()

  if (!body) {
    return type + ' · ' + machine
  }

  return cut(body, MAX_BODY_LENGTH)
}

function sendSystemNotification(event) {
  const eventId = String(event.event_id || '')

  if (!eventId) {
    throw new Error('Missing event_id')
  }

  if (notifiedEvents[eventId]) {
    log('Already notified: ' + eventId)
    return true
  }

  const isApproval = event.type === 'approval_request'
  const payload = event.payload || {}
  const actionableApproval = isApproval && event.source !== 'codex-notify'
  const approvalId = isApproval
    ? String(payload.approval_id || '')
    : ''

  let title = String(event.title || 'Codex Watch').slice(0, 60)
  let content = buildNotificationContent(event)
  let actions = []
  let vibrate = 0

  if (isApproval && approvalId) {
    title = cut(
      'APPROVAL · ' + projectOf(payload.cwd),
      MAX_APPROVAL_TITLE
    )

    content = cut(
      cleanCommand(payload.command) || content,
      MAX_APPROVAL_CONTENT
    )

    actions = actionableApproval ? [
      {
        text: 'APPROVE',
        file: ACTION_SERVICE,
        param: buildApprovalActionParam(approvalId, 'accept')
      },
      {
        text: 'REJECT',
        file: ACTION_SERVICE,
        param: buildApprovalActionParam(approvalId, 'decline')
      }
    ] : []

    vibrate = 1
    if (!actionableApproval) {
      content = cut(content + ' (approve in Codex terminal)', MAX_APPROVAL_CONTENT)
    }

    log('Approval notification: ' + approvalId)
  }

  log('Creating notification: ' + eventId)

  const notificationId = notify({
    title,
    content,
    actions,
    vibrate
  })

  log('notify id=' + notificationId)

  if (!notificationId) {
    log('Notification creation failed')
    return false
  }

  notifiedEvents[eventId] = true

  if (isApproval && approvalId) {
    approvalNotifications[approvalId] = {
      event_id: eventId,
      notification_id: notificationId
    }
  }

  log('Notification created: ' + eventId)

  return true
}

// ============================================================
// Background Service
// ============================================================

AppService(
  BasePage({
    state: {},

    // BasePage compatibility: no UI is created.
    build() {},

    onInit() {
      destroyed = false
      pollRunning = false
      minuteCount = 0

      log('started')

      const service = this

      // Run an initial poll when the background service starts.
      service.pollBridge()

      // System timer handles sub-minute polling when the service is alive.
      // Keep the minute sensor as a fallback in case the periodic timer is
      // throttled by the OS; pollRunning prevents concurrent requests.
      try {
        pollTimerId = createSysTimer(
          true,
          POLL_INTERVAL_MS,
          () => service.pollBridge()
        )
        log('fast polling registered: ' + POLL_INTERVAL_MS + 'ms')
      } catch (error) {
        log('fast polling unavailable: ' + errorText(error))
      }

      timeSensor.onPerMinute(() => {
        if (destroyed) return
        minuteCount++
        log('minute fallback tick=' + minuteCount)
        service.pollBridge()
      })
    },

    async pollBridge() {
      if (destroyed) {
        return
      }

      if (pollRunning) {
        log('Poll skipped: previous request pending')
        return
      }

      pollRunning = true

      try {
        log('Requesting bridge.poll')

        const result = await phoneRequest.run(
          'bridge.poll',
          () => this.request(
            {
              method: 'bridge.poll',
              params: {
                consumer_id: 'iphone',
                limit: POLL_LIMIT
              }
            },
            {
              timeout: PHONE_REQUEST_TIMEOUT
            }
          )
        )

        if (destroyed) {
          return
        }

        if (!result || result.ok !== true) {
          throw new Error('Invalid bridge.poll response')
        }

        const events = Array.isArray(result.events)
          ? result.events
          : []

        log('Poll OK, count=' + events.length)

        // Drain the small batch, with approvals sorted first by Bridge.
        // ACK only after notify() reports success.
        for (const item of events) {
          if (destroyed) break
          const eventId = String(item.event_id || '')
          if (!eventId) {
            log('Skipping event without event_id')
            continue
          }

          try {
            log('Received event: ' + eventId)
            if (!sendSystemNotification(item)) {
              log('Notification failed, ACK skipped: ' + eventId)
              continue
            }

            const ack = await phoneRequest.run(
              'bridge.ack',
              () => this.request(
                {
                  method: 'bridge.ack',
                  params: {
                    event_id: eventId,
                    consumer_id: 'iphone',
                    stage: 'watch_received'
                  }
                },
                { timeout: PHONE_REQUEST_TIMEOUT }
              )
            )

            if (!ack || ack.ok !== true) {
              throw new Error('Invalid ACK response')
            }
            log('Watch ACK OK: ' + eventId)
            delete notifiedEvents[eventId]
          } catch (eventError) {
            log('Delivery failed for ' + eventId + ': ' + errorText(eventError))
          }
        }

        // Drop notifications for approvals that are no longer
        // pending (decided from the watch, terminal, or expired).
        if (Object.keys(approvalNotifications).length > 0) {
          await this.checkStaleApprovals()
        }

      } catch (error) {
        log('Poll failed: ' + errorText(error))

      } finally {
        pollRunning = false
      }
    },

    async checkStaleApprovals() {
      const tracked = Object.keys(approvalNotifications)

      if (tracked.length === 0 || destroyed) {
        return
      }

      try {
        const result = await phoneRequest.run(
          'bridge.approval.list',
          () => this.request(
            {
              method: 'bridge.approval.list',
              params: {
                status: 'pending',
                limit: 50
              }
            },
            {
              timeout: PHONE_REQUEST_TIMEOUT
            }
          )
        )

        if (destroyed) {
          return
        }

        if (!result || result.ok !== true ||
          !Array.isArray(result.approvals)) {
          // Unknown state: keep the notifications.
          return
        }

        const stillPending = {}

        result.approvals.forEach((approval) => {
          stillPending[String(approval.approval_id)] = true
        })

        tracked.forEach((approvalId) => {
          const entry = approvalNotifications[approvalId]

          if (!entry || stillPending[approvalId]) {
            return
          }

          try {
            cancel([entry.notification_id])
            log(
              'Cancelled stale approval notification: ' +
              approvalId + ' id=' + entry.notification_id
            )
          } catch (cancelError) {
            log(
              'Cancel failed for ' + approvalId +
              ': ' + errorText(cancelError)
            )
          }

          delete approvalNotifications[approvalId]
        })

      } catch (error) {
        log('Stale check failed: ' + errorText(error))
      }
    },

    onDestroy() {
      destroyed = true
      if (pollTimerId !== null) {
        try {
          stopTimer(pollTimerId)
        } catch (error) {
          log('poll timer cleanup failed: ' + errorText(error))
        }
        pollTimerId = null
      }

      log('destroyed')
    }
  })
)
