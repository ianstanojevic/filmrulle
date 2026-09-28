# -*- coding: utf-8 -*-
"""FRYST referens: bildpipelinen exakt som den såg ut i Filmrulle v2.0
(före prestandaomskrivningen). Används ENBART av testsviten för att bevisa
att den optimerade `filmrulle.process` ger samma bild (inom flyttalsbrus)
för alla filmer och justeringar. Ändra aldrig den här filen — då tappar
jämförelsen sin mening."""
import threading

import numpy as np
from PIL import Image, ImageFilter


def _clampf(v, lo, hi):
    return lo if v < lo else hi if v > hi else v


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
