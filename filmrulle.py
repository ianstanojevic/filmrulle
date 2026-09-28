# -*- coding: utf-8 -*-
"""Filmrulle — en fristående fotoredigerare för analoga/retro filmsimuleringar.

Standalone tool: ingen delad kod med något annat projekt.

Designnot: skalet är medvetet byggt för att INTE likna författarens övriga
tkinter-verktyg. Där de är mörka, bärnstensfärgade, sans-serif med versala
mikroetiketter och en pixelritad ikon — är det här ett *ljust galleri*: fotot
hänger som en monterad kopia på matt kartong, typografin är serif (som
bildtexter i en fotobok), enda accenten är oxblod, reglagen är egenritade spår
istället för tk.Scale, och titelbaren lämnas ljus. Samma teknik, obesläktat
utseende.

All bildbehandling görs lokalt i numpy (float32, 0–1) + Pillow. Filmnamnen är
egna beskrivningar av *looken*. Pipelinen (``process``) är stegvis och delad av
alla presets; varje preset är bara en uppsättning parametrar (``Grade``).
"""

import base64
import io
import itertools
import json
import math
import os
import queue
import re
import sys
import threading
import time
import tkinter as tk
import traceback
from collections import OrderedDict
from dataclasses import asdict, dataclass, field, fields as dc_fields, replace
from functools import lru_cache
from tkinter import filedialog, font as tkfont, messagebox, simpledialog

import numpy as np
from PIL import Image, ImageFilter, ImageOps, ImageTk

# mjuka beroenden: HEIC (pillow-heif) och RAW (rawpy) — appen funkar utan dem,
# men kan då inte öppna de formaten
try:
    import pillow_heif
    pillow_heif.register_heif_opener()
    HEIF_OK = True
except Exception:      # noqa: BLE001
    HEIF_OK = False
try:
    import rawpy
    RAW_OK = True
except Exception:      # noqa: BLE001
    RAW_OK = False

VERSION = "2.1"
SESSION_EXT = ".filmrulle"
PRESETS_FILE = os.path.join(os.path.expanduser("~"),
                            ".filmrulle_presets.json")
SETTINGS_FILE = os.path.join(os.path.expanduser("~"),
                             ".filmrulle_settings.json")
ERROR_LOG = os.path.join(os.path.expanduser("~"), ".filmrulle_errors.log")
RAW_EXTS = {".cr2", ".cr3", ".nef", ".arw", ".dng", ".raf", ".orf", ".rw2"}
HEIF_EXTS = {".heic", ".heif"}


def _write_json_atomic(path, data):
    """Skriv JSON via temp-fil + os.replace. Ett avbrott mitt i skrivningen
    (krasch, full disk, strömavbrott) lämnar då den GAMLA filen orörd — med
    en direkt skrivning blev den halvskriven och oläsbar, vilket för
    presets-filen betydde att alla egna looks försvann vid nästa start."""
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def load_settings():
    """Små appinställningar som ska minnas mellan körningar (tema, fönster,
    exportformat). Egen fil, separat från presets — olika livslängd/syfte.
    Returnerar ALLTID en dict: giltig JSON av fel form ("[]", "null") fick
    tidigare appen att krascha vid start."""
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            d = json.load(f)
    except Exception:      # noqa: BLE001 — ingen/trasig fil = default
        return {}
    return d if isinstance(d, dict) else {}


def save_settings(d):
    try:
        _write_json_atomic(SETTINGS_FILE, d)
    except Exception:      # noqa: BLE001 — kosmetiskt, aldrig kritiskt
        pass


def log_error(context, exc_info=None):
    """Skriv ett undantag med traceback till ERROR_LOG. Den byggda exe:n
    (--windowed) saknar konsol, så utan loggen försvinner varje fel i en
    Tk-callback eller arbetartråd spårlöst — användaren ser bara att något
    'inte händer'. Roteras vid ~256 KB. Får aldrig själv kasta."""
    try:
        et, ev, tb = exc_info or sys.exc_info()
        if et is None:
            return
        text = "".join(traceback.format_exception(et, ev, tb))
        if os.path.exists(ERROR_LOG) and os.path.getsize(ERROR_LOG) > 256_000:
            os.replace(ERROR_LOG, ERROR_LOG + ".1")
        with open(ERROR_LOG, "a", encoding="utf-8") as f:
            f.write(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')} "
                    f"v{VERSION} [{context}]\n{text}\n")
    except Exception:      # noqa: BLE001
        pass


def _finite(v, default):
    """float(v) om det är ett ändligt tal, annars `default` (skyddar mot
    NaN/inf/strängar i handredigerade eller trasiga JSON-filer)."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return f if math.isfinite(f) else default


def _magic(path):
    """Filens första bytes — filändelsen ljuger ibland, innehållet gör det inte."""
    try:
        with open(path, "rb") as f:
            return f.read(4)
    except OSError:
        return b""


def _route(path):
    """Välj avkodare på filens INNEHÅLL, inte bara ändelsen. Returnerar
    (använd_rawpy, pillow_fallback_tillåten).

    - En fil kan heta .dng men vara en vanlig JPEG (kameraappar sparar om
      bilden vid överföring till mobil utan att byta ändelse) → Pillow.
    - Ett TIFF-baserat RAW-format (DNG/NEF/ARW/CR2…) som LibRaw inte klarar
      får INTE falla tillbaka på Pillow: Pillow öppnar då filens inbäddade
      förhandsvisning (för en GR III-DNG 160×120 px) och man får tyst en
      frimärksstor bild istället för ett fel."""
    ext = os.path.splitext(path)[1].lower()
    if ext not in RAW_EXTS:
        return False, True
    m = _magic(path)
    if m[:3] == b"\xff\xd8\xff" or m == b"\x89PNG":
        return False, True
    return True, m[:2] not in (b"II", b"MM")


def _open_raw(path):
    if not RAW_OK:
        raise RuntimeError("RAW-stöd saknas (installera rawpy)")
    return rawpy.imread(path)


def load_rgb(path):
    """Öppna en bildfil som en float32 RGB-array (0–1), fullt bitdjup bevarat.
    Vanliga format (+ HEIC om pillow-heif finns) via Pillow i 8 bitar; RAW via
    rawpy i **16 bitar** — kamerans sensor levererar 12–14 bitar per kanal,
    och en tidig avrundning till 8 bitar (256 nivåer) kastar tondjup som
    annars finns kvar vid kraftiga skugglyft/highlight-recovery. Används för
    Spara/Exportera (full upplösning); importen använder `load_preview`."""
    use_raw, fallback_ok = _route(path)
    if use_raw:
        try:
            with _open_raw(path) as raw:
                rgb16 = raw.postprocess(use_camera_wb=True, output_bps=16)
            a = rgb16.astype(np.float32)
            del rgb16
            a *= np.float32(1.0 / 65535.0)     # in-place: ingen extra 290 MB-kopia
            return a
        except Exception:      # noqa: BLE001
            if not fallback_ok:
                raise
    ext = os.path.splitext(path)[1].lower()
    if ext in HEIF_EXTS and not HEIF_OK:
        raise RuntimeError("HEIC-stöd saknas (installera pillow-heif)")
    # `with` stänger filen direkt — flerbildsformat (MPO-JPEG från många
    # kameror, TIFF, HEIC) höll annars filen låst i Windows tills GC:n kom
    with Image.open(path) as im:
        rgb = ImageOps.exif_transpose(im).convert("RGB")
    a = np.asarray(rgb, np.float32)
    a /= 255.0
    return a


def load_preview(path, max_side=None):
    """Snabb och minnessnål inläsning för IMPORT → (PIL RGB ≤ max_side px,
    fullbredd, fullhöjd). Förhandsvisningen är ändå 8 bitar och ≤1400 px, så
    att avkoda full upplösning i float32 (som importen gjorde) var slöseri —
    ~290 MB temporärt per GR III-DNG. RAW avkodas nu i halv upplösning/8
    bitar (verifierat: medelfärg inom <1/255 av full avkodning), JPEG skalas
    redan i DCT-steget via draft(). Fullmåtten (för info-raden) läses ur
    filhuvudet, med hänsyn till orientering. Uppmätt på 7 riktiga GR III-/
    iPhone-filer: 6.0 → 1.8 s totalt (DNG 1.8 → 0.6 s, JPEG 0.6 → 0.13 s)."""
    max_side = max_side or PREVIEW_MAX
    use_raw, fallback_ok = _route(path)
    if use_raw:
        try:
            with _open_raw(path) as raw:
                s = raw.sizes
                full_w, full_h = ((s.height, s.width) if s.flip in (5, 6)
                                  else (s.width, s.height))
                rgb = raw.postprocess(use_camera_wb=True, half_size=True,
                                      output_bps=8)
            im = Image.fromarray(rgb)
            im.thumbnail((max_side, max_side), Image.LANCZOS)
            return im, full_w, full_h
        except Exception:      # noqa: BLE001
            if not fallback_ok:
                raise
    ext = os.path.splitext(path)[1].lower()
    if ext in HEIF_EXTS and not HEIF_OK:
        raise RuntimeError("HEIC-stöd saknas (installera pillow-heif)")
    with Image.open(path) as im:
        full_w, full_h = im.size
        try:
            orient = im.getexif().get(0x0112, 1)
        except Exception:      # noqa: BLE001 — trasig EXIF = ingen rotation
            orient = 1
        if orient in (5, 6, 7, 8):
            full_w, full_h = full_h, full_w
        if im.format in ("JPEG", "MPO"):
            k = max_side / max(im.size)
            if k < 1.0:
                im.draft("RGB", (max(1, int(im.size[0] * k)),
                                 max(1, int(im.size[1] * k))))
        out = ImageOps.exif_transpose(im).convert("RGB")
    out.thumbnail((max_side, max_side), Image.LANCZOS)
    return out, full_w, full_h


def as_float01(arr):
    """uint8-bild → float32 0–1 (förhandsvisningar lagras som uint8 för att
    ta 4× mindre RAM); float-arrayer passerar orörda."""
    if arr is None or arr.dtype != np.uint8:
        return arr
    a = arr.astype(np.float32)
    a /= 255.0
    return a


# EXIF-taggar som bärs över till exporten (IFD0). Resten av IFD0 i en RAW/
# TIFF-källa är strukturtaggar (bredd, strip-offsets, SubIFDs …) som skulle
# bli skräp i en JPEG. MakerNote/Interop släpps: tillverkarnas MakerNotes
# innehåller ofta ABSOLUTA filoffsets som blir fel när blocket flyttas, och
# de spräcker lätt JPEG:ens 64 KB-gräns för EXIF.
_EXIF_KEEP = (0x010F, 0x0110, 0x0132, 0x013B, 0x8298)   # Make Model DateTime Artist Copyright
_EXIF_DROP_SUB = {0x927C, 0xA005}                          # MakerNote, Interop-pekare


def read_meta(path):
    """EXIF + ICC-profil ur källfilen, för att bäras över till exporten.
    Utan dem tappade varje export tagningsdatum/kamera (bildbibliotek sorterar
    på DateTimeOriginal) och — för iPhone-bilder i Display P3 — färgprofilen,
    så exporten tolkades som sRGB och såg urblekt ut. Aldrig ett undantag:
    saknad/trasig metadata = exportera utan."""
    meta = {"exif": None, "icc": None}
    try:
        with Image.open(path) as im:
            icc = im.info.get("icc_profile")
            if icc:
                meta["icc"] = icc
            src = im.getexif()
            dst = Image.Exif()
            for t in _EXIF_KEEP:
                if t in src:
                    dst[t] = src[t]
            dst[0x0112] = 1                   # pixlarna är redan uppräta
            dst[0x0131] = f"Filmrulle {VERSION}"
            sub = {k: v for k, v in src.get_ifd(0x8769).items()
                   if k not in _EXIF_DROP_SUB}
            if sub:
                dst[0x8769] = sub
            gps = src.get_ifd(0x8825)
            if gps:
                dst[0x8825] = dict(gps)
            b = dst.tobytes()
            if len(b) < 60000:                # JPEG APP1 rymmer max ~64 KB
                meta["exif"] = b
    except Exception:      # noqa: BLE001 — RAW som Pillow inte kan läsa m.m.
        pass
    return meta


def save_image_file(pil, path, quality=95, meta=None):
    """Skriv en bild med format ur filändelsen (JPEG = vald kvalitet, PNG/
    TIFF = förlustfritt, TIFF LZW), med källans EXIF + ICC. Skrivs till en
    temporär fil som byts in först när den är komplett — en avbruten export
    lämnar aldrig en halv, trasig bildfil med rätt namn."""
    ext = os.path.splitext(path)[1].lower()
    fmt = Image.registered_extensions().get(ext)
    if fmt is None:
        raise ValueError(f"Okänt filformat: {ext or '(ingen ändelse)'}")
    meta = meta or {}
    kw = {}
    if meta.get("icc") and fmt in ("JPEG", "PNG", "TIFF"):
        kw["icc_profile"] = meta["icc"]
    if meta.get("exif") and fmt in ("JPEG", "PNG", "TIFF"):
        kw["exif"] = meta["exif"]
        if fmt == "TIFF":
            # libtiff kan inte skriva de nästlade EXIF-/GPS-IFD:erna som
            # Pillow skickar vidare ("Bad LONG8 … EXIFIFDOffset") → TIFF
            # får bara IFD0-taggarna (kamera, datum, orientering)
            flat = Image.Exif()
            flat.load(meta["exif"])
            for t in (0x8769, 0x8825):
                flat.pop(t, None)
            kw["exif"] = flat.tobytes()
    if fmt == "JPEG":
        kw.update(quality=int(quality), subsampling=0)
    elif fmt == "TIFF":
        kw["compression"] = "tiff_lzw"
    tmp = path + ".part"
    try:
        try:
            pil.save(tmp, format=fmt, **kw)
        except (ValueError, OSError, RuntimeError):
            if "exif" not in kw:
                raise
            kw.pop("exif")                    # ovanlig EXIF som formatet vägrar
            pil.save(tmp, format=fmt, **kw)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass

# ----------------------------------------------------------------- palett
# Två teman med IDENTISKA nycklar. Namnen nedan (WALL, PAPER, …) är globala
# och läses vid *widget-bygge* (körtid), så ett tembyte + ombygge räcker för
# att hela skalet ska byta färg. Ljus = galleri/print (husstilens motsats);
# mörk = samma galleri men nattläge, skonsammare vid redigering i mörker.
THEMES = {
    "light": dict(
        WALL="#d7d2c6",      # fotoväggen bakom kopian
        PAPER="#e9e5dc",     # ram/chrome-bakgrund (matt kartong)
        PAPER2="#f3f0e9",    # paneler/kort
        MAT="#faf8f3",       # passepartout runt fotot (nära vitt)
        SHADOW="#b0aa9c",    # slagskugga bakom monterat foto
        INK="#211d18",       # text, varmt nära-svart
        INK2="#575147",      # sekundär text
        FAINT="#7d7568",     # svag text — mörkad för läsbar kontrast (~3.5:1)
        LINE="#cfc9bc",      # hårlinjer
        ACCENT="#7c3a34",    # oxblod — aktiv film, spårfyllning
        ACCENT_HI="#9a4a42",
        ONACCENT="#faf8f3",  # text ovanpå accent (ljus)
        GOOD="#4f7a4a",      # utvald (pick) — dämpad grön
        BAD="#a5524b",       # ratad (reject) — dämpad röd
        CMP="#af8a2e"),      # jämförelsemål (höger bild) — senapsgul
    "dark": dict(
        WALL="#131109",      # klart mörkare än PAPER — fotoväggen ska läsas
        PAPER="#211e18",     # som ett separat rum, inte smälta in i chromen
        PAPER2="#2a2620",
        MAT="#37332c",
        SHADOW="#0a0906",
        INK="#ece6d8",
        INK2="#b0a996",
        FAINT="#847c6a",     # något ljusare — sekundärtext var svårläst
        LINE="#443e33",      # tydligare hårlinjer/kortkanter i mörkret
        ACCENT="#b5675c",
        ACCENT_HI="#c9776b",
        ONACCENT="#f2ece0",
        GOOD="#77a56e",
        BAD="#c76f66",
        CMP="#d1a83e"),      # ljusare senapsgul, syns mot mörk bakgrund
}
THEME_NAME = "light"


def apply_theme(name):
    """Injicera ett temas färger som modul-globaler (WALL, PAPER, …)."""
    global THEME_NAME
    THEME_NAME = name if name in THEMES else "light"
    globals().update(THEMES[THEME_NAME])


apply_theme("light")   # måste köras innan klasserna definieras (default-args)

PREVIEW_MAX = 1400
THUMB_SRC = 240
CARD_W, CARD_IMG_H = 118, 78
ROLL_W, ROLL_H = 66, 46         # tumnaglar i foto-rullen (session)
DEFAULT_GS = 1.5        # kornstorlek (px) i standardläge
# tuple-mönster (inte en mellanslagsseparerad sträng) — den formen håller
# multi-val stabilt i Windows-dialogen. MEN för många mönster i EN grupp har
# visat sig få samma dialog att sluta visa filer alls (bara mappar syns) —
# därför uppdelat i flera mindre filtergrupper istället för en jättegrupp.
IMG_TYPES = [("Bilder", ("*.jpg", "*.jpeg", "*.png", "*.bmp", "*.tif",
                         "*.tiff", "*.webp")),
             ("RAW / HEIC", ("*.heic", "*.heif", "*.cr2", "*.cr3", "*.nef",
                             "*.arw", "*.dng", "*.raf", "*.orf", "*.rw2")),
             ("Filmrulle-projekt", "*" + SESSION_EXT),
             ("Alla filer", "*.*")]
IMPORT_EXTS = ({".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp"}
               | HEIF_EXTS | RAW_EXTS)


def mouse_buttons(platform=sys.platform, tk_version=tk.TkVersion):
    """(högerklick, mittenklick) som Tk-händelser. Tk 8.6 på macOS numrerar
    HÖGER-knappen 2 och mittenknappen 3 — omvänt mot Windows/X11 (ändrat i
    Tk 8.7). Hårdkodat <Button-3>/<Button-2> gjorde att högerklick i
    Mac-bygget började panorera och 'högerklick = ta bort' aldrig nåddes."""
    if platform == "darwin" and tk_version < 8.7:
        return "<Button-2>", "<Button-3>"
    return "<Button-3>", "<Button-2>"


RIGHT_BTN, MIDDLE_BTN = mouse_buttons()


def wheel_units(delta, platform=sys.platform):
    """MouseWheel-delta i 'Windows-enheter' (120 per hjulsteg). Tk på Windows
    ger ±120 per steg, men Tk 8.6 på macOS ±1 (och täta småvärden från
    styrplattan) — utan normalisering var scroll-zoomen i Mac-bygget i
    praktiken död (1.0015**1 ≈ 0.15 % per steg)."""
    if platform == "darwin" and abs(delta) < 120:
        return delta * 30
    return delta


def _resolve_family(cands):
    """Välj första installerade typsnittsfamiljen (kräver att en Tk-root finns)."""
    try:
        fams = {f.lower() for f in tkfont.families()}
    except Exception:
        return cands[0]
    for c in cands:
        if c.lower() in fams:
            return c
    return cands[0]


# =====================================================================
#  Filmgrade — parametrar för en look
# =====================================================================

@dataclass
class Grade:
    exposure: float = 0.0
    temp: float = 0.0
    tint: float = 0.0
    contrast: float = 0.0
    saturation: float = 0.0
    fade: float = 0.0
    grain: float = 0.0
    vignette: float = 0.0
    halation: float = 0.0
    split: float = 0.0
    shadow_tint: tuple = (0.0, 0.0, 0.0)
    highlight_tint: tuple = (0.0, 0.0, 0.0)
    clarity: float = 0.0         # lokal mellantonskontrast (-100..100)
    sharpen: float = 0.0         # oskarp mask (0..100)
    bw: bool = False
    tone: tuple = (0.0, 0.0, 0.0)
    curves: dict = field(default=None)
    # kornstorlek i px (1 = per pixel, större = grövre). Standard = UI:ts
    # standard: filmbyte läser nu filmens egen kornstorlek/struktur till
    # reglagen (tidigare skrevs de alltid över med 1.5/0, så Tri-X:s 1.6
    # var död kod och sparade presets tappade sin kornkaraktär)
    grain_size: float = DEFAULT_GS
    grain_rough: float = 0.0     # 0 = mjukt/molnigt, 100 = hårt/gryning korn
    user_curve: list = field(default=None)   # användarens tonkurva [(x,y)…]


ADJ_FIELDS = ("exposure", "temp", "tint", "contrast", "saturation",
              "clarity", "sharpen", "fade", "grain", "vignette", "halation")


def _clampf(v, lo, hi):
    return lo if v < lo else hi if v > hi else v


FILMS = [
    ("original", "Original", "orörd", Grade()),
    ("portra", "Porträtt 400", "varm · mjuk", Grade(
        temp=16, tint=6, contrast=-8, saturation=-6, fade=10,
        highlight_tint=(0.05, 0.03, -0.02), shadow_tint=(0.02, 0.0, 0.03),
        split=0.35, grain=8,
        curves={"r": [(0, 0.02), (1, 0.98)], "b": [(0, 0.04), (1, 0.94)]})),
    ("guld", "Guld 200", "gyllene · nostalgi", Grade(
        temp=17, tint=5, contrast=5, saturation=6, fade=5,
        highlight_tint=(0.05, 0.03, -0.03), shadow_tint=(0.02, 0.01, -0.01),
        split=0.28, grain=10,
        curves={"b": [(0, 0.0), (0.5, 0.47), (1, 0.92)]})),
    ("velvia", "Velvia", "mättad · knivskarp", Grade(
        temp=-6, contrast=18, saturation=22, fade=0, grain=6,
        curves={"r": [(0, 0), (0.25, 0.23), (0.75, 0.79), (1, 1)],
                "g": [(0, 0), (0.25, 0.24), (0.75, 0.78), (1, 1)]})),
    ("krom", "Krom 64", "ren · sval", Grade(
        temp=-14, tint=-4, contrast=14, saturation=8, grain=6,
        highlight_tint=(-0.03, 0.0, 0.05))),
    ("natt", "Natt 800T", "tungsten · halation", Grade(
        temp=-30, tint=6, contrast=10, saturation=6, fade=8,
        halation=60, grain=16, shadow_tint=(-0.05, -0.02, 0.10),
        highlight_tint=(0.04, 0.0, 0.02), split=0.5)),
    ("blekt", "Blekt", "matt · dammig", Grade(
        temp=4, contrast=-16, saturation=-22, fade=42, grain=14,
        highlight_tint=(0.04, 0.03, 0.0), shadow_tint=(0.03, 0.02, 0.01),
        split=0.3)),
    ("ektar", "Ektar 100", "mättad · naturtrogen", Grade(
        temp=6, contrast=12, saturation=18, grain=4,
        highlight_tint=(0.03, 0.01, -0.03), split=0.25,
        curves={"r": [(0, 0.0), (0.5, 0.52), (1, 1.0)]})),
    ("superia", "Superia 400", "sval · pastell", Grade(
        temp=-4, tint=-8, contrast=8, saturation=6, grain=13,
        shadow_tint=(-0.02, 0.04, -0.01), highlight_tint=(0.0, 0.02, -0.01),
        split=0.4)),
    ("ektachrome", "Ektachrome E100", "dia · redaktionell", Grade(
        temp=-8, contrast=16, saturation=12, grain=5,
        highlight_tint=(-0.02, 0.0, 0.04),
        curves={"b": [(0, 0.02), (0.5, 0.52), (1, 1.0)]})),
    ("polaroid", "Polaroid SX-70", "mjuk · drömsk", Grade(
        temp=8, contrast=-20, saturation=-8, fade=30, vignette=24, grain=10,
        highlight_tint=(0.05, 0.03, 0.02), shadow_tint=(0.03, 0.0, 0.04),
        split=0.3)),
    ("classicchrome", "Classic Chrome", "dov · reportage", Grade(
        temp=-3, tint=-2, contrast=10, saturation=-24, fade=8, grain=8,
        shadow_tint=(-0.02, 0.0, 0.025), split=0.3,
        curves={"b": [(0, 0.02), (0.5, 0.48), (1, 0.94)]})),
    ("classicneg", "Classic Neg", "teal · bärnsten", Grade(
        temp=5, tint=-2, contrast=16, saturation=10, fade=8, grain=10,
        shadow_tint=(-0.05, 0.0, 0.05), highlight_tint=(0.06, 0.03, -0.05),
        split=0.5, curves={"b": [(0, 0.06), (0.5, 0.48), (1, 0.9)]})),
    ("eterna", "Eterna", "cinematisk · flat", Grade(
        temp=-2, tint=-5, contrast=-4, saturation=-30, fade=16, grain=8,
        shadow_tint=(0.0, 0.02, 0.02), split=0.3,
        curves={"r": [(0, 0.03), (0.75, 0.72), (1, 0.9)],
                "g": [(0, 0.03), (1, 0.92)],
                "b": [(0, 0.05), (1, 0.9)]})),
    ("nostalgic", "Nostalgic Neg", "varm · 70-tal", Grade(
        temp=16, tint=4, contrast=-2, saturation=4, fade=20, grain=11,
        highlight_tint=(0.06, 0.04, -0.05), shadow_tint=(0.02, 0.0, 0.02),
        split=0.35, curves={"b": [(0, 0.05), (0.5, 0.46), (1, 0.9)]})),
    ("astia", "Astia", "mjuk · hud", Grade(
        temp=-2, tint=2, contrast=-12, saturation=-2, fade=6, grain=6,
        highlight_tint=(0.01, 0.0, 0.01), shadow_tint=(0.0, 0.0, 0.01),
        curves={"r": [(0, 0.02), (1, 0.97)]})),
    ("portra160", "Porträtt 160", "mjuk · subtil", Grade(
        temp=10, tint=3, contrast=-12, saturation=-10, fade=6, grain=4,
        highlight_tint=(0.03, 0.02, -0.01), shadow_tint=(0.01, 0.0, 0.02),
        split=0.3, curves={"r": [(0, 0.02), (1, 0.98)]})),
    ("pro400h", "Pro 400H", "luftig · sval", Grade(
        temp=-5, tint=-4, contrast=-6, saturation=-6, fade=10, grain=6,
        shadow_tint=(-0.03, 0.02, 0.03), highlight_tint=(-0.02, 0.02, 0.04),
        split=0.4, curves={"b": [(0, 0.04), (0.5, 0.5), (1, 0.98)]})),
    ("provia100f", "Provia 100F", "dia · neutral", Grade(
        temp=-2, contrast=14, saturation=12, grain=4,
        highlight_tint=(-0.01, 0.0, 0.02),
        curves={"g": [(0, 0.01), (0.5, 0.5), (1, 0.99)]})),
    ("cinestill50d", "Cinestill 50D", "slät · balanserad", Grade(
        temp=3, contrast=4, saturation=4, grain=3,
        highlight_tint=(0.02, 0.01, 0.0), split=0.2,
        curves={"r": [(0, 0.01), (1, 0.99)]})),
    ("cinestill800t", "Cinestill 800T", "tungsten · röd glöd", Grade(
        temp=-18, tint=4, contrast=8, saturation=8, fade=6,
        halation=75, grain=14, shadow_tint=(-0.04, 0.0, 0.08),
        highlight_tint=(0.05, 0.0, 0.0), split=0.5)),
    ("colorplus200", "ColorPlus 200", "budget · urblekt", Grade(
        temp=12, tint=2, contrast=-4, saturation=-4, fade=22, grain=13,
        highlight_tint=(0.05, 0.03, -0.03), shadow_tint=(0.03, 0.01, 0.0),
        split=0.35, curves={"b": [(0, 0.06), (0.5, 0.45), (1, 0.9)]})),
    ("agfavista", "Agfa Vista", "punchig · magenta", Grade(
        temp=4, tint=4, contrast=10, saturation=16, grain=12,
        shadow_tint=(0.04, -0.02, 0.03), highlight_tint=(0.02, 0.0, -0.02),
        split=0.4)),
    ("hp5", "HP5 Svartvitt", "korn · kontrast", Grade(
        bw=True, contrast=20, fade=6, grain=24)),
    ("trix", "Tri-X 400", "gritty · dokumentär", Grade(
        bw=True, contrast=34, fade=2, grain=32, grain_size=1.6)),
    ("acros", "Acros", "ren · djup kontrast", Grade(
        bw=True, contrast=28, clarity=12, sharpen=20, grain=6)),
]
FILM_BY_KEY = {f[0]: f for f in FILMS}


def film_entry(key):
    """FILMS-posten för `key`, med Original som fallback. En nyckel kan peka
    på en egen preset som raderats (ångra-historik, urklipp, äldre projekt)
    — det gav tidigare KeyError mitt i ett ångra och ett halvt återställt UI."""
    return FILM_BY_KEY.get(key) or FILM_BY_KEY["original"]


def film_slug(key):
    """Filnamnsvänligt filmnamn för exporter: inbyggda filmer använder sin
    nyckel ('velvia'), egna presets sitt namn — inte 'user_1721234567890'."""
    if not key.startswith("user_"):
        return key
    label = film_entry(key)[1] if key in FILM_BY_KEY else ""
    slug = re.sub(r"[^\w-]+", "_", str(label)).strip("_")
    return slug or "egen"


# =====================================================================
#  Egna presets — användarens sparade looks, egna kort i remsan
# =====================================================================

def sanitize_curve(pts):
    """Tolerant tolkning av en tonkurva ([[x,y],…]): skräp slängs, värden
    klämms till 0–1, sorteras på x och punkter med (nästan) samma x slås
    ihop. Dubbla x gav tidigare en nollbred PCHIP-sektion → exploderande
    lutning och en spik/solarisering i bilden. None om < 2 giltiga punkter."""
    if not isinstance(pts, (list, tuple)):
        return None
    clean = []
    for p in pts:
        if not isinstance(p, (list, tuple)) or len(p) < 2:
            continue
        x, y = _finite(p[0], None), _finite(p[1], None)
        if x is None or y is None:
            continue
        clean.append([_clampf(x, 0.0, 1.0), _clampf(y, 0.0, 1.0)])
    clean.sort(key=lambda q: q[0])
    out = []
    for q in clean:
        if out and q[0] - out[-1][0] < 0.005:
            out[-1] = q                      # senare punkt vinner
        else:
            out.append(q)
    return out if len(out) >= 2 else None


def _grade_from_dict(d):
    """Grade ur JSON-dict — tolerant mot okända/gamla fält OCH fel typer.
    Ett fält av fel typ (t.ex. "temp": "varm") klarade sig förut förbi
    inläsningen men kraschade sedan varje rendering med den filmen."""
    g = Grade()
    if not isinstance(d, dict):
        return g
    for f in dc_fields(Grade):
        if f.name not in d:
            continue
        v, cur = d[f.name], getattr(g, f.name)
        if isinstance(cur, bool):
            if isinstance(v, bool):
                setattr(g, f.name, v)
        elif isinstance(cur, (int, float)):
            setattr(g, f.name, _finite(v, cur))
        elif isinstance(cur, tuple):
            if isinstance(v, (list, tuple)) and len(v) == 3:
                setattr(g, f.name, tuple(_finite(t, 0.0) for t in v))
        elif f.name == "curves":
            if isinstance(v, dict):
                cv = {ch: sanitize_curve(v.get(ch)) for ch in "rgb"}
                setattr(g, f.name, {k: c for k, c in cv.items() if c} or None)
        elif f.name == "user_curve":
            setattr(g, f.name, sanitize_curve(v))
    g.grain_size = _clampf(g.grain_size, 1.0, 5.0)
    g.grain_rough = _clampf(g.grain_rough, 0.0, 100.0)
    return g


def load_user_presets():
    """Läs in sparade presets och registrera dem som filmer i remsan.
    Körs vid appstart INNAN korten byggs och sessioner läses (EditState.
    from_dict validerar film_key mot FILM_BY_KEY). Varje post valideras för
    sig — en trasig fil fick tidigare hela appen att krascha vid start."""
    try:
        with open(PRESETS_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:      # noqa: BLE001 — ingen fil = inga presets
        return
    presets = data.get("presets") if isinstance(data, dict) else None
    for p in presets if isinstance(presets, list) else []:
        if not isinstance(p, dict):
            continue
        key, label = p.get("key"), p.get("label")
        if not isinstance(key, str) or not key.startswith("user_") \
                or key in FILM_BY_KEY:
            continue
        label = label if isinstance(label, str) and label.strip() else "Egen"
        entry = (key, label, "egen preset", _grade_from_dict(p.get("grade")))
        FILMS.append(entry)
        FILM_BY_KEY[key] = entry


def save_user_presets():
    presets = [{"key": k, "label": lab, "grade": asdict(g)}
               for k, lab, _s, g in FILMS if k.startswith("user_")]
    try:
        _write_json_atomic(PRESETS_FILE, {"presets": presets})
    except Exception:      # noqa: BLE001
        log_error("save_user_presets")


# =====================================================================
#  Redigeringssession — varje importerat foto bär sin egen look
#  (Lightroom-liknande: importera flera, klicka dig igenom, exportera alla)
# =====================================================================

_uid_seq = itertools.count(1)


NO_CROP = (0.0, 0.0, 1.0, 1.0)


@dataclass
class EditState:
    """Vad som skiljer ett foto från ett annat i rullen: vald film + manuella
    offset ovanpå den, kornstorlek, styrka, beskärning + rät-upp-vinkel. Hålls
    separat från Grade så att UI:ts Track-widgets kan läsas/skrivas rakt av."""
    film_key: str = "original"
    adjust: dict = field(default_factory=lambda: {k: 0.0 for k in ADJ_FIELDS})
    grain_size: float = DEFAULT_GS
    grain_rough: float = 0.0         # kornstruktur (0 = mjukt, 100 = grovt)
    strength: float = 100.0
    crop: tuple = NO_CROP            # (x0,y0,x1,y1) normaliserat, rätad bild
    angle: float = 0.0               # rät-upp-vinkel i grader
    curve: list = field(default=None)             # tonkurva [(x,y)…] eller None

    def clone(self):
        return EditState(
            film_key=self.film_key, adjust=dict(self.adjust),
            grain_size=self.grain_size, grain_rough=self.grain_rough,
            strength=self.strength, crop=tuple(self.crop), angle=self.angle,
            curve=[list(p) for p in self.curve] if self.curve else None)

    def is_default(self):
        return (self.film_key == "original"
                and all(abs(v) < 1e-6 for v in self.adjust.values())
                and abs(self.grain_size - DEFAULT_GS) < 1e-6
                and abs(self.grain_rough) < 1e-6
                and abs(self.strength - 100.0) < 1e-6
                and abs(self.angle) < 1e-6
                and all(abs(a - b) < 1e-6 for a, b in zip(self.crop, NO_CROP))
                and not curve_is_active(self.curve))

    def to_dict(self):
        return {"film_key": self.film_key, "adjust": dict(self.adjust),
                "grain_size": self.grain_size, "grain_rough": self.grain_rough,
                "strength": self.strength,
                "crop": list(self.crop), "angle": self.angle,
                "curve": [list(p) for p in self.curve] if self.curve else None}

    @staticmethod
    def from_dict(d):
        """Tolerant mot trasiga/handredigerade projektfiler: varje fält
        valideras för sig (typ, ändlighet, intervall) och faller annars
        tillbaka på standardvärdet. Tidigare fällde ett enda dåligt fält
        ("crop": null → TypeError) hela projektet, och NaN-värden (som
        JSON tillåter) gav helt svarta bilder. Äldre projekt med
        "local_adjust" ignoreras tyst."""
        e = EditState()
        if not isinstance(d, dict):
            return e
        fk = d.get("film_key")
        e.film_key = fk if isinstance(fk, str) and fk in FILM_BY_KEY \
            else "original"
        adj = d.get("adjust")
        if isinstance(adj, dict):
            e.adjust = {k: _finite(adj.get(k), 0.0) for k in ADJ_FIELDS}
        e.grain_size = _clampf(_finite(d.get("grain_size"), DEFAULT_GS),
                               1.0, 5.0)
        e.grain_rough = _clampf(_finite(d.get("grain_rough"), 0.0), 0.0, 100.0)
        e.strength = _clampf(_finite(d.get("strength"), 100.0), 0.0, 100.0)
        e.crop = _valid_crop(d.get("crop"))
        e.angle = _clampf(_finite(d.get("angle"), 0.0), -45.0, 45.0)
        e.curve = sanitize_curve(d.get("curve"))
        return e


def _resolve_project_path(entry, pdir):
    """Fotots sökväg ur en projektpost: den absoluta om den finns, annars
    den RELATIVA till projektfilen (mappen har flyttats), annars den
    absoluta ändå — så att importen kan rapportera att fotot saknas."""
    p = entry.get("path")
    p = p if isinstance(p, str) else ""
    if p and os.path.exists(p):
        return p
    r = entry.get("rel")
    if isinstance(r, str) and r:
        cand = os.path.normpath(os.path.join(pdir, r))
        if os.path.exists(cand):
            return cand
    return p


def _valid_crop(c):
    """Normaliserad beskärning (x0,y0,x1,y1) i 0–1 med positiv yta, annars
    ingen beskärning."""
    if not isinstance(c, (list, tuple)) or len(c) != 4:
        return NO_CROP
    v = [_finite(t, None) for t in c]
    if None in v:
        return NO_CROP
    x0, y0, x1, y1 = (_clampf(t, 0.0, 1.0) for t in v)
    if x1 - x0 < 0.01 or y1 - y0 < 0.01:
        return NO_CROP
    return (x0, y0, x1, y1)


def curve_is_active(pts):
    """Är kurvan något annat än identitetslinjen?"""
    if not pts or len(pts) < 2:
        return False
    if len(pts) == 2:
        (x0, y0), (x1, y1) = pts[0], pts[-1]
        return not (abs(x0) < 1e-4 and abs(y0) < 1e-4
                    and abs(x1 - 1) < 1e-4 and abs(y1 - 1) < 1e-4)
    return True


@dataclass
class PhotoItem:
    """Ett foto i rullen. Bara nedskalade arbetskopior hålls i RAM — full
    upplösning läses om från disk vid export (se save_image/_batch_worker),
    annars vore en session med många högupplösta foton snabbt flera GB."""
    path: str
    prev_arr: np.ndarray
    thumb_src: np.ndarray
    w: int
    h: int
    edit: EditState = field(default_factory=EditState)
    rating: int = 0                  # -1 = ratad, 0 = neutral, +1 = utvald
    uid: int = field(default_factory=lambda: next(_uid_seq))


def make_photo_item(path, prev_img, full_w, full_h):
    """Bygg ett rullfoto ur en förhandsvisning (PIL). Förhandsvisningen
    lagras som **uint8** (3.9 MB för 1400 px) istället för float32 (15.7 MB)
    — en rulle med 200 foton tog annars ~3 GB RAM bara för dessa. Bara det
    AKTIVA fotot konverteras till float (i `_activate`). Tumnageln (240 px)
    används i varje rullkorts-/filmremsrendering och får förbli float32."""
    th = prev_img.copy()
    th.thumbnail((THUMB_SRC, THUMB_SRC), Image.LANCZOS)
    return PhotoItem(path=path, prev_arr=np.array(prev_img, np.uint8),
                     thumb_src=as_float01(np.array(th, np.uint8)),
                     w=int(full_w), h=int(full_h))


def apply_geometry(arr, crop, angle):
    """Räta upp (rotera) och beskär en float32-bild. Normaliserad crop gör
    resultatet upplösnings-oberoende → samma recept funkar på både
    förhandsvisning och fullupplösning."""
    a = arr
    if abs(angle) >= 1e-3:
        # rotera per kanal i FLOAT ('F'-mode) — den gamla uint8-rundturen
        # kvantiserade en 16-bitars RAW till 8 bitar mitt i pipelinen så
        # fort man rätat upp bilden, vilket kastade precis det tondjup
        # 16-bitars-inläsningen finns till för att bevara
        chans = [Image.fromarray(np.ascontiguousarray(arr[..., i]), mode="F")
                 .rotate(angle, resample=Image.BICUBIC, expand=True)
                 for i in range(3)]
        a = np.stack([np.asarray(c, np.float32) for c in chans], axis=-1)
        np.clip(a, 0.0, 1.0, out=a)   # bicubic kan översvänga vid kanter
    x0, y0, x1, y1 = crop
    if (x0, y0, x1, y1) == NO_CROP:
        return a
    h, w = a.shape[:2]
    cx0 = max(0, min(w - 2, int(round(x0 * w))))
    cy0 = max(0, min(h - 2, int(round(y0 * h))))
    cx1 = max(cx0 + 1, min(w, int(round(x1 * w))))
    cy1 = max(cy0 + 1, min(h, int(round(y1 * h))))
    return a[cy0:cy1, cx0:cx1]


def grade_from_edit(edit):
    """Bygg en klampad Grade från ett EditState. Delas av live-förhandsvisning,
    Spara och Exportera alla så alla tre ger identiskt resultat."""
    g = replace(film_entry(edit.film_key)[3])
    for fkey in ADJ_FIELDS:
        setattr(g, fkey, getattr(g, fkey) + edit.adjust.get(fkey, 0.0))
    g.exposure = _clampf(g.exposure, -3, 3)
    g.contrast = _clampf(g.contrast, -100, 100)
    g.saturation = _clampf(g.saturation, -100, 100)
    g.fade = _clampf(g.fade, 0, 100)
    g.grain = _clampf(g.grain, 0, 100)
    g.vignette = _clampf(g.vignette, 0, 100)
    g.halation = _clampf(g.halation, 0, 100)
    g.clarity = _clampf(g.clarity, -100, 100)
    g.sharpen = _clampf(g.sharpen, 0, 100)
    g.grain_size = _clampf(edit.grain_size, 1.0, 5.0)
    g.grain_rough = _clampf(edit.grain_rough, 0.0, 100.0)
    if curve_is_active(edit.curve):
        g.user_curve = sanitize_curve(edit.curve)
    return g


# =====================================================================
#  Bildpipeline (numpy, float32 0–1)  — oförändrad kärna
# =====================================================================

# Kurvor slås upp i en 65536-LUT istället för np.interp per pixel: ~2× snabbare
# (np.interp konverterar till float64 och binärsöker per värde), och felet —
# indata kvantiseras till 1/65535 — ligger >100× under en 8-bitarsnivå.
_LUT_N = 65536
_LUT_X = np.linspace(0.0, 1.0, _LUT_N)


def _lut_apply(chan, lut):
    """Slå upp en (redan 0–1-klämd) kanal i en _LUT_N-LUT."""
    idx = chan * np.float32(_LUT_N - 1)
    idx += np.float32(0.5)
    return lut[idx.astype(np.uint16)]


@lru_cache(maxsize=64)
def _curve_lut(key):
    """Linjär (filmernas inbakade) kurva → LUT. `key` = tuple av (x, y)."""
    xs = [p[0] for p in key]
    ys = [p[1] for p in key]
    lut = np.interp(_LUT_X, xs, ys).astype(np.float32)
    lut.flags.writeable = False              # delad via cachen
    return lut


@lru_cache(maxsize=32)
def _user_curve_lut(key):
    """Användarens PCHIP-tonkurva → LUT (samma 256-punkters PCHIP som
    editorn ritar, linjärt interpolerad mellan de punkterna)."""
    gx, gy = _pchip_lut(key)
    lut = np.interp(_LUT_X, gx, gy).astype(np.float32)
    lut.flags.writeable = False
    return lut


def _curve_key(pts):
    return tuple((float(p[0]), float(p[1])) for p in pts)


def _apply_curve(chan, pts):
    return _lut_apply(chan, _curve_lut(_curve_key(pts)))


def _pchip_lut(pts, n=256):
    """Mjuk MONOTON kubisk interpolation (Fritsch–Carlson/PCHIP) genom
    kurvpunkterna, utvärderad till en LUT. Monotoniteten garanterar att
    kurvan aldrig översvänger mellan punkterna (ingen ringning/solarisering
    som naturliga kubiska splines kan ge). Delas av BÅDE pipelinen och
    kurveditorns ritning så att linjen på skärmen är exakt den mappning
    som appliceras på bilden. Filmernas inbakade kurvor är fortsatt
    linjära (de är trimmade mot np.interp) — det här gäller bara
    användarens egen tonkurva."""
    xs = np.array([p[0] for p in pts], np.float64)
    ys = np.array([p[1] for p in pts], np.float64)
    gx = np.linspace(0.0, 1.0, n)
    if len(xs) == 2:
        gy = np.interp(gx, xs, ys)
        return gx.astype(np.float32), np.clip(gy, 0, 1).astype(np.float32)
    h = np.diff(xs)
    d = np.diff(ys) / np.maximum(h, 1e-9)
    m = np.zeros(len(xs))
    m[0], m[-1] = d[0], d[-1]
    for i in range(1, len(xs) - 1):
        if d[i - 1] * d[i] <= 0:
            m[i] = 0.0            # lokal extrempunkt -> platt tangent
        else:
            w1 = 2 * h[i] + h[i - 1]
            w2 = h[i] + 2 * h[i - 1]
            m[i] = (w1 + w2) / (w1 / d[i - 1] + w2 / d[i])
    idx = np.clip(np.searchsorted(xs, gx) - 1, 0, len(h) - 1)
    t = np.clip((gx - xs[idx]) / np.maximum(h[idx], 1e-9), 0.0, 1.0)
    t2, t3 = t * t, t * t * t
    gy = ((2 * t3 - 3 * t2 + 1) * ys[idx]
          + (t3 - 2 * t2 + t) * h[idx] * m[idx]
          + (-2 * t3 + 3 * t2) * ys[idx + 1]
          + (t3 - t2) * h[idx] * m[idx + 1])
    return gx.astype(np.float32), np.clip(gy, 0, 1).astype(np.float32)


def _hist_peak(chans):
    """Normaliseringstak för histogram. De yttersta binnarna (rena svart-/
    vitspikar från klippta skuggor/himmel) räknas inte — de kan bli många
    gånger högre än resten och tryckte annars ner hela kurvan till en rand."""
    inner = [h[1:-1] if len(h) > 2 else h for h in chans]
    return max(1, int(np.concatenate(inner).max()))


def _mix_hex(a, b, t):
    """Blanda två '#rrggbb'-färger (t=0 -> a, t=1 -> b)."""
    a, b = a.lstrip("#"), b.lstrip("#")
    return "#%02x%02x%02x" % tuple(
        int(int(a[i:i + 2], 16) * (1 - t) + int(b[i:i + 2], 16) * t + 0.5)
        for i in (0, 2, 4))


class VignetteCache:
    """Cachar den FÄRDIGKURVADE fallofmasken (r**2.2) per bildstorlek.

    Höll tidigare bara EN storlek, men förhandsvisning (1400 px), filmremsans
    tumnaglar, rullkort och export använder olika storlekar — cachen
    räknades om vid varje växling (~60 ms per byte). Nu en liten LRU; masker
    över ~4 MP (export) cachas inte, de används en gång och är stora.
    Masken byggs med broadcasting av två 1D-axlar istället för np.mgrid
    (som allokerade två fullstora int64-arrayer — ~380 MB vid 24 MP)."""

    MAX_ENTRIES = 4
    MAX_CACHED_PIXELS = 4_000_000

    def __init__(self):
        self._cache = OrderedDict()
        self._lock = threading.Lock()   # delas mellan preview- och batch-tråd

    def get(self, h, w):
        key = (h, w)
        with self._lock:
            m = self._cache.get(key)
            if m is not None:
                self._cache.move_to_end(key)
                return m
        cy, cx = (h - 1) / 2.0, (w - 1) / 2.0
        ny = (np.arange(h, dtype=np.float32) - cy) / (h / 2.0)
        nx = (np.arange(w, dtype=np.float32) - cx) / (w / 2.0)
        r = np.sqrt(nx[None, :] * nx[None, :] + ny[:, None] * ny[:, None])
        r /= 1.41421356
        np.clip(r, 0, 1, out=r)
        m = (r ** 2.2).astype(np.float32)
        m.flags.writeable = False
        if h * w <= self.MAX_CACHED_PIXELS:
            with self._lock:
                self._cache[key] = m
                while len(self._cache) > self.MAX_ENTRIES:
                    self._cache.popitem(last=False)
        return m


_VIG = VignetteCache()
_LUMA_W = np.array([0.299, 0.587, 0.114], np.float32)


def _luma(arr):
    """Luminans (Rec. 601) som EN matris-vektorprodukt — tre strided
    kanalmultiplikationer + summa + astype-kopia var ~4× långsammare."""
    return arr @ _LUMA_W


def _chan_add(x, m, coef=(1.0, 1.0, 1.0)):
    """x[..., c] += m * coef[c], kanal för kanal. Att broadcasta en
    (h,w)-mask som m[..., None] mot (h,w,3) — eller bilda ytterprodukten
    m[..., None] * vektor — är 4–6× långsammare i numpy än tre kanalvisa
    pass: den inre dimensionen (3) ger en kort, ineffektiv inre loop. Det
    mönstret stod för ungefär hälften av renderingstiden."""
    for c in range(3):
        k = coef[c]
        if k == 0:
            continue
        xc = x[..., c]
        if k == 1:
            xc += m
        else:
            xc += m * np.float32(k)


def _chan_mul(x, m):
    for c in range(3):
        xc = x[..., c]
        xc *= m


def _blur_f(chan, rad):
    """GaussianBlur på en float32-kanal (0–1). Pillow (t.o.m. 12.x) saknar
    gaussian_blur för 'F'-mode, så bluren går via 8 bitar — för de
    lågfrekventa masker detta används till (glöd/klarhet/skärpe-bas) är
    kvantiseringen försumbar. (Rotation/resize STÖDJER däremot F-mode,
    så geometrivägen behåller fullt bitdjup.)"""
    im = Image.fromarray((np.clip(chan, 0.0, 1.0) * 255).astype(np.uint8))
    return np.asarray(im.filter(ImageFilter.GaussianBlur(rad)),
                      np.float32) / 255.0


def _blur_big(chan, rad):
    """Stor-radie-blur via nedskalning: blurra på kvartsupplösning och
    skala upp igen. För lågfrekventa masker (halation-glöd, klarhetens
    basläger) är resultatet i praktiken identiskt, men kostnaden är 1/16
    av pixlarna — de här två stegen dominerade annars renderingstiden."""
    if rad < 6.0:
        return _blur_f(chan, rad)
    h, w = chan.shape
    s = 4
    sw, sh = max(1, w // s), max(1, h // s)
    im = Image.fromarray((np.clip(chan, 0.0, 1.0) * 255).astype(np.uint8))
    im = im.resize((sw, sh), Image.BILINEAR)
    im = im.filter(ImageFilter.GaussianBlur(rad / s))
    return np.asarray(im.resize((w, h), Image.BILINEAR), np.float32) / 255.0


def process(arr, g, seed=1234):
    """Hela filmpipelinen. Arbetar IN-PLACE på en egen kopia (`x`), och
    alla steg som applicerar en (h,w)-mask på bilden gör det kanal för
    kanal (`_chan_add`/`_chan_mul`) — samma formler och samma ordning som
    v2.0 (bevisat mot en fryst referens i testsviten), men ~2× snabbare.
    Uppmätt mot referensen i samma körning, 1400 px: Velvia 139 → 68 ms,
    Natt 800T 213 → 111 ms, Polaroid 178 → 85 ms."""
    x = arr.astype(np.float32, copy=True)
    if g.exposure or g.temp or g.tint:
        # exponering + vitbalans som EN förstärkning per kanal
        ev = float(2.0 ** g.exposure) if g.exposure else 1.0
        t, ti = g.temp / 100.0, g.tint / 100.0
        gains = ((1.0 + 0.35 * t) * (1.0 + 0.10 * ti),
                 1.0 - 0.20 * ti,
                 (1.0 - 0.35 * t) * (1.0 + 0.10 * ti))
        for c in range(3):
            k = ev * gains[c]
            if k != 1.0:
                xc = x[..., c]
                xc *= np.float32(k)
    np.clip(x, 0.0, 1.0, out=x)
    if g.contrast:
        f = 1.0 + (g.contrast / 100.0) * 0.9
        x *= np.float32(f)                   # (x-0.5)*f+0.5 i två pass
        x += np.float32(0.5 * (1.0 - f))
        np.clip(x, 0.0, 1.0, out=x)
    if g.curves:
        for i, ch in enumerate("rgb"):
            pts = g.curves.get(ch)
            if pts:
                x[..., i] = _apply_curve(x[..., i], pts)
    if g.user_curve:
        # användarens egen tonkurva — OVANPÅ filmens inbakade kurvor.
        # Mjuk monoton PCHIP via LUT (samma matematik som editorns ritning);
        # LUT:ens värden ligger redan i 0–1, ingen klämning behövs
        lut = _user_curve_lut(_curve_key(g.user_curve))
        for i in range(3):
            x[..., i] = _lut_apply(x[..., i], lut)
    if g.saturation:
        s = 1.0 + g.saturation / 100.0
        lum = _luma(x)                       # (x-lum)*s+lum == x*s+lum*(1-s)
        x *= np.float32(s)
        lum *= np.float32(1.0 - s)
        _chan_add(x, lum)
        np.clip(x, 0.0, 1.0, out=x)
    if g.split and (any(g.shadow_tint) or any(g.highlight_tint)):
        lum = _luma(x)
        _chan_add(x, (1.0 - lum) * g.split, g.shadow_tint)
        _chan_add(x, lum * g.split, g.highlight_tint)
        np.clip(x, 0.0, 1.0, out=x)
    if g.bw:
        lum = _luma(x)
        x = np.repeat(lum[..., None], 3, axis=2)
        if any(g.tone):
            _chan_add(x, 1.0 - np.abs(2.0 * lum - 1.0), g.tone)
            np.clip(x, 0.0, 1.0, out=x)
    if g.fade:
        fd = g.fade / 100.0
        floor = 0.09 * fd
        x *= np.float32(1.0 - floor - 0.04 * fd)
        x += np.float32(floor)
        np.clip(x, 0.0, 1.0, out=x)
    if g.clarity:
        # lokal mellantonskontrast: oskarp mask med STOR radie på luminansen
        h, w = x.shape[:2]
        lum = _luma(x)
        rad = max(2.0, min(h, w) * 0.02)
        d = lum - _blur_big(lum, rad)
        d *= 1.0 - np.abs(2.0 * lum - 1.0)   # spar högdager/skugga
        d *= np.float32(g.clarity / 100.0 * 0.9)
        _chan_add(x, d)
        np.clip(x, 0.0, 1.0, out=x)
    if g.sharpen:
        # oskarp mask med liten radie på LUMINANSEN — en blur istället för
        # tre (kanalvis), och luma-skärpning förstärker inte kromatiskt
        # brus/färgfrans som kanalvis skärpning gör
        h, w = x.shape[:2]
        rad = max(0.6, min(h, w) * 0.0015)
        lum = _luma(x)
        d = lum - _blur_f(lum, rad)
        d *= np.float32(g.sharpen / 100.0 * 1.4)
        _chan_add(x, d)
        np.clip(x, 0.0, 1.0, out=x)
    if g.halation:
        h, w = x.shape[:2]
        lum = _luma(x)
        thr = 0.65                  # lägre tröskel -> fler ljusa partier glöder
        mask = np.clip((lum - thr) / (1.0 - thr), 0.0, 1.0)
        mask *= np.sqrt(mask)       # == mask**1.5, men sqrt är hårdvarusnabb
        radius = max(2.0, min(h, w) * 0.02)   # bredare, mer filmisk spridning
        glow = _blur_big(mask, radius)
        amt = np.float32(g.halation / 100.0 * 1.6)   # kraftigare intensitet
        for c, k in enumerate((1.0, 0.32, 0.14)):    # röd-orange glöd
            xc = x[..., c]
            xc += glow * np.float32(k) * amt
        np.clip(x, 0.0, 1.0, out=x)
    if g.grain:
        h, w = x.shape[:2]
        rng = np.random.default_rng(seed)
        gs = max(1.0, float(g.grain_size))
        if gs <= 1.01:
            noise = rng.standard_normal((h, w)).astype(np.float32)
        else:
            # generera på lägre upplösning och skala upp → grövre korn
            nh, nw = max(1, int(round(h / gs))), max(1, int(round(w / gs)))
            small = rng.standard_normal((nh, nw)).astype(np.float32)
            noise = np.asarray(
                Image.fromarray(small, mode="F").resize((w, h), Image.BILINEAR),
                dtype=np.float32)
        # kornstruktur: forma bruskurvan utan att ändra mängden. Låg = mjukt
        # molnigt korn (orört gaussiskt brus), hög = hårt/gryning korn där små
        # värden punchas upp mot ytterlägena. Energin åternormaliseras så att
        # 'korn'-reglaget ensamt styr styrkan.
        rough = _clampf(float(getattr(g, "grain_rough", 0.0)), 0.0, 100.0) / 100.0
        if rough > 1e-3:
            noise = np.sign(noise) * (np.abs(noise) ** (1.0 - 0.6 * rough))
            std = float(noise.std()) or 1.0
            noise /= std
        lum = _luma(x)
        weight = 1.0 - (2.0 * lum - 1.0) ** 2
        # mjukare upprampning: en lätt gammakurva (>1) gör låga reglagevärden
        # subtila så man kan dosera finkorn, och en lägre topp (0.09) sänker
        # den totala intensiteten jämfört med den tidigare linjära 0.14.
        t = g.grain / 100.0
        d = noise * weight                  # (noise kan vara skrivskyddad)
        d *= np.float32((t ** 1.35) * 0.09)
        _chan_add(x, d)
        np.clip(x, 0.0, 1.0, out=x)
    if g.vignette:
        m = _VIG.get(x.shape[0], x.shape[1])   # redan **2.2-kurvad i cachen
        _chan_mul(x, 1.0 - (g.vignette / 100.0) * m)
    np.clip(x, 0.0, 1.0, out=x)
    return x


def to_pil(arr):
    return Image.fromarray((arr * 255.0 + 0.5).astype(np.uint8), "RGB")


def blend_strength(base, graded, k):
    """Styrke-blandning original↔graderad (0..1). Delad av förhandsvisning,
    rullkort, jämförelsevyn, Spara och Exportera alla — samma uttryck låg
    tidigare duplicerat på fem ställen, med risk att en framtida justering
    bara görs på några av dem."""
    if k >= 0.999:
        return graded
    if k <= 0.001:
        return base
    return base * (1.0 - k) + graded * k


# =====================================================================
#  Ikon — filmrulle-motiv, inbäddat som PNG (base64) så exe:n förblir en fil
# =====================================================================

_ICON_PNG_B64 = (
    "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"
)


def make_icon_image(size):
    im = Image.open(io.BytesIO(base64.b64decode(_ICON_PNG_B64))).convert("RGBA")
    if im.size != (size, size):
        im = im.resize((size, size), Image.LANCZOS)
    return im


def _dpi_setup():
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    except Exception:
        pass


def _hex_to_colorref(hexstr):
    """'#rrggbb' -> Win32 COLORREF (0x00bbggrr som ett DWORD)."""
    h = hexstr.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return r | (g << 8) | (b << 16)


def _work_area():
    """Skärmens användbara yta EXKLUSIVE aktivitetsfältet (Win32
    SPI_GETWORKAREA). `winfo_screenheight()` ger hela skärmens höjd
    INKLUSIVE ytan där aktivitetsfältet ligger, vilket får ett fönster
    centrerat/skalat mot den att sticka ner bakom eller krocka med det."""
    try:
        import ctypes
        from ctypes import wintypes
        rect = wintypes.RECT()
        SPI_GETWORKAREA = 0x0030
        ok = ctypes.windll.user32.SystemParametersInfoW(
            SPI_GETWORKAREA, 0, ctypes.byref(rect), 0)
        if ok:
            return rect.left, rect.top, rect.right, rect.bottom
    except Exception:      # noqa: BLE001
        pass
    return None


# =====================================================================
#  Renderare i bakgrundstråd
# =====================================================================

class Renderer(threading.Thread):
    def __init__(self, out_queue):
        super().__init__(daemon=True)
        self.out = out_queue
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self._job = None
        self._alive = True

    def submit(self, token, src_arr, grade):
        with self._lock:
            self._job = (token, src_arr, grade)
        self._wake.set()

    def stop(self):
        self._alive = False
        self._wake.set()

    def run(self):
        while self._alive:
            self._wake.wait()
            self._wake.clear()
            with self._lock:
                job, self._job = self._job, None
            if not job or not self._alive:
                continue
            token, src, grade = job
            try:
                self.out.put((token, process(src, grade)))
            except Exception as e:      # noqa: BLE001
                log_error("render")
                self.out.put((token, e))


# =====================================================================
#  Egenritat reglage-spår (ersätter tk.Scale — annat material)
# =====================================================================

class Track(tk.Frame):
    PAD = 9

    def __init__(self, master, label, lo, hi, on_change, fmt="{:+.0f}",
                 bg=PAPER2, width=190, serif=("Georgia", 9), on_grab=None,
                 step=None):
        super().__init__(master, bg=bg)
        self.lo, self.hi, self.on_change, self.fmt = lo, hi, on_change, fmt
        self.on_grab = on_grab       # fyras EN gång vid greppet (för ångra)
        self.step = step             # None = heuristik utifrån spannet
        self._last_grab = 0.0        # dämpar on_grab vid mushjuls-serier
        self.bg = bg
        self.value = 0.0
        head = tk.Frame(self, bg=bg)
        head.pack(fill="x")
        tk.Label(head, text=label, bg=bg, fg=INK2,
                 font=(serif[0], 9, "italic"), anchor="w").pack(side="left")
        self.val_lbl = tk.Label(head, text=fmt.format(0), bg=bg, fg=INK,
                                 font=(serif[0], 9), anchor="e")
        self.val_lbl.pack(side="right")
        self.cv = tk.Canvas(self, height=20, width=width, bg=bg,
                            highlightthickness=0, bd=0, cursor="hand2")
        self.cv.pack(fill="x")
        self.cv.bind("<Button-1>", self._press)
        self.cv.bind("<B1-Motion>", self._drag)
        self.cv.bind("<MouseWheel>", self._wheel)
        self.cv.bind("<Configure>", lambda e: self._redraw())

    def _press(self, e):
        if self.on_grab:            # ta ångra-ögonblicksbild innan värdet rörs
            self.on_grab()
            self._last_grab = time.monotonic()
        self._drag(e)

    def _step_size(self):
        if self.step:
            return self.step
        return 0.1 if (self.hi - self.lo) <= 4 else 1.0

    def _wheel(self, e):
        """Mushjul över spåret = finjustera ett steg i taget — precisions-
        komplement till dragning (som lätt hoppar flera enheter). En tät
        hjul-serie räknas som ETT grepp i ångra-hänseende, annars fylls
        ångra-stacken med en post per klick på hjulet."""
        now = time.monotonic()
        if self.on_grab and now - self._last_grab > 1.2:
            self.on_grab()
        self._last_grab = now
        step = self._step_size()
        v = self.value + step * (1 if e.delta > 0 else -1)
        # snappa till steget: upprepad addition av 0.1 gav 0.30000000000000004
        self.set(round(round(v / step) * step, 10))
        if self.on_change:
            self.on_change()

    def _width(self):
        w = self.cv.winfo_width()
        return w if w > 4 else int(self.cv["width"])

    def _x_of(self, v):
        frac = (v - self.lo) / (self.hi - self.lo)
        w = self._width()
        return self.PAD + frac * (w - 2 * self.PAD)

    def _drag(self, e):
        w = self._width()
        frac = _clampf((e.x - self.PAD) / max(1, w - 2 * self.PAD), 0.0, 1.0)
        v = self.lo + frac * (self.hi - self.lo)
        step = self._step_size()
        v = round(v / step) * step
        self.set(v)
        if self.on_change:
            self.on_change()

    def set(self, v):
        self.value = _clampf(v, self.lo, self.hi)
        self.val_lbl.configure(text=self.fmt.format(self.value))
        self._redraw()

    def get(self):
        return self.value

    def reset(self):
        self.set(0.0)

    def _redraw(self):
        c = self.cv
        c.delete("all")
        w = self._width()
        y = 10
        x0, x1 = self.PAD, w - self.PAD
        c.create_line(x0, y, x1, y, fill=LINE, width=3, capstyle="round")
        xk = self._x_of(self.value)
        # fyllning från nollpunkt (eller vänster) till knopp
        zero = self._x_of(0.0) if self.lo < 0 < self.hi else x0
        c.create_line(zero, y, xk, y, fill=ACCENT, width=3, capstyle="round")
        r = 6
        c.create_oval(xk - r, y - r, xk + r, y + r, fill=INK, outline=MAT,
                      width=2)


# =====================================================================
#  Kurveditor — dragbar tonkurva (egenritad canvas i appens stil)
# =====================================================================

class CurveEditor(tk.Frame):
    """Tonkurva i Lightroom-stil: mjuk monoton kurva (PCHIP — exakt samma
    matematik som pipelinen applicerar), luminanshistogram för aktivt foto
    i bakgrunden, runda handtag med hover/aktiv-tillstånd och en in→ut-
    värdeavläsning medan man drar."""
    PAD = 12
    HIT = 10         # träffradie för punktgrepp (px)

    def __init__(self, master, on_change, on_grab=None, width=256,
                 height=180, bg=PAPER2, serif=("Georgia", 8)):
        super().__init__(master, bg=bg)
        self.on_change = on_change
        self.on_grab = on_grab
        self.serif = serif
        self.pts = [[0.0, 0.0], [1.0, 1.0]]
        self._drag = None
        self._hover = None
        self._hist = None            # normaliserat luma-histogram (48 bins)
        self.cv = tk.Canvas(self, width=width, height=height, bg=MAT,
                            highlightthickness=1, highlightbackground=LINE,
                            bd=0, cursor="crosshair")
        self.cv.pack()
        self.cv.bind("<Button-1>", self._press)
        self.cv.bind("<B1-Motion>", self._motion)
        self.cv.bind("<ButtonRelease-1>", self._release)
        self.cv.bind(RIGHT_BTN, self._remove)
        self.cv.bind("<Motion>", self._on_hover)
        self.cv.bind("<Leave>", self._on_leave)
        self.cv.bind("<Configure>", lambda e: self._redraw())
        self._redraw()

    # ---- koordinater: normaliserat (0,0)=nere-vänster ↔ canvas-px ----
    def _dims(self):
        w = self.cv.winfo_width()
        h = self.cv.winfo_height()
        if w < 4 or h < 4:              # inte mappad än → begärd storlek
            w, h = int(self.cv["width"]), int(self.cv["height"])
        return w, h

    def _to_px(self, x, y):
        w, h = self._dims()
        return (self.PAD + x * (w - 2 * self.PAD),
                h - self.PAD - y * (h - 2 * self.PAD))

    def _to_norm(self, px, py):
        w, h = self._dims()
        return (_clampf((px - self.PAD) / max(1, w - 2 * self.PAD), 0, 1),
                _clampf((h - self.PAD - py) / max(1, h - 2 * self.PAD), 0, 1))

    def _hit(self, px, py):
        for i, (x, y) in enumerate(self.pts):
            hx, hy = self._to_px(x, y)
            if abs(px - hx) <= self.HIT and abs(py - hy) <= self.HIT:
                return i
        return None

    # ---- interaktion ----
    MIN_GAP = 0.02   # minsta x-avstånd mellan punkter (samma som vid drag)

    def _press(self, e):
        if self.on_grab:
            self.on_grab()          # ångra-ögonblick före ändring
        i = self._hit(e.x, e.y)
        if i is None:               # klick på tom yta = ny punkt …
            x, y = self._to_norm(e.x, e.y)
            near = min(range(len(self.pts)),
                       key=lambda j: abs(self.pts[j][0] - x))
            if abs(self.pts[near][0] - x) < self.MIN_GAP:
                # … utom om x hamnar för nära en befintlig punkt (t.ex. klick
                # i marginalen, där x kläms till 0/1): då skapades förut en
                # punkt med SAMMA x som ändpunkten → nollbred PCHIP-sektion
                # och en spik i kurvan. Ta istället tag i den närmaste punkten.
                i = near
            else:
                self.pts.append([x, y])
                self.pts.sort(key=lambda p: p[0])
                i = next(j for j, p in enumerate(self.pts)
                         if p[0] == x and p[1] == y)
        self._drag = i
        self._motion(e)

    def _motion(self, e):
        if self._drag is None:
            return
        i = self._drag
        x, y = self._to_norm(e.x, e.y)
        if i == 0:
            x = 0.0                 # ändpunkter låsta i x
        elif i == len(self.pts) - 1:
            x = 1.0
        else:                       # håll x mellan grannarna
            x = _clampf(x, self.pts[i - 1][0] + 0.02,
                        self.pts[i + 1][0] - 0.02)
        self.pts[i] = [x, y]
        self._redraw()
        if self.on_change:
            self.on_change()

    def _release(self, _e):
        self._drag = None
        self._redraw()              # krymp tillbaka aktivt handtag

    def _remove(self, e):
        i = self._hit(e.x, e.y)
        if i is None or i in (0, len(self.pts) - 1):
            return                  # ändpunkter kan inte tas bort
        if self.on_grab:
            self.on_grab()
        del self.pts[i]
        self._hover = None
        self._redraw()
        if self.on_change:
            self.on_change()

    def _on_hover(self, e):
        if self._drag is not None:
            return
        hit = self._hit(e.x, e.y)
        if hit != self._hover:
            self._hover = hit
            self._redraw()

    def _on_leave(self, _e):
        if self._hover is not None:
            self._hover = None
            self._redraw()

    # ---- API ----
    def set_points(self, pts):
        self.pts = sanitize_curve(pts) or [[0.0, 0.0], [1.0, 1.0]]
        self._drag = None
        self._hover = None
        self._redraw()

    def points_or_none(self):
        return [list(p) for p in self.pts] \
            if curve_is_active(self.pts) else None

    def reset(self):
        self.set_points(None)

    def set_histogram(self, arr):
        """Luminanshistogram för aktivt foto som bakgrund — visar VAR i
        tonomfånget bildens information faktiskt ligger, så man ser vilka
        delar av kurvan som påverkar något. arr = float32-bild eller None."""
        if arr is None:
            self._hist = None
        else:
            small = arr[::max(1, arr.shape[0] // 140),
                        ::max(1, arr.shape[1] // 180)]
            hv, _ = np.histogram(_luma(small), bins=48, range=(0.0, 1.0))
            self._hist = np.minimum(hv / _hist_peak([hv]), 1.0) \
                .astype(np.float32)
        self._redraw()

    # ---- ritning ----
    def _redraw(self):
        c = self.cv
        c.delete("all")
        w, h = self._dims()
        # luminanshistogram längst bak (dämpad fyllnad i väggens material)
        if self._hist is not None:
            hcol = _mix_hex(MAT, FAINT, 0.42)
            n = len(self._hist)
            pts = list(self._to_px(0.0, 0.0))
            for b in range(n):
                bx = (b + 0.5) / n
                pts += self._to_px(bx, float(self._hist[b]) * 0.92)
            pts += self._to_px(1.0, 0.0)
            c.create_polygon(pts, fill=hcol, outline="")
        # rutnät (kvartslinjer) + diagonal referens
        for f in (0.25, 0.5, 0.75):
            x0, y0 = self._to_px(f, 0)
            x1, y1 = self._to_px(f, 1)
            c.create_line(x0, y0, x1, y1, fill=LINE)
            x0, y0 = self._to_px(0, f)
            x1, y1 = self._to_px(1, f)
            c.create_line(x0, y0, x1, y1, fill=LINE)
        dx0, dy0 = self._to_px(0, 0)
        dx1, dy1 = self._to_px(1, 1)
        c.create_line(dx0, dy0, dx1, dy1, fill=FAINT, dash=(3, 3))
        # kurvan — samplad ur SAMMA PCHIP-LUT som pipelinen använder
        gx, gy = _pchip_lut(self.pts, n=110)
        line = []
        for x, y in zip(gx, gy):
            line += self._to_px(float(x), float(y))
        c.create_line(*line, fill=INK, width=2)
        # runda handtag: hover = ljusare, aktiv (dragen) = större
        for i, (x, y) in enumerate(self.pts):
            px, py = self._to_px(x, y)
            if i == self._drag:
                r, fill = 7, ACCENT_HI
            elif i == self._hover:
                r, fill = 6, ACCENT_HI
            else:
                r, fill = 5, ACCENT
            c.create_oval(px - r, py - r, px + r, py + r, fill=fill,
                          outline=MAT, width=2)
        # in→ut-avläsning medan man drar
        if self._drag is not None:
            x, y = self.pts[self._drag]
            c.create_text(self.PAD + 4, self.PAD + 2, anchor="nw",
                          text=f"{x:.2f} → {y:.2f}", fill=INK2,
                          font=(self.serif[0], 8, "italic"))


# =====================================================================
#  Huvudfönster
# =====================================================================

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self._settings = load_settings()
        saved_theme = self._settings.get("theme", "light")
        if saved_theme != THEME_NAME:
            apply_theme(saved_theme)   # måste ske FÖRE self.configure(bg=PAPER)
        self.title("Filmrulle")
        self.configure(bg=PAPER)
        self.minsize(1100, 700)
        # normalstorlek — 85% av det ANVÄNDBARA arbetsområdet (centrerad
        # där i, inte mot hela skärmen) — utan övre tak: ett tak på t.ex.
        # 1440x900 ser litet ut på en stor skärm, och ett hårdkodat
        # absoluttal blev för litet/klämt på en mindre skärm. `winfo_
        # screenheight()` räknar in ytan bakom aktivitetsfältet, vilket
        # fick fönstret att krocka med det — `_work_area()` exkluderar den.
        wa = _work_area()
        if wa:
            wl, wt, wr, wb = wa
        else:
            wl, wt = 0, 0
            wr, wb = self.winfo_screenwidth(), self.winfo_screenheight()
        aw, ah = wr - wl, wb - wt
        dw = max(1100, int(aw * 0.85))
        dh = max(700, int(ah * 0.85))
        x, y = wl + (aw - dw) // 2, wt + (ah - dh) // 2
        self.geometry(f"{dw}x{dh}+{x}+{y}")
        if self._settings.get("maximized", False):
            try:
                self.state("zoomed")  # var maximerat senast — minns det
            except Exception:
                pass

        self.serif = _resolve_family(["Constantia", "Cambria", "Georgia",
                                      "Palatino Linotype", "serif"])
        self.sans = _resolve_family(["Segoe UI", "Corbel", "Arial"])
        try:
            self._icon = ImageTk.PhotoImage(make_icon_image(64), master=self)
            self.iconphoto(True, self._icon)
        except Exception:      # noqa: BLE001 — kosmetiskt
            pass

        self.prev_arr = None
        self.thumb_src = None
        self.geo_arr = None           # prev_arr efter räta-upp + beskär
        self._geo_thumb = None        # thumb_src efter geometri (filmremsan)
        self.graded_arr = None
        self.cur_pil = None
        self._compose_arr = None      # senaste komponerade float-arr (histogram)
        self._tk_img = None
        self._blank = None
        self.film_key = "original"
        self.src_path = None
        self.intensity = 1.0
        self._cur_crop = NO_CROP      # aktivt fotos beskärning/vinkel (i UI)
        self._cur_angle = 0.0
        self._img_rect = None         # (x0,y0,scale) käll→canvas
        self._session_path = None
        self._token = 0
        self._cards = {}
        self._thumbs = {}
        self._render_after = None
        self._showing_original = False
        self._adjust_open = False
        # zoom/panorering
        self.zoom = 1.0
        self.pan_x = 0.0
        self.pan_y = 0.0
        self._view_pil = None
        self._panning = False
        self._fast_view = False       # BILINEAR under pan/zoom, LANCZOS annars
        self._crisp_after = None
        self._adj_x = 0.0
        self._adj_y = 0.0

        # foto-rulle (session) — Lightroom-liknande: importera flera,
        # klicka dig igenom var och en, exportera alla i slutet
        self.session = []
        self.active_idx = None
        self._clipboard_edit = None
        self._roll_cards = {}
        self._roll_drag = None        # dragomordning i foto-rullen
        self._undo = []               # ångra-stack av sessions-ögonblicksbilder
        self._redo = []               # gör-om-stack (töms vid ny ändring)
        # beskärningsläge
        self._crop_mode = False
        self._crop_rect = list(NO_CROP)
        self._crop_angle = 0.0
        self._crop_aspect = None      # None = fri; annars bredd/höjd
        self._crop_drag = None
        self._crop_disp = None        # rätad helbild (för overlay-ritning)
        self._curve_open = False
        # tema / ren vy / helskärm
        self._theme = THEME_NAME
        self._chrome_hidden = False
        self._fullscreen = False
        # jämförelse: två foton sida vid sida
        self._compare_mode = False
        self._compare_uid = None
        self._cmp_imgs = []
        # export-inställningar (delas av Spara + Exportera alla) — minns
        # mellan körningar via settings-filen (som tema/maximerat), annars
        # hoppade format+kvalitet tillbaka till JPEG/95 vid varje omstart
        self._export_open = False
        self.export_fmt = self._settings.get("export_fmt", "JPEG")
        if self.export_fmt not in ("JPEG", "PNG", "TIFF"):
            self.export_fmt = "JPEG"
        self.jpeg_quality = int(_clampf(
            _finite(self._settings.get("jpeg_quality"), 95), 60, 100))
        self._fmt_cards = {}
        self._settings_after = None   # debounce: settings skrivs inte per drag

        # trådad import (fil-I/O utanför GUI-tråden)
        self._importing = False
        self._imp_added = 0
        self._imp_failed = 0
        self._imp_err = None          # första felorsaken vid import
        self._pending_active = None   # aktivt index att återställa (projekt)
        self._exporting = False       # skydd: varna om man stänger mitt i
        self._close_warned = False    # en pågående diskskrivande export
        # chunkad filmremse-rendering (18+ kort byggs över flera event-varv)
        self._thumb_after = None
        self._thumb_queue = []
        self._thumb_src_ref = None
        self._resize_after = None     # debounce av fönsterresize-omritning
        self._poll_after = None
        # renderingscacher — nyckel = receptets signatur, så bara det som
        # faktiskt ändrats renderas om (ångra, synka, tembyte, jämförelse)
        self._roll_img_cache = {}     # uid -> (signatur, PhotoImage)
        self._cmp_cache = None        # (uid, signatur, PIL) — jämförelsebilden
        self._cmp_disp = None         # ((uid, signatur, dw, dh), PhotoImage)
        self._crop_src_ref = None     # beskärningsvyns rotation: (källa, vinkel)
        self._crop_img_key = None     #   … och dess skalade PhotoImage-nyckel

        load_user_presets()           # egna presets in i FILMS före kortbygget

        self._rq = queue.Queue()
        self._bq = queue.Queue()        # batch-förlopp
        self._iq = queue.Queue()        # importtrådens leveranser
        self._renderer = Renderer(self._rq)
        self._renderer.start()

        self._build_ui()
        self._poll_after = self.after(30, self._poll_render)
        self.after(300, self._enable_dnd)   # fönstret måste finnas först
        self.after(50, self._apply_titlebar_theme)  # hwnd måste finnas först
        self.bind("<Configure>", self._on_resize)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # tangentbord — Ctrl på Windows; på macOS dessutom Cmd, som Mac-
        # användare förväntar sig (Mac-bygget hade bara Ctrl-varianterna)
        shortcuts = [
            ("<Control-o>", lambda e: self.open_image()),
            ("<Control-s>", lambda e: self.save_image()),
            ("<Control-b>", lambda e: self.export_all()),
            ("<Control-c>", lambda e: self.copy_settings()),
            ("<Control-v>", lambda e: self.paste_settings()),
            ("<Control-Shift-V>", lambda e: self.sync_active_to_all()),
            ("<Control-Left>", lambda e: self._nav(-1)),
            ("<Control-Right>", lambda e: self._nav(1)),
            ("<Control-z>", lambda e: self.undo()),
            ("<Control-y>", lambda e: self.redo()),
            ("<Control-Shift-Z>", lambda e: self.redo()),
            ("<Control-Shift-S>", lambda e: self.save_session()),
        ]
        for seq, fn in shortcuts:
            self.bind(seq, fn)
            if sys.platform == "darwin":
                self.bind(seq.replace("Control", "Command"), fn)
        self.bind("<Delete>", lambda e: self.remove_active_photo())
        self.bind("<KeyPress-space>", self._show_original)
        self.bind("<KeyRelease-space>", self._show_graded)
        self.bind("<Key-0>", self._reset_zoom)
        self.bind("<F11>", lambda e: self._toggle_fullscreen())
        self.bind("<Key-p>", lambda e: self.rate_active(1))
        self.bind("<Key-x>", lambda e: self.rate_active(-1))
        self.bind("<Key-u>", lambda e: self.rate_active(0))
        self.bind("<Left>", lambda e: self._cycle_film(-1))
        self.bind("<Right>", lambda e: self._cycle_film(1))
        self.bind("<Escape>", self._on_escape)

    # -------- typsnitts-hjälpare --------
    def sf(self, size, *style):
        return (self.serif, size, *style)

    # -------- fel & bekräftelser --------
    def report_callback_exception(self, exc, val, tb):
        """Tk anropar denna för undantag i callbacks. Standardversionen
        skriver till stderr — som inte finns i den byggda exe:n — så felet
        försvann spårlöst. Nu loggas det och syns i statusraden."""
        log_error("callback", (exc, val, tb))
        try:
            self.name_lbl.configure(
                text=f"Ett fel inträffade ({exc.__name__}) — se "
                     f"{os.path.basename(ERROR_LOG)} i hemkatalogen")
        except Exception:      # noqa: BLE001 — UI:t kan vara halvrivet
            pass

    def _confirm(self, title, msg):
        """Ja/nej-dialog före destruktiva åtgärder (separat metod så att
        testerna kan svara utan modala fönster)."""
        return messagebox.askyesno(title, msg, parent=self)

    def _on_escape(self, _=None):
        """Escape backar ut ur det 'innersta' läget först: beskärning →
        öppna flytande paneler → jämförelse → helskärm → ren vy. Ett
        tryck = ett kliv, aldrig flera på en gång."""
        if self._crop_mode:
            self._cancel_crop()
        elif self._adjust_open or self._curve_open or self._export_open:
            if self._adjust_open:
                self._toggle_adjust()
            if self._curve_open:
                self.toggle_curve()
            if self._export_open:
                self._toggle_export_panel()
        elif self._compare_mode:
            self._exit_compare()
        elif self._fullscreen:
            self._toggle_fullscreen()
        elif self._chrome_hidden:
            self._toggle_chrome()

    # ---------------------------------------------------------- UI-bygge
    def _build_ui(self):
        # master=self: utan den hamnar bilden i den FÖRSTA Tk-tolken som
        # skapats i processen, och en andra App-instans kraschade
        self._blank = tk.PhotoImage(master=self, width=CARD_W,
                                    height=CARD_IMG_H)
        self._roll_blank = tk.PhotoImage(master=self, width=ROLL_W,
                                         height=ROLL_H)

        # ---- header ----
        head = tk.Frame(self, bg=PAPER, height=52)
        self._head = head
        head.pack(fill="x", side="top")
        head.pack_propagate(False)
        tk.Label(head, text="Filmrulle", bg=PAPER, fg=INK,
                 font=self.sf(19, "italic"), padx=20).pack(side="left")
        self.name_lbl = tk.Label(head, text="", bg=PAPER, fg=FAINT,
                                 font=self.sf(9, "italic"))
        self.name_lbl.pack(side="left", padx=8)
        # vy-verktyg (tema · helskärm · ren vy) längst till vänster
        tk.Frame(head, bg=LINE, width=1, height=24).pack(side="left", padx=8)
        self.theme_btn = self._chip(
            head, "☀ Dag" if THEME_NAME == "dark" else "☾ Natt",
            self._toggle_theme)
        self.theme_btn.pack(side="left", padx=2)
        self._chip(head, "⛶", self._toggle_fullscreen).pack(side="left", padx=2)
        self._chip(head, "Ren vy", self._toggle_chrome).pack(
            side="left", padx=2)
        self.save_btn = self._chip(head, "Spara", self.save_image)
        self.save_btn.pack(side="right", padx=(4, 20))
        self._set_enabled(self.save_btn, False)
        self.open_btn = self._chip(head, "Öppna", self.open_image,
                                   primary=True)
        self.open_btn.pack(side="right", padx=4)
        self._tool_sep(head)
        self.savesess_btn = self._chip(head, "Spara projekt",
                                       self.save_session)
        self.savesess_btn.pack(side="right", padx=4)
        self._set_enabled(self.savesess_btn, False)
        self._sep1 = tk.Frame(self, bg=LINE, height=1)
        self._sep1.pack(fill="x", side="top")

        # ---- foto-rulle (session) ----
        roll_wrap = tk.Frame(self, bg=PAPER)
        self._roll_wrap = roll_wrap
        roll_wrap.pack(fill="x", side="top")
        rtool = tk.Frame(roll_wrap, bg=PAPER, padx=20, pady=4)
        rtool.pack(fill="x")
        self.roll_count_lbl = tk.Label(
            rtool, text="Inga foton importerade", bg=PAPER, fg=FAINT,
            font=self.sf(9, "italic"))
        self.roll_count_lbl.pack(side="left")
        self.undo_btn = self._chip(rtool, "↶ Ångra", self.undo)
        self.undo_btn.pack(side="left", padx=(16, 0))
        self._set_enabled(self.undo_btn, False)
        self.redo_btn = self._chip(rtool, "Gör om ↷", self.redo)
        self.redo_btn.pack(side="left", padx=(4, 0))
        self._set_enabled(self.redo_btn, False)
        # höger: [Kopiera Klistra Synka] | [Ta bort] | [Exportera alla]
        self.export_btn = self._chip(rtool, "Exportera alla", self.export_all,
                                     primary=True)
        self.export_btn.pack(side="right")
        self._set_enabled(self.export_btn, False)
        self.fmt_btn = self._chip(rtool, "Format ▾", self._toggle_export_panel)
        self.fmt_btn.pack(side="right", padx=(0, 4))
        self._tool_sep(rtool)
        self._chip(rtool, "Ta bort", self.remove_active_photo).pack(
            side="right", padx=4)
        self.compare_btn = self._chip(rtool, "Jämför", self.toggle_compare)
        self.compare_btn.pack(side="right", padx=4)
        self._tool_sep(rtool)
        self._chip(rtool, "Synka → alla", self.sync_active_to_all).pack(
            side="right", padx=4)
        self._chip(rtool, "Klistra in", self.paste_settings).pack(
            side="right", padx=4)
        self._chip(rtool, "Kopiera", self.copy_settings).pack(
            side="right", padx=4)

        roll_strip_wrap = tk.Frame(roll_wrap, bg=PAPER, height=ROLL_H + 34)
        roll_strip_wrap.pack(fill="x")
        roll_strip_wrap.pack_propagate(False)
        self.roll_strip = tk.Canvas(roll_strip_wrap, bg=PAPER,
                                    highlightthickness=0, bd=0)
        self._arrow(roll_strip_wrap, "‹",
                    lambda: self.roll_strip.xview_scroll(-4, "units")).pack(
            side="left", fill="y", padx=(8, 2))
        self._arrow(roll_strip_wrap, "›",
                    lambda: self.roll_strip.xview_scroll(4, "units")).pack(
            side="right", fill="y", padx=(2, 8))
        self.roll_strip.pack(fill="both", expand=True, pady=(0, 6))
        self.roll_inner = tk.Frame(self.roll_strip, bg=PAPER)
        self.roll_strip.create_window((0, 0), window=self.roll_inner,
                                      anchor="nw")
        self.roll_inner.bind(
            "<Configure>",
            lambda e: self.roll_strip.configure(
                scrollregion=self.roll_strip.bbox("all")))
        for w in (self.roll_strip, self.roll_inner):
            w.bind("<MouseWheel>", self._wheel_roll)
        self._sep2 = tk.Frame(self, bg=LINE, height=1)
        self._sep2.pack(fill="x", side="top")

        # ---- bottenpanel: STYRKA-rad + filmremsa ----
        bottom = tk.Frame(self, bg=PAPER)
        self._bottom = bottom
        bottom.pack(fill="x", side="bottom")
        tk.Frame(bottom, bg=LINE, height=1).pack(fill="x", side="top")

        row = tk.Frame(bottom, bg=PAPER, padx=20, pady=9)
        row.pack(fill="x")
        self.film_name = tk.Label(row, text="Original", bg=PAPER, fg=INK,
                                  font=self.sf(12, "italic"), width=16,
                                  anchor="w")
        self.film_name.pack(side="left")
        self.strength = Track(row, "styrka", 0, 100, self._on_strength,
                              fmt="{:.0f}%", bg=PAPER, width=240,
                              serif=(self.serif, 9), on_grab=self._push_undo)
        self.strength.set(100)
        self.strength.pack(side="left", padx=(6, 16))
        # bildinfo (filnamn · mått · index) har en EGEN label här — headerns
        # name_lbl är enbart för statusmeddelanden; tidigare delade de fält
        # och skrev över varandra ("Nattläge" åt upp filnamnet, osv.)
        self.cmp_btn = self._chip(row, "Håll: original", None)
        self.cmp_btn.pack(side="right")
        self.cmp_btn.bind("<ButtonPress-1>", self._show_original)
        self.cmp_btn.bind("<ButtonRelease-1>", self._show_graded)
        self.adj_btn = self._chip(row, "Justera  ▲", self._toggle_adjust)
        self.adj_btn.pack(side="right", padx=10)
        # packas SIST: pack ger utrymme i packordning, så när raden är för
        # smal (långt filnamn, smalt fönster) är det info-texten som kortas —
        # förut trycktes Justera-knappen ihop till "tera"
        self.info_lbl = tk.Label(row, text="", bg=PAPER, fg=FAINT,
                                 font=self.sf(9, "italic"), anchor="w")
        self.info_lbl.pack(side="left", padx=(4, 0), fill="x", expand=True)

        strip_wrap = tk.Frame(bottom, bg=PAPER, height=CARD_IMG_H + 46)
        strip_wrap.pack(fill="x")
        strip_wrap.pack_propagate(False)
        self.strip = tk.Canvas(strip_wrap, bg=PAPER, highlightthickness=0,
                               bd=0)
        self._arrow(strip_wrap, "‹",
                    lambda: self.strip.xview_scroll(-4, "units")).pack(
            side="left", fill="y", padx=(8, 2))
        self._arrow(strip_wrap, "›",
                    lambda: self.strip.xview_scroll(4, "units")).pack(
            side="right", fill="y", padx=(2, 8))
        self.strip.pack(fill="both", expand=True, pady=(2, 8))
        self.strip_inner = tk.Frame(self.strip, bg=PAPER)
        self.strip.create_window((0, 0), window=self.strip_inner, anchor="nw")
        for key, label, sub, _g in FILMS:
            if key == "hp5":       # tunn avdelare: färgfilmer | svartvitt
                tk.Frame(self.strip_inner, bg=LINE, width=1).pack(
                    side="left", fill="y", padx=7, pady=10)
            self._make_card(key, label, sub)
        self.strip_inner.bind(
            "<Configure>",
            lambda e: self.strip.configure(scrollregion=self.strip.bbox("all")))
        for w in (self.strip, self.strip_inner):
            w.bind("<MouseWheel>", self._wheel_strip)
        self._mark_card("original")

        # ---- fotovägg (mitten) ----
        # width/height=1: canvasen fyller ändå allt ledigt utrymme (expand),
        # men dess STANDARDBEGÄRAN (7 cm, vid 200 % DPI ~530 px) fick pack att
        # klämma bort den sist packade widgeten — beskärningsraden — när
        # fönstret inte rymde allt
        self.canvas = tk.Canvas(self, bg=WALL, highlightthickness=0, bd=0,
                                width=1, height=1)
        self.canvas.pack(fill="both", expand=True, side="top")
        self.canvas.create_text(0, 0, text="Öppna ett foto för att börja",
                                 fill=INK2, font=self.sf(13, "italic"),
                                 tags="hint")
        self.canvas.bind("<ButtonPress-1>", self._on_canvas_press)
        self.canvas.bind("<B1-Motion>", self._on_canvas_motion)
        self.canvas.bind("<ButtonRelease-1>", self._on_canvas_release)
        self.canvas.bind("<Double-Button-1>", self._reset_zoom)
        self.canvas.bind("<MouseWheel>", self._on_zoom)
        # panorera genom att hålla ner mittenknappen (skrollhjulet) —
        # alternativ till vänsterklick-drag, som i galleriläge (ozoomat)
        # redan är upptagen av "håll för original". Knappnumret är
        # plattformsberoende (se mouse_buttons).
        mid = MIDDLE_BTN[-2]
        self.canvas.bind(MIDDLE_BTN, self._on_pan_press)
        self.canvas.bind(f"<B{mid}-Motion>", self._on_canvas_motion)
        self.canvas.bind(f"<ButtonRelease-{mid}>", self._on_canvas_release)
        self.canvas.bind(
            "<Configure>",
            lambda e: self.canvas.coords("hint", e.width / 2, e.height / 2))

        # ---- Justera-panel ----
        self.adjust = tk.Frame(self, bg=PAPER2, highlightthickness=1,
                               highlightbackground=LINE)
        pad = tk.Frame(self.adjust, bg=PAPER2, padx=16, pady=14)
        pad.pack(fill="both", expand=True)
        self._panel_title(pad, "⠿  Justera", self._toggle_adjust,
                          self._adj_drag_press, self._adj_drag_move)

        # histogram
        self.hist = tk.Canvas(pad, width=224, height=54, bg=MAT,
                              highlightthickness=1, highlightbackground=LINE,
                              bd=0)
        self.hist.pack(fill="x", pady=(0, 8))
        self._hist_arr = None       # senaste bilden, för omritning vid resize
        self.hist.bind("<Configure>", lambda e: self._update_histogram(
            self._hist_arr, force=True))

        # verktyg: auto · pipett · beskär
        trow = tk.Frame(pad, bg=PAPER2)
        trow.pack(fill="x", pady=(0, 4))
        self._chip(trow, "Auto", self.auto_enhance).pack(
            side="left", fill="x", expand=True, padx=(0, 3))
        self._chip(trow, "Beskär", self.enter_crop).pack(
            side="left", fill="x", expand=True, padx=3)
        trow2 = tk.Frame(pad, bg=PAPER2)
        trow2.pack(fill="x", pady=(0, 8))
        self.curve_btn = self._chip(trow2, "Kurva", self.toggle_curve)
        self.curve_btn.pack(side="left", fill="x", expand=True, padx=(0, 3))
        self._chip(trow2, "Preset +", self.save_preset).pack(
            side="left", fill="x", expand=True, padx=(3, 0))

        # reglagen i TVÅ kolumner (inte en lång stapel) — annars blir
        # panelen ~880px hög och får inte plats ovanför styrka-raden/
        # filmremsan i normalstora fönster, vilket fick den att överlappa
        # dem. Egen wrap-frame krävs: `pad` packar redan andra widgetar
        # ovanför/under, och grid+pack kan inte blandas i SAMMA förälder.
        self.sliders = {}
        specs = [("exposure", "exponering", -2, 2, "{:+.1f}"),
                 ("contrast", "kontrast", -100, 100, "{:+.0f}"),
                 ("clarity", "klarhet", -100, 100, "{:+.0f}"),
                 ("sharpen", "skärpa", 0, 100, "{:.0f}"),
                 ("saturation", "mättnad", -100, 100, "{:+.0f}"),
                 ("temp", "värme", -100, 100, "{:+.0f}"),
                 ("tint", "färgton", -100, 100, "{:+.0f}"),
                 ("fade", "blekning", -100, 100, "{:+.0f}"),
                 ("grain", "korn", -100, 100, "{:+.0f}"),
                 ("vignette", "vinjett", -100, 100, "{:+.0f}"),
                 ("halation", "halation", -100, 100, "{:+.0f}")]
        grid_wrap = tk.Frame(pad, bg=PAPER2)
        grid_wrap.pack(fill="x")
        grid_wrap.columnconfigure(0, weight=1)
        grid_wrap.columnconfigure(1, weight=1)

        def _grid_slider(i, s):
            r, c = divmod(i, 2)
            s.grid(row=r, column=c, padx=(0, 14) if c == 0 else 0,
                  pady=4, sticky="ew")

        for i, (key, lab, lo, hi, fmt) in enumerate(specs):
            s = Track(grid_wrap, lab, lo, hi, self._request_render, fmt=fmt,
                      bg=PAPER2, width=170, serif=(self.serif, 9),
                      on_grab=self._push_undo)
            _grid_slider(i, s)
            self.sliders[key] = s
        self.gsize = Track(grid_wrap, "kornstorlek", 1.0, 5.0,
                           self._request_render, fmt="{:.1f} px", bg=PAPER2,
                           width=170, serif=(self.serif, 9),
                           on_grab=self._push_undo)
        self.gsize.set(DEFAULT_GS)
        _grid_slider(len(specs), self.gsize)
        self.grough = Track(grid_wrap, "kornstruktur", 0, 100,
                            self._request_render, fmt="{:.0f}", bg=PAPER2,
                            width=170, serif=(self.serif, 9),
                            on_grab=self._push_undo)
        self.grough.set(0)
        _grid_slider(len(specs) + 1, self.grough)
        tk.Frame(pad, bg=LINE, height=1).pack(fill="x", pady=(10, 10))
        self._chip(pad, "Nollställ", self.reset_adjust).pack(fill="x")

        # ---- Beskärnings-panel (nere, visas bara i beskärningsläge) ----
        self.crop_bar = tk.Frame(self, bg=PAPER, highlightthickness=1,
                                 highlightbackground=LINE)
        cb = tk.Frame(self.crop_bar, bg=PAPER, padx=20, pady=8)
        cb.pack(fill="x")
        tk.Label(cb, text="Beskär", bg=PAPER, fg=INK,
                 font=self.sf(12, "italic")).pack(side="left", padx=(0, 14))
        for lab, ratio in [("Fri", None), ("Original", "orig"), ("1:1", 1.0),
                           ("3:2", 1.5), ("4:3", 4 / 3), ("16:9", 16 / 9),
                           ("2:3", 2 / 3), ("4:5", 4 / 5)]:
            self._chip(cb, lab, lambda r=ratio: self._set_aspect(r)).pack(
                side="left", padx=2)
        # step=0.1: spann-heuristiken hade gett HELA grader trots att
        # formatet visar en decimal — finjustering av horisonten kräver
        # tiondelar (en hel grad är väldigt synlig i ett horisontfoto)
        self.straight = Track(cb, "räta upp", -15, 15, self._on_straighten,
                              fmt="{:+.1f}°", bg=PAPER, width=150,
                              serif=(self.serif, 9), step=0.1)
        self.straight.pack(side="left", padx=(14, 14))
        self._chip(cb, "Klar", self._apply_crop, primary=True).pack(
            side="right", padx=(4, 0))
        self._chip(cb, "Avbryt", self._cancel_crop).pack(side="right", padx=4)

        # ---- Kurv-panel (flytande, togglas med "Kurva") ----
        self.curvewin = tk.Frame(self, bg=PAPER2, highlightthickness=1,
                                 highlightbackground=LINE)
        cpad = tk.Frame(self.curvewin, bg=PAPER2, padx=14, pady=12)
        cpad.pack(fill="both", expand=True)
        self._panel_title(cpad, "⠿  Tonkurva", self.toggle_curve,
                          self._cw_drag_press, self._cw_drag_move)
        self.curve_ed = CurveEditor(cpad, on_change=self._request_render,
                                    on_grab=self._push_undo, bg=PAPER2,
                                    serif=(self.serif, 8))
        self.curve_ed.pack()
        tk.Label(cpad, text="klicka = ny punkt · högerklick = ta bort",
                 bg=PAPER2, fg=FAINT, font=self.sf(8, "italic")).pack(
            anchor="w", pady=(4, 6))
        self._chip(cpad, "Återställ kurva", self._reset_curve).pack(fill="x")
        self._cw_x = self._cw_y = 0.0
        self._cw_init = False

        # ---- Export-panel (flytande, togglas med "Format") ----
        self.exportwin = tk.Frame(self, bg=PAPER2, highlightthickness=1,
                                  highlightbackground=LINE)
        epad = tk.Frame(self.exportwin, bg=PAPER2, padx=14, pady=12)
        epad.pack(fill="both", expand=True)
        self._panel_title(epad, "⠿  Exportformat", self._toggle_export_panel,
                          self._ew_drag_press, self._ew_drag_move, pady=(0, 8))
        frow = tk.Frame(epad, bg=PAPER2)
        frow.pack(fill="x", pady=(0, 6))
        for fmt in ("JPEG", "PNG", "TIFF"):
            b = self._chip(frow, fmt, lambda f=fmt: self._set_export_fmt(f))
            b.pack(side="left", fill="x", expand=True, padx=2)
            self._fmt_cards[fmt] = b
        tk.Label(epad, text="PNG/TIFF = förlustfritt (större filer)",
                 bg=PAPER2, fg=FAINT, font=self.sf(8, "italic")).pack(
            anchor="w", pady=(0, 8))
        self.qtrack = Track(epad, "JPEG-kvalitet", 60, 100, self._on_quality,
                            fmt="{:.0f}", bg=PAPER2, width=200,
                            serif=(self.serif, 9))
        self.qtrack.set(self.jpeg_quality)
        self.qtrack.pack(fill="x", pady=2)
        self._ew_x = self._ew_y = 0.0
        self._ew_init = False
        self._mark_export_fmt()

    # ---------- små byggblock ----------
    def _chip(self, parent, text, cmd, primary=False):
        bg = ACCENT if primary else PAPER2
        fg = ONACCENT if primary else INK
        b = tk.Label(parent, text=text, bg=bg, fg=fg, font=self.sf(10),
                     padx=13, pady=6, cursor="hand2",
                     highlightthickness=1,
                     highlightbackground=ACCENT if primary else LINE)
        b._base = (bg, fg)
        b._primary = primary
        b._disabled = False
        if cmd:
            # en inaktiverad knapp ska inte göra något — förut ändrade
            # _set_enabled bara färgen och kommandot kördes ändå
            b.bind("<Button-1>", lambda e: None if b._disabled else cmd())
        b.bind("<Enter>", lambda e: (not getattr(b, "_disabled", False))
               and b.configure(bg=ACCENT_HI if primary else PAPER))
        b.bind("<Leave>", lambda e: b.configure(bg=b._base[0]))
        return b

    def _set_enabled(self, btn, on):
        btn._disabled = not on
        btn.configure(fg=btn._base[1] if on else FAINT,
                      cursor="hand2" if on else "arrow")

    def _tool_sep(self, parent):
        """Tunn vertikal avdelare mellan knappgrupper i verktygsraden."""
        tk.Frame(parent, bg=LINE, width=1, height=24).pack(
            side="right", padx=8)

    def _arrow(self, parent, txt, cmd):
        """Diskret scrollpil vid remsornas kanter — utan den syns det inte
        att remsan fortsätter utanför fönstret."""
        a = tk.Label(parent, text=txt, bg=PAPER, fg=FAINT,
                     font=(self.serif, 14), cursor="hand2", padx=4)
        a.bind("<Button-1>", lambda e: cmd())
        a.bind("<Enter>", lambda e: a.configure(fg=ACCENT))
        a.bind("<Leave>", lambda e: a.configure(fg=FAINT))
        return a

    def _panel_title(self, parent, text, on_close, drag_press=None,
                     drag_move=None, pady=(0, 6)):
        """Titelrad för en flytande panel (Justera/Kurva/Format): dragbar
        rubrik till vänster + ett neutralt ×-kryss till höger som stänger
        panelen. Delad så alla flytande paneler stänger likadant."""
        row = tk.Frame(parent, bg=PAPER2)
        row.pack(anchor="w", fill="x", pady=pady)
        lbl = tk.Label(row, text=text, bg=PAPER2, fg=INK,
                       font=self.sf(12, "italic"),
                       cursor="fleur" if drag_press else "arrow", anchor="w")
        lbl.pack(side="left", fill="x", expand=True)
        if drag_press:
            lbl.bind("<ButtonPress-1>", drag_press)
        if drag_move:
            lbl.bind("<B1-Motion>", drag_move)
        close = tk.Label(row, text="×", bg=PAPER2, fg=FAINT,
                         font=self.sf(13), cursor="hand2", padx=4)
        close.bind("<Button-1>", lambda e: on_close())
        close.bind("<Enter>", lambda e: close.configure(fg=ACCENT))
        close.bind("<Leave>", lambda e: close.configure(fg=FAINT))
        close.pack(side="right")
        return row

    def _make_card(self, key, label, sub):
        outer = tk.Frame(self.strip_inner, bg=PAPER, padx=3, pady=3)
        outer.pack(side="left", padx=5)
        inner = tk.Frame(outer, bg=MAT, highlightthickness=1,
                         highlightbackground=LINE)
        inner.pack()
        img_lbl = tk.Label(inner, image=self._blank, bg=MAT, bd=0, padx=3,
                           pady=3)
        img_lbl.pack()
        name = tk.Label(inner, text=label, bg=MAT, fg=INK2,
                        font=self.sf(8, "italic"), pady=2)
        name.pack(fill="x")
        for w in (outer, inner, img_lbl, name):
            w.bind("<Button-1>", lambda e, k=key: self.select_film(k))
            w.bind(RIGHT_BTN, lambda e, k=key: self._delete_preset(k))
            w.bind("<MouseWheel>", self._wheel_strip)
            w.bind("<Enter>", lambda e, k=key: self._card_hover(k, True))
            w.bind("<Leave>", lambda e, k=key: self._card_hover(k, False))
        self._cards[key] = {"outer": outer, "inner": inner,
                            "img": img_lbl, "name": name}

    def _card_hover(self, key, on):
        if key == self.film_key:
            return
        self._cards[key]["outer"].configure(bg=FAINT if on else PAPER)

    def _mark_card(self, key):
        for k, c in self._cards.items():
            active = (k == key)
            c["outer"].configure(bg=ACCENT if active else PAPER)
            c["name"].configure(fg=INK if active else INK2,
                                font=self.sf(8, "bold") if active
                                else self.sf(8, "italic"))

    def _wheel_strip(self, e):
        self.strip.xview_scroll(-1 if e.delta > 0 else 1, "units")

    # ---------- foto-rullens kort ----------
    @staticmethod
    def _short_name(path, maxlen=12):
        """Filnamn utan ändelse, avkortat — 'R0003234 (2).JPG' skräpar bara
        ner rullen i full längd."""
        base = os.path.splitext(os.path.basename(path))[0]
        return base if len(base) <= maxlen else base[:maxlen - 1] + "…"

    def _make_roll_card(self, item, count=True):
        outer = tk.Frame(self.roll_inner, bg=PAPER, padx=3, pady=3)
        outer.pack(side="left", padx=4)
        inner = tk.Frame(outer, bg=MAT, highlightthickness=1,
                         highlightbackground=LINE)
        inner.pack()
        img_lbl = tk.Label(inner, image=self._roll_blank, bg=MAT, bd=0,
                           padx=2, pady=2)
        img_lbl.pack()
        name = tk.Label(inner, text=self._short_name(item.path), bg=MAT,
                        fg=FAINT, font=self.sf(7), pady=1)
        name.pack(fill="x")
        for w in (outer, inner, img_lbl, name):
            w.bind("<ButtonPress-1>",
                   lambda e, u=item.uid: self._roll_press(e, u))
            w.bind("<B1-Motion>",
                   lambda e, u=item.uid: self._roll_motion(e, u))
            w.bind("<ButtonRelease-1>",
                   lambda e, u=item.uid: self._roll_release(e, u))
            w.bind("<Shift-Button-1>",
                   lambda e, u=item.uid: self._set_compare(u))
            w.bind("<MouseWheel>", self._wheel_roll)
        self._roll_cards[item.uid] = {"outer": outer, "inner": inner,
                                      "img": img_lbl, "name": name}
        self._update_roll_card(item, count)

    # ---------- foto-rulle: dra för att ändra ordning ----------
    def _roll_press(self, e, uid):
        if e.state & 0x0001:      # Skift hanteras separat av _set_compare
            self._roll_drag = None
            return
        self._roll_drag = {"uid": uid, "engaged": False, "reordered": False,
                           "undo_pushed": False, "start_root_x": e.x_root,
                           "active_uid": (self.session[self.active_idx].uid
                                         if self.active_idx is not None
                                         else None)}

    def _roll_motion(self, e, uid):
        rd = self._roll_drag
        if not rd or rd["uid"] != uid:
            return
        if not rd["engaged"]:
            if abs(e.x_root - rd["start_root_x"]) < 6:
                return
            rd["engaged"] = True    # nog rörelse för att BÖRJA överväga drag
        cur_idx = next((i for i, it in enumerate(self.session)
                       if it.uid == uid), None)
        if cur_idx is None:
            return
        # räkna ut måletindex bland de ANDRA korten (utan att mutera listan
        # än) — små ryck (särskilt vid hög DPI-skalning) passerar lätt
        # 6px-tröskeln utan att pekaren faktiskt lämnat sitt eget kort; om
        # måletindex blir samma som nuvarande plats är det fortfarande bara
        # ett klick, och release ska då aktivera fotot precis som vanligt
        # (annars "försvann" tidigare ändringar bara för att bytet aldrig
        # gjordes — ordningen "ändrades" tyst till exakt samma ordning)
        self.update_idletasks()
        target = 0
        for it in self.session:
            if it.uid == uid:
                continue
            c = self._roll_cards.get(it.uid)
            if not c:
                continue
            cx = c["outer"].winfo_rootx() + c["outer"].winfo_width() / 2
            if e.x_root > cx:
                target += 1
        target = max(0, min(len(self.session) - 1, target))
        if target == cur_idx:
            return                  # ingen faktisk platsändring — förbli "klick"
        if not rd["undo_pushed"]:
            self._push_undo()       # gör drag-omordningen ångringsbar
            rd["undo_pushed"] = True
        rd["reordered"] = True
        item = self.session.pop(cur_idx)
        self.session.insert(target, item)
        self._repack_roll_cards()
        if rd["active_uid"] is not None:
            self.active_idx = next(
                i for i, it in enumerate(self.session)
                if it.uid == rd["active_uid"])

    def _roll_release(self, e, uid):
        rd = self._roll_drag
        self._roll_drag = None
        if not rd or rd["uid"] != uid:
            return
        if not rd["reordered"]:
            if self._compare_mode:
                # aktiv (vänster) är låst i jämförelseläget — klick i rullen
                # väljer istället höger jämförelsefoto (inget behov av Skift)
                self._set_compare(uid)
            else:
                self._on_roll_click(uid)      # rent klick = välj foto
        else:
            self._update_info()               # aktivt index kan ha skiftat
            self.name_lbl.configure(text="Ordning ändrad")

    def _repack_roll_cards(self):
        """Packa om rullens kort i self.session-ordning UTAN att riva/bygga
        om dem — snabbt (ingen omrendering), används vid dragomordning."""
        for it in self.session:
            c = self._roll_cards.get(it.uid)
            if c:
                c["outer"].pack_forget()
        for it in self.session:
            c = self._roll_cards.get(it.uid)
            if c:
                c["outer"].pack(side="left", padx=4)

    @staticmethod
    def _edit_sig(edit):
        """Hashbar signatur av ett recept — avgör om en cachad rendering
        fortfarande gäller."""
        return (edit.film_key, tuple(sorted(edit.adjust.items())),
                edit.grain_size, edit.grain_rough, edit.strength,
                tuple(edit.crop), edit.angle,
                _curve_key(edit.curve) if edit.curve else None)

    def _roll_thumb_image(self, item):
        """Rendera ett rullkorts tumnagel (hela pipelinen på 240 px-källan)."""
        base = apply_geometry(item.thumb_src, item.edit.crop, item.edit.angle)
        graded = process(base, grade_from_edit(item.edit))
        arr = blend_strength(base, graded, item.edit.strength / 100.0)
        pil = ImageOps.fit(to_pil(arr), (ROLL_W, ROLL_H), Image.LANCZOS)
        return ImageTk.PhotoImage(pil, master=self)

    def _update_roll_card(self, item, count=True):
        c = self._roll_cards.get(item.uid)
        if not c:
            return
        # rendera bara om receptet ändrats sedan förra renderingen — ångra/
        # gör om, "Synka → alla" och tembyte rev och renderade förut om
        # ALLA kort (40 foton ≈ 0.5 s frys vid varje ångra)
        sig = self._edit_sig(item.edit)
        cached = self._roll_img_cache.get(item.uid)
        if cached and cached[0] == sig:
            tkimg = cached[1]
        else:
            tkimg = self._roll_thumb_image(item)
            self._roll_img_cache[item.uid] = (sig, tkimg)
        c["img"].configure(image=tkimg)
        c["img"]._keep = tkimg       # håll referens (annars GC:as bilden)
        short = self._short_name(item.path)
        edited = not item.edit.is_default()
        mark = {1: "✓ ", -1: "✗ "}.get(item.rating, "")
        prefix = mark or ("● " if edited else "")
        color = {1: GOOD, -1: BAD}.get(item.rating,
                                       ACCENT if edited else FAINT)
        c["name"].configure(text=prefix + short, fg=color)
        border, bw = self._roll_border(item)
        c["inner"].configure(highlightbackground=border, highlightthickness=bw)
        if count:                    # batchanrop räknar EN gång på slutet
            self._refresh_roll_count()

    def _roll_border(self, item):
        """Kantfärg+tjocklek för ett rullkort: jämförelsemålet (senapsgul,
        tjockare) vinner över betygsfärg (grön/röd) — betyget syns ändå kvar
        via ✓/✗-prefixet i namnet, ingen information går förlorad."""
        if self._compare_mode and item.uid == self._compare_uid:
            return CMP, 2
        return {1: GOOD, -1: BAD}.get(item.rating, LINE), 1

    def _refresh_compare_borders(self):
        """Uppdatera ENDAST kantfärgen (ingen omrendering av tumnaglar) på
        alla rullkort — körs varje gång jämförelsemålet eller -läget ändras."""
        for it in self.session:
            c = self._roll_cards.get(it.uid)
            if c:
                border, bw = self._roll_border(it)
                c["inner"].configure(highlightbackground=border,
                                     highlightthickness=bw)

    def _mark_roll_active(self, uid):
        for u, c in self._roll_cards.items():
            c["outer"].configure(bg=ACCENT if u == uid else PAPER)

    def _on_roll_click(self, uid):
        idx = next((i for i, it in enumerate(self.session) if it.uid == uid),
                   None)
        if idx is not None and idx != self.active_idx:
            self._activate(idx)

    def _wheel_roll(self, e):
        self.roll_strip.xview_scroll(-1 if e.delta > 0 else 1, "units")

    def _refresh_roll_count(self):
        n = len(self.session)
        if n == 0:
            txt = "Inga foton importerade"
        else:
            txt = "1 foto" if n == 1 else f"{n} foton"
            edited = sum(1 for it in self.session
                         if not it.edit.is_default())
            if edited:
                txt += f" · {edited} redigerade"
        self.roll_count_lbl.configure(text=txt)

    # ---------------------------------------------------------- åtgärder
    def open_image(self):
        paths = filedialog.askopenfilenames(title="Öppna foton",
                                            filetypes=IMG_TYPES)
        # askopenfilenames kan returnera en enda Tcl-sträng (t.ex. när sökvägar
        # har mellanslag) — splitlist gör alltid en korrekt lista av filer
        if isinstance(paths, str):
            paths = self.tk.splitlist(paths)
        if not paths:
            return
        self._import_paths(paths)

    def _import_paths(self, paths):
        """Gemensam importväg för Öppna-dialogen OCH dra-och-släpp. En vald
        .filmrulle-projektfil öppnas som PROJEKT — Öppna-knappen är appens
        enda öppningsväg, så den måste kunna ta emot båda (annars går sparade
        projekt aldrig att öppna igen)."""
        proj = next((p for p in paths
                     if os.path.splitext(p)[1].lower() == SESSION_EXT), None)
        if proj is not None:
            self._load_session_file(proj)
            return
        paths = [p for p in paths
                 if os.path.splitext(p)[1].lower() in IMPORT_EXTS]
        if not paths:
            self.name_lbl.configure(text="Inga bildfiler att importera")
            return
        if self._importing:    # kolla FÖRE ångra-snapshoten (annars ett
            self.name_lbl.configure(text="Import pågår redan — vänta")
            return             # tomt ångra-steg för en import som aldrig sker)
        self._push_undo()      # så en import kan ångras (no-op om rullen tom)
        self._start_import([(p, None) for p in paths])

    def _start_import(self, jobs):
        """Starta trådad inläsning av (path, meta)-jobb. All fil-I/O och
        nedskalning sker i EN arbetartråd (inget Tk rörs där); färdiga
        arrayer levereras via self._iq och plockas upp av _drain_import på
        GUI-tråden, som bygger PhotoItem + rullkort. Innan detta lästes
        allt synkront på GUI-tråden — 30 st 24MP-foton frös hela appen i
        ~30s utan någon feedback alls."""
        if self._importing:
            self.name_lbl.configure(text="Import pågår redan — vänta")
            return
        self._importing = True
        self._imp_added = self._imp_failed = 0
        self._imp_err = None           # första felorsaken (visas i summeringen)
        self.name_lbl.configure(text=f"Importerar 0/{len(jobs)} …")
        threading.Thread(target=self._import_worker, daemon=True,
                         args=(list(jobs),)).start()

    def _import_worker(self, jobs):
        """Arbetartråd: läser en förhandsvisning per fil (inget Tk rörs här).
        Fullupplösningen hålls INTE i RAM — den läses om från disk vid
        Spara/Exportera — så importen behöver bara förhandsvisningen, som
        `load_preview` tar fram 3–6× snabbare än en full avkodning."""
        n = len(jobs)
        for i, (path, meta) in enumerate(jobs):
            try:
                prev, w, h = load_preview(path)
                self._iq.put(("photo", make_photo_item(path, prev, w, h),
                              meta))
            except Exception as e:  # noqa: BLE001 — hoppa över trasig fil,
                log_error(f"import {path}")          # men BEHÅLL orsaken
                self._iq.put(("fail", str(e) or type(e).__name__))
            self._iq.put(("prog", i + 1, n))
        self._iq.put(("idone",))

    def _drain_import(self):
        """Plocka upp färdiglästa foton från importtråden (GUI-tråden)."""
        try:
            while True:
                msg = self._iq.get_nowait()
                kind = msg[0]
                if kind == "photo":
                    _, item, meta = msg
                    if meta is not None:
                        item.edit, item.rating = meta
                    self.session.append(item)
                    self._make_roll_card(item)
                    self._imp_added += 1
                    self._set_enabled(self.export_btn, True)
                    self._set_enabled(self.savesess_btn, True)
                    if self.active_idx is None:
                        self._activate(len(self.session) - 1)
                    else:
                        # info-raden visade annars "(1/1)" efter en import
                        # av flera foton — totalen sattes bara vid aktivering
                        self._update_info()
                elif kind == "fail":
                    self._imp_failed += 1
                    if len(msg) > 1 and msg[1] and self._imp_err is None:
                        self._imp_err = msg[1][:90]
                elif kind == "prog":
                    _, done, total = msg
                    self.name_lbl.configure(
                        text=f"Importerar {done}/{total} …")
                elif kind == "idone":
                    self._importing = False
                    self._refresh_roll_count()
                    if self._pending_active is not None and self.session:
                        idx = min(self._pending_active,
                                  len(self.session) - 1)
                        self._pending_active = None
                        self.active_idx = None
                        self._activate(max(0, idx))
                    added, failed = self._imp_added, self._imp_failed
                    if not added and failed:
                        msgtxt = "Kunde inte öppna de valda filerna"
                    else:
                        msgtxt = ("1 foto importerat" if added == 1
                                  else f"{added} foton importerade")
                        if failed:
                            msgtxt += f" ({failed} misslyckades)"
                    if failed and self._imp_err:
                        msgtxt += f" — {self._imp_err}"
                    self.name_lbl.configure(text=msgtxt)
        except queue.Empty:
            pass

    def _build_thumbs(self):
        """Rendera filmremsans kort CHUNKAT över event-loopen (3 kort per
        after(1)-varv) istället för alla 18+ synkront — hela pipelinen
        gånger antalet filmer tog 50–150ms i EN klump vid varje fotobyte,
        vilket gjorde klickandet i rullen märkbart segt. Ett nytt anrop
        (nytt foto aktiverat) avbryter pågående kö och börjar om med den
        nya källan."""
        src = self._geo_thumb if self._geo_thumb is not None else self.thumb_src
        if src is None:
            return
        if self._thumb_after:
            self.after_cancel(self._thumb_after)
            self._thumb_after = None
        self._thumb_src_ref = src
        self._thumb_queue = list(FILMS)
        self._thumb_step()

    def _thumb_step(self):
        self._thumb_after = None
        src = self._thumb_src_ref
        if src is None:
            return
        for _ in range(3):
            if not self._thumb_queue:
                return
            key, _label, _sub, g = self._thumb_queue.pop(0)
            card = self._cards.get(key)
            if card is None:
                continue
            pil = ImageOps.fit(to_pil(process(src, g)),
                               (CARD_W, CARD_IMG_H), Image.LANCZOS)
            tkimg = ImageTk.PhotoImage(pil, master=self)
            self._thumbs[key] = tkimg
            try:
                card["img"].configure(image=tkimg)
            except tk.TclError:
                return          # widget riven (tembyte) — kön är förlegad
        if self._thumb_queue:
            self._thumb_after = self.after(1, self._thumb_step)

    def _cycle_film(self, step):
        keys = [f[0] for f in FILMS]
        cur = keys.index(self.film_key) if self.film_key in keys else 0
        self.select_film(keys[(cur + step) % len(keys)])

    # ---------- foto-rulle: aktivt foto & dess redigeringstillstånd ----------
    def _ui_edit_state(self):
        """Läs av UI:t (film + reglage + geometri + kurva) som ett
        fristående EditState."""
        return EditState(
            film_key=self.film_key,
            adjust={k: (self.sliders[k].get() if k in self.sliders else 0.0)
                   for k in ADJ_FIELDS},
            grain_size=self.gsize.get(),
            grain_rough=self.grough.get(),
            strength=self.strength.get(),
            crop=tuple(self._cur_crop),
            angle=self._cur_angle,
            curve=self.curve_ed.points_or_none())

    def _sync_ui_to_active(self):
        """Spara UI:ts nuvarande tillstånd till det foto som är aktivt just
        nu — innan man hoppar till ett annat foto eller exporterar allt."""
        if self.active_idx is None:
            return
        item = self.session[self.active_idx]
        item.edit = self._ui_edit_state()
        self._update_roll_card(item)

    def _load_edit_into_ui(self, edit):
        entry = film_entry(edit.film_key)     # raderad preset → Original
        self.film_key = entry[0]
        self._mark_card(self.film_key)
        self.film_name.configure(text=entry[1])
        for k, s in self.sliders.items():
            s.set(edit.adjust.get(k, 0.0))
        self.gsize.set(edit.grain_size)
        self.grough.set(edit.grain_rough)
        self.strength.set(edit.strength)
        self.intensity = edit.strength / 100.0
        self._cur_crop = tuple(edit.crop)
        self._cur_angle = edit.angle
        self.curve_ed.set_points(edit.curve)

    def _activate(self, idx):
        if not (0 <= idx < len(self.session)):
            return
        if self._crop_mode:
            # en väntande (ej tillämpad) beskärning hör till det FÖRRA fotot.
            # Utan detta låg läget kvar och "Klar" applicerade den rutan på
            # det nya fotot, medan canvasen fortfarande visade det gamla.
            self._exit_crop()
        self._sync_ui_to_active()      # spara det förra fotots ändringar
        self.active_idx = idx
        item = self.session[idx]
        self.prev_arr = as_float01(item.prev_arr)   # uint8 → float, bara aktivt
        self.thumb_src = item.thumb_src
        self.src_path = item.path
        self.zoom = 1.0
        self.pan_x = self.pan_y = 0.0
        self._load_edit_into_ui(item.edit)
        self._apply_geo(render=False)     # geo_arr/geo_thumb för nya fotot
        self.canvas.delete("hint")
        self._update_info()
        self._build_thumbs()
        self._mark_roll_active(item.uid)
        self._set_enabled(self.save_btn, True)
        self._request_render()

    def _apply_geo(self, render=True):
        """Räkna om räta-upp+beskär-resultatet för förhandsvisning + remsan."""
        if self.prev_arr is None:
            return
        self.geo_arr = apply_geometry(self.prev_arr, self._cur_crop,
                                      self._cur_angle)
        self._geo_thumb = apply_geometry(self.thumb_src, self._cur_crop,
                                         self._cur_angle)
        self.graded_arr = None            # gamla graden har fel dimensioner
        if self._curve_open:              # nytt foto/geometri -> nya toner
            self.curve_ed.set_histogram(self.geo_arr)
        if render:
            self._build_thumbs()
            self._request_render()

    def _update_info(self):
        """Uppdatera bildinfo-labeln (filnamn · mått · index i rullen)."""
        if self.active_idx is None or not self.session:
            self.info_lbl.configure(text="")
            return
        item = self.session[self.active_idx]
        self.info_lbl.configure(
            text=f"{os.path.basename(item.path)}  ·  {item.w}×{item.h}"
                 f"   ({self.active_idx + 1}/{len(self.session)})")

    def _nav(self, step):
        """Ctrl+Vänster/Höger: bläddra till förra/nästa foto i rullen."""
        if not self.session or self.active_idx is None:
            return
        if self._compare_mode:
            self.name_lbl.configure(
                text="Aktivt foto är låst i jämförelseläget — tryck Jämför "
                     "för att lämna")
            return
        new = max(0, min(len(self.session) - 1, self.active_idx + step))
        if new != self.active_idx:
            self._activate(new)

    def _reset_grain_to_film(self):
        """Kornstorlek/-struktur till FILMENS egna värden (Tri-X 1.6, en
        sparad presets inbakade korn) — inte alltid 1.5/0."""
        g = film_entry(self.film_key)[3]
        self.gsize.set(g.grain_size)
        self.grough.set(g.grain_rough)

    def select_film(self, key):
        if key == self.film_key:
            self.reset_adjust()       # klick på vald film = nollställ look
            return
        entry = film_entry(key)
        self._push_undo()
        self.film_key = entry[0]
        self._mark_card(self.film_key)
        self.film_name.configure(text=entry[1])
        for s in self.sliders.values():
            s.reset()
        self._reset_grain_to_film()
        self.curve_ed.reset()     # dokumenterat: "nollställs när du byter
        self.strength.set(100)    # film" — tonkurvan är en del av looken
        self.intensity = 1.0
        self._request_render()

    def reset_adjust(self):
        self._push_undo()
        for s in self.sliders.values():
            s.reset()
        self._reset_grain_to_film()
        self.curve_ed.reset()
        self._request_render()

    # ---------------------------------------------------------- histogram
    def _update_histogram(self, arr, force=False):
        if not force:
            self._hist_arr = arr    # cacha senaste bilden för <Configure>-omritning
        c = self.hist
        # FAKTISK bredd, inte den vid-konstruktion konfigurerade — panelen
        # packar canvasen med fill="x", så den stretchar bredare än de
        # ursprungliga 224px och c["width"] skulle bara rita en smal remsa
        # i vänsterkanten av den bredare rutan (samma klass av bugg som
        # Track._width/CurveEditor._dims skyddar mot på andra håll).
        w = c.winfo_width()
        w = w if w > 4 else int(c["width"])
        hgt = c.winfo_height()
        hgt = hgt if hgt > 4 else int(c["height"])
        c.delete("all")
        if arr is None:
            return
        small = arr[::max(1, arr.shape[0] // 120),
                    ::max(1, arr.shape[1] // 160)]
        bins = 64
        chans = []
        for i in range(3):
            hval, _ = np.histogram(small[..., i], bins=bins, range=(0, 1))
            chans.append(hval)
        # robust normalisering (se _hist_peak) + klipp det som spiller över,
        # så mittonerna fyller ut rutan
        peak = _hist_peak(chans)
        cols = ("#b06a5f", "#5f8a5c", "#5f79a8")   # dämpad r/g/b
        for hval, col in zip(chans, cols):
            pts = [1, hgt - 1]
            for b in range(bins):
                x = 1 + b / (bins - 1) * (w - 2)
                y = (hgt - 1) - min(hval[b] / peak, 1.0) * (hgt - 3)
                pts += [x, y]
            pts += [w - 1, hgt - 1]
            c.create_polygon(pts, fill="", outline=col, width=1)

    # ---------------------------------------------------------- auto/pipett
    def auto_enhance(self):
        """Auto-nivåer (exponering + kontrast från percentiler) + vitbalans
        (värme/tint), beräknat på det rätade/beskurna källfotot."""
        if self.geo_arr is None:
            return
        self._push_undo()
        a = self.geo_arr
        lum = _luma(a)
        lo, hi = np.percentile(lum, [1.0, 99.0])
        mid = (float(lo) + float(hi)) / 2.0
        ev = _clampf(math.log2(0.5 / max(mid, 1e-3)), -1.2, 1.2)
        rng = max(float(hi) - float(lo), 1e-3)
        contrast = _clampf((0.80 / rng - 1.0) * 100.0, 0.0, 45.0)
        # vitbalans: NEUTRALITETSVIKTAD gråvärld, inte ren kanalmedel. Ren
        # gråvärld antar att bildens genomsnitt SKA vara neutralgrått — på
        # en scen som är äkta varm (solljus, gyllene timme) är det antagandet
        # fel, och metoden "korrigerar" bort just den värmen (kylde ner
        # sommarbilder kraftigt mot blått, klampade ofta i botten -60).
        # Här väger starkt mättade pixlar (himmel, direkt solljus) nästan
        # ingenting — bara de mer neutrala partierna (moln, hud, betong,
        # skuggor) får styra balansen — plus en generell dämpning så en
        # äkta kvarvarande färgcast justeras med en nudge, inte en full
        # utjämning.
        mx = a.max(axis=-1)
        mn = a.min(axis=-1)
        sat = mx - mn                              # 0 = grå, mot 1 = ren färg
        weight = np.clip(1.0 - sat * 2.5, 0.05, 1.0)
        wsum = float(weight.sum()) + 1e-6
        mr = float((a[..., 0] * weight).sum() / wsum)
        mg = float((a[..., 1] * weight).sum() / wsum)
        mb = float((a[..., 2] * weight).sum() / wsum)
        damp = 0.5
        temp = _clampf((mb - mr) / (0.35 * (mr + mb) + 1e-6) * 100.0 * damp,
                       -60, 60)
        m_rb = (mr + mb) / 2.0
        tint = _clampf((mg - m_rb) / (0.1 * m_rb + 0.2 * mg + 1e-6)
                       * 100.0 * damp, -50, 50)
        self.sliders["exposure"].set(round(ev, 1))
        self.sliders["contrast"].set(round(contrast))
        self.sliders["temp"].set(round(temp))
        self.sliders["tint"].set(round(tint))
        self._request_render()
        self.name_lbl.configure(text="Auto-förbättrad")

    # ---------------------------------------------------------- betyg
    def rate_active(self, rating):
        """p = utvald, x = ratad, u = neutral. Ratade hoppas över i export."""
        if self.active_idx is None:
            return
        item = self.session[self.active_idx]
        item.rating = 0 if item.rating == rating else rating
        self._update_roll_card(item)
        self._mark_roll_active(item.uid)
        txt = {1: "Utvald ✓", -1: "Ratad ✗", 0: "Betyg rensat"}[item.rating]
        self.name_lbl.configure(text=txt)

    # ---------------------------------------------------------- egna presets
    def save_preset(self):
        """Spara nuvarande look (film + reglage + kurva) som eget kort."""
        if self.active_idx is None:
            return
        name = simpledialog.askstring("Spara preset", "Namn på preset:",
                                      parent=self)
        if not name or not name.strip():
            return
        g = grade_from_edit(self._ui_edit_state())
        key = f"user_{int(time.time() * 1000)}"
        entry = (key, name.strip(), "egen preset", g)
        FILMS.append(entry)
        FILM_BY_KEY[key] = entry
        self._make_card(key, name.strip(), "egen preset")
        self._build_thumbs()
        save_user_presets()
        self.name_lbl.configure(
            text=f"Preset '{name.strip()}' sparad — högerklick tar bort")
        return key

    def _delete_preset(self, key):
        """Högerklick på ett eget preset-kort tar bort det (efter bekräftelse
        — raderingen går inte att ångra, och ett felklick raderade förut en
        look direkt). Foton i rullen, i ångra-/gör om-historiken och i
        urklippet som använder preseten faller tillbaka på Original; att
        bara uppdatera rullen gav KeyError vid nästa ångra."""
        if not key.startswith("user_") or key not in FILM_BY_KEY:
            return
        label = FILM_BY_KEY[key][1]
        if not self._confirm("Ta bort preset",
                             f"Ta bort preseten '{label}' permanent?"):
            return
        self._sync_ui_to_active()
        for it in self.session:
            if it.edit.film_key == key:
                it.edit.film_key = "original"
                self._update_roll_card(it, count=False)
        for snap in self._undo + self._redo:
            for e in snap["edits"].values():
                if e.film_key == key:
                    e.film_key = "original"
        if self._clipboard_edit and self._clipboard_edit.film_key == key:
            self._clipboard_edit.film_key = "original"
        entry = FILM_BY_KEY.pop(key)
        FILMS.remove(entry)
        card = self._cards.pop(key, None)
        if card:
            card["outer"].destroy()
        self._thumbs.pop(key, None)
        self._refresh_roll_count()
        save_user_presets()
        if self.film_key == key:
            self.film_key = "original"
            self._mark_card("original")
            self.film_name.configure(text="Original")
            self._request_render()
        self.name_lbl.configure(text=f"Preset '{label}' borttagen")

    # ---------------------------------------------------------- kurveditor
    def _clamp_panel_pos(self, x, y, panel):
        """Håll en flytande panels (x,y) innanför NUVARANDE fönsterstorlek —
        och, vertikalt, innanför CANVASENS yta (inte hela fönstret). Bottom-
        raden (styrka/filmremsa) och toppens verktygsrader ligger UTANFÖR
        canvasen men fortfarande innanför fönstrets gränser, så att bara
        klämma mot fönstret räckte inte — en hög panel (många reglage) kunde
        fortfarande sträcka sig ner över och överlappa dem. Positionen
        cachas annars för alltid från första öppningen — om fönstret senare
        krymps (t.ex. avmaximerat) hamnar en tidigare beräknad/dragen
        position lätt fel. Anropas vid varje öppning OCH vid
        fönsterändring, inte bara en gång."""
        self.update_idletasks()
        # OBS: winfo_width()/height() ger 1 (inte 0!) innan panelen någonsin
        # place():ats — "1 or reqwidth" blir då 1 (sant), inte fallbacken.
        # Explicit tröskel istället för `or`, samma gotcha som Track/
        # CurveEditor råkat ut för tidigare i den här filen.
        pw = panel.winfo_width()
        pw = pw if pw > 4 else panel.winfo_reqwidth()
        ph = panel.winfo_height()
        ph = ph if ph > 4 else panel.winfo_reqheight()
        max_x = max(4, self.winfo_width() - pw - 4)
        cy0 = self.canvas.winfo_y()
        cy1 = cy0 + self.canvas.winfo_height()
        min_y = cy0 + 4
        max_y = max(min_y, cy1 - ph - 4)
        return _clampf(x, 4, max_x), _clampf(y, min_y, max_y)

    def toggle_curve(self):
        self._curve_open = not self._curve_open
        if self._curve_open:
            if not self._cw_init:
                self.update_idletasks()
                pw = self.curvewin.winfo_reqwidth()
                self._cw_x = max(8, self.winfo_width() - pw
                                 - self.adjust.winfo_reqwidth() - 40)
                self._cw_y = self.canvas.winfo_y() + 12
                self._cw_init = True
            self._cw_x, self._cw_y = self._clamp_panel_pos(
                self._cw_x, self._cw_y, self.curvewin)
            self.curvewin.place(x=self._cw_x, y=self._cw_y, anchor="nw")
            self.curvewin.lift()
            self.curve_ed.set_histogram(self.geo_arr)   # aktuellt fotos toner
            self.curve_btn.configure(bg=ACCENT, fg=ONACCENT)
        else:
            self.curvewin.place_forget()
            self.curve_btn.configure(bg=self.curve_btn._base[0],
                                     fg=self.curve_btn._base[1])

    def _reset_curve(self):
        self._push_undo()
        self.curve_ed.reset()
        self._request_render()

    def _cw_drag_press(self, e):
        self._cw_grab = (e.x_root, e.y_root, self._cw_x, self._cw_y)

    def _cw_drag_move(self, e):
        gx, gy, bx, by = self._cw_grab
        self._cw_x = _clampf(bx + (e.x_root - gx), 4, self.winfo_width() - 60)
        self._cw_y = _clampf(by + (e.y_root - gy), 4, self.winfo_height() - 60)
        self.curvewin.place(x=self._cw_x, y=self._cw_y, anchor="nw")

    # ---------------------------------------------------------- tema / vy
    def _toggle_theme(self):
        """Byt ljust/mörkt tema. Palettbytet läses vid widget-bygge, så vi
        river och bygger om hela skalet — sessionens foton (PhotoItem) och
        ångra-stackar lever vidare eftersom de bara är data."""
        self._sync_ui_to_active()
        sess, active = self.session, self.active_idx
        undo, redo = self._undo, self._redo
        clip, spath = self._clipboard_edit, self._session_path
        cmp_uid = self._compare_uid
        efmt, q = self.export_fmt, self.jpeg_quality
        new = "dark" if self._theme == "light" else "light"
        apply_theme(new)
        self._theme = new
        if self._thumb_after:          # pågående thumb-kö pekar på döda kort
            self.after_cancel(self._thumb_after)
            self._thumb_after = None
        self._thumb_queue = []
        for w in self.winfo_children():
            w.destroy()
        self.configure(bg=PAPER)
        # nollställ widget-register + flyktiga panellägen inför ombygget
        self._cards = {}
        self._thumbs = {}
        self._roll_cards = {}
        self._fmt_cards = {}
        self._adjust_open = False
        self._curve_open = self._cw_init = False
        self._export_open = self._ew_init = False
        self._compare_mode = self._crop_mode = False
        self._showing_original = self._chrome_hidden = False
        self.export_fmt, self.jpeg_quality = efmt, q
        self._build_ui()
        self.session = sess
        self._undo, self._redo = undo, redo
        self._clipboard_edit, self._session_path = clip, spath
        self._compare_uid = cmp_uid
        self._crop_src_ref = self._crop_img_key = None   # canvasen är ny
        self._cmp_disp = None
        for item in self.session:            # tumnaglar ur cachen — ingen
            self._make_roll_card(item, count=False)   # omrendering
        self._refresh_roll_count()
        if self.session:
            self._set_enabled(self.export_btn, True)
            self._set_enabled(self.savesess_btn, True)
            self._set_enabled(self.undo_btn, bool(self._undo))
            self._set_enabled(self.redo_btn, bool(self._redo))
            self.active_idx = None
            self._activate(min(active if active is not None else 0,
                               len(self.session) - 1))
        self.theme_btn.configure(text="☀ Dag" if new == "dark" else "☾ Natt")
        self._apply_titlebar_theme()
        self._settings["theme"] = new
        save_settings(self._settings)

    def _toggle_fullscreen(self):
        self._fullscreen = not self._fullscreen
        try:
            self.attributes("-fullscreen", self._fullscreen)
        except Exception:      # noqa: BLE001
            pass

    def _toggle_chrome(self):
        """Ren vy: dölj all chrome (header, rulle, filmremsa) och visa bara
        fotot — INTE äkta OS-helskärm (det gör bara F11/⛶, `_toggle_fullscreen`).
        Togglas via "Ren vy"-knappen. Om äkta helskärm råkar vara på sen
        tidigare stängs den av här, så Ren vy aldrig kan uppfattas som/
        sammanfalla med helskärm."""
        if self._fullscreen:
            self._toggle_fullscreen()
        self._chrome_hidden = not self._chrome_hidden
        top = (self._head, self._sep1, self._roll_wrap, self._sep2)
        if self._chrome_hidden:
            for w in top:
                w.pack_forget()
            self._bottom.pack_forget()
            if self._adjust_open:
                self._toggle_adjust()
            if self._curve_open:
                self.toggle_curve()
            if self._export_open:
                self._toggle_export_panel()
        else:
            for w in top:
                w.pack(fill="x", side="top", before=self.canvas)
            # tillbaka på SIN plats i pack-ordningen (före beskärningsraden/
            # canvasen) — packad sist hamnade den fel och klämdes först
            anchor = self.crop_bar if self.crop_bar.winfo_manager() \
                else self.canvas
            self._bottom.pack(fill="x", side="bottom", before=anchor)
        # samma omritning som vid fönsterändring — den väljer rätt vy för
        # läget. Förut ritades alltid gallerivyn, även i beskärningsläge
        # (rutan och handtagen försvann under en vanlig bild).
        self.after(40, self._resize_redraw)

    # ---------------------------------------------------------- jämförelse
    def _render_item_preview(self, item):
        """Rendera ett rull-fotos look i förhandsvisningsupplösning (för
        jämförelsevyn — engångsberäkning, inte via bakgrundstråden)."""
        base = apply_geometry(as_float01(item.prev_arr), item.edit.crop,
                              item.edit.angle)
        graded = process(base, grade_from_edit(item.edit))
        arr = blend_strength(base, graded, item.edit.strength / 100.0)
        return to_pil(arr)

    def _compare_image(self, item):
        """Jämförelsefotot renderat — CACHAT på receptets signatur. Det
        renderades förut om synkront vid VARJE compose, dvs. vid varje
        reglagedrag på vänsterbilden, fast högerbilden inte kan ändras
        medan man jämför (uppmätt: 123 → 25 ms per bildruta)."""
        sig = self._edit_sig(item.edit)
        c = self._cmp_cache
        if c is None or c[0] != item.uid or c[1] != sig:
            self._cmp_cache = c = (item.uid, sig,
                                   self._render_item_preview(item))
        return c[2]

    def toggle_compare(self):
        if self._compare_mode:
            self._exit_compare()
            return
        if len(self.session) < 2 or self.active_idx is None:
            self.name_lbl.configure(
                text="Behöver minst två foton för jämförelse")
            return
        if self._crop_mode:
            self._cancel_crop()
        if self._adjust_open:
            self._toggle_adjust()
        self._sync_ui_to_active()
        act_uid = self.session[self.active_idx].uid
        valid = (self._compare_uid != act_uid
                 and any(it.uid == self._compare_uid for it in self.session))
        if not valid:
            nb = (self.active_idx + 1) % len(self.session)
            self._compare_uid = self.session[nb].uid
        self._compare_mode = True
        self.zoom = 1.0
        self.pan_x = self.pan_y = 0.0
        self.compare_btn.configure(bg=ACCENT, fg=ONACCENT)
        self._draw_compare()
        self.name_lbl.configure(
            text="Jämför — klicka ett foto i rullen för att välja höger bild")

    def _exit_compare(self):
        self._compare_mode = False
        self._cmp_imgs = []
        self.compare_btn.configure(bg=self.compare_btn._base[0],
                                   fg=self.compare_btn._base[1])
        self._draw(self.cur_pil)
        self._refresh_compare_borders()

    def _set_compare(self, uid):
        """Skift-klick på ett rullkort: välj det som höger (jämför-)bild."""
        if self.active_idx is not None \
                and self.session[self.active_idx].uid == uid:
            return                     # kan inte jämföra ett foto med sig självt
        self._compare_uid = uid
        if self._compare_mode:
            self._draw_compare()
        else:
            comp = next((it for it in self.session if it.uid == uid), None)
            if comp:
                self.name_lbl.configure(
                    text=f"Jämför mot {self._short_name(comp.path, 20)} "
                         f"— tryck Jämför")

    def _draw_compare(self):
        if not self._compare_mode or self.active_idx is None:
            return
        act = self.session[self.active_idx]
        comp = next((it for it in self.session
                     if it.uid == self._compare_uid), None)
        if comp is None or comp is act:
            others = [it for it in self.session if it.uid != act.uid]
            if not others:
                self._exit_compare()
                return
            comp = others[0]
            self._compare_uid = comp.uid
        self.canvas.delete("art")
        self.canvas.delete("crop")
        cw = max(self.canvas.winfo_width(), 1)
        ch = max(self.canvas.winfo_height(), 1)
        colw = cw / 2.0
        self._cmp_imgs = []
        for i, (item, tag) in enumerate(((act, "aktiv"), (comp, "jämför"))):
            if i == 0 and self.cur_pil is not None:
                pil = self.cur_pil          # aktiv är redan renderad
            elif i == 0:
                pil = self._render_item_preview(item)
            else:
                pil = self._compare_image(item)     # cachad (se metoden)
            iw, ih = pil.size
            margin, mat = 28, 12
            avail_w = max(1, colw - 2 * (margin + mat))
            avail_h = max(1, ch - 2 * (margin + mat) - 24)
            s = min(avail_w / iw, avail_h / ih, 1.0)
            dw, dh = max(1, int(iw * s)), max(1, int(ih * s))
            key = (item.uid, self._cmp_cache[1] if i else None, dw, dh)
            if i == 1 and self._cmp_disp and self._cmp_disp[0] == key:
                tkimg = self._cmp_disp[1]           # även skalningen cachas
            else:
                disp = pil.resize((dw, dh), Image.LANCZOS) \
                    if (dw, dh) != (iw, ih) else pil
                tkimg = ImageTk.PhotoImage(disp, master=self)
                if i == 1:
                    self._cmp_disp = (key, tkimg)
            self._cmp_imgs.append(tkimg)
            cx = int(colw * i + colw / 2)
            cy = ch // 2 - 6
            x0, y0 = cx - dw // 2, cy - dh // 2
            self.canvas.create_rectangle(
                x0 - mat + 5, y0 - mat + 6, x0 + dw + mat + 5,
                y0 + dh + mat + 6, fill=SHADOW, outline="", tags="art")
            self.canvas.create_rectangle(
                x0 - mat, y0 - mat, x0 + dw + mat, y0 + dh + mat,
                fill=MAT, outline=LINE, tags="art")
            self.canvas.create_image(cx, cy, image=tkimg, tags="art")
            caption = f"{self._short_name(item.path, 22)}  ({tag})"
            self.canvas.create_text(
                cx, y0 + dh + mat + 13, text=caption,
                fill=INK2, font=self.sf(9, "italic"), tags="art")
        self.canvas.create_line(colw, 22, colw, ch - 22, fill=LINE,
                                tags="art")
        self._refresh_compare_borders()

    def _toggle_adjust(self):
        self._adjust_open = not self._adjust_open
        if self._adjust_open:
            # alltid samma förankrade plats (uppe till höger i canvasen) vid
            # varje öppning — minns INTE en tidigare dragen position (till
            # skillnad från Kurva/Format-panelerna). Panelen kan fortfarande
            # dras runt medan den är öppen, men återställs vid nästa öppning.
            self.update_idletasks()
            pw = self.adjust.winfo_reqwidth()
            self._adj_x = max(8, self.winfo_width() - pw - 16)
            self._adj_y = self.canvas.winfo_y() + 12
            self._adj_x, self._adj_y = self._clamp_panel_pos(
                self._adj_x, self._adj_y, self.adjust)
            self.adjust.place(x=self._adj_x, y=self._adj_y, anchor="nw")
            self.adjust.lift()
            self.adj_btn.configure(text="Justera  ▼")
        else:
            self.adjust.place_forget()
            self.adj_btn.configure(text="Justera  ▲")

    def _adj_drag_press(self, e):
        self._adj_grab = (e.x_root, e.y_root, self._adj_x, self._adj_y)

    def _adj_drag_move(self, e):
        gx, gy, bx, by = self._adj_grab
        pw = self.adjust.winfo_width()        # 1 (inte 0) om omappad —
        pw = pw if pw > 4 else self.adjust.winfo_reqwidth()   # "1 or X"-fällan
        ph = self.adjust.winfo_height()
        ph = ph if ph > 4 else self.adjust.winfo_reqheight()
        nx = _clampf(bx + (e.x_root - gx), 4, max(4, self.winfo_width() - pw - 4))
        ny = _clampf(by + (e.y_root - gy), 4,
                     max(4, self.winfo_height() - ph - 4))
        self._adj_x, self._adj_y = nx, ny
        self.adjust.place(x=nx, y=ny, anchor="nw")

    def _on_strength(self):
        self.intensity = self.strength.get() / 100.0
        self._compose()

    def _effective_grade(self):
        return grade_from_edit(self._ui_edit_state())

    # ---------------------------------------------------------- rendering
    def _request_render(self, *_):
        if self.prev_arr is None:
            return
        if self._render_after:
            self.after_cancel(self._render_after)
        self._render_after = self.after(45, self._do_render)

    def _do_render(self):
        self._render_after = None
        if self.geo_arr is None:
            return
        self._token += 1
        self._renderer.submit(self._token, self.geo_arr,
                              self._effective_grade())

    def _poll_render(self):
        """Hjärtat i GUI-uppdateringen. Hela kroppen är felskyddad och
        omschemaläggningen ligger i finally — ett enda oväntat fel här
        (t.ex. TclError när tembytet river widgets mitt i en pågående
        batchexport) fick annars kedjan av self.after() att brytas, och
        då slutade ALL rendering/exportstatus fungera för resten av
        sessionen utan något synligt felmeddelande."""
        try:
            latest = None
            try:
                while True:
                    latest = self._rq.get_nowait()
            except queue.Empty:
                pass
            if latest is not None:
                token, payload = latest
                if isinstance(payload, Exception):
                    self.name_lbl.configure(text=f"Renderfel: {payload}")
                elif token == self._token:
                    self.graded_arr = payload
                    self._compose()
            self._drain_batch()
            self._drain_import()
        except Exception:      # noqa: BLE001 — loopen får aldrig dö …
            log_error("poll")  # … men felet får inte heller försvinna tyst
        finally:
            try:
                self._poll_after = self.after(30, self._poll_render)
            except Exception:      # noqa: BLE001 — appen håller på att stängas
                pass

    def _compose(self):
        if self._crop_mode:
            return                        # beskärningsläget ritar sitt eget
        if self.graded_arr is None or self.geo_arr is None:
            return
        if self.graded_arr.shape != self.geo_arr.shape:
            return                        # väntar på ny render efter geometribyte
        arr = blend_strength(self.geo_arr, self.graded_arr, self.intensity)
        self._compose_arr = arr
        self.cur_pil = to_pil(arr)
        self._update_histogram(arr)
        if self._compare_mode:
            self._draw_compare()
        elif not self._showing_original:
            self._draw(self.cur_pil)

    def _base_scale(self, cw, ch, iw, ih):
        """Passa-in-skala (monterad kopia) — samma i galleri- och loupe-läge."""
        mat, margin = 16, 34
        avail_w = max(1, cw - 2 * (margin + mat))
        avail_h = max(1, ch - 2 * (margin + mat))
        s = min(avail_w / iw, avail_h / ih)
        return min(s, 1.0) if s > 0 else 1.0

    def _draw(self, pil_img):
        if pil_img is None:
            return
        self._view_pil = pil_img
        cw = max(self.canvas.winfo_width(), 1)
        ch = max(self.canvas.winfo_height(), 1)
        iw, ih = pil_img.size
        base = self._base_scale(cw, ch, iw, ih)
        self.canvas.delete("art")

        if self.zoom <= 1.0 + 1e-3:           # galleri-läge: monterad kopia
            self.pan_x = self.pan_y = 0.0
            mat = 16
            dw, dh = max(1, int(iw * base)), max(1, int(ih * base))
            disp = pil_img.resize((dw, dh), Image.LANCZOS) \
                if (dw, dh) != (iw, ih) else pil_img
            self._tk_img = ImageTk.PhotoImage(disp, master=self)
            x0, y0 = (cw - dw) // 2, (ch - dh) // 2
            self._img_rect = (x0, y0, base)
            self.canvas.create_rectangle(
                x0 - mat + 7, y0 - mat + 9, x0 + dw + mat + 7,
                y0 + dh + mat + 9, fill=SHADOW, outline="", tags="art")
            self.canvas.create_rectangle(
                x0 - mat, y0 - mat, x0 + dw + mat, y0 + dh + mat,
                fill=MAT, outline=LINE, tags="art")
            self.canvas.create_image(cw // 2, ch // 2, image=self._tk_img,
                                     tags="art")
            return

        # loupe-läge: beskär och skala bara det synliga, tillåt panorering
        s = base * self.zoom
        fw, fh = iw * s, ih * s
        maxx, maxy = max(0.0, (fw - cw) / 2), max(0.0, (fh - ch) / 2)
        self.pan_x = _clampf(self.pan_x, -maxx, maxx)
        self.pan_y = _clampf(self.pan_y, -maxy, maxy)
        X = cw / 2 - fw / 2 + self.pan_x
        Y = ch / 2 - fh / 2 + self.pan_y
        self._img_rect = (X, Y, s)
        sx0 = max(0, int(math.floor((0 - X) / s)))
        sy0 = max(0, int(math.floor((0 - Y) / s)))
        sx1 = min(iw, int(math.ceil((cw - X) / s)))
        sy1 = min(ih, int(math.ceil((ch - Y) / s)))
        if sx1 <= sx0 or sy1 <= sy0:
            return
        crop = pil_img.crop((sx0, sy0, sx1, sy1))
        dw = max(1, int((sx1 - sx0) * s))
        dh = max(1, int((sy1 - sy0) * s))
        # BILINEAR medan man drar/zoomar (mjukt), LANCZOS när det står stilla
        rs = Image.BILINEAR if self._fast_view else Image.LANCZOS
        self._tk_img = ImageTk.PhotoImage(crop.resize((dw, dh), rs),
                                          master=self)
        self.canvas.create_image(int(X + sx0 * s), int(Y + sy0 * s),
                                 anchor="nw", image=self._tk_img, tags="art")
        self.canvas.create_text(
            18, ch - 16, anchor="w", text=f"{int(round(self.zoom * 100))} %",
            fill=INK2, font=self.sf(9, "italic"), tags="art")

    # ---- zoom & panorering ----
    def _on_zoom(self, e):
        if self._view_pil is None or self._crop_mode or self._compare_mode:
            return
        cw = max(self.canvas.winfo_width(), 1)
        ch = max(self.canvas.winfo_height(), 1)
        iw, ih = self._view_pil.size
        base = self._base_scale(cw, ch, iw, ih)
        old = self.zoom
        new = _clampf(old * (1.0015 ** wheel_units(e.delta)), 1.0, 8.0)
        if abs(new - old) < 1e-4:
            return
        # håll punkten under muspekaren stilla
        s_old, s_new = base * old, base * new
        ox = (e.x - cw / 2 - self.pan_x) / s_old
        oy = (e.y - ch / 2 - self.pan_y) / s_old
        self.pan_x = e.x - cw / 2 - ox * s_new
        self.pan_y = e.y - ch / 2 - oy * s_new
        self.zoom = new
        self._fast_view = True
        self._draw(self._view_pil)
        self._schedule_crisp()

    def _schedule_crisp(self):
        """Rita om skarpt (LANCZOS) en kort stund efter senaste rörelsen."""
        if self._crisp_after:
            self.after_cancel(self._crisp_after)
        self._crisp_after = self.after(160, self._crisp_redraw)

    def _crisp_redraw(self):
        self._crisp_after = None
        if not self._panning:
            self._fast_view = False
            self._draw(self._view_pil)

    def _reset_zoom(self, _=None):
        if self._compare_mode:      # zoom är inaktivt i delad jämförelsevy
            return
        self.zoom = 1.0
        self.pan_x = self.pan_y = 0.0
        self._draw(self._view_pil)

    # ---------------------------------------------------------- beskärning
    def enter_crop(self):
        if self.prev_arr is None or self._crop_mode:
            return
        if self._compare_mode:
            self._exit_compare()
        if self._adjust_open:          # panelen skymmer annars Klar/Avbryt
            self._toggle_adjust()
        self._crop_mode = True
        self.zoom = 1.0
        self.pan_x = self.pan_y = 0.0
        self._crop_rect = list(self._cur_crop)
        self._crop_angle = self._cur_angle
        self._crop_aspect = None
        self.straight.set(self._cur_angle)
        # before=canvas: raden får sin plats FÖRE canvasen i pack-ordningen,
        # så det är canvasen (inte Klar/Avbryt) som krymper när utrymmet tar slut
        self.crop_bar.pack(fill="x", side="bottom", before=self.canvas)
        # crop_bar stjäl höjd från canvasen — utan update_idletasks() läser
        # _draw_crop() (via _crop_tf) canvasens GAMLA storlek eftersom pack-
        # layouten inte hunnit räkna om än, vilket ritade en beskärningsruta
        # för en större yta än vad som faktiskt syns (nedre handtag hamnar
        # utanför synligt område tills nästa layoutomräkning händer)
        self.update_idletasks()
        self._draw_crop()

    def _set_aspect(self, ratio):
        if not self._crop_mode:
            return
        if ratio == "orig":
            iw, ih = self._crop_disp.size if self._crop_disp else (1, 1)
            self._crop_aspect = iw / ih
        else:
            self._crop_aspect = ratio
        if self._crop_aspect:
            self._constrain_rect()
        self._draw_crop()

    def _on_straighten(self):
        if not self._crop_mode:
            return
        self._crop_angle = self.straight.get()
        self._draw_crop()

    def _norm_ratio(self):
        """Målförhållande uttryckt i NORMALISERAT (display) utrymme."""
        if not self._crop_aspect or not self._crop_disp:
            return None
        iw, ih = self._crop_disp.size
        return self._crop_aspect * ih / iw

    def _constrain_rect(self):
        nr = self._norm_ratio()
        if nr is None:
            return
        x0, y0, x1, y1 = self._crop_rect
        cw, ch = x1 - x0, y1 - y0
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        # behåll höjden, härled bredden ur ratio (eller tvärtom om ryms ej)
        nw = ch * nr
        if nw > 1.0:
            nw = min(cw, 1.0)
            ch = nw / nr
        else:
            nw = min(nw, 1.0)
        nx0 = _clampf(cx - nw / 2, 0, 1 - nw)
        ny0 = _clampf(cy - ch / 2, 0, 1 - ch)
        self._crop_rect = [nx0, ny0, nx0 + nw, ny0 + ch]

    def _crop_tf(self):
        """(x0,y0,dw,dh) för den rätade helbilden monterad på canvasen."""
        cw = max(self.canvas.winfo_width(), 1)
        ch = max(self.canvas.winfo_height(), 1)
        iw, ih = self._crop_disp.size
        m = 40
        scale = min((cw - 2 * m) / iw, (ch - 2 * m) / ih)
        dw, dh = int(iw * scale), int(ih * scale)
        return (cw - dw) // 2, (ch - dh) // 2, dw, dh

    def _crop_display(self):
        """Den rätade helbilden för beskärningsvyn — CACHAD per (källa,
        vinkel). Rotationen (bikubisk, per kanal, 1400 px) räknades förut om
        vid VARJE musrörelse när man drog i beskärningsrutan: ~140 ms per
        rörelse för ett uprätat foto, fast bara rutan ändrades."""
        key = round(self._crop_angle, 4)
        ref = self._crop_src_ref
        if ref is None or ref[0] is not self.prev_arr or ref[1] != key:
            self._crop_disp = to_pil(apply_geometry(self.prev_arr, NO_CROP,
                                                    self._crop_angle))
            self._crop_src_ref = (self.prev_arr, key)
            self._crop_img_key = None
        return self._crop_disp

    def _draw_crop(self):
        self._crop_display()
        self.canvas.delete("art")
        self.canvas.delete("crop")
        x0, y0, dw, dh = self._crop_tf()
        if dw < 2 or dh < 2:              # canvasen inte layoutad än
            return
        if self._crop_img_key != (dw, dh):    # skala bara om vid ny storlek
            self._crop_dispimg = ImageTk.PhotoImage(
                self._crop_disp.resize((dw, dh), Image.BILINEAR), master=self)
            self._crop_img_key = (dw, dh)
        self.canvas.create_image(x0, y0, anchor="nw", image=self._crop_dispimg,
                                 tags="crop")
        rx0 = x0 + self._crop_rect[0] * dw
        ry0 = y0 + self._crop_rect[1] * dh
        rx1 = x0 + self._crop_rect[2] * dw
        ry1 = y0 + self._crop_rect[3] * dh
        # mörka ytan utanför beskärningen (stipple = pseudo-transparens)
        for a, b, c, d in [(x0, y0, x0 + dw, ry0), (x0, ry1, x0 + dw, y0 + dh),
                           (x0, ry0, rx0, ry1), (rx1, ry0, x0 + dw, ry1)]:
            self.canvas.create_rectangle(a, b, c, d, fill="#000000",
                                         stipple="gray50", outline="",
                                         tags="crop")
        # dubbel kontur (mörk + ljus) — syns tydligt oavsett bildens ljushet,
        # till skillnad från en enkel 2px vit linje som kan försvinna mot
        # ljusa moln/himmel
        self.canvas.create_rectangle(rx0, ry0, rx1, ry1, outline="#000000",
                                     width=4, tags="crop")
        self.canvas.create_rectangle(rx0, ry0, rx1, ry1, outline="#ffffff",
                                     width=2, tags="crop")
        # tredjedelslinjer
        for f in (1 / 3, 2 / 3):
            self.canvas.create_line(rx0 + (rx1 - rx0) * f, ry0,
                                    rx0 + (rx1 - rx0) * f, ry1,
                                    fill="#ffffff", tags="crop")
            self.canvas.create_line(rx0, ry0 + (ry1 - ry0) * f,
                                    rx1, ry0 + (ry1 - ry0) * f,
                                    fill="#ffffff", tags="crop")
        # hörnhandtag (större) + kant-handtag (mitt på varje sida, avlånga
        # stänger) — utan kanthandtag var det bara den 2px tunna konturlinjen
        # att träffa för att dra i endast bredd/höjd, svårt att se/greppa
        mx, my = (rx0 + rx1) / 2, (ry0 + ry1) / 2
        for hx, hy in [(rx0, ry0), (rx1, ry0), (rx0, ry1), (rx1, ry1)]:
            self.canvas.create_rectangle(hx - 7, hy - 7, hx + 7, hy + 7,
                                         fill=ACCENT, outline="#ffffff",
                                         width=2, tags="crop")
        bar, elen = 5, 16
        for hx, hy, horiz in [(mx, ry0, True), (mx, ry1, True),
                              (rx0, my, False), (rx1, my, False)]:
            if horiz:
                box = (hx - elen, hy - bar, hx + elen, hy + bar)
            else:
                box = (hx - bar, hy - elen, hx + bar, hy + elen)
            self.canvas.create_rectangle(*box, fill=ACCENT, outline="#ffffff",
                                         width=2, tags="crop")

    def _crop_press(self, e):
        x0, y0, dw, dh = self._crop_tf()
        rx0 = x0 + self._crop_rect[0] * dw
        ry0 = y0 + self._crop_rect[1] * dh
        rx1 = x0 + self._crop_rect[2] * dw
        ry1 = y0 + self._crop_rect[3] * dh
        mx, my = (rx0 + rx1) / 2, (ry0 + ry1) / 2
        # hörn FÖRE kanter (prioritet om de skulle överlappa på små rutor)
        handles = {"nw": (rx0, ry0), "ne": (rx1, ry0),
                  "sw": (rx0, ry1), "se": (rx1, ry1),
                  "n": (mx, ry0), "s": (mx, ry1),
                  "w": (rx0, my), "e": (rx1, my)}
        for name, (hx, hy) in handles.items():
            if abs(e.x - hx) <= 12 and abs(e.y - hy) <= 12:
                self._crop_drag = ("corner", name, e.x, e.y,
                                   list(self._crop_rect))
                return
        if rx0 <= e.x <= rx1 and ry0 <= e.y <= ry1:
            self._crop_drag = ("move", None, e.x, e.y, list(self._crop_rect))

    def _crop_motion(self, e):
        if not self._crop_drag:
            return
        mode, corner, sx, sy, start = self._crop_drag
        x0, y0, dw, dh = self._crop_tf()
        if dw < 2 or dh < 2:
            return
        ndx = (e.x - sx) / dw
        ndy = (e.y - sy) / dh
        r = list(start)
        if mode == "move":
            w, h = r[2] - r[0], r[3] - r[1]
            nx0 = _clampf(r[0] + ndx, 0, 1 - w)
            ny0 = _clampf(r[1] + ndy, 0, 1 - h)
            r = [nx0, ny0, nx0 + w, ny0 + h]
        else:
            if "w" in corner:
                r[0] = _clampf(start[0] + ndx, 0, r[2] - 0.03)
            if "e" in corner:
                r[2] = _clampf(start[2] + ndx, r[0] + 0.03, 1)
            if "n" in corner:
                r[1] = _clampf(start[1] + ndy, 0, r[3] - 0.03)
            if "s" in corner:
                r[3] = _clampf(start[3] + ndy, r[1] + 0.03, 1)
            self._crop_rect = r
            nr = self._norm_ratio()
            if nr is not None:
                self._apply_ratio_to_corner(corner)
                r = self._crop_rect
        self._crop_rect = r
        self._draw_crop()

    def _apply_ratio_to_corner(self, corner):
        """Håll låst förhållande under drag. Hörn och vänster/höger-kant:
        höjden följer bredden (kring den fasta ankaren). Topp/botten-kant:
        BREDDEN följer höjden, centrerat — förut räknades höjden tillbaka ur
        den oförändrade bredden, så de handtagen gjorde ingenting alls."""
        nr = self._norm_ratio()
        x0, y0, x1, y1 = self._crop_rect
        if corner in ("n", "s"):
            h = y1 - y0
            w = h * nr
            if w > 1.0:                # får inte plats på bredden
                w = 1.0
                h = w / nr
                if corner == "n":
                    y0 = y1 - h
                else:
                    y1 = y0 + h
            x0 = _clampf((x0 + x1) / 2 - w / 2, 0.0, 1.0 - w)
            self._crop_rect = [x0, y0, x0 + w, y1]
            return
        w = x1 - x0
        h = w / nr
        if "n" in corner:      # övre kanten rör sig → ankare = nedre
            y0 = _clampf(y1 - h, 0, y1 - 0.02)
        else:                  # ankare = övre
            y1 = _clampf(y0 + h, y0 + 0.02, 1)
        self._crop_rect = [x0, y0, x1, y1]

    def _apply_crop(self):
        if not self._crop_mode:
            return
        self._push_undo()
        r = self._crop_rect
        self._cur_crop = (min(r[0], r[2]), min(r[1], r[3]),
                          max(r[0], r[2]), max(r[1], r[3]))
        self._cur_angle = self._crop_angle
        self._exit_crop()
        self._apply_geo()              # räkna om + rendera
        self._sync_ui_to_active()
        self.name_lbl.configure(text="Beskärning tillämpad")

    def _cancel_crop(self, _=None):
        if not self._crop_mode:
            return
        self._exit_crop()
        self._draw(self.cur_pil)

    def _exit_crop(self):
        self._crop_mode = False
        self._crop_drag = None
        self._crop_src_ref = self._crop_img_key = None   # släpp cachen
        self.canvas.delete("crop")
        self.crop_bar.pack_forget()

    def _on_canvas_press(self, e):
        if self._compare_mode:
            return          # vänsterbilden är låst — byt höger via rullen
        if self._crop_mode:
            self._crop_press(e)
            return
        if self.zoom > 1.0 + 1e-3:            # panorera i loupe-läge
            self._panning = True
            self._pan_start = (e.x, e.y, self.pan_x, self.pan_y)
        else:                                 # klick-håll = jämför original
            self._panning = False
            self._show_original()

    def _on_pan_press(self, e):
        """Mittenknappen (skrollhjulet nedtryckt) börjar alltid panorera,
        oavsett zoomnivå — till skillnad från vänsterklick är den inte
        upptagen av "håll för original" i galleriläge. Utan zoom har
        panoreringen ingen synlig effekt (helbilden visas redan centrerad),
        men blir aktiv så fort man zoomar in."""
        if self._compare_mode or self._crop_mode:
            return
        self._panning = True
        self._pan_start = (e.x, e.y, self.pan_x, self.pan_y)

    def _on_canvas_motion(self, e):
        if self._compare_mode:
            return
        if self._crop_mode:
            self._crop_motion(e)
            return
        if self._panning:
            sx, sy, px, py = self._pan_start
            self.pan_x = px + (e.x - sx)
            self.pan_y = py + (e.y - sy)
            self._fast_view = True
            self._draw(self._view_pil)

    def _on_canvas_release(self, e):
        if self._compare_mode:
            return
        if self._crop_mode:
            self._crop_drag = None
            return
        if self._panning:
            self._panning = False
            self._schedule_crisp()
        else:
            self._show_graded()

    def _show_original(self, _=None):
        if self.geo_arr is None or self._showing_original or self._crop_mode \
                or self._compare_mode:
            return                     # (guard även mot space-autorepeat)
        self._showing_original = True
        self._draw(to_pil(self.geo_arr))

    def _show_graded(self, _=None):
        if self._compare_mode:
            return
        self._showing_original = False
        if self.cur_pil is not None:
            self._draw(self.cur_pil)

    def _on_resize(self, e):
        if e.widget is not self:
            return
        # debounce — under interaktiv fönsterdragning kommer <Configure>
        # i praktiken per pixel, och att LANCZOS-skala om hela fotot (plus
        # klämma paneler) på VARJE event gjorde själva resize-rörelsen hackig
        if self._resize_after:
            self.after_cancel(self._resize_after)
        self._resize_after = self.after(80, self._resize_redraw)

    def _resize_redraw(self):
        self._resize_after = None
        if self._crop_mode:
            self._draw_crop()
        elif self._compare_mode:
            self._draw_compare()
        elif self._view_pil is not None and not self._showing_original \
                and not self._render_after:
            self._draw(self._view_pil)
        # håll ev. öppna flytande paneler innanför fönstret vid resize
        # (t.ex. avmaximering) — annars kan de hamna kvar utanför synligt
        # område tills man stänger och öppnar dem igen
        if self._adjust_open:
            self._adj_x, self._adj_y = self._clamp_panel_pos(
                self._adj_x, self._adj_y, self.adjust)
            self.adjust.place(x=self._adj_x, y=self._adj_y, anchor="nw")
        if self._curve_open:
            self._cw_x, self._cw_y = self._clamp_panel_pos(
                self._cw_x, self._cw_y, self.curvewin)
            self.curvewin.place(x=self._cw_x, y=self._cw_y, anchor="nw")
        if self._export_open:
            self._ew_x, self._ew_y = self._clamp_panel_pos(
                self._ew_x, self._ew_y, self.exportwin)
            self.exportwin.place(x=self._ew_x, y=self._ew_y, anchor="nw")

    # ---------------------------------------------------------- exportformat
    EXT_MAP = {"JPEG": ".jpg", "PNG": ".png", "TIFF": ".tif"}

    def _toggle_export_panel(self):
        self._export_open = not self._export_open
        if self._export_open:
            if not self._ew_init:
                self.update_idletasks()
                pw = self.exportwin.winfo_reqwidth()
                self._ew_x = max(8, self.winfo_width() - pw - 16)
                self._ew_y = self.canvas.winfo_y() + 12
                self._ew_init = True
            self._ew_x, self._ew_y = self._clamp_panel_pos(
                self._ew_x, self._ew_y, self.exportwin)
            self.exportwin.place(x=self._ew_x, y=self._ew_y, anchor="nw")
            self.exportwin.lift()
            self.fmt_btn.configure(bg=ACCENT, fg=ONACCENT)
        else:
            self.exportwin.place_forget()
            self.fmt_btn.configure(bg=self.fmt_btn._base[0],
                                   fg=self.fmt_btn._base[1])

    def _set_export_fmt(self, fmt):
        self.export_fmt = fmt
        self._mark_export_fmt()
        self._settings["export_fmt"] = fmt      # minns till nästa körning
        save_settings(self._settings)
        self.name_lbl.configure(text=f"Exportformat: {fmt}")

    def _mark_export_fmt(self):
        for f, b in self._fmt_cards.items():
            active = (f == self.export_fmt)
            b._primary = active
            b._base = (ACCENT if active else PAPER2,
                       ONACCENT if active else INK)
            b.configure(bg=b._base[0], fg=b._base[1])

    def _on_quality(self):
        self.jpeg_quality = int(round(self.qtrack.get()))
        self._settings["jpeg_quality"] = self.jpeg_quality   # minns …
        self._save_settings_soon()     # … men skrivs inte per musrörelse

    def _save_settings_soon(self, delay=600):
        """Debouncad skrivning av settings-filen. Kvalitetsreglaget anropade
        förut save_settings vid VARJE drag-händelse — dussintals fil-
        skrivningar per sekund från GUI-tråden."""
        if self._settings_after:
            self.after_cancel(self._settings_after)
        self._settings_after = self.after(delay, self._flush_settings)

    def _flush_settings(self):
        if self._settings_after:
            try:
                self.after_cancel(self._settings_after)
            except Exception:      # noqa: BLE001
                pass
            self._settings_after = None
        save_settings(self._settings)

    def _ew_drag_press(self, e):
        self._ew_grab = (e.x_root, e.y_root, self._ew_x, self._ew_y)

    def _ew_drag_move(self, e):
        gx, gy, bx, by = self._ew_grab
        self._ew_x = _clampf(bx + (e.x_root - gx), 4, self.winfo_width() - 60)
        self._ew_y = _clampf(by + (e.y_root - gy), 4, self.winfo_height() - 60)
        self.exportwin.place(x=self._ew_x, y=self._ew_y, anchor="nw")

    # ---------------------------------------------------------- spara
    def save_image(self):
        if getattr(self.save_btn, "_disabled", True) or self.src_path is None:
            return
        base = os.path.splitext(os.path.basename(self.src_path))[0] \
            + "_" + film_slug(self.film_key)
        de = self.EXT_MAP.get(self.export_fmt, ".jpg")
        path = filedialog.asksaveasfilename(
            title="Spara bild", defaultextension=de, initialfile=base,
            filetypes=[("JPEG", "*.jpg"), ("PNG", "*.png"),
                       ("TIFF", "*.tif")])
        if path:
            self._save_to(path)

    def _save_to(self, path):
        """Spara aktivt foto i full upplösning till `path` — i BAKGRUNDEN via
        samma arbetartråd som Exportera alla. Förut renderades det synkront
        på GUI-tråden och appen frös i flera sekunder per sparning."""
        if self._exporting or self.active_idx is None:
            return
        self._sync_ui_to_active()
        item = self.session[self.active_idx]
        self.name_lbl.configure(text="Sparar fullupplösning …")
        self._start_export([self._export_job(item, out=path)], single=True)

    # ---------------------------------------------------------- session-verktyg
    def copy_settings(self):
        """Kopiera aktivt fotos film + reglage (Ctrl+C)."""
        if self.active_idx is None:
            return
        self._clipboard_edit = self._ui_edit_state().clone()
        self.name_lbl.configure(text="Inställningar kopierade")

    def paste_settings(self):
        """Klistra in på aktivt foto (Ctrl+V)."""
        if self.active_idx is None or self._clipboard_edit is None:
            return
        self._push_undo()
        self._load_edit_into_ui(self._clipboard_edit.clone())
        # beskärning/vinkel ingår i receptet — räkna om geometrin, annars
        # syntes den inklistrade beskärningen först efter ett fotobyte
        self._apply_geo()

    def sync_active_to_all(self):
        """Applicera det AKTIVA fotots look på alla foton i rullen
        (Ctrl+Skift+V). Behöver ingen föregående Kopiera — 'Synka → alla'
        betyder just 'gör alla som den jag tittar på nu'."""
        if self.active_idx is None or not self.session:
            self.name_lbl.configure(text="Inget aktivt foto att synka från")
            return
        self._push_undo()
        self._sync_ui_to_active()          # skriv UI → aktivt fotos .edit
        src = self.session[self.active_idx].edit.clone()
        for item in self.session:
            item.edit = src.clone()
            self._update_roll_card(item, count=False)
        self._refresh_roll_count()
        self.name_lbl.configure(
            text=f"Alla {len(self.session)} foton synkade till aktuell look")

    def remove_active_photo(self):
        """Ta bort aktivt foto ur rullen (rör inte filen på disk)."""
        if self.active_idx is None:
            return
        if self._crop_mode:
            self._exit_crop()
        self._push_undo()
        item = self.session.pop(self.active_idx)
        card = self._roll_cards.pop(item.uid, None)
        if card:
            card["outer"].destroy()
        if not self.session:
            self._clear_active_view()
            self.name_lbl.configure(text="")
        else:
            new_idx = min(self.active_idx, len(self.session) - 1)
            self.active_idx = None      # tvinga _activate ladda om helt
            self._activate(new_idx)
        self._refresh_roll_count()

    def _clear_active_view(self):
        """Tom rulle: nollställ ALLT som hör till ett aktivt foto. Förut låg
        geo_arr kvar — mellanslag/klick på canvasen visade då det borttagna
        fotot igen, och efter "gör om" till en tom rulle stod både bilden
        och en aktiv Spara-knapp kvar."""
        if self._crop_mode:
            self._exit_crop()
        if self._compare_mode:
            self._compare_mode = False
            self._cmp_imgs = []
            self.compare_btn.configure(bg=self.compare_btn._base[0],
                                       fg=self.compare_btn._base[1])
        self.active_idx = None
        self.prev_arr = self.thumb_src = self.geo_arr = None
        self._geo_thumb = self._compose_arr = None
        self.src_path = None
        self.graded_arr = self.cur_pil = self._view_pil = None
        self._showing_original = False
        self._token += 1                  # släng ev. pågående rendering
        self.canvas.delete("art")
        self.canvas.delete("hint")
        self.canvas.create_text(
            0, 0, text="Öppna ett foto för att börja", fill=INK2,
            font=self.sf(13, "italic"), tags="hint")
        self.canvas.coords("hint", self.canvas.winfo_width() / 2,
                           self.canvas.winfo_height() / 2)
        self._update_histogram(None)
        if self._thumb_after:
            self.after_cancel(self._thumb_after)
            self._thumb_after = None
        self._thumb_queue = []
        for c in self._cards.values():    # filmremsan visade annars kvar
            c["img"].configure(image=self._blank)   # det borttagna fotot
        self._thumbs.clear()
        self._set_enabled(self.save_btn, False)
        self._set_enabled(self.export_btn, False)
        self._set_enabled(self.savesess_btn, False)
        self._update_info()

    # ---------------------------------------------------------- ångra/gör om
    def _snapshot(self):
        """Ögonblicksbild av HELA sessionen: medlemskap + varje fotos recept +
        betyg + aktivt index. `items` är grund kopia (samma PhotoItem-refs →
        inga tunga arraykopior); bara EditState klonas."""
        return {
            "items": list(self.session),
            "edits": {it.uid: it.edit.clone() for it in self.session},
            "ratings": {it.uid: it.rating for it in self.session},
            "active": self.active_idx,
        }

    def _restore(self, snap):
        if self._crop_mode:
            self._exit_crop()
        self.session = list(snap["items"])
        for it in self.session:
            e = snap["edits"].get(it.uid)
            if e is not None:
                it.edit = e.clone()
            it.rating = snap["ratings"].get(it.uid, it.rating)
        self._sync_roll_cards()
        if self.session:
            idx = max(0, min(snap.get("active") or 0, len(self.session) - 1))
            self.active_idx = None      # undvik att _activate syncar UI tillbaka
            self._activate(idx)
            self._set_enabled(self.export_btn, True)
            self._set_enabled(self.savesess_btn, True)
        else:
            self._clear_active_view()
        self._refresh_roll_count()

    def _push_undo(self):
        """Kallas FÖRE varje mutation. Tömmer gör-om-stacken (ny gren)."""
        if self.active_idx is None or not self.session:
            return                     # inget aktivt foto → inget att ångra
        self._sync_ui_to_active()      # frys det som står på skärmen just nu
        self._undo.append(self._snapshot())
        if len(self._undo) > 50:
            self._undo.pop(0)
        self._redo.clear()
        self._set_enabled(self.undo_btn, True)
        self._set_enabled(self.redo_btn, False)

    def undo(self, _=None):
        if self._importing:
            # importtråden levererar foton medan den kör — ett ångra mitt i
            # skulle återställa rullen och sedan få nya foton inskjutna
            self.name_lbl.configure(text="Vänta tills importen är klar")
            return
        if not self._undo:
            self.name_lbl.configure(text="Inget att ångra")
            return
        self._sync_ui_to_active()
        self._redo.append(self._snapshot())
        self._restore(self._undo.pop())
        self._set_enabled(self.undo_btn, bool(self._undo))
        self._set_enabled(self.redo_btn, True)
        self.name_lbl.configure(text="Ångrade senaste ändringen")

    def redo(self, _=None):
        if self._importing:
            self.name_lbl.configure(text="Vänta tills importen är klar")
            return
        if not self._redo:
            self.name_lbl.configure(text="Inget att göra om")
            return
        self._sync_ui_to_active()
        self._undo.append(self._snapshot())
        self._restore(self._redo.pop())
        self._set_enabled(self.redo_btn, bool(self._redo))
        self._set_enabled(self.undo_btn, True)
        self.name_lbl.configure(text="Gjorde om ändringen")

    def _sync_roll_cards(self):
        """Gör rullens kort till en spegel av `self.session` (ångra/gör om kan
        ändra medlemskap, ordning och recept): kort för borttagna foton rivs,
        saknade skapas, befintliga ÅTERANVÄNDS — och med renderingscachen
        ritas bara tumnaglar vars recept faktiskt ändrats. Förut revs och
        renderades alla kort om vid varje ångra."""
        live = {it.uid for it in self.session}
        for uid in [u for u in self._roll_cards if u not in live]:
            self._roll_cards.pop(uid)["outer"].destroy()
        for item in self.session:
            if item.uid in self._roll_cards:
                self._update_roll_card(item, count=False)
            else:
                self._make_roll_card(item, count=False)
        self._repack_roll_cards()

    # ---------------------------------------------------------- exportera alla
    def export_all(self):
        """Rendera varje foto i rullen med SITT EGET recept, i full upplösning."""
        if getattr(self.export_btn, "_disabled", True) or not self.session:
            return
        self._sync_ui_to_active()      # ta med ev. osparade ändringar
        outdir = filedialog.askdirectory(title="Exportera alla till mapp")
        if outdir:
            self._export_to_dir(outdir)

    def _export_to_dir(self, outdir):
        if self._exporting or not self.session:
            return
        self._sync_ui_to_active()
        picks = [it for it in self.session if it.rating != -1]   # ej ratade
        skipped = len(self.session) - len(picks)
        if not picks:
            self.name_lbl.configure(text="Alla foton är ratade — inget att "
                                         "exportera")
            return
        ext = self.EXT_MAP.get(self.export_fmt, ".jpg")
        tail = f" ({skipped} ratade hoppas över)" if skipped else ""
        self.name_lbl.configure(
            text=f"Exporterar {self.export_fmt}: 0/{len(picks)} …{tail}")
        self._start_export([self._export_job(it, outdir=outdir, ext=ext)
                            for it in picks])

    def _export_job(self, item, out=None, outdir=None, ext=None):
        """Allt arbetartråden behöver, fryst NU (receptet kan ändras medan
        exporten pågår). Antingen en exakt utfil (Spara) eller mapp +
        ändelse (Exportera alla, som väljer ett ledigt filnamn)."""
        base = (os.path.splitext(os.path.basename(item.path))[0] + "_"
                + film_slug(item.edit.film_key))
        return {"src": item.path, "grade": grade_from_edit(item.edit),
                "k": item.edit.strength / 100.0, "crop": item.edit.crop,
                "angle": item.edit.angle, "out": out, "outdir": outdir,
                "ext": ext, "base": base}

    def _start_export(self, jobs, single=False):
        self._set_enabled(self.export_btn, False)
        self._set_enabled(self.save_btn, False)
        self._exporting = True
        # kvaliteten fryses vid start — ändras reglaget mitt i en export
        # ska inte halva rullen få en annan kvalitet
        threading.Thread(target=self._export_worker, daemon=True,
                         args=(jobs, self.jpeg_quality, single)).start()

    def _export_worker(self, jobs, quality, single):
        """Arbetartråd för Spara OCH Exportera alla: läser om originalet i
        full upplösning, applicerar geometri + recept och skriver med
        källans EXIF/ICC. Felorsaken sparas (visades inte alls förut)."""
        saved = failed = 0
        first_err = out_name = None
        n = len(jobs)
        for i, j in enumerate(jobs):
            try:
                arr = apply_geometry(load_rgb(j["src"]), j["crop"], j["angle"])
                graded = process(arr, j["grade"])
                pil = to_pil(blend_strength(arr, graded, j["k"]))
                del arr, graded
                outp = j["out"]
                if outp is None:
                    outp = os.path.join(j["outdir"], j["base"] + j["ext"])
                    c = 1
                    while os.path.exists(outp):
                        outp = os.path.join(j["outdir"],
                                            f"{j['base']}_{c}{j['ext']}")
                        c += 1
                save_image_file(pil, outp, quality, read_meta(j["src"]))
                saved += 1
                out_name = os.path.basename(outp)
            except Exception as e:      # noqa: BLE001 — hoppa över trasig fil
                failed += 1
                log_error(f"export {j['src']}")
                if first_err is None:
                    first_err = (str(e) or type(e).__name__)[:90]
            self._bq.put(("prog", i + 1, n, os.path.basename(j["src"])))
        self._bq.put(("done", saved, failed, first_err, single, out_name))

    def _drain_batch(self):
        try:
            while True:
                msg = self._bq.get_nowait()
                if msg[0] == "prog":
                    _, done, total, name = msg
                    if total > 1:
                        self.name_lbl.configure(
                            text=f"Exporterar: {done}/{total} · {name}")
                elif msg[0] == "done":
                    _, saved, failed, err, single, out_name = msg
                    self._exporting = False
                    self._close_warned = False
                    self._set_enabled(self.export_btn, bool(self.session))
                    self._set_enabled(self.save_btn, self.src_path is not None)
                    if single:
                        txt = (f"Sparad ▸ {out_name}" if saved
                               else "Kunde inte spara")
                    else:
                        txt = f"Export klar · {saved} sparade"
                        if failed:
                            txt += f" ({failed} misslyckades)"
                    if failed and err:
                        txt += f" — {err}"
                    self.name_lbl.configure(text=txt)
        except queue.Empty:
            pass

    # ---------------------------------------------------------- projekt (session)
    def save_session(self):
        """Spara hela rullen (filvägar + recept + betyg) till en .filmrulle-fil
        så man kan öppna där man slutade. Bildpixlar sparas inte — bara vägar
        och redigeringar, så filen är liten och läses om från originalen."""
        if getattr(self.savesess_btn, "_disabled", True) or not self.session:
            return
        self._sync_ui_to_active()
        path = filedialog.asksaveasfilename(
            title="Spara projekt", defaultextension=SESSION_EXT,
            initialfile="projekt" + SESSION_EXT,
            filetypes=[("Filmrulle-projekt", "*" + SESSION_EXT)])
        if path:
            self._write_session(path)

    def _write_session(self, path):
        """Skriv projektfilen (atomiskt). Varje foto sparas med både absolut
        sökväg och sökväg RELATIV till projektfilen — flyttas hela mappen
        (extern disk, annan dator) hittas fotona ändå."""
        self._sync_ui_to_active()
        pdir = os.path.dirname(os.path.abspath(path))

        def rel(p):
            try:
                return os.path.relpath(os.path.abspath(p), pdir)
            except ValueError:          # olika enheter på Windows
                return None

        data = {"version": VERSION, "active": self.active_idx or 0,
                "photos": [{"path": os.path.abspath(it.path),
                            "rel": rel(it.path),
                            "edit": it.edit.to_dict(), "rating": it.rating}
                           for it in self.session]}
        try:
            _write_json_atomic(path, data)
        except Exception as e:      # noqa: BLE001
            log_error("save_session")
            self.name_lbl.configure(text=f"Kunde inte spara projekt: {e}")
            return False
        self._session_path = path
        self.name_lbl.configure(
            text=f"Projekt sparat ▸ {os.path.basename(path)}")
        return True

    def _load_session_file(self, path):
        """Läs en .filmrulle-projektfil och ladda fotona via den TRÅDADE
        importvägen (samma som Öppna/DnD) — projektöppning frös tidigare
        GUI-tråden precis som import gjorde. Separerad från dialogen så
        den går att anropa/testa direkt med en sökväg."""
        if self._importing:
            self.name_lbl.configure(text="Import pågår redan — vänta")
            return
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:      # noqa: BLE001
            self.name_lbl.configure(text=f"Kunde inte läsa projekt: {e}")
            return
        # TOLKA och VALIDERA allt innan nuvarande rulle rörs — förut rensades
        # rullen först, så en trasig projektfil gav krasch + tom rulle
        photos = data.get("photos") if isinstance(data, dict) else None
        if not isinstance(photos, list):
            self.name_lbl.configure(text="Ogiltig projektfil")
            return
        pdir = os.path.dirname(os.path.abspath(path))
        jobs = []
        for p in photos:
            if not isinstance(p, dict):
                continue
            src = _resolve_project_path(p, pdir)
            if not src:
                continue
            r = p.get("rating")
            r = r if isinstance(r, int) and not isinstance(r, bool) \
                and r in (-1, 0, 1) else 0
            jobs.append((src, (EditState.from_dict(p.get("edit")), r)))
        if not jobs:
            self.name_lbl.configure(text="Projektet innehåller inga foton")
            return
        if not any(os.path.exists(src) for src, _m in jobs):
            self.name_lbl.configure(
                text="Projektets foton hittades inte — har de flyttats?")
            return
        if self.session and not self._confirm(
                "Öppna projekt",
                f"Ersätta nuvarande rulle ({len(self.session)} foton)?\n"
                "Ändringar som inte sparats i ett projekt går förlorade."):
            return
        # först NU rensas nuvarande session
        self._undo.clear()
        self._redo.clear()
        self._set_enabled(self.undo_btn, False)
        self._set_enabled(self.redo_btn, False)
        for c in self._roll_cards.values():
            c["outer"].destroy()
        self._roll_cards.clear()
        self._roll_img_cache.clear()
        self._cmp_cache = self._cmp_disp = None
        self.session = []
        self._clear_active_view()
        self._refresh_roll_count()
        self._pending_active = int(_clampf(_finite(data.get("active"), 0),
                                           0, len(jobs) - 1))
        self._session_path = path
        self._start_import(jobs)

    # ---------------------------------------------------------- OS-titelrad
    def _apply_titlebar_theme(self):
        """Färga OS:ets egna titelrad (min/maximera/stäng behålls INTAKTA och
        helt native) så den matchar aktuellt tema, via Windows DWM-API —
        istället för en helt egenritad titelrad (overrideredirect), som
        skulle kräva att drag-att-flytta, snap-till-kant och minimera-till-
        aktivitetsfält byggs om för hand. DWMWA_CAPTION_COLOR/TEXT_COLOR
        (Windows 11) sätter exakt appens PAPER/INK; DWMWA_USE_IMMERSIVE_
        DARK_MODE (Windows 10 1809+ och uppåt) ger åtminstone rätt mörk/ljus
        ikonuppsättning om de förra två skulle saknas på äldre Windows."""
        try:
            import ctypes
            from ctypes import wintypes
            self.update_idletasks()
            user32 = ctypes.windll.user32
            user32.GetParent.restype = wintypes.HWND
            user32.GetParent.argtypes = [wintypes.HWND]
            hwnd = user32.GetParent(self.winfo_id())
            dwm = ctypes.windll.dwmapi
            dwm.DwmSetWindowAttribute.argtypes = [
                wintypes.HWND, ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint]
            dark = ctypes.c_int(1 if THEME_NAME == "dark" else 0)
            dwm.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(dark),
                                      ctypes.sizeof(dark))
            cap = ctypes.c_int(_hex_to_colorref(PAPER))
            dwm.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(cap),
                                      ctypes.sizeof(cap))
            txt = ctypes.c_int(_hex_to_colorref(INK))
            dwm.DwmSetWindowAttribute(hwnd, 36, ctypes.byref(txt),
                                      ctypes.sizeof(txt))
        except Exception:      # noqa: BLE001 — kosmetiskt, aldrig kritiskt
            pass

    # ---------------------------------------------------------- dra och släpp
    def _enable_dnd(self):
        """Ta emot filer släppta på fönstret. tkinter saknar inbyggt stöd, så
        vi registrerar fönstret för WM_DROPFILES via ctypes och fångar
        meddelandet med en subclassad WndProc (körs på GUI-tråden, så
        self.after(1, …) räcker för att gå in i importvägen säkert)."""
        if getattr(self, "_old_wndproc", None):
            return          # redan installerad — dubbel subclassing hänger
        try:
            import ctypes
            from ctypes import wintypes
            self.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(self.winfo_id())
            user32 = ctypes.windll.user32
            shell32 = ctypes.windll.shell32
            shell32.DragAcceptFiles(hwnd, True)
            shell32.DragQueryFileW.argtypes = [wintypes.WPARAM, wintypes.UINT,
                                               ctypes.c_wchar_p, wintypes.UINT]
            shell32.DragQueryFileW.restype = wintypes.UINT
            shell32.DragFinish.argtypes = [wintypes.WPARAM]
            LRESULT = ctypes.c_ssize_t
            WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND,
                                         wintypes.UINT, wintypes.WPARAM,
                                         wintypes.LPARAM)
            user32.CallWindowProcW.restype = LRESULT
            user32.CallWindowProcW.argtypes = [ctypes.c_void_p, wintypes.HWND,
                                               wintypes.UINT, wintypes.WPARAM,
                                               wintypes.LPARAM]
            user32.SetWindowLongPtrW.restype = ctypes.c_void_p
            user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int,
                                                 ctypes.c_void_p]
            WM_DROPFILES = 0x0233

            def wndproc(hw, msg, wp, lp):
                if msg == WM_DROPFILES:
                    files = []
                    try:
                        n = shell32.DragQueryFileW(wp, 0xFFFFFFFF, None, 0)
                        for i in range(n):
                            ln = shell32.DragQueryFileW(wp, i, None, 0)
                            buf = ctypes.create_unicode_buffer(ln + 1)
                            shell32.DragQueryFileW(wp, i, buf, ln + 1)
                            files.append(buf.value)
                    except Exception:      # noqa: BLE001
                        log_error("dnd")
                    finally:
                        try:               # frigör ALLTID HDROP-handtaget
                            shell32.DragFinish(wp)
                        except Exception:      # noqa: BLE001
                            pass
                    if files:
                        self.after(1, lambda f=tuple(files):
                                   self._import_paths(f))
                    return 0
                return user32.CallWindowProcW(self._old_wndproc, hw, msg,
                                              wp, lp)

            self._wndproc_ref = WNDPROC(wndproc)   # håll referens (GC!)
            self._old_wndproc = user32.SetWindowLongPtrW(
                hwnd, -4, ctypes.cast(self._wndproc_ref, ctypes.c_void_p))
        except Exception:      # noqa: BLE001 — DnD är trevligt, inte kritiskt
            pass

    def _on_close(self):
        # halvskrivna filer på disk om man stänger mitt i en batchexport —
        # ett stängningsförsök varnar, nästa avbryter medvetet
        if self._exporting and not self._close_warned:
            self._close_warned = True
            self.name_lbl.configure(
                text="Export pågår — klicka stäng igen för att avbryta")
            return
        try:
            # räkna både maximerat OCH äkta F11-helskärm som "var stor" —
            # ingen anledning att öppna litet nästa gång bara för att man
            # råkade stänga medan man var i F11 istället för state("zoomed")
            self._settings["maximized"] = (self.state() == "zoomed"
                                           or self._fullscreen)
            self._flush_settings()        # inkl. ev. debouncad ändring
        except Exception:      # noqa: BLE001 — kosmetiskt, aldrig kritiskt
            pass
        try:
            self._renderer.stop()
        except Exception:      # noqa: BLE001
            pass
        # avbryt schemalagda callbacks — annars kan de köras mot en redan
        # riven Tcl-tolk ("invalid command name …_poll_render")
        for aid in (self._poll_after, self._render_after, self._crisp_after,
                    self._resize_after, self._thumb_after,
                    self._settings_after):
            if aid:
                try:
                    self.after_cancel(aid)
                except Exception:      # noqa: BLE001
                    pass
        self.destroy()


def main():
    _dpi_setup()
    App().mainloop()


if __name__ == "__main__":
    main()
