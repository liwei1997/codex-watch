import { BasePage } from '@zeppos/zml/base-page'
import { back } from '@zos/router'
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
  addCard,
  addButton,
  addHeader,
  setButtonFocus,
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
// Codex Watch - Approvals
//
// Pending approval queue. Focus a card and select to open a
// DECISION overlay with APPROVE / REJECT / CANCEL. Refreshes
// every 15s while visible and right after each decision.
// Resolved/expired approvals show in a RECENT section.
//
// Touch (v1.1.2): cards are BUTTON widgets (click_func) —
// the only tappable surface verified on this firmware —
// with pass-through texts. Focus frames sit BELOW the card
// in Z-order and are color-driven, so they never block
// touch or hide.
//
// Data (v1.1.1): requests carry an explicit 15s timeout and
// fast retries (3s/6s) after a failure.
// ============================================================

const TAG = '[Codex Watch Approvals]'

const LIST_LIMIT = 10
const REFRESH_MS = 15 * 1000
const RELOAD_GUARD_MS = 4 * 1000
const MAX_PENDING_CARDS = 2
const OVERLAY_CLOSE_MS = 2500
const REQUEST_TIMEOUT = 15000
const FAST_RETRY_DELAYS_MS = [3000, 6000]

// Pending card geometry
const CARD_X = 52
const CARD_W = 376
const CARD_H = 88
const CARD_GAP = 12
const FIRST_CARD_Y = 134
const RECENT_Y = FIRST_CARD_Y + MAX_PENDING_CARDS * (CARD_H + CARD_GAP) + 4

Page(
  BasePage({
    state: {},

    onInit() {
      this.all = []
      this.actionable = []
      this.sent = []
      this.recent = []
      this.focusIndex = 0
      this.pendingItems = []
      this.listWidgets = []
      this.backButton = null
      this.status = null
      this.overlay = null
      this.loading = false
      this.refreshTimer = null
      this.fastRetryTimer = null
      this.fastRetryAt = 0
      this.lastLoadAt = 0
      this.selected = 0 // back/sync button focus
      this.destroyed = false

      console.log(TAG + ' onInit')
    },

    build() {
      drawBackground()
      addHeader('APPROVALS')

      this.status = addText(
        'CONNECTING...', 60, 110, 360, 20,
        14, COLORS.muted
      )

      this.recentLabel = addText(
        'RECENT', 62, RECENT_Y, 120, 20,
        13, COLORS.muted, 0
      )

      this.recentText = addText(
        '', 62, RECENT_Y + 20, 356, 36,
        13, COLORS.muted, 0
      )

      this.backButton = addButton(
        'BACK', 155, 396, 170, 50,
        () => back()
      )

      function updateFocus() {
        if (page.overlay) {
          return
        }

        setButtonFocus(
          page.backButton.focusFrame,
          page.selected === 1
        )

        page.pendingItems.forEach((item, index) => {
          setButtonFocus(
            item.frame,
            index === page.focusIndex
          )
        })
      }

      const page = this
      this.updateListFocus = updateFocus

      try {
        activateKeypad((action) => this.handleKey(action))
      } catch (error) {
        console.log(TAG + ' Keypad activate failed: ' + String(error))
      }

      this.load()
      this.refreshTimer = setInterval(
        () => this.load(), REFRESH_MS
      )
    },

    // ----------------------------------------------------------
    // List rendering
    // ----------------------------------------------------------

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

    renderList() {
      this.clearList()
      this.pendingItems = []

      const nowSec = Math.floor(Date.now() / 1000)

      this.actionable.slice(0, MAX_PENDING_CARDS)
        .forEach((approval, i) => {
          const y = FIRST_CARD_Y + i * (CARD_H + CARD_GAP)

          // Z-order: focus frame first (below), then the
          // BUTTON card, then pass-through texts on top.
          // The frame is color-driven (background = hidden),
          // so it never relies on setEnable for visibility
          // and being below the card it can never block the
          // card's touch.
          const frame = createWidget(widget.STROKE_RECT, {
            x: px(CARD_X - 5),
            y: px(y - 5),
            w: px(CARD_W + 10),
            h: px(CARD_H + 10),
            radius: px(23),
            line_width: px(4),
            color: COLORS.background
          })

          // BUTTON is the only widget that fires click_func on
          // this firmware (verified v1.1.2).
          const card = createWidget(widget.BUTTON, {
            x: px(CARD_X),
            y: px(y),
            w: px(CARD_W),
            h: px(CARD_H),
            text: '',
            color: COLORS.white,
            normal_color: COLORS.cardAlt,
            press_color: 0x33455a,
            radius: px(18),
            click_func: () => this.openOverlay(approval)
          })

          const project = projectOf(approval.cwd)
          const command = cleanCommand(approval.command)

          this.listWidgets.push(
            frame,
            card,
            addText(
              'PENDING · ' + project.toUpperCase(),
              CARD_X + 18, y + 10, CARD_W - 36, 20,
              13, COLORS.orangeText, 0,
              { passThrough: true }
            ),
            addText(
              truncate(command, 60),
              CARD_X + 18, y + 34, CARD_W - 60, 26,
              16, COLORS.white, 0,
              { passThrough: true }
            ),
            addText(
              ageText(approval.created_at, nowSec).toUpperCase(),
              CARD_X + CARD_W - 60, y + 10, 42, 20,
              13, COLORS.muted, 2,
              { passThrough: true }
            )
          )

          this.pendingItems.push({ approval, frame })
        })

      // "+N MORE" indicator
      if (this.actionable.length > MAX_PENDING_CARDS) {
        const y =
          FIRST_CARD_Y + MAX_PENDING_CARDS * (CARD_H + CARD_GAP) -
          CARD_GAP + 4

        this.listWidgets.push(
          addText(
            '+' +
            (this.actionable.length - MAX_PENDING_CARDS) +
            ' MORE PENDING',
            62, y, 356, 20, 13, COLORS.orangeText
          )
        )
      }

      // Sent (decision in flight)
      if (this.sent.length > 0) {
        this.listWidgets.push(
          addText(
            'SENT · AWAITING CONFIRMATION: ' +
            truncate(cleanCommand(this.sent[0].command), 30),
            62, RECENT_Y, 356, 20, 13, COLORS.greenText, 0
          )
        )
      }

      // Recent (resolved/expired)
      if (this.recent.length === 0) {
        this.recentText.text = 'No recent decisions.'
      } else {
        this.recentText.text = this.recent
          .map((a) => {
            const label =
              a.status === 'expired'
                ? 'EXPIRED'
                : (a.decision === 'accept' ? 'APPROVED' : 'REJECTED')

            return (
              label +
              ' · ' +
              truncate(cleanCommand(a.command), 34) +
              (a.decided_by ? ' · ' + a.decided_by.toUpperCase() : '')
            )
          })
          .join('\n')
      }

      if (this.actionable.length === 0) {
        this.status.text = 'ALL CLEAR'
        this.status.color = COLORS.greenText
      } else {
        this.status.text =
          this.actionable.length + ' PENDING APPROVAL(S)'
        this.status.color = COLORS.orangeText
      }

      if (this.focusIndex >= this.pendingItems.length) {
        this.focusIndex = Math.max(
          0, this.pendingItems.length - 1
        )
      }

      this.updateListFocus()
    },

    // ----------------------------------------------------------
    // Decision overlay
    // ----------------------------------------------------------

    openOverlay(approval) {
      if (this.overlay) {
        return
      }

      this.clearList()
      this.pendingItems = []

      const self = this

      const card = addCard(45, 88, 390, 300)
      const title = addText(
        'DECISION REQUIRED', 70, 100, 340, 22,
        14, COLORS.orangeText, 0
      )
      const command = addText(
        cleanCommand(approval.command),
        70, 128, 340, 96,
        18, COLORS.white, 0
      )
      const project = addText(
        projectOf(approval.cwd).toUpperCase() +
        ' · ' +
        ageText(
          approval.created_at,
          Math.floor(Date.now() / 1000)
        ).toUpperCase(),
        70, 230, 340, 20,
        12, COLORS.muted, 0
      )
      const status = addText(
        'SELECT A DECISION', 70, 254, 340, 22,
        14, COLORS.muted, 0
      )

      const approveItem = {
        button: addButton(
          'APPROVE', 70, 288, 105, 52,
          () => self.decisionAction('accept'),
          {
            normalColor: COLORS.green,
            pressColor: COLORS.greenPress,
            textSize: 16
          }
        ),
        normalColor: COLORS.green,
        pressColor: COLORS.greenPress
      }

      const rejectItem = {
        button: addButton(
          'REJECT', 188, 288, 105, 52,
          () => self.decisionAction('decline'),
          {
            normalColor: COLORS.red,
            pressColor: COLORS.redPress,
            textSize: 16
          }
        ),
        normalColor: COLORS.red,
        pressColor: COLORS.redPress
      }

      const cancelItem = {
        button: addButton(
          'CANCEL', 306, 288, 105, 52,
          () => self.overlay && self.overlay.close(),
          {
            normalColor: COLORS.cardAlt,
            pressColor: 0x3d5263,
            textSize: 16
          }
        ),
        normalColor: COLORS.cardAlt,
        pressColor: 0x3d5263
      }

      const buttons = [approveItem, rejectItem, cancelItem]

      const overlay = {
        approval,
        widgets: [card, title, command, project, status],
        buttons,
        selected: 2, // safe default
        busy: false,
        closeTimer: null
      }

      overlay.updateFocus = () => {
        buttons.forEach((item, i) => {
          setButtonFocus(
            item.button.focusFrame,
            i === overlay.selected && !overlay.busy
          )
        })
      }

      overlay.setStatus = (text, color) => {
        status.text = text
        status.color = color
      }

      overlay.close = () => {
        if (self.overlay !== overlay) {
          return
        }

        if (overlay.closeTimer) {
          clearTimeout(overlay.closeTimer)
          overlay.closeTimer = null
        }

        overlay.widgets.forEach((w) => {
          try {
            deleteWidget(w)
          } catch (error) {
            // ignore
          }
        })

        // Buttons live outside `widgets`; delete them too or they
        // stay on screen as stale tap targets after close.
        overlay.buttons.forEach((item) => {
          const b = item.button

          if (!b) {
            return
          }

          try {
            deleteWidget(b.button)
          } catch (error) {
            // ignore
          }

          try {
            deleteWidget(b.focusFrame)
          } catch (error) {
            // ignore
          }
        })

        self.overlay = null

        // Restore the pending/recent list. The 15s refresh and
        // onResume keep the data current from here.
        self.renderList()
      }

      overlay.keypad = (action) => {
        if (overlay.busy) {
          if (action === 'select') {
            overlay.close()
            return true
          }
          return true
        }

        if (action === 'up' || action === 'down') {
          const delta = action === 'down' ? 1 : buttons.length - 1
          overlay.selected =
            (overlay.selected + delta) % buttons.length
          overlay.updateFocus()
          return true
        }

        if (action === 'select') {
          if (overlay.selected === 0) {
            self.decisionAction('accept')
          } else if (overlay.selected === 1) {
            self.decisionAction('decline')
          } else {
            overlay.close()
          }
          return true
        }

        if (action === 'back') {
          overlay.close()
          return true
        }

        return false
      }

      // Hide the recent section while the overlay is open.
      this.recentLabel.setProperty(
        prop.MORE, { color: COLORS.background }
      )
      this.recentText.setProperty(
        prop.MORE, { color: COLORS.background }
      )

      this.overlay = overlay
      overlay.updateFocus()

      console.log(
        TAG + ' Overlay open for ' + approval.approval_id
      )
    },

    decisionAction(decision) {
      const overlay = this.overlay

      if (!overlay || overlay.busy) {
        return
      }

      overlay.busy = true
      overlay.updateFocus()
      overlay.setStatus('SENDING...', COLORS.orangeText)

      this.sendDecision(overlay, decision)
    },

    async sendDecision(overlay, decision) {
      const approval = overlay.approval

      try {
        const result = await this.request(
          {
            method: 'bridge.approval.decision',
            params: {
              approval_id: approval.approval_id,
              decision: decision
            }
          },
          { timeout: REQUEST_TIMEOUT }
        )

        if (this.destroyed) {
          return
        }

        if (this.overlay !== overlay) {
          return
        }

        if (result && result.ok === true) {
          if (decision === 'accept') {
            overlay.setStatus(
              'SENT: APPROVED', COLORS.greenText
            )
          } else {
            overlay.setStatus(
              'SENT: REJECTED', COLORS.redText
            )
          }

          console.log(
            TAG + ' Decision sent: ' + decision +
            ' ' + approval.approval_id
          )

          this.scheduleOverlayClose()

        } else {
          const err = result && result.error
            ? String(result.error)
            : 'decision rejected'

          if (/already/i.test(err)) {
            overlay.setStatus('ALREADY HANDLED', COLORS.muted)
            this.scheduleOverlayClose()
          } else {
            overlay.setStatus(
              'FAILED: ' + err.slice(0, 40), COLORS.redText
            )
            overlay.busy = false
            overlay.updateFocus()
          }
        }

      } catch (error) {
        if (this.destroyed) {
          return
        }

        if (this.overlay !== overlay) {
          return
        }

        overlay.setStatus(
          'FAILED: ' +
          String(
            error && error.message ? error.message : error
          ).slice(0, 40),
          COLORS.redText
        )
        overlay.busy = false
        overlay.updateFocus()
      }
    },

    scheduleOverlayClose() {
      const overlay = this.overlay

      if (!overlay || overlay.closeTimer) {
        return
      }

      const self = this

      overlay.closeTimer = setTimeout(() => {
        overlay.closeTimer = null

        if (self.overlay === overlay) {
          overlay.close()
        }
      }, OVERLAY_CLOSE_MS)
    },

    // ----------------------------------------------------------
    // Data
    // ----------------------------------------------------------

    async load(options) {
      if (this.destroyed || this.loading) {
        return
      }

      const force = options && options.force === true
      const now = Date.now()

      if (!force && now - this.lastLoadAt < RELOAD_GUARD_MS) {
        return
      }

      this.loading = true
      this.lastLoadAt = now

      try {
        const result = await this.request(
          {
            method: 'bridge.approval.list',
            params: { status: 'all', limit: LIST_LIMIT }
          },
          { timeout: REQUEST_TIMEOUT }
        )

        if (this.destroyed) {
          return
        }

        if (!result || result.ok !== true) {
          throw new Error('Invalid response from phone')
        }

        this.all = Array.isArray(result.approvals)
          ? result.approvals
          : []

        this.actionable = this.all.filter(
          (a) => a.status === 'pending'
        )
        this.sent = this.all.filter(
          (a) => a.status === 'sent'
        )
        this.recent = this.all
          .filter(
            (a) =>
              a.status === 'resolved' ||
              a.status === 'expired'
          )
          .slice(0, 2)

        // A decision resolved while the overlay is open: close it.
        if (this.overlay) {
          const stillPending = this.actionable.some(
            (a) => a.approval_id ===
              this.overlay.approval.approval_id
          )

          if (!stillPending && !this.overlay.busy) {
            this.overlay.close()
            return
          }
        }

        if (!this.overlay) {
          this.renderList()
        }

        this.fastRetryAt = 0

        console.log(
          TAG + ' Loaded: pending=' + this.actionable.length +
          ' recent=' + this.recent.length
        )

      } catch (error) {
        if (!this.destroyed) {
          this.status.text =
            'CONNECTION FAILED · ' + shortError(error, 18)
          this.status.color = COLORS.redText
          this.scheduleFastRetry()
        }
        console.log(
          TAG + ' Load failed: ' +
          String(error && error.message ? error.message : error)
        )
      } finally {
        this.loading = false
      }
    },

    // Fast retries after a failure so the page recovers in
    // seconds instead of waiting for the 15s refresh tick.
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
          this.load({ force: true })
        }
      }, delay)
    },

    // ----------------------------------------------------------
    // Keys
    // ----------------------------------------------------------

    handleKey(action) {
      if (this.overlay) {
        return this.overlay.keypad(action)
      }

      if (action === 'back') {
        back()
        return true
      }

      if (this.actionable.length === 0) {
        if (action === 'select') {
          back()
          return true
        }
        return false
      }

      if (action === 'up') {
        this.focusIndex =
          (this.focusIndex - 1 + this.pendingItems.length) %
          this.pendingItems.length
        this.updateListFocus()
        return true
      }

      if (action === 'down') {
        this.focusIndex =
          (this.focusIndex + 1) % this.pendingItems.length
        this.updateListFocus()
        return true
      }

      if (action === 'select') {
        const item = this.pendingItems[this.focusIndex]

        if (item) {
          this.openOverlay(item.approval)
        }
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
        activateKeypad((action) => this.handleKey(action))
      } catch (error) {
        console.log(TAG + ' Keypad re-activate failed: ' + String(error))
      }

      this.load({ force: true })
    },

    onPause() {
      if (this.refreshTimer) {
        clearInterval(this.refreshTimer)
        this.refreshTimer = null
      }
    },

    onDestroy() {
      console.log(TAG + ' destroyed')

      this.destroyed = true

      if (this.refreshTimer) {
        clearInterval(this.refreshTimer)
        this.refreshTimer = null
      }

      if (this.fastRetryTimer) {
        clearTimeout(this.fastRetryTimer)
        this.fastRetryTimer = null
      }

      if (this.overlay) {
        if (this.overlay.closeTimer) {
          clearTimeout(this.overlay.closeTimer)
        }

        this.overlay.widgets.forEach((w) => {
          try {
            deleteWidget(w)
          } catch (error) {
            // ignore
          }
        })

        this.overlay = null
      }

      this.clearList()

      // Sticky keypad: see inbox.js onDestroy comment.
    }
  })
)
