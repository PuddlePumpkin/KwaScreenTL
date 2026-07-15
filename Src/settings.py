import os
import json
import time
import customtkinter as ctk

from hotkeys import (
    MOD_CONTROL, MOD_ALT, MOD_SHIFT, MOD_NOREPEAT,
    VK_LCTRL, VK_RCTRL, VK_LALT, VK_RALT, VK_LSHIFT, VK_RSHIFT,
    _hk_display, _vk_to_display,
    user32,
)
from utils import _PROJECT_DIR

ctk.set_appearance_mode("light")
ctk.set_default_color_theme("blue")

_BG = "#E8E9ED"
_CARD = "#F3F4F6"
_BORDER = "#E5E7EB"
_ACCENT = "#4A6984"
_ACCENT_HOVER = "#3A546D"
_TEXT = "#1F2937"
_TEXT2 = "#9CA3AF"

class SettingsManager:
    def __init__(self, app):
        self.app = app
        self.window = None
        self.recording_action = None
        self.recording_btn = None
        self.recording_pending = None
        self.recording_prev = None
        self.hk_suppress_until = 0.0
        self.hk_btns = {}
        self._file = os.path.join(_PROJECT_DIR, "Data", "settings.json")
        self._build_window()

    # ── Preloaded window ─────────────────────────────────────────────────

    def _build_window(self):
        a = self.app
        win = ctk.CTkToplevel(a.root)
        win.withdraw()
        win.title("Settings")
        win.resizable(False, False)
        win.attributes("-topmost", True)
        win.configure(fg_color=_BG)
        icon_path = os.path.join(_PROJECT_DIR, "Launcher", "AppIcon.ico")
        if os.path.exists(icon_path):
            try:
                win.iconbitmap(icon_path)
            except Exception:
                pass
        self.window = win
        self._setup_ui(win)
        win.protocol("WM_DELETE_WINDOW", lambda: (
            self._cancel_recording(), win.withdraw()
        ))

    def _sync_from_app(self):
        a = self.app
        self._show_ocr_var.set(a.show_ocr_text)
        self._show_furigana_var.set(a.show_furigana)
        self._show_romaji_var.set(a.show_romaji)
        self._skip_nj_var.set(a.skip_non_japanese)
        self._show_crop_var.set(a.show_crop)
        self._translator_menu.set(
            "DeepL" if a.translator == "deepl" else "Google" if a.translator == "google" else "None")
        self._dict_type_menu.set("JA-EN" if a.dictionary_type == "English" else "JA-JA")
        opts = {"25%": 25, "50%": 50, "75%": 75, "100%": 100}
        rev = {v: k for k, v in opts.items()}
        self._scale_menu.set(rev[a.region_detect_scale])
        self._entries_slider.set(self._val_to_slider(a.max_dict_entries))
        self._entries_lbl.configure(text=self._slider_disp(self._val_to_slider(a.max_dict_entries)))
        self._senses_slider.set(self._val_to_slider(a.max_dict_senses))
        self._senses_lbl.configure(text=self._slider_disp(self._val_to_slider(a.max_dict_senses)))
        self._show_in_region_var.set(a.show_in_region_translation)
        self._skip_num_var.set(a.skip_numeric_only)
        self._threshold_slider.set(self._threshold_val_to_slider(a.in_region_auto_threshold))
        self._threshold_lbl.configure(text=self._threshold_disp(a.in_region_auto_threshold))
        for action in ("capture", "snip", "settings"):
            btn = self.hk_btns.get(action)
            if btn and btn.winfo_exists():
                btn.configure(text=getattr(a, f"hk_{action}")["display"])
        self._entries_lbl_dbg.configure(text=f"Entries: {self._count_cache_entries()}")

    def _start_refresh_timer(self):
        def refresh_count():
            if not self.window or not self.window.winfo_exists():
                return
            if self.window.winfo_viewable():
                self._entries_lbl_dbg.configure(text=f"Entries: {self._count_cache_entries()}")
                self.window.after(5000, refresh_count)
        self.window.after(5000, refresh_count)

    # ── Persistence ──────────────────────────────────────────────────────

    def load(self):
        try:
            with open(self._file, "r") as f:
                data = json.load(f)
            a = self.app
            a.show_crop = data.get("show_crop", True)
            a.show_ocr_text = data.get("show_ocr_text", True)
            a.show_furigana = data.get("show_furigana", True)
            a.show_romaji = data.get("show_romaji", True)
            a.skip_non_japanese = data.get("skip_non_japanese", a.skip_non_japanese)
            a.translator = data.get("translator", a.translator)
            a.dictionary_type = data.get("dictionary_type", "English")
            a.region_detect_scale = data.get("region_detect_scale", 100)
            a.max_dict_entries = data.get("max_dict_entries", 4)
            a.max_dict_senses = data.get("max_dict_senses", 4)
            a.show_in_region_translation = data.get("show_in_region_translation", False)
            a.skip_numeric_only = data.get("skip_numeric_only", True)
            a.in_region_auto_threshold = data.get("in_region_auto_threshold", 0)
            hk = data.get("hotkeys", {})
            if "capture" in hk:
                a.hk_capture = hk["capture"]
            if "snip" in hk:
                a.hk_snip = hk["snip"]
            if "settings" in hk:
                a.hk_settings = hk["settings"]
            a.show_translation = a.translator != "none"
        except (FileNotFoundError, json.JSONDecodeError):
            pass

    def save(self):
        a = self.app
        data = {
            "show_crop": a.show_crop,
            "show_ocr_text": a.show_ocr_text,
            "show_furigana": a.show_furigana,
            "show_romaji": a.show_romaji,
            "skip_non_japanese": a.skip_non_japanese,
            "translator": a.translator,
            "dictionary_type": a.dictionary_type,
            "region_detect_scale": a.region_detect_scale,
            "max_dict_entries": a.max_dict_entries,
            "max_dict_senses": a.max_dict_senses,
            "show_in_region_translation": a.show_in_region_translation,
            "skip_numeric_only": a.skip_numeric_only,
            "in_region_auto_threshold": a.in_region_auto_threshold,
            "hotkeys": {
                "capture": a.hk_capture,
                "snip": a.hk_snip,
                "settings": a.hk_settings,
            },
        }
        with open(self._file, "w") as f:
            json.dump(data, f)

    # ── Toggle window ────────────────────────────────────────────────────

    def toggle(self):
        if not self.window or not self.window.winfo_exists():
            self._build_window()
        if self.window.winfo_viewable():
            self._cancel_recording()
            self.window.withdraw()
        else:
            self._sync_from_app()
            self._start_refresh_timer()
            self.window.deiconify()
            self.window.lift()
            self.window.focus_force()

    # ── UI helpers ───────────────────────────────────────────────────────

    def _card(self, win):
        """White card container with border."""
        card = ctk.CTkFrame(win, fg_color=_CARD, border_color=_BORDER,
                            border_width=1, corner_radius=8)
        card.pack(fill="x", padx=8, pady=6, ipady=6)
        return card

    def _section_label(self, card, text):
        """Section header inside a card."""
        ctk.CTkLabel(card, text=text, font=("Segoe UI", 13, "bold"),
                     anchor="w", text_color=_TEXT).pack(fill="x", padx=14, pady=(6, 4))

    def _row(self, card):
        """Transparent row inside a card."""
        f = ctk.CTkFrame(card, fg_color="transparent")
        f.pack(fill="x", padx=14, pady=1)
        return f

    def _field(self, parent, text):
        ctk.CTkLabel(parent, text=text, anchor="w", width=160,
                     font=("Segoe UI", 12), text_color=_TEXT).pack(side="left", padx=(4, 0))

    def _chk(self, parent, text, initial, cmd):
        var = ctk.BooleanVar(value=initial)
        ctk.CTkCheckBox(parent, text=text, variable=var, onvalue=True, offvalue=False,
                        command=cmd, font=("Segoe UI", 11), height=20,
                        fg_color=_ACCENT, hover_color=_ACCENT_HOVER,
                        text_color=_TEXT).pack(anchor="w", padx=(4, 0), pady=2)
        return var

    def _menu(self, parent, values, initial, cmd):
        m = ctk.CTkOptionMenu(parent, values=values, command=cmd,
                              width=130, font=("Segoe UI", 12),
                              fg_color=_ACCENT, button_color=_ACCENT,
                              button_hover_color=_ACCENT_HOVER,
                              dropdown_fg_color=_CARD,
                              dropdown_hover_color=_ACCENT,
                              text_color="#FFFFFF")
        m.set(initial)
        m.pack(side="left")
        return m

    def _slider_row(self, card, label_text, value, callback):
        f = self._row(card)
        self._field(f, label_text)
        lbl = ctk.CTkLabel(f, text=self._slider_disp(value), font=("Segoe UI", 12, "bold"),
                           width=80, text_color=_ACCENT, anchor="w")
        lbl.pack(side="right")
        sl = ctk.CTkSlider(f, from_=1, to=10, number_of_steps=9,
                           command=callback, width=160,
                           button_color=_ACCENT, button_hover_color=_ACCENT_HOVER,
                           progress_color=_ACCENT)
        sl.set(value)
        sl.pack(side="left")
        return sl, lbl

    @staticmethod
    def _val_to_slider(v):
        return 10 if v >= 1000 else v

    @staticmethod
    def _slider_to_val(v):
        return 1000 if v >= 10 else int(round(v))

    @staticmethod
    def _slider_disp(v):
        return "No limit" if v >= 10 else str(int(round(v)))

    @staticmethod
    def _threshold_val_to_slider(v):
        if v <= 0: return 1
        return v - 1

    @staticmethod
    def _threshold_slider_to_val(s):
        s = int(round(s))
        if s <= 1: return 0
        return s + 1

    @staticmethod
    def _threshold_disp(v):
        if v <= 0: return "Off"
        return str(v) + "+"

    # ── Build UI ─────────────────────────────────────────────────────────

    def _setup_ui(self, win):
        a = self.app

        def on_toggle(*_):
            a.show_crop = self._show_crop_var.get()
            a.show_ocr_text = self._show_ocr_var.get()
            if not a.show_ocr_text:
                self._show_furigana_var.set(False)
            a.show_furigana = self._show_furigana_var.get()
            a.show_romaji = self._show_romaji_var.get()
            a.skip_non_japanese = self._skip_nj_var.get()
            a.skip_numeric_only = self._skip_num_var.get()
            old_in_region = a.show_in_region_translation
            a.show_in_region_translation = self._show_in_region_var.get()
            if a.show_in_region_translation and a.in_region_auto_threshold > 0:
                a.in_region_auto_threshold = 0
                self._threshold_slider.set(self._threshold_val_to_slider(0))
                self._threshold_lbl.configure(text=self._threshold_disp(0))
            self.save()
            a._refresh_hover_card()
            if old_in_region != a.show_in_region_translation:
                a._refresh_in_region_translations()

        # ── Hover Card ───────────────────────────────────────────────
        card = self._card(win)
        self._section_label(card, "Hover Card")
        self._show_ocr_var = self._chk(self._row(card), "Show OCR text", a.show_ocr_text, on_toggle)
        self._show_furigana_var = self._chk(self._row(card), "Show furigana", a.show_furigana, on_toggle)
        self._show_romaji_var = self._chk(self._row(card), "Show romaji", a.show_romaji, on_toggle)

        # ── Translation & Dictionary ──────────────────────────────────
        card = self._card(win)
        self._section_label(card, "Translation & Dictionary")
        r = self._row(card)
        self._field(r, "Service:")
        self._translator_menu = self._menu(
            r, ["DeepL", "Google", "None"],
            "DeepL" if a.translator == "deepl" else "Google" if a.translator == "google" else "None",
            lambda val: self._on_translator(val))

        r = self._row(card)
        self._field(r, "Dictionary:")
        self._dict_type_menu = self._menu(
            r, ["JA-EN", "JA-JA"],
            "JA-EN" if a.dictionary_type == "English" else "JA-JA",
            lambda val: self._on_dict_type(val))

        def on_entries_slider(v):
            val = self._slider_to_val(float(v))
            a.max_dict_entries = val
            self._entries_lbl.configure(text=self._slider_disp(val))
            self.save()

        def on_senses_slider(v):
            val = self._slider_to_val(float(v))
            a.max_dict_senses = val
            self._senses_lbl.configure(text=self._slider_disp(val))
            self.save()

        self._entries_slider, self._entries_lbl = self._slider_row(
            card, "Max entries:", self._val_to_slider(a.max_dict_entries), on_entries_slider)
        self._senses_slider, self._senses_lbl = self._slider_row(
            card, "Max senses/entry:", self._val_to_slider(a.max_dict_senses), on_senses_slider)

        # ── OCR Filter ───────────────────────────────────────────────
        card = self._card(win)
        self._section_label(card, "OCR Filter")
        self._skip_nj_var = self._chk(self._row(card), "Skip non-Japanese OCR regions",
                                       a.skip_non_japanese, on_toggle)
        self._skip_num_var = self._chk(self._row(card), "Skip numeric-only OCR regions",
                                        a.skip_numeric_only, on_toggle)
        self._show_in_region_var = self._chk(self._row(card), "Show translation in OCR region",
                                              a.show_in_region_translation, on_toggle)

        def _on_threshold(v):
            val = self._threshold_slider_to_val(float(v))
            a.in_region_auto_threshold = val
            self._threshold_lbl.configure(text=self._threshold_disp(val))
            if val > 0:
                self._show_in_region_var.set(False)
                a.show_in_region_translation = False
            self.save()
            a._refresh_in_region_translations()
            a._refresh_hover_card()

        f = self._row(card)
        self._field(f, "Auto in-region at ≥")
        self._threshold_lbl = ctk.CTkLabel(f, text=self._threshold_disp(a.in_region_auto_threshold),
                                           font=("Segoe UI", 12, "bold"),
                                           width=50, text_color=_ACCENT, anchor="w")
        self._threshold_lbl.pack(side="right")
        self._threshold_slider = ctk.CTkSlider(f, from_=1, to=14, number_of_steps=13,
                                               command=_on_threshold, width=160,
                                               button_color=_ACCENT, button_hover_color=_ACCENT_HOVER,
                                               progress_color=_ACCENT)
        self._threshold_slider.set(self._threshold_val_to_slider(a.in_region_auto_threshold))
        self._threshold_slider.pack(side="left")

        # ── OCR Scaling ──────────────────────────────────────────────
        card = self._card(win)
        self._section_label(card, "OCR Scaling")
        opts = {"25%": 25, "50%": 50, "75%": 75, "100%": 100}
        rev = {v: k for k, v in opts.items()}
        r = self._row(card)
        self._field(r, "OCR Prepass Scale:")
        self._scale_menu = self._menu(r, list(opts.keys()), rev[a.region_detect_scale],
                                      lambda val: self._on_scale(val, opts))

        # ── Hotkeys ──────────────────────────────────────────────────
        card = self._card(win)
        self._section_label(card, "Hotkeys")
        for action, hk, label in [
            ("capture", a.hk_capture, "Capture window:"),
            ("snip", a.hk_snip, "Capture region:"),
            ("settings", a.hk_settings, "Open settings:"),
        ]:
            r = self._row(card)
            self._field(r, label)
            btn = ctk.CTkButton(r, text=hk["display"], width=180, font=("Segoe UI", 12),
                                fg_color=_ACCENT, hover_color=_ACCENT_HOVER,
                                text_color="#FFFFFF")
            btn.configure(command=lambda a=action, b=btn: self._start_recording(a, b))
            btn.pack(side="left")
            ctk.CTkLabel(r, text="(Esc to reset)", font=("Segoe UI", 10),
                         text_color=_TEXT2).pack(side="left", padx=(8, 0))
            self.hk_btns[action] = btn

        # ── Debug ────────────────────────────────────────────────────
        card = self._card(win)
        self._section_label(card, "Debug")
        self._show_crop_var = self._chk(self._row(card), "Show cropped image", a.show_crop, on_toggle)

        r = self._row(card)
        ctk.CTkButton(r, text="Purge translation caches", command=lambda: self._on_purge(win),
                      font=("Segoe UI", 11), width=180, height=30,
                      fg_color="transparent", border_color=_ACCENT, border_width=1,
                      text_color=_ACCENT, hover_color="#F0F4F8").pack(side="left")
        self._entries_lbl_dbg = ctk.CTkLabel(r, text=f"Entries: {self._count_cache_entries()}",
                                             font=("Segoe UI", 11), text_color=_TEXT2)
        self._entries_lbl_dbg.pack(side="left", padx=12)

        win.update_idletasks()
        win.geometry(f"{win.winfo_reqwidth()}x{win.winfo_reqheight()}")

    # ── UI callbacks ─────────────────────────────────────────────────────

    def _on_translator(self, val):
        a = self.app
        internal = "deepl" if val == "DeepL" else "google" if val == "Google" else "none"
        if internal != a.translator:
            old_translation = a.show_translation
            a.show_translation = internal != "none"
            a._switch_translator(internal)
            self.save()
            if old_translation != a.show_translation:
                if a.show_translation:
                    a._retranslate_boxes()
                else:
                    for box in a.ocr_boxes:
                        box['data']['english'] = ""
                    a._refresh_hover_card()

    def _on_dict_type(self, val):
        a = self.app
        internal = "English" if val == "JA-EN" else "Monolingual"
        if internal != a.dictionary_type:
            a.dictionary_type = internal
            self.save()
            if not a._check_dict_files():
                self._dict_type_menu.set("JA-EN" if a.dictionary_type == "English" else "JA-JA")
            a._retranslate_boxes()

    def _on_scale(self, val, opts):
        self.app.region_detect_scale = opts[val]
        self.save()

    def _on_purge(self, win):
        self.app._purge_caches()
        self._entries_lbl_dbg.configure(text=f"Entries: {self._count_cache_entries()}")

    @staticmethod
    def _count_cache_entries():
        count = 0
        for service in ["deepl", "google"]:
            path = os.path.join(_PROJECT_DIR, "Data", f"translation_cache_{service}.json")
            if os.path.exists(path):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        count += len(json.load(f))
                except Exception:
                    pass
        return count

    # ── Hotkey defaults ──────────────────────────────────────────────────

    def _hk_defaults(self):
        return {
            "capture": {"mod": MOD_CONTROL | MOD_ALT | MOD_SHIFT | MOD_NOREPEAT, "vk": ord('E')},
            "snip": {"mod": MOD_CONTROL | MOD_ALT | MOD_SHIFT | MOD_NOREPEAT, "vk": ord('R')},
            "settings": {"mod": MOD_CONTROL | MOD_ALT | MOD_SHIFT | MOD_NOREPEAT, "vk": ord('S')},
        }

    def _reset_hk_to_default(self, action):
        hk = dict(self._hk_defaults()[action])
        hk["display"] = _hk_display(hk)
        setattr(self.app, f"hk_{action}", hk)
        btn = self.hk_btns.get(action)
        if btn and btn.winfo_exists():
            btn.configure(text=hk["display"])

    # ── Hotkey recording ─────────────────────────────────────────────────

    def _start_recording(self, action, btn):
        a = self.app
        if self.recording_action and self.recording_action != action and self.recording_prev is not None:
            setattr(a, f"hk_{self.recording_action}", self.recording_prev)
            if self.recording_btn and self.recording_btn.winfo_exists():
                self.recording_btn.configure(text=self.recording_prev["display"])
        self.recording_action = action
        self.recording_btn = btn
        self.recording_pending = None
        self.recording_prev = getattr(a, f"hk_{action}")
        btn.configure(text="\u2026")
        a.root.after(50, self._poll_recording)

    def _poll_recording(self):
        if not self.recording_action:
            return

        is_down = lambda vk: bool(user32.GetAsyncKeyState(vk) & 0x8000)
        a = self.app

        if is_down(27):
            hk = dict(self._hk_defaults()[self.recording_action])
            hk["display"] = _hk_display(hk)
            self._finish_recording(hk)
            return

        for vk in list(range(0x30, 0x3A)) + list(range(0x41, 0x5B)) + list(range(0x70, 0x7C)):
            if not is_down(vk):
                continue
            if self.recording_pending is not None:
                break

            ctrl_down = is_down(VK_LCTRL) or is_down(VK_RCTRL)
            alt_down = is_down(VK_LALT) or is_down(VK_RALT)
            shift_down = is_down(VK_LSHIFT) or is_down(VK_RSHIFT)

            modifiers = MOD_NOREPEAT
            mod_parts = []
            if ctrl_down:
                modifiers |= MOD_CONTROL
                mod_parts.append("Ctrl")
            if alt_down:
                modifiers |= MOD_ALT
                mod_parts.append("Alt")
            if shift_down:
                modifiers |= MOD_SHIFT
                mod_parts.append("Shift")

            mod_parts.append(_vk_to_display(vk))

            for other in ("capture", "snip", "settings"):
                if other == self.recording_action:
                    continue
                ohk = getattr(a, f"hk_{other}")
                if ohk["vk"] == vk and (ohk["mod"] & ~MOD_NOREPEAT) == (modifiers & ~MOD_NOREPEAT):
                    self._reset_hk_to_default(other)
                    hk = dict(self._hk_defaults()[self.recording_action])
                    hk["display"] = _hk_display(hk)
                    self._reset_hk_to_default(self.recording_action)
                    a._hk_dirty.set()
                    self.save()
                    self._finish_recording(hk)
                    return

            self.recording_pending = {"mod": modifiers, "vk": vk, "display": "+".join(mod_parts)}
            break

        if self.recording_pending is not None:
            hk = self.recording_pending
            if not is_down(hk["vk"]):
                mods_down = (is_down(VK_LCTRL) or is_down(VK_RCTRL)
                             or is_down(VK_LALT) or is_down(VK_RALT)
                             or is_down(VK_LSHIFT) or is_down(VK_RSHIFT))
                if not mods_down:
                    self._finish_recording(hk)
                    return

        a.root.after(50, self._poll_recording)

    def _finish_recording(self, hk):
        a = self.app
        setattr(a, f"hk_{self.recording_action}", hk)
        self.recording_btn.configure(text=hk["display"])
        self.recording_action = None
        self.recording_btn = None
        self.recording_pending = None
        self.recording_prev = None
        a._hk_dirty.set()
        self.save()
        self.hk_suppress_until = time.time() + 0.3
        self._wait_keys_up()

    def _wait_keys_up(self):
        if time.time() >= self.hk_suppress_until:
            is_down = lambda vk: bool(user32.GetAsyncKeyState(vk) & 0x8000)
            still_held = any(
                is_down(vk) for vk in
                list(range(0x30, 0x3A)) + list(range(0x41, 0x5B)) + list(range(0x70, 0x7C))
                + [VK_LCTRL, VK_RCTRL, VK_LALT, VK_RALT, VK_LSHIFT, VK_RSHIFT]
            )
            if not still_held:
                self.hk_suppress_until = 0.0
                return
        self.app.root.after(50, self._wait_keys_up)

    def _cancel_recording(self):
        if not self.recording_action:
            return
        self.recording_action = None
        self.recording_btn = None
        self.recording_pending = None
        self.recording_prev = None
