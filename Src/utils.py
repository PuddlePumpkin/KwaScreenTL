import os
import re
import json
import ctypes
import ctypes.wintypes

_PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.makedirs(os.path.join(_PROJECT_DIR, "Data"), exist_ok=True)

SANKOKU_DB = os.path.join(_PROJECT_DIR, "KwaScreenTLMonolingual", "sankokudict.db")
KANKI_DB = os.path.join(_PROJECT_DIR, "KwaScreenTLMonolingual", "kankidict.db")
API_KEYS_FILE = os.path.join(_PROJECT_DIR, "Data", "apikeys.json")

_JP_PATTERN = re.compile(r'[\u3040-\u309f\u30a0-\u30ff\u4e00-\u9faf]')

def contains_japanese(text):
    return bool(_JP_PATTERN.search(text))

def _is_kanji(ch):
    return '\u4e00' <= ch <= '\u9faf'

def _is_kana(ch):
    return '\u3040' <= ch <= '\u30ff'

def find_word_at_point(words, ox, oy):
    for i, w in enumerate(words):
        if (w['x'] <= ox <= w['x'] + w['width'] and
                w['y'] <= oy <= w['y'] + w['height']):
            return i, sum(len(words[j]['text']) for j in range(i))
    return -1, -1

def get_chunk_at_offset(kakasi_items, char_idx):
    if char_idx < 0 or not kakasi_items:
        return None, -1
    coff = 0
    for i, item in enumerate(kakasi_items):
        orig = item.get('orig', '')
        end = coff + len(orig)
        if coff <= char_idx < end:
            return item, i
        coff = end
    return None, -1

def get_chunk_field(kakasi_items, char_idx, field, default=''):
    item, _ = get_chunk_at_offset(kakasi_items, char_idx)
    if item is None:
        return default
    return item.get(field, default)

def get_combined_chunk_forms(kakasi_items, char_idx):
    if char_idx < 0 or not kakasi_items:
        return []
    coff = 0
    idx = -1
    for i, item in enumerate(kakasi_items):
        orig = item.get('orig', '')
        if coff <= char_idx < coff + len(orig):
            idx = i
            break
        coff += len(orig)
    if idx < 0:
        return []
    forms = set()
    if idx + 1 < len(kakasi_items):
        forms.add(kakasi_items[idx].get('orig', '') + kakasi_items[idx + 1].get('orig', ''))
    if idx > 0:
        forms.add(kakasi_items[idx - 1].get('orig', '') + kakasi_items[idx].get('orig', ''))
    if idx > 0 and idx + 1 < len(kakasi_items):
        forms.add(kakasi_items[idx - 1].get('orig', '') + kakasi_items[idx].get('orig', '') + kakasi_items[idx + 1].get('orig', ''))
    return [f for f in forms if contains_japanese(f)]

def get_single_char_at_offset(kakasi_items, char_idx):
    item, _ = get_chunk_at_offset(kakasi_items, char_idx)
    if item is None:
        return None
    coff = 0
    for it in kakasi_items:
        o = it.get('orig', '')
        if it is item:
            off = char_idx - coff
            if 0 <= off < len(o):
                return o[off]
            return None
        coff += len(o)
    return None

def load_api_keys():
    try:
        with open(API_KEYS_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}

def save_api_keys(keys):
    with open(API_KEYS_FILE, "w") as f:
        json.dump(keys, f, indent=2)

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

user32 = ctypes.windll.user32

def make_translucent(hwnd, alpha=0xBB):
    try:
        hwnd = user32.GetAncestor(hwnd, 2)
        ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex | WS_EX_LAYERED)
        user32.SetLayeredWindowAttributes(hwnd, 0, alpha, LWA_ALPHA)
    except Exception:
        pass
