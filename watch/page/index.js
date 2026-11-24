import { BasePage } from '@zeppos/zml/base-page'
import { createWidget, widget, prop } from '@zos/ui'
import { px } from '@zos/utils'
import { push } from '@zos/router'
import { requestPermission } from '@zos/app'
import { start } from '@zos/app-service'
import { createSysTimer, stopTimer } from '@zos/timer'
import { COLORS, drawBackground, addText } from './ui'
import { activateKeypad } from './keypad'

const TAG = '[Codex Watch]'
const BACKGROUND_SERVICE = 'app-service/codex_background'
let backgroundStartInFlight = false

function launchBackgroundService() {
  if (backgroundStartInFlight) return
  backgroundStartInFlight = true
  try {
    const result = start({
      file: BACKGROUND_SERVICE,
      reload: true,
      complete_func: status => {
        backgroundStartInFlight = false
        if (!status || status.result !== true) console.log(TAG + ' Background start failed')
      }
    })
    if (result !== 0) backgroundStartInFlight = false
  } catch (error) {
    backgroundStartInFlight = false
    console.log(TAG + ' Background exception: ' + String(error))
  }
}

Page(BasePage({
  state: {},

  onInit() {
    this.focus = null
    this.refreshTimer = null
    this.destroyed = false
  },

  build() {
    drawBackground()
    addText('CODEX WATCH', 60, 50, 360, 38, 27, COLORS.white)
    addText('AWAY NOTIFICATIONS', 60, 102, 360, 25, 15, COLORS.muted)
    this.status = addText('CONNECTING...', 60, 165, 360, 28, 16, COLORS.muted)
    this.count = addText('—', 60, 215, 360, 72, 52, COLORS.greenText)
    this.detail = addText('PENDING APPROVALS', 60, 292, 360, 28, 15, COLORS.white)
    this.card = createWidget(widget.BUTTON, {
      x: px(60), y: px(350), w: px(360), h: px(64),
      text: 'OPEN APPROVALS',
      color: COLORS.white,
      normal_color: COLORS.card,
      press_color: COLORS.cardAlt,
      radius: px(16),
      click_func: () => this.openApprovals()
    })
    activateKeypad(action => {
      if (action === 'select') this.openApprovals()
      return action === 'up' || action === 'down' || action === 'select'
    })
    this.load()
    this.refreshTimer = setInterval(() => this.load(), 30000)
    this.startBackground()
  },

  startBackground() {
    requestPermission({
      permissions: ['device:os.bg_service'],
      callback: result => {
        if (Array.isArray(result) && result[0] === 2) launchBackgroundService()
      }
    })
  },

  async load() {
    if (this.destroyed || this.loading) return
    this.loading = true
    try {
      const result = await this.request({
        method: 'bridge.approval.list',
        params: { status: 'pending', limit: 50 }
      }, { timeout: 15000 })
      if (!result || !result.ok) throw new Error(result && result.error || 'Bridge unavailable')
      const approvals = result.approvals || []
      this.count.text = String(approvals.length)
      this.status.text = 'BRIDGE CONNECTED'
      this.detail.text = approvals.length
        ? String(approvals.length) + ' APPROVAL(S) WAITING'
        : 'NO PENDING APPROVALS'
      this.status.color = COLORS.greenText
    } catch (error) {
      this.status.text = 'BRIDGE OFFLINE'
      this.status.color = COLORS.redText
      this.detail.text = String(error && error.message || error).slice(0, 40)
    } finally {
      this.loading = false
    }
  },

  openApprovals() {
    push({ url: 'page/approvals' })
  },

  onResume() {
    this.load()
    activateKeypad(action => {
      if (action === 'select') this.openApprovals()
      return action === 'up' || action === 'down' || action === 'select'
    })
  },

  onPause() {
    if (this.refreshTimer) {
      clearInterval(this.refreshTimer)
      this.refreshTimer = null
    }
  },

  onDestroy() {
    this.destroyed = true
    if (this.refreshTimer) clearInterval(this.refreshTimer)
    try { stopTimer(this.refreshTimer) } catch (error) {}
  }
}))
