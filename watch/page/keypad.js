import {
  onKey,
  KEY_UP,
  KEY_DOWN,
  KEY_SELECT,
  KEY_EVENT_CLICK,
  KEY_EVENT_LONG_PRESS
} from '@zos/interaction'


let activeHandler = null
let registered = false
let suppressClickUntil = 0
const CLICK_SUPPRESS_MS = 600

function mapKey(key) {
  switch (key) {
    case KEY_UP:
      return 'up'

    case KEY_DOWN:
      return 'down'

    case KEY_SELECT:
      return 'select'

    default:
      return null
  }
}

function registerKeyListener() {
  if (registered) {
    return
  }

  onKey({
    callback: (key, event) => {
      if (!activeHandler) {
        return false
      }

      const action = mapKey(key)

      if (!action) {
        return false
      }

      if (event === KEY_EVENT_LONG_PRESS) {
        suppressClickUntil = Date.now() + CLICK_SUPPRESS_MS
        console.log('[Keypad] LONG:', action)
        return activeHandler(action) !== false
      }

      if (event === KEY_EVENT_CLICK) {
        if (Date.now() < suppressClickUntil) {
          return true
        }

        console.log('[Keypad] CLICK:', action)
        return activeHandler(action) !== false
      }

      return false
    }
  })

  registered = true
}

export function activateKeypad(handler) {
  registerKeyListener()
  activeHandler = handler
}

export function deactivateKeypad() {
  activeHandler = null
}
