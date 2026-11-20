import { BaseApp } from '@zeppos/zml/base-app'

App(
  BaseApp({
    onCreate(options) {
      console.log('[Codex Watch] App created')
    },

    onDestroy(options) {
      console.log('[Codex Watch] App destroyed')
    }
  })
)
