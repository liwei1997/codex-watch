import { BasePage } from '@zeppos/zml/base-page'
import { notify } from '@zos/notification'
import { createSysTimer, stopTimer } from '@zos/timer'
import { exit } from '@zos/app-service'

import { createSystemRequestRunner } from './system_request_runner'

import {
  parseApprovalActionParam,
  truncate
} from '../common/format'


const TAG = '[Codex Watch ApprovalAction]'
const PHONE_REQUEST_TIMEOUT = 15000

const phoneRequest = createSystemRequestRunner({
  timeoutMs: PHONE_REQUEST_TIMEOUT,
  createTimer: createSysTimer,
  stopTimer
})

let handled = false

function log(message) {
  console.log(TAG + ' ' + message)
}

function notifyResult(title, content) {
  try {
    const id = notify({
      title,
      content,
      actions: [],
      vibrate: 1
    })

    log('result notification id=' + id)
  } catch (error) {
    log('result notification failed: ' + String(error))
  }
}

function finish() {
  try {
    exit()
  } catch (error) {
    log('exit failed: ' + String(error))
  }
}

AppService(
  BasePage({
    state: {},

    build() {},

    onInit(params) {
      log('onInit: ' + JSON.stringify(params))
      this.handleAction(params)
    },

    onEvent(data) {
      log('onEvent: ' + JSON.stringify(data))
      this.handleAction(data)
    },

    async handleAction(params) {
      if (handled) {
        return
      }

      handled = true

      const action = parseApprovalActionParam(params)

      if (!action) {
        log('Unrecognized action data')
        notifyResult('APPROVAL ACTION', 'UNRECOGNIZED ACTION DATA')
        finish()
        return
      }

      log(
        'decision=' + action.decision +
        ' approval=' + action.approval_id
      )

      try {
        const result = await phoneRequest.run(
          'bridge.approval.decision',
          () => this.request(
            {
              method: 'bridge.approval.decision',
              params: {
                approval_id: action.approval_id,
                decision: action.decision
              }
            },
            {
              timeout: PHONE_REQUEST_TIMEOUT
            }
          )
        )

        if (result && result.ok === true) {
          notifyResult(
            'APPROVAL SENT',
            action.decision === 'accept'
              ? 'APPROVED FROM WATCH'
              : 'REJECTED FROM WATCH'
          )
        } else {
          const err = result && result.error
            ? String(result.error)
            : 'decision rejected'

          if (/already/i.test(err)) {
            notifyResult('APPROVAL', 'ALREADY HANDLED')
          } else {
            notifyResult(
              'APPROVAL',
              'FAILED: ' + truncate(err, 80)
            )
          }
        }

      } catch (error) {
        notifyResult(
          'APPROVAL',
          'FAILED: ' + truncate(
            String(error && error.message ? error.message : error),
            80
          )
        )
      }

      finish()
    }
  })
)
