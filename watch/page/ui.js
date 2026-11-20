import {
  createWidget,
  widget,
  align,
  text_style,
  prop,
  event
} from '@zos/ui'

import { px } from '@zos/utils'

// ============================================================
// Shared UI primitives (Zepp OS 4.0)
//
// TOUCH (v1.1.1 fix): FILL_RECT has NO click_func parameter
// in the 4.0 API — it is silently ignored, which is why the
// v1.1.0 cards were untouchable. Tappable surfaces use the
// generic widget event API instead:
//
//   attachClick(card, handler)      // CLICK_DOWN on the card
//   setPassThrough(overlayingWidget)// let touches fall through
//                                   // texts/dots stacked above
//
// Per the 4.0 docs, setEnable(false) means the widget does
// not respond to screen gesture events: it stays visible but
// stops intercepting touches, so the FILL_RECT below receives
// CLICK_DOWN.
// ============================================================

export const COLORS = {
  background: 0x0b1117,
  card: 0x192631,
  cardAlt: 0x263440,

  green: 0x174b39,
  greenPress: 0x246c51,

  orange: 0x503a18,
  orangePress: 0x735322,

  red: 0x552b30,
  redPress: 0x77383f,

  white: 0xffffff,
  muted: 0x9eacb8,

  greenText: 0x86efac,
  orangeText: 0xfbbf24,
  redText: 0xff8585,

  focus: 0xffffff
}

export function drawBackground() {
  createWidget(widget.FILL_RECT, {
    x: px(0),
    y: px(0),
    w: px(480),
    h: px(480),
    color: COLORS.background
  })
}

// Make a plain widget (e.g. a FILL_RECT card) tappable.
// CLICK_DOWN is the official 4.0 touch event.
export function attachClick(targetWidget, onClick) {
  if (
    !targetWidget ||
    typeof targetWidget.addEventListener !== 'function'
  ) {
    return
  }

  try {
    targetWidget.addEventListener(event.CLICK_DOWN, onClick)
  } catch (error) {
    console.log('[Codex Watch UI] attachClick failed: ' + String(error))
  }
}

// Widgets stacked above a tappable widget (texts, dots,
// frames) intercept gesture events. Disable their gesture
// response so touches reach the widget below.
export function setPassThrough(targetWidget) {
  if (
    !targetWidget ||
    typeof targetWidget.setEnable !== 'function'
  ) {
    return
  }

  try {
    targetWidget.setEnable(false)
  } catch (error) {
    console.log('[Codex Watch UI] setPassThrough failed: ' + String(error))
  }
}

// Compact one-line error text for on-device status display.
export function shortError(error, max = 24) {
  const s = String(
    error && error.message ? error.message : error || 'ERROR'
  ).replace(/\s+/g, ' ').trim()
  return s.slice(0, max)
}

export function addText(
  text,
  x,
  y,
  w,
  h,
  size = 20,
  color = COLORS.white,
  alignH = align.CENTER_H,
  options = {}
) {
  const textWidget = createWidget(widget.TEXT, {
    x: px(x),
    y: px(y),
    w: px(w),
    h: px(h),
    text: String(text),
    text_size: px(size),
    color,
    align_h: alignH,
    align_v: align.CENTER_V,
    text_style: text_style.WRAP
  })

  // Text stacked on a tappable card must not swallow the
  // touch: let CLICK_DOWN fall through to the card.
  if (options.passThrough) {
    setPassThrough(textWidget)
  }

  return textWidget
}

export function addCard(
  x,
  y,
  w,
  h,
  color = COLORS.card
) {
  return createWidget(widget.FILL_RECT, {
    x: px(x),
    y: px(y),
    w: px(w),
    h: px(h),
    radius: px(18),
    color
  })
}

export function addButton(
  text,
  x,
  y,
  w,
  h,
  onClick,
  options = {}
) {
  const radius = options.radius || 16

  const button = createWidget(widget.BUTTON, {
    x: px(x),
    y: px(y),
    w: px(w),
    h: px(h),

    text,
    text_size: px(options.textSize || 19),

    color: options.textColor || COLORS.white,

    normal_color:
      options.normalColor || COLORS.cardAlt,

    press_color:
      options.pressColor || 0x3d5263,

    radius: px(radius),

    // BUTTON is the documented 4.0 widget that supports
    // click_func directly.
    click_func: onClick
  })

  // White focus frame, color-driven visibility.
  // setEnable(false) = gesture pass-through only (4.0 doc),
  // so the frame never blocks the button's touch.
  const focusFrame = createWidget(widget.STROKE_RECT, {
    x: px(x - 5),
    y: px(y - 5),
    w: px(w + 10),
    h: px(h + 10),

    radius: px(radius + 5),
    line_width: px(4),

    // Hidden until selected
    color: COLORS.background
  })

  focusFrame.setEnable(false)

  return {
    button,
    focusFrame
  }
}

// Pass the STROKE_RECT focus frame widget (e.g. item.focusFrame
// from addButton(), or a raw frame created for a card).
export function setButtonFocus(focusFrame, focused) {
  focusFrame.setProperty(prop.MORE, {
    color: focused
      ? COLORS.focus
      : COLORS.background
  })
}

export function addHeader(title) {
  addText(
    'CODEX WATCH',
    80, 32, 320, 25,
    15,
    COLORS.greenText
  )

  addText(
    title,
    65, 65, 350, 42,
    28,
    COLORS.white
  )
}
