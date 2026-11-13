# forced-markitdown - jak to działa

## Pliki

| Plik | Rola |
| --- | --- |
| `.claude-plugin/plugin.json` | Manifest pluginu |
| `.claude-plugin/marketplace.json` | Lokalny marketplace (source `./`), żeby dało się zainstalować z folderu |
| `hooks/hooks.json` | Wskazuje moduł hooków |
| `hooks/register.ts` | Cała logika po stronie Claude Code (hooki, komendy) |
| `scripts/konwertuj.py` | Konwersja przez markitdown, obrazy, cache. Zwraca JSON w ostatniej linii stdout |
| `tests/forced-markitdown.test.ts` | Testy hooków (`claude plugin test .`), Python udawany przez hook `process.run` |
| `tsconfig.json` | Do type-checku (`tsc -p .`, gdy silnik położy typy w `.claude-plugin/types`) |

## Przepływ

1. **`tool.call` (Read)** - plik z obsługiwanym rozszerzeniem -> `konwertuj()` -> `next({...e, file_path: md})`. `pages` jest usuwane i (dla PDF ze znacznikami) zamieniane na `offset`/`limit`. Do wyniku dochodzi `context` z notką (gdzie obrazy, jak je otworzyć). Błąd konwersji -> `next(e)` bez zmian + toast.
2. **`prompt.mention`** - `@plik.pdf` -> `next({...e, path: md})` + notka w `context`.
3. **`prompt.submit`** - załącznik typu `document` (PDF wklejony do prompta): bajty przez `$.prompt.media` (kawałkami po 4 MiB, base64 sklejany) -> Python przez stdin -> treść `.md` w `context`. Flaga `usunDokumenty` mówi hookowi `session.append` (door `prompt`), ile bloków `document` wyrzucić z wiersza prompta. Usuwamy tylko, gdy wszystkie konwersje się udały.
4. **`tool.check` (Read)** - ścieżka w folderze cache -> werdykt jak dla `Read` oryginału (`$.tool.check` na źródle). Źródło z mapy sesji, a po przeładowaniu albo w nowej sesji z `meta.json` folderu (tylko pod `%USERPROFILE%\.claude\markitdown-cache`). Załączniki (`null` w mapie, źródło w `zalaczniki/`) -> `allow`. Ścieżka sprawdzana po `realPath`, więc `..` nie wyprowadzi poza folder.
5. **Podgląd strony z grafiką (leniwy)** - `Read` na `…/strony/strona_NNN.png`, którego nie ma -> Python `podglad` renderuje stronę -> `next(e)` czyta gotowy plik. Model sam decyduje, kiedy go otworzyć (znacznik w `.md` + notka w `context`).
6. **Komendy** - rejestrowane w `session.start`, obsługiwane przez `command.run`. Stan wł./wył. w `$.store` (`wlaczony`), czyli między sesjami.

## Trudne rozwiązania

- **Import markitdown trwa ~4 s** (magika/onnxruntime). Skrypt sprawdza cache (`meta.json`: `mtime_ns`, rozmiar, `WERSJA_CACHE`) *przed* importem. Zmiana formatu wyjścia = podbij `WERSJA_CACHE`.
- **Równoległe odczyty tego samego pliku** - `wTrakcie: Map<ścieżka, Promise>` w TS, więc jeden plik konwertuje jeden proces.
- **Obrazy DOCX/PPTX** - `convert(..., keep_data_uris=True)` daje base64 w Markdown. Regex `DATA_URI` wycina je do `obrazy/obraz_NNN.ext` (dedup po SHA-1 treści) i wstawia ścieżkę bezwzględną (z `<...>`, gdy jest spacja).
- **Obrazy PDF** - markitdown ich nie wyciąga. `pypdfium2` (zależność pdfplumber): `page.get_objects(FPDF_PAGEOBJ_IMAGE)` -> `PdfImage.extract()` (JPEG bez rekompresji, reszta PNG). Pomijane < 48 px. Limit 300 obrazów na plik.
- **Strony PDF** - pdfminer rozdziela strony znakiem `\f`; jeśli jest, tekst dzielimy i wstawiamy `<!-- strona N -->` + obrazy danej strony. Numery linii znaczników idą do `strony` w JSON i służą do mapowania `pages`. Ścieżka pdfplumber (tabele/formularze) nie ma `\f` -> obrazy w sekcji na końcu.
- **Skany** - strona bez tekstu i bez obrazów -> `page.render(scale=2)` do PNG.
- **Wykrywanie grafiki (przy konwersji, bez renderu)** - `obiektyStrony()` bierze obiekty poziomu 0 z ramkami (granice obiektów w Form XObject pdfium podaje w układzie formularza, więc liczy je tylko jako zawartość rodzica). `powtarzalne()`: ten sam typ i ta sama ramka (zaokrąglona do 1 pt) na ≥30% stron (min. 2) = logo/stopka. `analizaStrony()` pomija tło (>90% strony), pasy 7% u góry/dołu i powtarzalne. Strona ma grafikę przy ≥1 obrazie ≥48 px albo ≥8 ścieżkach (`MIN_SCIEZEK`). Powtarzalne obrazy (logo) nie są też linkowane przy każdej stronie.
- **Kadr podglądu** - ramka grafiki + zapas 4% (podpisy osi). Zapas nie wchodzi na wykluczone elementy leżące w całości po zewnętrznej stronie grafiki (logo nad, stopka pod). Render tylko kadru (`render(crop=…)`) w skali dającej ~1500 px dłuższego boku (1,5-4x). Potem `cofnijKrawedzie()`: krawędź przecinająca treść (pół linii tekstu) cofa się do pierwszej pustej linii, najwyżej o zapas. Na koniec cięcie bieli. Strona obrócona (`/Rotate`) -> tylko cięcie bieli, bo współrzędne kadru nie są pewne.
- **Bezpieczeństwo podglądu** - Python renderuje tylko pod katalogiem cache, a źródło bierze z `meta.json`, nie ze ścieżki od modelu.
- **Uprawnienia** - cache leży poza projektem, więc zwykły `Read` z cache pytałby o zgodę albo, gdyby dać `allow`, obchodził reguły. Mapa `folder cache -> źródło` + `$.tool.check` na źródle daje dokładnie te same decyzje co dla oryginału.
- **Stdout Pythona** - biblioteki potrafią coś wypisać, więc konwersja idzie w `redirect_stdout(stderr)`, a TS bierze ostatnią linię stdout. `-X utf8` + `ensure_ascii=True`, żeby polskie znaki nie psuły JSON na Windows.
- **Python** - próbowane kolejno `python`, `py -3`, `python3`; pierwszy działający zapamiętany do przeładowania modułu. Bez `-I`, bo markitdown siedzi w user site-packages.

## Testy

```bash
python -X utf8 scripts/konwertuj.py selftest
```

```bash
claude plugin test .
```

```bash
claude plugin validate .
```
