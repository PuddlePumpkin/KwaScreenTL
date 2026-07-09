# Monolingual Dictionary Setup

To enable Japanese Monolingual Mode in KwaScreenTL, you need to extract the required Yomichan/Yomitan dictionary ZIP files into the `Dicts/` folder inside this directory, then run the converter script.

## 1. Download and Extract Dictionaries

Download the following dictionaries and extract their ZIP files into `KwaScreenTLMonolingual/Dicts/`:

1. **[JA-JA] 三省堂国語辞典　第八版**
   - **Download Link**: [Google Drive](https://drive.google.com/file/d/13A5Es8kjAV6FvDx8zu4Nz5cAZgHn604c/view?usp=drive_link)
   - **Extracted Folder Name**: `[JA-JA] 三省堂国語辞典　第八版` (should contain `index.json`, `term_bank_*.json`, etc.)

2. **06 [JA-JA] 漢検漢字辞典　第二版**
   - **Download Link**: [Google Drive Folder](https://drive.google.com/drive/folders/16frMMOiqCtO-1cscRdjxuO5gUn_GmqVe)
   - **Extracted Folder Name**: `06 [JA-JA] 漢検漢字辞典　第二版` (should contain `index.json`, `term_bank_*.json`, etc.)

### Alternative Sources
- Additional dictionaries can be found in this [Google Drive Folder](https://drive.google.com/drive/folders/1LXMIOoaWASIntlx1w08njNU005lS5lez) or on the [Yomitan Dictionaries Github Page](https://github.com/MarvNC/yomitan-dictionaries).

---

## 2. Directory Structure

Once extracted, your directory layout must look like this:

```text
KwaScreenTLMonolingual/
├── Dicts/
│   ├── [JA-JA] 三省堂国語辞典　第八版/
│   │   ├── index.json
│   │   ├── term_bank_1.json
│   │   └── ...
│   └── 06 [JA-JA] 漢検漢字辞典　第二版/
│       ├── index.json
│       ├── term_bank_1.json
│       └── ...
├── Src/
│   ├── convert_dict.py
│   └── convert_kanjidict.py
├── convert_dicts.bat
├── jamdict.db
└── README.md
```

---

## 3. Generate Databases

Run **`convert_dicts.bat`** (double-click it or run from command prompt) to build:
- `sankokudict.db` (from Sanseido)
- `kankidict.db` (from Kanken)

Once generated, launch KwaScreenTL and change your dictionary setting to **Monolingual** in the settings window.
