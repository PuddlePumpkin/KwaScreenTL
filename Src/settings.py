import os
import json
import time
import tkinter as tk
import tkinter.messagebox as tkmb

from hotkeys import (
    MOD_CONTROL, MOD_ALT, MOD_SHIFT, MOD_NOREPEAT,
    VK_LCTRL, VK_RCTRL, VK_LALT, VK_RALT, VK_LSHIFT, VK_RSHIFT,
    _hk_display, _vk_to_display,
    user32,
)


_PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.makedirs(os.path.join(_PROJECT_DIR, "Data"), exist_ok=True)

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

    # ── Persistence ──────────────────────────────────────────────────────

    def load(self):
        try:
            with open(self._file, "r") as f:
                data = json.load(f)
            a = self.app
            a.show_crop = data.get("show_crop", True)
            a.show_romaji = data.get("show_romaji", True)
            a.skip_non_japanese = data.get("skip_non_japanese", a.skip_non_japanese)
            a.show_translation = data.get("show_translation", True)
            a.translator = data.get("translator", a.translator)
            a.dictionary_type = data.get("dictionary_type", "English")
            a.region_detect_scale = data.get("region_detect_scale", 100)
            hk = data.get("hotkeys", {})
            if "capture" in hk:
                a.hk_capture = hk["capture"]
            if "snip" in hk:
                a.hk_snip = hk["snip"]
            if "settings" in hk:
                a.hk_settings = hk["settings"]
        except (FileNotFoundError, json.JSONDecodeError):
            pass

    def save(self):
        a = self.app
        data = {
            "show_crop": a.show_crop,
            "show_romaji": a.show_romaji,
            "skip_non_japanese": a.skip_non_japanese,
                "show_translation": a.show_translation,
                "translator": a.translator,
                "dictionary_type": a.dictionary_type,
                "region_detect_scale": a.region_detect_scale,
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
        if self.window and self.window.winfo_exists():
            self._cancel_recording()
            self.hk_btns.clear()
            self.window.destroy()
            self.window = None
            return

        a = self.app
        win = tk.Toplevel(a.root)
        win.withdraw()
        win.title("Settings")
        win.resizable(False, False)
        win.attributes("-topmost", True)
        icon_path = os.path.join(_PROJECT_DIR, "Launcher", "AppIcon.ico")
        if os.path.exists(icon_path):
            try:
                win.iconbitmap(icon_path)
            except Exception:
                pass
        self.window = win
        self._setup_ui(win)
        win.deiconify()

    def _setup_ui(self, win):
        a = self.app

        self._show_crop_var = tk.BooleanVar(value=a.show_crop)
        self._show_romaji_var = tk.BooleanVar(value=a.show_romaji)
        self._skip_nj_var = tk.BooleanVar(value=a.skip_non_japanese)
        self._show_translation_var = tk.BooleanVar(value=a.show_translation)

        def on_toggle():
            old_translation = a.show_translation
            a.show_crop = self._show_crop_var.get()
            a.show_romaji = self._show_romaji_var.get()
            a.skip_non_japanese = self._skip_nj_var.get()
            
            new_show_trans = self._show_translation_var.get()
            if new_show_trans and a.translator == "none":
                a.translator = "google"
                self._translator_var.set("Google")
                a._switch_translator("google")
            
            a.show_translation = new_show_trans
            self.save()
            
            if old_translation != a.show_translation:
                if a.show_translation:
                    a._retranslate_boxes()
                else:
                    for box in a.ocr_boxes:
                        box['data']['english'] = ""
                    a._refresh_hover_card()
            else:
                a._refresh_hover_card()

        pad = {"padx": 12, "pady": 3}

        tk.Label(win, text="Hover Card", font=("Segoe UI", 9, "bold"),
                 anchor="w").pack(fill="x", padx=12, pady=(10, 2))
        sep = tk.Frame(win, height=1, bg="#c0c0c0")
        sep.pack(fill="x", padx=12)
        tk.Checkbutton(win, text="Show romaji", variable=self._show_romaji_var,
                       command=on_toggle).pack(anchor="w", **pad)
        tk.Checkbutton(win, text="Show translation", variable=self._show_translation_var,
                       command=on_toggle).pack(anchor="w", **pad)

        tk.Label(win, text="Translation & Dictionary", font=("Segoe UI", 9, "bold"),
                 anchor="w").pack(fill="x", padx=12, pady=(10, 2))
        sep_t = tk.Frame(win, height=1, bg="#c0c0c0")
        sep_t.pack(fill="x", padx=12)

        self._translator_var = tk.StringVar(value="DeepL" if a.translator == "deepl" else "Google" if a.translator == "google" else "None")

        def on_translator_change(*_):
            val = self._translator_var.get()
            internal_val = "deepl" if val == "DeepL" else "google" if val == "Google" else "none"
            if internal_val != a.translator:
                a._switch_translator(internal_val)
                self.save()
                if internal_val == "none":
                    self._show_translation_var.set(False)
                    on_toggle()

        self._translator_var.trace_add("write", on_translator_change)

        f_trans = tk.Frame(win)
        f_trans.pack(fill="x", padx=12, pady=3)
        tk.Label(f_trans, text="Service:", anchor="w", width=20).pack(side="left")
        tk.OptionMenu(f_trans, self._translator_var, "DeepL", "Google", "None").pack(side="left")

        f_dict = tk.Frame(win)
        f_dict.pack(fill="x", padx=12, pady=3)
        tk.Label(f_dict, text="Dictionary:", anchor="w", width=20).pack(side="left")

        self._dict_type_var = tk.StringVar(value="JA-EN" if a.dictionary_type == "English" else "JA-JA")
        def on_dict_type_change(*_):
            val = self._dict_type_var.get()
            internal_val = "English" if val == "JA-EN" else "Monolingual"
            if internal_val != a.dictionary_type:
                a.dictionary_type = internal_val
                self.save()
                if not a._check_dict_files():
                    self._dict_type_var.set("JA-EN" if a.dictionary_type == "English" else "JA-JA")
                a._retranslate_boxes()
        self._dict_type_var.trace_add("write", on_dict_type_change)
        tk.OptionMenu(f_dict, self._dict_type_var, "JA-EN", "JA-JA").pack(side="left")
 
        tk.Label(win, text="OCR Filter", font=("Segoe UI", 9, "bold"),
                anchor="w").pack(fill="x", padx=12, pady=(10, 2))
        sep2 = tk.Frame(win, height=1, bg="#c0c0c0")
        sep2.pack(fill="x", padx=12)
        tk.Checkbutton(win, text="Skip non-Japanese text", variable=self._skip_nj_var,
                       command=on_toggle).pack(anchor="w", **pad)

        tk.Label(win, text="OCR Scaling", font=("Segoe UI", 9, "bold"),
                 anchor="w").pack(fill="x", padx=12, pady=(10, 2))
        sep3 = tk.Frame(win, height=1, bg="#c0c0c0")
        sep3.pack(fill="x", padx=12)

        opts = {"25%": 25, "50%": 50, "75%": 75, "100%": 100}

        def opt_label(v):
            return next(k for k, val in opts.items() if val == v)

        self._region_detect_var = tk.StringVar(value=opt_label(a.region_detect_scale))

        def on_scale_change(*_):
            val = opts[self._region_detect_var.get()]
            a.region_detect_scale = val
            self.save()

        self._region_detect_var.trace_add("write", on_scale_change)

        f1 = tk.Frame(win)
        f1.pack(fill="x", padx=12, pady=3)
        tk.Label(f1, text="OCR Prepass Scale:", anchor="w", width=20).pack(side="left")
        tk.OptionMenu(f1, self._region_detect_var, *opts.keys()).pack(side="left")

        # ── Hotkeys section ──────────────────────────────────────────────

        tk.Label(win, text="Hotkeys", font=("Segoe UI", 9, "bold"),
                 anchor="w").pack(fill="x", padx=12, pady=(10, 2))
        sep_hk = tk.Frame(win, height=1, bg="#c0c0c0")
        sep_hk.pack(fill="x", padx=12)

        def make_rec_btn(action, hk):
            f = tk.Frame(win)
            f.pack(fill="x", padx=12, pady=2)
            labels = {"capture": "Capture window:", "snip": "Capture region:", "settings": "Open settings:"}
            tk.Label(f, text=labels[action], anchor="w", width=20).pack(side="left")
            btn = tk.Button(f, text=hk["display"], width=20,
                            command=lambda a=action: self._start_recording(a, btn))
            btn.pack(side="left", padx=(0, 4))
            tk.Label(f, text="(Esc to reset)", font=("Segoe UI", 7), fg="gray").pack(side="left")
            self.hk_btns[action] = btn

        make_rec_btn("capture", a.hk_capture)
        make_rec_btn("snip", a.hk_snip)
        make_rec_btn("settings", a.hk_settings)

        # ── Debug section ────────────────────────────────────────────────

        tk.Label(win, text="Debug", font=("Segoe UI", 9, "bold"),
                 anchor="w").pack(fill="x", padx=12, pady=(10, 2))
        sep4 = tk.Frame(win, height=1, bg="#c0c0c0")
        sep4.pack(fill="x", padx=12)
        tk.Checkbutton(win, text="Show cropped image", variable=self._show_crop_var,
                       command=on_toggle).pack(anchor="w", **pad)

        tk.Label(win, text="", font=("Segoe UI", 3)).pack()
        
        def get_total_entries():
            count = 0
            for service in ["deepl", "google"]:
                path = os.path.join(_PROJECT_DIR, "Data", f"translation_cache_{service}.json")
                if os.path.exists(path):
                    try:
                        with open(path, "r", encoding="utf-8") as f:
                            data = json.load(f)
                            count += len(data)
                    except Exception:
                        pass
            return count

        def on_purge():
            a._purge_caches()
            lbl_entries.config(text=f"Entries: {get_total_entries()}")

        f_purge = tk.Frame(win)
        f_purge.pack(fill="x", padx=12, pady=3)
        tk.Button(f_purge, text="Purge translation caches",
                  command=on_purge,
                  font=("Segoe UI", 8)).pack(side="left")
        
        lbl_entries = tk.Label(f_purge, text=f"Entries: {get_total_entries()}", 
                               font=("Segoe UI", 8), fg="gray")
        lbl_entries.pack(side="left", padx=10)

        def refresh_count():
            if win.winfo_exists():
                lbl_entries.config(text=f"Entries: {get_total_entries()}")
                win.after(5000, refresh_count)
        win.after(5000, refresh_count)

        win.update_idletasks()
        win.geometry(f"{win.winfo_reqwidth()}x{win.winfo_reqheight()}")

        win.protocol("WM_DELETE_WINDOW", lambda: (
            self._cancel_recording(), self.hk_btns.clear(),
            setattr(self, 'window', None), win.destroy()
        ))

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
            btn.config(text=hk["display"])

    # ── Hotkey recording ─────────────────────────────────────────────────

    def _start_recording(self, action, btn):
        a = self.app
        if self.recording_action and self.recording_action != action and self.recording_prev is not None:
            setattr(a, f"hk_{self.recording_action}", self.recording_prev)
            if self.recording_btn and self.recording_btn.winfo_exists():
                self.recording_btn.config(text=self.recording_prev["display"])
        self.recording_action = action
        self.recording_btn = btn
        self.recording_pending = None
        self.recording_prev = getattr(a, f"hk_{action}")
        btn.config(text="\u2026")
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
        self.recording_btn.config(text=hk["display"])
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
