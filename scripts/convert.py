"""forced-markitdown: convert files to Markdown via markitdown, keeping images.

Usage (result is always one JSON line on stdout):
  python convert.py file <path> [--force]        convert a file (from cache)
  python convert.py stdin <name>                 base64 on stdin -> save to cache -> convert
  python convert.py preview <page_NNN.png>       render PDF page cropped to its graphics (on demand)
  python convert.py info                         versions, cache dir, size
  python convert.py clear                        delete cache
  python convert.py selftest                     test on generated files
Option --cache <dir> changes the cache dir (default ~/.claude/markitdown-cache).
"""

import base64
import contextlib
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile

# Change when the output format changes - old cache entries stop matching.
CACHE_VERSION = 5
MAX_IMAGES = 300
MIN_IMAGE_SIDE = 48  # px; smaller ones are usually icons/lines
EDGE_ZONE = 0.07  # top/bottom 7% of page = header/footer, skipped for graphics
MIN_PATHS = 8  # this many vector paths on a page = "has graphics"
PREVIEW_LONG_SIDE = 1500  # px; what the model sees without downscaling
DEFAULT_CACHE = os.path.join(os.path.expanduser("~"), ".claude", "markitdown-cache")

DATA_URI = re.compile(r"data:image/([\w.+-]+);base64,([A-Za-z0-9+/=]+)")
EXT_MIME = {"jpeg": "jpg", "svg+xml": "svg", "x-emf": "emf", "x-wmf": "wmf", "tiff": "tif"}


def emit(data):
    sys.stdout.write(json.dumps(data, ensure_ascii=True) + "\n")
    sys.stdout.flush()


def pathMd(path):
    """Path to insert in a Markdown link (spaces -> <...>)."""
    s = path.replace("\\", "/")
    return f"<{s}>" if " " in s else s


def folderFor(source, cache):
    absPath = os.path.normcase(os.path.realpath(source))
    short = hashlib.sha1(absPath.encode("utf-8")).hexdigest()[:12]
    name = re.sub(r"[^\w.-]+", "_", os.path.basename(source))[:60]
    return os.path.join(cache, f"{name}-{short}")


class Images:
    """Saves images to a folder, dedupes by content."""

    def __init__(self, dir):
        self.dir = dir
        self.seen = {}
        self.counter = 0

    def save(self, data, ext):
        short = hashlib.sha1(data).hexdigest()
        if short in self.seen:
            return self.seen[short]
        if self.counter >= MAX_IMAGES:
            return None
        os.makedirs(self.dir, exist_ok=True)
        self.counter += 1
        path = os.path.join(self.dir, f"image_{self.counter:03d}.{ext}")
        with open(path, "wb") as f:
            f.write(data)
        self.seen[short] = path
        return path


def extractDataUris(md, images):
    def replace(m):
        kind, b64 = m.group(1).lower(), m.group(2)
        try:
            data = base64.b64decode(b64)
        except Exception:
            return "image-broken"
        path = images.save(data, EXT_MIME.get(kind, kind))
        return pathMd(path) if path else "image-skipped-limit"

    return DATA_URI.sub(replace, md)


def pageObjects(page):
    """Top-level page objects: type, bbox (l,b,r,t) and graphic counters inside.

    Bboxes only from level 0 - pdfium gives bounds of objects inside a Form XObject
    in form coordinates, not page coordinates.
    """
    import pypdfium2.raw as pdfium_c

    result = []
    current = None
    for obj in page.get_objects():
        if obj.level == 0:
            try:
                bbox = tuple(obj.get_bounds())
            except Exception:
                bbox = None
            current = {"type": obj.type, "bbox": bbox, "paths": 0, "images": 0}
            result.append(current)
        if current is None:
            continue
        if obj.type == pdfium_c.FPDF_PAGEOBJ_PATH:
            current["paths"] += 1
        elif obj.type == pdfium_c.FPDF_PAGEOBJ_IMAGE:
            try:
                widthPx, heightPx = obj.get_px_size()
                current["images"] += widthPx >= MIN_IMAGE_SIDE and heightPx >= MIN_IMAGE_SIDE
            except Exception:
                pass
    return result


def objectKey(o):
    return (o["type"],) + tuple(round(v) for v in o["bbox"]) if o["bbox"] else None


def repeated(pagesObjects):
    """Keys of objects in the same place on many pages (logo, footer line)."""
    if len(pagesObjects) < 2:
        return set()
    counter = {}
    for objects in pagesObjects:
        for k in {objectKey(o) for o in objects if o["bbox"] and o["type"] != 1}:
            counter[k] = counter.get(k, 0) + 1
    threshold = max(2, -(-3 * len(pagesObjects) // 10))  # 30% of pages, min. 2
    return {k for k, n in counter.items() if n >= threshold}


def analyzePage(objects, rep, cropbox):
    """Page graphics without background, header/footer and repeated elements.

    Returns (paths, images, graphics bbox or None, bboxes of excluded: header/footer/repeated).
    """
    import pypdfium2.raw as pdfium_c

    l0, b0, r0, t0 = cropbox
    width, height = r0 - l0, t0 - b0
    paths = images = 0
    bbox = None
    excluded = []
    for o in objects:
        if not o["bbox"]:
            continue
        l, b, r, t = o["bbox"]
        if (r - l) * (t - b) > 0.9 * width * height:  # page background / full-page frame
            continue
        if (objectKey(o) in rep or t < b0 + height * EDGE_ZONE
                or b > t0 - height * EDGE_ZONE):
            excluded.append(o["bbox"])
            continue
        if o["type"] == pdfium_c.FPDF_PAGEOBJ_TEXT:
            continue
        paths += o["paths"]
        images += o["images"]
        if o["paths"] or o["images"] or o["type"] == pdfium_c.FPDF_PAGEOBJ_SHADING:
            bbox = o["bbox"] if bbox is None else (
                min(bbox[0], l), min(bbox[1], b), max(bbox[2], r), max(bbox[3], t))
    return paths, images, bbox, excluded


def trimEdges(img, limit):
    """Frame edge crosses content (half a text line, piece of logo) -> pull it inward
    to the first empty line, at most `limit` px (not further than the margin around the graphic)."""
    from PIL import ImageOps

    mask = ImageOps.invert(img.convert("L")).point(lambda p: 255 if p > 10 else 0)
    w, h = mask.size

    def empty(x0, y0, x1, y1):
        return mask.crop((x0, y0, x1, y1)).getbbox() is None

    top, bottom, left, right = 0, h, 0, w
    if not empty(0, 0, w, 1):
        top = next((y for y in range(1, min(limit, h // 2)) if empty(0, y, w, y + 1)), 0)
    if not empty(0, h - 1, w, h):
        bottom = next((y + 1 for y in range(h - 2, max(h - 1 - limit, h // 2), -1) if empty(0, y, w, y + 1)), h)
    if not empty(0, 0, 1, h):
        left = next((x for x in range(1, min(limit, w // 2)) if empty(x, 0, x + 1, h)), 0)
    if not empty(w - 1, 0, w, h):
        right = next((x + 1 for x in range(w - 2, max(w - 1 - limit, w // 2), -1) if empty(x, 0, x + 1, h)), w)
    return img.crop((left, top, right, bottom))


def previewPath(folder, no):
    return os.path.join(folder, "pages", f"page_{no:03d}.png")


def preview(png, cache):
    """Renders PDF page cropped to its graphics (on demand, at first Read)."""
    import pypdfium2 as pdfium
    from PIL import ImageOps

    png = os.path.realpath(png)
    if not png.startswith(os.path.realpath(cache) + os.sep):
        return {"ok": False, "error": "preview only inside cache dir"}
    match = re.fullmatch(r"page_(\d+)\.png", os.path.basename(png))
    folder = os.path.dirname(os.path.dirname(png))
    metaFile = os.path.join(folder, "meta.json")
    if not match or not os.path.exists(metaFile):
        return {"ok": False, "error": "not a page preview path"}
    with open(metaFile, encoding="utf-8") as f:
        meta = json.load(f)
    rep = {tuple(k) for k in meta.get("repeated", [])}
    no = int(match.group(1))

    pdf = pdfium.PdfDocument(meta["source"])
    try:
        if not 1 <= no <= len(pdf):
            return {"ok": False, "error": f"PDF has {len(pdf)} pages"}
        page = pdf[no - 1]
        l0, b0, r0, t0 = page.get_cropbox()
        _, _, bbox, excluded = analyzePage(pageObjects(page), rep, (l0, b0, r0, t0))
        crop = (0, 0, 0, 0)
        width, height = r0 - l0, t0 - b0
        margin = 0.04 * min(width, height)  # for axis labels and legends next to paths
        cropped = False
        # On a rotated page we don't trust coordinates - only white trimming remains.
        if bbox and page.get_rotation() == 0:
            gl, gb, gr, gt = bbox
            l, b, r, t = gl - margin, gb - margin, gr + margin, gt + margin
            # Margin does not enter header/footer/logo lying entirely outside the graphic.
            for el, eb, er, et in excluded:
                if et <= gb:
                    b = max(b, et)
                elif eb >= gt:
                    t = min(t, eb)
                elif er <= gl:
                    l = max(l, er)
                elif el >= gr:
                    r = min(r, el)
            l, b, r, t = max(l0, l), max(b0, b), min(r0, r), min(t0, t)
            if (r - l) * (t - b) > 0.01 * width * height:
                crop = (l - l0, b - b0, r0 - r, t0 - t)
                width, height = r - l, t - b
                cropped = True
        scale = min(4.0, max(1.5, PREVIEW_LONG_SIDE / max(width, height)))
        img = page.render(scale=scale, crop=crop).to_pil().convert("RGB")
    finally:
        pdf.close()

    if cropped:
        img = trimEdges(img, int(margin * scale))

    # White trim: everything lighter than 245 is background.
    mask = ImageOps.invert(img.convert("L")).point(lambda p: 255 if p > 10 else 0)
    outline = mask.getbbox()
    if outline:
        pad = 12
        img = img.crop((max(0, outline[0] - pad), max(0, outline[1] - pad),
                        min(img.width, outline[2] + pad), min(img.height, outline[3] + pad)))
    os.makedirs(os.path.dirname(png), exist_ok=True)
    img.save(png)
    return {"ok": True, "png": png, "page": no, "size": list(img.size)}


def imagesFromPdf(source, md, images, folder):
    """Appends PDF page images and preview markers for pages with graphics.

    Returns (md, whether page markers are present, page numbers with preview, repeated keys).
    """
    import pypdfium2 as pdfium
    import pypdfium2.raw as pdfium_c

    pdf = pdfium.PdfDocument(source)
    tempDir = tempfile.mkdtemp(prefix="fmd-")
    previews = []
    try:
        pagesObjects = [pageObjects(pdf[i]) for i in range(len(pdf))]
        rep = repeated(pagesObjects)
        onPage = []
        for i in range(len(pdf)):
            page = pdf[i]
            paths, imgs, _, _ = analyzePage(pagesObjects[i], rep, page.get_cropbox())
            if imgs or paths >= MIN_PATHS:
                previews.append(i + 1)
            lst = []
            skipParent = False
            for obj in page.get_objects():
                if obj.level == 0:
                    try:
                        skipParent = objectKey({"type": obj.type, "bbox": tuple(obj.get_bounds())}) in rep
                    except Exception:
                        skipParent = False
                if skipParent or obj.type != pdfium_c.FPDF_PAGEOBJ_IMAGE:
                    continue
                try:
                    width, height = obj.get_px_size()
                    if width < MIN_IMAGE_SIDE or height < MIN_IMAGE_SIDE:
                        continue
                    prefix = os.path.join(tempDir, "x")
                    obj.extract(prefix, fb_format="png")
                    file = next(p for p in os.listdir(tempDir) if p.startswith("x."))
                    full = os.path.join(tempDir, file)
                    with open(full, "rb") as f:
                        data = f.read()
                    os.remove(full)
                    path = images.save(data, file.split(".", 1)[1])
                    if path and path not in lst:
                        lst.append(path)
                except Exception:
                    continue
            onPage.append(lst)

        pageTexts = md.split("\f") if "\f" in md else None
        # Scan without text layer: render pages that have neither text nor images.
        for i, lst in enumerate(onPage):
            emptyText = (pageTexts[i].strip() == "") if pageTexts and i < len(pageTexts) else not md.strip()
            if emptyText and not lst:
                img = pdf[i].render(scale=2).to_pil()
                buffer = os.path.join(tempDir, "render.png")
                img.save(buffer)
                with open(buffer, "rb") as f:
                    path = images.save(f.read(), "png")
                if path:
                    lst.append(path)
                    if i + 1 in previews:
                        previews.remove(i + 1)  # whole page is already an image
    finally:
        shutil.rmtree(tempDir, ignore_errors=True)
        pdf.close()

    def links(no, lst):
        return "\n".join(f"![page {no}, image {k}]({pathMd(p)})" for k, p in enumerate(lst, 1))

    def marker(no):
        path = previewPath(folder, no).replace("\\", "/")
        return (f"<!-- page {no}: graphics (chart/figure/table) - page preview cropped "
                f"to graphics: Read `{path}` -->")

    if pageTexts:
        parts = []
        for i in range(len(onPage)):
            text = pageTexts[i].strip() if i < len(pageTexts) else ""
            block = f"<!-- page {i + 1} -->\n\n{text}"
            if onPage[i]:
                block += "\n\n" + links(i + 1, onPage[i])
            if i + 1 in previews:
                block += "\n\n" + marker(i + 1)
            parts.append(block.rstrip())
        rest = "\n".join(t.strip() for t in pageTexts[len(onPage):] if t.strip())
        return "\n\n".join(parts) + ("\n\n" + rest if rest else ""), True, previews, rep

    sections = [links(i + 1, lst) for i, lst in enumerate(onPage) if lst]
    if sections:
        md = md.rstrip() + "\n\n## Images from PDF\n\n" + "\n\n".join(sections)
    if previews:
        md = md.rstrip() + "\n\n## Pages with graphics\n\n" + "\n".join(marker(nr) for nr in previews)
    return md, False, previews, rep


def convert(source, cache, force=False):
    source = os.path.realpath(source)
    stat = os.stat(source)
    folder = folderFor(source, cache)
    metaFile = os.path.join(folder, "meta.json")

    if not force and os.path.exists(metaFile):
        try:
            with open(metaFile, encoding="utf-8") as f:
                meta = json.load(f)
            if (meta.get("version") == CACHE_VERSION and meta.get("mtime") == stat.st_mtime_ns
                    and meta.get("size") == stat.st_size and os.path.exists(meta.get("md", ""))):
                return dict(meta, ok=True, cached=True)
        except Exception:
            pass

    # Import only here: takes a few seconds, the cache skips it.
    with contextlib.redirect_stdout(sys.stderr):
        from markitdown import MarkItDown

        md = MarkItDown(enable_plugins=False).convert(source, keep_data_uris=True).markdown or ""

    shutil.rmtree(folder, ignore_errors=True)
    os.makedirs(folder, exist_ok=True)
    imagesDir = os.path.join(folder, "images")
    images = Images(imagesDir)

    md = extractDataUris(md, images)
    withMarkers = False
    previews = []
    rep = set()
    if source.lower().endswith(".pdf"):
        with contextlib.redirect_stdout(sys.stderr):
            md, withMarkers, previews, rep = imagesFromPdf(source, md, images, os.path.realpath(folder))

    header = (f"<!-- forced-markitdown | source: {source.replace(chr(92), '/')} | images: {images.counter}"
              + (f" in {imagesDir.replace(chr(92), '/')} (Read an image path to view it)" if images.counter else "")
              + " -->")
    body = header + "\n\n" + md.strip() + "\n"
    lines = body.split("\n")
    pages = [i + 1 for i, line in enumerate(lines) if re.fullmatch(r"<!-- page \d+ -->", line)] if withMarkers else []

    mdName = os.path.splitext(os.path.basename(source))[0] + ".md"
    mdFile = os.path.join(folder, mdName)
    with open(mdFile, "w", encoding="utf-8", newline="\n") as f:
        f.write(body)

    meta = {
        "version": CACHE_VERSION, "source": source, "mtime": stat.st_mtime_ns, "size": stat.st_size,
        "md": mdFile, "folder": os.path.realpath(folder), "imagesDir": imagesDir,
        "images": images.counter, "pages": pages, "lines": len(lines), "chars": len(body),
        "previews": previews, "repeated": sorted(rep),
    }
    with open(metaFile, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False)
    return dict(meta, ok=True, cached=False)


def fromStdin(name, cache):
    data = base64.b64decode(sys.stdin.buffer.read())
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", os.path.basename(name)) or "attachment.pdf"
    attDir = os.path.join(cache, "attachments", hashlib.sha1(data).hexdigest()[:12])
    os.makedirs(attDir, exist_ok=True)
    file = os.path.join(attDir, name)
    if not os.path.exists(file):
        with open(file, "wb") as f:
            f.write(data)
    result = convert(file, cache)
    result["attachmentDir"] = os.path.realpath(attDir)
    return result


def dirSize(dir):
    files, size = 0, 0
    for root, _, names in os.walk(dir):
        for n in names:
            if n.endswith(".md"):
                files += 1
            try:
                size += os.path.getsize(os.path.join(root, n))
            except OSError:
                pass
    return files, size


def info(cache):
    from importlib.metadata import version

    files, size = dirSize(cache)
    return {"ok": True, "python": sys.executable, "pythonVersion": sys.version.split()[0],
            "markitdown": version("markitdown"), "cache": cache, "mdFiles": files, "bytes": size}


def clear(cache):
    files, size = dirSize(cache)
    shutil.rmtree(cache, ignore_errors=True)
    return {"ok": True, "cache": cache, "mdFiles": files, "bytes": size}


def selftest():
    """Generates a PPTX with an image and a scanned PDF, converts them, checks the result."""
    from PIL import Image
    from pptx import Presentation
    from pptx.util import Inches

    tmp = tempfile.mkdtemp(prefix="fmd-test-")
    cache = os.path.join(tmp, "cache")
    try:
        png = os.path.join(tmp, "red.png")
        Image.new("RGB", (120, 80), "red").save(png)

        pptx = os.path.join(tmp, "slides test.pptx")
        prez = Presentation()
        slide = prez.slides.add_slide(prez.slide_layouts[5])
        slide.shapes.title.text = "Slide title"
        slide.shapes.add_picture(png, Inches(1), Inches(2))
        prez.save(pptx)

        pdf = os.path.join(tmp, "scan.pdf")
        Image.new("RGB", (400, 300), "blue").save(pdf)

        w = convert(pptx, cache)
        body = open(w["md"], encoding="utf-8").read()
        assert "Slide title" in body, body
        assert w["images"] == 1 and "base64" not in body, body
        assert os.path.exists(os.path.join(w["imagesDir"], "image_001.png"))
        assert convert(pptx, cache)["cached"] is True

        w = convert(pdf, cache)
        body = open(w["md"], encoding="utf-8").read()
        assert w["images"] == 1, body
        assert "page 1" in body, body

        assert w["previews"] == [], w  # scan: page already rendered whole

        # Vector chart in the middle of an A4 page -> marker + preview cropped to the chart.
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        vector = os.path.join(tmp, "chart.pdf")
        fig = plt.figure(figsize=(8.27, 11.69))
        fig.text(0.1, 0.8, "Fig. 1 shows the temperature rise over the measurement time.")
        ax = fig.add_axes((0.3, 0.4, 0.3, 0.2))
        ax.plot([0, 1, 2, 3], [1, 3, 2, 4])
        ax.set_xlabel("time [s]")
        fig.savefig(vector)
        plt.close(fig)

        w = convert(vector, cache)
        body = open(w["md"], encoding="utf-8").read()
        assert w["previews"] == [1], w
        assert "page_001.png" in body, body
        png = os.path.join(w["folder"], "pages", "page_001.png")
        p = preview(png, cache)
        assert p["ok"] and os.path.exists(png), p
        width, height = p["size"]
        assert abs(width / height - 8.27 / 11.69) > 0.15, p  # not the whole A4 page
        assert not preview(os.path.join(tmp, "pages", "page_001.png"), cache)["ok"]  # outside cache

        def obj(bbox):
            return {"type": 2, "bbox": bbox, "paths": 1, "images": 0}

        pages = [[obj((0, 800, 100, 840)), obj((10, 10 + i * 50, 200, 300))] for i in range(3)]
        assert repeated(pages) == {(2, 0, 800, 100, 840)}, repeated(pages)  # logo yes, charts no
        assert repeated(pages[:1]) == set()

        assert DATA_URI.sub("X", "![a](data:image/png;base64,QUJD)") == "![a](X)"
        assert pathMd(r"C:\a b\c.png") == "<C:/a b/c.png>"
        return {"ok": True, "selftest": "passed"}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv):
    cache = DEFAULT_CACHE
    if "--cache" in argv:
        i = argv.index("--cache")
        cache = argv[i + 1]
        del argv[i:i + 2]
    force = "--force" in argv
    argv = [a for a in argv if a != "--force"]
    command = argv[0] if argv else ""
    try:
        if command == "file" and len(argv) > 1:
            if not os.path.isfile(argv[1]):
                return {"ok": False, "error": f"no file: {argv[1]}"}
            return convert(argv[1], cache, force)
        if command == "preview" and len(argv) > 1:
            with contextlib.redirect_stdout(sys.stderr):
                return preview(argv[1], cache)
        if command == "stdin" and len(argv) > 1:
            return fromStdin(argv[1], cache)
        if command == "info":
            return info(cache)
        if command == "clear":
            return clear(cache)
        if command == "selftest":
            return selftest()
        return {"ok": False, "error": "usage: file <path> | preview <png> | stdin <name> | info | clear | selftest"}
    except AssertionError as e:
        return {"ok": False, "error": f"selftest failed: {str(e)[:500]}"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:500]}"}


if __name__ == "__main__":
    emit(main(sys.argv[1:]))
