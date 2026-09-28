# -*- coding: utf-8 -*-
"""Gemensamma fixtures för Filmrulles testsvit.

Varje test får egna settings/presets/fel-loggfiler i en tmp-katalog —
användarens riktiga filer i hemkatalogen rörs aldrig — och FILMS-listan
återställs efteråt (tester skapar/raderar egna presets).
"""
import os
import sys
import time
from pathlib import Path

import tkinter as tk

import numpy as np
import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import filmrulle as F  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(F, "SETTINGS_FILE", str(tmp_path / "settings.json"))
    monkeypatch.setattr(F, "PRESETS_FILE", str(tmp_path / "presets.json"))
    monkeypatch.setattr(F, "ERROR_LOG", str(tmp_path / "errors.log"),
                        raising=False)
    films, by_key = list(F.FILMS), dict(F.FILM_BY_KEY)
    yield
    F.FILMS[:] = films
    F.FILM_BY_KEY.clear()
    F.FILM_BY_KEY.update(by_key)


def _close(app):
    app._exporting = False
    try:
        app._on_close()
    except Exception:  # noqa: BLE001 — redan stängd
        pass


@pytest.fixture
def app_factory():
    """Skapa App-instanser (flera samtidigt går) som städas efter testet."""
    made = []

    def make():
        # Tcl:s init-skript kan sporadiskt fallera ("invalid command name
        # tcl_findLibrary") när dussintals tolkar skapas/rivs i rad i SAMMA
        # process — sker aldrig i appen (en tolk), bara i testsviten
        for attempt in range(3):
            try:
                a = F.App()
                break
            except tk.TclError:
                if attempt == 2:
                    raise
                time.sleep(0.2)
        a._confirm = lambda *args, **kw: True   # inga modala dialoger i test
        a.update_idletasks()
        made.append(a)
        return a

    yield make
    for a in reversed(made):
        _close(a)


@pytest.fixture
def app(app_factory):
    return app_factory()


def settle(app, cond=lambda: True, timeout=5.0):
    """Kör Tk-händelseloopen tills `cond()` är sant (eller timeout)."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.update()
        if cond():
            return True
        time.sleep(0.01)
    return cond()


def make_item(seed, w=320, h=210, path=None):
    """Ett rullfoto byggt EXAKT som importen bygger det (uint8-förhands-
    visning + float-tumnagel), från en slumpad bild."""
    rng = np.random.default_rng(seed)
    img = Image.fromarray((rng.random((h, w, 3)) * 255).astype(np.uint8))
    return F.make_photo_item(path or f"foto{seed}.jpg", img, w, h)


def load(app, n, w=320, h=210):
    """Lägg in n foton i rullen, aktivera det första och vänta på rendering."""
    for s in range(n):
        it = make_item(s + 1, w, h)
        app.session.append(it)
        app._make_roll_card(it)
    app._refresh_roll_count()
    app.active_idx = None
    app._activate(0)
    settle(app, lambda: app.cur_pil is not None)
    return app.session


def write_jpeg(path, size=(64, 48), color=(120, 80, 40), **save_kw):
    Image.new("RGB", size, color).save(path, format="JPEG", **save_kw)
    return path


def wait_import(app, timeout=20.0):
    settle(app, lambda: not app._importing, timeout)


def wait_export(app, timeout=30.0):
    settle(app, lambda: not app._exporting, timeout)
