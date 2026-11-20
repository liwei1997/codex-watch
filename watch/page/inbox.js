import { BasePage } from '@zeppos/zml/base-page'
import { back, push } from '@zos/router'
import {
  createWidget,
  widget,
  prop,
  align,
  deleteWidget
} from '@zos/ui'
import { px } from '@zos/utils'

import {
  COLORS,
  drawBackground,
  addText,
  addButton,
  addHeader,
  setButtonFocus,
  setPassThrough,
  shortError
} from './ui'

import { activateKeypad } from './keypad'

import {
  cleanCommand,
  truncate,
  ageText,
  eventKindLabel
} from '../common/format'

// ============================================================
// Codex Watch - Inbox
//
// Scrollable, newest-first event history from
// GET /v1/events/history. Cards show kind badge, title,
// machine and age; undelivered events carry a NEW dot and
// are ACKed once displayed (so the background service does
// not re-push them).
//
// Touch (v1.1.2): cards are BUTTON widgets (click_func)
// — the only tappable surface verified on this firmware —
// with gesture pass-through texts/dots stacked on top.
//
// Data (v1.1.1): requests carry an explicit 15s timeout and
// fast retries (3s/6s) after a failure.
// ============================================================

const TAG = '[Codex Watch Inbox]'

const HISTORY_LIMIT = 20
const SYNC_MS = 30 * 1000
const RELOAD_GUARD_MS = 5 * 1000
const MAX_AUTO_ACK = 5
const REQUEST_TIMEOUT = 15000
const FAST_RETRY_DELAYS_MS = [3000, 6000]

// List geometry (screen coordinates)
const LIST_X = 52
const LIST_Y = 138
const LIST_W = 376
const LIST_H = 252
const CARD_H = 84
const CARD_GAP = 10
const STEP = CARD_H + CARD_GAP
const MAX_FALLBACK_CARDS = 3

const KIND_COLORS = {
  'APPROVAL': COLORS.orangeText,
  'TASK': COLORS.greenText,
  'SESSION': COLORS.greenText,
  'EVENT': COLORS.muted
}

Page(
  BasePage({
    state: {},

    onInit() {
      this.events = []
      this.listWidgets = []
      this.container = null
      this.containerOk = null
      this.loading = false
      this.syncTimer = null
      this.fastRetryTimer = null
      this.fastRetryAt = 0
      this.lastSyncAt = 0
      this.selected = 0
      this.destroyed = false

      console.log(TAG + ' onInit')
    },

    build() {
      drawBackground()
      addHeader('INBOX')

      this.status = addText(
        'SYNCING...', 60, 112, 360, 22,
        14, COLORS.muted
      )

      // Scroll container (falls back to a static list if the
      // device does not provide VIEW_CONTAINER).
      this.createList()

      const items = [
        {
          button: addButton(
            'SYNC', 65, 402, 170, 52,
            () => this.sync(),
            {
              normalColor: COLORS.green,
              pressColor: COLORS.greenPress
            }
          ),
          normalColor: COLORS.green,
          pressColor: COLORS.greenPress,
          action: () => this.sync()
        },
        {
          button: addButton(
            'BACK', 245, 402, 170, 52,
            () => back()
          ),
          normalColor: COLORS.cardAlt,
          pressColor: 0x3d5263,
          action: () => back()
        }
      ]

      this.syncItem = items[0]

      function updateFocus() {
        items.forEach((item, index) => {
          setButtonFocus(
            item.button.focusFrame,
            index === page.selected
          )
        })
      }

      const page = this

      this.updateListFocus = updateFocus
      updateFocus()

      try {
        activateKeypad((action) => this.handleKeypad(action, updateFocus))
      } catch (error) {
        console.log(TAG + ' Keypad activate failed: ' + String(error))
      }

      // Initial sync + periodic refresh while visible.
      this.sync()
      this.syncTimer = setInterval(() => this.sync(), SYNC_MS)
    },

    // ----------------------------------------------------------
    // List rendering
    // ----------------------------------------------------------

    createList() {
      try {
        const container = createWidget(widget.VIEW_CONTAINER, {
          x: px(LIST_X),
          y: px(LIST_Y),
          w: px(LIST_W),
          h: px(LIST_H),
          scroll_enable: 1,
          pos_y: 0
        })

        if (!container || typeof container.createWidget !== 'function') {
          throw new Error('VIEW_CONTAINER unusable')
        }

        this.container = container
        this.containerOk = true

        console.log(TAG + ' VIEW_CONTAINER ready')
      } catch (error) {
        this.container = null
        this.containerOk = false

        console.log(
          TAG + ' Falling back to static list: ' +
          String(error)
        )
      }
    },

    createChild(x, y, w, h, type, props) {
      if (this.containerOk) {
        // Container coordinates: x/y are relative to the
        // container's top-left corner.
        return this.container.createWidget(type, {
          ...props,
          x: px(x),
          y: px(y)
        })
      }

      // Fallback: place directly on the page.
      return createWidget(type, {
        ...props,
        x: px(LIST_X + x),
        y: px(LIST_Y + y)
      })
    },

    clearList() {
      this.listWidgets.forEach((w) => {
        try {
          deleteWidget(w)
        } catch (error) {
          // ignore
        }
      })

      this.listWidgets = []
    },

    resetScroll() {
      if (this.containerOk) {
        try {
          this.container.setProperty(prop.MORE, { pos_y: 0 })
        } catch (error) {
          // ignore
        }
      }
    },

    eventBody(event) {
      if (event.type === 'approval_request') {
        const payload = event.payload || {}

        return truncate(
          cleanCommand(payload.command) ||
          event.body ||
          event.title,
          60
        )
      }

      return truncate(event.body || event.title || '', 60)
    },

    renderList() {
      this.clearList()
      const events = this.events

      if (events.length === 0) {
        this.listWidgets.push(
          this.createTextInList(
            'INBOX EMPTY', 16, 30, 344, 26, 20, COLORS.muted
          ),
          this.createTextInList(
            'Sync when your first Codex event arrives.',
            16, 60, 344, 20, 13, COLORS.muted
          )
        )
        return
      }

      const nowSec = Math.floor(Date.now() / 1000)

      events.forEach((event, i) => {
        const y = i * STEP

        if (!this.containerOk && i >= MAX_FALLBACK_CARDS) {
          return
        }

        const kind = eventKindLabel(event.type)
        const kindColor = KIND_COLORS[kind] || COLORS.muted
        const isApproval = event.type === 'approval_request'

        // BUTTON is the only widget that fires click_func on
        // this firmware (verified v1.1.2).
        const card = this.createChild(0, y, LIST_W, CARD_H,
          widget.BUTTON, {
            w: px(LIST_W),
            h: px(CARD_H),
            text: '',
            color: COLORS.white,
            normal_color: isApproval ? COLORS.cardAlt : COLORS.card,
            press_color: isApproval ? 0x33455a : 0x24384a,
            radius: px(16),
            click_func: () => this.onCardTap(event)
          })

        // Texts on top of the card must let the touch fall
        // through to the card.
        this.listWidgets.push(
          card,
          this.createTextInList(
            kind, 18, y + 10, 140, 20, 13, kindColor, true
          ),
          this.createTextInList(
            this.eventBody(event),
            18, y + 32, 310, 24, 15, COLORS.white, true
          ),
          this.createTextInList(
            (event.machine_id || 'codex') +
            ' · ' +
            ageText(event.created_at, nowSec).toUpperCase(),
            18, y + 58, 300, 18, 12, COLORS.muted, true
          )
        )

        if (!event.delivered) {
          const dot = this.createChild(344, y + 12, 14, 14,
            widget.FILL_RECT, {
              w: px(14),
              h: px(14),
              radius: px(7),
              color: COLORS.orangeText
            })
          setPassThrough(dot)
          this.listWidgets.push(dot)
        }
      })

      this.resetScroll()
    },

    createTextInList(text, x, y, w, h, size, color, passThrough = false) {
      const textWidget = this.createChild(x, y, w, h, widget.TEXT, {
        w: px(w),
        h: px(h),
        text: String(text),
        text_size: px(size),
        color,
        align_h: align.LEFT_H,
        align_v: align.CENTER_V
      })

      if (passThrough) {
        setPassThrough(textWidget)
      }

      return textWidget
    },

    onCardTap(event) {
      if (event.type !== 'approval_request') {
        return
      }

      console.log(TAG + ' Open approvals page')

      try {
        push({ url: 'page/approvals' })
      } catch (error) {
        console.log(TAG + ' Navigation failed: ' + String(error))
      }
    },

    // ----------------------------------------------------------
    // Data
    // ----------------------------------------------------------

    async sync(options) {
      if (this.destroyed || this.loading) {
        return
      }

      const force = options && options.force === true
      const now = Date.now()

      if (!force && now - this.lastSyncAt < RELOAD_GUARD_MS) {
        return
      }

      this.loading = true
      this.lastSyncAt = now

      try {
        const result = await this.request(
          {
            method: 'bridge.history',
            params: {
              consumer_id: 'iphone',
              limit: HISTORY_LIMIT
            }
          },
          { timeout: REQUEST_TIMEOUT }
        )

        if (this.destroyed) {
          return
        }

        if (!result || result.ok !== true) {
          throw new Error('Invalid response from phone')
        }

        this.events = Array.isArray(result.events)
          ? result.events
          : []

        this.status.text =
          this.events.length + ' EVENT(S) · ' + this.nowText()
        this.status.color = COLORS.greenText
        this.fastRetryAt = 0

        this.renderList()
        this.ackDisplayed()

        console.log(
          TAG + ' Sync OK, count=' + this.events.length
        )

      } catch (error) {
        if (!this.destroyed) {
          this.status.text =
            'CONNECTION FAILED · ' + shortError(error, 18)
          this.status.color = COLORS.redText
          this.scheduleFastRetry()
        }
        console.log(
          TAG + ' Sync failed: ' +
          String(error && error.message ? error.message : error)
        )

      } finally {
        this.loading = false
      }
    },

    // Fast retries after a failure so the page recovers in
    // seconds instead of waiting for the 30s sync tick.
    scheduleFastRetry() {
      if (this.fastRetryTimer) {
        return
      }

      this.fastRetryAt += 1

      if (this.fastRetryAt > FAST_RETRY_DELAYS_MS.length) {
        return
      }

      const delay = FAST_RETRY_DELAYS_MS[this.fastRetryAt - 1]

      this.fastRetryTimer = setTimeout(() => {
        this.fastRetryTimer = null

        if (!this.destroyed) {
          console.log(TAG + ' Fast retry #' + this.fastRetryAt)
          this.sync({ force: true })
        }
      }, delay)
    },

    // Events shown on screen count as received. ACK the newest
    // undelivered ones so the background service will not push
    // duplicates after it restarts.
    async ackDisplayed() {
      // Approval events are intentionally left for the
      // background service: it creates the notification with
      // APPROVE/REJECT buttons and only ACKs after the push
      // succeeded. ACKing them here would suppress the push.
      const undelivered = this.events
        .filter(
          (e) => !e.delivered && e.type !== 'approval_request'
        )
        .slice(0, MAX_AUTO_ACK)

      for (const event of undelivered) {
        try {
          await this.request(
            {
              method: 'bridge.ack',
              params: {
                event_id: event.event_id,
                consumer_id: 'iphone',
                stage: 'watch_received'
              }
            },
            { timeout: REQUEST_TIMEOUT }
          )

          event.delivered = true
        } catch (error) {
          console.log(
            TAG + ' ACK failed for ' + event.event_id +
            ': ' + String(error)
          )
        }
      }
    },

    nowText() {
      const d = new Date()
      return (
        ('0' + d.getHours()).slice(-2) + ':' +
        ('0' + d.getMinutes()).slice(-2)
      )
    },

    // ----------------------------------------------------------
    // Keys
    // ----------------------------------------------------------

    handleKeypad(action, updateFocus) {
      if (action === 'up' || action === 'down') {
        this.selected = this.selected === 0 ? 1 : 0
        updateFocus()
        return true
      }

      if (action === 'select') {
        if (this.selected === 0) {
          this.sync()
        } else {
          back()
        }
        return true
      }

      if (action === 'back') {
        back()
        return true
      }

      return false
    },

    // ----------------------------------------------------------
    // Lifecycle
    // ----------------------------------------------------------

    onResume() {
      console.log(TAG + ' resumed')

      // A back navigation destroys the previous page's keypad
      // handler; re-claim it for this page.
      try {
        activateKeypad((action) => this.handleKeypad(action, this.updateListFocus))
      } catch (error) {
        console.log(TAG + ' Keypad re-activate failed: ' + String(error))
      }

      this.sync({ force: true })
    },

    onPause() {
      if (this.syncTimer) {
        clearInterval(this.syncTimer)
        this.syncTimer = null
      }
    },

    onDestroy() {
      console.log(TAG + ' destroyed')

      this.destroyed = true

      if (this.syncTimer) {
        clearInterval(this.syncTimer)
        this.syncTimer = null
      }

      if (this.fastRetryTimer) {
        clearTimeout(this.fastRetryTimer)
        this.fastRetryTimer = null
      }

      this.clearList()

      // Sticky keypad: the visible page re-activates the shared
      // handler in build()/onResume(); do not deactivate here,
      // this page may be destroyed after the previous page has
      // already re-activated its own handler.
    }
  })
)
