import ctypes
import ctypes.wintypes
import json
import os
import queue
import re
import threading
import time

# PaddlePaddle 3.3 PIR compatibility workaround — must be set before any
# paddle/paddlex import, so it goes right at the top.
os.environ["FLAGS_enable_pir_with_executor_in_serial_mode"] = "0"

# ── OCR engine selector ──────────────────────────────────────────────────────
# "windows" → WinOCR (Windows.Media.Ocr, fast but less accurate)
# "paddle"  → PaddleOCR (deep-learning, more accurate, ~5s first-load)
OCR_ENGINE = "paddle"
# ─────────────────────────────────────────────────────────────────────────────

# ── Translation backend ──────────────────────────────────────────────────────
# "google"  → Google Translate (online, free, no key needed)
# "deepl"   → DeepL API  (online, needs key in deeplapikey.txt)
TRANSLATOR = "deepl"
# ─────────────────────────────────────────────────────────────────────────────

# ── OCR upscale factors (multi-scale fusion) ──────────────────────────────────
# Multiple scales are tried and results merged.  Lower scales catch large text
# with good word grouping; higher scales catch small/corner text.
# Tweak these to balance speed vs coverage.
OCR_SCALES = [5, 7]

# ── Snip-mode OCR scale (Ctrl+Alt+Shift+R) ──────────────────────────────────
# Scale used when drag-selecting a custom region.  Can be different from
# OCR_SCALES since snips are often smaller areas (less memory pressure).
SNIP_OCR_SCALE = 5

# ── Language filter ─────────────────────────────────────────────────────────
# When True, non-Japanese OCR text (numbers, English UI) is skipped.
# Set to False to show everything (useful for debugging OCR coverage).
SKIP_NON_JAPANESE = False
# ─────────────────────────────────────────────────────────────────────────────

# ── Window capture crop (px to trim from captured window edges) ──────────────
CROP_TOP = 30
CROP_BOTTOM = 10
CROP_LEFT = 10
CROP_RIGHT = 10

FONT_CHOICES = ["Yu Gothic UI", "Yu Gothic", "Meiryo", "MS Gothic", "MS Mincho", "Segoe UI"]
DEFAULT_FONT = "Yu Gothic UI"
FONT_SIZES = [16, 18, 20, 22, 24, 26, 28, 32]
DEFAULT_FONT_SIZE = 22

import tkinter as tk
import tkinter.font as tkfont
from PIL import Image, ImageTk
import mss
import onnxruntime  # must precede winocr to avoid WinRT DLL conflict
import winocr
import jaconv
import pykakasi
from jamdict import Jamdict
from sudachipy import Dictionary, SplitMode
from deep_translator import GoogleTranslator
from concurrent.futures import ThreadPoolExecutor
import webbrowser
import urllib.parse

# ── DeepL API key loader ───────────────────────────────────────────────────
_deepl_api_key = None

def get_deepl_api_key():
    global _deepl_api_key
    if _deepl_api_key is None:
        key_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "deeplapikey.txt")
        try:
            with open(key_path, "r") as f:
                _deepl_api_key = f.read().strip()
        except Exception as e:
            print(f"Failed to read DeepL API key from {key_path}: {e}")
            _deepl_api_key = ""
    return _deepl_api_key

# ── PaddleOCR lazy loader ──────────────────────────────────────────────────
_paddle_ocr = None

def get_paddle_ocr():
    global _paddle_ocr
    if _paddle_ocr is None:
        from paddleocr import PaddleOCR
        print("[PaddleOCR] Loading models... (first load may download ~200MB)")
        _paddle_ocr = PaddleOCR(
            lang='japan', engine='onnxruntime',
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
            return_word_box=True,
        )
        print("[PaddleOCR] Ready.")
    return _paddle_ocr

def translate_deepl(text):
    import requests
    key = get_deepl_api_key()
    if not key:
        return "[DeepL: no API key - add to deeplapikey.txt]"
    resp = requests.post(
        "https://api-free.deepl.com/v2/translate",
        headers={"Authorization": f"DeepL-Auth-Key {key}"},
        json={"text": [text], "source_lang": "JA", "target_lang": "EN"}
    )
    if resp.status_code == 403:
        return "[DeepL: Unauthorized - check your API key]"
    resp.raise_for_status()
    return resp.json()["translations"][0]["text"]

# Win32 structures for mouse cursor position
class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

def get_mouse_pos():
    pt = POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y

def capture_moused_monitor():
    mx, my = get_mouse_pos()
    with mss.MSS() as sct:
        # sct.monitors[0] is the virtual screen spanning all monitors
        # sct.monitors[1:] are the individual physical monitors
        for monitor in sct.monitors[1:]:
            left = monitor["left"]
            top = monitor["top"]
            width = monitor["width"]
            height = monitor["height"]
            if left <= mx < left + width and top <= my < top + height:
                return sct.grab(monitor), monitor
        return sct.grab(sct.monitors[1]), sct.monitors[1]

# SudachiPy's Rust tokenizer is NOT thread-safe → use thread-local instances
_sudachi_local = threading.local()
def _get_sudachi():
    if not hasattr(_sudachi_local, 'tokenizer'):
        _sudachi_local.tokenizer = Dictionary().create()
    return _sudachi_local.tokenizer

kks = pykakasi.kakasi()

# Jamdict's SQLite connection is NOT thread-safe → use thread-local instances
_jam_local = threading.local()
def _get_jam():
    if not hasattr(_jam_local, 'jam'):
        _jam_local.jam = Jamdict()
    return _jam_local.jam

def contains_japanese(text):
    """Check if the text contains any Japanese characters (Hiragana, Katakana, Kanji)."""
    # Unicode ranges:
    # Hiragana: \u3040-\u309f
    # Katakana: \u30a0-\u30ff
    # Kanji (CJK Unified Ideographs): \u4e00-\u9faf
    pattern = re.compile(r'[\u3040-\u309f\u30a0-\u30ff\u4e00-\u9faf]')
    return bool(pattern.search(text))

def _is_kanji(ch):
    """Check if a character is a CJK ideograph."""
    return '\u4e00' <= ch <= '\u9faf'

def _build_alternatives(orig, sudachi_hira):
    """Build list of alternative readings for a token using jamdict."""
    alts = []
    seen = set()
    def _add(h, rtype='unknown'):
        if h in seen:
            return
        seen.add(h)
        h_hepburn = " ".join([r['hepburn'] for r in kks.convert(h)])
        alts.append({'hira': h, 'hepburn': h_hepburn, 'type': rtype})

    _add(sudachi_hira)

    # pykakasi reading (dictionary-based fallback)
    try:
        pk = kks.convert(orig)
        pk_hira = " ".join([i['hira'] if i['hira'] else i['orig'] for i in pk])
        pk_hira_norm = pk_hira.replace(" ", "")
        if pk_hira_norm != sudachi_hira:
            _add(pk_hira_norm)
    except Exception:
        pass

    # Jamdict lookups
    if any(_is_kanji(c) for c in orig):
        try:
            jam_result = _get_jam().lookup(orig)

            # 1. JMDict whole-word readings (type=unknown)
            for entry in jam_result.entries:
                for kf in entry.kana_forms:
                    r = jaconv.kata2hira(kf.text) if kf.text else ''
                    if r:
                        _add(r)

            # 2. KanjiDic2 per-character readings (type=on/kun/nanori)
            for ch in jam_result.chars:
                lit = ch.literal
                if lit is None or lit not in orig:
                    continue
                for g in ch.rm_groups:
                    for r in g.on_readings:
                        _add(jaconv.kata2hira(str(r)), 'on')
                    for r in g.kun_readings:
                        clean = jaconv.kata2hira(str(r)).split('.')[0].replace('-', '')
                        if clean:
                            _add(clean, 'kun')
                if hasattr(ch, 'nanoris') and ch.nanoris:
                    for n in ch.nanoris:
                        _add(jaconv.kata2hira(str(n)), 'nanori')
        except Exception:
            pass

    # Context-based filter: compound (jukugo) → on'yomi, standalone → kun'yomi
    # XXX: May need to revert this filtering if it causes too many missing readings
    kanji_count = sum(1 for c in orig if _is_kanji(c))
    default = alts[0]
    if kanji_count > 1:
        alts = [a for a in alts if a['type'] in ('on', 'unknown')]
    elif kanji_count == 1:
        alts = [a for a in alts if a['type'] in ('kun', 'nanori', 'unknown')]
    if default not in alts:
        alts.insert(0, default)

    return alts

def translate_and_convert(japanese_text):
    """Convert Japanese to Romaji, Hiragana, and English translation."""
    print(f"[TRANSLATE] input='{japanese_text}'")
    try:
        # Use SudachiPy (Mode C = longest natural chunks) for context-aware readings
        tokens = _get_sudachi().tokenize(japanese_text, SplitMode.C)
        items = []
        for token in tokens:
            orig = token.surface()
            reading = token.reading_form()
            hira = jaconv.kata2hira(reading) if reading else orig
            alternatives = _build_alternatives(orig, hira)
            items.append({
                'orig': orig,
                'hira': alternatives[0]['hira'],
                'hepburn': alternatives[0]['hepburn'],
                'alternatives': alternatives,
                'active_idx': 0,
            })
        romaji = " ".join([item['hepburn'] for item in items])
        kana = " ".join([item['hira'] if item['hira'] else item['orig'] for item in items])
        
        # Translate to English
        if TRANSLATOR == "deepl":
            english = translate_deepl(japanese_text)
        else:
            english = GoogleTranslator(source='ja', target='en').translate(japanese_text)
    except Exception as e:
        import traceback
        traceback.print_exc()
        romaji = "[Error]"
        kana = japanese_text
        english = f"Translation error: {e}"
        items = []
        
    return {
        'original': japanese_text,
        'romaji': romaji,
        'kana': kana,
        'english': english,
        'kakasi_items': items,
    }

def get_line_bounding_rect(line):
    """Calculate the bounding rectangle of a line based on its words."""
    words = line.get('words', [])
    if not words:
        return {'x': 0, 'y': 0, 'width': 0, 'height': 0}
    
    xs = [w['bounding_rect']['x'] for w in words]
    ys = [w['bounding_rect']['y'] for w in words]
    rights = [w['bounding_rect']['x'] + w['bounding_rect']['width'] for w in words]
    bottoms = [w['bounding_rect']['y'] + w['bounding_rect']['height'] for w in words]
    
    min_x = min(xs)
    min_y = min(ys)
    max_right = max(rights)
    max_bottom = max(bottoms)
    
    return {
        'x': min_x,
        'y': min_y,
        'width': max_right - min_x,
        'height': max_bottom - min_y
    }

class ScreenFreezerApp:
    def __init__(self):
        self.root = tk.Tk()
        self.root.withdraw()  # Hide root window
        self.msg_queue = queue.Queue()
        self.active_window = None
        self.previous_hwnd = None
        self.pil_img = None
        self.tk_img = None
        self.overlay_hwnd = None
        self.overlay_visible = True
        self.is_dragging = False
        self.current_hover_idx = -1
        self.ocr_boxes = []
        self._settings_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")
        self.japanese_font = DEFAULT_FONT
        self.japanese_font_size = DEFAULT_FONT_SIZE
        self.font_size_en = 11
        self._load_settings()
        self._settings_window = None
        
        # Start checking the queue for trigger events or translation results
        self.root.after(100, self.check_queue)
        self.root.after(500, self.check_focus)

    def _load_settings(self):
        defaults = {"show_crop": True, "show_romaji": True, "skip_non_japanese": SKIP_NON_JAPANESE}
        try:
            with open(self._settings_file, "r") as f:
                data = json.load(f)
            for k, v in defaults.items():
                setattr(self, k, data.get(k, v))
            self.japanese_font = data.get("japanese_font", DEFAULT_FONT)
            self.japanese_font_size = data.get("japanese_font_size", DEFAULT_FONT_SIZE)
            self.font_size_en = data.get("font_size_en", 11)
        except (FileNotFoundError, json.JSONDecodeError):
            for k, v in defaults.items():
                setattr(self, k, v)

    def _save_settings(self):
        data = {
            "show_crop": self.show_crop,
            "show_romaji": self.show_romaji,
            "skip_non_japanese": self.skip_non_japanese,
            "japanese_font": self.japanese_font,
            "japanese_font_size": self.japanese_font_size,
            "font_size_en": self.font_size_en,
        }
        with open(self._settings_file, "w") as f:
            json.dump(data, f)

    def check_queue(self):
        try:
            while True:
                msg_type, data = self.msg_queue.get_nowait()
                if msg_type == "trigger":
                    self.freeze_screen()
                elif msg_type == "trigger_snip":
                    self.enter_snip_mode()
                elif msg_type == "ocr_complete":
                    self.display_translations(data)
                elif msg_type == "toggle_settings":
                    self.toggle_settings()
        except queue.Empty:
            pass
        self.root.after(50, self.check_queue)

    def trigger(self):
        self.msg_queue.put(("trigger", None))

    def trigger_snip(self):
        self.msg_queue.put(("trigger_snip", None))

    def trigger_settings(self):
        self.msg_queue.put(("toggle_settings", None))

    def toggle_settings(self):
        if self._settings_window and self._settings_window.winfo_exists():
            self._settings_window.destroy()
            self._settings_window = None
            return
        win = tk.Toplevel(self.root)
        win.title("Settings")
        win.resizable(False, False)
        win.attributes("-topmost", True)
        self._settings_window = win

        self._show_crop_var = tk.BooleanVar(value=self.show_crop)
        self._show_romaji_var = tk.BooleanVar(value=self.show_romaji)
        self._skip_nj_var = tk.BooleanVar(value=self.skip_non_japanese)

        def on_crop_toggle():
            self.show_crop = self._show_crop_var.get()
            self._save_settings()
            self._refresh_hover_card()

        def on_romaji_toggle():
            self.show_romaji = self._show_romaji_var.get()
            self._save_settings()
            self._refresh_hover_card()

        def on_skip_nj_toggle():
            self.skip_non_japanese = self._skip_nj_var.get()
            self._save_settings()

        def on_font_change(*_):
            self.japanese_font = self._font_var.get()
            self._save_settings()
            self._refresh_hover_card()

        def on_font_size_change(*_):
            self.japanese_font_size = int(self._font_size_var.get())
            self._save_settings()
            self._refresh_hover_card()

        def on_font_size_en_change(*_):
            self.font_size_en = int(self._font_size_en_var.get())
            self._save_settings()
            self._refresh_hover_card()

        pad = {"padx": 12, "pady": 3}
        tk.Label(win, text="Hover Card", font=("Segoe UI", 9, "bold"),
                 anchor="w").pack(fill="x", padx=12, pady=(10, 2))
        sep = tk.Frame(win, height=1, bg="#c0c0c0")
        sep.pack(fill="x", padx=12)
        tk.Checkbutton(win, text="Show cropped image", variable=self._show_crop_var,
                       command=on_crop_toggle).pack(anchor="w", **pad)
        tk.Checkbutton(win, text="Show romaji", variable=self._show_romaji_var,
                       command=on_romaji_toggle).pack(anchor="w", **pad)

        tk.Label(win, text="OCR Filter", font=("Segoe UI", 9, "bold"),
                 anchor="w").pack(fill="x", padx=12, pady=(10, 2))
        sep2 = tk.Frame(win, height=1, bg="#c0c0c0")
        sep2.pack(fill="x", padx=12)
        tk.Checkbutton(win, text="Skip non-Japanese text", variable=self._skip_nj_var,
                       command=on_skip_nj_toggle).pack(anchor="w", **pad)

        tk.Label(win, text="Japanese Font", font=("Segoe UI", 9, "bold"),
                 anchor="w").pack(fill="x", padx=12, pady=(10, 2))
        sep3 = tk.Frame(win, height=1, bg="#c0c0c0")
        sep3.pack(fill="x", padx=12)
        self._font_var = tk.StringVar(value=self.japanese_font)
        self._font_var.trace("w", on_font_change)
        om = tk.OptionMenu(win, self._font_var, *FONT_CHOICES)
        om.config(width=20)
        om.pack(anchor="w", **pad)

        tk.Label(win, text="Font Size (pt)", font=("Segoe UI", 9, "bold"),
                 anchor="w").pack(fill="x", padx=12, pady=(10, 2))
        sep4 = tk.Frame(win, height=1, bg="#c0c0c0")
        sep4.pack(fill="x", padx=12)
        self._font_size_var = tk.StringVar(value=str(self.japanese_font_size))
        self._font_size_var.trace("w", on_font_size_change)
        om2 = tk.OptionMenu(win, self._font_size_var, *[str(s) for s in FONT_SIZES])
        om2.config(width=20)
        om2.pack(anchor="w", **pad)

        tk.Label(win, text="Font Size EN (pt)", font=("Segoe UI", 9, "bold"),
                 anchor="w").pack(fill="x", padx=12, pady=(10, 2))
        sep5 = tk.Frame(win, height=1, bg="#c0c0c0")
        sep5.pack(fill="x", padx=12)
        self._font_size_en_var = tk.StringVar(value=str(self.font_size_en))
        self._font_size_en_var.trace("w", on_font_size_en_change)
        EN_FONT_SIZES = [9, 10, 11, 12, 13, 14, 16, 18]
        om3 = tk.OptionMenu(win, self._font_size_en_var, *[str(s) for s in EN_FONT_SIZES])
        om3.config(width=20)
        om3.pack(anchor="w", **pad)

        win.update_idletasks()
        win.geometry(f"{win.winfo_reqwidth()}x{win.winfo_reqheight()}")

        win.protocol("WM_DELETE_WINDOW", lambda: (
            setattr(self, '_settings_window', None), win.destroy()
        ))

    def _refresh_hover_card(self):
        if self.current_hover_idx >= 0 and self.current_hover_idx < len(self.ocr_boxes):
            self.clear_hover_translation()
            self.show_hover_translation(self.ocr_boxes[self.current_hover_idx])

    def check_focus(self):
        if not self.active_window:
            self.root.after(500, self.check_focus)
            return

        # Don't hide while user is actively selecting text
        if self.is_dragging:
            self.root.after(500, self.check_focus)
            return

        fg = user32.GetForegroundWindow()
        # Hide overlay when neither the game nor the overlay is focused
        if self.overlay_visible and fg not in (self.overlay_hwnd, self.previous_hwnd):
            self.overlay_visible = False
            self.active_window.withdraw()
        # Re-show when the game (or overlay) becomes active again
        elif not self.overlay_visible and fg in (self.overlay_hwnd, self.previous_hwnd):
            self.active_window.deiconify()
            self.active_window.attributes("-topmost", True)
            self.overlay_visible = True

        self.root.after(500, self.check_focus)

    def freeze_screen(self):
        if self.active_window is not None:
            return  # Already active

        # 1. Capture currently active window handle + its screen bounds
        self.previous_hwnd = ctypes.windll.user32.GetForegroundWindow()

        # Get the focused window's bounding rect in screen coordinates
        rect = ctypes.wintypes.RECT()
        ctypes.windll.user32.GetWindowRect(self.previous_hwnd, ctypes.byref(rect))
        win_rect = {
            'left': rect.left,
            'top': rect.top,
            'right': rect.right,
            'bottom': rect.bottom,
        }

        # 2. Capture the full monitor screen where the mouse cursor is located
        sct_img, monitor = capture_moused_monitor()
        self.pil_img = Image.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")

        # Convert window rect to monitor-local coordinates
        mx_off = monitor['left']
        my_off = monitor['top']
        win_local = {
            'x': max(0, win_rect['left'] - mx_off),
            'y': max(0, win_rect['top']  - my_off),
            'w': min(monitor['width'],  win_rect['right']  - mx_off) - max(0, win_rect['left'] - mx_off),
            'h': min(monitor['height'], win_rect['bottom'] - my_off) - max(0, win_rect['top']  - my_off),
        }

        # Apply crop margins to trim window chrome / unwanted edges
        win_local['x'] += CROP_LEFT
        win_local['y'] += CROP_TOP
        win_local['w'] -= CROP_LEFT + CROP_RIGHT
        win_local['h'] -= CROP_TOP + CROP_BOTTOM

        # 3. Create overlay window positioned over the game window (not full screen)
        overlay_x = win_rect['left'] + CROP_LEFT
        overlay_y = win_rect['top'] + CROP_TOP
        overlay_w = win_local['w']
        overlay_h = win_local['h']

        self.active_window = tk.Toplevel(self.root)
        self.active_window.overrideredirect(True)
        self.active_window.geometry(f"{overlay_w}x{overlay_h}+{overlay_x}+{overlay_y}")

        self.canvas = tk.Canvas(self.active_window, borderwidth=0, highlightthickness=0, bg="black")
        self.canvas.pack(fill="both", expand=True)

        # Capture overlay top-level HWND for focus tracking
        self.active_window.update_idletasks()
        self.overlay_hwnd = user32.GetAncestor(self.active_window.winfo_id(), 2)  # GA_ROOT
        self.overlay_visible = True

        # 4. Show only the cropped game window content as background
        bg_crop = self.pil_img.crop((win_local['x'], win_local['y'],
                                      win_local['x'] + win_local['w'],
                                      win_local['y'] + win_local['h']))
        self.tk_img = ImageTk.PhotoImage(bg_crop)
        self.canvas.create_image(0, 0, image=self.tk_img, anchor="nw")

        # Draw a subtle border to indicate the overlay is active
        self.canvas.create_rectangle(0, 0, overlay_w - 1, overlay_h - 1,
                                      outline="#007aff", width=2)

        # Loader text
        self.loader_text = self.canvas.create_text(
            overlay_w // 2, 25,
            text="[ Running OCR / Translation... ]",
            fill="#ffffff", font=("Segoe UI", 14, "bold"), justify="center"
        )
        self.canvas.create_rectangle(
            overlay_w // 2 - 160, 8, overlay_w // 2 + 160, 46,
            fill="#1c1c1e", outline="#3a3a3c", width=2, tags="loader_bg"
        )
        self.canvas.tag_raise(self.loader_text)

        # 5. Bind escape key to close
        self.active_window.bind("<Escape>", lambda e: self.unfreeze_screen())

        # 6. Bring overlay to front
        self.active_window.attributes("-topmost", True)
        self.active_window.focus_force()

        # 7. Start background thread for OCR & Translation
        threading.Thread(
            target=self.process_ocr,
            args=(self.pil_img, win_local),
            daemon=True
        ).start()

    # ── Snip mode (drag-select region, Ctrl+Alt+Shift+R) ────────────────────

    def enter_snip_mode(self):
        if self.active_window:
            return
        sct_img, self.snip_monitor = capture_moused_monitor()
        self.pil_img = Image.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")

        mw = self.snip_monitor['width']
        mh = self.snip_monitor['height']
        ml = self.snip_monitor['left']
        mt = self.snip_monitor['top']

        self.snip_window = tk.Toplevel(self.root)
        self.snip_window.overrideredirect(True)
        self.snip_window.geometry(f"{mw}x{mh}+{ml}+{mt}")

        self.snip_canvas = tk.Canvas(self.snip_window, borderwidth=0, highlightthickness=0, bg="black")
        self.snip_canvas.pack(fill="both", expand=True)

        bg_tk = ImageTk.PhotoImage(self.pil_img)
        self.snip_bg_tk = bg_tk
        self.snip_canvas.create_image(0, 0, image=bg_tk, anchor="nw")

        # Dim overlay
        self.snip_canvas.create_rectangle(0, 0, mw, mh, fill="black", stipple="gray12", tags="dim")

        self.snip_start = None
        self.snip_rect_id = None
        self.snip_canvas.bind("<Button-1>", self.snip_mouse_down)
        self.snip_canvas.bind("<B1-Motion>", self.snip_mouse_drag)
        self.snip_canvas.bind("<ButtonRelease-1>", self.snip_mouse_up)
        self.snip_canvas.bind("<Escape>", lambda e: self._close_snip())

        self.snip_window.attributes("-topmost", True)
        self.snip_window.focus_force()

    def _close_snip(self):
        if self.snip_window:
            self.snip_window.destroy()
            self.snip_window = None
            self.snip_canvas = None
            self.snip_monitor = None
            self.snip_bg_tk = None
            self.snip_start = None
            self.snip_rect_id = None

    def snip_mouse_down(self, event):
        self.snip_start = (event.x, event.y)

    def snip_mouse_drag(self, event):
        if not self.snip_start:
            return
        c = self.snip_canvas
        if self.snip_rect_id:
            c.delete(self.snip_rect_id)
        x1, y1 = self.snip_start
        x2, y2 = event.x, event.y
        self.snip_rect_id = c.create_rectangle(
            x1, y1, x2, y2, outline="#00ff00", width=3, tags="snip_sel"
        )

    def snip_mouse_up(self, event):
        if not self.snip_start:
            return
        x1 = min(self.snip_start[0], event.x)
        y1 = min(self.snip_start[1], event.y)
        x2 = max(self.snip_start[0], event.x)
        y2 = max(self.snip_start[1], event.y)
        w, h = x2 - x1, y2 - y1
        if w < 20 or h < 20:
            self._close_snip()
            return

        # Convert canvas (monitor-local) coords to screen coords
        ml = self.snip_monitor['left']
        mt = self.snip_monitor['top']
        screen_rect = (ml + x1, mt + y1, ml + x2, mt + y2)
        self._close_snip()

        self.freeze_screen_region(screen_rect)

    def freeze_screen_region(self, screen_rect):
        """Freeze a user-selected region (screen coords: left, top, right, bottom)."""
        left, top, right, bottom = screen_rect
        w, h = right - left, bottom - top

        self.active_window = tk.Toplevel(self.root)
        self.active_window.overrideredirect(True)
        self.active_window.geometry(f"{w}x{h}+{left}+{top}")

        self.canvas = tk.Canvas(self.active_window, borderwidth=0, highlightthickness=0, bg="black")
        self.canvas.pack(fill="both", expand=True)

        self.active_window.update_idletasks()
        self.overlay_hwnd = user32.GetAncestor(self.active_window.winfo_id(), 2)
        self.overlay_visible = True

        # Crop the full image to the selected region
        mx_off = self.snip_monitor['left']
        my_off = self.snip_monitor['top']
        win_local = {
            'x': left - mx_off,
            'y': top - my_off,
            'w': w,
            'h': h,
        }

        bg_crop = self.pil_img.crop((win_local['x'], win_local['y'],
                                      win_local['x'] + w, win_local['y'] + h))
        self.tk_img = ImageTk.PhotoImage(bg_crop)
        self.canvas.create_image(0, 0, image=self.tk_img, anchor="nw")
        self.canvas.create_rectangle(0, 0, w - 1, h - 1, outline="#007aff", width=2)

        self.loader_text = self.canvas.create_text(
            w // 2, 25,
            text="[ Running OCR / Translation... ]",
            fill="#ffffff", font=("Segoe UI", 14, "bold"), justify="center"
        )
        self.canvas.create_rectangle(
            w // 2 - 160, 8, w // 2 + 160, 46,
            fill="#1c1c1e", outline="#3a3a3c", width=2, tags="loader_bg"
        )
        self.canvas.tag_raise(self.loader_text)
        self.active_window.bind("<Escape>", lambda e: self.unfreeze_screen())
        self.active_window.attributes("-topmost", True)
        self.active_window.focus_force()

        threading.Thread(
            target=self.process_ocr,
            args=(self.pil_img, win_local, SNIP_OCR_SCALE),
            daemon=True
        ).start()

    def run_ocr_paddle(self, win_crop):
        """Run PaddleOCR on the crop, return lines in same format as run_ocr_at_scale."""
        ocr = get_paddle_ocr()
        import numpy as np
        img_array = np.array(win_crop.convert("RGB"))
        results = list(ocr.predict(img_array))
        lines = []
        if results:
            r = results[0]
            dt_polys = r.get('dt_polys', [])
            rec_texts = r.get('rec_texts', [])
            for poly, text in zip(dt_polys, rec_texts):
                text = re.sub(r'\s+', '', text).strip()
                if not text:
                    continue
                xs = [int(p[0]) for p in poly]
                ys = [int(p[1]) for p in poly]
                bbox = {
                    'x': min(xs),
                    'y': min(ys),
                    'width': max(xs) - min(xs),
                    'height': max(ys) - min(ys)
                }
                chars = list(text)
                cw = bbox['width'] / max(len(chars), 1)
                words = []
                for ci, ch in enumerate(chars):
                    words.append({
                        'text': ch,
                        'bounding_rect': {
                            'x': bbox['x'] + int(ci * cw),
                            'y': bbox['y'],
                            'width': int(cw),
                            'height': bbox['height'],
                        }
                    })
                lines.append({
                    'text': text,
                    'words': words,
                })
        return lines

    def run_ocr_at_scale(self, win_crop, scale):
        """Run WinOCR at a given scale factor, return lines with bboxes in overlay coords."""
        ocr_img = win_crop.resize(
            (win_crop.width * scale, win_crop.height * scale),
            Image.LANCZOS
        )
        ocr_res = winocr.recognize_pil_sync(ocr_img, 'ja')
        lines = ocr_res.get('lines', [])
        for line in lines:
            for word in line.get('words', []):
                br = word['bounding_rect']
                br['x'] /= scale
                br['y'] /= scale
                br['width'] /= scale
                br['height'] /= scale
        return lines

    def merge_lines(self, *line_sets):
        """Merge lines from multiple OCR passes, keeping the best text per region."""
        def iou(a, b):
            x1 = max(a['x'], b['x'])
            y1 = max(a['y'], b['y'])
            x2 = min(a['x'] + a['width'], b['x'] + b['width'])
            y2 = min(a['y'] + a['height'], b['y'] + b['height'])
            if x2 <= x1 or y2 <= y1:
                return 0.0
            inter = (x2 - x1) * (y2 - y1)
            u = a['width'] * a['height'] + b['width'] * b['height'] - inter
            return inter / u if u else 0.0

        merged = []
        for lines in line_sets:
            for line in lines:
                bbox = get_line_bounding_rect(line)
                text = line.get('text', '').strip()
                text_clean = re.sub(r'\s+', '', text)
                if not text_clean:
                    continue
                if self.skip_non_japanese and not contains_japanese(text_clean):
                    continue

                # Check overlap with existing merged lines
                found = False
                for i, existing in enumerate(merged):
                    eb = existing['bbox']
                    if iou(bbox, eb) > 0.3:
                        # Keep the line with longer text (more complete)
                        existing_text = existing['line'].get('text', '').strip()
                        existing_clean = re.sub(r'\s+', '', existing_text)
                        if len(text_clean) > len(existing_clean):
                            merged[i] = {'line': line, 'bbox': bbox}
                        found = True
                        break
                if not found:
                    merged.append({'line': line, 'bbox': bbox})
        return [m['line'] for m in merged]

    def process_ocr(self, pil_img, win_local, single_scale=None):
        """Run OCR on the focused window crop only, then offset boxes to screen coords.
           If single_scale is set, runs one pass at that scale (snip mode)."""
        win_x, win_y = win_local['x'], win_local['y']
        win_crop = pil_img.crop((win_x, win_y, win_x + win_local['w'], win_y + win_local['h']))
        
        try:
            engine = globals().get("OCR_ENGINE", "windows")
            if engine == "paddle":
                lines = self.run_ocr_paddle(win_crop)
            else:
                SCALES = [single_scale] if single_scale else globals().get("OCR_SCALES", [3, 7])
                lines = self.merge_lines(
                    *(self.run_ocr_at_scale(win_crop, s) for s in SCALES)
                )
            
            translation_targets = []
            for line in lines:
                text = line.get('text', '').strip()
                print(f"[OCR RAW] '{text}'")
                bbox = get_line_bounding_rect(line)

                words_data = []
                for word in line.get('words', []):
                    br = word['bounding_rect']
                    wt = word.get('text', '').strip()
                    if wt:
                        words_data.append({
                            'text': wt,
                            'x': br['x'],
                            'y': br['y'],
                            'width': br['width'],
                            'height': br['height']
                        })

                crop_pil = None
                text = text.strip()
                text = re.sub(r'\s+', '', text)
                if not text:
                    continue
                if SKIP_NON_JAPANESE and not contains_japanese(text):
                    print(f"[OCR SKIP] '{text}' (no Japanese chars)")
                    continue
                translation_targets.append((text, bbox, crop_pil, words_data))

            # Fetch translations in parallel
            boxes = []
            with ThreadPoolExecutor(max_workers=8) as executor:
                futures = {
                    executor.submit(translate_and_convert, text): (bbox, crop_pil, words_data)
                    for text, bbox, crop_pil, words_data in translation_targets
                }
                for future in futures:
                    bbox, crop_pil, words_data = futures[future]
                    try:
                        res = future.result()
                        # For windows engine, crop from full monitor image
                        if crop_pil is None:
                            crop_pil = pil_img.crop((
                                max(0, int(bbox['x'] + win_x)),
                                max(0, int(bbox['y'] + win_y)),
                                min(pil_img.width,  int(bbox['x'] + bbox['width'] + win_x)),
                                min(pil_img.height, int(bbox['y'] + bbox['height'] + win_y))
                            ))
                        
                        # Estimate width based on longest line of text or crop width
                        max_chars = max(len(res['original']), len(res['romaji']), len(res['kana']), len(res['english']))
                        text_w = max_chars * 7 + 24
                        w = min(max(text_w, bbox['width'] + 24, 180), 400)
                        
                        # Estimate height including cropped image height
                        h = bbox['height'] + 130 + (len(res['english']) // 40) * 16
                        
                        print(f"[RESULT] orig='{res['original']}' english='{res['english']}'")
                        boxes.append({
                            'w': w,
                            'h': h,
                            'data': res,
                            'orig_bbox': bbox,
                            'crop_pil': crop_pil,
                            'words': words_data
                        })
                    except Exception as e:
                        print("Error processing translation:", e)

            # Post result back to main GUI thread
            self.msg_queue.put(("ocr_complete", boxes))
        except Exception as e:
            print("OCR process failed:", e)
            self.msg_queue.put(("ocr_complete", []))

    def display_translations(self, boxes):
        """Store OCR results, draw highlights, and enable hover-to-show translation."""
        if not self.active_window:
            return

        self.canvas.delete(self.loader_text)
        self.canvas.delete("loader_bg")

        self.ocr_boxes = boxes
        self.current_hover_idx = -1
        self.hover_window_id = None
        self.highlight_refs = []
        self.crop_tk_imgs = []
        self.is_dragging = False
        self.selection_box_idx = -1
        self.selection_start = -1
        self.selection_end = -1
        self.selection_overlays = []
        self._hover_card_items = []
        self._hover_card_hl_items = []
        self._hover_card_bg_id = None
        self._hover_chunk_positions = []
        self._hover_hl_y = 0
        self._hover_word_idx = -1
        self._hover_kakasi_items = []
        self._hover_fg_items = {}
        self._hover_romaji_id = None
        self._hover_overlay_items = []

        for box in boxes:
            orig = box['orig_bbox']
            ref = self.canvas.create_rectangle(
                orig['x'] - 2, orig['y'] - 2,
                orig['x'] + orig['width'] + 2, orig['y'] + orig['height'] + 2,
                outline="#007aff", width=2, tags="ocr_highlight"
            )
            self.highlight_refs.append(ref)

        self.canvas.bind("<Motion>", self.on_mouse_move)
        self.canvas.bind("<Leave>", lambda e: self.clear_hover_translation())
        self.canvas.bind("<Button-1>", self.on_mouse_down)
        self.canvas.bind("<B1-Motion>", self.on_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_release)
        self.canvas.bind("<Button-3>", self.on_right_click)
        self.canvas.bind("<Shift-Button-3>", self.on_shift_right_click)
        self.canvas.bind("<Button-2>", self.on_middle_click)
        self.canvas.bind("<MouseWheel>", self.on_furigana_scroll)
        self.canvas.bind("<Button-4>", self.on_furigana_scroll)
        self.canvas.bind("<Button-5>", self.on_furigana_scroll)

    def on_mouse_move(self, event):
        if self.is_dragging:
            return
        if not hasattr(self, 'ocr_boxes') or not self.ocr_boxes:
            return

        hover_idx = -1
        for i, box in enumerate(self.ocr_boxes):
            ob = box['orig_bbox']
            if ob['x'] <= event.x <= ob['x'] + ob['width'] and \
               ob['y'] <= event.y <= ob['y'] + ob['height']:
                hover_idx = i
                break

        if hover_idx != self.current_hover_idx:
            self.clear_hover_translation()
            self.current_hover_idx = hover_idx
            if hover_idx >= 0:
                self.show_hover_translation(self.ocr_boxes[hover_idx])
        elif hover_idx >= 0:
            wi = self.get_word_at_pos(hover_idx, event.x, event.y)
            if wi >= 0 and wi != self._hover_word_idx:
                self._hover_word_idx = wi
                self.update_hover_highlights(self.ocr_boxes[hover_idx], wi)
                self._show_hover_overlay_highlight(hover_idx, wi)

    def clear_hover_translation(self):
        for item_id in self._hover_card_items:
            self.canvas.delete(item_id)
        self._hover_card_items = []
        self._hover_chunk_positions = []
        self._hover_card_bg_id = None
        self._clear_hover_tags()
        for ref in self.highlight_refs:
            self.canvas.itemconfig(ref, outline="#007aff", width=2)
        self.current_hover_idx = -1
        self._hover_kakasi_items = []
        self._hover_fg_items = {}
        self._hover_romaji_id = None
        self._hover_word_idx = -1
        self._clear_hover_overlay()

    def show_hover_translation(self, box):
        data = box['data']
        crop_pil = box['crop_pil']
        orig = box['orig_bbox']
        w = box['w']

        idx = self.ocr_boxes.index(box)
        if 0 <= idx < len(self.highlight_refs):
            self.canvas.itemconfig(self.highlight_refs[idx], outline="#ff9500", width=3)

        screen_w = self.canvas.winfo_width()

        x = orig['x'] + (orig['width'] - w) // 2
        gap = 4
        card_bottom = orig['y'] - gap

        x = max(0, min(x, screen_w - w))

        kf = tkfont.Font(family=self.japanese_font, size=self.japanese_font_size, weight="bold")
        en_font = tkfont.Font(family="Segoe UI", size=self.font_size_en, weight="bold")
        ki = data.get('kakasi_items', [])
        full_text = ''.join(it['orig'] for it in ki)
        pad_x = 6

        # Expand card width to fit original Japanese text
        orig_w = kf.measure(data['original'])
        if orig_w + pad_x * 2 > w:
            w = orig_w + pad_x * 2
            x = orig['x'] + (orig['width'] - w) // 2
            x = max(0, min(x, screen_w - w))

        # Calculate card height based on visible content
        furigana_size = max(8, self.japanese_font_size // 2 - 1)
        ff_temp = tkfont.Font(family=self.japanese_font, size=furigana_size)
        fg_ascent = ff_temp.metrics("ascent")
        fg_line_h = ff_temp.metrics("linespace")
        top_pad = max(8, 6)
        if self.show_crop:
            crop_tk = ImageTk.PhotoImage(crop_pil)
            self.crop_tk_imgs.append(crop_tk)
            ih = crop_tk.height()
            content_h = 3 + ih + 24
        else:
            content_h = top_pad
        line_h = max(30, kf.metrics("linespace"))
        content_h += line_h + 2  # original text line + gap
        content_h += fg_line_h + 2  # furigana + gap
        if self.show_romaji:
            content_h += 18 + 2  # romaji + gap
        est_chars_per_line = max(1, (w - 16) // 7)
        en_lines = max(1, -(-len(data['english']) // est_chars_per_line))
        en_line_h = en_font.metrics("linespace")
        en_height = en_lines * en_line_h
        content_h += en_height  # english
        content_h += 3  # bottom padding
        card_h = content_h

        # Card positioned above the OCR box, extending upward
        card_h = content_h
        card_top = max(0, card_bottom - card_h)
        card_y = card_top

        # Background card
        self._hover_card_bg_id = self.canvas.create_rectangle(
            x, card_top, x + w, card_bottom,
            fill="#ffffff", outline="#e5e5ea", width=1
        )
        self._hover_card_items.append(self._hover_card_bg_id)

        # Crop image
        if self.show_crop:
            self._hover_card_items.append(self.canvas.create_image(
                x + 5, card_y + 3, image=crop_tk, anchor="nw"
            ))
            line_y = card_y + 3 + ih + 24
        else:
            line_y = card_y + top_pad

        # Original text (bold Japanese)
        self._hover_kakasi_items = ki
        self._hover_chunk_positions = []
        ascent = kf.metrics("ascent")
        descent = kf.metrics("descent")
        self._hover_hl_y = line_y
        self._hover_card_items.append(self.canvas.create_text(
            x + pad_x, line_y, text=data['original'],
            font=(self.japanese_font, self.japanese_font_size, "bold"),
            fill="#a31515", anchor="nw"
        ))

        # Furigana below original text
        ff = (self.japanese_font, furigana_size)
        fg_y = line_y + line_h - 2
        char_off = 0
        self._hover_fg_items = {}  # chunk_idx → canvas item id
        self._hover_card_x = x
        self._hover_pad_x = pad_x
        self._hover_romaji_chunks = []  # per-chunk {x, w} for highlight rect
        for item in ki:
            orig = item.get('orig', '')
            hira = item.get('hira') or orig
            prefix_w = kf.measure(full_text[:char_off])
            group_w = kf.measure(orig)
            chunk_x = x + pad_x + prefix_w
            cp = {
                'x': chunk_x,
                'w': group_w,
                'char_start': char_off,
                'char_end': char_off + len(orig),
            }
            self._hover_chunk_positions.append(cp)
            if orig != hira:
                cx = x + pad_x + prefix_w + group_w / 2
                fg_id = self.canvas.create_text(
                    cx, fg_y, text=hira, font=ff, fill="#248a3d", anchor="n"
                )
                self._hover_card_items.append(fg_id)
                self._hover_fg_items[char_off] = fg_id
            char_off += len(orig)

        # Romaji and English y positions
        romaji_y = fg_y + fg_line_h + 2
        eng_y = romaji_y + 18 if self.show_romaji else fg_y + fg_line_h + 2

        # Romaji text (single text item + per-chunk position tracking for highlight)
        self._hover_romaji_id = None
        self._hover_romaji_y = romaji_y
        if self.show_romaji:
            self._hover_romaji_id = self.canvas.create_text(
                x + pad_x, romaji_y, text=data['romaji'],
                font=("Segoe UI", max(7, self.font_size_en - 2), "italic"),
                fill="#0066cc", anchor="nw"
            )
            self._hover_card_items.append(self._hover_romaji_id)
            # Compute per-chunk romaji x/w for highlight rect
            rf = tkfont.Font(family="Segoe UI", size=max(7, self.font_size_en - 2), slant="italic")
            rfx = x + pad_x
            for item in ki:
                rt = item.get('hepburn', '') or item.get('orig', '')
                rw = rf.measure(rt)
                self._hover_romaji_chunks.append({'x': rfx, 'w': rw})
                rfx += rw + rf.measure(' ')

        # Store furigana bounds for scroll hit detection
        self._hover_fg_y = fg_y
        self._hover_fg_ascent = fg_ascent
        self._hover_fg_line_h = fg_line_h

        # English text
        self._hover_card_items.append(self.canvas.create_text(
            x + pad_x, eng_y, text=data['english'],
            font=("Segoe UI", self.font_size_en, "bold"),
            fill="#1c1c1e", anchor="nw", width=w - 16
        ))

    def on_furigana_scroll(self, event):
        """Cycle furigana reading for the highlighted (hovered) kanji chunk."""
        if self.current_hover_idx < 0 or self._hover_word_idx < 0:
            return
        # Find chunk matching the currently highlighted word
        box = self.ocr_boxes[self.current_hover_idx]
        ki = box['data']['kakasi_items']
        target = None
        for cp in self._hover_chunk_positions:
            if cp['char_start'] <= self._hover_word_idx < cp['char_end']:
                # Find corresponding item in kakasi_items by char offset
                off = 0
                for it in ki:
                    if off == cp['char_start']:
                        target = it
                        break
                    off += len(it.get('orig',''))
                break
        if target is None:
            return
        alts = target.get('alternatives', [])
        if len(alts) < 2:
            return
        # Determine direction
        delta = event.delta if hasattr(event, 'delta') else (120 if event.num == 4 else -120)
        step = 1 if delta > 0 else -1
        new_idx = (target['active_idx'] + step) % len(alts)
        target['active_idx'] = new_idx
        target['hira'] = alts[new_idx]['hira']
        target['hepburn'] = alts[new_idx]['hepburn']
        # Update furigana canvas text
        fg_id = self._hover_fg_items.get(cp['char_start'])
        if fg_id is not None and target['hira'] != target['orig']:
            self.canvas.itemconfig(fg_id, text=target['hira'])
        # Update romaji
        new_romaji = " ".join([i['hepburn'] for i in ki])
        box['data']['romaji'] = new_romaji
        if self._hover_romaji_id is not None:
            self.canvas.itemconfig(self._hover_romaji_id, text=new_romaji)
            # Recompute romaji chunk positions for accurate highlight
            rf = tkfont.Font(family="Segoe UI", size=max(7, self.font_size_en - 2), slant="italic")
            rfx = self._hover_card_x + self._hover_pad_x
            self._hover_romaji_chunks = []
            for it in ki:
                rt = it.get('hepburn', '') or it.get('orig', '')
                rw = rf.measure(rt)
                self._hover_romaji_chunks.append({'x': rfx, 'w': rw})
                rfx += rw + rf.measure(' ')
            # Refresh highlight rects with new positions
            self.update_hover_highlights(
                self.ocr_boxes[self.current_hover_idx], self._hover_word_idx
            )


    def _build_item_ranges(self, items):
        """Build char→kana and char→romaji index ranges from kakasi_items."""
        kana_ranges = []
        romaji_ranges = []
        kana_off = 0
        romaji_off = 0
        char_off = 0
        for idx, item in enumerate(items):
            orig = item.get('orig', '')
            orig_len = len(orig)
            kana_t = item.get('hira') or orig
            romaji_t = item.get('hepburn', '')
            kana_ranges.append({
                'char_start': char_off, 'char_end': char_off + orig_len,
                'text_start': kana_off, 'text_end': kana_off + len(kana_t),
            })
            romaji_ranges.append({
                'char_start': char_off, 'char_end': char_off + orig_len,
                'text_start': romaji_off, 'text_end': romaji_off + len(romaji_t),
            })
            char_off += orig_len
            kana_off += len(kana_t)
            romaji_off += len(romaji_t)
            if idx < len(items) - 1:
                kana_off += 1
                romaji_off += 1
        return kana_ranges, romaji_ranges

    def _clear_hover_overlay(self):
        for item in self._hover_overlay_items:
            self.canvas.delete(item)
        self._hover_overlay_items = []

    def _show_hover_overlay_highlight(self, box_idx, word_idx):
        self._clear_hover_overlay()
        box = self.ocr_boxes[box_idx]
        items = box['data'].get('kakasi_items', [])
        if not items:
            return
        kana_ranges, _ = self._build_item_ranges(items)
        chunk_chars = None
        for kr in kana_ranges:
            if kr['char_start'] <= word_idx < kr['char_end']:
                chunk_chars = (kr['char_start'], kr['char_end'])
                break
        if not chunk_chars:
            return
        words = box.get('words', [])
        xs, ys = [], []
        for wi in range(chunk_chars[0], chunk_chars[1]):
            if wi < len(words):
                w = words[wi]
                xs.append(w['x'])
                xs.append(w['x'] + w['width'])
                ys.append(w['y'])
                ys.append(w['y'] + w['height'])
        if not xs:
            return
        x1, x2 = min(xs), max(xs)
        y1, y2 = min(ys), max(ys)
        item = self.canvas.create_rectangle(
            x1, y1, x2, y2,
            fill="#ffe082", stipple="gray25", outline=""
        )
        self._hover_overlay_items.append(item)

    def _clear_hover_tags(self):
        for item_id in self._hover_card_hl_items:
            self.canvas.delete(item_id)
        self._hover_card_hl_items = []

    def update_hover_highlights(self, box, char_idx):
        """Draw highlight rect in card for the hovered chunk."""
        self._clear_hover_tags()
        if char_idx < 0 or not hasattr(self, '_hover_hl_y'):
            return
        for i, cp in enumerate(self._hover_chunk_positions):
            if cp['char_start'] <= char_idx < cp['char_end']:
                hl = self.canvas.create_rectangle(
                    cp['x'], self._hover_hl_y,
                    cp['x'] + cp['w'], self._hover_hl_y + 28,
                    fill="#ffe082", outline=""
                )
                self._hover_card_hl_items.append(hl)
                if self._hover_card_bg_id is not None:
                    self.canvas.lift(hl, self._hover_card_bg_id)
                # Highlight matching romaji chunk
                if self.show_romaji and i < len(self._hover_romaji_chunks):
                    rc = self._hover_romaji_chunks[i]
                    hl2 = self.canvas.create_rectangle(
                        rc['x'], self._hover_romaji_y,
                        rc['x'] + rc['w'], self._hover_romaji_y + 16,
                        fill="#ffe082", outline=""
                    )
                    self._hover_card_hl_items.append(hl2)
                    if self._hover_card_bg_id is not None:
                        self.canvas.lift(hl2, self._hover_card_bg_id)
                break

    # ── Word-level selection & clipboard ──────────────────────────────────────

    def get_word_at_pos(self, box_idx, x, y):
        words = self.ocr_boxes[box_idx].get('words', [])
        for wi, w in enumerate(words):
            if w['x'] <= x <= w['x'] + w['width'] and \
               w['y'] <= y <= w['y'] + w['height']:
                return wi
        # fallback: closest word by center distance
        best, best_d = -1, float('inf')
        for wi, w in enumerate(words):
            cx = w['x'] + w['width'] / 2
            cy = w['y'] + w['height'] / 2
            d = (cx - x) ** 2 + (cy - y) ** 2
            if d < best_d:
                best_d = d
                best = wi
        return best

    def clear_selection_visuals(self):
        for item in self.selection_overlays:
            self.canvas.delete(item)
        self.selection_overlays = []

    def on_mouse_down(self, event):
        self.clear_selection_visuals()
        self.selection_box_idx = -1
        self.selection_start = -1
        self.selection_end = -1

        for bi, box in enumerate(self.ocr_boxes):
            ob = box['orig_bbox']
            if ob['x'] <= event.x <= ob['x'] + ob['width'] and \
               ob['y'] <= event.y <= ob['y'] + ob['height']:
                self.selection_box_idx = bi
                wi = self.get_word_at_pos(bi, event.x, event.y)
                if wi >= 0:
                    self.selection_start = wi
                    self.selection_end = wi
                    self.draw_selection_highlight()
                self.is_dragging = True
                break

    def on_drag(self, event):
        if self.selection_box_idx < 0:
            return
        box = self.ocr_boxes[self.selection_box_idx]
        ob = box['orig_bbox']
        if not (ob['x'] <= event.x <= ob['x'] + ob['width'] and
                ob['y'] <= event.y <= ob['y'] + ob['height']):
            return
        wi = self.get_word_at_pos(self.selection_box_idx, event.x, event.y)
        if wi >= 0 and wi != self.selection_end:
            self.selection_end = wi
            self.draw_selection_highlight()

    def draw_selection_highlight(self):
        self.clear_selection_visuals()
        if self.selection_box_idx < 0 or self.selection_start < 0 or self.selection_end < 0:
            return
        start = min(self.selection_start, self.selection_end)
        end = max(self.selection_start, self.selection_end)
        words = self.ocr_boxes[self.selection_box_idx].get('words', [])
        for wi in range(start, end + 1):
            if wi < len(words):
                w = words[wi]
                item = self.canvas.create_rectangle(
                    w['x'], w['y'], w['x'] + w['width'], w['y'] + w['height'],
                    fill="#007aff", stipple="gray50", outline=""
                )
                self.selection_overlays.append(item)

    def on_release(self, event):
        if not self.is_dragging:
            return
        self.is_dragging = False
        if self.selection_box_idx < 0 or self.selection_start < 0 or self.selection_end < 0:
            return
        start = min(self.selection_start, self.selection_end)
        end = max(self.selection_start, self.selection_end)
        words = self.ocr_boxes[self.selection_box_idx].get('words', [])

        if start == end:
            # Click (no drag) → read the whole line aloud, no highlight
            self.clear_selection_visuals()
            box_text = self.ocr_boxes[self.selection_box_idx]['data']['original']
            if box_text:
                threading.Thread(target=self.read_aloud, args=(box_text,), daemon=True).start()
        else:
            # Drag → copy selected words to clipboard
            selected = ''.join(w['text'] for wi, w in enumerate(words) if start <= wi <= end)
            if selected:
                self.root.clipboard_clear()
                self.root.clipboard_append(selected)
                print(f"[clipboard] {selected}")

    def get_current_text(self):
        """Return selected text, or hovered text as fallback."""
        if self.selection_box_idx >= 0 and self.selection_start >= 0 and self.selection_end >= 0:
            start = min(self.selection_start, self.selection_end)
            end = max(self.selection_start, self.selection_end)
            words = self.ocr_boxes[self.selection_box_idx].get('words', [])
            return ''.join(w['text'] for wi, w in enumerate(words) if start <= wi <= end)
        if self.current_hover_idx >= 0:
            return self.ocr_boxes[self.current_hover_idx]['data']['original']
        return None

    def on_right_click(self, event):
        text = self.get_current_text()
        if text:
            url = f"https://jisho.org/search/{urllib.parse.quote(text)}"
            webbrowser.open(url)

    def on_shift_right_click(self, event):
        text = self.get_current_text()
        if text:
            url = f"https://www.deepl.com/en/translator#ja/en/{urllib.parse.quote(text)}"
            webbrowser.open(url)

    def on_middle_click(self, event):
        text = self.get_current_text()
        if text:
            threading.Thread(target=self.read_aloud, args=(text,), daemon=True).start()

    def read_aloud(self, text):
        import asyncio
        import tempfile
        # Use Windows MCI (winmm.dll) to play MP3 silently without external player
        try:
            import edge_tts
        except ImportError:
            print("edge-tts not installed — run: pip install edge-tts")
            return

        tmp = tempfile.mktemp(suffix=".mp3")
        try:
            async def _save():
                tts = edge_tts.Communicate(text, voice="ja-JP-NanamiNeural")
                await tts.save(tmp)
            asyncio.run(_save())

            from ctypes import windll
            mci = windll.winmm.mciSendStringW
            buf = ctypes.create_unicode_buffer(256)
            mci(f'open "{tmp}" alias tts', buf, len(buf), 0)
            mci('play tts wait', buf, len(buf), 0)
            mci('close tts', buf, len(buf), 0)
        except Exception as e:
            print(f"TTS error: {e}")
        finally:
            try:
                os.unlink(tmp)
            except Exception:
                pass

    def unfreeze_screen(self):
        if self.active_window:
            self.active_window.destroy()
            self.active_window = None
            self.overlay_hwnd = None
            self.overlay_visible = True
            self.tk_img = None
            self.pil_img = None
            self.crop_tk_imgs = []
            self.ocr_boxes = []
            self.current_hover_idx = -1
            self.hover_window_id = None
            self.highlight_refs = []
            self.is_dragging = False
            self.selection_box_idx = -1
            self.selection_start = -1
            self.selection_end = -1
            self.selection_overlays = []

        # Restore focus to the previously active window
        if self.previous_hwnd:
            ctypes.windll.user32.SetForegroundWindow(self.previous_hwnd)
            self.previous_hwnd = None

    def run(self):
        self.root.mainloop()

# ── Win32 RegisterHotKey (works across elevation & fullscreen) ────────────────
user32 = ctypes.windll.user32
WM_HOTKEY = 0x0312
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_NOREPEAT = 0x4000

def register_hotkey_win32(app):
    # PeekMessageW to ensure this thread has a message queue
    msg = ctypes.wintypes.MSG()
    user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)  # PM_NOREMOVE

    mods = MOD_CONTROL | MOD_ALT | MOD_SHIFT | MOD_NOREPEAT
    HK_CAPTURE  = 1  # Ctrl+Alt+Shift+E
    HK_SNIP     = 2  # Ctrl+Alt+Shift+R
    HK_SETTINGS = 3  # Ctrl+Alt+Shift+S

    reg_ok = user32.RegisterHotKey(None, HK_CAPTURE,  mods, ord('E'))
    reg_ok = user32.RegisterHotKey(None, HK_SNIP,     mods, ord('R')) and reg_ok
    reg_ok = user32.RegisterHotKey(None, HK_SETTINGS, mods, ord('S')) and reg_ok

    if not reg_ok:
        err = ctypes.get_last_error()
        print(f"[DEBUG] RegisterHotKey failed with error {err}. Falling back to GetAsyncKeyState polling.")

        # ── Fallback: GetAsyncKeyState polling ──────────────────────────────
        def is_key_down(vk):
            return bool(user32.GetAsyncKeyState(vk) & 0x8000)

        VK_MAP = {
            "ctrl":  (0xA2, 0xA3),
            "alt":   (0xA4, 0xA5),
            "shift": (0xA0, 0xA1),
            "e":     (0x45, None),
            "r":     (0x52, None),
            "s":     (0x53, None),
        }
        pressed_e = False
        pressed_r = False
        pressed_s = False
        while True:
            ctrl  = is_key_down(VK_MAP["ctrl"][0])  or is_key_down(VK_MAP["ctrl"][1])
            alt   = is_key_down(VK_MAP["alt"][0])   or is_key_down(VK_MAP["alt"][1])
            shift = is_key_down(VK_MAP["shift"][0]) or is_key_down(VK_MAP["shift"][1])
            if ctrl and alt and shift and is_key_down(VK_MAP["e"][0]):
                if not pressed_e:
                    pressed_e = True
                    app.trigger()
            elif ctrl and alt and shift and is_key_down(VK_MAP["r"][0]):
                if not pressed_r:
                    pressed_r = True
                    app.trigger_snip()
            elif ctrl and alt and shift and is_key_down(VK_MAP["s"][0]):
                if not pressed_s:
                    pressed_s = True
                    app.trigger_settings()
            else:
                pressed_e = False
                pressed_r = False
                pressed_s = False
            time.sleep(0.05)
        return

    print("[DEBUG] RegisterHotKey succeeded – waiting for WM_HOTKEY.")
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0):
        if msg.message == WM_HOTKEY:
            if msg.wParam == HK_CAPTURE:
                app.trigger()
            elif msg.wParam == HK_SNIP:
                app.trigger_snip()
            elif msg.wParam == HK_SETTINGS:
                app.trigger_settings()
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))

def main():
    app = ScreenFreezerApp()

    # Start Win32 hotkey thread (RegisterHotKey with fallback to GetAsyncKeyState)
    threading.Thread(target=register_hotkey_win32, args=(app,), daemon=True).start()

    print("Application started.")
    print("  Ctrl+Alt+Shift+E  Capture game window for OCR / translation")
    print("  Ctrl+Alt+Shift+R  Snip mode (drag-select a region)")
    print("  Ctrl+Alt+Shift+S  Settings panel")
    print("  Press Escape while frozen to unfreeze and restore focus.")

    try:
        app.run()
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    main()
