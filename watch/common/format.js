
const WRAPPER_PATTERNS = [
  /^\/bin\/bash\s+-lc\s+(['"])([\s\S]*)\1\s*$/,
  /^\/bin\/bash\s+lc\s+(['"])([\s\S]*)\1\s*$/,
  /^bash\s+-lc\s+(['"])([\s\S]*)\1\s*$/,
  /^bash\s+lc\s+(['"])([\s\S]*)\1\s*$/,
  /^\/bin\/sh\s+-lc\s+(['"])([\s\S]*)\1\s*$/,
  /^sh\s+-lc\s+(['"])([\s\S]*)\1\s*$/
]

export function cleanCommand(cmd) {
  if (cmd === null || cmd === undefined) {
    return ''
  }

  let s = String(cmd).trim()

  for (const pattern of WRAPPER_PATTERNS) {
    const match = s.match(pattern)

    if (match) {
      s = match[2].trim()
      break
    }
  }

  s = s.replace(/^\/?bin\/(bash|sh)\s+(-l|-lc|c)\s+/, '')

  return s.trim()
}

export function projectOf(cwd) {
  if (!cwd) {
    return 'codex'
  }

  const parts = String(cwd).split('/').filter(Boolean)

  return parts.length > 0 ? parts[parts.length - 1] : 'codex'
}

export function buildApprovalActionParam(approvalId, decision) {
  return JSON.stringify({
    approval_id: String(approvalId),
    decision: String(decision)
  })
}

export function parseApprovalActionParam(param) {
  let value = param

  if (
    value &&
    typeof value === 'object' &&
    !Array.isArray(value) &&
    typeof value.param === 'string'
  ) {
    value = value.param
  }

  if (typeof value === 'string') {
    try {
      value = JSON.parse(value)
    } catch (error) {
      return null
    }
  }

  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    return null
  }

  const approvalId = String(value.approval_id || '').trim()
  const decision = String(value.decision || '').trim().toLowerCase()

  if (!approvalId) {
    return null
  }

  if (decision !== 'accept' && decision !== 'decline') {
    return null
  }

  return { approval_id: approvalId, decision: decision }
}

export function ageText(createdSec, nowSec) {
  if (
    createdSec === null ||
    createdSec === undefined ||
    nowSec === null ||
    nowSec === undefined
  ) {
    return 'NEW'
  }

  const created = Number(createdSec)
  const now = Number(nowSec)

  if (!Number.isFinite(created) || !Number.isFinite(now)) {
    return 'NEW'
  }

  const diff = Math.max(0, now - created)

  if (diff < 90) {
    return 'NOW'
  }

  if (diff < 3600) {
    return Math.max(1, Math.round(diff / 60)) + 'M'
  }

  if (diff < 172800) {
    return Math.round(diff / 3600) + 'H'
  }

  return Math.round(diff / 86400) + 'D'
}

export function truncate(text, max) {
  const s = String(text === null || text === undefined ? '' : text)

  if (s.length <= max) {
    return s
  }

  return s.slice(0, Math.max(0, max - 1)) + '…'
}

export function eventKindLabel(type) {
  switch (String(type || '')) {
    case 'approval_request':
      return 'APPROVAL'
    case 'task_completed':
      return 'TASK'
    case 'session_started':
      return 'SESSION'
    default:
      return 'EVENT'
  }
}
