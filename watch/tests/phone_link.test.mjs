import test from 'node:test'
import assert from 'node:assert/strict'

import {
  isBleConnected,
  phoneConnectionLabel,
} from '../common/phone_link.js'

test('BLE status is read safely', () => {
  assert.equal(isBleConnected(() => true), true)
  assert.equal(isBleConnected(() => false), false)
  assert.equal(isBleConnected(() => { throw new Error('unavailable') }), false)
})

test('phone connection errors are distinguished from bridge errors', () => {
  assert.equal(phoneConnectionLabel(null, false), 'PHONE DISCONNECTED')
  assert.equal(phoneConnectionLabel({ code: 2 }, true), 'PHONE DISCONNECTED')
  assert.equal(phoneConnectionLabel({ code: 1 }, true), 'PHONE APP RECONNECTING')
  assert.equal(phoneConnectionLabel({ code: 3 }, true), 'PHONE APP RECONNECTING')
  assert.equal(phoneConnectionLabel({ code: 4 }, true), 'PHONE LINK TIMEOUT')
  assert.equal(phoneConnectionLabel(new Error('bridge offline'), true), 'BRIDGE OFFLINE')
})
