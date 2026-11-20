import test from 'node:test'
import assert from 'node:assert/strict'

import {
  RequestTimeoutError,
  createSystemRequestRunner,
} from '../app-service/system_request_runner.js'

function createTimerHarness() {
  let nextId = 1
  const timers = new Map()
  const stopped = []

  return {
    createTimer(periodic, period, callback) {
      const id = nextId++

      timers.set(id, {
        periodic,
        period,
        callback,
      })

      return id
    },

    stopTimer(id) {
      stopped.push(id)
      timers.delete(id)
    },

    fire(id) {
      const timer = timers.get(id)

      assert.ok(timer, 'expected timer ' + id + ' to exist')
      timers.delete(id)
      timer.callback()
    },

    getTimer(id) {
      return timers.get(id)
    },

    stopped,
  }
}

test('a system timeout releases the runner so the next request can run', async () => {
  const timers = createTimerHarness()
  let resolveFirst

  const runner = createSystemRequestRunner({
    timeoutMs: 15_000,
    createTimer: timers.createTimer,
    stopTimer: timers.stopTimer,
  })

  const first = runner.run(
    'bridge.poll',
    () => new Promise(resolve => {
      resolveFirst = resolve
    }),
  )

  assert.deepEqual(timers.getTimer(1), {
    periodic: false,
    period: 15_000,
    callback: timers.getTimer(1).callback,
  })
  timers.fire(1)

  await assert.rejects(
    first,
    error => {
      assert.ok(error instanceof RequestTimeoutError)
      assert.equal(
        error.message,
        'bridge.poll timed out after 15000ms',
      )
      return true
    },
  )

  const second = runner.run(
    'bridge.poll',
    () => Promise.resolve('recovered'),
  )

  assert.equal(await second, 'recovered')

  resolveFirst('late response')
  await Promise.resolve()
})

test('a completed request stops its system timeout', async () => {
  const timers = createTimerHarness()

  const runner = createSystemRequestRunner({
    timeoutMs: 15_000,
    createTimer: timers.createTimer,
    stopTimer: timers.stopTimer,
  })

  const result = await runner.run(
    'bridge.ack',
    () => Promise.resolve({ ok: true }),
  )

  assert.deepEqual(result, { ok: true })
  assert.deepEqual(timers.stopped, [1])
  assert.equal(timers.getTimer(1), undefined)
})

test('a request error is preserved and its system timeout is stopped', async () => {
  const timers = createTimerHarness()
  const expected = new Error('phone disconnected')

  const runner = createSystemRequestRunner({
    timeoutMs: 15_000,
    createTimer: timers.createTimer,
    stopTimer: timers.stopTimer,
  })

  await assert.rejects(
    runner.run(
      'bridge.poll',
      () => Promise.reject(expected),
    ),
    error => error === expected,
  )

  assert.deepEqual(timers.stopped, [1])

  assert.equal(
    await runner.run(
      'bridge.poll',
      () => Promise.resolve('next request'),
    ),
    'next request',
  )
})
