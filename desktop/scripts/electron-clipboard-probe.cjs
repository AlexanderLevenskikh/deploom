// Real-Electron clipboard probe. Runs as an Electron main process, performs a
// write -> read-back round trip on a SYNTHETIC marker (never anything from a
// user's clipboard), and exits 0 on byte-identical success, 2 on mismatch,
// 3 on device failure, 4 when the app could not reach ready state.
const { app, clipboard } = require('electron')

const marker = process.argv[2] ?? ''
if (!marker) {
  console.log('CLIPBOARD_PROBE ' + JSON.stringify({ ok: false, error: 'NO_MARKER' }))
  app.exit(3)
}

app.whenReady().then(() => {
  try {
    clipboard.writeText(marker)
    const back = clipboard.readText()
    const ok = back === marker
    console.log(
      'CLIPBOARD_PROBE ' +
        JSON.stringify({ ok, markerBytes: Buffer.byteLength(marker, 'utf8'), readbackBytes: Buffer.byteLength(back, 'utf8'), markerEqual: ok }),
    )
    app.exit(ok ? 0 : 2)
  } catch (error) {
    console.log('CLIPBOARD_PROBE ' + JSON.stringify({ ok: false, error: String(error && error.message ? error.message : error) }))
    app.exit(3)
  }
})
