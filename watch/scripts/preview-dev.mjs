import { spawn } from 'node:child_process'
import { readFile, writeFile } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const configPath = resolve(root, 'app.json')
const original = await readFile(configPath, 'utf8')
const config = JSON.parse(original)
const previewAppId = Number(process.env.CODEX_WATCH_PREVIEW_APP_ID || 73521997)

if (!Number.isSafeInteger(previewAppId) || previewAppId <= 0 || previewAppId === config.app.appId) {
  throw new Error('Invalid preview appId')
}

config.app.appId = previewAppId
await writeFile(configPath, JSON.stringify(config, null, 2) + '\n')
console.log('[Codex Watch] Preview appId: ' + previewAppId)

let child
let exitCode = 1

try {
  child = spawn('zeus', ['preview', ...process.argv.slice(2)], {
    cwd: root,
    stdio: 'inherit'
  })
  exitCode = await new Promise((resolveExit, reject) => {
    child.once('error', reject)
    child.once('exit', (code, signal) => resolveExit(code ?? (signal ? 1 : 0)))
    for (const signal of ['SIGINT', 'SIGTERM']) {
      process.once(signal, () => {
        if (child && !child.killed) child.kill(signal)
      })
    }
  })
} catch (error) {
  console.error('[Codex Watch] Preview failed: ' + String(error))
} finally {
  await writeFile(configPath, original)
  console.log('[Codex Watch] Restored production appId: ' + config.app.appId)
}

process.exitCode = exitCode
