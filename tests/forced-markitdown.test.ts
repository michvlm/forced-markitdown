import { expect, test } from 'claude-code/testing'
import type { On } from 'claude-code'

// Hooki testu stoją za silnikiem: process.run udaje skrypt Pythona.
const ZRODLO = 'C:\\proj\\raport.pdf'
const FOLDER = 'C:\\cache\\raport.pdf-abc'
const MD = 'C:\\cache\\raport.pdf-abc\\raport.md'
const WYNIK_PY = {
  ok: true, zrodlo: ZRODLO, md: MD, folder: FOLDER, obrazyDir: `${FOLDER}\\obrazy`,
  obrazy: 3, strony: [3, 20, 40], linie: 60, zCache: false,
}

function silnik(on: On, argvy: string[][]) {
  on('process.run', (_, e) => {
    argvy.push([...e.argv])
    return {
      value: {
        exitCode: 0, stdout: `ostrzezenie\n${JSON.stringify(WYNIK_PY)}\n`, stderr: '',
        isStdoutTruncated: false, isStderrTruncated: false,
      },
    }
  })
  on('store.get', () => ({ value: undefined }))
  on('store.set', () => ({ value: undefined }))
  on('fs.stat', (_, e) => ({ value: { kind: 'file', size: 1, mtimeMs: 0, isLink: false, realPath: e.path } }))
}

test('Read na PDF czyta .md, pages -> offset/limit, notka w context', async ($, on) => {
  const argvy: string[][] = []
  silnik(on, argvy)
  let widziane: Record<string, unknown> = {}
  on('tool.call', { tool: 'Read' }, (_, e) => {
    widziane = { ...e }
    return { result: {} }
  })

  const wynik = await $.tool.call({ tool: 'Read', file_path: ZRODLO, pages: '2-3' })

  expect(widziane.file_path).toBe(MD)
  expect(widziane.pages).toBeUndefined()
  expect(widziane.offset).toBe(20)
  expect(widziane.limit).toBe(41) // strona 4 nie istnieje -> do końca (linie + 1)
  expect(wynik.context?.[0]).toContain('3 obrazów')
  expect(argvy[0]).toContain('plik')
  expect(argvy[0]).toContain(ZRODLO)
})

test('nieobsługiwany plik przechodzi bez konwersji', async ($, on) => {
  const argvy: string[][] = []
  silnik(on, argvy)
  let widziane = ''
  on('tool.call', { tool: 'Read' }, (_, e) => {
    widziane = e.file_path
    return { result: {} }
  })

  await $.tool.call({ tool: 'Read', file_path: 'C:\\proj\\main.ts' })

  expect(widziane).toBe('C:\\proj\\main.ts')
  expect(argvy.length).toBe(0)
})

test('@wzmianka PDF załącza .md', async ($, on) => {
  silnik(on, [])
  let sciezka = ''
  on('prompt.mention', (_, e) => {
    sciezka = e.path
    return { type: 'file' }
  })

  const wynik = await $.prompt.mention({ mention: 'raport.pdf', path: ZRODLO })

  expect(sciezka).toBe(MD)
  expect(wynik.deny).toBeUndefined()
  expect(wynik.context?.[0]).toContain('forced-markitdown')
})

test('odczyt z cache oceniany jak odczyt oryginału', async ($, on) => {
  silnik(on, [])
  on('tool.call', { tool: 'Read' }, () => ({ result: {} }))
  on('tool.check', (_, e) => {
    const p = (e.input as { file_path: string }).file_path
    return { decision: p === ZRODLO ? 'allow' : 'ask' }
  })

  await $.tool.call({ tool: 'Read', file_path: ZRODLO }) // zapamiętuje folder -> źródło

  expect((await $.tool.check({ tool: 'Read', input: { file_path: MD } })).decision).toBe('allow')
  expect((await $.tool.check({ tool: 'Read', input: { file_path: `${FOLDER}\\obrazy\\obraz_001.png` } })).decision).toBe('allow')
  expect((await $.tool.check({ tool: 'Read', input: { file_path: 'C:\\inne\\x.md' } })).decision).toBe('ask')
})

test('Read podglądu strony: render przy pierwszym odczycie, potem zwykły Read', async ($, on) => {
  const argvy: string[][] = []
  silnik(on, argvy)
  let istnieje = false
  on('fs.exists', () => ({ value: istnieje }))
  const widziane: string[] = []
  on('tool.call', { tool: 'Read' }, (_, e) => {
    widziane.push(e.file_path)
    return { result: {} }
  })
  const png = `${FOLDER}\\strony\\strona_002.png`

  await $.tool.call({ tool: 'Read', file_path: png })
  istnieje = true
  await $.tool.call({ tool: 'Read', file_path: png })

  expect(widziane).toEqual([png, png])
  expect(argvy.length).toBe(1)
  expect(argvy[0]).toContain('podglad')
  expect(argvy[0]).toContain(png)
})

test('notka mówi modelowi o stronach z grafiką', async ($, on) => {
  on('process.run', () => ({
    value: {
      exitCode: 0, stdout: JSON.stringify({ ...WYNIK_PY, podglady: [2, 5] }), stderr: '',
      isStdoutTruncated: false, isStderrTruncated: false,
    },
  }))
  on('store.get', () => ({ value: undefined }))
  on('tool.call', { tool: 'Read' }, () => ({ result: {} }))

  const wynik = await $.tool.call({ tool: 'Read', file_path: ZRODLO })

  expect(wynik.context?.[0]).toContain('Strony z grafiką')
  expect(wynik.context?.[0]).toContain('2, 5')
})

test('uprawnienia z meta.json w cache po przeładowaniu (bez wcześniejszej konwersji)', async ($, on) => {
  const korzen = 'C:\\Users\\t\\.claude\\markitdown-cache'
  on('env.get', (_, e) => ({ value: e.name === 'USERPROFILE' ? 'C:\\Users\\t' : undefined }))
  on('fs.stat', (_, e) => ({ value: { kind: 'file', size: 1, mtimeMs: 0, isLink: false, realPath: e.path } }))
  on('fs.read', (_, e) => ({
    value: e.path.toLowerCase().includes('raport.pdf-abc') ? JSON.stringify({ zrodlo: ZRODLO }) : '{}',
  }))
  on('tool.check', (_, e) => {
    const p = (e.input as { file_path: string }).file_path
    return { decision: p === ZRODLO ? 'allow' : 'ask' }
  })

  const png = `${korzen}\\raport.pdf-abc\\strony\\strona_001.png`
  expect((await $.tool.check({ tool: 'Read', input: { file_path: png } })).decision).toBe('allow')
  expect((await $.tool.check({ tool: 'Read', input: { file_path: `${korzen}\\inny-x\\a.md` } })).decision).toBe('ask')
  expect((await $.tool.check({ tool: 'Read', input: { file_path: 'C:\\gdzies\\raport.pdf-abc\\a.md' } })).decision).toBe('ask')
})

test('/markitdown-off wyłącza podmianę, /markitdown-on włącza', async ($, on) => {
  silnik(on, [])
  on('command.register', (_, e) => ({ value: { command: e.name } }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  const widziane: string[] = []
  on('tool.call', { tool: 'Read' }, (_, e) => {
    widziane.push(e.file_path)
    return { result: {} }
  })

  await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })
  // origin i presentation stempluje kit, jak silnik w sesji.
  type Komenda = Parameters<typeof $.command.run>[0]
  const off = await $.command.run({ command: 'markitdown-off', args: '' } as Komenda)
  await $.tool.call({ tool: 'Read', file_path: ZRODLO })
  await $.command.run({ command: 'markitdown-on', args: '' } as Komenda)
  await $.tool.call({ tool: 'Read', file_path: ZRODLO })

  expect(off.text).toContain('wyłączony')
  expect(widziane).toEqual([ZRODLO, MD])
})
