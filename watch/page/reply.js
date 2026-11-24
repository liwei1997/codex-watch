import { BasePage } from '@zeppos/zml/base-page'
import { createKeyboard, deleteKeyboard, inputType } from '@zos/ui'
import { back } from '@zos/router'
import { addHeader, addText, addButton, drawBackground, COLORS } from './ui'
import { activateKeypad, deactivateKeypad } from './keypad'

const REQUEST_TIMEOUT = 30000

Page(BasePage({
  state: {},

  onInit(params) {
    let value = {}
    try { value = typeof params === 'string' ? JSON.parse(params) : params || {} } catch (error) {}
    this.threadId = String(value.thread_id || '')
    this.project = String(value.project || 'CODEX')
    this.text = ''
    this.busy = false
    this.keyboard = null
  },

  build() {
    drawBackground()
    addHeader('REPLY')
    addText(this.project.toUpperCase(), 55, 116, 370, 28, 16, COLORS.greenText)
    this.preview = addText('Tap EDIT or VOICE', 55, 158, 370, 112, 20, COLORS.white, 0)
    this.status = addText('', 55, 286, 370, 30, 14, COLORS.muted)
    addButton('EDIT', 55, 350, 112, 58, () => this.openKeyboard(inputType.CHAR))
    addButton('VOICE', 184, 350, 112, 58, () => this.openKeyboard(inputType.VOICE))
    addButton('SEND', 313, 350, 112, 58, () => this.send(), {
      normalColor: COLORS.green,
      pressColor: COLORS.greenPress,
      textSize: 16
    })
    activateKeypad(action => {
      if (action === 'back') back()
      return action === 'back'
    })
  },

  openKeyboard(type) {
    if (this.keyboard || this.busy) return
    deactivateKeypad()
    this.keyboard = createKeyboard({
      inputType: type,
      text: this.text,
      onComplete: (_, result) => {
        this.text = String(result && result.data || '').trim()
        this.keyboard = null
        deleteKeyboard()
        this.preview.text = this.text || 'Tap EDIT or VOICE'
        this.status.text = this.text ? 'READY TO SEND' : 'EMPTY MESSAGE'
        activateKeypad(action => {
          if (action === 'back') back()
          return action === 'back'
        })
      },
      onCancel: () => {
        this.keyboard = null
        deleteKeyboard()
        activateKeypad(action => {
          if (action === 'back') back()
          return action === 'back'
        })
      }
    })
  },

  async send() {
    if (this.busy || !this.threadId || !this.text) return
    this.busy = true
    this.status.text = 'SENDING...'
    try {
      const result = await this.request({
        method: 'bridge.thread.reply',
        params: { thread_id: this.threadId, text: this.text }
      }, { timeout: REQUEST_TIMEOUT })
      if (!result || result.ok !== true) throw new Error(result && result.error || 'Send failed')
      this.status.text = 'SENT TO CODEX'
      setTimeout(() => back(), 1200)
    } catch (error) {
      this.status.text = String(error && error.message || error).slice(0, 42)
      this.busy = false
    }
  },

  onDestroy() {
    deactivateKeypad()
    if (this.keyboard) {
      try { deleteKeyboard() } catch (error) {}
      this.keyboard = null
    }
  }
}))
