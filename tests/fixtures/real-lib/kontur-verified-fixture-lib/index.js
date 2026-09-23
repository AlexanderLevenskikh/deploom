'use strict'

// F6 real-resource library: a real npm package (installed from a local
// tarball), consumed by build.js with a real `require`. The exported helpers
// give fixtures controllable COMPATIBLE/INCOMPATIBLE candidates without a
// live registry.

function hasSatisfyingPatch(current, spec) {
  const want = String(spec).replace(/^\^/, '').split('.').map(Number)
  const cur = String(current).split('.').map(Number)
  return cur[0] === want[0] && cur[1] >= want[1] && cur[2] >= want[2]
}

function isEven(n) {
  return Number(n) % 2 === 0
}

module.exports = { hasSatisfyingPatch, isEven }
