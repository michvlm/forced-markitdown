"""forced-markitdown: konwersja plikow do Markdown przez markitdown, z obrazami.

Uzycie (wynik zawsze jako jedna linia JSON na stdout):
  python konwertuj.py plik <sciezka> [--wymus]   konwersja pliku (z cache)
  python konwertuj.py stdin <nazwa>              base64 na stdin -> zapis do cache -> konwersja
  python konwertuj.py podglad <strona_NNN.png>   render strony PDF przycietej do grafiki (na zadanie)
  python konwertuj.py info                       wersje, katalog cache, rozmiar
  python konwertuj.py czysc                      usuwa cache
  python konwertuj.py selftest                   test na wygenerowanych plikach
Opcja --cache <katalog> zmienia katalog cache (domyslnie ~/.claude/markitdown-cache).
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

# Zmien gdy zmienia sie format wyjscia - stare wpisy cache przestana pasowac.
WERSJA_CACHE = 4
MAX_OBRAZOW = 300
MIN_BOK_OBRAZU = 48  # px; mniejsze to zwykle ikonki/linie
STREFA_BRZEGU = 0.07  # gorne/dolne 7% strony = naglowek/stopka, pomijane przy grafice
MIN_SCIEZEK = 8  # tyle sciezek wektorowych na stronie = "jest grafika"
DLUGI_BOK_PODGLADU = 1500  # px; tyle widzi model bez skalowania w dol
DOMYSLNY_CACHE = os.path.join(os.path.expanduser("~"), ".claude", "markitdown-cache")

DATA_URI = re.compile(r"data:image/([\w.+-]+);base64,([A-Za-z0-9+/=]+)")
ROZSZ_MIME = {"jpeg": "jpg", "svg+xml": "svg", "x-emf": "emf", "x-wmf": "wmf", "tiff": "tif"}


def wypisz(dane):
    sys.stdout.write(json.dumps(dane, ensure_ascii=True) + "\n")
    sys.stdout.flush()


def sciezkaMd(sciezka):
    """Ścieżka do wstawienia w link Markdown (spacje -> <...>)."""
    s = sciezka.replace("\\", "/")
    return f"<{s}>" if " " in s else s


def folderDla(zrodlo, cache):
    absP = os.path.normcase(os.path.realpath(zrodlo))
    skrot = hashlib.sha1(absP.encode("utf-8")).hexdigest()[:12]
    nazwa = re.sub(r"[^\w.-]+", "_", os.path.basename(zrodlo))[:60]
    return os.path.join(cache, f"{nazwa}-{skrot}")


class Obrazy:
    """Zapisuje obrazy do folderu, deduplikuje po treści."""

    def __init__(self, katalog):
        self.katalog = katalog
        self.widziane = {}
        self.licznik = 0

    def zapisz(self, dane, rozsz):
        skrot = hashlib.sha1(dane).hexdigest()
        if skrot in self.widziane:
            return self.widziane[skrot]
        if self.licznik >= MAX_OBRAZOW:
            return None
        os.makedirs(self.katalog, exist_ok=True)
        self.licznik += 1
        sciezka = os.path.join(self.katalog, f"obraz_{self.licznik:03d}.{rozsz}")
        with open(sciezka, "wb") as f:
            f.write(dane)
        self.widziane[skrot] = sciezka
        return sciezka


def wyciagnijDataUri(md, obrazy):
    def podmien(m):
        typ, b64 = m.group(1).lower(), m.group(2)
        try:
            dane = base64.b64decode(b64)
        except Exception:
            return "obraz-uszkodzony"
        sciezka = obrazy.zapisz(dane, ROZSZ_MIME.get(typ, typ))
        return sciezkaMd(sciezka) if sciezka else "obraz-pominiety-limit"

    return DATA_URI.sub(podmien, md)


def obiektyStrony(strona):
    """Obiekty najwyzszego poziomu strony: typ, ramka (l,b,r,t) i liczniki grafiki w srodku.

    Ramki tylko z poziomu 0 - granice obiektow wewnatrz Form XObject pdfium
    podaje w ukladzie formularza, nie strony.
    """
    import pypdfium2.raw as pdfium_c

    wynik = []
    biezacy = None
    for obj in strona.get_objects():
        if obj.level == 0:
            try:
                ramka = tuple(obj.get_bounds())
            except Exception:
                ramka = None
            biezacy = {"typ": obj.type, "ramka": ramka, "sciezki": 0, "obrazki": 0}
            wynik.append(biezacy)
        if biezacy is None:
            continue
        if obj.type == pdfium_c.FPDF_PAGEOBJ_PATH:
            biezacy["sciezki"] += 1
        elif obj.type == pdfium_c.FPDF_PAGEOBJ_IMAGE:
            try:
                szerPx, wysPx = obj.get_px_size()
                biezacy["obrazki"] += szerPx >= MIN_BOK_OBRAZU and wysPx >= MIN_BOK_OBRAZU
            except Exception:
                pass
    return wynik


def kluczObiektu(o):
    return (o["typ"],) + tuple(round(v) for v in o["ramka"]) if o["ramka"] else None


def powtarzalne(obiektyStron):
    """Klucze obiektow w tym samym miejscu na wielu stronach (logo, linia stopki)."""
    if len(obiektyStron) < 2:
        return set()
    licznik = {}
    for obiekty in obiektyStron:
        for k in {kluczObiektu(o) for o in obiekty if o["ramka"] and o["typ"] != 1}:
            licznik[k] = licznik.get(k, 0) + 1
    prog = max(2, -(-3 * len(obiektyStron) // 10))  # 30% stron, min. 2
    return {k for k, n in licznik.items() if n >= prog}


def analizaStrony(obiekty, powt, cropbox):
    """Grafika strony bez tla, naglowka/stopki i elementow powtarzalnych.

    Zwraca (sciezki, obrazki, ramka grafiki lub None, ramki wykluczonych: naglowek/stopka/powtarzalne).
    """
    import pypdfium2.raw as pdfium_c

    l0, b0, r0, t0 = cropbox
    szer, wys = r0 - l0, t0 - b0
    sciezki = obrazki = 0
    ramka = None
    wykluczone = []
    for o in obiekty:
        if not o["ramka"]:
            continue
        l, b, r, t = o["ramka"]
        if (r - l) * (t - b) > 0.9 * szer * wys:  # tlo strony / ramka na cala strone
            continue
        if (kluczObiektu(o) in powt or t < b0 + wys * STREFA_BRZEGU
                or b > t0 - wys * STREFA_BRZEGU):
            wykluczone.append(o["ramka"])
            continue
        if o["typ"] == pdfium_c.FPDF_PAGEOBJ_TEXT:
            continue
        sciezki += o["sciezki"]
        obrazki += o["obrazki"]
        if o["sciezki"] or o["obrazki"] or o["typ"] == pdfium_c.FPDF_PAGEOBJ_SHADING:
            ramka = o["ramka"] if ramka is None else (
                min(ramka[0], l), min(ramka[1], b), max(ramka[2], r), max(ramka[3], t))
    return sciezki, obrazki, ramka, wykluczone


def cofnijKrawedzie(obraz, limit):
    """Krawedz kadru przecina tresc (pol linii tekstu, kawalek logo) -> cofnij ja do srodka
    do pierwszej pustej linii, najwyzej o `limit` px (czyli nie dalej niz zapas wokol grafiki)."""
    from PIL import ImageOps

    maska = ImageOps.invert(obraz.convert("L")).point(lambda p: 255 if p > 10 else 0)
    w, h = maska.size

    def pusta(x0, y0, x1, y1):
        return maska.crop((x0, y0, x1, y1)).getbbox() is None

    gora, dol, lewa, prawa = 0, h, 0, w
    if not pusta(0, 0, w, 1):
        gora = next((y for y in range(1, min(limit, h // 2)) if pusta(0, y, w, y + 1)), 0)
    if not pusta(0, h - 1, w, h):
        dol = next((y + 1 for y in range(h - 2, max(h - 1 - limit, h // 2), -1) if pusta(0, y, w, y + 1)), h)
    if not pusta(0, 0, 1, h):
        lewa = next((x for x in range(1, min(limit, w // 2)) if pusta(x, 0, x + 1, h)), 0)
    if not pusta(w - 1, 0, w, h):
        prawa = next((x + 1 for x in range(w - 2, max(w - 1 - limit, w // 2), -1) if pusta(x, 0, x + 1, h)), w)
    return obraz.crop((lewa, gora, prawa, dol))


def sciezkaPodgladu(folder, nr):
    return os.path.join(folder, "strony", f"strona_{nr:03d}.png")


def podglad(png, cache):
    """Renderuje strone PDF przycieta do grafiki (na zadanie, przy pierwszym Read)."""
    import pypdfium2 as pdfium
    from PIL import ImageOps

    png = os.path.realpath(png)
    if not png.startswith(os.path.realpath(cache) + os.sep):
        return {"ok": False, "blad": "podglad tylko w katalogu cache"}
    dopasowanie = re.fullmatch(r"strona_(\d+)\.png", os.path.basename(png))
    folder = os.path.dirname(os.path.dirname(png))
    plikMeta = os.path.join(folder, "meta.json")
    if not dopasowanie or not os.path.exists(plikMeta):
        return {"ok": False, "blad": "to nie jest sciezka podgladu strony"}
    with open(plikMeta, encoding="utf-8") as f:
        meta = json.load(f)
    powt = {tuple(k) for k in meta.get("powtarzalne", [])}
    nr = int(dopasowanie.group(1))

    pdf = pdfium.PdfDocument(meta["zrodlo"])
    try:
        if not 1 <= nr <= len(pdf):
            return {"ok": False, "blad": f"PDF ma {len(pdf)} stron"}
        strona = pdf[nr - 1]
        l0, b0, r0, t0 = strona.get_cropbox()
        _, _, ramka, wykluczone = analizaStrony(obiektyStrony(strona), powt, (l0, b0, r0, t0))
        przyciecie = (0, 0, 0, 0)
        szer, wys = r0 - l0, t0 - b0
        zapas = 0.04 * min(szer, wys)  # na podpisy osi i legendy obok sciezek
        przycieto = False
        # Przy obroconej stronie nie ufamy wspolrzednym - zostaje samo ciecie bieli.
        if ramka and strona.get_rotation() == 0:
            gl, gb, gr, gt = ramka
            l, b, r, t = gl - zapas, gb - zapas, gr + zapas, gt + zapas
            # Zapas nie wchodzi na naglowek/stopke/logo lezace w calosci po zewnetrznej stronie.
            for el, eb, er, et in wykluczone:
                if et <= gb:
                    b = max(b, et)
                elif eb >= gt:
                    t = min(t, eb)
                elif er <= gl:
                    l = max(l, er)
                elif el >= gr:
                    r = min(r, el)
            l, b, r, t = max(l0, l), max(b0, b), min(r0, r), min(t0, t)
            if (r - l) * (t - b) > 0.01 * szer * wys:
                przyciecie = (l - l0, b - b0, r0 - r, t0 - t)
                szer, wys = r - l, t - b
                przycieto = True
        skala = min(4.0, max(1.5, DLUGI_BOK_PODGLADU / max(szer, wys)))
        obraz = strona.render(scale=skala, crop=przyciecie).to_pil().convert("RGB")
    finally:
        pdf.close()

    if przycieto:
        obraz = cofnijKrawedzie(obraz, int(zapas * skala))

    # Ciecie bieli: wszystko jasniejsze niz 245 to tlo.
    maska = ImageOps.invert(obraz.convert("L")).point(lambda p: 255 if p > 10 else 0)
    obrys = maska.getbbox()
    if obrys:
        m = 12
        obraz = obraz.crop((max(0, obrys[0] - m), max(0, obrys[1] - m),
                            min(obraz.width, obrys[2] + m), min(obraz.height, obrys[3] + m)))
    os.makedirs(os.path.dirname(png), exist_ok=True)
    obraz.save(png)
    return {"ok": True, "png": png, "strona": nr, "rozmiar": list(obraz.size)}


def obrazyPdf(zrodlo, md, obrazy, folder):
    """Dokleja obrazy stron PDF i znaczniki podgladu stron z grafika.

    Zwraca (md, czy_znaczniki_stron, numery stron z podgladem, klucze powtarzalnych).
    """
    import pypdfium2 as pdfium
    import pypdfium2.raw as pdfium_c

    pdf = pdfium.PdfDocument(zrodlo)
    tymczasowy = tempfile.mkdtemp(prefix="fmd-")
    podglady = []
    try:
        obiektyStron = [obiektyStrony(pdf[i]) for i in range(len(pdf))]
        powt = powtarzalne(obiektyStron)
        naStronie = []
        for i in range(len(pdf)):
            strona = pdf[i]
            sciezki, obrazki, _, _ = analizaStrony(obiektyStron[i], powt, strona.get_cropbox())
            if obrazki or sciezki >= MIN_SCIEZEK:
                podglady.append(i + 1)
            lista = []
            pominRodzica = False
            for obj in strona.get_objects():
                if obj.level == 0:
                    try:
                        pominRodzica = kluczObiektu({"typ": obj.type, "ramka": tuple(obj.get_bounds())}) in powt
                    except Exception:
                        pominRodzica = False
                if pominRodzica or obj.type != pdfium_c.FPDF_PAGEOBJ_IMAGE:
                    continue
                try:
                    szer, wys = obj.get_px_size()
                    if szer < MIN_BOK_OBRAZU or wys < MIN_BOK_OBRAZU:
                        continue
                    prefiks = os.path.join(tymczasowy, "x")
                    obj.extract(prefiks, fb_format="png")
                    plik = next(p for p in os.listdir(tymczasowy) if p.startswith("x."))
                    pelna = os.path.join(tymczasowy, plik)
                    with open(pelna, "rb") as f:
                        dane = f.read()
                    os.remove(pelna)
                    sciezka = obrazy.zapisz(dane, plik.split(".", 1)[1])
                    if sciezka and sciezka not in lista:
                        lista.append(sciezka)
                except Exception:
                    continue
            naStronie.append(lista)

        tekstyStron = md.split("\f") if "\f" in md else None
        # Skan bez warstwy tekstowej: renderuj strony bez tekstu i bez obrazów.
        for i, lista in enumerate(naStronie):
            pustyTekst = (tekstyStron[i].strip() == "") if tekstyStron and i < len(tekstyStron) else not md.strip()
            if pustyTekst and not lista:
                obraz = pdf[i].render(scale=2).to_pil()
                bufor = os.path.join(tymczasowy, "render.png")
                obraz.save(bufor)
                with open(bufor, "rb") as f:
                    sciezka = obrazy.zapisz(f.read(), "png")
                if sciezka:
                    lista.append(sciezka)
                    if i + 1 in podglady:
                        podglady.remove(i + 1)  # cala strona juz jest obrazem
    finally:
        shutil.rmtree(tymczasowy, ignore_errors=True)
        pdf.close()

    def linki(nr, lista):
        return "\n".join(f"![strona {nr}, obraz {k}]({sciezkaMd(p)})" for k, p in enumerate(lista, 1))

    def znacznik(nr):
        sciezka = sciezkaPodgladu(folder, nr).replace("\\", "/")
        return (f"<!-- strona {nr}: grafika (wykres/rysunek/tabela) - podglad strony przyciety "
                f"do grafiki: Read `{sciezka}` -->")

    if tekstyStron:
        czesci = []
        for i in range(len(naStronie)):
            tekst = tekstyStron[i].strip() if i < len(tekstyStron) else ""
            blok = f"<!-- strona {i + 1} -->\n\n{tekst}"
            if naStronie[i]:
                blok += "\n\n" + linki(i + 1, naStronie[i])
            if i + 1 in podglady:
                blok += "\n\n" + znacznik(i + 1)
            czesci.append(blok.rstrip())
        reszta = "\n".join(t.strip() for t in tekstyStron[len(naStronie):] if t.strip())
        return "\n\n".join(czesci) + ("\n\n" + reszta if reszta else ""), True, podglady, powt

    sekcje = [linki(i + 1, l) for i, l in enumerate(naStronie) if l]
    if sekcje:
        md = md.rstrip() + "\n\n## Obrazy z PDF\n\n" + "\n\n".join(sekcje)
    if podglady:
        md = md.rstrip() + "\n\n## Strony z grafika\n\n" + "\n".join(znacznik(nr) for nr in podglady)
    return md, False, podglady, powt


def konwertuj(zrodlo, cache, wymus=False):
    zrodlo = os.path.realpath(zrodlo)
    stat = os.stat(zrodlo)
    folder = folderDla(zrodlo, cache)
    plikMeta = os.path.join(folder, "meta.json")

    if not wymus and os.path.exists(plikMeta):
        try:
            with open(plikMeta, encoding="utf-8") as f:
                meta = json.load(f)
            if (meta.get("wersja") == WERSJA_CACHE and meta.get("mtime") == stat.st_mtime_ns
                    and meta.get("rozmiar") == stat.st_size and os.path.exists(meta.get("md", ""))):
                return dict(meta, ok=True, zCache=True)
        except Exception:
            pass

    # Import dopiero tu: trwa kilka sekund, cache go omija.
    with contextlib.redirect_stdout(sys.stderr):
        from markitdown import MarkItDown

        md = MarkItDown(enable_plugins=False).convert(zrodlo, keep_data_uris=True).markdown or ""

    shutil.rmtree(folder, ignore_errors=True)
    os.makedirs(folder, exist_ok=True)
    katalogObrazow = os.path.join(folder, "obrazy")
    obrazy = Obrazy(katalogObrazow)

    md = wyciagnijDataUri(md, obrazy)
    zeZnacznikami = False
    podglady = []
    powt = set()
    if zrodlo.lower().endswith(".pdf"):
        with contextlib.redirect_stdout(sys.stderr):
            md, zeZnacznikami, podglady, powt = obrazyPdf(zrodlo, md, obrazy, os.path.realpath(folder))

    naglowek = (f"<!-- forced-markitdown | zrodlo: {zrodlo.replace(chr(92), '/')} | obrazy: {obrazy.licznik}"
                + (f" w {katalogObrazow.replace(chr(92), '/')} (Read na sciezce obrazu pokaze go)" if obrazy.licznik else "")
                + " -->")
    tresc = naglowek + "\n\n" + md.strip() + "\n"
    linie = tresc.split("\n")
    strony = [i + 1 for i, l in enumerate(linie) if re.fullmatch(r"<!-- strona \d+ -->", l)] if zeZnacznikami else []

    nazwaMd = os.path.splitext(os.path.basename(zrodlo))[0] + ".md"
    plikMd = os.path.join(folder, nazwaMd)
    with open(plikMd, "w", encoding="utf-8", newline="\n") as f:
        f.write(tresc)

    meta = {
        "wersja": WERSJA_CACHE, "zrodlo": zrodlo, "mtime": stat.st_mtime_ns, "rozmiar": stat.st_size,
        "md": plikMd, "folder": os.path.realpath(folder), "obrazyDir": katalogObrazow,
        "obrazy": obrazy.licznik, "strony": strony, "linie": len(linie), "znaki": len(tresc),
        "podglady": podglady, "powtarzalne": sorted(powt),
    }
    with open(plikMeta, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False)
    return dict(meta, ok=True, zCache=False)


def zeStdin(nazwa, cache):
    dane = base64.b64decode(sys.stdin.buffer.read())
    nazwa = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", os.path.basename(nazwa)) or "zalacznik.pdf"
    katalog = os.path.join(cache, "zalaczniki", hashlib.sha1(dane).hexdigest()[:12])
    os.makedirs(katalog, exist_ok=True)
    plik = os.path.join(katalog, nazwa)
    if not os.path.exists(plik):
        with open(plik, "wb") as f:
            f.write(dane)
    wynik = konwertuj(plik, cache)
    wynik["katalogZalacznika"] = os.path.realpath(katalog)
    return wynik


def rozmiarKatalogu(katalog):
    pliki, bajty = 0, 0
    for korzen, _, nazwy in os.walk(katalog):
        for n in nazwy:
            if n.endswith(".md"):
                pliki += 1
            try:
                bajty += os.path.getsize(os.path.join(korzen, n))
            except OSError:
                pass
    return pliki, bajty


def info(cache):
    from importlib.metadata import version

    pliki, bajty = rozmiarKatalogu(cache)
    return {"ok": True, "python": sys.executable, "wersjaPython": sys.version.split()[0],
            "markitdown": version("markitdown"), "cache": cache, "plikiMd": pliki, "bajty": bajty}


def czysc(cache):
    pliki, bajty = rozmiarKatalogu(cache)
    shutil.rmtree(cache, ignore_errors=True)
    return {"ok": True, "cache": cache, "plikiMd": pliki, "bajty": bajty}


def selftest():
    """Generuje PPTX z obrazem i PDF-skan, konwertuje, sprawdza wynik."""
    from PIL import Image
    from pptx import Presentation
    from pptx.util import Inches

    tmp = tempfile.mkdtemp(prefix="fmd-test-")
    cache = os.path.join(tmp, "cache")
    try:
        png = os.path.join(tmp, "czerwony.png")
        Image.new("RGB", (120, 80), "red").save(png)

        pptx = os.path.join(tmp, "slajdy test.pptx")
        prez = Presentation()
        slajd = prez.slides.add_slide(prez.slide_layouts[5])
        slajd.shapes.title.text = "Tytul slajdu"
        slajd.shapes.add_picture(png, Inches(1), Inches(2))
        prez.save(pptx)

        pdf = os.path.join(tmp, "skan.pdf")
        Image.new("RGB", (400, 300), "blue").save(pdf)

        w = konwertuj(pptx, cache)
        tresc = open(w["md"], encoding="utf-8").read()
        assert "Tytul slajdu" in tresc, tresc
        assert w["obrazy"] == 1 and "base64" not in tresc, tresc
        assert os.path.exists(os.path.join(w["obrazyDir"], "obraz_001.png"))
        assert konwertuj(pptx, cache)["zCache"] is True

        w = konwertuj(pdf, cache)
        tresc = open(w["md"], encoding="utf-8").read()
        assert w["obrazy"] == 1, tresc
        assert "strona 1" in tresc, tresc

        assert w["podglady"] == [], w  # skan: strona juz wyrenderowana w calosci

        # Wektorowy wykres na srodku strony A4 -> znacznik + podglad przyciety do wykresu.
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        wektor = os.path.join(tmp, "wykres.pdf")
        fig = plt.figure(figsize=(8.27, 11.69))
        fig.text(0.1, 0.8, "Rys. 1 pokazuje wzrost temperatury w czasie pomiaru.")
        ax = fig.add_axes((0.3, 0.4, 0.3, 0.2))
        ax.plot([0, 1, 2, 3], [1, 3, 2, 4])
        ax.set_xlabel("czas [s]")
        fig.savefig(wektor)
        plt.close(fig)

        w = konwertuj(wektor, cache)
        tresc = open(w["md"], encoding="utf-8").read()
        assert w["podglady"] == [1], w
        assert "strona_001.png" in tresc, tresc
        png = os.path.join(w["folder"], "strony", "strona_001.png")
        p = podglad(png, cache)
        assert p["ok"] and os.path.exists(png), p
        szer, wys = p["rozmiar"]
        assert abs(szer / wys - 8.27 / 11.69) > 0.15, p  # nie cala strona A4
        assert not podglad(os.path.join(tmp, "strony", "strona_001.png"), cache)["ok"]  # poza cache

        def obiekt(ramka):
            return {"typ": 2, "ramka": ramka, "sciezki": 1, "obrazki": 0}

        strony = [[obiekt((0, 800, 100, 840)), obiekt((10, 10 + i * 50, 200, 300))] for i in range(3)]
        assert powtarzalne(strony) == {(2, 0, 800, 100, 840)}, powtarzalne(strony)  # logo tak, wykresy nie
        assert powtarzalne(strony[:1]) == set()

        assert DATA_URI.sub("X", "![a](data:image/png;base64,QUJD)") == "![a](X)"
        assert sciezkaMd(r"C:\a b\c.png") == "<C:/a b/c.png>"
        return {"ok": True, "selftest": "zaliczony"}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv):
    cache = DOMYSLNY_CACHE
    if "--cache" in argv:
        i = argv.index("--cache")
        cache = argv[i + 1]
        del argv[i:i + 2]
    wymus = "--wymus" in argv
    argv = [a for a in argv if a != "--wymus"]
    komenda = argv[0] if argv else ""
    try:
        if komenda == "plik" and len(argv) > 1:
            if not os.path.isfile(argv[1]):
                return {"ok": False, "blad": f"brak pliku: {argv[1]}"}
            return konwertuj(argv[1], cache, wymus)
        if komenda == "podglad" and len(argv) > 1:
            with contextlib.redirect_stdout(sys.stderr):
                return podglad(argv[1], cache)
        if komenda == "stdin" and len(argv) > 1:
            return zeStdin(argv[1], cache)
        if komenda == "info":
            return info(cache)
        if komenda == "czysc":
            return czysc(cache)
        if komenda == "selftest":
            return selftest()
        return {"ok": False, "blad": "uzycie: plik <sciezka> | podglad <png> | stdin <nazwa> | info | czysc | selftest"}
    except AssertionError as e:
        return {"ok": False, "blad": f"selftest nie przeszedl: {str(e)[:500]}"}
    except Exception as e:
        return {"ok": False, "blad": f"{type(e).__name__}: {str(e)[:500]}"}


if __name__ == "__main__":
    wypisz(main(sys.argv[1:]))
