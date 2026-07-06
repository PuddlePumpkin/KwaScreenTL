# KwaScreenTL

⚠️ Warning: AI vibe coded slop ⚠️

Screen translation tool for Japanese applications. Captures the active monitor, runs OCR via PaddleOCR, translates using DeepL, and displays popup cards with dictionary data (JMdict/Jamdict).

## Setup

1. Python 3.10+ recommended.
2. Run `setup.bat` to create a virtual environment and install dependencies.
3. Run `run.bat` to start the app.

## Usage

| Key | Action |
|---|---|
| `Ctrl+Alt+Shift+E` | Capture hovered window / dismiss OCR boxes |
| `Ctrl+Alt+Shift+R` | Snip mode (drag-select a region) |
| `Ctrl+Alt+Shift+S` | Toggle settings panel |
| `Escape` | (in snip mode) Cancel selection |

| Mouse (on OCR box) | Action |
|---|---|
| `Hover` | Show translation card |
| `Click+drag` | Select text to copy |
| `Ctrl+Click` | Show kanji info |
| `Right-click` | Open in Jisho |
| `Shift+Right-click` | Open in DeepL |
| `Middle-click` | Text-to-speech |
| `Mousewheel` (over card) | Scroll |

## Features

- **PaddleOCR** (`japan` model, ONNX runtime) with per-character bounding boxes
- **DeepL** translation (free API) with romaji/kana via pykakasi
- **JMdict** (via Jamdict) for word/kanji definitions — local offline dictionary
- **Snip mode** for manual region capture
- **Skip non-Japanese** mode to filter out non-JP OCR results
- **Text-to-speech** via edge-tts (ja-JP-NanamiNeural)
- All library output (paddle, onnxruntime) redirected to `app.log`

## Settings

Toggles in the settings panel (`Ctrl+Alt+Shift+S`), persisted to `settings.json`:
- **Show romaji / Show translation** — hover card display
- **Skip non-Japanese text** — filter non-JP OCR results
- **Show cropped image** — debug: show the OCR crop in the hover card
- **OCR Prepass Scale** — % scale for faster region detection (25/50/75/100)

## Used Libraries

- [PaddleOCR](https://github.com/PaddlePaddle/PaddleOCR) — Japanese OCR
- [Deep-Translator](https://github.com/nidhaloff/deep-translator) — Google translation
- [pykakasi](https://github.com/miurahr/pykakasi) — Japanese → romaji/kana
- [jamdict](https://github.com/neocl/jamdict) — JMdict dictionary lookup
- [SudachiPy](https://github.com/WorksApplications/SudachiPy) — Morphological analysis
- [jaconv](https://github.com/ikegami-yukino/jaconv) — Kana conversion
- [mss](https://github.com/BoboTiG/python-mss) — Screen capture
- [Pillow](https://python-pillow.org/) — Image processing
- [edge-tts](https://github.com/rany2/edge-tts) — Text-to-speech
- [PaddlePaddle](https://github.com/PaddlePaddle/Paddle) / [ONNX Runtime](https://github.com/microsoft/onnxruntime) — OCR backend
- [requests](https://github.com/psf/requests) — DeepL API calls (direct)
