// Flush the result before exit: Electron app.exit can discard buffered stdout.
const { app, clipboard } = require('electron')
const marker = process.argv[2] ?? ''
let finished = false
let readyTimer
function finish(payload, code) {
  if (finished) return
  finished = true
  clearTimeout(readyTimer)
  process.stdout.write('CLIPBOARD_PROBE ' + JSON.stringify(payload) + '\n', error => {
    // Return from the stdout I/O callback before shutting down Chromium.
    // Success uses normal app.quit cleanup; abrupt exit can race native teardown.
    setImmediate(() => {
      if (!error && code === 0) app.quit()
      else app.exit(error ? 3 : code)
    })
  })
}
if (!marker) {
  finish({ ok: false, error: 'NO_MARKER' }, 3)
} else {
  readyTimer = setTimeout(() => finish({ ok: false, error: 'APP_READY_TIMEOUT' }, 4), 30_000)
  app.whenReady().then(() => {
    if (finished) return
    try {
      clipboard.writeText(marker)
      const back = clipboard.readText()
      const ok = back === marker
      finish({ ok, markerBytes: Buffer.byteLength(marker, 'utf8'), readbackBytes: Buffer.byteLength(back, 'utf8'), markerEqual: ok }, ok ? 0 : 2)
    } catch (error) {
      finish({ ok: false, error: String(error && error.message ? error.message : error) }, 3)
    }
  }).catch(error => finish({ ok: false, error: 'APP_READY_FAILED', detail: String(error && error.message ? error.message : error) }, 4))
}
