export class RequestTimeoutError extends Error {
  constructor(label, timeoutMs) {
    super(label + ' timed out after ' + timeoutMs + 'ms')
    this.name = 'RequestTimeoutError'
  }
}

export function createSystemRequestRunner(options) {
  const {
    timeoutMs,
    createTimer,
    stopTimer,
  } = options

  let running = false

  function run(label, operation) {
    if (running) {
      return Promise.reject(
        new Error('Another phone request is already running'),
      )
    }

    running = true

    const request = new Promise((resolve, reject) => {
      let settled = false
      let timerId = null

      function stopActiveTimer() {
        if (timerId === null) {
          return
        }

        const id = timerId
        timerId = null

        try {
          stopTimer(id)
        } catch (error) {
          // A completed request must not fail because timer cleanup did.
        }
      }

      function finish(callback, value, stopSystemTimer) {
        if (settled) {
          return
        }

        settled = true

        if (stopSystemTimer) {
          stopActiveTimer()
        }

        callback(value)
      }

      try {
        const createdTimerId = createTimer(
          false,
          timeoutMs,
          () => {
            timerId = null

            finish(
              reject,
              new RequestTimeoutError(label, timeoutMs),
              false,
            )
          },
        )

        timerId = createdTimerId

        if (settled) {
          stopActiveTimer()
          return
        }

        const operationResult = operation()

        Promise.resolve(operationResult).then(
          value => finish(resolve, value, true),
          error => finish(reject, error, true),
        )
      } catch (error) {
        finish(reject, error, true)
      }
    })

    return request.finally(() => {
      running = false
    })
  }

  return {
    run,
  }
}
