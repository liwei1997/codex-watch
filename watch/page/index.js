import { BasePage } from '@zeppos/zml/base-page'

import {
  createWidget,
  widget,
  prop,
  align
} from '@zos/ui'

import { px } from '@zos/utils'

import { push } from '@zos/router'

import { requestPermission } from '@zos/app'
import { start } from '@zos/app-service'

import {
  COLORS,
  drawBackground,
  addText,
  shortError
} from './ui'

import { activateKeypad } from './keypad'

import {
  cleanCommand,
  projectOf,
  truncate,
  ageText
} from '../common/format'

// ============================================================
// Codex Watch - Home (live dashboard)
//
// Zepp OS 4.0 / Amazfit T-Rex 3 Pro
//
// Two tappable cards:
//   APPROVALS  -> page/approvals (pending queue + decisions)
//   MESSAGES   -> page/inbox     (scrollable event history)
//
// Touch (v1.1.2): the two cards are BUTTON widgets
// (click_func) - the only tappable surface verified on
// this firmware. Texts stacked on top are gesture
// pass-through. Focus frames sit BELOW the cards in
// Z-order and are color-driven, so nothing blocks or
// hides anything.
//
// Data (v1.1.1): the two dashboard requests run
// independently with an explicit 15s timeout. A failed
// endpoint shows PARTIAL data instead of killing the whole
// page, and fast retries (3s/6s) recover from the startup
// race that left v1.1.0 stuck on OFFLINE.
//
// Refreshes every 60s while visible and launches the
// background App Service that pushes new Codex events.
// ============================================================

const TAG = '[Codex Watch]'

const BACKGROUND_SERVICE = 'app-service/codex_background'
const REFRESH_MS = 60 * 1000
const RELOAD_GUARD_MS = 5 * 1000
const REQUEST_TIMEOUT = 15000
const FAST_RETRY_DELAYS_MS = [3000, 6000]

const CARDS = [
  {
    id: 'approvals',
    title: 'APPROVALS',
    page: 'page/approvals'
  },
  {
    id: 'messages',
    title: 'MESSAGES',
    page: 'page/inbox'
  }
]

// ============================================================
// Background App Service
// ============================================================

let backgroundStartRequested = false

function launchBackgroundService() {
  if (backgroundStartRequested) {
    console.log(TAG + ' Background start already requested')
    return
  }

  backgroundStartRequested = true

  try {
    const result = start({
      file: BACKGROUND_SERVICE,
      reload: true,

      complete_func: (status) => {
        console.log(
          TAG + ' Background start callback: ' +
          JSON.stringify(status)
        )

        if (!status || status.result !== true) {
          backgroundStartRequested = false
          console.log(TAG + ' Background start failed')
        } else {
          console.log(TAG + ' Background service running')
        }
      }
    })

    console.log(TAG + ' Background start return: ' + result)

    if (result !== 0) {
      backgroundStartRequested = false
    }
  } catch (error) {
    backgroundStartRequested = false
    console.log(TAG + ' Background exception: ' + String(error))
  }
}

function startCodexBackground() {
  console.log(TAG + ' Checking background permission')

  try {
    const permissionResult = requestPermission({
      permissions: ['device:os.bg_service'],

      callback: (result) => {
        console.log(
          TAG + ' Permission callback: ' + JSON.stringify(result)
        )

        if (
          Array.isArray(result) &&
          result.length > 0 &&
          result[0] === 2
        ) {
          launchBackgroundService()
        } else {
          console.log(TAG + ' Background permission denied')
        }
      }
    })

    console.log(TAG + ' Permission request return: ' + permissionResult)

    // 2 means permission was already granted.
    if (permissionResult === 2) {
      launchBackgroundService()
    }
  } catch (error) {
    console.log(TAG + ' Permission exception: ' + String(error))
  }
}

// ============================================================
// Home page
// ============================================================

Page(BasePage({
  state: {},

  onInit() {
    this.focusIndex = 0
    this.lastLoadAt = 0
    this.refreshTimer = null
    this.fastRetryTimer = null
    this.fastRetryAt = 0
    this.busyLoad = false
    this.destroyed = false

    console.log(TAG + ' Home onInit')
  },

  build() {
    this.drawHome()
    try {
      activateKeypad((action) => this.handleKeyAction(action))
    } catch (error) {
      console.log(TAG + ' Keypad activate failed: ' + String(error))
    }
    this.startAutoRefresh()
    this.loadDashboard()

    // Launch the push-notification service after UI init.
    startCodexBackground()
  },

  // ----------------------------------------------------------
  // UI
  // ----------------------------------------------------------

  drawHome() {
    drawBackground()

    addText('CODEX WATCH', 60, 34, 360, 34, 26, COLORS.white)

    // Link status row
    this.linkDot = createWidget(widget.FILL_RECT, {
      x: px(118),
      y: px(80),
      w: px(14),
      h: px(14),
      radius: px(7),
      color: COLORS.muted
    })

    this.linkStatus = addText(
      'SYNCING...', 142, 74, 240, 24, 15, COLORS.muted, 0
    )

    // Cards with their focus frames.
    //
    // Z-order per card: frame first (below), then the
    // BUTTON card, then the pass-through texts on top.
    // The frame is color-driven (background = hidden) so it
    // never relies on setEnable for visibility, and being
    // below the card it can never block the card's touch.
    this.focusFrames = []
    const self = this

    CARDS.forEach((card, i) => {
      const y = 118 + i * 152

      const frame = createWidget(widget.STROKE_RECT, {
        x: px(50),
        y: px(y - 5),
        w: px(380),
        h: px(146),
        radius: px(23),
        line_width: px(4),
        color: COLORS.background
      })
      self.focusFrames.push(frame)

      // BUTTON is the only widget that fires click_func on
      // this firmware (verified v1.1.2); FILL_RECT +
      // addEventListener(CLICK_DOWN) does not.
      const button = createWidget(widget.BUTTON, {
        x: px(55),
        y: px(y),
        w: px(370),
        h: px(136),
        text: '',
        color: COLORS.white,
        normal_color: COLORS.card,
        press_color: 0x24384a,
        radius: px(18),
        click_func: () => self.openCard(i)
      })

      addText(
        card.title, 80, y + 12, 200, 22, 14, COLORS.muted, 0,
        { passThrough: true }
      )

      const countColor = i === 0 ? COLORS.orangeText : COLORS.greenText
      const count = addText(
        '-', 80, y + 38, 90, 74, 46, countColor, 0,
        { passThrough: true }
      )

      const detail = addText(
        i === 0 ? 'Checking pending approvals...' : 'Checking inbox...',
        185, y + 32, 222, 96,
        15, COLORS.white, 0,
        { passThrough: true }
      )

      if (i === 0) {
        self.approvalCount = count
        self.approvalDetail = detail
      } else {
        self.messageCount = count
        self.messageDetail = detail
      }
    })

    addText('TAP KEYS OR CARDS', 60, 432, 360, 24, 15, COLORS.muted)

    this.updateFocus()
  },

  updateFocus() {
    this.focusFrames.forEach((frame, i) => {
      frame.setProperty(
        prop.MORE,
        { color: i === this.focusIndex ? COLORS.focus : COLORS.background }
      )
    })
  },

  // ----------------------------------------------------------
  // Navigation
  // ----------------------------------------------------------

  openCard(i) {
    const card = CARDS[i]

    if (!card || this.isNavigating) {
      return
    }

    this.isNavigating = true
    console.log(TAG + ' Open ' + card.id)

    try {
      push({ url: card.page })
    } catch (error) {
      this.isNavigating = false
      console.log(TAG + ' Navigation failed: ' + String(error))
    }
  },

  // ----------------------------------------------------------
  // Data
  // ----------------------------------------------------------

  nowText() {
    const d = new Date()
    const hh = ('0' + d.getHours()).slice(-2)
    const mm = ('0' + d.getMinutes()).slice(-2)
    return hh + ':' + mm
  },

  setLinkStatus(text, color) {
    this.linkStatus.text = text
    this.linkStatus.color = color
    this.linkDot.setProperty(prop.MORE, { color })
  },

  updateApprovalsCard(approvals) {
    const nowSec = Math.floor(Date.now() / 1000)

    this.approvalCount.text = String(approvals.length)

    if (approvals.length === 0) {
      this.approvalCount.color = COLORS.greenText
      this.approvalDetail.text = 'ALL CLEAR\nNo pending approvals.'
      this.approvalDetail.color = COLORS.muted
    } else {
      this.approvalCount.color = COLORS.orangeText
      const first = approvals[0]
      const command = truncate(
        cleanCommand(first.command), 60
      )
      const project = projectOf(first.cwd)

      this.approvalDetail.text =
        command + '\n' + project.toUpperCase()
      this.approvalDetail.color = COLORS.white
    }
  },

  updateMessagesCard(events) {
    const nowSec = Math.floor(Date.now() / 1000)

    const undelivered = events.filter(
      (e) => !e.delivered
    ).length

    this.messageCount.text = String(undelivered)
    this.messageCount.color =
      undelivered > 0 ? COLORS.orangeText : COLORS.greenText

    if (events.length === 0) {
      this.messageDetail.text =
        'INBOX EMPTY\nNothing received yet.'
      this.messageDetail.color = COLORS.muted
    } else {
      const latest = events[0]
      this.messageDetail.text =
        truncate(latest.title || latest.type || 'EVENT', 40) +
        '\n' +
        ageText(latest.created_at, nowSec).toUpperCase() +
        (latest.delivered ? '' : ' · NEW')
      this.messageDetail.color = COLORS.white
    }
  },

  // Probe which generation of app-side JS the phone is actually running.
  // Returns a short label: 'JS <marker>' when the phone side answers with
  // its build marker, 'JS 115' when it answers with the 1.1.5 unknown-method
  // marker, 'JS OLD' for anything older.
  probePhoneJs() {
    return this.request(
      { method: 'bridge.version', params: {} },
      { timeout: 8000 }
    ).then(
      (result) => {
        const mark = result && result.js ? String(result.js) : '?'
        console.log(TAG + ' Phone side JS: ' + mark)
        return 'JS ' + mark
      },
      (error) => {
        const msg =
          error && error.message ? error.message : String(error)
        console.log(TAG + ' Phone side probe failed: ' + msg)
        if (msg.indexOf('Unknown 20261120') >= 0) {
          return 'JS 115'
        }
        return 'JS OLD'
      }
    )
  },

  async loadDashboard(options) {
    const now = Date.now()
    const force = options && options.force === true

    if (this.destroyed || this.busyLoad) {
      return
    }

    if (!force && now - this.lastLoadAt < RELOAD_GUARD_MS) {
      return
    }

    this.busyLoad = true
    this.lastLoadAt = now

    const jsProbe = this.probePhoneJs()

    // The two requests are independent: in v1.1.0 a single
    // failed/slow endpoint (e.g. the side service still
    // booting) failed the whole Promise.all and the page
    // sat on OFFLINE until the next 60s tick.
    let approvals = null
    let approvalsError = null
    let events = null
    let historyError = null

    try {
      const approvalResult = await this.request(
        {
          method: 'bridge.approval.list',
          params: { status: 'pending', limit: 5 }
        },
        { timeout: REQUEST_TIMEOUT }
      )

      if (!approvalResult || approvalResult.ok !== true) {
        throw new Error('bad approval response')
      }

      approvals = Array.isArray(approvalResult.approvals)
        ? approvalResult.approvals
        : []
    } catch (error) {
      approvalsError = error
    }

    try {
      const historyResult = await this.request(
        {
          method: 'bridge.history',
          params: { consumer_id: 'iphone', limit: 20 }
        },
        { timeout: REQUEST_TIMEOUT }
      )

      if (!historyResult || historyResult.ok !== true) {
        throw new Error('bad history response')
      }

      events = Array.isArray(historyResult.events)
        ? historyResult.events
        : []
    } catch (error) {
      historyError = error
    }

    this.busyLoad = false

    let jsLabel = null
    try {
      jsLabel = await jsProbe
    } catch (error) {
      jsLabel = 'JS ?'
    }

    if (this.destroyed) {
      return
    }

    if (approvals) {
      this.updateApprovalsCard(approvals)
    }

    if (events) {
      this.updateMessagesCard(events)
    }

    if (approvalsError && historyError) {
      this.setLinkStatus(
        'OFFLINE ' + jsLabel + ' · ' + shortError(approvalsError, 12),
        COLORS.redText
      )
      this.scheduleFastRetry()
      console.log(
        TAG + ' Dashboard load failed: ' +
        String(
          approvalsError && approvalsError.message
            ? approvalsError.message
            : approvalsError
        )
      )
    } else if (approvalsError || historyError) {
      this.setLinkStatus(
        'PARTIAL · ' + this.nowText(),
        COLORS.orangeText
      )
    } else {
      this.fastRetryAt = 0
      this.setLinkStatus(
        'LINKED · ' + this.nowText(),
        COLORS.greenText
      )
      console.log(TAG + ' Dashboard refreshed')
    }
  },

  // Fast retries after a failure so the page recovers in
  // seconds instead of waiting for the 60s refresh tick.
  scheduleFastRetry() {
    if (this.fastRetryTimer) {
      return
    }

    this.fastRetryAt += 1

    if (this.fastRetryAt > FAST_RETRY_DELAYS_MS.length) {
      return
    }

    const delay = FAST_RETRY_DELAYS_MS[this.fastRetryAt - 1]
    const page = this

    this.fastRetryTimer = setTimeout(() => {
      this.fastRetryTimer = null

      if (!this.destroyed) {
        console.log(TAG + ' Fast retry #' + page.fastRetryAt)
        this.loadDashboard({ force: true })
      }
    }, delay)
  },

  // ----------------------------------------------------------
  // Auto refresh
  // ----------------------------------------------------------

  startAutoRefresh() {
    if (this.refreshTimer) {
      return
    }

    this.refreshTimer = setInterval(() => {
      this.loadDashboard()
    }, REFRESH_MS)
  },

  stopAutoRefresh() {
    if (this.refreshTimer) {
      clearInterval(this.refreshTimer)
      this.refreshTimer = null
    }
  },

  // ----------------------------------------------------------
  // Hardware buttons
  // ----------------------------------------------------------

  handleKeyAction(action) {
    if (action === 'up') {
      this.focusIndex = (this.focusIndex - 1 + CARDS.length) % CARDS.length
      this.updateFocus()
      return true
    }

    if (action === 'down') {
      this.focusIndex = (this.focusIndex + 1) % CARDS.length
      this.updateFocus()
      return true
    }

    if (action === 'select') {
      this.openCard(this.focusIndex)
      return true
    }

    return false
  },

  onResume() {
    this.isNavigating = false
    console.log(TAG + ' Home resumed')

    // A back navigation destroys the previous page's keypad
    // handler; re-claim it for this page.
    try {
      activateKeypad((action) => this.handleKeyAction(action))
    } catch (error) {
      console.log(TAG + ' Keypad re-activate failed: ' + String(error))
    }

    this.loadDashboard({ force: true })
  },

  onPause() {
    this.stopAutoRefresh()
  },

  onDestroy() {
    console.log(TAG + ' Home destroyed')

    this.destroyed = true
    this.stopAutoRefresh()

    if (this.fastRetryTimer) {
      clearTimeout(this.fastRetryTimer)
      this.fastRetryTimer = null
    }

    // Keep the background service running.
    // The shared keypad handler is sticky: the visible page
    // re-activates it in build()/onResume().
  }
}))
