import ctypes
import ctypes.wintypes
import queue
import time
import re
import threading

# ── OCR engine selector ──────────────────────────────────────────────────────
# Set to "manga" for manga-ocr (best accuracy, uses ML model, slower first run)
# Set to "windows" for Windows Native OCR (fast, no extra model download)
OCR_ENGINE = "windows"
# ─────────────────────────────────────────────────────────────────────────────

# ── Window capture crop (px to trim from captured window edges) ──────────────
CROP_TOP = 30
CROP_BOTTOM = 10
CROP_LEFT = 10
CROP_RIGHT = 10

# torch MUST be imported before tkinter on Windows to avoid c10.dll WinError 1114
if OCR_ENGINE == "manga":
    try:
        import torch  # noqa: F401 - side-effect import fixes DLL load order
    except Exception:
        pass

import os
import tkinter as tk
from PIL import Image, ImageTk
import mss
import winocr
import pykakasi
from deep_translator import GoogleTranslator
from concurrent.futures import ThreadPoolExecutor
import webbrowser
import urllib.parse

# Lazy-initialised manga-ocr instance (loaded only if OCR_ENGINE == "manga")
_manga_ocr = None
_manga_ocr_lock = threading.Lock()

def get_manga_ocr():
    global _manga_ocr
    if _manga_ocr is None:
        with _manga_ocr_lock:
            if _manga_ocr is None:
                from manga_ocr import MangaOcr
                _manga_ocr = MangaOcr()
    return _manga_ocr

def prewarm_manga_ocr():
    """Load manga-ocr model in background so first hotkey press is instant."""
    if OCR_ENGINE == "manga":
        try:
            get_manga_ocr()
            print("manga-ocr model loaded and ready.")
        except Exception as e:
            print(f"manga-ocr failed to pre-load: {e}")


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

# Initialize PyKakasi globally (thread-safe)
# GoogleTranslator is NOT thread-safe, so we instantiate it per-call instead
kks = pykakasi.kakasi()

def contains_japanese(text):
    """Check if the text contains any Japanese characters (Hiragana, Katakana, Kanji)."""
    # Unicode ranges:
    # Hiragana: \u3040-\u309f
    # Katakana: \u30a0-\u30ff
    # Kanji (CJK Unified Ideographs): \u4e00-\u9faf
    pattern = re.compile(r'[\u3040-\u309f\u30a0-\u30ff\u4e00-\u9faf]')
    return bool(pattern.search(text))

def translate_and_convert(japanese_text):
    """Convert Japanese to Romaji, Hiragana, and English translation."""
    try:
        # Get PyKakasi conversions
        result = kks.convert(japanese_text)
        romaji = " ".join([item['hepburn'] for item in result])
        # hira contains hiragana, but we fallback to orig for symbols/numbers
        kana = " ".join([item['hira'] if item['hira'] else item['orig'] for item in result])
        
        # Translate to English — create a fresh instance per call (thread-safe)
        english = GoogleTranslator(source='ja', target='en').translate(japanese_text)
    except Exception as e:
        romaji = "[Error]"
        kana = japanese_text
        english = f"Translation error: {e}"
        
    return {
        'original': japanese_text,
        'romaji': romaji,
        'kana': kana,
        'english': english
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
        
        # Start checking the queue for trigger events or translation results
        self.root.after(100, self.check_queue)
        self.root.after(500, self.check_focus)

    def check_queue(self):
        try:
            while True:
                msg_type, data = self.msg_queue.get_nowait()
                if msg_type == "trigger":
                    self.freeze_screen()
                elif msg_type == "ocr_complete":
                    self.display_translations(data)
        except queue.Empty:
            pass
        self.root.after(50, self.check_queue)

    def trigger(self):
        self.msg_queue.put(("trigger", None))

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

    def process_ocr(self, pil_img, win_local):
        """Run OCR on the focused window crop only, then offset boxes to screen coords."""
        win_x, win_y = win_local['x'], win_local['y']
        win_crop = pil_img.crop((win_x, win_y, win_x + win_local['w'], win_y + win_local['h']))
        
        OCR_SCALE = 5
        try:
            # Upscale the focused window crop for better OCR accuracy
            ocr_img = win_crop.resize(
                (win_crop.width * OCR_SCALE, win_crop.height * OCR_SCALE),
                Image.LANCZOS
            )
            ocr_res = winocr.recognize_pil_sync(ocr_img, 'ja')
            lines = ocr_res.get('lines', [])
            # Scale bboxes from OCR space -> overlay space (no offset needed;
            # the crop area is exactly the overlay window area)
            for line in lines:
                for word in line.get('words', []):
                    br = word['bounding_rect']
                    br['x'] /= OCR_SCALE
                    br['y'] /= OCR_SCALE
                    br['width']  /= OCR_SCALE
                    br['height'] /= OCR_SCALE
            
            # Prepare targets — Windows OCR provides bounding boxes for layout.
            # If using manga-ocr, we still use Windows boxes but replace the text
            # with manga-ocr's superior recognition on each crop.
            translation_targets = []
            for line in lines:
                win_text = line.get('text', '').strip()
                bbox = get_line_bounding_rect(line)  # now in overlay coords

                # Collect word-level data for selectable regions
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

                if OCR_ENGINE == "manga":
                    # Crop from full monitor image (offset overlay coords by win_x/y)
                    crop_pil = pil_img.crop((
                        max(0, int(bbox['x'] + win_x)),
                        max(0, int(bbox['y'] + win_y)),
                        min(pil_img.width,  int(bbox['x'] + bbox['width'] + win_x)),
                        min(pil_img.height, int(bbox['y'] + bbox['height'] + win_y))
                    ))
                    # Skip tiny/empty crops
                    if crop_pil.width < 4 or crop_pil.height < 4:
                        continue
                    mocr = get_manga_ocr()
                    text = mocr(crop_pil)
                else:
                    text = win_text
                    crop_pil = None

                text = text.strip()
                if not text or not contains_japanese(text):
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
                        h = bbox['height'] + 110 + (len(res['english']) // 40) * 16
                        
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
        self.canvas.bind("<Button-2>", self.on_middle_click)

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

    def clear_hover_translation(self):
        if self.hover_window_id:
            self.canvas.delete(self.hover_window_id)
            self.hover_window_id = None
        for ref in self.highlight_refs:
            self.canvas.itemconfig(ref, outline="#007aff", width=2)
        self.current_hover_idx = -1

    def show_hover_translation(self, box):
        data = box['data']
        crop_pil = box['crop_pil']
        orig = box['orig_bbox']
        w = box['w']
        h = box['h']

        idx = self.ocr_boxes.index(box)
        if 0 <= idx < len(self.highlight_refs):
            self.canvas.itemconfig(self.highlight_refs[idx], outline="#ff9500", width=3)

        screen_w = self.canvas.winfo_width()
        screen_h = self.canvas.winfo_height()

        x = orig['x'] + (orig['width'] - w) // 2
        y = orig['y'] + orig['height'] + 8

        if y + h > screen_h:
            y = orig['y'] - h - 8
            if y < 0:
                y = (screen_h - h) // 2
                x = (screen_w - w) // 2

        x = max(0, min(x, screen_w - w))
        y = max(0, min(y, screen_h - h))

        frame = tk.Frame(
            self.canvas, bg="#ffffff", padx=8, pady=6,
            highlightbackground="#e5e5ea", highlightcolor="#e5e5ea",
            highlightthickness=1, bd=0
        )

        crop_tk = ImageTk.PhotoImage(crop_pil)
        self.crop_tk_imgs.append(crop_tk)
        tk.Label(frame, image=crop_tk, bg="#ffffff", bd=0).pack(anchor="w", pady=(0, 4))
        tk.Label(frame, text=data['original'], fg="#a31515", bg="#ffffff",
                 font=("Segoe UI", 11, "bold"), anchor="w", justify="left"
                 ).pack(fill="x", pady=(0, 2))
        tk.Label(frame, text=data['kana'], fg="#248a3d", bg="#ffffff",
                 font=("Segoe UI", 10, "normal"), anchor="w", justify="left"
                 ).pack(fill="x", pady=(0, 2))
        tk.Label(frame, text=data['romaji'], fg="#0066cc", bg="#ffffff",
                 font=("Segoe UI", 9, "italic"), anchor="w", justify="left"
                 ).pack(fill="x", pady=(0, 2))
        tk.Label(frame, text=data['english'], fg="#1c1c1e", bg="#ffffff",
                 font=("Segoe UI", 11, "bold"), anchor="w", justify="left",
                 wraplength=w - 16
                 ).pack(fill="x")

        self.hover_window_id = self.canvas.create_window(x, y, window=frame, anchor="nw")

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
            # Click (no drag) → read the whole line aloud
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

    HOTKEY_ID = 1
    mods = MOD_CONTROL | MOD_ALT | MOD_SHIFT | MOD_NOREPEAT

    if not user32.RegisterHotKey(None, HOTKEY_ID, mods, ord('E')):
        err = ctypes.get_last_error()
        print(f"[DEBUG] RegisterHotKey failed with error {err}. Falling back to GetAsyncKeyState polling.")

        # ── Fallback: GetAsyncKeyState polling ──────────────────────────────
        def is_key_down(vk):
            return bool(user32.GetAsyncKeyState(vk) & 0x8000)

        VK_MAP = {
            "ctrl":  (0xA2, 0xA3),   # L/R CONTROL
            "alt":   (0xA4, 0xA5),   # L/R MENU (Alt)
            "shift": (0xA0, 0xA1),   # L/R SHIFT
            "e":     (0x45, None),
        }
        pressed = False
        while True:
            ctrl  = is_key_down(VK_MAP["ctrl"][0])  or is_key_down(VK_MAP["ctrl"][1])
            alt   = is_key_down(VK_MAP["alt"][0])   or is_key_down(VK_MAP["alt"][1])
            shift = is_key_down(VK_MAP["shift"][0]) or is_key_down(VK_MAP["shift"][1])
            e     = is_key_down(VK_MAP["e"][0])
            if ctrl and alt and shift and e:
                if not pressed:
                    pressed = True
                    app.trigger()
            else:
                pressed = False
            time.sleep(0.05)
        return

    print("[DEBUG] RegisterHotKey succeeded – waiting for WM_HOTKEY.")
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0):
        if msg.message == WM_HOTKEY and msg.wParam == HOTKEY_ID:
            app.trigger()
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))

def main():
    app = ScreenFreezerApp()

    # Start Win32 hotkey thread (RegisterHotKey with fallback to GetAsyncKeyState)
    threading.Thread(target=register_hotkey_win32, args=(app,), daemon=True).start()

    print("Application started. Press Ctrl+Alt+Shift+E to freeze and translate.")
    print("Press Escape while frozen to unfreeze and restore focus.")

    # Pre-warm the manga-ocr model in the background so first use is instant
    threading.Thread(target=prewarm_manga_ocr, daemon=True).start()

    try:
        app.run()
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    main()
