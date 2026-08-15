import { readFileSync, statSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

const iconIco = fileURLToPath(new URL('../build/icon.ico', import.meta.url))
const iconPng = fileURLToPath(new URL('../build/icon.png', import.meta.url))
if (statSync(iconIco).size < 10_000 || statSync(iconPng).size < 2_000) throw new Error('Application icon assets are missing or unexpectedly empty')
const ico = readFileSync(iconIco)
const png = readFileSync(iconPng)
if (ico[0] !== 0 || ico[1] !== 0 || ico[2] !== 1 || ico[3] !== 0) throw new Error('build/icon.ico is not a Windows icon')
if (png.subarray(1, 4).toString('ascii') !== 'PNG') throw new Error('build/icon.png is not a PNG')
const builder = readFileSync(new URL('../electron-builder.yml', import.meta.url), 'utf8')
if (!builder.includes('icon: build/icon.ico') || !builder.includes('- build/icon.ico')) throw new Error('Packager is not configured to use/include the application icon')
console.log('Application icon assets OK')
