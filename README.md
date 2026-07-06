# KwaScreenTL

![Screenshot](/Docs/CaptureExampleScaled.png)

⚠️ Warning: AI vibe coded slop ⚠️

Screen translation tool for Japanese applications. Runs OCR via PaddleOCR, translates using DeepL / Google translate, and displays popup cards with dictionary data (JMdict/Jamdict).

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

| Mouse | Action |
|---|---|
| `Hover` | Show translation card |
| `Click` | Show dictionary |
| `Right-click` | Show kanji info |
| `Ctrl+Click` | Show kanji info (with hover highlight) |
| `Middle-click` | Text-to-speech |
| `Mousewheel` (over kanji) | Scroll possible kanji readings |

## Features

- Captures the active window or a selected screen region via OCR
- Displays romaji, kana, and furigana readings
- Translates Japanese text to English (DeepL or Google)
- Local offline dictionary lookups (JMdict) for words and individual kanji
- Snip mode for manual region capture
- Toggle visibility of translation and romaji display 
- Text-to-speech

## Settings

Toggles in the settings panel (`Ctrl+Alt+Shift+S`), persisted to `settings.json`:
- **Show romaji / Show translation** — hover card display
- **Skip non-Japanese text** — filter non-JP OCR results
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
