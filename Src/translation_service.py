"""Network translation services and translation cache management."""

import os
import json
import requests
from deep_translator import GoogleTranslator
from utils import _PROJECT_DIR, load_api_keys as _load_api_keys, save_api_keys as _save_api_keys


# ── API Key Management ────────────────────────────────────────────────────────

_deepl_api_key = None

def get_deepl_api_key():
    global _deepl_api_key
    if _deepl_api_key is None:
        keys = _load_api_keys()
        _deepl_api_key = keys.get("deepl", "")
    return _deepl_api_key

def set_deepl_api_key(key):
    global _deepl_api_key
    _deepl_api_key = key
    keys = _load_api_keys()
    keys["deepl"] = key
    _save_api_keys(keys)


# ── Network Error Handling ────────────────────────────────────────────────────

def network_error_msg(service, exc):
    if isinstance(exc, requests.exceptions.Timeout):
        reason = "request timed out"
    elif isinstance(exc, requests.exceptions.ConnectionError):
        err = str(exc).lower()
        if "name resolution" in err or "getaddrinfo" in err or "name or service not known" in err:
            reason = "DNS resolution failed"
        elif "connection refused" in err:
            reason = "connection refused"
        elif "remote disconnected" in err or "connection reset" in err:
            reason = "connection lost"
        else:
            reason = "connection failed"
    else:
        reason = "no response"
    return f"[Network Error: {reason} from {service} API]"


# ── DeepL Translation ─────────────────────────────────────────────────────────

def translate_deepl(text):
    key = get_deepl_api_key()
    if not key:
        return "[DeepL: no API key - add to apikeys.json]"
    try:
        resp = requests.post(
            "https://api-free.deepl.com/v2/translate",
            headers={"Authorization": f"DeepL-Auth-Key {key}"},
            json={"text": [text], "source_lang": "JA", "target_lang": "EN"}
        )
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
        return network_error_msg("DeepL", e)
    if resp.status_code == 403:
        return "[DeepL: Unauthorized - check your API key]"
    try:
        resp.raise_for_status()
    except requests.exceptions.HTTPError:
        return f"[DeepL: HTTP {resp.status_code}]"
    return resp.json()["translations"][0]["text"]

def translate_deepl_batch(texts):
    """Translate a batch of Japanese texts to English via a single DeepL API call."""
    if not texts:
        return []
    key = get_deepl_api_key()
    if not key:
        return ["[DeepL: no API key - add to apikeys.json]"] * len(texts)
    try:
        resp = requests.post(
            "https://api-free.deepl.com/v2/translate",
            headers={"Authorization": f"DeepL-Auth-Key {key}"},
            json={"text": texts, "source_lang": "JA", "target_lang": "EN"}
        )
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
        return [network_error_msg("DeepL", e)] * len(texts)
    if resp.status_code == 403:
        return ["[DeepL: Unauthorized - check your API key]"] * len(texts)
    try:
        resp.raise_for_status()
    except requests.exceptions.HTTPError:
        return [f"[DeepL: HTTP {resp.status_code}]"] * len(texts)
    return [t["text"] for t in resp.json()["translations"]]


# ── Google Translation ────────────────────────────────────────────────────────

def translate_google(text):
    try:
        return GoogleTranslator(source='ja', target='en').translate(text)
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
        return network_error_msg("Google", e)

def translate_google_batch(texts):
    """Translate a list of Japanese texts to English one at a time via Google."""
    gt = GoogleTranslator(source='ja', target='en')
    results = []
    for t in texts:
        try:
            results.append(gt.translate(t))
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            results.append(network_error_msg("Google", e))
    return results


# ── Translation Cache ─────────────────────────────────────────────────────────

_translation_cache = {}
trans_hits = 0
trans_misses = 0
_TRANSLATION_CACHE_MAX = 1000

def _cache_path(service):
    return os.path.join(_PROJECT_DIR, "Data", f"translation_cache_{service}.json")

def load_cache(service):
    global _translation_cache
    _translation_cache = {}
    try:
        with open(_cache_path(service), "r") as f:
            raw = json.load(f)
    except Exception:
        return
    for k, v in raw.items():
        if isinstance(v, dict) and "translation" in v:
            _translation_cache[k] = v
        else:
            _translation_cache[k] = {"translation": v, "hits": 0}
    cache_trim()

def save_cache(service):
    try:
        with open(_cache_path(service), "w") as f:
            json.dump(_translation_cache, f, indent=2)
    except Exception:
        pass

def cache_trim():
    if len(_translation_cache) <= _TRANSLATION_CACHE_MAX:
        return
    sorted_items = sorted(_translation_cache.items(), key=lambda x: x[1]["hits"])
    for k, _ in sorted_items[:len(sorted_items) - _TRANSLATION_CACHE_MAX]:
        del _translation_cache[k]

def reset_cache_stats():
    global trans_hits, trans_misses
    trans_hits = 0
    trans_misses = 0

def purge_all_caches():
    for svc in ("deepl", "google"):
        try:
            os.remove(_cache_path(svc))
        except Exception:
            pass
    _translation_cache.clear()
    reset_cache_stats()

def cache_lookup(text):
    """Return cached translation string for *text*, or None."""
    entry = _translation_cache.get(text)
    if entry is not None:
        global trans_hits
        trans_hits += 1
        entry["hits"] += 1
        return entry["translation"]
    return None

def cache_size():
    """Return the number of entries in the translation cache."""
    return len(_translation_cache)

def is_error(result):
    """Return True if a translation result is an actual error string."""
    if not isinstance(result, str) or not result.startswith("["):
        return False
    known_errors = (
        "[Network Error:",
        "[DeepL:",
    )
    return result.startswith(known_errors)

def cache_store(text, translation):
    """Store a successful translation in the cache. Silently ignores errors."""
    if is_error(translation):
        return
    _translation_cache[text] = {"translation": translation, "hits": 1}
    cache_trim()


# ── Cached Translation ────────────────────────────────────────────────────────

def translate_or_cached(text, service="deepl"):
    """Return cached translation result or translate and cache it."""
    hit = cache_lookup(text)
    if hit is not None:
        return hit
    global trans_misses
    trans_misses += 1
    if service == "deepl":
        result = translate_deepl(text)
    else:
        result = translate_google(text)
    if not result.startswith("["):
        cache_store(text, result)
        save_cache(service)
    return result
