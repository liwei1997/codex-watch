import { BaseSideService } from '@zeppos/zml/base-side'

import { BRIDGE_URL, BRIDGE_TOKEN } from './bridge-config'


const BRIDGE_BASE_URL = BRIDGE_URL

const DEFAULT_CONSUMER_ID = 'iphone'

const SIDE_JS_MARK = '119-20261124'
const REQUEST_TIMEOUT = 10000


function parseResponseBody(body) {
  if (body === null || body === undefined || body === '') {
    return {}
  }

  if (typeof body === 'string') {
    try {
      return JSON.parse(body)
    } catch (error) {
      return { raw: body }
    }
  }

  return body
}

function getErrorMessage(error) {
  if (error && error.message) {
    return error.message
  }

  return String(error)
}

function buildUrl(path) {
  return BRIDGE_BASE_URL.replace(/\/+$/, '') + path
}

async function requestBridge(service, options) {
  const response = await service.fetch({
    url: options.url,
    method: options.method,
    headers: {
      'Authorization': 'Bearer ' + BRIDGE_TOKEN,
      'Content-Type': 'application/json',
      'Accept': 'application/json'
    },
    body: options.body,
    timeout: REQUEST_TIMEOUT
  })

  const status = Number(response && response.status)

  const body = parseResponseBody(response && response.body)

  if (
    status &&
    (status < 200 || status >= 300) &&
    !options.allowError
  ) {
    throw new Error(
      'Bridge HTTP ' + status + ': ' + JSON.stringify(body)
    )
  }

  return body
}


AppSideService(
  BaseSideService({

    onInit() {
      console.log('[Codex Watch] Side Service initialized')

      console.log(
        '[Codex Watch] Bridge URL: ' + BRIDGE_BASE_URL
      )
    },

    onRun() {
      console.log('[Codex Watch] Side Service running')
    },


    async onRequest(req, res) {
      const method = req.method
      const params = req.params || {}

      console.log(
        '[Codex Watch] Request: ' + method
      )

      try {


        if (method === 'bridge.version') {

          res(null, {
            ok: true,
            js: SIDE_JS_MARK,
            methods: [
              'bridge.version',
              'bridge.poll',
              'bridge.history',
              'bridge.ack',
              'bridge.approval.list',
              'bridge.approval.decision'
            ]
          })

          return
        }


        if (method === 'bridge.poll') {

          if (
            BRIDGE_BASE_URL.indexOf('YOUR_UBUNTU') >= 0 ||
            BRIDGE_TOKEN.indexOf('PASTE_') === 0
          ) {
            throw new Error(
              'Please configure BRIDGE_BASE_URL and BRIDGE_TOKEN'
            )
          }

          console.log('[Codex Watch] Polling Bridge')

          const consumerId =
            params.consumer_id || DEFAULT_CONSUMER_ID

          const limit = Math.max(
            1,
            Math.min(50, parseInt(params.limit, 10) || 20)
          )

          const url =
            buildUrl('/v1/events') +
            '?consumer_id=' +
            encodeURIComponent(consumerId) +
            '&limit=' +
            limit

          const data = await requestBridge(this, {
            url,
            method: 'get'
          })

          const events = Array.isArray(data.events)
            ? data.events
            : []

          console.log(
            '[Codex Watch] Received events: ' + events.length
          )

          res(null, {
            ok: data.ok !== false,
            consumer_id: data.consumer_id || consumerId,
            count: events.length,
            events
          })

          return
        }


        if (method === 'bridge.history') {

          const consumerId =
            params.consumer_id || DEFAULT_CONSUMER_ID

          const limit = Math.max(
            1,
            Math.min(50, parseInt(params.limit, 10) || 20)
          )

          const url =
            buildUrl('/v1/events/history') +
            '?consumer_id=' +
            encodeURIComponent(consumerId) +
            '&limit=' +
            limit

          const data = await requestBridge(this, {
            url,
            method: 'get'
          })

          const events = Array.isArray(data.events)
            ? data.events
            : []

          console.log(
            '[Codex Watch] History events: ' + events.length
          )

          res(null, {
            ok: data.ok !== false,
            consumer_id: data.consumer_id || consumerId,
            count: events.length,
            events
          })

          return
        }


        if (method === 'bridge.ack') {

          const payload = {
            event_id: params.event_id,
            consumer_id:
              params.consumer_id || DEFAULT_CONSUMER_ID,
            stage: params.stage || 'watch_received'
          }

          if (!payload.event_id) {
            throw new Error('Missing event_id')
          }

          console.log(
            '[Codex Watch] ACK: ' + payload.event_id
          )

          const data = await requestBridge(this, {
            url: buildUrl('/v1/ack'),
            method: 'post',
            body: JSON.stringify(payload)
          })

          console.log(
            '[Codex Watch] ACK response: ' +
            JSON.stringify(data)
          )

          res(null, data)

          return
        }


        if (method === 'bridge.approval.list') {

          const status = params.status || 'pending'

          const limit = Math.max(
            1,
            Math.min(50, parseInt(params.limit, 10) || 20)
          )

          const url =
            buildUrl('/v1/approvals') +
            '?status=' +
            encodeURIComponent(status) +
            '&limit=' +
            limit

          const data = await requestBridge(this, {
            url,
            method: 'get'
          })

          const approvals = Array.isArray(data.approvals)
            ? data.approvals
            : []

          console.log(
            '[Codex Watch] Approvals: ' + approvals.length
          )

          res(null, {
            ok: data.ok !== false,
            count: approvals.length,
            approvals
          })

          return
        }


        if (method === 'bridge.approval.decision') {

          const approvalId = String(params.approval_id || '')

          if (!approvalId) {
            throw new Error('Missing approval_id')
          }

          const decision = String(params.decision || '').toLowerCase()

          if (decision !== 'accept' && decision !== 'decline') {
            throw new Error('decision must be accept or decline')
          }

          console.log(
            '[Codex Watch] Decision: ' +
            decision + ' ' + approvalId
          )

          const data = await requestBridge(this, {
            url:
              buildUrl('/v1/approvals') +
              '/' +
              encodeURIComponent(approvalId) +
              '/decision',
            method: 'post',
            body: JSON.stringify({ decision: decision }),
            allowError: true
          })

          res(null, data)

          return
        }


        console.log(
          '[Codex Watch] Unknown method: ' + method
        )
        console.log(
          '[Codex Watch] Unknown request: ' +
          JSON.stringify(req)
        )

        res('Unknown method: ' + method)

      } catch (error) {

        const message = getErrorMessage(error)

        console.error(
          '[Codex Watch] Request failed: ' + message
        )

        res(message)
      }
    },


    onCall(data) {
      console.log(
        '[Codex Watch] Watch call: ' +
        JSON.stringify(data)
      )
    },

    onDestroy() {
      console.log(
        '[Codex Watch] Side Service destroyed'
      )
    }

  })
)

