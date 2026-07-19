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
import threading
import time
import tkinter as tk
from dataclasses import asdict, dataclass, field, fields as dc_fields, replace
from tkinter import filedialog, font as tkfont, simpledialog

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageOps, ImageTk

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

VERSION = "2.0"
SESSION_EXT = ".filmrulle"
PRESETS_FILE = os.path.join(os.path.expanduser("~"),
                            ".filmrulle_presets.json")
SETTINGS_FILE = os.path.join(os.path.expanduser("~"),
                             ".filmrulle_settings.json")
RAW_EXTS = {".cr2", ".cr3", ".nef", ".arw", ".dng", ".raf", ".orf", ".rw2"}
HEIF_EXTS = {".heic", ".heif"}


def load_settings():
    """Små appinställningar som ska minnas mellan körningar (just nu bara
    tema). Egen fil, separat från presets — olika livslängd/syfte."""
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:      # noqa: BLE001 — ingen fil = defaultinställningar
        return {}


def save_settings(d):
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=1)
    except Exception:      # noqa: BLE001
        pass


def load_rgb(path):
    """Öppna en bildfil som en float32 RGB-array (0–1), fullt bitdjup bevarat.
    Vanliga format (+ HEIC om pillow-heif finns) via Pillow i 8 bitar; RAW via
    rawpy i **16 bitar** — kamerans sensor levererar 12–14 bitar per kanal,
    och en tidig avrundning till 8 bitar (256 nivåer) kastar tondjup som
    annars finns kvar vid kraftiga skugglyft/highlight-recovery. Delas av
    ALLA inläsningsvägar (import, session, spara, export) så både
    formatstöd och bitdjup är konsekvent överallt."""
    ext = os.path.splitext(path)[1].lower()
    if ext in RAW_EXTS:
        if not RAW_OK:
            raise RuntimeError("RAW-stöd saknas (installera rawpy)")
        with rawpy.imread(path) as raw:
            rgb16 = raw.postprocess(use_camera_wb=True, output_bps=16)
        return rgb16.astype(np.float32) / 65535.0
    if ext in HEIF_EXTS and not HEIF_OK:
        raise RuntimeError("HEIC-stöd saknas (installera pillow-heif)")
    im = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
    return np.asarray(im, np.float32) / 255.0

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
    grain_size: float = 1.0      # 1 = per pixel, större = grövre korn
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
        temp=-6, contrast=26, saturation=34, fade=0, grain=6,
        curves={"r": [(0, 0), (0.25, 0.2), (0.75, 0.82), (1, 1)],
                "g": [(0, 0), (0.25, 0.22), (0.75, 0.8), (1, 1)]})),
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
    ("hp5", "HP5 Svartvitt", "korn · kontrast", Grade(
        bw=True, contrast=20, fade=6, grain=24)),
    ("trix", "Tri-X 400", "gritty · dokumentär", Grade(
        bw=True, contrast=34, fade=2, grain=32, grain_size=1.6)),
]
FILM_BY_KEY = {f[0]: f for f in FILMS}


# =====================================================================
#  Egna presets — användarens sparade looks, egna kort i remsan
# =====================================================================

def _grade_from_dict(d):
    """Grade ur JSON-dict; tolerant mot okända/gamla fält."""
    valid = {f.name for f in dc_fields(Grade)}
    kw = {k: v for k, v in d.items() if k in valid}
    for tup in ("shadow_tint", "highlight_tint", "tone"):
        if isinstance(kw.get(tup), list):
            kw[tup] = tuple(kw[tup])
    return Grade(**kw)


def load_user_presets():
    """Läs in sparade presets och registrera dem som filmer i remsan.
    Körs vid appstart INNAN korten byggs och sessioner läses (EditState.
    from_dict validerar film_key mot FILM_BY_KEY)."""
    try:
        with open(PRESETS_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:      # noqa: BLE001 — ingen fil = inga presets
        return
    for p in data.get("presets", []):
        key, label = p.get("key"), p.get("label", "Egen")
        if not key or not key.startswith("user_") or key in FILM_BY_KEY:
            continue
        try:
            entry = (key, label, "egen preset", _grade_from_dict(
                p.get("grade", {})))
        except Exception:      # noqa: BLE001 — korrupt preset hoppas över
            continue
        FILMS.append(entry)
        FILM_BY_KEY[key] = entry


def save_user_presets():
    presets = [{"key": k, "label": lab, "grade": asdict(g)}
               for k, lab, _s, g in FILMS if k.startswith("user_")]
    try:
        with open(PRESETS_FILE, "w", encoding="utf-8") as f:
            json.dump({"presets": presets}, f, indent=1)
    except Exception:      # noqa: BLE001
        pass


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
        e = EditState()
        e.film_key = d.get("film_key", "original")
        if e.film_key not in FILM_BY_KEY:
            e.film_key = "original"
        adj = d.get("adjust", {})
        e.adjust = {k: float(adj.get(k, 0.0)) for k in ADJ_FIELDS}
        e.grain_size = float(d.get("grain_size", DEFAULT_GS))
        e.grain_rough = float(d.get("grain_rough", 0.0))
        e.strength = float(d.get("strength", 100.0))
        c = d.get("crop", NO_CROP)
        e.crop = tuple(float(v) for v in c) if len(c) == 4 else NO_CROP
        e.angle = float(d.get("angle", 0.0))
        cv = d.get("curve")
        e.curve = [[float(p[0]), float(p[1])] for p in cv] \
            if cv and len(cv) >= 2 else None
        return e            # (äldre projekt med "local_adjust" ignoreras tyst)


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
    g = replace(FILM_BY_KEY[edit.film_key][3])
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
        g.user_curve = sorted((float(p[0]), float(p[1]))
                              for p in edit.curve)
    return g


# =====================================================================
#  Bildpipeline (numpy, float32 0–1)  — oförändrad kärna
# =====================================================================

def _apply_curve(chan, pts):
    xs = np.array([p[0] for p in pts], dtype=np.float32)
    ys = np.array([p[1] for p in pts], dtype=np.float32)
    return np.interp(chan, xs, ys).astype(np.float32)


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


def _mix_hex(a, b, t):
    """Blanda två '#rrggbb'-färger (t=0 -> a, t=1 -> b)."""
    a, b = a.lstrip("#"), b.lstrip("#")
    return "#%02x%02x%02x" % tuple(
        int(int(a[i:i + 2], 16) * (1 - t) + int(b[i:i + 2], 16) * t + 0.5)
        for i in (0, 2, 4))


class VignetteCache:
    """Cachar den FÄRDIGKURVADE fallofmasken (r**2.2) — potensen på en
    hel kanal är dyr och beräknades tidigare om vid varje rendering trots
    att själva radiemasken var cachad."""

    def __init__(self):
        self._k = None
        self._m = None
        self._lock = threading.Lock()   # delas mellan preview- och batch-tråd

    def get(self, h, w):
        with self._lock:
            if self._k != (h, w):
                yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
                cy, cx = (h - 1) / 2.0, (w - 1) / 2.0
                nx = (xx - cx) / (w / 2.0)
                ny = (yy - cy) / (h / 2.0)
                r = np.sqrt(nx * nx + ny * ny) / 1.41421356
                m = np.clip(r, 0, 1).astype(np.float32)
                self._m = (m ** 2.2).astype(np.float32)
                self._k = (h, w)
            return self._m


_VIG = VignetteCache()


def _luma(arr):
    return (arr[..., 0] * 0.299 + arr[..., 1] * 0.587
            + arr[..., 2] * 0.114).astype(np.float32)


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
    """Hela filmpipelinen. Arbetar IN-PLACE på en egen kopia (`x`) —
    varje `x = np.clip(...)`/aritmetisk omskrivning i den gamla versionen
    allokerade en ny ~16 MB-buffer per steg för en 1400px-förhandsvisning;
    med `out=x` och `+=`-former återanvänds samma buffer genom hela kedjan."""
    x = arr.astype(np.float32, copy=True)
    if g.exposure:
        x *= float(2.0 ** g.exposure)
    if g.temp or g.tint:
        t = g.temp / 100.0
        ti = g.tint / 100.0
        x[..., 0] *= (1.0 + 0.35 * t) * (1.0 + 0.10 * ti)
        x[..., 1] *= 1.0 - 0.20 * ti
        x[..., 2] *= (1.0 - 0.35 * t) * (1.0 + 0.10 * ti)
    np.clip(x, 0.0, 1.0, out=x)
    if g.contrast:
        f = 1.0 + (g.contrast / 100.0) * 0.9
        x -= 0.5
        x *= f
        x += 0.5
        np.clip(x, 0.0, 1.0, out=x)
    if g.curves:
        for i, ch in enumerate("rgb"):
            pts = g.curves.get(ch)
            if pts:
                x[..., i] = _apply_curve(x[..., i], pts)
    if g.user_curve:
        # användarens egen tonkurva — OVANPÅ filmens inbakade kurvor.
        # Mjuk monoton PCHIP via LUT (samma matematik som editorns ritning)
        gx, gy = _pchip_lut(g.user_curve)
        for i in range(3):
            x[..., i] = np.interp(x[..., i], gx, gy).astype(np.float32)
        np.clip(x, 0.0, 1.0, out=x)
    if g.saturation:
        s = 1.0 + g.saturation / 100.0
        lum = _luma(x)[..., None]
        x -= lum
        x *= s
        x += lum
        np.clip(x, 0.0, 1.0, out=x)
    if g.split and (any(g.shadow_tint) or any(g.highlight_tint)):
        lum = _luma(x)
        sh = ((1.0 - lum) * g.split)[..., None]
        hi = (lum * g.split)[..., None]
        x += sh * np.array(g.shadow_tint, np.float32)
        x += hi * np.array(g.highlight_tint, np.float32)
        np.clip(x, 0.0, 1.0, out=x)
    if g.bw:
        lum = _luma(x)[..., None]
        x = np.repeat(lum, 3, axis=2)
        if any(g.tone):
            mid = 1.0 - np.abs(2.0 * lum - 1.0)
            x += mid * np.array(g.tone, np.float32)
            np.clip(x, 0.0, 1.0, out=x)
    if g.fade:
        fd = g.fade / 100.0
        floor = 0.09 * fd
        x *= 1.0 - floor - 0.04 * fd
        x += floor
        np.clip(x, 0.0, 1.0, out=x)
    if g.clarity:
        # lokal mellantonskontrast: oskarp mask med STOR radie på luminansen
        h, w = x.shape[:2]
        lum = _luma(x)
        rad = max(2.0, min(h, w) * 0.02)
        detail = (lum - _blur_big(lum, rad))[..., None]
        midweight = (1.0 - np.abs(2.0 * lum - 1.0))[..., None]  # spar högdager/skugga
        x += detail * midweight * (g.clarity / 100.0 * 0.9)
        np.clip(x, 0.0, 1.0, out=x)
    if g.sharpen:
        # oskarp mask med liten radie på LUMINANSEN — en blur istället för
        # tre (kanalvis), och luma-skärpning förstärker inte kromatiskt
        # brus/färgfrans som kanalvis skärpning gör
        h, w = x.shape[:2]
        rad = max(0.6, min(h, w) * 0.0015)
        lum = _luma(x)
        x += (lum - _blur_f(lum, rad))[..., None] * (g.sharpen / 100.0 * 1.4)
        np.clip(x, 0.0, 1.0, out=x)
    if g.halation:
        h, w = x.shape[:2]
        lum = _luma(x)
        thr = 0.65                  # lägre tröskel -> fler ljusa partier glöder
        mask = np.clip((lum - thr) / (1.0 - thr), 0.0, 1.0)
        mask *= np.sqrt(mask)       # == mask**1.5, men sqrt är hårdvarusnabb
        radius = max(2.0, min(h, w) * 0.02)   # bredare, mer filmisk spridning
        glow = _blur_big(mask, radius)
        amt = g.halation / 100.0 * 1.6        # kraftigare intensitet
        halo = np.array([1.0, 0.32, 0.14], np.float32)
        x += glow[..., None] * halo * amt
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
        amt = (t ** 1.35) * 0.09
        x += (noise * weight * amt)[..., None]
        np.clip(x, 0.0, 1.0, out=x)
    if g.vignette:
        m = _VIG.get(x.shape[0], x.shape[1])   # redan **2.2-kurvad i cachen
        amt = g.vignette / 100.0
        x *= (1.0 - amt * m)[..., None]
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
        self.set(self.value + self._step_size() * (1 if e.delta > 0 else -1))
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
        self.cv.bind("<Button-3>", self._remove)
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
    def _press(self, e):
        if self.on_grab:
            self.on_grab()          # ångra-ögonblick före ändring
        i = self._hit(e.x, e.y)
        if i is None:               # klick på tom yta = ny punkt
            x, y = self._to_norm(e.x, e.y)
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
        self.pts = [[float(p[0]), float(p[1])] for p in pts] \
            if pts and len(pts) >= 2 else [[0.0, 0.0], [1.0, 1.0]]
        self.pts.sort(key=lambda p: p[0])
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
            peak = max(1, int(hv.max()))
            self._hist = (hv / peak).astype(np.float32)
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
            self._icon = ImageTk.PhotoImage(make_icon_image(64))
            self.iconphoto(True, self._icon)
        except Exception:
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
        # export-inställningar (delas av Spara + Exportera alla)
        self._export_open = False
        self.export_fmt = "JPEG"      # JPEG | PNG | TIFF
        self.jpeg_quality = 95
        self._fmt_cards = {}

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

        load_user_presets()           # egna presets in i FILMS före kortbygget

        self._rq = queue.Queue()
        self._bq = queue.Queue()        # batch-förlopp
        self._iq = queue.Queue()        # importtrådens leveranser
        self._renderer = Renderer(self._rq)
        self._renderer.start()

        self._build_ui()
        self.after(30, self._poll_render)
        self.after(300, self._enable_dnd)   # fönstret måste finnas först
        self.after(50, self._apply_titlebar_theme)  # hwnd måste finnas först
        self.bind("<Configure>", self._on_resize)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # tangentbord
        self.bind("<Control-o>", lambda e: self.open_image())
        self.bind("<Control-s>", lambda e: self.save_image())
        self.bind("<Control-b>", lambda e: self.export_all())
        self.bind("<Control-c>", lambda e: self.copy_settings())
        self.bind("<Control-v>", lambda e: self.paste_settings())
        self.bind("<Control-Shift-V>", lambda e: self.sync_active_to_all())
        self.bind("<Control-Left>", lambda e: self._nav(-1))
        self.bind("<Control-Right>", lambda e: self._nav(1))
        self.bind("<Control-z>", lambda e: self.undo())
        self.bind("<Control-y>", lambda e: self.redo())
        self.bind("<Control-Shift-Z>", lambda e: self.redo())
        self.bind("<Control-Shift-S>", lambda e: self.save_session())
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
        self._blank = tk.PhotoImage(width=CARD_W, height=CARD_IMG_H)
        self._roll_blank = tk.PhotoImage(width=ROLL_W, height=ROLL_H)

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
        self.info_lbl = tk.Label(row, text="", bg=PAPER, fg=FAINT,
                                 font=self.sf(9, "italic"))
        self.info_lbl.pack(side="left", padx=(4, 0))
        self.cmp_btn = self._chip(row, "Håll: original", None)
        self.cmp_btn.pack(side="right")
        self.cmp_btn.bind("<ButtonPress-1>", self._show_original)
        self.cmp_btn.bind("<ButtonRelease-1>", self._show_graded)
        self.adj_btn = self._chip(row, "Justera  ▲", self._toggle_adjust)
        self.adj_btn.pack(side="right", padx=10)

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
        self.canvas = tk.Canvas(self, bg=WALL, highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True, side="top")
        self.canvas.create_text(0, 0, text="Öppna ett foto för att börja",
                                 fill=INK2, font=self.sf(13, "italic"),
                                 tags="hint")
        self.canvas.bind("<ButtonPress-1>", self._on_canvas_press)
        self.canvas.bind("<B1-Motion>", self._on_canvas_motion)
        self.canvas.bind("<ButtonRelease-1>", self._on_canvas_release)
        self.canvas.bind("<Double-Button-1>", self._reset_zoom)
        self.canvas.bind("<MouseWheel>", self._on_zoom)
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
        if cmd:
            b.bind("<Button-1>", lambda e: cmd())
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
            w.bind("<Button-3>", lambda e, k=key: self._delete_preset(k))
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

    def _make_roll_card(self, item):
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
        self._update_roll_card(item)

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

    def _update_roll_card(self, item):
        c = self._roll_cards.get(item.uid)
        if not c:
            return
        base = apply_geometry(item.thumb_src, item.edit.crop, item.edit.angle)
        g = grade_from_edit(item.edit)
        graded = process(base, g)
        arr = blend_strength(base, graded, item.edit.strength / 100.0)
        pil = ImageOps.fit(to_pil(arr), (ROLL_W, ROLL_H), Image.LANCZOS)
        tkimg = ImageTk.PhotoImage(pil)
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
        n = len(jobs)
        for i, (path, meta) in enumerate(jobs):
            try:
                arr = load_rgb(path)
                # fullupplösningen hålls INTE i RAM — laddas om från disk
                # vid Spara/Exportera. Preview/thumb behöver inte fullt
                # bitdjup, så de skalas ner via en 8-bitars PIL-bild.
                h, w = arr.shape[:2]
                full_pil = to_pil(arr)
                prev = full_pil.copy()
                prev.thumbnail((PREVIEW_MAX, PREVIEW_MAX), Image.LANCZOS)
                th = full_pil.copy()
                th.thumbnail((THUMB_SRC, THUMB_SRC), Image.LANCZOS)
                self._iq.put(("photo", path,
                              np.asarray(prev, np.float32) / 255.0,
                              np.asarray(th, np.float32) / 255.0,
                              w, h, meta))
            except Exception as e:  # noqa: BLE001 — hoppa över trasig fil,
                self._iq.put(("fail", str(e)))   # men BEHÅLL orsaken
            self._iq.put(("prog", i + 1, n))
        self._iq.put(("idone",))

    def _drain_import(self):
        """Plocka upp färdiglästa foton från importtråden (GUI-tråden)."""
        try:
            while True:
                msg = self._iq.get_nowait()
                kind = msg[0]
                if kind == "photo":
                    _, path, prev, th, w, h, meta = msg
                    item = PhotoItem(path=path, prev_arr=prev, thumb_src=th,
                                     w=w, h=h)
                    if meta is not None:
                        item.edit, item.rating = meta
                    self.session.append(item)
                    self._make_roll_card(item)
                    self._imp_added += 1
                    self._set_enabled(self.export_btn, True)
                    self._set_enabled(self.savesess_btn, True)
                    if self.active_idx is None:
                        self._activate(len(self.session) - 1)
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
            tkimg = ImageTk.PhotoImage(pil)
            self._thumbs[key] = tkimg
            try:
                card["img"].configure(image=tkimg)
            except tk.TclError:
                return          # widget riven (tembyte) — kön är förlegad
        if self._thumb_queue:
            self._thumb_after = self.after(1, self._thumb_step)

    def _cycle_film(self, step):
        keys = [f[0] for f in FILMS]
        i = (keys.index(self.film_key) + step) % len(keys)
        self.select_film(keys[i])

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
        self.film_key = edit.film_key
        self._mark_card(self.film_key)
        self.film_name.configure(text=FILM_BY_KEY[self.film_key][1])
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
        self._sync_ui_to_active()      # spara det förra fotots ändringar
        self.active_idx = idx
        item = self.session[idx]
        self.prev_arr = item.prev_arr
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

    def select_film(self, key):
        if key == self.film_key:
            self.reset_adjust()       # klick på vald film = nollställ look
            return
        self._push_undo()
        self.film_key = key
        self._mark_card(key)
        self.film_name.configure(text=FILM_BY_KEY[key][1])
        for s in self.sliders.values():
            s.reset()
        self.gsize.set(DEFAULT_GS)
        self.grough.set(0)
        self.curve_ed.reset()     # dokumenterat: "nollställs när du byter
        self.strength.set(100)    # film" — tonkurvan är en del av looken
        self.intensity = 1.0
        self._request_render()

    def reset_adjust(self):
        self._push_undo()
        for s in self.sliders.values():
            s.reset()
        self.gsize.set(DEFAULT_GS)
        self.grough.set(0)
        self.curve_ed.reset()
        self._request_render()

    # ---------------------------------------------------------- histogram
    def _update_histogram(self, arr):
        c = self.hist
        w = int(c["width"])
        hgt = int(c["height"])
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
        # robust normalisering: rena svart/vit-spikar (yttersta binnarna) kan
        # bli många gånger högre än resten och skulle annars trycka ner hela
        # kurvan. Sätt taket från de inre binnarna och klipp det som spiller
        # över, så mittonerna fyller ut rutan.
        interior = np.concatenate([h[1:-1] for h in chans]) if bins > 2 \
            else np.concatenate(chans)
        peak = max(1, int(interior.max()))
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

    def _delete_preset(self, key):
        """Högerklick på ett eget preset-kort tar bort det. Foton i rullen
        som använder presetens faller tillbaka på Original."""
        if not key.startswith("user_") or key not in FILM_BY_KEY:
            return
        self._sync_ui_to_active()
        label = FILM_BY_KEY[key][1]
        for it in self.session:
            if it.edit.film_key == key:
                it.edit.film_key = "original"
                self._update_roll_card(it)
        entry = FILM_BY_KEY.pop(key)
        FILMS.remove(entry)
        card = self._cards.pop(key, None)
        if card:
            card["outer"].destroy()
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
        for item in self.session:
            self._make_roll_card(item)
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
            self._bottom.pack(fill="x", side="bottom")
        self.after(40, lambda: self._draw_compare() if self._compare_mode
                   else self._draw(self._view_pil))

    # ---------------------------------------------------------- jämförelse
    def _render_item_preview(self, item):
        """Rendera ett rull-fotos look i förhandsvisningsupplösning (för
        jämförelsevyn — engångsberäkning, inte via bakgrundstråden)."""
        base = apply_geometry(item.prev_arr, item.edit.crop, item.edit.angle)
        graded = process(base, grade_from_edit(item.edit))
        arr = blend_strength(base, graded, item.edit.strength / 100.0)
        return to_pil(arr)

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
            else:
                pil = self._render_item_preview(item)
            iw, ih = pil.size
            margin, mat = 28, 12
            avail_w = max(1, colw - 2 * (margin + mat))
            avail_h = max(1, ch - 2 * (margin + mat) - 24)
            s = min(avail_w / iw, avail_h / ih, 1.0)
            dw, dh = max(1, int(iw * s)), max(1, int(ih * s))
            disp = pil.resize((dw, dh), Image.LANCZOS) \
                if (dw, dh) != (iw, ih) else pil
            tkimg = ImageTk.PhotoImage(disp)
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
        pw = self.adjust.winfo_width() or self.adjust.winfo_reqwidth()
        ph = self.adjust.winfo_height() or self.adjust.winfo_reqheight()
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
        except Exception:      # noqa: BLE001 — loopen får aldrig dö
            pass
        finally:
            try:
                self.after(30, self._poll_render)
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
            self._tk_img = ImageTk.PhotoImage(disp)
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
        self._tk_img = ImageTk.PhotoImage(crop.resize((dw, dh), rs))
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
        new = _clampf(old * (1.0015 ** e.delta), 1.0, 8.0)
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
        self.crop_bar.pack(fill="x", side="bottom")
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

    def _draw_crop(self):
        self._crop_disp = to_pil(apply_geometry(self.prev_arr, NO_CROP,
                                                self._crop_angle))
        self.canvas.delete("art")
        self.canvas.delete("crop")
        x0, y0, dw, dh = self._crop_tf()
        if dw < 2 or dh < 2:              # canvasen inte layoutad än
            return
        self._crop_dispimg = ImageTk.PhotoImage(
            self._crop_disp.resize((dw, dh), Image.BILINEAR))
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
        """Justera höjden efter bredden (kring den fasta hörnankaren) så att
        låst förhållande hålls under hörndrag."""
        nr = self._norm_ratio()
        x0, y0, x1, y1 = self._crop_rect
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

    def _ew_drag_press(self, e):
        self._ew_grab = (e.x_root, e.y_root, self._ew_x, self._ew_y)

    def _ew_drag_move(self, e):
        gx, gy, bx, by = self._ew_grab
        self._ew_x = _clampf(bx + (e.x_root - gx), 4, self.winfo_width() - 60)
        self._ew_y = _clampf(by + (e.y_root - gy), 4, self.winfo_height() - 60)
        self.exportwin.place(x=self._ew_x, y=self._ew_y, anchor="nw")

    def _save_pil(self, pil, path):
        """Skriv en PIL-bild med format ur filändelsen. JPEG = vald kvalitet,
        PNG/TIFF = förlustfritt (TIFF LZW-komprimerad)."""
        ext = os.path.splitext(path)[1].lower()
        if ext == ".png":
            pil.save(path)
        elif ext in (".tif", ".tiff"):
            pil.save(path, compression="tiff_lzw")
        else:
            pil.save(path, quality=int(self.jpeg_quality), subsampling=0)

    # ---------------------------------------------------------- spara
    def save_image(self):
        if getattr(self.save_btn, "_disabled", True) or self.src_path is None:
            return
        base = os.path.splitext(os.path.basename(self.src_path))[0] \
            + "_" + self.film_key
        de = self.EXT_MAP.get(self.export_fmt, ".jpg")
        path = filedialog.asksaveasfilename(
            title="Spara bild", defaultextension=de, initialfile=base,
            filetypes=[("JPEG", "*.jpg"), ("PNG", "*.png"),
                       ("TIFF", "*.tif")])
        if not path:
            return
        self.name_lbl.configure(text="Renderar fullupplösning …")
        self.update_idletasks()
        try:
            full = load_rgb(self.src_path)
            full = apply_geometry(full, self._cur_crop, self._cur_angle)
            graded = process(full, self._effective_grade())
            arr = blend_strength(full, graded, self.intensity)
            self._save_pil(to_pil(arr), path)
        except Exception as e:      # noqa: BLE001
            self.name_lbl.configure(text=f"Kunde inte spara: {e}")
            return
        self.name_lbl.configure(text=f"Sparad ▸ {os.path.basename(path)}")

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
        self._request_render()

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
            self._update_roll_card(item)
        self.name_lbl.configure(
            text=f"Alla {len(self.session)} foton synkade till aktuell look")

    def remove_active_photo(self):
        """Ta bort aktivt foto ur rullen (rör inte filen på disk)."""
        if self.active_idx is None:
            return
        self._push_undo()
        item = self.session.pop(self.active_idx)
        card = self._roll_cards.pop(item.uid, None)
        if card:
            card["outer"].destroy()
        if not self.session:
            self.active_idx = None
            self.prev_arr = self.thumb_src = None
            self.src_path = None
            self.graded_arr = self.cur_pil = self._view_pil = None
            self.canvas.delete("art")
            self.canvas.create_text(
                0, 0, text="Öppna ett foto för att börja", fill=INK2,
                font=self.sf(13, "italic"), tags="hint")
            self.canvas.coords("hint", self.canvas.winfo_width() / 2,
                               self.canvas.winfo_height() / 2)
            self._set_enabled(self.save_btn, False)
            self._set_enabled(self.export_btn, False)
            self._set_enabled(self.savesess_btn, False)
            self.name_lbl.configure(text="")
            self._update_info()
        else:
            new_idx = min(self.active_idx, len(self.session) - 1)
            self.active_idx = None      # tvinga _activate ladda om helt
            self._activate(new_idx)
        self._refresh_roll_count()

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
        self.session = list(snap["items"])
        for it in self.session:
            e = snap["edits"].get(it.uid)
            if e is not None:
                it.edit = e.clone()
            it.rating = snap["ratings"].get(it.uid, it.rating)
        self._rebuild_roll_cards()
        if self.session:
            idx = max(0, min(snap["active"], len(self.session) - 1))
            self.active_idx = None      # undvik att _activate syncar UI tillbaka
            self._activate(idx)
        else:
            self.active_idx = None
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
        if not self._redo:
            self.name_lbl.configure(text="Inget att göra om")
            return
        self._sync_ui_to_active()
        self._undo.append(self._snapshot())
        self._restore(self._redo.pop())
        self._set_enabled(self.redo_btn, bool(self._redo))
        self._set_enabled(self.undo_btn, True)
        self.name_lbl.configure(text="Gjorde om ändringen")

    def _rebuild_roll_cards(self):
        """Riv och bygg om rullens kort utifrån aktuell `self.session`
        (används av ångra, som kan ändra medlemskapet)."""
        for c in self._roll_cards.values():
            c["outer"].destroy()
        self._roll_cards.clear()
        for item in self.session:
            self._make_roll_card(item)

    # ---------------------------------------------------------- exportera alla
    def export_all(self):
        """Rendera varje foto i rullen med SITT EGET recept, i full upplösning."""
        if getattr(self.export_btn, "_disabled", True) or not self.session:
            return
        self._sync_ui_to_active()      # ta med ev. osparade ändringar
        outdir = filedialog.askdirectory(title="Exportera alla till mapp")
        if not outdir:
            return
        # hoppa över ratade foton
        picks = [it for it in self.session if it.rating != -1]
        skipped = len(self.session) - len(picks)
        jobs = [(item.path, grade_from_edit(item.edit),
                item.edit.strength / 100.0, item.edit.film_key,
                item.edit.crop, item.edit.angle)
               for item in picks]
        if not jobs:
            self.name_lbl.configure(text="Alla foton är ratade — inget att "
                                         "exportera")
            return
        self._set_enabled(self.export_btn, False)
        self._set_enabled(self.save_btn, False)
        self._exporting = True
        ext = self.EXT_MAP.get(self.export_fmt, ".jpg")
        tail = f" ({skipped} ratade hoppas över)" if skipped else ""
        self.name_lbl.configure(
            text=f"Exporterar {self.export_fmt}: 0/{len(jobs)} …{tail}")
        threading.Thread(target=self._batch_worker, daemon=True,
                         args=(jobs, outdir, ext)).start()

    def _batch_worker(self, jobs, outdir, ext):
        saved = failed = 0
        n = len(jobs)
        for i, (path, grade, k, film_key, crop, angle) in enumerate(jobs):
            try:
                arr = load_rgb(path)
                arr = apply_geometry(arr, crop, angle)
                graded = process(arr, grade)
                pil = to_pil(blend_strength(arr, graded, k))
                base = os.path.splitext(os.path.basename(path))[0] \
                    + "_" + film_key
                outp = os.path.join(outdir, base + ext)
                c = 1
                while os.path.exists(outp):
                    outp = os.path.join(outdir, f"{base}_{c}{ext}")
                    c += 1
                self._save_pil(pil, outp)
                saved += 1
            except Exception:      # noqa: BLE001 — hoppa över trasig fil
                failed += 1
            self._bq.put(("prog", i + 1, n, os.path.basename(path)))
        self._bq.put(("done", saved, failed))

    def _drain_batch(self):
        try:
            while True:
                msg = self._bq.get_nowait()
                if msg[0] == "prog":
                    _, done, total, name = msg
                    self.name_lbl.configure(
                        text=f"Exporterar: {done}/{total} · {name}")
                elif msg[0] == "done":
                    _, saved, failed = msg
                    self._exporting = False
                    self._close_warned = False
                    self._set_enabled(self.export_btn, bool(self.session))
                    self._set_enabled(self.save_btn, self.src_path is not None)
                    extra = f" ({failed} misslyckades)" if failed else ""
                    self.name_lbl.configure(
                        text=f"Export klar · {saved} sparade{extra}")
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
        if not path:
            return
        data = {"version": VERSION, "active": self.active_idx or 0,
                "photos": [{"path": os.path.abspath(it.path),
                            "edit": it.edit.to_dict(), "rating": it.rating}
                           for it in self.session]}
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=1)
        except Exception as e:      # noqa: BLE001
            self.name_lbl.configure(text=f"Kunde inte spara projekt: {e}")
            return
        self._session_path = path
        self.name_lbl.configure(
            text=f"Projekt sparat ▸ {os.path.basename(path)}")

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
        if self._compare_mode:         # gamla jämförelse-uid:n blir stallade
            self._exit_compare()
        # rensa nuvarande session
        self._undo.clear()
        self._redo.clear()
        self._set_enabled(self.undo_btn, False)
        self._set_enabled(self.redo_btn, False)
        for c in self._roll_cards.values():
            c["outer"].destroy()
        self._roll_cards.clear()
        self.session = []
        self.active_idx = None
        self._refresh_roll_count()
        jobs = [(p.get("path", ""),
                 (EditState.from_dict(p.get("edit", {})),
                  int(p.get("rating", 0))))
                for p in data.get("photos", [])]
        if not jobs:
            self.name_lbl.configure(text="Projektet innehåller inga foton")
            return
        self._pending_active = int(data.get("active", 0))
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
                    try:
                        n = shell32.DragQueryFileW(wp, 0xFFFFFFFF, None, 0)
                        files = []
                        for i in range(n):
                            ln = shell32.DragQueryFileW(wp, i, None, 0)
                            buf = ctypes.create_unicode_buffer(ln + 1)
                            shell32.DragQueryFileW(wp, i, buf, ln + 1)
                            files.append(buf.value)
                        shell32.DragFinish(wp)
                        if files:
                            self.after(1, lambda f=tuple(files):
                                       self._import_paths(f))
                    except Exception:      # noqa: BLE001
                        pass
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
            save_settings(self._settings)
        except Exception:      # noqa: BLE001 — kosmetiskt, aldrig kritiskt
            pass
        try:
            self._renderer.stop()
        except Exception:
            pass
        self.destroy()


def main():
    _dpi_setup()
    App().mainloop()


if __name__ == "__main__":
    main()
