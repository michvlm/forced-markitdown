# forced-markitdown

**Claude Code reads your documents as clean Markdown, images included, and you never have to ask.**

A Claude Code plugin (a hooks module, or "mod") that forces every PDF, DOCX, PPTX, XLSX and similar file through [microsoft/markitdown](https://github.com/microsoft/markitdown) before the model sees it. Drop a PDF in the chat or let Claude `Read` one, and the model gets a `.md` file. You don't type a conversion command and Claude doesn't have to remember to run one.

---

## Why

Out of the box, Claude Code sends a PDF to the model as page images or as a document block, which costs a lot of tokens. Office files it can't read at all. Markdown is smaller, searchable, and easy for the model to quote. The usual workaround is asking the model to run `markitdown` in a shell, which you have to remember every time and which throws images away.

## Features

- 🔁 **Automatic, three ways in**
  - every `Read` the model makes on a supported file,
  - every `@file.pdf` mention in your prompt,
  - every PDF pasted into the prompt as a document block.
- 🖼️ **Images are kept**
  - Images embedded in DOCX/PPTX (base64 inside markitdown's output) are written out as PNG/JPG files and linked from the Markdown.
  - Bitmaps inside PDFs are extracted too. markitdown alone drops them.
  - Claude opens any image with `Read` and actually sees it.
- 📈 **Vector charts, on demand**
  - Charts drawn as vectors in a PDF (Excel, matplotlib, LaTeX exports) aren't images, so every extractor loses them.
  - This plugin marks each page that has graphics with a preview path.
  - The page is rendered only when the model asks for it, and it's cropped tightly to the graphic, without margins, header, footer or logo.
  - Until then it costs nothing.
- 📄 **Page-aware PDFs**
  - The Markdown has `<!-- page N -->` page markers.
  - `Read(pages: "3-5")` on a PDF is turned into the matching line range of the Markdown.
- 🖨️ **Scanned PDFs work** - a page with no text layer is rendered to PNG, so the model can read it visually.
- ⚡ **Cached**
  - Each file is converted once and redone only when it changes.
  - A cache hit takes about 0.1 s; a fresh conversion takes about 4 s, which is markitdown's import time.
- 🔒 **Permissions unchanged**
  - Reading the cached `.md` or a preview is judged exactly like reading the original file.
  - Rendering is limited to the cache folder.
- 🛟 **Safe fallback** - if a conversion fails, Claude gets the original file and you get a toast.

## Requirements

- Claude Code with hooks-module (mod) support. Tested on 2.1.318 and 2.1.321.
- Python 3 on `PATH`, as `python`, `py -3` or `python3`, with markitdown installed:

```bash
pip install "markitdown[all]"
```

`pypdfium2` and `Pillow` come with `markitdown[all]` and handle PDF images and previews. Nothing else is needed.

## Install

The repository is private for now, so you need read access on GitHub and a git credential for github.com, for example from `gh auth login` followed by `gh auth setup-git`.

At the prompt of a Claude Code terminal session:

```
/plugin install forced-markitdown --marketplace michvlm/forced-markitdown
```

From a shell:

```bash
claude plugin install forced-markitdown --marketplace michvlm/forced-markitdown
```

In the desktop app, where `/plugin` doesn't run: click **+** next to the prompt box, then **Plugins**, then **Add marketplace** with `michvlm/forced-markitdown`, then **Add plugin**.

**Working from a local clone:** `claude plugin marketplace add <folder>` reads the plugin straight from that folder, so `/reload-plugins` picks up your edits.

## Commands

| Command | What it does |
| --- | --- |
| `/markitdown-status` | On/off state, Python, markitdown version, cache size, conversions this session |
| `/markitdown-on` | Turn automatic conversion on (default; remembered across sessions) |
| `/markitdown-off` | Turn it off: Claude gets the original files |
| `/markitdown-convert <path> [--force]` | Convert a file right now and print the `.md` path (`--force` skips the cache) |
| `/markitdown-clear` | Delete the cache |

## Supported formats

`.pdf .docx .pptx .xlsx .xls .msg .epub .zip .mp3 .wav .m4a`

`.html .csv .json .xml .ipynb .txt` and images are left out on purpose. They are source files Claude reads, and often edits, better as they are.

## How it works

```
Read / @mention / pasted PDF
        │
        ▼
hooks/register.ts ── python scripts/convert.py ──► ~/.claude/markitdown-cache/<name>-<hash>/
        │                                               ├── <name>.md      (text + image links + page markers)
        │                                               ├── images/        (extracted images)
        │                                               ├── pages/         (page previews, rendered on first Read)
        │                                               └── meta.json
        ▼
model reads <name>.md, then Reads page_NNN.png only when it needs a chart
```

See [overview.md](overview.md) for the design notes: cache invalidation, header and footer detection, crop heuristics and the permission mapping.

## Known limitations

- **Charts arrive as a page crop, not one by one.** A page with a table and a chart gives one image that holds both.
- **Tables count as graphics.** Table rules are vectors, so such pages get a preview marker. That helps when markitdown has scrambled the table.
- **Cropping is heuristic.**
  - An object found in the same place on at least 30% of pages, or sitting in the top or bottom 7% of a page, is treated as a header or footer.
  - A chart repeated identically on many pages would be taken for decoration.
  - A label right at the crop edge may be dropped. Its text is still in the Markdown.
- **Some PDFs have no page markers.** When markitdown detects tables or forms it joins the text without page breaks. Images and preview markers then go to sections at the end of the file, and `pages` in `Read` is ignored.
- **Audio** transcription needs internet (Google Speech), and MP3/M4A also need `ffmpeg`.
- **`Bash` isn't intercepted.** `cat file.pdf` or `pdftotext` still see the original. Only `Read` and `@` mentions are converted.

## Development

```bash
python -X utf8 scripts/convert.py selftest
```

```bash
claude plugin test .
```

```bash
claude plugin validate .
```

## License

MIT
