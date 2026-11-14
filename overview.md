# forced-markitdown - how it works

## Files

| File | Role |
| --- | --- |
| `.claude-plugin/plugin.json` | Plugin manifest |
| `.claude-plugin/marketplace.json` | Local marketplace (source `./`), so it can be installed from the folder |
| `hooks/hooks.json` | Points to the hooks module |
| `hooks/register.ts` | All the logic on the Claude Code side (hooks, commands) |
| `scripts/convert.py` | Conversion via markitdown, images, cache. Returns JSON on the last line of stdout |
| `tests/forced-markitdown.test.ts` | Hook tests (`claude plugin test .`), Python faked by the `process.run` hook |
| `tsconfig.json` | For type-checking (`tsc -p .`, once the engine puts types in `.claude-plugin/types`) |

## Flow

1. **`tool.call` (Read)** - file with a supported extension -> `convert()` -> `next({...e, file_path: md})`. `pages` is removed and (for PDFs with markers) turned into `offset`/`limit`. A `context` note is added to the result (where the images are, how to open them). Conversion error -> `next(e)` unchanged + toast.
2. **`prompt.mention`** - `@file.pdf` -> `next({...e, path: md})` + note in `context`.
3. **`prompt.submit`** - a `document` attachment (PDF pasted into the prompt): bytes via `$.prompt.media` (chunks of 4 MiB, base64 joined) -> Python via stdin -> `.md` content in `context`. The `dropDocuments` flag tells the `session.append` hook (door `prompt`) how many `document` blocks to drop from the prompt row. We drop them only if every conversion succeeded.
4. **`tool.check` (Read)** - path in the cache folder -> verdict as for the original `Read` (`$.tool.check` on the source). The source comes from the session map, and after a module reload or in a new session from the folder's `meta.json` (only under `%USERPROFILE%\.claude\markitdown-cache`). Attachments (`null` in the map, source in `attachments/`) -> `allow`. The path is checked by `realPath`, so `..` cannot escape the folder.
5. **Page preview with graphics (lazy)** - `Read` on `…/pages/page_NNN.png` that does not exist yet -> Python `preview` renders the page -> `next(e)` reads the ready file. The model decides when to open it (marker in the `.md` + note in `context`).
6. **Commands** - registered in `session.start`, handled by `command.run`. On/off state lives in `$.store` (`enabled`), so it persists between sessions.

## Hard solutions

- **markitdown import takes ~4 s** (magika/onnxruntime). The script checks the cache (`meta.json`: `mtime_ns`, size, `CACHE_VERSION`) *before* the import. Changing the output format = bump `CACHE_VERSION`.
- **Parallel reads of the same file** - `inFlight: Map<path, Promise>` in TS, so one file is converted by one process at a time.
- **DOCX/PPTX images** - `convert(..., keep_data_uris=True)` gives base64 in the Markdown. The `DATA_URI` regex cuts them out into `images/image_NNN.ext` (dedup by SHA-1 of the content) and inserts the absolute path (wrapped in `<...>` when there is a space).
- **PDF images** - markitdown does not extract them. `pypdfium2` (a pdfplumber dependency): `page.get_objects(FPDF_PAGEOBJ_IMAGE)` -> `PdfImage.extract()` (JPEG without recompression, the rest as PNG). Images < 48 px are skipped. Limit 300 images per file.
- **PDF pages** - pdfminer separates pages with the `\f` character; if it is present, the text is split and `<!-- page N -->` plus that page's images are inserted. The line numbers of the markers go to `pages` in the JSON and are used to map `pages` in `Read`. The pdfplumber path (tables/forms) has no `\f` -> images go into a section at the end.
- **Scans** - a page with no text and no images -> `page.render(scale=2)` to PNG.
- **Graphics detection (at conversion time, no render)** - `pageObjects()` takes top-level objects with bboxes (pdfium gives bounds of objects inside a Form XObject in form coordinates, so only the parent's content is counted). `repeated()`: same type and same bbox (rounded to 1 pt) on >=30% of pages (min. 2) = logo/footer. `analyzePage()` skips the background (>90% of the page), the 7% bands at the top/bottom, and repeated elements. A page has graphics if it has >=1 image >= 48 px or >=8 paths (`MIN_PATHS`). Repeated images (logos) are not linked on every page either.
- **Preview crop** - graphics bbox + a 4% margin (axis labels). The margin does not enter excluded elements that lie entirely outside the graphic (logo above, footer below). Only the crop is rendered (`render(crop=...)`), at a scale giving ~1500 px on the long side (1.5-4x). Then `trimEdges()`: an edge that crosses content (half a text line) is pulled back to the first empty line, at most by the margin. At the end, white trimming. A rotated page (`/Rotate`) gets only white trimming, because the crop coordinates are not reliable there.
- **Preview safety** - Python renders only under the cache dir, and takes the source from `meta.json`, not from a path given by the model.
- **Permissions** - the cache lives outside the project, so a plain `Read` from the cache would ask for consent or, if allowed, bypass the rules. The map `cache folder -> source` + `$.tool.check` on the source gives exactly the same decisions as for the original.
- **Python stdout** - libraries can print things, so the conversion runs inside `redirect_stdout(stderr)` and TS takes the last line of stdout. `-X utf8` + `ensure_ascii=True`, so non-ASCII characters don't break the JSON on Windows.
- **Python** - tried in order: `python`, `py -3`, `python3`; the first one that works is remembered until the module reloads. No `-I`, because markitdown lives in the user site-packages.

## Tests

```bash
python -X utf8 scripts/convert.py selftest
```

```bash
claude plugin test .
```

```bash
claude plugin validate .
```
