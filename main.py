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

# ── Redirect all library stderr to a log file ─────────────────────────────────
import warnings
warnings.filterwarnings("ignore", message="No ccache found")
import sys
import msvcrt
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
STD_ERROR_HANDLE = -12
_log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.log")
_log_fd = os.open(_log_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
_log_handle = msvcrt.get_osfhandle(_log_fd)
kernel32.SetStdHandle(STD_ERROR_HANDLE, _log_handle)
os.dup2(_log_fd, 2)
os.close(_log_fd)
sys.stderr = open(_log_path, "a", encoding="utf-8")
# ─────────────────────────────────────────────────────────────────────────────

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

# ── Box padding (px) — extra invisible area around OCR boxes for easier mouse aiming ─
BOX_PAD = 2
BORDER_WIDTH = 5

# ── Card background color ────────────────────────────────────────────────────
CARD_BG = "#f2f2f7"



import tkinter as tk
import tkinter.font as tkfont
from PIL import Image, ImageTk
import mss
import jaconv
import pykakasi
from jamdict import Jamdict
from sudachipy import Dictionary, SplitMode
from deep_translator import GoogleTranslator
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
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
            pass
            _deepl_api_key = ""
    return _deepl_api_key

# ── PaddleOCR lazy loader ──────────────────────────────────────────────────
_paddle_ocr = None

def get_paddle_ocr():
    global _paddle_ocr
    if _paddle_ocr is None:
        import logging
        from paddleocr import PaddleOCR
        logging.getLogger("ppocr").setLevel(logging.WARNING)
        logging.getLogger("paddlex").setLevel(logging.WARNING)
        try:
            import onnxruntime
            providers = onnxruntime.get_available_providers()
            if 'CUDAExecutionProvider' in providers:
                print("GPU acceleration enabled (CUDA)")
            elif 'TensorrtExecutionProvider' in providers:
                print("GPU acceleration enabled (TensorRT)")
            else:
                print("GPU acceleration not available, using CPU")
        except Exception:
            print("GPU acceleration not available, using CPU")
        _paddle_ocr = PaddleOCR(
            lang='japan', engine='onnxruntime',
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
            return_word_box=False,
            text_det_thresh=0.4,
            text_det_box_thresh=0.6,
            text_recognition_batch_size=16,
        )
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

_deepl_cache = {}

def _translate_or_cached(text):
    """Return cached DeepL result or translate and cache it."""
    if text in _deepl_cache:
        return _deepl_cache[text]
    result = translate_deepl(text)
    _deepl_cache[text] = result
    return result

def translate_deepl_batch(texts):
    """Translate a batch of Japanese texts to English via a single DeepL API call."""
    if not texts:
        return []
    import requests
    key = get_deepl_api_key()
    if not key:
        return ["[DeepL: no API key - add to deeplapikey.txt]"] * len(texts)
    resp = requests.post(
        "https://api-free.deepl.com/v2/translate",
        headers={"Authorization": f"DeepL-Auth-Key {key}"},
        json={"text": texts, "source_lang": "JA", "target_lang": "EN"}
    )
    if resp.status_code == 403:
        return ["[DeepL: Unauthorized - check your API key]"] * len(texts)
    resp.raise_for_status()
    return [t["text"] for t in resp.json()["translations"]]

# Win32 constants for click-through overlay
WS_EX_LAYERED = 0x80000
WS_EX_TRANSPARENT = 0x20
WS_EX_NOACTIVATE = 0x08000000
GWL_EXSTYLE = -20
LWA_COLORKEY = 0x1
LWA_ALPHA = 0x2
RGN_OR = 2
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010

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
_jam_db_path = None
def _get_jam():
    global _jam_db_path
    if not hasattr(_jam_local, 'jam'):
        _jam_local.jam = Jamdict()
        if _jam_db_path is None:
            _jam_db_path = getattr(_jam_local.jam, '_Jamdict__db_file', None)
    return _jam_local.jam

def _get_english_meanings(literal):
    """Query jamdict SQLite directly for English-only kanji meanings."""
    db = _jam_db_path
    if not db:
        return []
    try:
        import sqlite3
        conn = sqlite3.connect(db)
        cur = conn.cursor()
        cur.execute("""
            SELECT DISTINCT m.value FROM meaning m
            JOIN rm_group g ON m.gid = g.ID
            JOIN character c ON g.cid = c.ID
            WHERE c.literal = ? AND (m.m_lang IS NULL OR m.m_lang = '')
        """, (literal,))
        res = [row[0] for row in cur.fetchall()]
        conn.close()
        return res
    except Exception:
        return []

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

def _is_kana(ch):
    """Check if a character is Hiragana or Katakana."""
    return '\u3040' <= ch <= '\u30ff'

def _segment_jp(text):
    """Split text into runs of (segment, is_japanese)."""
    segments = []
    cur = ""
    in_jp = None
    for ch in text:
        is_jp = _is_kanji(ch) or _is_kana(ch)
        if in_jp is None:
            in_jp = is_jp
            cur = ch
        elif is_jp == in_jp:
            cur += ch
        else:
            segments.append((cur, in_jp))
            cur = ch
            in_jp = is_jp
    if cur:
        segments.append((cur, in_jp))
    return segments

def _build_alternatives(orig, sudachi_hira):
    """Build list of alternative readings for a token using jamdict."""
    # Non-kanji tokens (kana/punctuation/symbols): no alternatives needed
    if not any(_is_kanji(c) for c in orig):
        h_hepburn = " ".join([r['hepburn'] for r in kks.convert(sudachi_hira)])
        return [{'hira': sudachi_hira, 'hepburn': h_hepburn, 'type': 'unknown'}]

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
                        raw = jaconv.kata2hira(str(r))
                        if raw.startswith('-') or raw.endswith('-'):
                            continue  # compound-only suffix/prefix reading
                        clean = raw.split('.')[0].replace('-', '')
                        if clean:
                            _add(clean, 'kun')
                if hasattr(ch, 'nanoris') and ch.nanoris:
                    for n in ch.nanoris:
                        _add(jaconv.kata2hira(str(n)), 'nanori')
        except Exception:
            pass

    # Context-based filter: keep relevant reading types
    # XXX: May need to revert this filtering if it causes too many missing readings
    kanji_count = sum(1 for c in orig if _is_kanji(c))
    default = alts[0]
    if kanji_count > 1:
        # Compound: only keep word-level readings (JMDict/Sudachi).
        # Per-character on/kun/nanori don't form valid compound readings.
        alts = [a for a in alts if a['type'] == 'unknown']
    elif kanji_count == 1:
        # Standalone kanji: drop nanori and on for inflected verb forms
        if orig and not _is_kanji(orig[-1]):
            alts = [a for a in alts if a['type'] not in ('nanori', 'on')]
            # Drop bare stem readings shorter than the full inflected form
            # (e.g. ねが for 願え would lose the え in the romaji)
            sudachi_len = len(sudachi_hira)
            alts = [a for a in alts
                    if a is alts[0] or len(a['hira']) >= sudachi_len]
    if default not in alts:
        alts.insert(0, default)
    return alts


# ── Number+counter reading correction ─────────────────────────────────────

_COUNTER_READINGS = {
    # (number_key, counter_char) → reading
    # number_key: str of the number as it appears, '*' for general rule
    '1人': 'ひとり', '2人': 'ふたり', '3人': 'さんにん', '4人': 'よにん', '5人': 'ごにん',
    '6人': 'ろくにん', '7人': 'しちにん', '8人': 'はちにん', '9人': 'きゅうにん', '10人': 'じゅうにん',
    '何人': 'なんにん',
    '1日': 'ついたち', '2日': 'ふつか', '3日': 'みっか', '4日': 'よっか', '5日': 'いつか',
    '6日': 'むいか', '7日': 'なのか', '8日': 'ようか', '9日': 'ここのか', '10日': 'とおか',
    '14日': 'じゅうよっか', '20日': 'はつか', '24日': 'にじゅうよっか',
    '1月': 'いちがつ', '2月': 'にがつ', '3月': 'さんがつ', '4月': 'しがつ', '5月': 'ごがつ',
    '6月': 'ろくがつ', '7月': 'しちがつ', '8月': 'はちがつ', '9月': 'くがつ', '10月': 'じゅうがつ',
    '11月': 'じゅういちがつ', '12月': 'じゅうにがつ',
    '1個': 'いっこ', '2個': 'にこ', '3個': 'さんこ', '4個': 'よんこ', '5個': 'ごこ',
    '6個': 'ろっこ', '7個': 'ななこ', '8個': 'はっこ', '9個': 'きゅうこ', '10個': 'じゅっこ',
    '1匹': 'いっぴき', '2匹': 'にひき', '3匹': 'さんびき', '4匹': 'よんひき', '5匹': 'ごひき',
    '6匹': 'ろっぴき', '7匹': 'ななひき', '8匹': 'はっぴき', '9匹': 'きゅうひき', '10匹': 'じゅっぴき',
    '1本': 'いっぽん', '2本': 'にほん', '3本': 'さんぼん', '4本': 'よんほん', '5本': 'ごほん',
    '6本': 'ろっぽん', '7本': 'ななほん', '8本': 'はっぽん', '9本': 'きゅうほん', '10本': 'じゅっぽん',
    '1杯': 'いっぱい', '2杯': 'にはい', '3杯': 'さんばい', '4杯': 'よんはい', '5杯': 'ごはい',
    '6杯': 'ろっぱい', '7杯': 'ななはい', '8杯': 'はっぱい', '9杯': 'きゅうはい', '10杯': 'じゅっぱい',
    '1回': 'いっかい', '2回': 'にかい', '3回': 'さんかい', '4回': 'よんかい', '5回': 'ごかい',
    '6回': 'ろっかい', '7回': 'ななかい', '8回': 'はっかい', '9回': 'きゅうかい', '10回': 'じゅっかい',
    '1階': 'いっかい', '2階': 'にかい', '3階': 'さんがい', '4階': 'よんかい', '5階': 'ごかい',
    '6階': 'ろっかい', '7階': 'ななかい', '8階': 'はっかい', '9階': 'きゅうかい', '10階': 'じゅっかい',
    '1歳': 'いっさい', '2歳': 'にさい', '3歳': 'さんさい', '4歳': 'よんさい', '5歳': 'ごさい',
    '6歳': 'ろくさい', '7歳': 'ななさい', '8歳': 'はっさい', '9歳': 'きゅうさい', '10歳': 'じゅっさい',
}

def _number_to_reading(num_str):
    """Convert a numeric string to Japanese kana reading (e.g. '100' → 'ひゃく')."""
    n = int(num_str)
    if n == 0: return 'ゼロ'
    if n == 1: return 'いち'
    if n == 2: return 'に'
    if n == 3: return 'さん'
    if n == 4: return 'よん'
    if n == 5: return 'ご'
    if n == 6: return 'ろく'
    if n == 7: return 'なな'
    if n == 8: return 'はち'
    if n == 9: return 'きゅう'
    if n == 10: return 'じゅう'
    if n == 100: return 'ひゃく'
    if n == 1000: return 'せん'
    if n == 10000: return 'いちまん'
    if 11 <= n <= 99:
        tens = n // 10
        ones = n % 10
        s = 'じゅう'
        if tens > 1: s = _number_to_reading(str(tens)) + s
        if ones: s += _number_to_reading(str(ones))
        return s
    if 101 <= n <= 999:
        hundreds = n // 100
        rest = n % 100
        s = 'ひゃく' if hundreds == 1 else _number_to_reading(str(hundreds)) + 'ひゃく'
        if rest: s += _number_to_reading(str(rest))
        return s
    if 1001 <= n <= 9999:
        thousands = n // 1000
        rest = n % 1000
        s = 'せん' if thousands == 1 else _number_to_reading(str(thousands)) + 'せん'
        if rest: s += _number_to_reading(str(rest))
        return s
    # 10000+ — simplified, just read as まん
    man = n // 10000
    rest = n % 10000
    s = _number_to_reading(str(man)) + 'まん' if man else ''
    if rest: s += _number_to_reading(str(rest))
    return s

def _fix_number_counter(tokens):
    """Post-process Sudachi tokens: correct readings for numbers and number+counter combos.
    Returns list of dicts with keys: surface, reading, hira, hepburn, dict_form."""
    result = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        orig = token.surface()
        if not orig.isdigit():
            result.append({
                'surface': orig,
                'reading': token.reading_form(),
                'hira': None,
                'hepburn': None,
                'dict_form': token.dictionary_form(),
            })
            i += 1
            continue
        # Digit-only token
        next_is_counter = (i + 1 < len(tokens)
                           and len(tokens[i + 1].surface()) == 1
                           and _is_kanji(tokens[i + 1].surface()))
        if next_is_counter:
            next_token = tokens[i + 1]
            next_orig = next_token.surface()
            key = orig + next_orig
            if key in _COUNTER_READINGS:
                reading = _COUNTER_READINGS[key]
                result.append({
                    'surface': orig + next_orig,
                    'reading': reading,
                    'hira': reading,
                    'hepburn': None,
                    'dict_form': orig + next_orig,
                })
                i += 2
                continue
            # No special rule: combine number reading + counter's own reading
            num_reading = _number_to_reading(orig)
            counter_reading = jaconv.kata2hira(next_token.reading_form())
            combined_reading = num_reading + counter_reading
            result.append({
                'surface': orig + next_orig,
                'reading': combined_reading,
                'hira': combined_reading,
                'hepburn': None,
                'dict_form': orig + next_orig,
            })
            i += 2
            continue
        # Standalone number
        num_reading = _number_to_reading(orig)
        result.append({
            'surface': orig,
            'reading': num_reading,
            'hira': num_reading,
            'hepburn': None,
            'dict_form': orig,
        })
        i += 1
    return result


@lru_cache(maxsize=2048)
def _process_japanese(text):
    """Local processing: tokenize, convert to romaji/kana. Pure function (cached)."""
    try:
        tokens = _get_sudachi().tokenize(text, SplitMode.C)
    except Exception:
        return (text, '[Error]', text, ())

    fixed = _fix_number_counter(tokens)
    items = []  # list of (orig, dict_form, hira, hepburn, alternatives, active_idx, no_trail_space)
    for ft in fixed:
        orig = ft['surface']
        reading = ft['reading']
        hira = ft['hira'] or (jaconv.kata2hira(reading) if reading else orig)
        dict_form = orig
        if any(_is_kanji(c) for c in orig):
            alternatives = _build_alternatives(orig, hira)
            items.append((orig, dict_form, alternatives[0]['hira'],
                          alternatives[0]['hepburn'], alternatives, 0, False))
        elif orig.isdigit():
            romaji = " ".join([r['hepburn'] for r in kks.convert(hira)])
            alternatives = ((hira, romaji, 'number'),)
            items.append((orig, dict_form, hira, romaji, alternatives, 0, False))
        elif not any(_is_kana(c) for c in orig):
            alternatives = ((orig, orig, 'unknown'),)
            items.append((orig, dict_form, orig, orig, alternatives, 0, False))
        else:
            alternatives = _build_alternatives(orig, hira)
            items.append((orig, dict_form, alternatives[0]['hira'],
                          alternatives[0]['hepburn'], alternatives, 0, False))

    for i in range(len(items) - 1):
        h1 = items[i][3]
        h2 = items[i+1][3]
        if h1.endswith('tsu') and h2 and h2[0] in 'bcdfghjklmnpqrstvwxyz':
            items[i] = items[i][:3] + (h1[:-3],) + items[i][4:6] + (True,)
            items[i+1] = items[i+1][:3] + (h2[0] + h2,) + items[i+1][4:]

    romaji_parts = []
    for i, item in enumerate(items):
        h = item[3] or item[0]
        if i > 0 and not items[i-1][6]:
            romaji_parts.append(' ')
        romaji_parts.append(h)
    romaji = ''.join(romaji_parts)
    kana = " ".join([item[2] if item[2] else item[0] for item in items])

    return (text, romaji, kana, tuple(items))


def _unpack_items(items_tuple):
    """Convert cached tuple representation back to list-of-dicts format."""
    result = []
    for item in items_tuple:
        orig, df, hira, hep, alts, active, _ = item
        if isinstance(alts, tuple) and len(alts) > 0 and isinstance(alts[0], tuple):
            alts = [{'hira': a[0], 'hepburn': a[1], 'type': a[2]} for a in alts]
        result.append({
            'orig': orig, 'dict_form': df, 'hira': hira,
            'hepburn': hep, 'alternatives': alts, 'active_idx': active,
        })
    return result


def translate_and_convert(japanese_text, do_translate=True):
    """Convert Japanese to Romaji, Hiragana, and English translation."""
    try:
        text, romaji, kana, items_tuple = _process_japanese(japanese_text)
        items = _unpack_items(items_tuple)

        if do_translate:
            if TRANSLATOR == "deepl":
                english = _translate_or_cached(japanese_text)
            else:
                english = GoogleTranslator(source='ja', target='en').translate(japanese_text)
        else:
            english = ""
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
        self.root.withdraw()
        self.msg_queue = queue.Queue()
        self.active = False
        self.pil_img = None
        self.ocr_boxes = []
        self._box_windows = []          # list of (Toplevel, Canvas, idx) per OCR box
        self._card_window = None        # Toplevel for hover card or None
        self._card_canvas = None        # Canvas inside card window
        self.overlay_x = 0              # screen-X of captured game window
        self.overlay_y = 0              # screen-Y of captured game window
        self.overlay_w = 0
        self.overlay_h = 0
        self.current_hover_idx = -1
        self.is_dragging = False
        self._card_data = None
        self._card_data_idx = -1
        self._card_box = None
        self._card_token_positions = []
        self._card_romaji_positions = []
        self._card_hover_char_idx = -1
        self._card_xy = None
        self._dict_lookup_seq = 0
        self._ctrl_held = False
        self._prev_focus_hwnd = None
        self._loading_win = None
        self._overlay_hidden = False
        self._selection_box_idx = -1
        self._selection_start = -1
        self._selection_end = -1
        self.show_crop = True
        self.show_romaji = True
        self.skip_non_japanese = SKIP_NON_JAPANESE
        self.show_translation = True
        self.japanese_font = "Meiryo"
        self.japanese_font_size = 16
        self.font_size_en = 10
        self._settings_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")
        self._settings_window = None
        self._load_settings()

        # Pre-warm PaddleOCR in background so first scan doesn't pay model-load cost
        self._prewarm_event = threading.Event()
        threading.Thread(target=self._prewarm_paddle, daemon=True).start()

        # Start checking the queue for trigger events or translation results
        self.root.after(100, self.check_queue)

    def _prewarm_paddle(self):
        get_paddle_ocr()
        self._prewarm_event.set()

    def _apply_round_corners(self, hwnd, w, h, r=4):
        try:
            rgn = ctypes.windll.gdi32.CreateRoundRectRgn(0, 0, w + 1, h + 1, r, r)
            user32.SetWindowRgn(hwnd, rgn, True)
        except Exception:
            pass

    def _on_ctrl_key(self, held):
        if self._ctrl_held == held:
            return
        self._ctrl_held = held
        self._refresh_highlights()

    def _is_ctrl_held(self):
        try:
            return bool(user32.GetAsyncKeyState(0xA2) & 0x8000) or bool(user32.GetAsyncKeyState(0xA3) & 0x8000)
        except Exception:
            return self._ctrl_held

    def _poll_ctrl_state(self):
        ctrl = self._is_ctrl_held()
        if ctrl != self._ctrl_held:
            self._ctrl_held = ctrl
            self._refresh_highlights()

    def _refresh_highlights(self):
        if self._card_window and self._card_hover_char_idx >= 0:
            self._render_card()
            idx = self.current_hover_idx
            if 0 <= idx < len(self.ocr_boxes):
                self._redraw_box_highlight(idx, self._card_hover_char_idx)

    def _redraw_box_highlight(self, idx, char_off):
        box = self.ocr_boxes[idx]
        bbox = box['orig_bbox']
        words = box.get('words', [])
        _, canvas2 = self._box_windows[idx][:2]
        canvas2.delete("word_hl")
        ki = box['data'].get('kakasi_items', [])
        if not ki:
            return
        if self._is_ctrl_held():
            wcoff = 0
            for w in words:
                wlen = len(w['text'])
                if wcoff <= char_off < wcoff + wlen:
                    lx = w['x'] - bbox['x'] + BOX_PAD
                    ly = w['y'] - bbox['y'] + BOX_PAD
                    canvas2.create_rectangle(
                        lx, ly, lx + w['width'], ly + w['height'],
                        fill="#ffe69c", stipple="gray50", outline="#ffc107", width=1,
                        tags="word_hl")
                    break
                wcoff += wlen
        else:
            ci_off = 0
            chunk_cs = -1
            chunk_ce = -1
            for item in ki:
                orig_len = len(item.get('orig', ''))
                if ci_off <= char_off < ci_off + orig_len:
                    chunk_cs = ci_off
                    chunk_ce = ci_off + orig_len
                    break
                ci_off += orig_len
            if chunk_cs >= 0:
                min_lx = min_ly = max_rx = max_by = None
                wcoff = 0
                for w in words:
                    wlen = len(w['text'])
                    if wcoff < chunk_ce and wcoff + wlen > chunk_cs:
                        lx = w['x'] - bbox['x'] + BOX_PAD
                        ly = w['y'] - bbox['y'] + BOX_PAD
                        rx = lx + w['width']
                        by = ly + w['height']
                        canvas2.create_rectangle(
                            lx, ly, rx, by,
                            fill="#ffe69c", stipple="gray50", outline="", tags="word_hl")
                        if min_lx is None or lx < min_lx: min_lx = lx
                        if min_ly is None or ly < min_ly: min_ly = ly
                        if max_rx is None or rx > max_rx: max_rx = rx
                        if max_by is None or by > max_by: max_by = by
                    wcoff += wlen
                if min_lx is not None:
                    canvas2.create_rectangle(
                        min_lx, min_ly, max_rx, max_by,
                        fill="", outline="#ffc107", width=1, tags="word_hl")

    def check_queue(self):
        try:
            while True:
                msg_type, data = self.msg_queue.get_nowait()
                if msg_type == "trigger":
                    if self.active:
                        self.unfreeze_screen()
                    else:
                        self.freeze_screen()
                elif msg_type == "trigger_snip":
                    self.enter_snip_mode()
                elif msg_type == "ocr_complete":
                    self.display_translations(data)
                elif msg_type == "ocr_boxes_ready":
                    # Dismiss loading overlay on first box arrival
                    if self._loading_win:
                        try:
                            self._loading_win.destroy()
                        except Exception:
                            pass
                        self._loading_win = None
                        self._load_tk_img = None
                    self.display_translations(data)
                elif msg_type == "ocr_data_ready":
                    total = (time.time() - self._ocr_start_time) * 1000
                    ocr_ms = getattr(self, '_last_ocr_ms', 0)
                    proc_ms = getattr(self, '_last_proc_ms', 0)
                    trans_ms = getattr(self, '_last_translation_ms', 0)
                    print(f"OCR: {data} regions, {ocr_ms:.0f}ms")
                    print(f"Processing: {proc_ms:.0f}ms")
                    if trans_ms:
                        print(f"Translation: {trans_ms:.0f}ms")
                    ci = _process_japanese.cache_info()
                    prev_hits = getattr(self, '_prev_cache_hits', 0)
                    prev_misses = getattr(self, '_prev_cache_misses', 0)
                    dh = ci.hits - prev_hits
                    dm = ci.misses - prev_misses
                    self._prev_cache_hits = ci.hits
                    self._prev_cache_misses = ci.misses
                    print(f"Cache: {dh} hits, {dm} misses, {ci.currsize} entries")
                    print(f"DeepL cache: {len(_deepl_cache)} entries")
                    print(f"Total: {total:.0f}ms\n")
                    # Re-render card if currently showing (data was updated in-place)
                    if self._card_window and self._card_data_idx >= 0:
                        self._render_card()
                elif msg_type == "toggle_settings":
                    self.toggle_settings()
        except queue.Empty:
            pass
        self._poll_ctrl_state()
        self.root.after(50, self.check_queue)

    def trigger(self):
        self.msg_queue.put(("trigger", None))

    def trigger_snip(self):
        self.msg_queue.put(("trigger_snip", None))

    def trigger_settings(self):
        self.msg_queue.put(("toggle_settings", None))

    def _load_settings(self):
        try:
            with open(self._settings_file, "r") as f:
                data = json.load(f)
            self.show_crop = data.get("show_crop", True)
            self.show_romaji = data.get("show_romaji", True)
            self.skip_non_japanese = data.get("skip_non_japanese", SKIP_NON_JAPANESE)
            self.show_translation = data.get("show_translation", True)
        except (FileNotFoundError, json.JSONDecodeError):
            pass

    def _save_settings(self):
        data = {
            "show_crop": self.show_crop,
            "show_romaji": self.show_romaji,
            "skip_non_japanese": self.skip_non_japanese,
            "show_translation": self.show_translation,
        }
        with open(self._settings_file, "w") as f:
            json.dump(data, f)

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
        self._show_translation_var = tk.BooleanVar(value=self.show_translation)

        def on_toggle():
            old_translation = self.show_translation
            self.show_crop = self._show_crop_var.get()
            self.show_romaji = self._show_romaji_var.get()
            self.skip_non_japanese = self._skip_nj_var.get()
            self.show_translation = self._show_translation_var.get()
            self._save_settings()
            if old_translation != self.show_translation:
                if self.show_translation:
                    self._retranslate_boxes()
                else:
                    for box in self.ocr_boxes:
                        box['data']['english'] = ""
                    self._refresh_hover_card()
            else:
                self._refresh_hover_card()

        pad = {"padx": 12, "pady": 3}
        tk.Label(win, text="Hover Card", font=("Segoe UI", 9, "bold"),
                 anchor="w").pack(fill="x", padx=12, pady=(10, 2))
        sep = tk.Frame(win, height=1, bg="#c0c0c0")
        sep.pack(fill="x", padx=12)
        tk.Checkbutton(win, text="Show romaji", variable=self._show_romaji_var,
                       command=on_toggle).pack(anchor="w", **pad)
        tk.Checkbutton(win, text="Show translation", variable=self._show_translation_var,
                       command=on_toggle).pack(anchor="w", **pad)

        tk.Label(win, text="OCR Filter", font=("Segoe UI", 9, "bold"),
                 anchor="w").pack(fill="x", padx=12, pady=(10, 2))
        sep2 = tk.Frame(win, height=1, bg="#c0c0c0")
        sep2.pack(fill="x", padx=12)
        tk.Checkbutton(win, text="Skip non-Japanese text", variable=self._skip_nj_var,
                       command=on_toggle).pack(anchor="w", **pad)

        tk.Label(win, text="Debug", font=("Segoe UI", 9, "bold"),
                 anchor="w").pack(fill="x", padx=12, pady=(10, 2))
        sep3 = tk.Frame(win, height=1, bg="#c0c0c0")
        sep3.pack(fill="x", padx=12)
        tk.Checkbutton(win, text="Show cropped image", variable=self._show_crop_var,
                       command=on_toggle).pack(anchor="w", **pad)

        win.update_idletasks()
        win.geometry(f"{win.winfo_reqwidth()}x{win.winfo_reqheight()}")

        win.protocol("WM_DELETE_WINDOW", lambda: (
            setattr(self, '_settings_window', None), win.destroy()
        ))

    def _refresh_hover_card(self):
        self._hide_card()
        if self.current_hover_idx >= 0 and self.current_hover_idx < len(self.ocr_boxes):
            self._show_card(self.current_hover_idx)

    def _retranslate_boxes(self):
        """Re-translate all OCR boxes whose english field is empty."""
        boxes = list(self.ocr_boxes)
        def _do():
            for box in boxes:
                text = box['data'].get('original', '')
                if text and not box['data'].get('english'):
                    try:
                        if TRANSLATOR == "deepl":
                            eng = _translate_or_cached(text)
                        else:
                            eng = GoogleTranslator(source='ja', target='en').translate(text)
                        box['data']['english'] = eng
                    except Exception:
                        pass
            self.root.after(0, self._refresh_hover_card)
        threading.Thread(target=_do, daemon=True).start()

    def freeze_screen(self):
        if self.active:
            self.unfreeze_screen()
            return

        # Capture currently active window handle + its screen bounds
        fg_hwnd = ctypes.windll.user32.GetForegroundWindow()
        self._prev_focus_hwnd = fg_hwnd
        rect = ctypes.wintypes.RECT()
        ctypes.windll.user32.GetWindowRect(fg_hwnd, ctypes.byref(rect))
        win_rect = {'left': rect.left, 'top': rect.top, 'right': rect.right, 'bottom': rect.bottom}

        # Capture the full monitor where the mouse cursor is located
        sct_img, monitor = capture_moused_monitor()
        self.pil_img = Image.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")

        mx_off = monitor['left']
        my_off = monitor['top']
        win_local = {
            'x': max(0, win_rect['left'] - mx_off),
            'y': max(0, win_rect['top']  - my_off),
            'w': min(monitor['width'],  win_rect['right']  - mx_off) - max(0, win_rect['left'] - mx_off),
            'h': min(monitor['height'], win_rect['bottom'] - my_off) - max(0, win_rect['top']  - my_off),
        }
        win_local['x'] += CROP_LEFT
        win_local['y'] += CROP_TOP
        win_local['w'] -= CROP_LEFT + CROP_RIGHT
        win_local['h'] -= CROP_TOP + CROP_BOTTOM

        # Store overlay position for box window placement
        self.overlay_x = win_rect['left'] + CROP_LEFT
        self.overlay_y = win_rect['top'] + CROP_TOP
        self.overlay_w = win_local['w']
        self.overlay_h = win_local['h']
        self.active = True
        self._ocr_start_time = time.time()

        # Loading overlay: captured game region + blue border + status text
        win_crop = self.pil_img.crop((win_local['x'], win_local['y'],
                                      win_local['x'] + win_local['w'],
                                      win_local['y'] + win_local['h']))
        self._loading_win = tk.Toplevel(self.root)
        self._loading_win.overrideredirect(True)
        self._loading_win.geometry(f"{win_local['w']}x{win_local['h']}+{self.overlay_x}+{self.overlay_y}")
        self._loading_win.attributes("-topmost", True)
        lc = tk.Canvas(self._loading_win, width=win_local['w'], height=win_local['h'],
                       borderwidth=0, highlightthickness=0)
        lc.pack()
        load_tk = ImageTk.PhotoImage(win_crop)
        self._load_tk_img = load_tk
        lc.create_image(0, 0, image=load_tk, anchor="nw")
        lc.create_rectangle(0, 0, win_local['w'] - 1, win_local['h'] - 1,
                            outline="#007aff", width=2)
        lc.create_rectangle(win_local['w'] // 2 - 160, 8,
                            win_local['w'] // 2 + 160, 46,
                            fill="#1c1c1e", outline="#3a3a3c", width=2)
        load_text = lc.create_text(win_local['w'] // 2, 25,
                                   text="[ Running OCR / Translation... ]",
                                   fill="#ffffff", font=("Segoe UI", 14, "bold"))
        lc.tag_raise(load_text)
        try:
            self._loading_win.update_idletasks()
            hwnd = user32.GetAncestor(self._loading_win.winfo_id(), 2)
            ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex | WS_EX_LAYERED | WS_EX_TRANSPARENT)
            user32.SetLayeredWindowAttributes(hwnd, 0, 0xDD, LWA_ALPHA)
        except Exception:
            pass

        # Start OCR & Translation in background
        threading.Thread(
            target=self.process_ocr,
            args=(self.pil_img, win_local),
            daemon=True
        ).start()

    # ── Snip mode (drag-select region, Ctrl+Alt+Shift+R) ────────────────────

    def enter_snip_mode(self):
        if self.active:
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

        self.freeze_screen_region(screen_rect, ml, mt)

    def freeze_screen_region(self, screen_rect, mx_off=0, my_off=0):
        """Freeze a user-selected region (screen coords: left, top, right, bottom)."""
        left, top, right, bottom = screen_rect
        w, h = right - left, bottom - top

        self.overlay_x = left
        self.overlay_y = top
        self.overlay_w = w
        self.overlay_h = h
        self.active = True
        self._ocr_start_time = time.time()

        win_local = {
            'x': left - mx_off,
            'y': top - my_off,
            'w': w,
            'h': h,
        }

        threading.Thread(
            target=self.process_ocr,
            args=(self.pil_img, win_local),
            daemon=True
        ).start()

    def run_ocr_paddle(self, win_crop):
        """Run PaddleOCR on the crop, return lines with per-char bounding boxes."""
        ocr = get_paddle_ocr()
        import numpy as np
        img_array = np.array(win_crop)
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
                line_bbox = {
                    'x': min(xs),
                    'y': min(ys),
                    'width': max(xs) - min(xs),
                    'height': max(ys) - min(ys)
                }
                chars = list(text)
                words = []
                weights = [1.0 for ch in chars]
                if weights and chars[-1] in '。、！）」』】〙〛〕\u3001\u3002\uff01\uff09':
                    weights[-1] = 0.3
                total_w = sum(weights)
                accum = 0.0
                for ci, ch in enumerate(chars):
                    cw = weights[ci] / total_w * line_bbox['width']
                    x0 = line_bbox['x'] + int(accum)
                    accum += cw
                    x1 = line_bbox['x'] + int(accum)
                    words.append({
                        'text': ch,
                        'bounding_rect': {
                            'x': x0,
                            'y': line_bbox['y'],
                            'width': x1 - x0,
                            'height': line_bbox['height'],
                        }
                    })
                lines.append({
                    'text': text,
                    'words': words,
                })
        return lines

    def process_ocr(self, pil_img, win_local):
        """Run OCR on the focused window crop only, then offset boxes to screen coords."""
        win_x, win_y = win_local['x'], win_local['y']
        win_crop = pil_img.crop((win_x, win_y, win_x + win_local['w'], win_y + win_local['h']))
        
        try:
            # Phase 0: OCR
            ocr_start = time.time()
            lines = self.run_ocr_paddle(win_crop)
            ocr_time = (time.time() - ocr_start) * 1000
            self._last_ocr_ms = ocr_time

            # Build translation targets and initial placeholder boxes
            translation_targets = []
            boxes = []
            for line in lines:
                text = line.get('text', '').strip()
                bbox = get_line_bounding_rect(line)

                words_data = []
                for word in line.get('words', []):
                    br = word['bounding_rect']
                    wt = word.get('text', '').strip()
                    if wt:
                        words_data.append({
                            'text': wt,
                            'x': br['x'], 'y': br['y'],
                            'width': br['width'], 'height': br['height'],
                        })

                text = text.strip()
                text = re.sub(r'\s+', '', text)
                if not text:
                    continue
                if self.skip_non_japanese and not contains_japanese(text) and not text.isdigit():
                    continue
                translation_targets.append((text, bbox, words_data))

                # Crop image for the overlay window background
                box_crop = pil_img.crop((
                    max(0, int(bbox['x'] + win_x)),
                    max(0, int(bbox['y'] + win_y)),
                    min(pil_img.width,  int(bbox['x'] + bbox['width'] + win_x)),
                    min(pil_img.height, int(bbox['y'] + bbox['height'] + win_y))
                ))

                # Placeholder data for immediate display
                w = min(max(len(text) * 7 + 24, bbox['width'] + 24, 180), 400)
                h = bbox['height'] + 130
                boxes.append({
                    'w': w, 'h': h,
                    'data': {'original': text, 'romaji': '', 'kana': '', 'english': '', 'kakasi_items': []},
                    'orig_bbox': bbox, 'crop_pil': box_crop, 'words': words_data,
                })

            # Send boxes to main thread immediately (progressive rendering)
            self.msg_queue.put(("ocr_boxes_ready", boxes))

            # Phase 1: local processing (parallel, cached)
            proc_start = time.time()
            with ThreadPoolExecutor(max_workers=8) as executor:
                futures = {
                    executor.submit(_process_japanese, text): i
                    for i, (text, _, _) in enumerate(translation_targets)
                }
                for future in futures:
                    i = futures[future]
                    try:
                        text, romaji, kana, items_tuple = future.result()
                        items = _unpack_items(items_tuple)
                        boxes[i]['data'].update({'romaji': romaji, 'kana': kana, 'kakasi_items': items})
                        # Re-estimate box size based on real content
                        max_chars = max(len(text), len(romaji), len(kana))
                        text_w = max_chars * 7 + 24
                        boxes[i]['w'] = min(max(text_w, boxes[i]['orig_bbox']['width'] + 24, 180), 400)
                    except Exception:
                        pass
            self._last_proc_ms = (time.time() - proc_start) * 1000

            # Phase 2: batch translation API (cached)
            if self.show_translation and translation_targets:
                trans_start = time.time()
                texts = [t[0] for t in translation_targets]
                # Batch-translate uncached texts, serve cached ones instantly
                cached_set = _deepl_cache
                translations = [None] * len(texts)
                uncached_texts = []
                uncached_indices = []
                for i, t in enumerate(texts):
                    if t in cached_set:
                        translations[i] = cached_set[t]
                    else:
                        uncached_texts.append(t)
                        uncached_indices.append(i)
                if uncached_texts:
                    batch_results = translate_deepl_batch(uncached_texts)
                    for t, res in zip(uncached_texts, batch_results):
                        cached_set[t] = res
                    for idx, res in zip(uncached_indices, batch_results):
                        translations[idx] = res
                for i, trans in enumerate(translations):
                    if i < len(boxes) and trans is not None:
                        boxes[i]['data']['english'] = trans
                        h_extra = (len(trans) // 40) * 16
                        boxes[i]['h'] = boxes[i]['orig_bbox']['height'] + 130 + h_extra
                self._last_translation_ms = (time.time() - trans_start) * 1000
            else:
                self._last_translation_ms = 0

            # Notify main thread that data is fully ready
            self.msg_queue.put(("ocr_data_ready", len(boxes)))

        except Exception:
            import traceback
            traceback.print_exc()
            self.msg_queue.put(("ocr_boxes_ready", []))

    def display_translations(self, boxes):
        """Create per-box translucent overlay windows from OCR results."""
        if not self.active:
            return

        # Destroy previous box windows
        self._destroy_box_windows()

        # Dismiss loading overlay
        if self._loading_win:
            try:
                self._loading_win.destroy()
            except Exception:
                pass
            self._loading_win = None
            self._load_tk_img = None

        self.ocr_boxes = boxes
        self.current_hover_idx = -1
        self.crop_tk_imgs = []

        for idx, box in enumerate(boxes):
            bbox = box['orig_bbox']
            bx = int(self.overlay_x + bbox['x']) - BOX_PAD
            by = int(self.overlay_y + bbox['y']) - BOX_PAD
            bw = max(int(bbox['width']), 4)
            bh = max(int(bbox['height']), 4)
            win_w = bw + BOX_PAD * 2
            win_h = bh + BOX_PAD * 2

            win = tk.Toplevel(self.root)
            win.overrideredirect(True)
            win.geometry(f"{win_w}x{win_h}+{bx}+{by}")
            win.attributes("-topmost", True)

            canvas = tk.Canvas(win, width=win_w, height=win_h,
                               borderwidth=0, highlightthickness=0, bg="black")
            canvas.pack()

            # Cropped OCR image scaled to fit original box size, offset by BOX_PAD
            crop = box.get('crop_pil')
            if crop:
                crop_resized = crop.resize((bw, bh), Image.LANCZOS)
                crop_tk = ImageTk.PhotoImage(crop_resized)
                self.crop_tk_imgs.append(crop_tk)
                canvas.create_image(BOX_PAD, BOX_PAD, image=crop_tk, anchor="nw")

            # Blue bounding border at full window size (padding included)
            canvas.create_rectangle(0, 0, win_w - 1, win_h - 1,
                                    outline="#007aff", width=BORDER_WIDTH, tags="box_border")

            # Translucency via WS_EX_LAYERED + LWA_ALPHA
            try:
                win.update_idletasks()
                hwnd = user32.GetAncestor(win.winfo_id(), 2)
                ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
                user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex | WS_EX_LAYERED)
                user32.SetLayeredWindowAttributes(hwnd, 0, 0xBB, LWA_ALPHA)
            except Exception:
                pass

            # Mouse bindings
            canvas.bind("<Enter>", lambda e, i=idx: self._box_enter(i))
            canvas.bind("<Leave>", lambda e, i=idx: self._box_leave(i))
            canvas.bind("<Motion>", lambda e, i=idx: self._box_motion(e, i))
            canvas.bind("<MouseWheel>", lambda e, i=idx: self._box_mousewheel(e, i))
            canvas.bind("<Button-1>", lambda e, i=idx: self._box_click(e, i))
            canvas.bind("<B1-Motion>", lambda e, i=idx: self._box_drag(e, i))
            canvas.bind("<ButtonRelease-1>", lambda e, i=idx: self._box_release(e, i))
            canvas.bind("<Button-3>", lambda e, i=idx: self._box_right_click(e, i))
            canvas.bind("<Shift-Button-3>", lambda e, i=idx: self._box_shift_right_click(e, i))
            canvas.bind("<Button-2>", lambda e, i=idx: self._box_middle_click(e, i))

            # Escape on the box window itself
            win.bind("<Escape>", lambda e: self.unfreeze_screen())
            win.bind("<KeyPress-Control_L>", lambda e: self._on_ctrl_key(True))
            win.bind("<KeyPress-Control_R>", lambda e: self._on_ctrl_key(True))
            win.bind("<KeyRelease-Control_L>", lambda e: self._on_ctrl_key(False))
            win.bind("<KeyRelease-Control_R>", lambda e: self._on_ctrl_key(False))

            self._box_windows.append((win, canvas, idx))

    def _destroy_box_windows(self):
        """Destroy all box windows and hide card."""
        self._hide_card()
        for win, _, _ in self._box_windows:
            try:
                win.destroy()
            except Exception:
                pass
        self._box_windows = []

    def _box_enter(self, idx):
        """Mouse entered a box window → highlight it and show card."""
        if self.current_hover_idx != idx:
            self._hide_card()
            self.current_hover_idx = idx
            try:
                self._box_windows[idx][0].focus_force()
            except Exception:
                pass
        # Highlight this box, reset others
        for _, canvas, i in self._box_windows:
            color = "#ff9500" if i == idx else "#007aff"
            w = BORDER_WIDTH
            try:
                canvas.itemconfig("box_border", outline=color, width=w)
                canvas.delete("word_hl")
            except Exception:
                pass
        self._show_card(idx)

    def _box_leave(self, idx):
        """Mouse left a box window → reset highlights and hide card."""
        if self.is_dragging:
            return
        self._hide_card()
        for _, canvas, _ in self._box_windows:
            try:
                canvas.itemconfig("box_border", outline="#007aff", width=BORDER_WIDTH)
                canvas.delete("word_hl")
            except Exception:
                pass

    def _box_click(self, event, idx):
        """Mouse down on a box → start word selection."""
        if idx < 0 or idx >= len(self.ocr_boxes):
            return
        # Clear previous selection highlight (inner canvas)
        _, canvas = self._box_windows[idx][:2]
        canvas.delete("sel_hl")
        self._selection_box_idx = -1
        self._selection_start = -1
        self._selection_end = -1
        box = self.ocr_boxes[idx]
        bbox = box['orig_bbox']
        words = box.get('words', [])
        if not words:
            return
        ox = bbox['x'] - BOX_PAD + event.x
        oy = bbox['y'] - BOX_PAD + event.y
        wi = -1
        for i, w in enumerate(words):
            if w['x'] <= ox <= w['x'] + w['width'] and \
               w['y'] <= oy <= w['y'] + w['height']:
                wi = i
                break
        if wi < 0:
            return
        self._selection_box_idx = idx
        self._selection_start = wi
        self._selection_end = wi
        self.is_dragging = True
        self._click_time = time.time()
        self._click_char_off = sum(len(words[j]['text']) for j in range(wi))

    def _box_drag(self, event, idx):
        """Drag on a box → extend word selection with visual highlight."""
        if not self.is_dragging or self._selection_box_idx != idx:
            return
        if idx < 0 or idx >= len(self.ocr_boxes):
            return
        box = self.ocr_boxes[idx]
        bbox = box['orig_bbox']
        words = box.get('words', [])
        if not words:
            return
        ox = bbox['x'] - BOX_PAD + event.x
        oy = bbox['y'] - BOX_PAD + event.y
        wi = -1
        for i, w in enumerate(words):
            if w['x'] <= ox <= w['x'] + w['width'] and \
               w['y'] <= oy <= w['y'] + w['height']:
                wi = i
                break
        if wi < 0:
            self._withdraw_dict_card()
            return
        self._selection_end = wi
        # Redraw selection highlight on inner canvas
        _, canvas = self._box_windows[idx][:2]
        canvas.delete("sel_hl")
        bbox = self.ocr_boxes[idx]['orig_bbox']
        start = min(self._selection_start, self._selection_end)
        end = max(self._selection_start, self._selection_end)
        for wi2 in range(start, end + 1):
            if wi2 < len(words):
                w = words[wi2]
                lx = w['x'] - bbox['x'] + BOX_PAD
                ly = w['y'] - bbox['y'] + BOX_PAD
                canvas.create_rectangle(lx, ly, lx + w['width'], ly + w['height'],
                                    fill="#007aff", stipple="gray50", outline="",
                                    tags="sel_hl")

    def _box_release(self, event, idx):
        """Mouse release on a box → finalize selection."""
        if not self.is_dragging or self._selection_box_idx != idx:
            return
        self.is_dragging = False
        if self._selection_start < 0 or self._selection_end < 0:
            return
        start = min(self._selection_start, self._selection_end)
        end = max(self._selection_start, self._selection_end)
        words = self.ocr_boxes[idx].get('words', [])
        if start == end:
            if hasattr(self, '_click_char_off'):
                self._card_hover_char_idx = self._click_char_off
                ctrl = self._is_ctrl_held()
                self._update_dict_card(single_char=ctrl)
            _, canvas2 = self._box_windows[idx][:2]
            canvas2.delete("sel_hl")
            self._selection_box_idx = -1
            self._selection_start = -1
            self._selection_end = -1
        else:
            # Drag → copy selected text to clipboard, keep highlight
            selected = ''.join(w['text'] for wi, w in enumerate(words) if start <= wi <= end)
            if selected:
                self.root.clipboard_clear()
                self.root.clipboard_append(selected)

    def _get_selected_text(self):
        """Return the currently selected text, or None."""
        box_idx = self._selection_box_idx
        if box_idx < 0 or self._selection_start < 0 or self._selection_end < 0:
            return None
        start = min(self._selection_start, self._selection_end)
        end = max(self._selection_start, self._selection_end)
        words = self.ocr_boxes[box_idx].get('words', [])
        return ''.join(w['text'] for wi, w in enumerate(words) if start <= wi <= end)

    def _get_hovered_chunk_text(self):
        """Return the orig text of the hovered kakasi chunk, or None."""
        if self._card_hover_char_idx < 0 or not self._card_data:
            return None
        ki = self._card_data.get('kakasi_items', [])
        coff = 0
        for item in ki:
            orig = item.get('orig', '')
            if coff <= self._card_hover_char_idx < coff + len(orig):
                return orig.strip()
            coff += len(orig)
        return None

    def _get_hovered_chunk_dict_form(self):
        """Return the dictionary form of the hovered kakasi chunk, or orig text if none, or None."""
        if self._card_hover_char_idx < 0 or not self._card_data:
            return None
        ki = self._card_data.get('kakasi_items', [])
        coff = 0
        for item in ki:
            orig = item.get('orig', '')
            if coff <= self._card_hover_char_idx < coff + len(orig):
                return item.get('dict_form', orig).strip()
            coff += len(orig)
        return None

    def _get_hovered_single_char(self):
        """Return the single character at the hovered position, or None."""
        if self._card_hover_char_idx < 0 or not self._card_data:
            return None
        ki = self._card_data.get('kakasi_items', [])
        coff = 0
        for item in ki:
            orig = item.get('orig', '')
            if coff <= self._card_hover_char_idx < coff + len(orig):
                offset = self._card_hover_char_idx - coff
                if 0 <= offset < len(orig):
                    return orig[offset]
                return None
            coff += len(orig)
        return None

    def _update_dict_card(self, single_char=False):
        """Start async dictionary lookup for the hovered word."""
        if self._card_hover_char_idx < 0 or not self._card_box:
            self._withdraw_dict_card()
            return

        word = self._get_hovered_single_char() if single_char else self._get_hovered_chunk_dict_form()
        if not word or not contains_japanese(word):
            self._withdraw_dict_card()
            return

        card_w = max(self._card_box.get('w', 200), 200, 340)
        self._dict_lookup_seq += 1
        seq = self._dict_lookup_seq
        self._withdraw_dict_card()

        import threading
        t = threading.Thread(target=self._dict_lookup_thread, args=(word, card_w, seq, single_char), daemon=True)
        t.start()

    def _dict_lookup_thread(self, word, card_w, seq, single_char=False):
        """Background thread: perform jamdict lookup and post result to main thread."""
        try:
            jam = _get_jam()
            res = jam.lookup(word)

            kanji_data = []
            kanji_chars = [c for c in word if _is_kanji(c)]
            for uk in set(kanji_chars):
                char_obj = None
                for c_obj in res.chars:
                    if c_obj.literal == uk:
                        char_obj = c_obj
                        break
                if not char_obj:
                    try:
                        char_res = jam.lookup(uk)
                        if char_res.chars:
                            char_obj = char_res.chars[0]
                    except Exception:
                        pass
                if char_obj:
                    kanji_data.append(char_obj)

            if single_char:
                if not kanji_data:
                    self.root.after(0, self._dict_lookup_skip, seq)
                    return
            elif not res.entries:
                self.root.after(0, self._dict_lookup_skip, seq)
                return

            self.root.after(0, self._dict_lookup_show, word, card_w, seq, res, kanji_data, single_char)
        except Exception:
            self.root.after(0, self._dict_lookup_skip, seq)

    def _dict_lookup_show(self, word, card_w, seq, res, kanji_data, single_char=False):
        """Main thread: render dict card from lookup results."""
        if seq != self._dict_lookup_seq:
            return
        current = self._get_hovered_single_char() if single_char else self._get_hovered_chunk_dict_form()
        if current != word:
            self._withdraw_dict_card()
            return

        if not hasattr(self, '_dict_window') or not self._dict_window:
            self._dict_window = tk.Toplevel(self.root)
            self._dict_window.overrideredirect(True)
            self._dict_window.attributes("-topmost", True)
            self._dict_canvas = tk.Canvas(self._dict_window, borderwidth=0, highlightthickness=0, bg=CARD_BG)
            self._dict_canvas.pack(fill="both", expand=True)
            self._dict_window.bind("<Escape>", lambda e: self.unfreeze_screen())
            try:
                self._dict_window.update_idletasks()
                hwnd = user32.GetAncestor(self._dict_window.winfo_id(), 2)
                ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
                user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex | WS_EX_LAYERED)
                user32.SetLayeredWindowAttributes(hwnd, 0, 0xFA, LWA_ALPHA)
            except Exception:
                pass
        else:
            try:
                self._dict_window.deiconify()
            except Exception:
                pass

        canvas = self._dict_canvas
        canvas.delete("all")

        if single_char:
            card_w = max(card_w, 400)

        title_font = (self.japanese_font, 11, "bold")
        body_font = ("Segoe UI", 10)
        pos_font = ("Segoe UI", 8, "italic")
        kanji_info_font = (self.japanese_font, 9)
        jp_font = (self.japanese_font, 10)

        tf = tkfont.Font(font=title_font)
        bf = tkfont.Font(font=body_font)
        pf = tkfont.Font(font=pos_font)
        jf = tkfont.Font(font=jp_font)
        title_h = tf.metrics("linespace")
        body_h = bf.metrics("linespace")
        pos_h = pf.metrics("linespace")
        jp_h = jf.metrics("linespace")

        def _nlines(font_obj, text, wrap_width):
            if not text:
                return 0
            tw = font_obj.measure(text)
            return max(1, -(-tw // wrap_width))

        pad_x = 8
        ly = 6
        wrap_w = max(card_w - 16, 180)
        wrap_w_inner = max(card_w - 22, 170)

        if not single_char:
            entries = list(res.entries)
            entries.sort(key=lambda e: (
                0 if any(p == 'particle' for s in e.senses for p in (s.pos or []))
                else (1 if not e.kanji_forms else 2)
            ))
            for entry in entries[:4]:
                kanji_texts = [k.text for k in entry.kanji_forms]
                kana_texts = [k.text for k in entry.kana_forms]
                header = ""
                if kanji_texts:
                    header += " / ".join(kanji_texts)
                if kana_texts:
                    if header:
                        header += f" ({', '.join(kana_texts)})"
                    else:
                        header += ", ".join(kana_texts)

                canvas.create_text(pad_x, ly, text=header, font=title_font, fill="#0066cc", anchor="nw", width=wrap_w)
                ly += _nlines(tf, header, wrap_w) * title_h + 4

                for si, sense in enumerate(entry.senses[:3]):
                    glosses = ", ".join(g.text for g in sense.gloss)
                    pos = " • ".join(sense.pos) if sense.pos else ""

                    if pos:
                        canvas.create_text(pad_x + 6, ly, text=pos, font=pos_font, fill="#8e8e93", anchor="nw", width=wrap_w_inner)
                        ly += _nlines(pf, pos, wrap_w_inner) * pos_h + 2

                    def_text = f"{si + 1}. {glosses}"
                    canvas.create_text(pad_x + 6, ly, text=def_text, font=body_font, fill="#1c1c1e", anchor="nw", width=wrap_w_inner)
                    ly += _nlines(bf, def_text, wrap_w_inner) * body_h + 4

        kanji_chars = [c for c in word if _is_kanji(c)]
        if single_char and kanji_chars:
            unique_kanjis = []
            seen_k = set()
            for c in kanji_chars:
                if c not in seen_k:
                    seen_k.add(c)
                    unique_kanjis.append(c)

            for ui, uk in enumerate(unique_kanjis):
                if ui > 0:
                    ly += 6
                    canvas.create_line(pad_x, ly, card_w - pad_x, ly, fill="#e5e5ea")
                    ly += 8

                char_obj = None
                for c_obj in kanji_data:
                    if c_obj.literal == uk:
                        char_obj = c_obj
                        break

                if char_obj:
                    grade = getattr(char_obj, 'grade', None)
                    jlpt = getattr(char_obj, 'jlpt', None)
                    strokes = getattr(char_obj, 'stroke_count', None)

                    try:
                        if grade is not None:
                            grade = int(grade)
                    except (ValueError, TypeError):
                        grade = None
                    try:
                        if jlpt is not None:
                            jlpt = int(jlpt)
                    except (ValueError, TypeError):
                        jlpt = None

                    jlpt_str = ""
                    if jlpt is not None:
                        if jlpt == 4:
                            jlpt_str = "N5"
                        elif jlpt == 3:
                            jlpt_str = "N4"
                        elif jlpt == 2:
                            jlpt_str = "N3/N2"
                        elif jlpt == 1:
                            jlpt_str = "N1"
                        else:
                            jlpt_str = f"L{jlpt}"

                    # Blue kanji title
                    canvas.create_text(pad_x, ly, text=f"{uk} — Kanji Info", font=title_font, fill="#0066cc", anchor="nw")
                    ly += title_h + 4

                    # Meta info line (JLPT, Grade, strokes)
                    meta_parts = []
                    if jlpt_str:
                        meta_parts.append(f"JLPT: {jlpt_str}")
                    if grade is not None:
                        grade_str = f"G{grade}"
                        if 1 <= grade <= 6:
                            grade_str += " (Elem)"
                        elif grade == 8:
                            grade_str += " (Sec)"
                        meta_parts.append(f"Grade: {grade_str}")
                    if strokes is not None:
                        meta_parts.append(f"{strokes} strokes")
                    if meta_parts:
                        meta_text = " • ".join(meta_parts)
                        canvas.create_text(pad_x + 6, ly, text=meta_text, font=pos_font, fill="#8e8e93", anchor="nw", width=wrap_w_inner)
                        ly += _nlines(pf, meta_text, wrap_w_inner) * pos_h + 4

                    # Meanings
                    try:
                        eng = _get_english_meanings(uk)
                        if eng:
                            content = ", ".join(eng)
                            canvas.create_text(pad_x + 6, ly, text=content, font=body_font, fill="#1c1c1e", anchor="nw", width=wrap_w_inner)
                            ly += _nlines(bf, content, wrap_w_inner) * body_h + 4
                    except Exception:
                        pass

                    # On and Kun readings
                    try:
                        rm_groups = getattr(char_obj, 'rm_groups', [])
                        if rm_groups:
                            on_all = []
                            kun_all = []
                            for g in rm_groups:
                                for r in getattr(g, 'on_readings', []) or []:
                                    on_all.append(str(r))
                                for r in getattr(g, 'kun_readings', []) or []:
                                    kun_all.append(str(r))
                            if on_all:
                                on_content = " • ".join(on_all)
                                canvas.create_text(pad_x + 6, ly, text="On:", font=("Segoe UI", 10, "bold"), fill="#8e8e93", anchor="nw")
                                ly += jp_h + 1
                                canvas.create_text(pad_x + 6, ly, text=on_content, font=jp_font, fill="#1c1c1e", anchor="nw", width=wrap_w_inner)
                                ly += _nlines(jf, on_content, wrap_w_inner) * jp_h + 4
                            if kun_all:
                                kun_content = " • ".join(kun_all)
                                canvas.create_text(pad_x + 6, ly, text="Kun:", font=("Segoe UI", 10, "bold"), fill="#8e8e93", anchor="nw")
                                ly += jp_h + 1
                                canvas.create_text(pad_x + 6, ly, text=kun_content, font=jp_font, fill="#1c1c1e", anchor="nw", width=wrap_w_inner)
                                ly += _nlines(jf, kun_content, wrap_w_inner) * jp_h + 4
                    except Exception:
                        pass

        canvas.update_idletasks()
        bbox = canvas.bbox("all")
        dict_h = (bbox[3] if bbox else ly) + 8
        dict_x = self.overlay_x
        dict_y = self.overlay_y + self.overlay_h + 4

        screen_limit_y = 1080
        try:
            screen_limit_y = self.root.winfo_screenheight()
        except Exception:
            pass
        if dict_y + dict_h > screen_limit_y:
            dict_y += screen_limit_y - (dict_y + dict_h)

        self._dict_window.geometry(f"{card_w}x{dict_h}+{dict_x}+{dict_y}")
        self._dict_window.lift()
        canvas.configure(height=dict_h, width=card_w)
        try:
            self._dict_window.update_idletasks()
            hwnd = user32.GetAncestor(self._dict_window.winfo_id(), 2)
            self._apply_round_corners(hwnd, card_w, dict_h)
        except Exception:
            pass

    def _dict_lookup_skip(self, seq):
        """Main thread: skip dict card (no useful results)."""
        if seq == self._dict_lookup_seq:
            self._withdraw_dict_card()

    def _withdraw_dict_card(self):
        """Withdraw the dictionary card (keep window alive, just hide)."""
        if hasattr(self, '_dict_window') and self._dict_window:
            try:
                self._dict_window.withdraw()
            except Exception:
                pass

    def _hide_dict_card(self):
        """Destroy the dictionary card permanently."""
        if hasattr(self, '_dict_window') and self._dict_window:
            try:
                self._dict_window.destroy()
            except Exception:
                pass
            self._dict_window = None
            self._dict_canvas = None

    def _get_action_text(self, idx):
        """Return selected text, or hovered chunk text, or None."""
        text = self._get_selected_text()
        if text:
            return text
        text = self._get_hovered_chunk_text()
        if text:
            return text
        if 0 <= idx < len(self.ocr_boxes):
            text = self.ocr_boxes[idx]['data']['original']
            return text.strip()
        return None

    def _box_right_click(self, _event, idx):
        """Right-click on a box → open in Jisho."""
        text = self._get_action_text(idx)
        if text:
            url = f"https://jisho.org/search/{urllib.parse.quote(text)}"
            webbrowser.open(url)

    def _box_shift_right_click(self, _event, idx):
        """Shift+Right-click on a box → open in DeepL."""
        text = self._get_action_text(idx)
        if text:
            url = f"https://www.deepl.com/en/translator#ja/en/{urllib.parse.quote(text)}"
            webbrowser.open(url)

    def _box_middle_click(self, _event, idx):
        """Middle-click on a box → TTS."""
        text = self._get_action_text(idx)
        if text:
            threading.Thread(target=self.read_aloud, args=(text,), daemon=True).start()

    def _show_card(self, idx):
        """Display the translation card as a separate Toplevel near the hovered box."""
        if idx < 0 or idx >= len(self.ocr_boxes):
            return
        self._card_data_idx = idx
        self._card_data = self.ocr_boxes[idx]['data']
        self._card_box = self.ocr_boxes[idx]
        self._card_hover_char_idx = -1

        if self._card_window:
            self._render_card()
            return

        box = self._card_box
        data = self._card_data
        bbox = box['orig_bbox']
        card_w = box['w']

        # Expand card width to fit Japanese text if needed
        kf = tkfont.Font(family=self.japanese_font, size=self.japanese_font_size, weight="bold")
        orig_w = kf.measure(data['original'])
        if orig_w + 12 > card_w:
            card_w = orig_w + 12
        # Also measure romaji
        if self.show_romaji and data.get('romaji'):
            rf = tkfont.Font(family="Segoe UI", size=max(7, self.font_size_en - 2), slant="italic")
            rom_w = rf.measure(data['romaji']) + 12
            if rom_w > card_w:
                card_w = rom_w

        # Card position: centered above the bounding box
        screen_x = int(self.overlay_x + bbox['x'] + (bbox['width'] - card_w) // 2)
        screen_y = int(self.overlay_y + bbox['y'])
        screen_x = max(0, screen_x)
        # Clamp card width so it doesn't extend off-screen
        screen_limit = 1920  # rough fallback
        try:
            screen_limit = self.root.winfo_screenwidth()
        except Exception:
            pass
        max_card_w = screen_limit - screen_x - 8
        if card_w > max_card_w:
            card_w = max(max_card_w, 180)
            screen_x = max(0, screen_x)
        if screen_x + card_w > screen_limit:
            screen_x = screen_limit - card_w - 8

        # Estimate card height (+ padding) — must match _render_card layout
        en_font = tkfont.Font(family="Segoe UI", size=self.font_size_en, weight="bold")
        content_h = 0
        if self.show_crop and box.get('crop_pil'):
            content_h += 3 + box['crop_pil'].height + 24
        else:
            content_h += 8
        line_h = max(30, kf.metrics("linespace"))
        content_h += line_h  # Japanese text
        furigana_size = max(8, self.japanese_font_size // 2 - 1)
        ff_temp = tkfont.Font(family=self.japanese_font, size=furigana_size)
        content_h += ff_temp.metrics("linespace") + 2  # furigana + gap
        if self.show_romaji:
            content_h += 18 + 2
        if self.show_translation:
            est_chars = max(1, (card_w - 16) // 7)
            en_lines = max(1, -(-len(data.get('english', '')) // est_chars))
            content_h += en_lines * en_font.metrics("linespace")
            content_h += 6
        card_h = content_h

        # Place card above the box (or below if not enough room)
        card_bottom = int(screen_y - 4)
        card_top = max(0, card_bottom - card_h)
        if card_top < 20:
            card_top = int(screen_y + bbox['height'] + 4)
            card_bottom = card_top + card_h

        win = tk.Toplevel(self.root)
        win.overrideredirect(True)
        win.geometry(f"{card_w}x{card_h}+{screen_x}+{card_top}")
        win.attributes("-topmost", True)
        canvas = tk.Canvas(win, width=card_w, height=card_h,
                           borderwidth=0, highlightthickness=0, bg=CARD_BG)
        canvas.pack()
        win.bind("<Escape>", lambda e: self.unfreeze_screen())

        canvas.bind("<Leave>", self._card_leave)

        self._card_window = win
        self._card_canvas = canvas
        self._card_xy = (screen_x, card_top)
        self._render_card()

    def _render_card(self):
        """Draw/refresh card content on the existing card canvas."""
        canvas = self._card_canvas
        canvas.delete("all")
        data = self._card_data
        box = self._card_box
        card_w = int(canvas.cget('width'))

        kf = tkfont.Font(family=self.japanese_font, size=self.japanese_font_size, weight="bold")
        en_font = tkfont.Font(family="Segoe UI", size=self.font_size_en, weight="bold")
        furigana_size = max(8, self.japanese_font_size // 2 - 1)
        ff_temp = tkfont.Font(family=self.japanese_font, size=furigana_size)
        ff = (self.japanese_font, furigana_size)
        line_h = max(30, kf.metrics("linespace"))
        fg_line_h = ff_temp.metrics("linespace")

        pad_x = 6
        ly = 0

        # Crop image
        if self.show_crop and box.get('crop_pil'):
            crop_tk = ImageTk.PhotoImage(box['crop_pil'])
            self.crop_tk_imgs.append(crop_tk)
            canvas.create_image(5, ly + 3, image=crop_tk, anchor="nw")
            ly += 3 + box['crop_pil'].height + 24
        else:
            ly += 8

        # Japanese text line
        jp_y = ly
        canvas.create_text(pad_x, jp_y, text=data['original'],
                           font=(self.japanese_font, self.japanese_font_size, "bold"),
                           fill="#a31515", anchor="nw", tags="jp_text")
        jp_text_bottom = jp_y + line_h

        # Token position tracking for hover highlight
        ki = data.get('kakasi_items', [])
        full_text = ''.join(it['orig'] for it in ki)
        fg_y = jp_text_bottom
        char_off = 0
        self._card_token_positions = []
        self._card_romaji_positions = []
        rf = tkfont.Font(family="Segoe UI", size=max(7, self.font_size_en - 2), slant="italic") if self.show_romaji else None
        rfx = pad_x
        for item_idx, item in enumerate(ki):
            orig = item.get('orig', '')
            hira = item.get('hira') or orig
            prefix_w = kf.measure(full_text[:char_off])
            group_w = kf.measure(orig)
            x_start = pad_x + prefix_w
            x_end = pad_x + prefix_w + group_w
            self._card_token_positions.append((x_start, x_end, item_idx))

            # Track romaji positions with italic font for accurate highlight
            if self.show_romaji and rf:
                rt = item.get('hepburn', '') or orig
                rw = rf.measure(rt)
                self._card_romaji_positions.append((rfx, rw, item_idx))
                rfx += rw
                if not item.get('_no_trail_space'):
                    rfx += rf.measure(' ')

            # Draw furigana for kanji tokens
            if orig != hira and any(_is_kanji(c) for c in orig):
                okuri_count = 0
                for ch in reversed(orig):
                    if _is_kana(ch):
                        okuri_count += 1
                    else:
                        break
                if okuri_count > 0 and okuri_count < len(hira):
                    k_orig = orig[:-okuri_count]
                    k_hira = hira[:-okuri_count]
                else:
                    k_orig = orig
                    k_hira = hira
                if any(_is_kanji(c) for c in k_orig):
                    kw = kf.measure(k_orig)
                    cx = pad_x + prefix_w + kw / 2
                    canvas.create_text(cx, fg_y + 2, text=k_hira, font=ff,
                                       fill="#248a3d", anchor="n", tags="furigana")
            char_off += len(orig)
        ly = fg_y + fg_line_h + 2

        # Romaji
        rom_y = ly
        if self.show_romaji:
            canvas.create_text(pad_x, rom_y, text=data['romaji'],
                               font=("Segoe UI", max(7, self.font_size_en - 2), "italic"),
                               fill="#0066cc", anchor="nw", tags="romaji_text")
            ly += 18 + 2

        # English
        if self.show_translation:
            eng_y = ly
            canvas.create_text(pad_x, eng_y, text=data['english'],
                               font=("Segoe UI", self.font_size_en, "bold"),
                               fill="#1c1c1e", anchor="nw", width=card_w - 16, tags="eng_text")
            est_chars = max(1, (card_w - 16) // 7)
            en_lines = max(1, -(-len(data.get('english', '')) // est_chars))
            card_h = eng_y + en_lines * en_font.metrics("linespace") + 6
        else:
            card_h = ly
        canvas.configure(height=card_h)
        if self._card_window and hasattr(self, '_card_xy'):
            cx, cy = self._card_xy
            self._card_window.geometry(f"{card_w}x{card_h}+{cx}+{cy}")
            try:
                hwnd = user32.GetAncestor(self._card_window.winfo_id(), 2)
                self._apply_round_corners(hwnd, card_w, card_h)
            except Exception:
                pass

        # Map hovered OCR character index → kakasi_items chunk index
        hover_chunk_idx = -1
        if self._card_hover_char_idx >= 0:
            coff = 0
            for ci, item in enumerate(ki):
                orig = item.get('orig', '')
                if coff <= self._card_hover_char_idx < coff + len(orig):
                    hover_chunk_idx = ci
                    break
                coff += len(orig)

        # Draw highlight for hovered word, then raise text above it
        if hover_chunk_idx >= 0:
            if self._is_ctrl_held() and self._card_hover_char_idx >= 0:
                hx1 = kf.measure(full_text[:self._card_hover_char_idx]) + pad_x
                hx2 = kf.measure(full_text[:self._card_hover_char_idx + 1]) + pad_x
                canvas.create_rectangle(
                    hx1 - 1, jp_y + 1, hx2 + 1, jp_text_bottom - 1,
                    fill="#fff3cd", outline="#ffc107", width=1, tags="highlight"
                )
                if self.show_romaji:
                    for rx, rw, ri in self._card_romaji_positions:
                        if ri == hover_chunk_idx:
                            canvas.create_rectangle(
                                rx - 1, rom_y + 1, rx + rw + 1, rom_y + 17,
                                fill="#fff3cd", outline="#ffc107", width=1, tags="highlight"
                            )
                            break
            else:
                for x1, x2, idx in self._card_token_positions:
                    if idx == hover_chunk_idx:
                        canvas.create_rectangle(
                            x1 - 1, jp_y + 1, x2 + 1, jp_text_bottom - 1,
                            fill="#fff3cd", outline="#ffc107", width=1, tags="highlight"
                        )
                        if self.show_romaji:
                            for rx, rw, ri in self._card_romaji_positions:
                                if ri == hover_chunk_idx:
                                    canvas.create_rectangle(
                                        rx - 1, rom_y + 1, rx + rw + 1, rom_y + 17,
                                        fill="#fff3cd", outline="#ffc107", width=1, tags="highlight"
                            )
                        break
            canvas.tag_raise("jp_text")
            canvas.tag_raise("furigana")
            canvas.tag_raise("romaji_text")
            canvas.tag_raise("eng_text")

    def _box_motion(self, event, idx):
        """Mouse moved over a box canvas → find OCR word under cursor, highlight on card."""
        if idx < 0 or idx >= len(self.ocr_boxes):
            return
        box = self.ocr_boxes[idx]
        bbox = box['orig_bbox']
        words = box.get('words', [])
        if not words or not self._card_window:
            return
        ox = bbox['x'] - BOX_PAD + event.x
        oy = bbox['y'] - BOX_PAD + event.y
        wi = -1
        for i, w in enumerate(words):
            if w['x'] <= ox <= w['x'] + w['width'] and \
               w['y'] <= oy <= w['y'] + w['height']:
                wi = i
                break
        if wi < 0:
            return

        # Convert word index → character offset for kakasi_items mapping
        char_off = sum(len(words[j]['text']) for j in range(wi))

        self._redraw_box_highlight(idx, char_off)

        if char_off != self._card_hover_char_idx:
            self._card_hover_char_idx = char_off
            self._render_card()

    def _card_leave(self, _event):
        """Handle mouse leave from card canvas — hide card."""
        self._hide_card()

    def _box_mousewheel(self, event, _idx):
        """Mousewheel over a box → cycle reading for the hovered token."""
        if self._card_hover_char_idx < 0:
            return
        ki = self._card_data.get('kakasi_items', [])
        # Map char_idx → chunk_idx
        coff = 0
        chunk_idx = -1
        for ci, item in enumerate(ki):
            orig = item.get('orig', '')
            if coff <= self._card_hover_char_idx < coff + len(orig):
                chunk_idx = ci
                break
            coff += len(orig)
        if chunk_idx < 0 or chunk_idx >= len(ki):
            return
        item = ki[chunk_idx]
        alts = item.get('alternatives', [])
        if len(alts) <= 1:
            return
        delta = -1 if event.delta > 0 else 1
        active = item.get('active_idx', 0)
        active = (active + delta) % len(alts)
        item['active_idx'] = active
        item['hira'] = alts[active]['hira']
        item['hepburn'] = alts[active]['hepburn']
        self._card_data['romaji'] = " ".join([it['hepburn'] for it in ki])
        self._render_card()

    def _hide_card(self):
        """Destroy the card Toplevel if visible."""
        self._hide_dict_card()
        if self._card_window:
            try:
                self._card_window.destroy()
            except Exception:
                pass
            self._card_window = None
            self._card_canvas = None
            self._card_xy = None
        self._card_data = None
        self._card_data_idx = -1
        self._card_box = None
        self._card_token_positions = []
        self._card_romaji_positions = []
        self._card_hover_char_idx = -1
        self._selection_box_idx = -1
        self._selection_start = -1
        self._selection_end = -1
        for _, canvas2 in (e[:2] for e in self._box_windows):
            try:
                canvas2.delete("word_hl")
                canvas2.delete("sel_hl")
            except Exception:
                pass

    def read_aloud(self, text):
        import asyncio, tempfile
        import edge_tts
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
        except Exception:
            pass
        finally:
            try:
                os.unlink(tmp)
            except Exception:
                pass

    def unfreeze_screen(self):
        if not self.active:
            return
        self.active = False
        self._destroy_box_windows()
        self._hide_dict_card()
        if hasattr(self, 'snip_window') and self.snip_window:
            self._close_snip()
        if hasattr(self, 'active_window') and self.active_window:
            try:
                self.active_window.destroy()
            except Exception:
                pass
            self.active_window = None
            self.overlay_hwnd = None
        if self._loading_win:
            try:
                self._loading_win.destroy()
            except Exception:
                pass
            self._loading_win = None
            self._load_tk_img = None
        if self._prev_focus_hwnd:
            try:
                user32.SetForegroundWindow(self._prev_focus_hwnd)
            except Exception:
                pass
        self._prev_focus_hwnd = None
        self._overlay_hidden = False
        self.pil_img = None
        self.crop_tk_imgs = []
        self.ocr_boxes = []
        self.current_hover_idx = -1
        self._card_data = None
        self._card_data_idx = -1
        self._card_box = None
        self._card_token_positions = []
        self._card_romaji_positions = []
        self._card_hover_char_idx = -1
        self._selection_box_idx = -1
        self._selection_start = -1
        self._selection_end = -1

    def check_focus(self):
        if not self.active:
            self.root.after(500, self.check_focus)
            return
        fg = user32.GetForegroundWindow()
        all_hwnds = {self._prev_focus_hwnd}
        for win, _, _ in self._box_windows:
            try:
                all_hwnds.add(user32.GetAncestor(win.winfo_id(), 2))
            except Exception:
                pass
        if self._card_window:
            try:
                all_hwnds.add(user32.GetAncestor(self._card_window.winfo_id(), 2))
            except Exception:
                pass
        if hasattr(self, '_dict_window') and self._dict_window:
            try:
                all_hwnds.add(user32.GetAncestor(self._dict_window.winfo_id(), 2))
            except Exception:
                pass
        if fg not in all_hwnds:
            if not self._overlay_hidden:
                self._overlay_hidden = True
                for win, _, _ in self._box_windows:
                    try:
                        win.withdraw()
                    except Exception:
                        pass
                if hasattr(self, '_dict_window') and self._dict_window:
                    try:
                        self._dict_window.withdraw()
                    except Exception:
                        pass
                self._hide_card()
        else:
            if self._overlay_hidden:
                self._overlay_hidden = False
                for win, _, _ in self._box_windows:
                    try:
                        win.deiconify()
                    except Exception:
                        pass
                if hasattr(self, '_dict_window') and self._dict_window:
                    try:
                        self._dict_window.deiconify()
                    except Exception:
                        pass
        self.root.after(500, self.check_focus)

    def run(self):
        self.check_focus()
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
        pass

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

    pass
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
    print("Application started")
    print("Loading OCR Model...\n")

    app = ScreenFreezerApp()
    app._prewarm_event.wait()
    print("Application Ready\n")
    print("  Ctrl+Alt+Shift+E  Capture game window for OCR / translation")
    print("  Ctrl+Alt+Shift+R  Snip mode (drag-select a region)")
    print("  Ctrl+Alt+Shift+S  Settings panel")
    print("  Press Escape while frozen to unfreeze and restore focus.")

    # Start Win32 hotkey thread (RegisterHotKey with fallback to GetAsyncKeyState)
    threading.Thread(target=register_hotkey_win32, args=(app,), daemon=True).start()

    try:
        app.run()
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    main()
