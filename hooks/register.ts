import type { EngineInterface, Register } from 'claude-code'

type $T = EngineInterface

// Formats the model gets as .md. No html/csv/json/xml/ipynb/images:
// those are source/image files the model reads (and edits) better on its own.
const SUPPORTED = ['.pdf', '.docx', '.pptx', '.xlsx', '.xls', '.msg', '.epub', '.zip', '.mp3', '.wav', '.m4a']
const PYTHONS = [['python'], ['py', '-3'], ['python3']]
// PDF page preview with graphics: the file is created only at the first Read.
const PREVIEW_RE = /[\\/]pages[\\/]page_\d+\.png$/i
const COMMANDS = [
  { name: 'markitdown-status', description: 'forced-markitdown: state, versions, cache' },
  { name: 'markitdown-on', description: 'forced-markitdown: enable automatic conversion' },
  { name: 'markitdown-off', description: 'forced-markitdown: disable automatic conversion' },
  { name: 'markitdown-convert', description: 'forced-markitdown: convert a file to .md now', argumentHint: '<path> [--force]' },
  { name: 'markitdown-clear', description: 'forced-markitdown: clear the cache of converted files' },
]

type Result = {
  ok: boolean
  error?: string
  source?: string
  md?: string
  folder?: string
  imagesDir?: string
  images?: number
  pages?: number[]
  previews?: number[]
  lines?: number
  cached?: boolean
  attachmentDir?: string
  [key: string]: unknown
}

let python: string[] | undefined
let state: boolean | undefined
let sessionConversions = 0
let dropDocuments = 0
const inFlight = new Map<string, Promise<Result>>()
// folder in cache -> source file (null = attachment pasted by the user)
const sources = new Map<string, string | null>()

const norm = (p: string) => p.replace(/\\/g, '/').replace(/\/+$/, '').toLowerCase()
const baseName = (p: string) => p.split(/[\\/]/).pop() ?? p
const extension = (p: string) => (/\.[^./\\]+$/.exec(p)?.[0] ?? '').toLowerCase()
const isSupported = (p: string) => SUPPORTED.includes(extension(p))

async function enabled($: $T) {
  state ??= (await $.store.get('enabled')) !== false
  return state
}

async function setState($: $T, value: boolean) {
  state = value
  await $.store.set('enabled', value)
  $.ui.status(value ? undefined : 'markitdown: off')
}

/** Runs the Python script; returns its JSON (last line of stdout). */
async function run($: $T, argv: string[], stdin?: string): Promise<Result> {
  const script = `${$.plugin.root}/scripts/convert.py`
  let error = 'no Python on PATH (python / py / python3)'
  for (const candidate of python ? [python] : PYTHONS) {
    try {
      const r = await $.process.run([...candidate, '-X', 'utf8', script, ...argv], { stdin, timeoutMs: 600_000 })
      const line = r.stdout.trim().split('\n').pop() ?? ''
      try {
        const result = JSON.parse(line) as Result
        python = candidate
        return result
      } catch {
        error = `${candidate.join(' ')}: ${(r.stderr || r.stdout).trim().split('\n').slice(-3).join(' | ').slice(0, 400)}`
      }
    } catch (err) {
      error = `${candidate.join(' ')}: ${String(err).slice(0, 200)}`
    }
  }
  return { ok: false, error }
}

function remember(w: Result, source: string | null) {
  if (w.folder) sources.set(norm(w.folder), source)
  if (w.attachmentDir) sources.set(norm(w.attachmentDir), null)
}

/** One Python call per file at a time (parallel Reads of the same file wait for it). */
function once($: $T, argv: string[]): Promise<Result> {
  const key = norm(argv.join('|'))
  let promise = inFlight.get(key)
  if (!promise) {
    promise = run($, argv).finally(() => inFlight.delete(key))
    inFlight.set(key, promise)
  }
  return promise
}

async function convert($: $T, path: string, force = false): Promise<Result> {
  const w = await once($, ['file', path, ...(force ? ['--force'] : [])])
  if (w.ok) {
    remember(w, w.source ?? path)
    if (!w.cached) {
      sessionConversions++
      $.ui.toast(`markitdown: ${baseName(path)} → .md${w.images ? ` (${w.images} img)` : ''}`)
    }
  } else {
    $.ui.toast(`markitdown: failed (${baseName(path)}): ${w.error}`, { timeoutMs: 8000 })
  }
  return w
}

function note(w: Result, original: string) {
  let text = `forced-markitdown: file ${original} was automatically converted to Markdown by markitdown (${w.md}).`
  if (w.images) {
    text += ` It contains ${w.images} images saved in ${w.imagesDir} and linked in the text - use Read on an image path to view it.`
  }
  if (w.pages?.length) text += ' PDF pages are marked with <!-- page N --> comments.'
  if (w.previews?.length) {
    text += ` Pages with graphics (charts, figures, tables - also vector ones not present in the text): ${w.previews.join(', ')}.` +
      ' Each has a marker <!-- page N: graphics ... Read `…/page_NNN.png` -->. When the question or text concerns a drawing,' +
      ' chart, diagram or table, Read that path: you get the page cropped to the graphic (generated on read).'
  }
  return text
}

/** Read(pages: "3-5") on a PDF -> offset/limit in the .md, based on the page markers. */
function pageRange(pages: string, w: Result): { offset?: number; limit?: number } {
  const m = /^\s*(\d+)\s*(?:-\s*(\d+))?\s*$/.exec(pages)
  const list = w.pages ?? []
  if (!m || !list.length) return {}
  const start = list[Number(m[1]) - 1]
  if (start === undefined) return {}
  const end = list[Number(m[2] ?? m[1])] ?? (w.lines ?? start) + 1
  return { offset: start, limit: Math.max(1, end - start) }
}

async function cacheRoot($: $T) {
  const home = (await $.env.get('USERPROFILE')) ?? (await $.env.get('HOME'))
  return home ? norm(`${home}/.claude/markitdown-cache`) : undefined
}

/** Is the file inside the cache folder? Returns its source (null = attachment) or undefined. */
async function sourceFor($: $T, path: string): Promise<string | null | undefined> {
  // Quick filter; the realPath check below decides anyway (a path with `..` won't pass).
  if (!norm(path).includes('markitdown-cache/') && !sources.size) return undefined
  const st = await $.fs.stat(path, { resolve: true }).catch(() => undefined)
  if (!st?.realPath) return undefined
  const real = norm(st.realPath)
  for (const [folder, source] of sources) {
    if (real.startsWith(folder + '/')) return source
  }
  // After a module reload / in a new session: source from the folder's meta.json in the cache dir.
  const root = await cacheRoot($)
  if (!root || !real.startsWith(root + '/')) return undefined
  const end = real.indexOf('/', root.length + 1)
  if (end < 0) return undefined
  const folder = real.slice(0, end)
  const meta = await $.fs.read(`${folder}/meta.json`).then(t => JSON.parse(t) as Result).catch(() => undefined)
  if (typeof meta?.source !== 'string') return undefined
  const source = norm(meta.source).startsWith(`${root}/attachments/`) ? null : meta.source
  sources.set(folder, source)
  return source
}

async function readMedia($: $T, id: string): Promise<string | undefined> {
  let b64 = ''
  let offset: number | undefined = 0
  while (offset !== undefined) {
    const chunk = await $.prompt.media({ id, offset })
    if (chunk.base64 === undefined) return undefined
    b64 += chunk.base64
    offset = chunk.next
  }
  return b64
}

const mb = (b: number) => `${(b / 1024 / 1024).toFixed(1)} MB`

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    for (const command of COMMANDS) await $.command.register(command)
    if (!(await enabled($))) $.ui.status('markitdown: off')
    return next(e)
  })

  // Model reads a file -> gets .md. Page preview with graphics -> rendered at the first read.
  on('tool.call', { tool: 'Read' }, async ($, e, next) => {
    if (PREVIEW_RE.test(e.file_path)) {
      if (!(await $.fs.exists(e.file_path))) {
        const w = await once($, ['preview', e.file_path])
        if (!w.ok) $.ui.toast(`markitdown: page preview failed: ${w.error}`, { timeoutMs: 8000 })
      }
      return next(e)
    }
    if (!isSupported(e.file_path) || !(await enabled($))) return next(e)
    const w = await convert($, e.file_path)
    if (!w.ok || !w.md) return next(e)
    const { pages, ...rest } = e
    const result = await next({ ...rest, file_path: w.md, ...(pages ? pageRange(pages, w) : {}) })
    if (result.deny !== undefined || result.isError) return result
    return { ...result, context: [...(result.context ?? []), note(w, e.file_path)] }
  })

  // Permissions: reading from the cache is judged like reading the original (no bypassing rules).
  on('tool.check', { tool: 'Read' }, async ($, e, next) => {
    const path = (e.input as { file_path?: unknown } | undefined)?.file_path
    if (typeof path !== 'string') return next(e)
    const source = await sourceFor($, path)
    if (source === undefined) return next(e)
    if (source === null) return { decision: 'allow', reason: 'forced-markitdown: user attachment' }
    return $.tool.check({ tool: 'Read', input: { file_path: source } })
  })

  // @file.pdf in the prompt -> attaches the .md.
  on('prompt.mention', async ($, e, next) => {
    if (!isSupported(e.path) || !(await enabled($))) return next(e)
    const w = await convert($, e.path)
    if (!w.ok || !w.md) return next(e)
    const result = await next({ ...e, path: w.md })
    if (result.deny !== undefined) return result
    return { ...result, context: [...(result.context ?? []), note(w, e.path)] }
  })

  // Pasted/dropped document (document block) -> .md content in the context.
  on('prompt.submit', async ($, e, next) => {
    const documents = (e.attachments ?? []).filter(
      a => a.type === 'document' && a.id && (a.mediaType === 'application/pdf' || isSupported(a.filename ?? '')),
    )
    if (!documents.length || !(await enabled($))) return next(e)
    const contexts: string[] = []
    let all = true
    for (const [i, a] of documents.entries()) {
      const file = a.filename ?? `attachment_${i + 1}.pdf`
      const b64 = await readMedia($, a.id!)
      const w = b64 ? await run($, ['stdin', file], b64) : { ok: false, error: 'cannot read attachment' }
      if (!w.ok || !w.md) {
        all = false
        $.ui.toast(`markitdown: failed (${file}): ${w.error}`, { timeoutMs: 8000 })
        continue
      }
      remember(w, null)
      sessionConversions++
      $.ui.toast(`markitdown: ${file} → .md${w.images ? ` (${w.images} img)` : ''}`)
      contexts.push(`${note(w, file)}\n\nContent (Markdown):\n\n${await $.fs.read(w.md)}`)
    }
    // The original document block is removed only when everything converted.
    if (all) dropDocuments = documents.length
    try {
      return await next(contexts.length ? { ...e, context: [...(e.context ?? []), ...contexts] } : e)
    } finally {
      dropDocuments = 0
    }
  })

  on('session.append', { door: 'prompt' }, ($, e, next) => {
    if (!dropDocuments) return next(e)
    let count = dropDocuments
    dropDocuments = 0
    const content = e.message.content.filter(b => !(b.type === 'document' && count-- > 0))
    return next({ ...e, message: { ...e.message, content } })
  })

  on('command.run', { command: 'markitdown-status' }, async $ => {
    const i = await run($, ['info'])
    const lines = [
      `forced-markitdown: ${(await enabled($)) ? 'ON' : 'OFF'}`,
      i.ok
        ? `Python: ${i.python} (${i.pythonVersion}) | markitdown ${i.markitdown}`
        : `Python/markitdown: ERROR - ${i.error}`,
      i.ok ? `Cache: ${i.cache} (${i.mdFiles} .md files, ${mb(Number(i.bytes))})` : '',
      `Conversions in this session: ${sessionConversions}`,
      `Supported: ${SUPPORTED.join(' ')}`,
    ]
    return { text: lines.filter(Boolean).join('\n') }
  })

  on('command.run', { command: 'markitdown-on' }, async $ => {
    await setState($, true)
    return { text: 'forced-markitdown enabled: PDF/DOCX/PPTX/XLSX... reach the model as .md.' }
  })

  on('command.run', { command: 'markitdown-off' }, async $ => {
    await setState($, false)
    return { text: 'forced-markitdown OFF: files reach the model in their original form.' }
  })

  on('command.run', { command: 'markitdown-convert' }, async ($, e) => {
    const force = /(^|\s)--force(\s|$)/.test(e.args)
    const path = e.args.replace(/(^|\s)--force(\s|$)/, ' ').trim().replace(/^["']|["']$/g, '')
    if (!path) return { text: 'Usage: /markitdown-convert <path> [--force]' }
    const w = await convert($, path, force)
    if (!w.ok) return { text: `Conversion error ${path}: ${w.error}` }
    return {
      text: `${note(w, w.source ?? path)}${w.cached ? ' (from cache)' : ''}`,
    }
  })

  on('command.run', { command: 'markitdown-clear' }, async $ => {
    const w = await run($, ['clear'])
    sources.clear()
    return {
      text: w.ok
        ? `Cache cleared: ${w.cache} (${w.mdFiles} .md files, ${mb(Number(w.bytes))}).`
        : `Error: ${w.error}`,
    }
  })
}
