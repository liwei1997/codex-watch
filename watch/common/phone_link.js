export function isBleConnected(connectStatus) {
  try {
    return connectStatus() === true
  } catch (error) {
    return false
  }
}

export function phoneConnectionLabel(error, connected) {
  const code = Number(error && error.code)

  if (!connected || code === 2) return 'PHONE DISCONNECTED'
  if (code === 1 || code === 3) return 'PHONE APP RECONNECTING'
  if (code === 4) return 'PHONE LINK TIMEOUT'
  return 'BRIDGE OFFLINE'
}
