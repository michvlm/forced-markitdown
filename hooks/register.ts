import type { EngineInterface, Register } from 'claude-code'

type $T = EngineInterface

// Formaty, które model dostaje jako .md. Bez html/csv/json/xml/ipynb/obrazów:
// to pliki źródłowe/obrazy, które model czyta (i edytuje) lepiej sam.
const OBSLUGIWANE = ['.pdf', '.docx', '.pptx', '.xlsx', '.xls', '.msg', '.epub', '.zip', '.mp3', '.wav', '.m4a']
const PYTHONY = [['python'], ['py', '-3'], ['python3']]
// Podgląd strony PDF z grafiką: plik powstaje dopiero przy pierwszym Read.
const PODGLAD = /[\\/]strony[\\/]strona_\d+\.png$/i
const KOMENDY = [
  { name: 'markitdown-status', description: 'forced-markitdown: stan, wersje, cache' },
  { name: 'markitdown-on', description: 'forced-markitdown: włącz automatyczną konwersję' },
  { name: 'markitdown-off', description: 'forced-markitdown: wyłącz automatyczną konwersję' },
  { name: 'markitdown-convert', description: 'forced-markitdown: skonwertuj plik do .md teraz', argumentHint: '<ścieżka> [--wymus]' },
  { name: 'markitdown-clear', description: 'forced-markitdown: wyczyść cache skonwertowanych plików' },
]

type Wynik = {
  ok: boolean
  blad?: string
  zrodlo?: string
  md?: string
  folder?: string
  obrazyDir?: string
  obrazy?: number
  strony?: number[]
  podglady?: number[]
  linie?: number
  zCache?: boolean
  katalogZalacznika?: string
  [klucz: string]: unknown
}

let python: string[] | undefined
let stan: boolean | undefined
let konwersjeSesji = 0
let usunDokumenty = 0
const wTrakcie = new Map<string, Promise<Wynik>>()
// folder w cache -> plik źródłowy (null = załącznik wklejony przez użytkownika)
const zrodla = new Map<string, string | null>()

const norm = (p: string) => p.replace(/\\/g, '/').replace(/\/+$/, '').toLowerCase()
const nazwa = (p: string) => p.split(/[\\/]/).pop() ?? p
const rozszerzenie = (p: string) => (/\.[^./\\]+$/.exec(p)?.[0] ?? '').toLowerCase()
const obslugiwany = (p: string) => OBSLUGIWANE.includes(rozszerzenie(p))

async function wlaczony($: $T) {
  stan ??= (await $.store.get('wlaczony')) !== false
  return stan
}

async function ustawStan($: $T, wartosc: boolean) {
  stan = wartosc
  await $.store.set('wlaczony', wartosc)
  $.ui.status(wartosc ? undefined : 'markitdown: wył.')
}

/** Odpala skrypt Pythona; zwraca jego JSON (ostatnia linia stdout). */
async function uruchom($: $T, argv: string[], stdin?: string): Promise<Wynik> {
  const skrypt = `${$.plugin.root}/scripts/konwertuj.py`
  let blad = 'brak Pythona na PATH (python / py / python3)'
  for (const kandydat of python ? [python] : PYTHONY) {
    try {
      const r = await $.process.run([...kandydat, '-X', 'utf8', skrypt, ...argv], { stdin, timeoutMs: 600_000 })
      const linia = r.stdout.trim().split('\n').pop() ?? ''
      try {
        const wynik = JSON.parse(linia) as Wynik
        python = kandydat
        return wynik
      } catch {
        blad = `${kandydat.join(' ')}: ${(r.stderr || r.stdout).trim().split('\n').slice(-3).join(' | ').slice(0, 400)}`
      }
    } catch (err) {
      blad = `${kandydat.join(' ')}: ${String(err).slice(0, 200)}`
    }
  }
  return { ok: false, blad }
}

function zapamietaj(w: Wynik, zrodlo: string | null) {
  if (w.folder) zrodla.set(norm(w.folder), zrodlo)
  if (w.katalogZalacznika) zrodla.set(norm(w.katalogZalacznika), null)
}

/** Jedno wywołanie Pythona na plik naraz (równoległe Read tego samego pliku czekają na nie). */
function raz($: $T, argv: string[]): Promise<Wynik> {
  const klucz = norm(argv.join('|'))
  let obietnica = wTrakcie.get(klucz)
  if (!obietnica) {
    obietnica = uruchom($, argv).finally(() => wTrakcie.delete(klucz))
    wTrakcie.set(klucz, obietnica)
  }
  return obietnica
}

async function konwertuj($: $T, sciezka: string, wymus = false): Promise<Wynik> {
  const w = await raz($, ['plik', sciezka, ...(wymus ? ['--wymus'] : [])])
  if (w.ok) {
    zapamietaj(w, w.zrodlo ?? sciezka)
    if (!w.zCache) {
      konwersjeSesji++
      $.ui.toast(`markitdown: ${nazwa(sciezka)} → .md${w.obrazy ? ` (${w.obrazy} obr.)` : ''}`)
    }
  } else {
    $.ui.toast(`markitdown: nie udało się (${nazwa(sciezka)}): ${w.blad}`, { timeoutMs: 8000 })
  }
  return w
}

function notka(w: Wynik, oryginal: string) {
  let tekst = `forced-markitdown: plik ${oryginal} został automatycznie skonwertowany do Markdown przez markitdown (${w.md}).`
  if (w.obrazy) {
    tekst += ` Zawiera ${w.obrazy} obrazów zapisanych w ${w.obrazyDir} i podlinkowanych w treści - użyj Read na ścieżce obrazu, by go zobaczyć.`
  }
  if (w.strony?.length) tekst += ' Strony PDF są oznaczone komentarzami <!-- strona N -->.'
  if (w.podglady?.length) {
    tekst += ` Strony z grafiką (wykresy, rysunki, tabele - także wektorowe, których nie ma w tekście): ${w.podglady.join(', ')}.` +
      ' Każda ma znacznik <!-- strona N: grafika ... Read `…/strona_NNN.png` -->. Gdy pytanie lub tekst dotyczy rysunku,' +
      ' wykresu, schematu albo tabeli, zrób Read na tej ścieżce: dostaniesz stronę przyciętą do grafiki (generowaną przy odczycie).'
  }
  return tekst
}

/** Read(pages: "3-5") na PDF -> offset/limit w .md po znacznikach stron. */
function zakresStron(pages: string, w: Wynik): { offset?: number; limit?: number } {
  const m = /^\s*(\d+)\s*(?:-\s*(\d+))?\s*$/.exec(pages)
  const strony = w.strony ?? []
  if (!m || !strony.length) return {}
  const start = strony[Number(m[1]) - 1]
  if (start === undefined) return {}
  const koniec = strony[Number(m[2] ?? m[1])] ?? (w.linie ?? start) + 1
  return { offset: start, limit: Math.max(1, koniec - start) }
}

async function korzenCache($: $T) {
  const dom = (await $.env.get('USERPROFILE')) ?? (await $.env.get('HOME'))
  return dom ? norm(`${dom}/.claude/markitdown-cache`) : undefined
}

/** Plik leży w folderze cache? Zwraca źródło (null = załącznik) albo undefined. */
async function zrodloDla($: $T, sciezka: string): Promise<string | null | undefined> {
  // Szybki filtr; decyduje i tak realPath niżej (ścieżka z `..` nie przejdzie).
  if (!norm(sciezka).includes('markitdown-cache/') && !zrodla.size) return undefined
  const st = await $.fs.stat(sciezka, { resolve: true }).catch(() => undefined)
  if (!st?.realPath) return undefined
  const prawdziwa = norm(st.realPath)
  for (const [folder, zrodlo] of zrodla) {
    if (prawdziwa.startsWith(folder + '/')) return zrodlo
  }
  // Po przeładowaniu modułu / w nowej sesji: źródło z meta.json folderu w katalogu cache.
  const korzen = await korzenCache($)
  if (!korzen || !prawdziwa.startsWith(korzen + '/')) return undefined
  const koniec = prawdziwa.indexOf('/', korzen.length + 1)
  if (koniec < 0) return undefined
  const folder = prawdziwa.slice(0, koniec)
  const meta = await $.fs.read(`${folder}/meta.json`).then(t => JSON.parse(t) as Wynik).catch(() => undefined)
  if (typeof meta?.zrodlo !== 'string') return undefined
  const zrodlo = norm(meta.zrodlo).startsWith(`${korzen}/zalaczniki/`) ? null : meta.zrodlo
  zrodla.set(folder, zrodlo)
  return zrodlo
}

async function czytajMedia($: $T, id: string): Promise<string | undefined> {
  let b64 = ''
  let offset: number | undefined = 0
  while (offset !== undefined) {
    const kawalek = await $.prompt.media({ id, offset })
    if (kawalek.base64 === undefined) return undefined
    b64 += kawalek.base64
    offset = kawalek.next
  }
  return b64
}

const mb = (b: number) => `${(b / 1024 / 1024).toFixed(1)} MB`

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    for (const komenda of KOMENDY) await $.command.register(komenda)
    if (!(await wlaczony($))) $.ui.status('markitdown: wył.')
    return next(e)
  })

  // Model czyta plik -> dostaje .md. Podgląd strony z grafiką -> render przy pierwszym odczycie.
  on('tool.call', { tool: 'Read' }, async ($, e, next) => {
    if (PODGLAD.test(e.file_path)) {
      if (!(await $.fs.exists(e.file_path))) {
        const w = await raz($, ['podglad', e.file_path])
        if (!w.ok) $.ui.toast(`markitdown: podgląd strony się nie udał: ${w.blad}`, { timeoutMs: 8000 })
      }
      return next(e)
    }
    if (!obslugiwany(e.file_path) || !(await wlaczony($))) return next(e)
    const w = await konwertuj($, e.file_path)
    if (!w.ok || !w.md) return next(e)
    const { pages, ...reszta } = e
    const wynik = await next({ ...reszta, file_path: w.md, ...(pages ? zakresStron(pages, w) : {}) })
    if (wynik.deny !== undefined || wynik.isError) return wynik
    return { ...wynik, context: [...(wynik.context ?? []), notka(w, e.file_path)] }
  })

  // Uprawnienia: odczyt z cache oceniany jak odczyt oryginału (bez obchodzenia reguł).
  on('tool.check', { tool: 'Read' }, async ($, e, next) => {
    const sciezka = (e.input as { file_path?: unknown } | undefined)?.file_path
    if (typeof sciezka !== 'string') return next(e)
    const zrodlo = await zrodloDla($, sciezka)
    if (zrodlo === undefined) return next(e)
    if (zrodlo === null) return { decision: 'allow', reason: 'forced-markitdown: załącznik użytkownika' }
    return $.tool.check({ tool: 'Read', input: { file_path: zrodlo } })
  })

  // @plik.pdf w promptcie -> załącza .md.
  on('prompt.mention', async ($, e, next) => {
    if (!obslugiwany(e.path) || !(await wlaczony($))) return next(e)
    const w = await konwertuj($, e.path)
    if (!w.ok || !w.md) return next(e)
    const wynik = await next({ ...e, path: w.md })
    if (wynik.deny !== undefined) return wynik
    return { ...wynik, context: [...(wynik.context ?? []), notka(w, e.path)] }
  })

  // Wklejony/upuszczony dokument (blok document) -> treść .md w kontekście.
  on('prompt.submit', async ($, e, next) => {
    const dokumenty = (e.attachments ?? []).filter(
      a => a.type === 'document' && a.id && (a.mediaType === 'application/pdf' || obslugiwany(a.filename ?? '')),
    )
    if (!dokumenty.length || !(await wlaczony($))) return next(e)
    const konteksty: string[] = []
    let wszystkie = true
    for (const [i, a] of dokumenty.entries()) {
      const plik = a.filename ?? `zalacznik_${i + 1}.pdf`
      const b64 = await czytajMedia($, a.id!)
      const w = b64 ? await uruchom($, ['stdin', plik], b64) : { ok: false, blad: 'nie można odczytać załącznika' }
      if (!w.ok || !w.md) {
        wszystkie = false
        $.ui.toast(`markitdown: nie udało się (${plik}): ${w.blad}`, { timeoutMs: 8000 })
        continue
      }
      zapamietaj(w, null)
      konwersjeSesji++
      $.ui.toast(`markitdown: ${plik} → .md${w.obrazy ? ` (${w.obrazy} obr.)` : ''}`)
      konteksty.push(`${notka(w, plik)}\n\nTreść (Markdown):\n\n${await $.fs.read(w.md)}`)
    }
    // Oryginalny blok document usuwamy tylko gdy wszystko się skonwertowało.
    if (wszystkie) usunDokumenty = dokumenty.length
    try {
      return await next(konteksty.length ? { ...e, context: [...(e.context ?? []), ...konteksty] } : e)
    } finally {
      usunDokumenty = 0
    }
  })

  on('session.append', { door: 'prompt' }, ($, e, next) => {
    if (!usunDokumenty) return next(e)
    let ile = usunDokumenty
    usunDokumenty = 0
    const content = e.message.content.filter(b => !(b.type === 'document' && ile-- > 0))
    return next({ ...e, message: { ...e.message, content } })
  })

  on('command.run', { command: 'markitdown-status' }, async $ => {
    const i = await uruchom($, ['info'])
    const linie = [
      `forced-markitdown: ${(await wlaczony($)) ? 'WŁĄCZONY' : 'WYŁĄCZONY'}`,
      i.ok
        ? `Python: ${i.python} (${i.wersjaPython}) | markitdown ${i.markitdown}`
        : `Python/markitdown: BŁĄD - ${i.blad}`,
      i.ok ? `Cache: ${i.cache} (${i.plikiMd} plików .md, ${mb(Number(i.bajty))})` : '',
      `Konwersje w tej sesji: ${konwersjeSesji}`,
      `Obsługiwane: ${OBSLUGIWANE.join(' ')}`,
    ]
    return { text: linie.filter(Boolean).join('\n') }
  })

  on('command.run', { command: 'markitdown-on' }, async $ => {
    await ustawStan($, true)
    return { text: 'forced-markitdown włączony: PDF/DOCX/PPTX/XLSX... trafią do modelu jako .md.' }
  })

  on('command.run', { command: 'markitdown-off' }, async $ => {
    await ustawStan($, false)
    return { text: 'forced-markitdown wyłączony: pliki trafiają do modelu w oryginale.' }
  })

  on('command.run', { command: 'markitdown-convert' }, async ($, e) => {
    const wymus = /(^|\s)--wymus(\s|$)/.test(e.args)
    const sciezka = e.args.replace(/(^|\s)--wymus(\s|$)/, ' ').trim().replace(/^["']|["']$/g, '')
    if (!sciezka) return { text: 'Użycie: /markitdown-convert <ścieżka> [--wymus]' }
    const w = await konwertuj($, sciezka, wymus)
    if (!w.ok) return { text: `Błąd konwersji ${sciezka}: ${w.blad}` }
    return {
      text: `${notka(w, w.zrodlo ?? sciezka)}${w.zCache ? ' (z cache)' : ''}`,
    }
  })

  on('command.run', { command: 'markitdown-clear' }, async $ => {
    const w = await uruchom($, ['czysc'])
    zrodla.clear()
    return {
      text: w.ok
        ? `Cache wyczyszczony: ${w.cache} (${w.plikiMd} plików .md, ${mb(Number(w.bajty))}).`
        : `Błąd: ${w.blad}`,
    }
  })
}
