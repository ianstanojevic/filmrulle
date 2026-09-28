# -*- coding: utf-8 -*-
"""Regressionstester för Filmrulle. Varje test i avsnitt 1–5 motsvarar en
bugg som hittades vid revisionen (sept 2026) och bevisar att den är åtgärdad;
avsnitt 6 bevisar att den prestandaoptimerade pipelinen ger samma bild som
den frysta v2.0-referensen."""
import json
import math
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import tkinter as tk
from PIL import Image

import filmrulle as F
import reference_pipeline_v20 as REF
from conftest import load, make_item, settle, wait_export, wait_import, \
    write_jpeg


def _add_preset(app, monkeypatch, name="Testpreset"):
    monkeypatch.setattr(F.simpledialog, "askstring", lambda *a, **k: name)
    return app.save_preset()


# =====================================================================
#  1. Krascher och datarförlust
# =====================================================================

def test_undo_after_deleting_preset_does_not_crash(app, monkeypatch):
    """Ångra-snapshots höll kvar nyckeln till en raderad preset →
    KeyError i FILM_BY_KEY vid ångra, med halvt återställt UI."""
    load(app, 2)
    key = _add_preset(app, monkeypatch)
    app.select_film(key)
    app._push_undo()
    app._delete_preset(key)
    app.undo()
    settle(app)
    assert app.film_key in F.FILM_BY_KEY
    assert all(it.edit.film_key in F.FILM_BY_KEY for it in app.session)
    for snap in app._undo + app._redo:
        assert all(e.film_key in F.FILM_BY_KEY for e in snap["edits"].values())


def test_unknown_film_key_falls_back_to_original():
    e = F.EditState()
    e.film_key = "user_raderad"
    g = F.grade_from_edit(e)            # tidigare: KeyError
    assert g.exposure == 0 and not g.bw
    assert F.film_entry("finns_inte")[0] == "original"


def test_malformed_project_does_not_wipe_current_session(app, tmp_path):
    """Nuvarande rulle rensades INNAN projektfilen tolkats — en trasig fil
    gav krasch och en tom rulle."""
    load(app, 2)
    for bad in ('{"photos": "inte en lista"}', "[1, 2, 3]", '"text"'):
        p = tmp_path / "bad.filmrulle"
        p.write_text(bad, encoding="utf-8")
        app._load_session_file(str(p))
        assert len(app.session) == 2
        assert not app._importing


def test_project_with_garbage_fields_still_loads(app, tmp_path):
    img = write_jpeg(tmp_path / "a.jpg")
    data = {"version": "2.0", "active": "x", "photos": [
        {"path": str(img), "rating": "bra",
         "edit": {"film_key": "velvia", "crop": None, "angle": "NaN",
                  "strength": float("nan"), "adjust": [1, 2],
                  "grain_size": 99, "curve": [[0, 0], [1, 1], [1.0, 0.5]]}},
        "inte ett foto"]}
    p = tmp_path / "skräp.filmrulle"
    p.write_text(json.dumps(data), encoding="utf-8")
    app._load_session_file(str(p))
    wait_import(app)
    assert len(app.session) == 1
    e = app.session[0].edit
    assert e.film_key == "velvia"
    assert e.crop == F.NO_CROP
    assert e.strength == 100.0 and e.angle == 0.0
    assert 1.0 <= e.grain_size <= 5.0
    assert app.session[0].rating == 0
    settle(app, lambda: app.cur_pil is not None)
    assert np.isfinite(app.geo_arr).all()


@pytest.mark.parametrize("raw", [
    None, "x", [], {"crop": [0.5, 0.5, 0.1, 0.1]},
    {"crop": ["a", 0, 1, 1]}, {"adjust": {"contrast": "hög"}},
    {"curve": "rak"}, {"curve": [[0.5]]}, {"angle": float("inf")},
])
def test_editstate_from_dict_tolerates_garbage(raw):
    e = F.EditState.from_dict(raw)
    assert e.crop == F.NO_CROP or (e.crop[0] < e.crop[2] and e.crop[1] < e.crop[3])
    assert all(math.isfinite(v) for v in e.adjust.values())
    assert math.isfinite(e.angle)
    g = F.grade_from_edit(e)
    out = F.process(np.full((8, 8, 3), 0.5, np.float32), g)
    assert np.isfinite(out).all()


def test_settings_file_with_non_dict_json(app_factory):
    """Giltig JSON som inte är ett objekt ("[]", "null") kraschade appen
    vid start ('list' object has no attribute 'get')."""
    for content in ("[1, 2]", "null", '"text"'):
        Path(F.SETTINGS_FILE).write_text(content, encoding="utf-8")
        a = app_factory()
        assert a.export_fmt == "JPEG" and a.jpeg_quality == 95


def test_settings_with_wrong_types(app_factory):
    Path(F.SETTINGS_FILE).write_text(
        '{"jpeg_quality": "hög", "export_fmt": 5, "theme": 3}', encoding="utf-8")
    a = app_factory()
    assert a.jpeg_quality == 95 and a.export_fmt == "JPEG"


@pytest.mark.parametrize("content", [
    "[]", "null", '{"presets": ["x", 5, null]}',
    '{"presets": [{"key": "user_1", "label": 7, "grade": {"temp": "varm", '
    '"shadow_tint": "blå", "curves": [1], "user_curve": "x", "bw": "ja"}}]}',
])
def test_corrupt_presets_file_does_not_crash(content, app_factory):
    Path(F.PRESETS_FILE).write_text(content, encoding="utf-8")
    a = app_factory()
    load(a, 1)
    for key, _l, _s, g in F.FILMS:           # alla (även inlästa) går att rendera
        assert np.isfinite(F.process(np.full((6, 6, 3), .4, np.float32), g)).all()


def test_json_write_is_atomic(tmp_path, monkeypatch):
    """Ett avbrott mitt i skrivningen lämnade en halvskriven fil — för
    presets betydde det att ALLA egna looks försvann."""
    p = tmp_path / "presets.json"
    F._write_json_atomic(str(p), {"a": 1})
    real = F.json.dump

    def boom(obj, f, **kw):
        f.write('{"trasig": ')
        raise OSError("disken full")

    monkeypatch.setattr(F.json, "dump", boom)
    with pytest.raises(OSError):
        F._write_json_atomic(str(p), {"a": 2})
    monkeypatch.setattr(F.json, "dump", real)
    assert json.loads(p.read_text(encoding="utf-8")) == {"a": 1}
    assert [x.name for x in tmp_path.iterdir()] == ["presets.json"]


def test_delete_preset_requires_confirmation(app, monkeypatch):
    load(app, 1)
    key = _add_preset(app, monkeypatch)
    app._confirm = lambda *a, **k: False
    app._delete_preset(key)
    assert key in F.FILM_BY_KEY
    app._confirm = lambda *a, **k: True
    app._delete_preset(key)
    assert key not in F.FILM_BY_KEY
    assert key not in app._thumbs


def test_open_project_asks_before_replacing_session(app, tmp_path):
    load(app, 2)
    img = write_jpeg(tmp_path / "b.jpg")
    p = tmp_path / "p.filmrulle"
    p.write_text(json.dumps({"photos": [{"path": str(img)}]}), encoding="utf-8")
    app._confirm = lambda *a, **k: False
    app._load_session_file(str(p))
    assert len(app.session) == 2 and not app._importing


def test_project_with_only_missing_photos_keeps_session(app, tmp_path):
    load(app, 2)
    p = tmp_path / "p.filmrulle"
    p.write_text(json.dumps({"photos": [{"path": str(tmp_path / "borta.jpg")}]}),
                 encoding="utf-8")
    app._load_session_file(str(p))
    assert len(app.session) == 2
    assert "hittades inte" in app.name_lbl.cget("text")


def test_project_survives_moving_its_folder(app, tmp_path):
    """Projektet sparade bara absoluta sökvägar — flyttades mappen (extern
    disk, annan dator) gick inget foto att öppna."""
    d1 = tmp_path / "rulle"
    d1.mkdir()
    img = write_jpeg(d1 / "x.jpg")
    app._import_paths([str(img)])
    wait_import(app)
    app._write_session(str(d1 / "p.filmrulle"))
    d2 = tmp_path / "flyttad"
    shutil.move(str(d1), str(d2))
    app._load_session_file(str(d2 / "p.filmrulle"))
    wait_import(app)
    assert len(app.session) == 1
    assert Path(app.session[0].path).parent == d2


def test_callback_exceptions_are_logged(app):
    """Den byggda exe:n saknar konsol — fel i Tk-callbacks försvann spårlöst."""
    try:
        raise ValueError("testfel-123")
    except ValueError:
        app.report_callback_exception(*sys.exc_info())
    assert "testfel-123" in Path(F.ERROR_LOG).read_text(encoding="utf-8")


def test_two_app_instances_can_coexist(app_factory):
    """tk.PhotoImage skapades utan master → bilden hamnade i FÖRSTA
    Tk-tolken och en andra instans kraschade ('image doesn't exist')."""
    app_factory()
    b = app_factory()
    load(b, 1)
    assert b.cur_pil is not None


# =====================================================================
#  2. Logikfel i redigeringsflödet
# =====================================================================

def test_switching_photo_exits_crop_mode(app):
    """Beskärningsläget låg kvar vid fotobyte — 'Klar' applicerade då det
    FÖRRA fotots väntande beskärning på det nya."""
    load(app, 2)
    app.enter_crop()
    app._crop_rect = [0.1, 0.1, 0.5, 0.5]
    app._on_roll_click(app.session[1].uid)
    assert not app._crop_mode
    app._apply_crop()                      # no-op utanför beskärningsläget
    assert app.session[1].edit.crop == F.NO_CROP
    assert app.session[0].edit.crop == F.NO_CROP


def test_removing_photo_in_crop_mode_exits_crop(app):
    load(app, 2)
    app.enter_crop()
    app.remove_active_photo()
    assert not app._crop_mode


def test_paste_updates_preview_geometry(app):
    """Inklistrad beskärning/vinkel syntes inte förrän man bytte foto."""
    load(app, 2)
    app._cur_crop = (0.0, 0.0, 0.5, 0.5)
    app._apply_geo()
    app.copy_settings()
    app._on_roll_click(app.session[1].uid)
    full_w = app.geo_arr.shape[1]
    app.paste_settings()
    assert app.geo_arr.shape[1] < full_w


def test_removing_last_photo_clears_view(app):
    """geo_arr låg kvar — mellanslag/klick visade det borttagna fotot."""
    load(app, 1)
    app.remove_active_photo()
    assert app.geo_arr is None and app.cur_pil is None
    app._show_original()
    assert not app._showing_original


def test_redo_to_empty_roll_clears_view(app):
    load(app, 1)
    app.remove_active_photo()
    app.undo()
    settle(app, lambda: app.cur_pil is not None)
    app.redo()
    assert app.session == [] and app.src_path is None and app.geo_arr is None
    assert app.save_btn._disabled and app.export_btn._disabled


def test_select_film_uses_films_own_grain(app, monkeypatch):
    """Filmbyte satte alltid kornstorlek 1.5/struktur 0 — Tri-X:s 1.6 var
    död kod och en sparad preset tappade sin kornkaraktär."""
    load(app, 1)
    app.select_film("trix")
    assert app.gsize.get() == pytest.approx(1.6)
    app.gsize.set(3.2)
    app.grough.set(55)
    key = _add_preset(app, monkeypatch, "Grovt")
    app.select_film("original")
    assert app.gsize.get() == pytest.approx(F.DEFAULT_GS)
    app.select_film(key)
    assert app.gsize.get() == pytest.approx(3.2)
    assert app.grough.get() == pytest.approx(55)


def test_curve_click_in_margin_never_duplicates_x():
    """Klick i kurveditorns marginal gav en punkt med SAMMA x som
    ändpunkten → PCHIP-lutningen exploderade (spik i högdagrarna)."""
    root = tk.Tk()
    try:
        ed = F.CurveEditor(root, on_change=None)
        ed.pack()
        root.update()
        w, h = ed._dims()
        for px in (w - 2, 2, w - 4):
            e = SimpleNamespace(x=px, y=h // 3)
            ed._press(e)
            ed._release(e)
        xs = [p[0] for p in ed.pts]
        assert xs == sorted(xs)
        assert all(b - a >= 0.019 for a, b in zip(xs, xs[1:]))
    finally:
        root.destroy()


def test_sanitize_curve_drops_duplicates_and_garbage():
    pts = F.sanitize_curve([[1, 1], [0, 0], [1.0, 0.5], ["x", 2], [0.5, 9]])
    xs = [p[0] for p in pts]
    assert xs == sorted(xs) and len(set(xs)) == len(xs)
    ys = [p[1] for p in pts]
    assert all(0.0 <= y <= 1.0 for y in ys)
    _gx, gy = F._pchip_lut(pts)
    assert gy.min() >= min(ys) - 1e-6 and gy.max() <= max(ys) + 1e-6
    assert F.sanitize_curve([[0, 0]]) is None
    assert F.sanitize_curve("x") is None


def test_undo_blocked_while_importing(app):
    load(app, 1)
    app._push_undo()
    app.sliders["contrast"].set(20)
    app._importing = True
    app.undo()
    assert len(app._undo) == 1
    app._importing = False


def test_locked_aspect_top_edge_drag_resizes(app):
    """Med låst förhållande gjorde topp-/bottenhandtagen ingenting — höjden
    räknades tillbaka ur den oförändrade bredden."""
    load(app, 1)
    app.enter_crop()
    app._set_aspect(1.0)
    before = list(app._crop_rect)
    x0, y0, dw, dh = app._crop_tf()
    e = SimpleNamespace(x=int(x0 + (before[0] + before[2]) / 2 * dw),
                        y=int(y0 + before[1] * dh))
    app._crop_press(e)
    e.y += int(dh * 0.15)
    app._crop_motion(e)
    after = app._crop_rect
    assert after[1] > before[1] + 0.05
    iw, ih = app._crop_disp.size
    aspect = (after[2] - after[0]) * iw / ((after[3] - after[1]) * ih)
    assert aspect == pytest.approx(1.0, rel=0.02)


def test_track_wheel_snaps_to_step(app):
    t = app.sliders["exposure"]
    t.set(0.0)
    for _ in range(3):
        t._wheel(SimpleNamespace(delta=120))
    assert t.get() == 0.3                    # inte 0.30000000000000004


def test_disabled_chip_does_not_run_command(app):
    hits = []
    b = app._chip(app, "X", lambda: hits.append(1))
    b.place(x=5, y=5)
    app.update()
    app._set_enabled(b, False)
    b.event_generate("<Button-1>", x=2, y=2)
    app.update()
    assert hits == []
    app._set_enabled(b, True)
    b.event_generate("<Button-1>", x=2, y=2)
    app.update()
    assert hits == [1]


def test_mouse_buttons_and_wheel_per_platform():
    """Tk 8.6 på macOS numrerar HÖGER-knappen 2 och mitten 3 (omvänt mot
    Windows) och ger hjul-delta ±1 per steg istället för ±120."""
    assert F.mouse_buttons("win32", 8.6) == ("<Button-3>", "<Button-2>")
    assert F.mouse_buttons("darwin", 8.6) == ("<Button-2>", "<Button-3>")
    assert F.mouse_buttons("darwin", 9.0) == ("<Button-3>", "<Button-2>")
    assert F.wheel_units(120, "win32") == 120
    assert F.wheel_units(1, "darwin") >= 20
    assert F.wheel_units(-1, "darwin") <= -20


# =====================================================================
#  3. Fil-I/O: import, export, metadata
# =====================================================================

def test_tiff_container_raw_failure_is_not_replaced_by_thumbnail(tmp_path):
    """Pillow öppnar en äkta DNG som dess 160 px-inbäddade miniatyr — om
    LibRaw inte kan avkoda filen fick man TYST en frimärksstor bild."""
    p = tmp_path / "trasig.dng"
    Image.new("RGB", (16, 12), (200, 10, 10)).save(p, format="TIFF")
    with pytest.raises(Exception):
        F.load_rgb(str(p))
    with pytest.raises(Exception):
        F.load_preview(str(p))


def test_jpeg_with_raw_extension_loads(tmp_path):
    p = tmp_path / "R0000107.DNG"
    write_jpeg(p, size=(64, 48))
    assert F.load_rgb(str(p)).shape == (48, 64, 3)
    prev, w, h = F.load_preview(str(p))
    assert (w, h) == (64, 48) and prev.size == (64, 48)


def test_load_preview_reports_full_size_and_orientation(tmp_path):
    p = tmp_path / "rot.jpg"
    ex = Image.Exif()
    ex[0x0112] = 6                            # roterad 90° i kameran
    write_jpeg(p, size=(3000, 2000), exif=ex.tobytes(), quality=85)
    prev, w, h = F.load_preview(str(p))
    assert (w, h) == (2000, 3000)             # uppräta fullmått
    assert max(prev.size) <= F.PREVIEW_MAX
    assert prev.size[0] < prev.size[1]        # porträtt efter rotation
    assert F.load_rgb(str(p)).shape[:2] == (3000, 2000)


def test_imported_preview_is_stored_as_uint8(app, tmp_path):
    """float32-förhandsvisningar tog 15.7 MB/foto (200 foton = 3.1 GB)."""
    p = write_jpeg(tmp_path / "a.jpg", size=(1600, 1000))
    app._import_paths([str(p)])
    wait_import(app)
    assert app.session[0].prev_arr.dtype == np.uint8
    assert app.session[0].prev_arr.shape == (875, 1400, 3)
    assert app.prev_arr.dtype == np.float32 and app.prev_arr.max() <= 1.0


def test_import_while_importing_does_not_push_undo(app, tmp_path):
    load(app, 1)
    app._importing = True
    app._import_paths([str(write_jpeg(tmp_path / "a.jpg"))])
    assert app._undo == []
    app._importing = False


def test_export_preserves_exif_and_icc(tmp_path):
    """Exporten tappade tagningsdatum/kamera (EXIF) och färgprofilen —
    iPhone-bilder (Display P3) såg urblekta ut efter export."""
    src = tmp_path / "src.jpg"
    ex = Image.Exif()
    ex[0x010F] = "RICOH"
    ex[0x0112] = 6
    ex[0x8769] = {0x9003: "2026:07:14 12:34:56"}
    icc = b"FAKE-ICC-PROFILE" * 20
    write_jpeg(src, size=(80, 60), exif=ex.tobytes(), icc_profile=icc)
    meta = F.read_meta(str(src))
    for ext in (".jpg", ".png", ".tif"):
        out = tmp_path / f"out{ext}"
        F.save_image_file(Image.new("RGB", (60, 80)), str(out), 90, meta)
        with Image.open(out) as o:
            assert o.info.get("icc_profile") == icc, ext
            e = o.getexif()
            assert e.get(0x010F) == "RICOH", ext
            assert e.get(0x0112) == 1, ext            # pixlarna är redan uppräta
            if ext == ".jpg":
                assert e.get_ifd(0x8769).get(0x9003) == "2026:07:14 12:34:56"
    assert not list(tmp_path.glob("*.part"))


def test_save_rejects_unknown_extension(tmp_path):
    with pytest.raises(ValueError):
        F.save_image_file(Image.new("RGB", (4, 4)), str(tmp_path / "x.okänd"))


def test_threaded_single_save_writes_file(app, tmp_path):
    img = write_jpeg(tmp_path / "in.jpg", size=(200, 150))
    app._import_paths([str(img)])
    wait_import(app)
    out = tmp_path / "ut.jpg"
    app._save_to(str(out))
    assert app._exporting                        # renderas i bakgrunden
    wait_export(app)
    with Image.open(out) as o:
        assert o.size == (200, 150)
    assert "Sparad" in app.name_lbl.cget("text")


def test_export_reports_failure_reason(app, tmp_path):
    img = write_jpeg(tmp_path / "in.jpg")
    app._import_paths([str(img)])
    wait_import(app)
    img.unlink()                                 # källan försvinner
    outdir = tmp_path / "ut"
    outdir.mkdir()
    app._export_to_dir(str(outdir))
    wait_export(app)
    txt = app.name_lbl.cget("text")
    assert "misslyckades" in txt and "—" in txt


def test_export_filename_uses_preset_label(app, monkeypatch):
    load(app, 1)
    key = _add_preset(app, monkeypatch, "Min Look! 2")
    assert F.film_slug(key) == "Min_Look_2"
    assert F.film_slug("velvia") == "velvia"


def test_quality_slider_does_not_write_settings_per_event(app, monkeypatch):
    """Varje musrörelse på JPEG-kvalitetsreglaget skrev settings-filen."""
    writes = []
    monkeypatch.setattr(F, "save_settings", lambda d: writes.append(dict(d)))
    for q in range(70, 100):
        app.qtrack.set(q)
        app._on_quality()
    assert len(writes) == 0
    app._flush_settings()
    assert writes[-1]["jpeg_quality"] == 99


# =====================================================================
#  4. Prestanda (antal dyra operationer, inte väggklocka)
# =====================================================================

def test_compare_mode_does_not_rerender_right_image(app, monkeypatch):
    """Högerbilden renderades om synkront vid VARJE compose (123 → 25 ms)."""
    load(app, 2)
    calls = []
    real = app._render_item_preview
    monkeypatch.setattr(app, "_render_item_preview",
                        lambda it: (calls.append(it.uid), real(it))[1])
    app.toggle_compare()
    for _ in range(5):
        app._compose()
    assert calls.count(app._compare_uid) == 1


def test_crop_drag_does_not_rerotate_image(app, monkeypatch):
    """Varje musrörelse i beskärningsläget roterade om hela förhands-
    visningen (~140 ms/rörelse för ett uprätat foto)."""
    load(app, 1)
    app._cur_angle = 3.0
    app._apply_geo()
    app.enter_crop()
    n = []
    real = F.apply_geometry
    monkeypatch.setattr(F, "apply_geometry",
                        lambda *a, **k: (n.append(1), real(*a, **k))[1])
    x0, y0, dw, dh = app._crop_tf()
    e = SimpleNamespace(x=int(x0 + dw / 2), y=int(y0 + dh / 2))
    app._crop_press(e)
    for _ in range(10):
        e.x += 2
        app._crop_motion(e)
    assert n == []


def test_undo_rerenders_only_changed_roll_cards(app, monkeypatch):
    """Ångra rev och renderade om ALLA rullkort (40 foton ≈ 0.5 s frys)."""
    load(app, 6)
    app._push_undo()
    app.sliders["contrast"].set(25)
    app._sync_ui_to_active()
    n = []
    real = app._roll_thumb_image
    monkeypatch.setattr(app, "_roll_thumb_image",
                        lambda it: (n.append(it.uid), real(it))[1])
    app.undo()
    assert n == [app.session[0].uid]


def test_theme_toggle_reuses_roll_thumbnails(app, monkeypatch):
    load(app, 5)
    n = []
    real = app._roll_thumb_image
    monkeypatch.setattr(app, "_roll_thumb_image",
                        lambda it: (n.append(it.uid), real(it))[1])
    app._toggle_theme()
    assert n == []
    app._toggle_theme()


def test_vignette_cache_holds_several_sizes():
    c = F.VignetteCache()
    a = c.get(90, 120)
    c.get(30, 40)
    assert c.get(90, 120) is a                # tidigare: omräknad (thrash)
    ref = REF.VignetteCache().get(90, 120)
    assert np.array_equal(a, ref)


# =====================================================================
#  5. UI-regressioner (hittade vid visuell kontroll med riktiga foton)
# =====================================================================

def test_info_label_counts_all_imported_photos(app, tmp_path):
    """Info-raden visade "(1/1)" efter import av flera foton — totalen
    sattes bara när det första fotot aktiverades under den trådade importen."""
    paths = [str(write_jpeg(tmp_path / f"{i}.jpg")) for i in range(3)]
    app._import_paths(paths)
    wait_import(app)
    assert "(1/3)" in app.info_lbl.cget("text")


def test_adjust_button_not_squeezed_by_long_filename(app):
    """Ett långt filnamn i info-raden tryckte ihop Justera-knappen ("tera")."""
    app.geometry("1100x700")
    load(app, 1)
    app.session[0].path = "C:/" + "mycket_långt_filnamn_" * 6 + ".DNG"
    app._update_info()
    settle(app, lambda: False, 0.3)
    assert app.adj_btn.winfo_width() >= app.adj_btn.winfo_reqwidth() - 2
    assert app.cmp_btn.winfo_width() >= app.cmp_btn.winfo_reqwidth() - 2


def test_crop_bar_stays_visible_in_short_window(app):
    """Beskärningsraden (med Klar/Avbryt) packades EFTER canvasen, som begär
    7 cm höjd. När fönstret var för lågt för allt (t.ex. 200 % DPI-skalning,
    ej maximerat) klämdes den sist packade — beskärningsraden — till 0 px."""
    app.minsize(1, 1)
    app.geometry("1100x600")
    load(app, 1)
    app.enter_crop()
    settle(app, lambda: False, 0.3)
    assert app.crop_bar.winfo_viewable()
    assert app.crop_bar.winfo_height() >= app.crop_bar.winfo_reqheight() - 2


def test_clean_view_toggle_keeps_crop_overlay(app):
    """Ren vy av/på ritade om GALLERIVYN även i beskärningsläge — rutan och
    handtagen försvann under en vanlig bild medan läget fortfarande var på."""
    load(app, 1)
    app.enter_crop()
    app._toggle_chrome()
    settle(app, lambda: False, 0.2)
    app._toggle_chrome()
    settle(app, lambda: False, 0.3)
    assert app._crop_mode
    assert app.canvas.find_withtag("crop")
    assert not app.canvas.find_withtag("art")


def test_histogram_fills_actual_canvas_width(app):
    """Juli 2026: histogrammet ritades bara i canvasens konfigurerade bredd."""
    load(app, 1)
    app._toggle_adjust()
    app.update()
    app._update_histogram(app._compose_arr)
    w = app.hist.winfo_width()
    xs = [x for i in app.hist.find_all() for x in app.hist.coords(i)[0::2]]
    assert max(xs) >= w - 3


# =====================================================================
#  6. Pipeline: optimerad == fryst v2.0-referens
# =====================================================================

TOL_MAX = 1.0 / 255          # ingen pixel får flytta mer än en 8-bitarsnivå
TOL_MEAN = 5e-5


def _rand_img(seed, h=97, w=131):
    rng = np.random.default_rng(seed)
    img = rng.random((h, w, 3)).astype(np.float32)
    img[5:25, 10:40] = 0.97                   # ljus fläck → halation
    return img


def _assert_same(new, old):
    d = np.abs(new.astype(np.float64) - old.astype(np.float64))
    assert new.shape == old.shape and new.dtype == np.float32
    assert d.max() <= TOL_MAX, d.max()
    assert d.mean() <= TOL_MEAN, d.mean()


@pytest.mark.parametrize("key", [f[0] for f in F.FILMS])
def test_pipeline_matches_reference_per_film(key):
    img = _rand_img(7)
    g = F.grade_from_edit(F.EditState(film_key=key))
    _assert_same(F.process(img, g, seed=3), REF.process(img, g, seed=3))


def test_pipeline_matches_reference_with_all_adjustments():
    rng = np.random.default_rng(11)
    keys = [f[0] for f in F.FILMS]
    img = _rand_img(5)
    for trial in range(16):
        e = F.EditState(film_key=keys[trial % len(keys)])
        e.adjust = {k: float(rng.uniform(-60, 60)) for k in F.ADJ_FIELDS}
        e.adjust["exposure"] = float(rng.uniform(-1.5, 1.5))
        e.grain_size = float(rng.uniform(1.0, 4.0))
        e.grain_rough = float(rng.uniform(0, 100))
        if trial % 2:
            e.curve = [[0, 0.05], [0.3, 0.2], [0.7, 0.85], [1, 0.95]]
        g = F.grade_from_edit(e)
        if trial % 3 == 0:
            g.bw, g.tone = True, (0.03, 0.0, -0.02)
        _assert_same(F.process(img, g, seed=trial), REF.process(img, g, seed=trial))


def test_process_does_not_mutate_input():
    img = _rand_img(3)
    before = img.copy()
    F.process(img, F.grade_from_edit(F.EditState(film_key="natt")))
    assert np.array_equal(img, before)


def test_grade_default_grain_size_matches_ui_default():
    assert F.Grade().grain_size == F.DEFAULT_GS
