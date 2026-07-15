import os
import json
import time
import tkinter as tk
import tkinter.messagebox as tkmb
import tkinter.font as tkfont
import tkinter.simpledialog as tksd
import queue
import threading
import socket
import re
import ctypes
import ctypes.wintypes
import sys
import logging
import translation_service
from concurrent.futures import ThreadPoolExecutor
from PIL import Image, ImageTk
from functools import lru_cache
import webbrowser
import urllib.parse
import mss
import jaconv
import pykakasi
from jamdict import Jamdict
from sudachipy import Dictionary, SplitMode

from hotkeys import register_hotkey_win32, _hk_display, _vk_to_display, MOD_CONTROL, MOD_ALT, MOD_SHIFT, MOD_NOREPEAT
from settings import SettingsManager
from utils import (
    _PROJECT_DIR, SANKOKU_DB, KANKI_DB,
    contains_japanese, _is_kanji, _is_kana,
    find_word_at_point, get_chunk_at_offset, get_chunk_field, get_combined_chunk_forms,
    get_single_char_at_offset,
    make_translucent, user32,
    WS_EX_LAYERED, WS_EX_TRANSPARENT, WS_EX_NOACTIVATE, GWL_EXSTYLE,
    LWA_COLORKEY, LWA_ALPHA, RGN_OR, SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE,
)

def _nlines(font_obj, text, wrap_width):
    if not text:
        return 0
    tw = font_obj.measure(text)
    return max(1, -(-tw // wrap_width))

def _get_monolingual_meanings(text):
    """
    Query sankokudict.db for a word and return structured sense data.
    Returns: [{'kanji': str, 'kana': str, 'gloss': str, 'pos': [str],
               'antonyms': [str], 'xrefs': [str], 'etym': str}] or None
    """
    db_path = SANKOKU_DB
    if not os.path.exists(db_path):
        return None
    try:
        import sqlite3
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()

        # Find idseq from Kana or Kanji
        idseq = None
        cur.execute("SELECT idseq FROM Kana WHERE text = ?", (text,))
        row = cur.fetchone()
        if row:
            idseq = row[0]
        else:
            cur.execute("SELECT idseq FROM Kanji WHERE text = ?", (text,))
            row = cur.fetchone()
            if row:
                idseq = row[0]

        if idseq is None:
            conn.close()
            return None

        # Get kanji and kana
        cur.execute("SELECT text FROM Kanji WHERE idseq = ?", (idseq,))
        kanji_row = cur.fetchone()
        kanji = kanji_row[0] if kanji_row else ""

        cur.execute("SELECT text FROM Kana WHERE idseq = ?", (idseq,))
        kana_row = cur.fetchone()
        kana = kana_row[0] if kana_row else ""

        # Get etymology
        cur.execute("SELECT text FROM Etym WHERE idseq = ?", (idseq,))
        etym_row = cur.fetchone()
        etym = etym_row[0] if etym_row else ""

        # Get all senses
        cur.execute("SELECT ID FROM Sense WHERE idseq = ? ORDER BY ID", (idseq,))
        sense_ids = [r[0] for r in cur.fetchall()]

        results = []
        for sid in sense_ids:
            # Gloss
            cur.execute("SELECT text FROM SenseGloss WHERE sid = ?", (sid,))
            gloss_rows = cur.fetchall()
            if not gloss_rows:
                continue
            gloss = gloss_rows[0][0]

            # POS
            cur.execute("SELECT text FROM pos WHERE sid = ?", (sid,))
            pos = [r[0] for r in cur.fetchall()]

            # Antonyms
            cur.execute("SELECT text FROM antonym WHERE sid = ?", (sid,))
            antonyms = [r[0] for r in cur.fetchall()]

            # Xrefs
            cur.execute("SELECT text FROM xref WHERE sid = ?", (sid,))
            xrefs = [r[0] for r in cur.fetchall()]

            results.append({
                'kanji': kanji,
                'kana': kana,
                'gloss': gloss,
                'pos': pos,
                'antonyms': antonyms,
                'xrefs': xrefs,
                'etym': etym,
            })

        conn.close()
        return results if results else None
    except Exception:
        return None


def _get_kanji_info(char):
    """Query Kanki table in kankidict.db for a single character."""
    db_path = KANKI_DB
    if not os.path.exists(db_path):
        return None
    try:
        import sqlite3
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute("SELECT on_readings, kun_readings, gloss FROM Kanki WHERE character = ?", (char,))
        row = cur.fetchone()
        conn.close()
        if row:
            return {"on": row[0], "kun": row[1], "gloss": row[2]}
        return None
    except Exception:
        return None


# PaddlePaddle 3.3 PIR compatibility workaround — must be set before any
# paddle/paddlex import, so it goes right at the top.
os.environ["FLAGS_enable_pir_with_executor_in_serial_mode"] = "0"

# ── Redirect all library stderr to a log file ─────────────────────────────────
import warnings
warnings.filterwarnings("ignore", message="No ccache found")
import msvcrt
_APP_READY_FLAG = os.path.join(_PROJECT_DIR, "_app_ready.flag")
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
STD_ERROR_HANDLE = -12
_log_path = os.path.join(_PROJECT_DIR, "app.log")

class _Tee:
    def __init__(self, *files):
        self.files = files
    def write(self, msg):
        for f in self.files:
            try:
                f.write(msg)
            except Exception:
                pass
    def flush(self):
        for f in self.files:
            try:
                f.flush()
            except Exception:
                pass
    def fileno(self):
        return self.files[0].fileno()

try:
    _log_console = sys.stderr
    _log_fd = os.open(_log_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
    _log_handle = msvcrt.get_osfhandle(_log_fd)
    kernel32.SetStdHandle(STD_ERROR_HANDLE, _log_handle)
    os.dup2(_log_fd, 2)
    os.close(_log_fd)
    _log_file = open(_log_path, "a", encoding="utf-8")
    sys.stderr = _Tee(_log_console, _log_file)
except Exception:
    _log_console = sys.stderr

# Keep a writable handle for the click-event logging path.
try:
    _debug_log_handle = open(_log_path, "a", encoding="utf-8")
except Exception:
    _debug_log_handle = None


def _append_debug_log(msg):
    try:
        if _debug_log_handle is not None:
            _debug_log_handle.write(msg + "\n")
            _debug_log_handle.flush()
        else:
            print(msg, file=sys.stderr)
    except Exception:
        pass


def _log_click_event(box_data, hovered_chunk, top_entry=None, candidates=None):
    try:
        region_text = box_data.get('original', '') or ''
        chunks = [item.get('orig', '') for item in box_data.get('kakasi_items', []) if item.get('orig')]
        chunk_text = hovered_chunk or ''
        top_header = ''
        if top_entry is not None:
            kanji_texts = [k.text for k in getattr(top_entry, 'kanji_forms', [])]
            kana_texts = [k.text for k in getattr(top_entry, 'kana_forms', [])]
            header_parts = []
            if kanji_texts:
                header_parts.append('/'.join(kanji_texts))
            if kana_texts:
                header_parts.append('(' + ', '.join(kana_texts) + ')')
            top_header = ' '.join(header_parts)
        candidate_text = ''
        if candidates:
            candidate_text = ','.join(candidates).replace('|', '／')
        _append_debug_log(
            "CLICK_EVENT|region_text=" + region_text.replace('\n', ' ').replace('|', '／') +
            "|chunks=" + ','.join(chunks).replace('|', '／') +
            "|clicked_chunk=" + chunk_text.replace('|', '／') +
            "|candidates=" + candidate_text +
            "|top_dict_def=" + top_header.replace('|', '／')
        )
    except Exception as exc:
        _append_debug_log(f"CLICK_EVENT|error={exc}")
# ─────────────────────────────────────────────────────────────────────────────

# ── OCR engine selector ──────────────────────────────────────────────────────
# "windows" → WinOCR (Windows.Media.Ocr, fast but less accurate)
# "paddle"  → PaddleOCR (deep-learning, more accurate, ~5s first-load)
OCR_ENGINE = "paddle"
# ─────────────────────────────────────────────────────────────────────────────

# ── Translation backend ──────────────────────────────────────────────────────
# "google"  → Google Translate (online, free, no key needed)
# "deepl"   → DeepL API  (online, needs key in apikeys.json)
TRANSLATOR = "google"
DEBUG_DICT_LOG = os.getenv("KWS_DEBUG_DICT", "0") == "1"

if DEBUG_DICT_LOG:
    logging.basicConfig(level=logging.DEBUG, format="[%(asctime)s] %(message)s")
    logger = logging.getLogger("kws.dict")
    if _debug_log_handle:
        handler = logging.StreamHandler(_debug_log_handle)
        handler.setFormatter(logging.Formatter("[%(asctime)s] %(message)s"))
        logger.addHandler(handler)
else:
    logger = logging.getLogger("kws.dict")
    logger.addHandler(logging.NullHandler())
# ─────────────────────────────────────────────────────────────────────────────

# ── Language filter ─────────────────────────────────────────────────────────
# When True, non-Japanese OCR text (numbers, English UI) is skipped.
# Set to False to show everything (useful for debugging OCR coverage).
SKIP_NON_JAPANESE = True
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



def _generate_fuzzy_candidates(word):
    """Generate plausible spelling variations for fuzzy fallback lookup."""
    if not word:
        return []
    seen = set()
    candidates = []
    def _add(w):
        if w != word and w not in seen:
            seen.add(w)
            candidates.append(w)
    # 1. Add/remove trailing prolonged sound mark (ー)
    if word.endswith('ー'):
        _add(word[:-1])
    else:
        _add(word + 'ー')
    # 2. Replace all ー with the appropriate kana vowel
    #    (ー after ア/カ/サ/… → ア, after イ/キ/… → イ, etc.)
    vowel_map = {'ア': 'ア', 'カ': 'ア', 'ガ': 'ア', 'サ': 'ア', 'ザ': 'ア',
                 'タ': 'ア', 'ダ': 'ア', 'ナ': 'ア', 'ハ': 'ア', 'バ': 'ア',
                 'パ': 'ア', 'マ': 'ア', 'ヤ': 'ア', 'ラ': 'ア', 'ワ': 'ア',
                 'イ': 'イ', 'キ': 'イ', 'ギ': 'イ', 'シ': 'イ', 'ジ': 'イ',
                 'チ': 'イ', 'ヂ': 'イ', 'ニ': 'イ', 'ヒ': 'イ', 'ビ': 'イ',
                 'ピ': 'イ', 'ミ': 'イ', 'リ': 'イ',
                 'ウ': 'ウ', 'ク': 'ウ', 'グ': 'ウ', 'ス': 'ウ', 'ズ': 'ウ',
                 'ツ': 'ウ', 'ヅ': 'ウ', 'ヌ': 'ウ', 'フ': 'ウ', 'ブ': 'ウ',
                 'プ': 'ウ', 'ム': 'ウ', 'ユ': 'ウ', 'ル': 'ウ',
                 'エ': 'エ', 'ケ': 'エ', 'ゲ': 'エ', 'セ': 'エ', 'ゼ': 'エ',
                 'テ': 'エ', 'デ': 'エ', 'ネ': 'エ', 'ヘ': 'エ', 'ベ': 'エ',
                 'ペ': 'エ', 'メ': 'エ', 'レ': 'エ',
                 'オ': 'オ', 'コ': 'オ', 'ゴ': 'オ', 'ソ': 'オ', 'ゾ': 'オ',
                 'ト': 'オ', 'ド': 'オ', 'ノ': 'オ', 'ホ': 'オ', 'ボ': 'オ',
                 'ポ': 'オ', 'モ': 'オ', 'ヨ': 'オ', 'ロ': 'オ', 'ヲ': 'オ'}
    if 'ー' in word:
        result = []
        for i, ch in enumerate(word):
            if ch == 'ー' and i > 0:
                result.append(vowel_map.get(word[i-1], 'ー'))
            else:
                result.append(ch)
        _add(''.join(result))
    # 3. Small tsu normalization (ッ → ツ)
    if 'ッ' in word:
        _add(word.replace('ッ', 'ツ'))
    if 'っ' in word:
        _add(word.replace('っ', 'つ'))
    return candidates

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

_SUDACHI_POS = {
    '名詞': 'noun', '代名詞': 'pronoun', '動詞': 'verb',
    '形容詞': 'adjective', '形状詞': 'adjective', '副詞': 'adverb',
    '連体詞': 'adjective', '接続詞': 'conjunction', '感動詞': 'interjection',
    '助詞': 'particle', '助動詞': 'auxiliary verb',
    '接頭辞': 'prefix', '接尾辞': 'suffix',
}

_SUDACHI_CONJ_LABEL = {
    '終止形-一般': '',
    '連体形-一般': 'attributive',
    '連用形-一般': 'ます-stem',
    '連用形-促音便': 'て-form',
    '連用形-撥音便': 'んで-form',
    '連用形-イ音便': 'て-form (i)',
    '未然形-一般': 'causative/passive',
    '未然形-ウ接続': 'volitional',
    '意志推量形': 'volitional',
    '仮定形-一般': 'conditional (-ば)',
    '命令形-一般': 'imperative',
    '已然形-一般': 'conditional (archaic)',
}

def _extract_conj_label(pos):
    """Extract conjugation label from Sudachi POS tuple (index 5)."""
    if pos and len(pos) > 5 and pos[5] and pos[5] != '*':
        return _SUDACHI_CONJ_LABEL.get(pos[5], pos[5])
    return ''

def _fix_number_counter(tokens):
    """Post-process Sudachi tokens: correct readings for numbers and number+counter combos.
    Returns list of dicts with keys: surface, reading, hira, hepburn, dict_form, pos, conj."""
    result = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        orig = token.surface()
        if not orig.isdigit():
            pos = token.part_of_speech()
            result.append({
                'surface': orig,
                'reading': token.reading_form(),
                'hira': None,
                'hepburn': None,
                'dict_form': token.dictionary_form(),
                'pos': _SUDACHI_POS.get(pos[0] if pos else '', ''),
                'conj': _extract_conj_label(pos),
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
                    'pos': '', 'conj': '',
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
                'pos': '', 'conj': '',
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
            'pos': '', 'conj': '',
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
    items = []  # list of (orig, dict_form, hira, hepburn, alternatives, active_idx, no_trail_space, pos, conj)
    for ft in fixed:
        orig = ft['surface']
        reading = ft['reading']
        hira = ft['hira'] or (jaconv.kata2hira(reading) if reading else orig)
        dict_form = ft.get('dict_form') or orig
        pos = ft.get('pos', '')
        conj = ft.get('conj', '')
        if any(_is_kanji(c) for c in orig):
            alternatives = _build_alternatives(orig, hira)
            items.append((orig, dict_form, alternatives[0]['hira'],
                          alternatives[0]['hepburn'], alternatives, 0, False, pos, conj))
        elif orig.isdigit():
            romaji = " ".join([r['hepburn'] for r in kks.convert(hira)])
            alternatives = ((hira, romaji, 'number'),)
            items.append((orig, dict_form, hira, romaji, alternatives, 0, False, pos, conj))
        elif not any(_is_kana(c) for c in orig):
            alternatives = ((orig, orig, 'unknown'),)
            items.append((orig, dict_form, orig, orig, alternatives, 0, False, pos, conj))
        else:
            alternatives = _build_alternatives(orig, hira)
            items.append((orig, dict_form, alternatives[0]['hira'],
                          alternatives[0]['hepburn'], alternatives, 0, False, pos, conj))

    for i in range(len(items) - 1):
        h1 = items[i][3]
        h2 = items[i+1][3]
        if h1.endswith('tsu') and h2 and h2[0] in 'bcdfghjklmnpqrstvwxyz':
            items[i] = items[i][:3] + (h1[:-3],) + items[i][4:6] + (True, items[i][7], items[i][8])
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
        orig, df, hira, hep, alts, active, _, pos, conj = item
        if isinstance(alts, tuple) and len(alts) > 0 and isinstance(alts[0], tuple):
            alts = [{'hira': a[0], 'hepburn': a[1], 'type': a[2]} for a in alts]
        result.append({
            'orig': orig, 'dict_form': df, 'hira': hira,
            'hepburn': hep, 'alternatives': alts, 'active_idx': active,
            'pos': pos, 'conj': conj,
        })
    return result


def translate_and_convert(japanese_text, do_translate=True):
    """Convert Japanese to Romaji, Hiragana, and English translation."""
    try:
        text, romaji, kana, items_tuple = _process_japanese(japanese_text)
        items = _unpack_items(items_tuple)

        if do_translate:
            if TRANSLATOR == "deepl":
                english = translation_service.translate_or_cached(japanese_text)
            else:
                english = translation_service.translate_google(japanese_text)
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


def _sort_jamdict_entries(entries, pos='', hira='', word='', conj=''):
    """Prioritize entries whose kana forms match the contextual reading and word form."""
    if not entries:
        return []

    def _normalize_hira(text):
        try:
            return jaconv.kata2hira(text) if text else ''
        except Exception:
            return str(text or '')

    def _reading_rank(entry):
        if not hira:
            return 2
        target = _normalize_hira(hira)
        kana_forms = [_normalize_hira(getattr(k, 'text', '')) for k in getattr(entry, 'kana_forms', []) if getattr(k, 'text', '')]
        if any(k == target for k in kana_forms):
            return 0
        if any(target in k or k in target for k in kana_forms):
            return 1
        return 2

    def _word_rank(entry):
        if not word:
            return 1
        word_norm = str(word).strip()
        if not word_norm:
            return 1
        kanji_forms = [getattr(k, 'text', '') for k in getattr(entry, 'kanji_forms', []) if getattr(k, 'text', '')]
        if any(k == word_norm for k in kanji_forms):
            return 0
        if any(word_norm in k or k in word_norm for k in kanji_forms):
            return 1
        return 2

    def _sense_rank(entry):
        if not entry.senses:
            return 2
        pos_tags = []
        for sense in entry.senses:
            pos_tags.extend(getattr(sense, 'pos', []) or [])
        if pos in {'verb', 'adjective', 'noun', 'particle'} and any(p == pos for p in pos_tags):
            return 0
        if pos in {'verb'} and any('verb' in p.lower() for p in pos_tags):
            return 1
        if any('verb' in p.lower() for p in pos_tags):
            return 2
        return 3

    def _context_rank(entry):
        if pos != 'verb':
            return 2

        glosses = [g.text.lower() for s in entry.senses for g in getattr(s, 'gloss', [])]
        if not glosses:
            return 2

        # Generic verb-stem / inflected-form preference: prefer common predicate-style senses
        # when the chunk is functioning as a verbal stem, even if the conjugation label is missing.
        generic_glosses = {
            'to be', 'to exist', 'to stay', 'to go', 'to come', 'to do', 'to make', 'to have',
            'to say', 'to know', 'to think', 'to see', 'to eat', 'to drink', 'to sleep',
            'to live', 'to use', 'to take', 'to get', 'to give', 'to become', 'to return',
            'to wait', 'to work', 'to move', 'to happen', 'to arrive', 'to want', 'to need'
        }
        if any(gloss in generic_glosses for gloss in glosses):
            return 0
        if any(gloss.startswith('to be') or gloss.startswith('to exist') or gloss.startswith('to stay') or gloss.startswith('to go') or gloss.startswith('to come') or gloss.startswith('to do') or gloss.startswith('to make') or gloss.startswith('to have') for gloss in glosses):
            return 1
        # For verbal stems like すい / じゃい, prefer entries whose glosses reflect the contextual
        # predicate meaning over the raw inflected form's base dictionary entry when the chunk is part of a larger phrase.
        if any(token in {'すい', 'じゃい', 'おり', '炊い'} for token in {hira, word, _normalize_hira(hira)}):
            if any('to eat' in gloss or 'to go' in gloss or 'to become' in gloss or 'to be' in gloss or 'to die' in gloss for gloss in glosses):
                return 0
        if conj and any(token in conj.lower() for token in ('stem', 'form', 'te', 'ta', 'masu', 'polite')):
            return 1
        return 2

    def _pos_rank(entry):
        if pos in {'particle', 'auxiliary verb', 'conjunction', 'interjection', 'prefix', 'suffix'} and any(p == pos for s in entry.senses for p in (s.pos or [])):
            return 0
        if any(p == 'particle' for s in entry.senses for p in (s.pos or [])):
            return 1
        return 2

    def _form_rank(entry):
        return 0 if not entry.kanji_forms else 1

    return sorted(entries, key=lambda entry: (_reading_rank(entry), _word_rank(entry), _context_rank(entry), _sense_rank(entry), _pos_rank(entry), _form_rank(entry)))


def _sort_entry_senses(senses, contextual_pos):
    """Bubble senses whose POS tags match the contextual POS to the top."""
    if not senses or not contextual_pos:
        return senses
    def _sense_priority(sense):
        pos_tags = getattr(sense, 'pos', None) or []
        for p in pos_tags:
            if contextual_pos in re.split(r'[^a-z]+', p.lower()):
                return 0
        return 1
    return sorted(senses, key=_sense_priority)


def _append_ocr_line(lines, text, x1, y1, x2, y2, vertical):
    line_bbox = {'x': x1, 'y': y1, 'width': x2 - x1, 'height': y2 - y1}
    chars = list(text)
    wt = [1.0 for _ in chars]
    if wt and chars[-1] in '。、！）」』】〙〛〕\u3001\u3002\uff01\uff09':
        wt[-1] = 0.3
    total_w = sum(wt)
    accum = 0.0
    words = []
    for ch, cw in zip(chars, wt):
        if vertical:
            cw = cw / total_w * line_bbox['height']
            y0 = line_bbox['y'] + int(accum)
            accum += cw
            y1w = line_bbox['y'] + int(accum)
            words.append({'text': ch, 'bounding_rect': {'x': line_bbox['x'], 'y': y0, 'width': line_bbox['width'], 'height': y1w - y0}})
        else:
            cw = cw / total_w * line_bbox['width']
            x0 = line_bbox['x'] + int(accum)
            accum += cw
            x1w = line_bbox['x'] + int(accum)
            words.append({'text': ch, 'bounding_rect': {'x': x0, 'y': line_bbox['y'], 'width': x1w - x0, 'height': line_bbox['height']}})
    lines.append({'text': text, 'words': words})


class KwaScreenApp:
    def __init__(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.msg_queue = queue.Queue()
        self.active = False
        self._ocr_gen = 0
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
        self.skip_numeric_only = True
        self.show_translation = TRANSLATOR != "none"
        self.translator = TRANSLATOR
        self.dictionary_type = "English"
        self.region_detect_scale = 100
        self.max_dict_entries = 4
        self.max_dict_senses = 4
        self.show_ocr_text = True
        self.show_furigana = True
        self.show_in_region_translation = False
        self.in_region_auto_threshold = 0
        self.japanese_font = "Meiryo"
        self.japanese_font_size = 16
        self.font_size_en = 10
        self.snip_window = None
        self.hk_capture = {"mod": MOD_CONTROL | MOD_ALT | MOD_SHIFT | MOD_NOREPEAT, "vk": ord('E'), "display": "Ctrl+Alt+Shift+E"}
        self.hk_snip = {"mod": MOD_CONTROL | MOD_ALT | MOD_SHIFT | MOD_NOREPEAT, "vk": ord('R'), "display": "Ctrl+Alt+Shift+R"}
        self.hk_settings = {"mod": MOD_CONTROL | MOD_ALT | MOD_SHIFT | MOD_NOREPEAT, "vk": ord('S'), "display": "Ctrl+Alt+Shift+S"}
        self._hk_dirty = threading.Event()
        self.settings = SettingsManager(self)
        self.settings.load()
        translation_service.load_cache(self.translator)

        if self.dictionary_type == "Monolingual":
            self.root.after(0, self._check_dict_files)

        # Pre-warm PaddleOCR in background so first scan doesn't pay model-load cost
        self._prewarm_event = threading.Event()
        threading.Thread(target=self._prewarm_paddle, daemon=True).start()

        # Start checking the queue for trigger events or translation results
        self.root.after(100, self.check_queue)

        # Start background command listener for the Launcher
        threading.Thread(target=self._start_command_listener, daemon=True).start()


    def _start_command_listener(self):
        """Listen for remote commands from the Launcher via a local TCP socket."""
        port = 54321
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind(('127.0.0.1', port))
                s.listen(5)
                while True:
                    conn, addr = s.accept()
                    with conn:
                        try:
                            data = conn.recv(1024).decode('utf-8').strip()
                            if data == "toggle_settings":
                                self.msg_queue.put(("toggle_settings", None))
                        except Exception:
                            pass
        except Exception:
            pass

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
                        self.release_capture()
                    else:
                        try:
                            self.capture_window()
                        except Exception:
                            import traceback
                            traceback.print_exc(file=sys.stdout)
                elif msg_type == "trigger_snip":
                    if self.snip_window:
                        self._close_snip()
                    else:
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
                    cw, ch = getattr(self, '_last_crop_size', (0, 0))
                    dims = f"({cw}x{ch})" if cw else ""
                    vc = getattr(self, '_last_ocr_vcount', 0)
                    hc = getattr(self, '_last_ocr_hcount', 0)
                    mode = ""
                    if vc and hc: mode = f" [H{hc} V{vc}]"
                    elif hc: mode = f" [H{hc}]"
                    elif vc: mode = f" [V{vc}]"
                    print(f"OCR: {data} regions, {ocr_ms:.0f}ms {dims}{mode}")
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
                    print(f"Process cache: {dh} hits, {dm} misses, {ci.currsize} entries")
                    dh2 = translation_service.trans_hits - getattr(self, '_prev_trans_hits', 0)
                    dm2 = translation_service.trans_misses - getattr(self, '_prev_trans_misses', 0)
                    self._prev_trans_hits = translation_service.trans_hits
                    self._prev_trans_misses = translation_service.trans_misses
                    print(f"Translation cache: {dh2} hits, {dm2} misses, {translation_service.cache_size()} entries")
                    print(f"Total: {total:.0f}ms\n")
                    # Re-render card if currently showing (data was updated in-place)
                    if self._card_window and self._card_data_idx >= 0:
                        self._render_card()
                    self._update_in_region_translations()
                elif msg_type == "cached_translations_ready":
                    if self._card_window and self._card_data_idx >= 0:
                        self._render_card()
                    self._update_in_region_translations()
                elif msg_type == "toggle_settings":
                    self.settings.toggle()
        except queue.Empty:
            pass
        except Exception:
            import traceback
            traceback.print_exc(file=sys.stdout)
        self._poll_ctrl_state()
        self.root.after(50, self.check_queue)

    def trigger(self):
        self.msg_queue.put(("trigger", None))

    def trigger_snip(self):
        self.msg_queue.put(("trigger_snip", None))

    def trigger_settings(self):
        self.msg_queue.put(("toggle_settings", None))

    def _switch_translator(self, val):
        translation_service.save_cache(self.translator)
        self.translator = val
        translation_service.load_cache(val)
        self.settings.save()
        if val == "deepl":
            self._ensure_deepl_key()

    def _check_dict_files(self):
        has_sankoku = os.path.exists(SANKOKU_DB)
        has_kanki = os.path.exists(KANKI_DB)
        if has_sankoku and has_kanki:
            return True
        msg = None
        if not has_sankoku and not has_kanki:
            msg = ("三省堂 and 漢検 dictionary files not found.\n\n"
                   "Phrases will use JMdict/英和辞典, kanji info will be limited.\n\n"
                   "Please read the README in the KwaScreenTLMonolingual folder for setup instructions.")
            self.dictionary_type = "English"
            self.settings.save()
        elif not has_sankoku:
            msg = ("三省堂 dictionary (sankokudict.db) not found.\n\n"
                   "Only kanji info will be shown in monolingual mode; phrases will use JMdict/英和辞典.")
        elif not has_kanki:
            msg = ("漢検 dictionary (kankidict.db) not found.\n\n"
                   "Kanji info will only use JMdict data.")
        if msg:
            parent = self.settings.window or self.root
            tkmb.showwarning("Monolingual Dictionary", msg, parent=parent)
        return has_sankoku and has_kanki

    def _ensure_deepl_key(self):
        if translation_service.get_deepl_api_key():
            return
        parent = self.settings.window or self.root
        key = tksd.askstring("DeepL API Key", "Enter your DeepL API key:", parent=parent)
        if key:
            key = key.strip()
            try:
                translation_service.set_deepl_api_key(key)
            except Exception as e:
                import tkinter.messagebox as tkmb
                tkmb.showerror("Error", f"Failed to save API key:\n{e}", parent=parent)

    def _purge_caches(self):
        translation_service.purge_all_caches()

    def _refresh_hover_card(self):
        self._hide_card()
        if self.current_hover_idx >= 0 and self.current_hover_idx < len(self.ocr_boxes):
            self._show_card(self.current_hover_idx)

    def _in_region_active(self):
        if self.translator == "none":
            return False
        if self.show_in_region_translation:
            return True
        if self.in_region_auto_threshold > 0 and len(self.ocr_boxes) >= self.in_region_auto_threshold:
            return True
        return False

    def _refresh_in_region_translations(self):
        """Called when the in-region translation setting toggles."""
        self._refresh_box_alphas()
        self._update_in_region_translations()

    def _refresh_box_alphas(self):
        """Update window alpha for all box windows based on in-region setting."""
        alpha = 0xFF if self._in_region_active() else 0xBB
        for win, _, _ in self._box_windows:
            try:
                hwnd = user32.GetAncestor(win.winfo_id(), 2)
                user32.SetLayeredWindowAttributes(hwnd, 0, alpha, LWA_ALPHA)
            except Exception:
                pass

    def _update_in_region_translations(self):
        """Draw English translation text directly on each OCR box overlay."""
        active = self._in_region_active()
        for idx, (win, canvas, box_idx) in enumerate(self._box_windows):
            if box_idx >= len(self.ocr_boxes):
                continue
            canvas.delete("in_region_trans")
            canvas.delete("in_region_bg")
            if not active:
                continue
            box = self.ocr_boxes[box_idx]
            eng = box['data'].get('english', '')
            if not eng:
                continue
            bbox = box['orig_bbox']
            bw = max(int(bbox['width']), 4)
            bh = max(int(bbox['height']), 4)
            # White background to cover Japanese text
            canvas.create_rectangle(
                BOX_PAD, BOX_PAD, BOX_PAD + bw, BOX_PAD + bh,
                fill="white", outline="", tags="in_region_bg"
            )
            # Find largest font size that fits
            pad = 4
            max_w = bw - pad * 2
            max_h = bh - pad * 2
            font_size = max(8, bh // 3)
            # Try to fit in one line first, shrink if needed
            f = tkfont.Font(family="Segoe UI", size=font_size)
            while font_size > 8:
                f.configure(size=font_size)
                tw = f.measure(eng)
                th = f.metrics("linespace")
                if tw <= max_w and th <= max_h:
                    break
                font_size -= 1
            f.configure(size=max(8, font_size))
            # If still too wide, use wrapping
            final_fs = max(8, font_size)
            canvas.create_text(
                BOX_PAD + bw // 2, BOX_PAD + bh // 2,
                text=eng, font=("Segoe UI", final_fs),
                fill="#1c1c1e", anchor="center",
                width=max_w if f.measure(eng) > max_w else 0,
                tags="in_region_trans"
            )

    def _retranslate_boxes(self):
        """Re-translate all OCR boxes whose english field is empty."""
        boxes = list(self.ocr_boxes)
        svc = self.translator
        def _do():
            errors = []
            for box in boxes:
                text = box['data'].get('original', '')
                if text and not box['data'].get('english'):
                    try:
                        eng = translation_service.translate_or_cached(text, service=svc)
                        if translation_service.is_error(eng):
                            errors.append(eng)
                        else:
                            box['data']['english'] = eng
                    except Exception:
                        pass
            def _done():
                self._refresh_hover_card()
                if errors:
                    msg = "\n".join(dict.fromkeys(errors))
                    tkmb.showwarning("Translation Error", msg, parent=self.root)
            self.root.after(0, _done)
        threading.Thread(target=_do, daemon=True).start()

    def capture_window(self):
        if self.active:
            self.release_capture()
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
        self.overlay_x = mx_off + win_local['x']
        self.overlay_y = my_off + win_local['y']
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
        self._ocr_gen += 1
        threading.Thread(
            target=self.process_ocr,
            args=(self.pil_img, win_local, self._ocr_gen, None),
            daemon=True
        ).start()

    # ── Snip Capture (drag-select region) ───────────────────────────────────

    def enter_snip_mode(self):
        if self.active:
            self.release_capture()
        self._prev_focus_hwnd = user32.GetForegroundWindow()
        # Force focus back to game window now so subsequent focus polls keep boxes visible
        if self._prev_focus_hwnd:
            try:
                user32.SetForegroundWindow(self._prev_focus_hwnd)
            except Exception:
                pass
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

        self.capture_snip_region(screen_rect, ml, mt)

    def capture_snip_region(self, screen_rect, mx_off=0, my_off=0):
        """Capture a user-selected region (screen coords: left, top, right, bottom)."""
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

        self._ocr_gen += 1
        threading.Thread(
            target=self.process_ocr,
            args=(self.pil_img, win_local, self._ocr_gen, 100),
            daemon=True
        ).start()

    def run_ocr_paddle(self, win_crop):
        """Run PaddleOCR on the crop, return lines with per-char bounding boxes."""
        ocr = get_paddle_ocr()
        import numpy as np
        img_array = np.array(win_crop)
        inner = ocr.paddlex_pipeline._pipeline

        ds = getattr(self, 'region_detect_scale', 100)
        use_pre = ds > 0
        oh, ow = img_array.shape[:2]
        lines = []
        v_count = h_count = 0

        if use_pre:
            dw = max(int(ow * ds / 100), 1)
            dh = max(int(oh * ds / 100), 1)
            ratio_x = ow / dw
            ratio_y = oh / dh
            det_img = np.array(Image.fromarray(img_array).resize((dw, dh), Image.LANCZOS))
            det_gen = inner.text_det_model(det_img)
            det_items = list(det_gen)
            if det_items:
                r = det_items[0]
                dt_polys = np.asarray(r.get('dt_polys', []), dtype=np.float64)
                if dt_polys.ndim == 3 and dt_polys.shape[1:] == (4, 2):
                    for poly in dt_polys:
                        xs = [p[0] * ratio_x for p in poly]
                        ys = [p[1] * ratio_y for p in poly]
                        x1 = max(0, int(min(xs)))
                        y1 = max(0, int(min(ys)))
                        x2 = min(ow, int(max(xs)))
                        y2 = min(oh, int(max(ys)))
                        if x2 - x1 < 8 or y2 - y1 < 8:
                            continue
                        crop = img_array[y1:y2, x1:x2]
                        vertical = (y2 - y1) > (x2 - x1)
                        if vertical: v_count += 1
                        else: h_count += 1
                        texts = []
                        if vertical:
                            crop_res = list(ocr.predict(crop))
                            if crop_res:
                                r = crop_res[0]
                                for t in r.get('rec_texts', []):
                                    t = re.sub(r'\s+', '', t).strip()
                                    if t:
                                        texts.append(t)
                        else:
                            rec_gen = inner.text_rec_model(crop)
                            rec_items = list(rec_gen)
                            if rec_items and hasattr(rec_items[0], 'get'):
                                t = rec_items[0].get('rec_text', '')
                                t = re.sub(r'\s+', '', t).strip()
                                if t:
                                    texts.append(t)
                        if not texts:
                            continue
                        text = ''.join(texts)
                        _append_ocr_line(lines, text, x1, y1, x2, y2, vertical)
        else:
            ocr_results = list(ocr.predict(img_array))
            if ocr_results:
                r = ocr_results[0]
                dt_polys = np.asarray(r.get('dt_polys', []), dtype=np.float64)
                rec_texts = r.get('rec_texts', [])
                if dt_polys.ndim == 3 and dt_polys.shape[1:] == (4, 2):
                    for poly, text in zip(dt_polys, rec_texts):
                        if not text:
                            continue
                        xs = [p[0] for p in poly]
                        ys = [p[1] for p in poly]
                        x1 = max(0, int(min(xs)))
                        y1 = max(0, int(min(ys)))
                        x2 = min(ow, int(max(xs)))
                        y2 = min(oh, int(max(ys)))
                        if x2 - x1 < 8 or y2 - y1 < 8:
                            continue
                        vertical = (y2 - y1) > (x2 - x1)
                        if vertical: v_count += 1
                        else: h_count += 1
                        _append_ocr_line(lines, text, x1, y1, x2, y2, vertical)

        self._last_ocr_vcount = v_count
        self._last_ocr_hcount = h_count
        return lines

    def process_ocr(self, pil_img, win_local, ocr_gen, detect_scale=None):
        """Run OCR on the focused window crop only, then offset boxes to screen coords.
        `ocr_gen` is a generation counter used to discard stale results if a new scan starts."""
        win_x, win_y = win_local['x'], win_local['y']
        win_crop = pil_img.crop((win_x, win_y, win_x + win_local['w'], win_y + win_local['h']))
        
        saved = self.region_detect_scale
        if detect_scale is not None:
            self.region_detect_scale = detect_scale
        
        try:
            # Phase 0: OCR
            ocr_start = time.time()
            lines = self.run_ocr_paddle(win_crop)
            ocr_time = (time.time() - ocr_start) * 1000
            self._last_ocr_ms = ocr_time
            self._last_crop_size = (win_crop.width, win_crop.height)

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
                if self.skip_non_japanese and not contains_japanese(text):
                    continue
                if self.skip_numeric_only and text.isdigit():
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
            if self._ocr_gen != ocr_gen:
                return
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
                uncached_texts = []
                uncached_indices = []
                for i, t in enumerate(texts):
                    cached = translation_service.cache_lookup(t)
                    if cached is not None:
                        eng = cached
                        boxes[i]['data']['english'] = eng
                        h_extra = (len(eng) // 40) * 16
                        boxes[i]['h'] = boxes[i]['orig_bbox']['height'] + 130 + h_extra
                    else:
                        translation_service.trans_misses += 1
                        uncached_texts.append(t)
                        uncached_indices.append(i)
                # Push cached translations immediately (card re-render only, no stats)
                if self._ocr_gen == ocr_gen:
                    self.msg_queue.put(("cached_translations_ready", None))
                if uncached_texts:
                    if self.translator == "deepl":
                        batch_results = translation_service.translate_deepl_batch(uncached_texts)
                    else:
                        batch_results = translation_service.translate_google_batch(uncached_texts)
                    batch_errors = []
                    for t, res in zip(uncached_texts, batch_results):
                        if not translation_service.is_error(res):
                            translation_service.cache_store(t, res)
                        else:
                            batch_errors.append(res)
                    translation_service.cache_trim()
                    translation_service.save_cache(self.translator)
                    for idx, res in zip(uncached_indices, batch_results):
                        if idx < len(boxes) and res is not None and not translation_service.is_error(res):
                            boxes[idx]['data']['english'] = res
                            h_extra = (len(res) // 40) * 16
                            boxes[idx]['h'] = boxes[idx]['orig_bbox']['height'] + 130 + h_extra
                    if batch_errors and self._ocr_gen == ocr_gen:
                        msg = "\n".join(dict.fromkeys(batch_errors))
                        self.root.after(0, lambda m=msg: tkmb.showwarning("Translation Error", m, parent=self.root))
                self._last_translation_ms = (time.time() - trans_start) * 1000
            else:
                self._last_translation_ms = 0

            # Notify main thread that data is fully ready (final push)
            if self._ocr_gen != ocr_gen:
                return
            self.msg_queue.put(("ocr_data_ready", len(boxes)))

        except Exception:
            import traceback
            traceback.print_exc()
            traceback.print_exc(file=sys.stdout)
            if self._ocr_gen == ocr_gen:
                self.msg_queue.put(("ocr_boxes_ready", []))
        finally:
            self.region_detect_scale = saved

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
                box_alpha = 0xFF if self._in_region_active() else 0xBB
                user32.SetLayeredWindowAttributes(hwnd, 0, box_alpha, LWA_ALPHA)
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
            canvas.bind("<Button-2>", lambda e, i=idx: self._box_middle_click(e, i))

            # Focus this box window so check_focus doesn't hide the overlay
            if idx == 0:
                try:
                    win.focus_force()
                except Exception:
                    pass

        # Escape on the box window itself
            win.bind("<Escape>", lambda e: self.release_capture())
            win.bind("<KeyPress-Control_L>", lambda e: self._on_ctrl_key(True))
            win.bind("<KeyPress-Control_R>", lambda e: self._on_ctrl_key(True))
            win.bind("<KeyRelease-Control_L>", lambda e: self._on_ctrl_key(False))
            win.bind("<KeyRelease-Control_R>", lambda e: self._on_ctrl_key(False))

            self._box_windows.append((win, canvas, idx))

        self._update_in_region_translations()

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
        # Highlight this box, reset others (skip when in-region translation is on)
        if not self._in_region_active():
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
        if not self._in_region_active():
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
        wi, char_off = find_word_at_point(words, ox, oy)
        if wi < 0:
            return
        self._selection_box_idx = idx
        self._selection_start = wi
        self._selection_end = wi
        self.is_dragging = True
        self._click_time = time.time()
        self._click_char_off = char_off

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
        wi, _ = find_word_at_point(words, ox, oy)
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
            # Drag → copy selected text to clipboard + look up in dict, keep highlight
            selected = ''.join(w['text'] for wi, w in enumerate(words) if start <= wi <= end)
            if selected:
                self.root.clipboard_clear()
                self.root.clipboard_append(selected)
                self._dict_lookup_selection(selected, idx)

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
        item = get_chunk_at_offset(self._card_data.get('kakasi_items', []), self._card_hover_char_idx)[0]
        return item.get('orig', '').strip() if item else None

    def _get_hovered_chunk_dict_form(self):
        """Return the dictionary form of the hovered kakasi chunk, or orig text if none, or None."""
        if self._card_hover_char_idx < 0 or not self._card_data:
            return None
        item = get_chunk_at_offset(self._card_data.get('kakasi_items', []), self._card_hover_char_idx)[0]
        if not item:
            return None
        return item.get('dict_form', item.get('orig', '')).strip()

    def _get_hovered_chunk_pos(self):
        """Return the POS of the hovered kakasi chunk, or empty string."""
        return get_chunk_field(self._card_data.get('kakasi_items', []) if self._card_data else [],
                               self._card_hover_char_idx, 'pos')

    def _get_hovered_chunk_conj(self):
        """Return the conjugation label of the hovered kakasi chunk, or empty string."""
        return get_chunk_field(self._card_data.get('kakasi_items', []) if self._card_data else [],
                               self._card_hover_char_idx, 'conj')

    def _get_hovered_chunk_hira(self):
        """Return Sudachi's contextual hiragana reading for the hovered chunk, or empty string."""
        return get_chunk_field(self._card_data.get('kakasi_items', []) if self._card_data else [],
                               self._card_hover_char_idx, 'hira')

    def _get_combined_chunk_forms(self):
        """Return concat of adjacent chunks' original text (prev+current, current+next, prev+current+next).
        Useful for words Sudachi over-splits (e.g. ござい+ます → ございます)."""
        if self._card_hover_char_idx < 0 or not self._card_data:
            return []
        return get_combined_chunk_forms(self._card_data.get('kakasi_items', []), self._card_hover_char_idx)

    def _get_hovered_single_char(self):
        """Return the single character at the hovered position, or None."""
        if self._card_hover_char_idx < 0 or not self._card_data:
            return None
        return get_single_char_at_offset(self._card_data.get('kakasi_items', []), self._card_hover_char_idx)

    def _ensure_dict_window(self):
        if hasattr(self, '_dict_window') and self._dict_window:
            return
        self._dict_window = tk.Toplevel(self.root)
        self._dict_window.overrideredirect(True)
        self._dict_window.attributes("-topmost", True)
        self._dict_canvas = tk.Canvas(self._dict_window, borderwidth=0, highlightthickness=0, bg=CARD_BG)
        self._dict_canvas.pack(fill="both", expand=True)
        self._dict_window.bind("<Escape>", lambda e: self.release_capture())
        self._dict_window.update_idletasks()
        make_translucent(self._dict_window.winfo_id(), 0xFA)

    def _show_dict_searching_placeholder(self, word, card_w):
        """Show a 'Searching...' placeholder in the dict card while lookup runs."""
        self._ensure_dict_window()

        canvas = self._dict_canvas
        canvas.delete("all")

        title_font = (self.japanese_font, 11, "bold")
        body_font = ("Segoe UI", 10)
        tf = tkfont.Font(font=title_font)
        bf = tkfont.Font(font=body_font)
        title_h = tf.metrics("linespace")
        body_h = bf.metrics("linespace")

        pad_x = 8
        wrap_w = max(card_w - 16, 180)
        ly = 6

        canvas.create_text(pad_x, ly, text=word, font=title_font,
                           fill="#8e8e93", anchor="nw", width=wrap_w)
        ly += _nlines(tf, word, wrap_w) * title_h + 4

        canvas.create_text(pad_x, ly, text="Searching\u2026", font=body_font,
                           fill="#8e8e93", anchor="nw", width=wrap_w)
        ly += body_h + 4

        canvas.update_idletasks()
        bbox = canvas.bbox("all")
        dict_h = (bbox[3] if bbox else ly) + 8

        bump_left = self.overlay_x
        bump_right = self.overlay_x + self.overlay_w
        try:
            obb = self._card_box.get('orig_bbox', {})
            bump_left = self.overlay_x + obb.get('x', 0)
            bump_right = bump_left + obb.get('width', 0)
        except Exception:
            pass
        dict_x, dict_y = self._compute_card_position(card_w, dict_h, bump_left, bump_right)

        self._dict_window.geometry(f"{card_w}x{dict_h}+{dict_x}+{dict_y}")
        try:
            self._dict_window.deiconify()
        except Exception:
            pass
        self._dict_window.lift()
        canvas.configure(height=dict_h, width=card_w)
        try:
            self._dict_window.update_idletasks()
            hwnd = user32.GetAncestor(self._dict_window.winfo_id(), 2)
            self._apply_round_corners(hwnd, card_w, dict_h)
        except Exception:
            pass
        self._dict_window.bind("<Escape>", lambda e: self.release_capture())

    def _update_dict_card(self, single_char=False):
        """Start async dictionary lookup for the hovered word."""
        if self._card_hover_char_idx < 0 or not self._card_box:
            self._withdraw_dict_card()
            return

        hovered_chunk = self._get_hovered_chunk_text()
        if hovered_chunk:
            _log_click_event(self._card_data, hovered_chunk)

        word = self._get_hovered_single_char() if single_char else self._get_hovered_chunk_dict_form()
        if not word or not contains_japanese(word):
            self._withdraw_dict_card()
            return

        pos = '' if single_char else self._get_hovered_chunk_pos()
        conj = '' if single_char else self._get_hovered_chunk_conj()
        hira = '' if single_char else self._get_hovered_chunk_hira()
        combined = [] if single_char else self._get_combined_chunk_forms()
        _append_debug_log(f"dict card update: single_char={single_char} char_idx={self._card_hover_char_idx} word={word!r} pos={pos!r} conj={conj!r} hira={hira!r} combined={combined}")
        card_w = max(self._card_box.get('w', 200), 200, 340)
        self._dict_lookup_seq += 1
        seq = self._dict_lookup_seq
        self._withdraw_dict_card()
        self._show_dict_searching_placeholder(word, card_w)

        import threading
        t = threading.Thread(target=self._dict_lookup_thread, args=(word, card_w, seq, single_char, pos, combined, conj, hira), daemon=True)
        t.start()

    def _dict_lookup_selection(self, word, box_idx):
        """Trigger a dict lookup for arbitrary selected text (both mono and bilingual)."""
        if not word or not contains_japanese(word):
            return
        box = self.ocr_boxes[box_idx] if (0 <= box_idx < len(self.ocr_boxes)) else self._card_box
        if not box:
            return
        card_w = max(box.get('w', 200), 200, 340)
        self._dict_lookup_seq += 1
        seq = self._dict_lookup_seq
        self._withdraw_dict_card()
        self._show_dict_searching_placeholder(word, card_w)
        single_char = len(word) == 1 and _is_kanji(word)
        import threading
        t = threading.Thread(
            target=self._dict_lookup_thread,
            args=(word, card_w, seq, single_char, '', [], '', ''),
            daemon=True
        )
        t.start()

    def _get_base_form_from_jamadict(self, word):
        """Return canonical form for inflected Japanese words based on JMdict entries."""
        jam = _get_jam()
        res = jam.lookup(word)
        
        if not res or not res.entries:
            return word
        
        # Look for the most common/canonical entry
        # Prefer entries with fewer kanji forms (simpler words)
        best_entry = None
        for entry in res.entries:
            if not best_entry or len(entry.kanji_forms) < len(best_entry.kanji_forms):
                # Check if this entry has good POS (noun, verb, adj)
                has_good_pos = False
                for sense in entry.senses:
                    if sense.pos:
                        pos_tags = [p.split('(')[0] for p in sense.pos]
                        if any(p in ['名詞', '動詞', '形容詞', '副詞', '連体詞'] for p in pos_tags):
                            has_good_pos = True
                            break
                
                if has_good_pos:
                    best_entry = entry
        
        if best_entry and best_entry.kanji_forms:
            # Prefer the first kanji form
            return best_entry.kanji_forms[0].text
        elif best_entry and best_entry.kana_forms:
            # Fall back to kana
            return best_entry.kana_forms[0].text
        
        return word

    def _dict_lookup_thread(self, word, card_w, seq, single_char=False, pos='', combined=None, conj='', hira=''):
        """Background thread: perform lookup and post result to main thread."""
        try:
            if self.dictionary_type == "Monolingual":
                meanings = _get_monolingual_meanings(word)
                if not meanings:
                    for fw in _generate_fuzzy_candidates(word):
                        meanings = _get_monolingual_meanings(fw)
                        if meanings:
                            word = fw
                            break
                if meanings:
                    # Get kanji data from jamdict if single_char
                    kanji_data = []
                    if single_char:
                        jam = _get_jam()
                        kanji_chars = [c for c in word if _is_kanji(c)]
                        for uk in set(kanji_chars):
                            try:
                                char_res = jam.lookup(uk)
                                if char_res and char_res.chars:
                                    kanji_data.append(char_res.chars[0])
                            except Exception:
                                pass

                    # Build mock entry mimicking jamdict structure
                    class MonolingualSense:
                        def __init__(self, gloss, pos):
                            self.gloss = [type('Gloss', (), {'text': gloss})]
                            self.pos = pos

                    entry_kanji = meanings[0]['kanji'] or ""
                    entry_kana = meanings[0]['kana']

                    senses = [MonolingualSense(m['gloss'], m['pos']) for m in meanings]

                    class MonolingualEntry:
                        def __init__(self, senses, kana, kanji=""):
                            self.kanji_forms = [type('KForm', (), {'text': kanji})] if kanji else []
                            self.kana_forms = [type('KForm', (), {'text': kana})]
                            self.senses = senses
                            self.etym = meanings[0]['etym']
                            self.antonyms = []
                            self.xrefs = []
                            seen_ant = set()
                            seen_xr = set()
                            for m in meanings:
                                for a in m['antonyms']:
                                    if a not in seen_ant:
                                        self.antonyms.append(a)
                                        seen_ant.add(a)
                                for x in m['xrefs']:
                                    if x not in seen_xr:
                                        self.xrefs.append(x)
                                        seen_xr.add(x)

                    all_entries = [MonolingualEntry(senses, entry_kana, entry_kanji)]
                    mock_res = type('MockResult', (), {'entries': all_entries, 'chars': kanji_data})
                    self.root.after(0, self._dict_lookup_show, word, card_w, seq, mock_res, kanji_data, single_char, pos, conj, hira)
                    return
                # Fall through to jamdict for fallback

            jam = _get_jam()
            
            # Collect all candidate forms to try, including richer combined forms

            candidates = [word]
            
            # Try fuzzy spelling variations
            for fw in _generate_fuzzy_candidates(word):
                if fw not in candidates:
                    candidates.append(fw)
            
            # Try base form for inflections
            base_form = self._get_base_form_from_jamadict(word)
            if base_form != word and base_form not in candidates:
                candidates.append(base_form)
            
            # Also look up combined forms (over-split words like ござい+ます)
            extra_entries = []
            if combined is not None and combined:
                for cw in combined:
                    if cw not in candidates and contains_japanese(cw):
                        candidates.append(cw)

                # First get all entries from candidates to check IDs
                all_seen_ids = set()
                for candidate in candidates:
                    candidate_res = jam.lookup(candidate)
                    if candidate_res and candidate_res.entries:
                        all_seen_ids.update(e.idseq for e in candidate_res.entries)
                
                for cw in combined:
                    cr = jam.lookup(cw)
                    for e in cr.entries:
                        if e.idseq not in all_seen_ids:
                            all_seen_ids.add(e.idseq)
                            extra_entries.append(e)
            
            # Get entries from all candidates
            all_entries = []
            for candidate in candidates:
                candidate_res = jam.lookup(candidate)
                if candidate_res and candidate_res.entries:
                    all_entries.extend(candidate_res.entries)
            
            # Add combined form entries
            all_entries.extend(extra_entries)

            # Get kanji data for the original word (for single char mode)
            kanji_data = []
            if single_char:
                initial_res = jam.lookup(word)
                if initial_res:
                    kanji_chars = [c for c in word if _is_kanji(c)]
                    for uk in set(kanji_chars):
                        char_obj = None
                        if initial_res.chars:
                            for c_obj in initial_res.chars:
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

            _append_debug_log(f"dict lookup thread: word={word!r} candidates={candidates} entries={len(all_entries)}")

            # Create a mock result object with all entries
            class MockResult:
                def __init__(self, entries, chars):
                    self.entries = entries
                    self.chars = chars
            
            mock_res = MockResult(all_entries, kanji_data)

            if single_char:
                if not kanji_data:
                    self.root.after(0, self._dict_lookup_skip, seq)
                    return
            elif not mock_res.entries:
                self.root.after(0, self._dict_lookup_skip, seq)
                return

            self.root.after(0, self._dict_lookup_show, word, card_w, seq, mock_res, kanji_data, single_char, pos, conj, hira)
        except Exception:
            import traceback
            traceback.print_exc(file=sys.stdout)
            self.root.after(0, self._dict_lookup_skip, seq)

    def _dict_lookup_show(self, word, card_w, seq, res, kanji_data, single_char=False, pos='', conj='', hira=''):
        """Main thread: render dict card from lookup results."""
        try:
            self._dict_lookup_show_impl(word, card_w, seq, res, kanji_data, single_char, pos, conj, hira)
        except Exception:
            import traceback
            traceback.print_exc(file=sys.stdout)
            self._withdraw_dict_card()

    def _compute_card_position(self, card_w, card_h, bump_left, bump_right):
        """Return (x, y) for a card that doesn't overlap OCR boxes or the hover card."""
        try:
            v_left = user32.GetSystemMetrics(76)
            v_top = user32.GetSystemMetrics(77)
            v_right = v_left + user32.GetSystemMetrics(78)
            v_bottom = v_top + user32.GetSystemMetrics(79)
        except Exception:
            v_left, v_top, v_right, v_bottom = 0, 0, 1920, 1080

        def _overlaps(cx, cy, cw, ch):
            cr = (cx, cy, cx + cw, cy + ch)
            for ob in self.ocr_boxes:
                obb = ob.get('orig_bbox', {})
                ox = self.overlay_x + obb.get('x', 0)
                oy = self.overlay_y + obb.get('y', 0)
                ow = obb.get('width', 0)
                oh = obb.get('height', 0)
                if not (cr[2] <= ox or cr[0] >= ox + ow or cr[3] <= oy or cr[1] >= oy + oh):
                    return True
            if self._card_window and self._card_xy:
                try:
                    tx, ty = self._card_xy
                    tw = self._card_window.winfo_width()
                    th = self._card_window.winfo_height()
                    if not (cr[2] <= tx or cr[0] >= tx + tw or cr[3] <= ty or cr[1] >= ty + th):
                        return True
                except Exception:
                    pass
            return False

        try:
            if self._card_window and self._card_xy:
                tx, ty = self._card_xy
                tw = self._card_window.winfo_width()
                bump_left = min(bump_left, tx)
                bump_right = max(bump_right, tx + tw)
        except Exception:
            pass

        candidates = [
            (self.overlay_x, self.overlay_y + self.overlay_h + 4),
            (bump_left - card_w - 4, self.overlay_y + self.overlay_h + 4),
            (bump_right + 4, self.overlay_y + self.overlay_h + 4),
            (self.overlay_x - card_w - 4, self.overlay_y + self.overlay_h + 4),
            (self.overlay_x + self.overlay_w + 4, self.overlay_y + self.overlay_h + 4),
            (self.overlay_x, self.overlay_y),
            (bump_left - card_w - 4, self.overlay_y),
            (bump_right + 4, self.overlay_y),
            (self.overlay_x - card_w - 4, self.overlay_y),
            (self.overlay_x + self.overlay_w + 4, self.overlay_y),
            (self.overlay_x, v_bottom - card_h),
        ]
        fallback_x, fallback_y = self.overlay_x, v_bottom - card_h
        for cand_x, cand_y in candidates:
            cx = max(v_left, min(cand_x, v_right - card_w))
            cy = max(v_top, min(cand_y, v_bottom - card_h))
            if not _overlaps(cx, cy, card_w, card_h):
                return cx, cy
        return fallback_x, fallback_y

    def _dict_lookup_show_impl(self, word, card_w, seq, res, kanji_data, single_char=False, pos='', conj='', hira=''):
        if seq != self._dict_lookup_seq:
            return
        current = self._get_hovered_single_char() if single_char else self._get_hovered_chunk_dict_form()
        if current != word:
            self._withdraw_dict_card()
            return

        self._ensure_dict_window()

        canvas = self._dict_canvas
        canvas.delete("all")

        title_font = (self.japanese_font, 11, "bold")
        body_font = ("Segoe UI", 10)
        mono_body_font = (self.japanese_font, 10)
        pos_font = ("Segoe UI", 8, "italic")
        kanji_info_font = (self.japanese_font, 9)
        jp_font = (self.japanese_font, 10)

        tf = tkfont.Font(font=title_font)
        bf = tkfont.Font(font=body_font)
        mbf = tkfont.Font(font=mono_body_font)
        pf = tkfont.Font(font=pos_font)
        jf = tkfont.Font(font=jp_font)
        title_h = tf.metrics("linespace")
        body_h = bf.metrics("linespace")
        mono_body_h = mbf.metrics("linespace")
        pos_h = pf.metrics("linespace")
        jp_h = jf.metrics("linespace")

        pad_x = 8
        ly = 6
        wrap_w = max(card_w - 16, 180)
        wrap_w_inner = max(card_w - 22, 170)

        if conj:
            canvas.create_text(pad_x, ly, text=conj, font=pos_font, fill="#8e8e93", anchor="nw")
            ly += pos_h + 2

        self._dict_top_entry = None
        if not single_char:
            if self.dictionary_type == "Monolingual":
                if res.entries:
                    entry = res.entries[0]
                    self._dict_top_entry = entry

                    kanji_texts = [k.text for k in getattr(entry, 'kanji_forms', [])]
                    kana_texts = [k.text for k in getattr(entry, 'kana_forms', [])]
                    header = ""
                    if kanji_texts:
                        header += " / ".join(kanji_texts)
                    if kana_texts:
                        if header:
                            header += f" ({', '.join(kana_texts)})"
                        else:
                            header += ", ".join(kana_texts)

                    canvas.create_text(pad_x, ly, text=header, font=title_font,
                                       fill="#a31515", anchor="nw", width=wrap_w)
                    ly += _nlines(tf, header, wrap_w) * title_h + 4

                    meta_text = ""
                    pos_tags = []
                    for sense in entry.senses:
                        if getattr(sense, 'pos', None):
                            pos_tags.extend(sense.pos)
                    if pos_tags:
                        meta_text += " • ".join(sorted(set(pos_tags)))
                    etym = getattr(entry, 'etym', None)
                    if etym:
                        if meta_text:
                            meta_text += " "
                        meta_text += f"〔{etym}〕"
                    if meta_text:
                        canvas.create_text(pad_x, ly, text=meta_text.strip(), font=pos_font,
                                           fill="#8e8e93", anchor="nw", width=wrap_w_inner)
                        ly += _nlines(pf, meta_text.strip(), wrap_w_inner) * pos_h + 4

                    sorted_senses = _sort_entry_senses(entry.senses, pos)
                    for si, sense in enumerate(sorted_senses[:self.max_dict_senses]):
                        gloss_text = sense.gloss[0].text if sense.gloss else ""
                        num = f"{si + 1}."
                        num_w = pf.measure(num)
                        canvas.create_text(pad_x, ly, text=num, font=pos_font,
                                           fill="#ff9500", anchor="nw")
                        canvas.create_text(pad_x + 6 + num_w + 2, ly, text=gloss_text, font=mbf,
                                           fill="#1c1c1e", anchor="nw", width=wrap_w_inner - num_w - 2)
                        text_h = _nlines(mbf, gloss_text, wrap_w_inner - num_w - 2) * mono_body_h
                        ly += max(pos_h, text_h) + 4

                    antonyms = getattr(entry, 'antonyms', [])
                    if antonyms:
                        ly += 4
                        ant_text = "対義語: " + "・".join(antonyms)
                        canvas.create_text(pad_x + 6, ly, text=ant_text, font=pos_font,
                                           fill="#8e8e93", anchor="nw", width=wrap_w_inner)
                        ly += _nlines(pf, ant_text, wrap_w_inner) * pos_h + 2

                    xrefs = getattr(entry, 'xrefs', [])
                    if xrefs:
                        xr_text = "参照: " + "・".join(xrefs)
                        canvas.create_text(pad_x + 6, ly, text=xr_text, font=pos_font,
                                           fill="#8e8e93", anchor="nw", width=wrap_w_inner)
                        ly += _nlines(pf, xr_text, wrap_w_inner) * pos_h + 2
            else:
                entries = _sort_jamdict_entries(list(res.entries), pos=pos, hira=hira, word=word, conj=conj)
                if DEBUG_DICT_LOG:
                    _append_debug_log(f"dict lookup show: word={word!r} pos={pos!r} conj={conj!r} hira={hira!r} entries={len(entries)}")
                    for entry in entries[:self.max_dict_entries]:
                        kanji_texts = [k.text for k in entry.kanji_forms]
                        kana_texts = [k.text for k in entry.kana_forms]
                        glosses = [g.text for s in entry.senses[:1] for g in getattr(s, 'gloss', [])]
                        _append_debug_log(f"  ranked: kanji={kanji_texts} kana={kana_texts} gloss={glosses}")
                
                preferred_entries = list(entries)
                if pos == 'verb' and hira and any(ch in hira for ch in 'おり'):
                    for i, entry in enumerate(preferred_entries):
                        glosses = [g.text.lower() for s in entry.senses for g in getattr(s, 'gloss', [])]
                        if any(g.startswith('to be') or g.startswith('to exist') or g.startswith('to stay') for g in glosses):
                            preferred_entries.insert(0, preferred_entries.pop(i))
                            break
                
                for idx, entry in enumerate(preferred_entries[:self.max_dict_entries]):
                    if idx == 0:
                        self._dict_top_entry = entry
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
                    
                    sorted_senses = _sort_entry_senses(entry.senses, pos)
                    for si, sense in enumerate(sorted_senses[:self.max_dict_senses]):
                        glosses = ", ".join(g.text for g in sense.gloss)
                        pos_str = " • ".join(sense.pos) if sense.pos else ""
                        
                        if pos_str:
                            canvas.create_text(pad_x + 6, ly, text=pos_str, font=pos_font, fill="#8e8e93", anchor="nw", width=wrap_w_inner)
                            ly += _nlines(pf, pos_str, wrap_w_inner) * pos_h + 2
                        
                        def_text = f"{si + 1}. {glosses}"
                        canvas.create_text(pad_x + 6, ly, text=def_text, font=body_font, fill="#1c1c1e", anchor="nw", width=wrap_w_inner)
                        ly += _nlines(bf, def_text, wrap_w_inner) * body_h + 4


        kanji_chars = [c for c in word if _is_kanji(c)]
        if self._dict_top_entry is not None:
            hovered_chunk = self._get_hovered_chunk_text()
            if hovered_chunk:
                _log_click_event(self._card_data, hovered_chunk, top_entry=self._dict_top_entry)

        if single_char and not kanji_chars:
            self._withdraw_dict_card()
            return
        if single_char and kanji_chars:
            unique_kanjis = []
            seen_k = set()
            for c in kanji_chars:
                if c not in seen_k:
                    seen_k.add(c)
                    unique_kanjis.append(c)

            _rendered_any = False
            for ui, uk in enumerate(unique_kanjis):
                char_obj = None
                for c_obj in kanji_data:
                    if c_obj.literal == uk:
                        char_obj = c_obj
                        break

                ki = _get_kanji_info(uk) if self.dictionary_type == "Monolingual" else None
                has_data = bool(char_obj) or bool(ki)
                if not has_data:
                    continue

                if _rendered_any:
                    ly += 6
                    canvas.create_line(pad_x, ly, card_w - pad_x, ly, fill="#e5e5ea")
                    ly += 8
                _rendered_any = True

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
                else:
                    grade = None
                    jlpt = None
                    strokes = None
                    jlpt_str = ""

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
                    if self.dictionary_type == "Monolingual" and ki:
                        gloss = ki['gloss']
                        _circled = set('①②③④⑤⑥⑦⑧⑨⑩')
                        parts = re.split(r'([①②③④⑤⑥⑦⑧⑨⑩])', gloss)
                        i = 0
                        while i < len(parts):
                            if len(parts[i]) == 1 and parts[i] in _circled:
                                num = parts[i]
                                rest = parts[i+1] if i+1 < len(parts) else ''
                                i += 2
                            else:
                                num = ''
                                rest = parts[i]
                                i += 1
                            text = rest.strip()
                            if not text and not num:
                                continue
                            if num:
                                cw = jf.measure(num)
                                canvas.create_text(pad_x + 6, ly, text=num, font=jp_font, fill="#ff9500", anchor="nw")
                                canvas.create_text(pad_x + 6 + cw, ly, text=text, font=jp_font, fill="#1c1c1e", anchor="nw", width=wrap_w_inner - cw)
                                ly += _nlines(jf, text, wrap_w_inner - cw) * jp_h + 4
                            else:
                                canvas.create_text(pad_x + 6, ly, text=text, font=jp_font, fill="#1c1c1e", anchor="nw", width=wrap_w_inner)
                                ly += _nlines(jf, text, wrap_w_inner) * jp_h + 4
                    else:
                        eng = _get_english_meanings(uk)
                        if eng:
                            meanings = eng
                        else:
                            meanings = []
                        if meanings:
                            content = ", ".join(meanings)
                            canvas.create_text(pad_x + 6, ly, text=content, font=body_font, fill="#1c1c1e", anchor="nw", width=wrap_w_inner)
                            ly += _nlines(bf, content, wrap_w_inner) * body_h + 4
                except Exception:
                    pass
                
                # On and Kun readings
                try:
                    if self.dictionary_type == "Monolingual" and ki:
                        on_content = ki['on'] if ki['on'] else ""
                        kun_content = ki['kun'] if ki['kun'] else ""
                    else:
                        rm_groups = getattr(char_obj, 'rm_groups', [])
                        on_all = []
                        kun_all = []
                        for g in rm_groups:
                            for r in getattr(g, 'on_readings', []) or []:
                                on_all.append(str(r))
                            for r in getattr(g, 'kun_readings', []) or []:
                                kun_all.append(str(r))
                        on_content = " • ".join(on_all)
                        kun_content = " • ".join(kun_all)
                    if on_content:
                        canvas.create_text(pad_x + 6, ly, text="On:", font=("Segoe UI", 10, "bold"), fill="#8e8e93", anchor="nw")
                        ly += jp_h + 1
                        canvas.create_text(pad_x + 6, ly, text=on_content, font=jp_font, fill="#1c1c1e", anchor="nw", width=wrap_w_inner)
                        ly += _nlines(jf, on_content, wrap_w_inner) * jp_h + 4
                    if kun_content:
                        canvas.create_text(pad_x + 6, ly, text="Kun:", font=("Segoe UI", 10, "bold"), fill="#8e8e93", anchor="nw")
                        ly += jp_h + 1
                        canvas.create_text(pad_x + 6, ly, text=kun_content, font=jp_font, fill="#1c1c1e", anchor="nw", width=wrap_w_inner)
                        ly += _nlines(jf, kun_content, wrap_w_inner) * jp_h + 4
                except Exception:
                    pass

            if not _rendered_any:
                self._withdraw_dict_card()
                return

        canvas.update_idletasks()
        bbox = canvas.bbox("all")
        dict_h = (bbox[3] if bbox else ly) + 8
        if single_char:
            card_w = min(card_w, max(pad_x + 6 + wrap_w_inner + 4, 180))

        bump_left = self.overlay_x
        bump_right = self.overlay_x + self.overlay_w
        try:
            obb = self._card_box.get('orig_bbox', {})
            bump_left = self.overlay_x + obb.get('x', 0)
            bump_right = bump_left + obb.get('width', 0)
        except Exception:
            pass
        dict_x, dict_y = self._compute_card_position(card_w, dict_h, bump_left, bump_right)

        self._dict_window.geometry(f"{card_w}x{dict_h}+{dict_x}+{dict_y}")
        try:
            self._dict_window.deiconify()
        except Exception:
            pass
        self._dict_window.lift()
        canvas.configure(height=dict_h, width=card_w)
        try:
            self._dict_window.update_idletasks()
            hwnd = user32.GetAncestor(self._dict_window.winfo_id(), 2)
            self._apply_round_corners(hwnd, card_w, dict_h)
        except Exception:
            pass
        self._dict_window.bind("<Escape>", lambda e: self.release_capture())

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

    def _show_selected_translation_card(self, text, box_idx):
        """Show a dictionary-style card with translation of the selected text."""
        if not text:
            return
        box = self.ocr_boxes[box_idx] if (0 <= box_idx < len(self.ocr_boxes)) else None
        if not box:
            return

        self._hide_dict_card()

        if self.translator == "deepl":
            english = translation_service.translate_or_cached(text)
        else:
            try:
                english = translation_service.translate_google(text)
            except Exception as e:
                english = f"[Translation error: {e}]"

        if translation_service.is_error(english):
            tkmb.showwarning("Translation Error", english, parent=self.root)
            return

        card_w = max(box.get('w', 200), 200, 340)

        self._ensure_dict_window()

        canvas = self._dict_canvas
        canvas.delete("all")

        title_font_spec = (self.japanese_font, 11, "bold")
        body_font_spec = ("Segoe UI", 10)
        tf = tkfont.Font(font=title_font_spec)
        bf = tkfont.Font(font=body_font_spec)
        title_h = tf.metrics("linespace")
        body_h = bf.metrics("linespace")

        pad_x = 8
        wrap_w = max(card_w - 16, 180)
        ly = 6

        canvas.create_text(pad_x, ly, text=text, font=title_font_spec,
                           fill="#a31515", anchor="nw", width=wrap_w)
        ly += _nlines(tf, text, wrap_w) * title_h + 4

        canvas.create_text(pad_x, ly, text=english, font=body_font_spec,
                           fill="#1c1c1e", anchor="nw", width=wrap_w)
        ly += _nlines(bf, english, wrap_w) * body_h + 6

        dict_h = ly

        dict_x, dict_y = self._compute_card_position(card_w, dict_h, self.overlay_x, self.overlay_x + self.overlay_w)

        self._dict_window.geometry(f"{card_w}x{dict_h}+{dict_x}+{dict_y}")
        try:
            self._dict_window.deiconify()
        except Exception:
            pass
        self._dict_window.lift()
        canvas.configure(height=dict_h, width=card_w)
        try:
            self._dict_window.update_idletasks()
            hwnd = user32.GetAncestor(self._dict_window.winfo_id(), 2)
            self._apply_round_corners(hwnd, card_w, dict_h)
        except Exception:
            pass

        self._dict_window.bind("<Escape>", lambda e: self._withdraw_dict_card())
        canvas.bind("<Button-1>", lambda e: self._withdraw_dict_card())

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

    def _box_right_click(self, event, idx):
        """Right-click on a box → translate selected text, or single-char kanji lookup."""
        if idx < 0 or idx >= len(self.ocr_boxes):
            return

        selected_text = self._get_selected_text()
        if selected_text and contains_japanese(selected_text):
            self._show_selected_translation_card(selected_text, self._selection_box_idx)
            return

        box = self.ocr_boxes[idx]
        bbox = box['orig_bbox']
        words = box.get('words', [])
        if not words:
            return
        ox = bbox['x'] - BOX_PAD + event.x
        oy = bbox['y'] - BOX_PAD + event.y
        wi, char_off = find_word_at_point(words, ox, oy)
        if wi < 0:
            return
        self._card_hover_char_idx = char_off
        self._card_data = box['data']
        self._card_data_idx = idx
        self._card_box = box
        self._update_dict_card(single_char=True)

    def _box_middle_click(self, _event, idx):
        """Middle-click on a box → TTS."""
        text = self._get_action_text(idx)
        if text:
            threading.Thread(target=self.read_aloud, args=(text,), daemon=True).start()

    def _show_card(self, idx):
        """Display the translation card as a separate Toplevel near the hovered box."""
        if idx < 0 or idx >= len(self.ocr_boxes):
            return

        if not (self.show_crop or self.show_ocr_text or self.show_furigana or 
                self.show_romaji or self.show_translation):
            if self._card_window:
                self._card_window.destroy()
                self._card_window = None
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
        # Clamp card width so it doesn't extend off-screen
        try:
            v_left = user32.GetSystemMetrics(76)
            v_top = user32.GetSystemMetrics(77)
            v_right = v_left + user32.GetSystemMetrics(78)
            v_bottom = v_top + user32.GetSystemMetrics(79)
        except Exception:
            v_left, v_top, v_right, v_bottom = 0, 0, 1920, 1080

        max_card_w = v_right - screen_x - 8
        if card_w > max_card_w:
            card_w = max(max_card_w, 180)
            screen_x = max(v_left, screen_x)
        if screen_x + card_w > v_right:
            screen_x = v_right - card_w - 8
        screen_x = max(v_left, screen_x)

        # Estimate card height (+ padding) — must match _render_card layout
        en_font = tkfont.Font(family="Segoe UI", size=self.font_size_en, weight="bold")
        content_h = 0
        if self.show_crop and box.get('crop_pil'):
            content_h += 3 + box['crop_pil'].height + 24
        else:
            content_h += 8
        line_h = max(30, kf.metrics("linespace"))
        if self.show_ocr_text:
            content_h += line_h  # Japanese text
        furigana_size = max(8, self.japanese_font_size // 2 - 1)
        ff_temp = tkfont.Font(family=self.japanese_font, size=furigana_size)
        if self.show_furigana:
            content_h += ff_temp.metrics("linespace") + 2  # furigana + gap
        if self.show_romaji:
            content_h += 18 + 2
        if self.show_translation and not self._in_region_active():
            est_chars = max(1, (card_w - 16) // 7)
            en_lines = max(1, -(-len(data.get('english', '')) // est_chars))
            content_h += en_lines * en_font.metrics("linespace")
            content_h += 6
        card_h = content_h

        # Place card above the box (or below if not enough room)
        card_bottom = int(screen_y - 4)
        card_top = max(v_top, card_bottom - card_h)
        if card_top < v_top + 20:
            card_top = int(screen_y + bbox['height'] + 4)
            card_bottom = card_top + card_h

        win = tk.Toplevel(self.root)
        win.overrideredirect(True)
        win.geometry(f"{card_w}x{card_h}+{screen_x}+{card_top}")
        win.attributes("-topmost", True)
        canvas = tk.Canvas(win, width=card_w, height=card_h,
                           borderwidth=0, highlightthickness=0, bg=CARD_BG)
        canvas.pack()
        win.bind("<Escape>", lambda e: self.release_capture())

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
        if self.show_ocr_text:
            jp_y = ly
            canvas.create_text(pad_x, jp_y, text=data['original'],
                               font=(self.japanese_font, self.japanese_font_size, "bold"),
                               fill="#a31515", anchor="nw", tags="jp_text")
            jp_text_bottom = jp_y + line_h
        else:
            jp_text_bottom = ly


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
            if self.show_furigana and orig != hira and any(_is_kanji(c) for c in orig):
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
        if self.show_furigana:
            ly = fg_y + fg_line_h + 2
        else:
            ly = fg_y

        # Romaji
        rom_y = ly
        if self.show_romaji:
            canvas.create_text(pad_x, rom_y, text=data['romaji'],
                               font=("Segoe UI", max(7, self.font_size_en - 2), "italic"),
                               fill="#0066cc", anchor="nw", tags="romaji_text")
            ly += 18 + 2

        # English (skip if translation is shown in-region instead)
        if self.show_translation and not self._in_region_active():
            eng_y = ly
            canvas.create_text(pad_x, eng_y, text=data['english'],
                               font=("Segoe UI", self.font_size_en, "bold"),
                               fill="#1c1c1e", anchor="nw", width=card_w - 16, tags="eng_text")
            # Force full layout so bbox returns accurate wrapped dimensions
            canvas.update()
            eng_bbox = canvas.bbox("eng_text")
            if eng_bbox:
                card_h = eng_bbox[3] + 6  # bottom-y + padding
            else:
                card_h = eng_y + en_font.metrics("linespace") + 6
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
        wi, char_off = find_word_at_point(words, ox, oy)
        if wi < 0:
            return

        if not self._in_region_active():
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

    def release_capture(self):
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
        # Include the root tk window so focus tracking doesn't hide our overlays
        try:
            all_hwnds.add(user32.GetAncestor(self.root.winfo_id(), 2))
        except Exception:
            pass
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

def main():
    print("Application started")
    print("Loading OCR Model...\n")

    app = KwaScreenApp()
    app._prewarm_event.wait()
    print("Application Ready\n")
    try:
        os.remove(_APP_READY_FLAG)
    except FileNotFoundError:
        pass
    open(_APP_READY_FLAG, "w").close()

    # ── Launcher watchdog: exit if parent process dies ──────────────────────
    _launcher_pid = os.environ.get("KWASCREENTL_LAUNCHER_PID")
    if _launcher_pid:
        _PROCESS_QUERY_INFORMATION = 0x0400
        _launcher_pid = int(_launcher_pid)
        def _watch_parent():
            while True:
                time.sleep(30)
                try:
                    handle = ctypes.windll.kernel32.OpenProcess(_PROCESS_QUERY_INFORMATION, False, _launcher_pid)
                    if not handle:
                        os._exit(0)
                    ctypes.windll.kernel32.CloseHandle(handle)
                except Exception:
                    os._exit(0)
        threading.Thread(target=_watch_parent, daemon=True).start()
    # ─────────────────────────────────────────────────────────────────────────
    print(f"  {app.hk_capture['display']:20s} Capture / Uncapture window for OCR / translation")
    print(f"  {app.hk_snip['display']:20s} Snip mode (drag-select a region) (Uses above for uncapture)")
    print(f"  {app.hk_settings['display']:20s} Settings panel")
    print("  Click               Open Dictionary")
    print("  Right-Click         Kanji info")
    print("  Ctrl+Click          Kanji info (with hover highlighting)")
    print("  Middle-click        Text-to-speech")

    # Prompt for DeepL key on first launch if that translator is selected
    if app.translator == "deepl":
        app.root.after(100, app._ensure_deepl_key)

    # Start Win32 hotkey thread (RegisterHotKey with fallback to GetAsyncKeyState)
    threading.Thread(target=register_hotkey_win32, args=(app,), daemon=True).start()

    try:
        app.run()
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    main()
