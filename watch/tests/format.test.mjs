import test from 'node:test'
import assert from 'node:assert/strict'

import {
  cleanCommand,
  projectOf,
  buildApprovalActionParam,
  parseApprovalActionParam,
  ageText,
  truncate,
  eventKindLabel,
} from '../common/format.js'

test('cleanCommand strips a /bin/bash -lc single-quoted wrapper', () => {
  assert.equal(
    cleanCommand("/bin/bash -lc 'git push origin main'"),
    'git push origin main',
  )
})

test('cleanCommand strips a double-quoted wrapper', () => {
  assert.equal(
    cleanCommand('/bin/bash -lc "npm test && npm run build"'),
    'npm test && npm run build',
  )
})

test('cleanCommand keeps a bare command untouched', () => {
  assert.equal(cleanCommand('git status'), 'git status')
})

test('cleanCommand falls back for an unquoted wrapper', () => {
  assert.equal(cleanCommand('/bin/bash -lc git status'), 'git status')
})

test('cleanCommand handles missing input', () => {
  assert.equal(cleanCommand(null), '')
  assert.equal(cleanCommand(undefined), '')
  assert.equal(cleanCommand(''), '')
})

test('projectOf returns the basename of a cwd', () => {
  assert.equal(projectOf('/home/me/proj/clam-uwb'), 'clam-uwb')
  assert.equal(projectOf('/a/b/'), 'b')
  assert.equal(projectOf(''), 'codex')
  assert.equal(projectOf(null), 'codex')
})

test('approval action param round-trips through JSON', () => {
  const param = buildApprovalActionParam('abc123', 'accept')
  assert.equal(typeof param, 'string')
  assert.deepEqual(
    parseApprovalActionParam(param),
    { approval_id: 'abc123', decision: 'accept' },
  )
})

test('parseApprovalActionParam accepts an already-parsed object', () => {
  assert.deepEqual(
    parseApprovalActionParam({ approval_id: 'x', decision: 'decline' }),
    { approval_id: 'x', decision: 'decline' },
  )
})

test('parseApprovalActionParam accepts a { param: "..." } wrapper', () => {
  assert.deepEqual(
    parseApprovalActionParam({
      param: buildApprovalActionParam('y', 'accept'),
    }),
    { approval_id: 'y', decision: 'accept' },
  )
})

test('parseApprovalActionParam rejects bad payloads', () => {
  assert.equal(parseApprovalActionParam('not-json'), null)
  assert.equal(parseApprovalActionParam(''), null)
  assert.equal(parseApprovalActionParam(null), null)
  assert.equal(parseApprovalActionParam({}), null)
  assert.equal(
    parseApprovalActionParam({ approval_id: '', decision: 'accept' }),
    null,
  )
  assert.equal(
    parseApprovalActionParam({ approval_id: 'a', decision: 'maybe' }),
    null,
  )
})

test('ageText buckets elapsed time', () => {
  const now = 1_000_000
  assert.equal(ageText(now, now), 'NOW')
  assert.equal(ageText(now - 60, now), 'NOW')
  assert.equal(ageText(now - 120, now), '2M')
  assert.equal(ageText(now - 1500, now), '25M')
  assert.equal(ageText(now - 3700, now), '1H')
  assert.equal(ageText(now - 90_000, now), '25H')
  assert.equal(ageText(now - 300_000, now), '3D')
  assert.equal(ageText(null, now), 'NEW')
})

test('truncate cuts long text with an ellipsis', () => {
  assert.equal(truncate('hello', 10), 'hello')
  assert.equal(truncate('hello world', 8), 'hello w…')
  assert.equal(truncate(null, 5), '')
})

test('eventKindLabel maps known types', () => {
  assert.equal(eventKindLabel('approval_request'), 'APPROVAL')
  assert.equal(eventKindLabel('task_completed'), 'TASK')
  assert.equal(eventKindLabel('weird_thing'), 'EVENT')
  assert.equal(eventKindLabel(null), 'EVENT')
})
