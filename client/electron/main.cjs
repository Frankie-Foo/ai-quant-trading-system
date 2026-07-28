const { app, BrowserWindow } = require('electron')
const { spawn } = require('node:child_process')
const fs = require('node:fs')
const path = require('node:path')

let backend = null

const delay = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds))

async function waitUntilReady(url) {
  for (let attempt = 0; attempt < 60; attempt += 1) {
    try {
      const response = await fetch(`${url}/v1/health`)
      if (response.ok) return
    } catch {
      // The local Python process may still be starting.
    }
    await delay(250)
  }
  throw new Error('本地决策引擎没有在限定时间内启动')
}

function localRuntime() {
  const projectRoot = path.resolve(__dirname, '..', '..')
  const virtualPython = path.join(projectRoot, '.venv', 'Scripts', 'python.exe')
  const python = fs.existsSync(virtualPython) ? virtualPython : 'python'
  const port = process.env.ADAPTIVE_CLIENT_PORT || '8787'
  const url = `http://127.0.0.1:${port}`
  backend = spawn(
    python,
    [
      '-m',
      'scripts.serve_adaptive_client',
      '--host',
      '127.0.0.1',
      '--port',
      port,
      '--static-root',
      path.join(projectRoot, 'client', 'dist'),
    ],
    {
      cwd: projectRoot,
      windowsHide: true,
      stdio: 'ignore',
    },
  )
  return url
}

async function createWindow() {
  const remote = (process.env.ADAPTIVE_CLIENT_REMOTE_URL || '').trim()
  if (remote && !remote.startsWith('https://') && !remote.startsWith('http://127.0.0.1')) {
    throw new Error('远程客户端地址必须使用 HTTPS')
  }
  const url = remote || localRuntime()
  await waitUntilReady(url)
  const window = new BrowserWindow({
    width: 1440,
    height: 930,
    minWidth: 1120,
    minHeight: 720,
    backgroundColor: '#080c0e',
    autoHideMenuBar: true,
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  })
  await window.loadURL(url)
}

app.whenReady().then(createWindow).catch((error) => {
  console.error(error.message)
  app.quit()
})

app.on('window-all-closed', () => {
  if (backend && !backend.killed) backend.kill()
  app.quit()
})
