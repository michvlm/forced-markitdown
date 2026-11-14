import { expect, test } from 'claude-code/testing'
import type { On } from 'claude-code'

// Test hooks sit in front of the engine: process.run fakes the Python script.
const SOURCE = 'C:\\proj\\report.pdf'
const FOLDER = 'C:\\cache\\report.pdf-abc'
const MD = 'C:\\cache\\report.pdf-abc\\report.md'
const RESULT_PY = {
  ok: true, source: SOURCE, md: MD, folder: FOLDER, imagesDir: `${FOLDER}\\images`,
  images: 3, pages: [3, 20, 40], lines: 60, cached: false,
}

function engine(on: On, argvs: string[][]) {
  on('process.run', (_, e) => {
    argvs.push([...e.argv])
    return {
      value: {
        exitCode: 0, stdout: `warning\n${JSON.stringify(RESULT_PY)}\n`, stderr: '',
        isStdoutTruncated: false, isStderrTruncated: false,
      },
    }
  })
  on('store.get', () => ({ value: undefined }))
  on('store.set', () => ({ value: undefined }))
  on('fs.stat', (_, e) => ({ value: { kind: 'file', size: 1, mtimeMs: 0, isLink: false, realPath: e.path } }))
}

test('Read on PDF reads the .md, pages -> offset/limit, note in context', async ($, on) => {
  const argvs: string[][] = []
  engine(on, argvs)
  let seen: Record<string, unknown> = {}
  on('tool.call', { tool: 'Read' }, (_, e) => {
    seen = { ...e }
    return { result: {} }
  })

  const result = await $.tool.call({ tool: 'Read', file_path: SOURCE, pages: '2-3' })

  expect(seen.file_path).toBe(MD)
  expect(seen.pages).toBeUndefined()
  expect(seen.offset).toBe(20)
  expect(seen.limit).toBe(41) // page 4 does not exist -> to the end (lines + 1)
  expect(result.context?.[0]).toContain('3 images')
  expect(argvs[0]).toContain('file')
  expect(argvs[0]).toContain(SOURCE)
})

test('unsupported file passes through without conversion', async ($, on) => {
  const argvs: string[][] = []
  engine(on, argvs)
  let seen = ''
  on('tool.call', { tool: 'Read' }, (_, e) => {
    seen = e.file_path
    return { result: {} }
  })

  await $.tool.call({ tool: 'Read', file_path: 'C:\\proj\\main.ts' })

  expect(seen).toBe('C:\\proj\\main.ts')
  expect(argvs.length).toBe(0)
})

test('@mention of a PDF attaches the .md', async ($, on) => {
  engine(on, [])
  let path = ''
  on('prompt.mention', (_, e) => {
    path = e.path
    return { type: 'file' }
  })

  const result = await $.prompt.mention({ mention: 'report.pdf', path: SOURCE })

  expect(path).toBe(MD)
  expect(result.deny).toBeUndefined()
  expect(result.context?.[0]).toContain('forced-markitdown')
})

test('cache read is checked like the original read', async ($, on) => {
  engine(on, [])
  on('tool.call', { tool: 'Read' }, () => ({ result: {} }))
  on('tool.check', (_, e) => {
    const p = (e.input as { file_path: string }).file_path
    return { decision: p === SOURCE ? 'allow' : 'ask' }
  })

  await $.tool.call({ tool: 'Read', file_path: SOURCE }) // remembers folder -> source

  expect((await $.tool.check({ tool: 'Read', input: { file_path: MD } })).decision).toBe('allow')
  expect((await $.tool.check({ tool: 'Read', input: { file_path: `${FOLDER}\\images\\image_001.png` } })).decision).toBe('allow')
  expect((await $.tool.check({ tool: 'Read', input: { file_path: 'C:\\other\\x.md' } })).decision).toBe('ask')
})

test('Read of a page preview: render on first read, then plain Read', async ($, on) => {
  const argvs: string[][] = []
  engine(on, argvs)
  let exists = false
  on('fs.exists', () => ({ value: exists }))
  const seen: string[] = []
  on('tool.call', { tool: 'Read' }, (_, e) => {
    seen.push(e.file_path)
    return { result: {} }
  })
  const png = `${FOLDER}\\pages\\page_002.png`

  await $.tool.call({ tool: 'Read', file_path: png })
  exists = true
  await $.tool.call({ tool: 'Read', file_path: png })

  expect(seen).toEqual([png, png])
  expect(argvs.length).toBe(1)
  expect(argvs[0]).toContain('preview')
  expect(argvs[0]).toContain(png)
})

test('note tells the model about pages with graphics', async ($, on) => {
  on('process.run', () => ({
    value: {
      exitCode: 0, stdout: JSON.stringify({ ...RESULT_PY, previews: [2, 5] }), stderr: '',
      isStdoutTruncated: false, isStderrTruncated: false,
    },
  }))
  on('store.get', () => ({ value: undefined }))
  on('tool.call', { tool: 'Read' }, () => ({ result: {} }))

  const result = await $.tool.call({ tool: 'Read', file_path: SOURCE })

  expect(result.context?.[0]).toContain('Pages with graphics')
  expect(result.context?.[0]).toContain('2, 5')
})

test('permissions from meta.json in the cache after reload (without earlier conversion)', async ($, on) => {
  const root = 'C:\\Users\\t\\.claude\\markitdown-cache'
  on('env.get', (_, e) => ({ value: e.name === 'USERPROFILE' ? 'C:\\Users\\t' : undefined }))
  on('fs.stat', (_, e) => ({ value: { kind: 'file', size: 1, mtimeMs: 0, isLink: false, realPath: e.path } }))
  on('fs.read', (_, e) => ({
    value: e.path.toLowerCase().includes('report.pdf-abc') ? JSON.stringify({ source: SOURCE }) : '{}',
  }))
  on('tool.check', (_, e) => {
    const p = (e.input as { file_path: string }).file_path
    return { decision: p === SOURCE ? 'allow' : 'ask' }
  })

  const png = `${root}\\report.pdf-abc\\pages\\page_001.png`
  expect((await $.tool.check({ tool: 'Read', input: { file_path: png } })).decision).toBe('allow')
  expect((await $.tool.check({ tool: 'Read', input: { file_path: `${root}\\other-x\\a.md` } })).decision).toBe('ask')
  expect((await $.tool.check({ tool: 'Read', input: { file_path: 'C:\\elsewhere\\report.pdf-abc\\a.md' } })).decision).toBe('ask')
})

test('/markitdown-off disables replacement, /markitdown-on enables it', async ($, on) => {
  engine(on, [])
  on('command.register', (_, e) => ({ value: { command: e.name } }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  const seen: string[] = []
  on('tool.call', { tool: 'Read' }, (_, e) => {
    seen.push(e.file_path)
    return { result: {} }
  })

  await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })
  // origin and presentation are stamped by the kit, like the engine does in a session.
  type Command = Parameters<typeof $.command.run>[0]
  const off = await $.command.run({ command: 'markitdown-off', args: '' } as Command)
  await $.tool.call({ tool: 'Read', file_path: SOURCE })
  await $.command.run({ command: 'markitdown-on', args: '' } as Command)
  await $.tool.call({ tool: 'Read', file_path: SOURCE })

  expect(off.text).toContain('OFF')
  expect(seen).toEqual([SOURCE, MD])
})
