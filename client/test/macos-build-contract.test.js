import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

const here = path.dirname(fileURLToPath(import.meta.url))
const clientRoot = path.resolve(here, '..')

test('macOS distribution builds a verified bootstrap archive before packaging', () => {
  const pkg = JSON.parse(fs.readFileSync(path.join(clientRoot, 'package.json'), 'utf8'))
  assert.match(pkg.scripts['dist:mac:analyst'], /bootstrap:mac:analyst/)
  assert.equal(
    fs.existsSync(path.join(clientRoot, 'scripts', 'build_macos_bootstrap.sh')),
    true,
  )
})
