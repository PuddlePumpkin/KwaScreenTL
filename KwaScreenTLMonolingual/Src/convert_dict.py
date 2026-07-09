import os
import json
import sqlite3
import re
import sys

sys.stdout.reconfigure(encoding='utf-8')

# ── Structured content parsing ──────────────────────────────────────────

def extract_text(node):
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(extract_text(item) for item in node)
    if isinstance(node, dict):
        if node.get("tag") == "img":
            title = node.get("title", "")
            return f"({title})" if title else ""
        content = node.get("content")
        if content:
            return extract_text(content)
    return ""


def collect_named_elements(node):
    """Walk tree and yield (name, text, node) for every element with data.name."""
    if isinstance(node, dict):
        data = node.get("data")
        if isinstance(data, dict):
            name = data.get("name")
            if name:
                yield (name, extract_text(node), node)
        content = node.get("content")
        if content:
            yield from collect_named_elements(content)
    elif isinstance(node, list):
        for item in node:
            yield from collect_named_elements(item)


def collect_named_elements_filtered(node, target_names):
    """Like collect_named_elements but only yields entries matching target_names."""
    for name, text, subnode in collect_named_elements(node):
        if name in target_names:
            yield (name, text, subnode)


def find_sense_groups(node):
    """Return list of 語義 sub-trees found in the content tree."""
    senses = []
    if isinstance(node, dict):
        data = node.get("data")
        if isinstance(data, dict) and data.get("name") == "語義":
            senses.append(node)
        content = node.get("content")
        if content:
            senses.extend(find_sense_groups(content))
    elif isinstance(node, list):
        for item in node:
            senses.extend(find_sense_groups(item))
    return senses


def find_first_by_name(node, target_name):
    """Find the first node with the given data.name."""
    if isinstance(node, dict):
        data = node.get("data")
        if isinstance(data, dict) and data.get("name") == target_name:
            return node
        content = node.get("content")
        if content:
            result = find_first_by_name(content, target_name)
            if result:
                return result
    elif isinstance(node, list):
        for item in node:
            result = find_first_by_name(item, target_name)
            if result:
                return result
    return None


def extract_kaisetsubu_text(node):
    """Extract plain text from the 解説部 section only (skip 見出部)."""
    kaisetsu = find_first_by_name(node, "解説部")
    if kaisetsu:
        return extract_text(kaisetsu)
    return ""


# ── POS extraction ──────────────────────────────────────────────────────

def extract_pos_from_node(node):
    """Extract POS text from the 見出部 section (品詞 element)."""
    items = list(collect_named_elements_filtered(node, {"品詞"}))
    if items:
        return items[0][1]
    return ""


def map_pos_tags(pos_text):
    tags = []
    if not pos_text:
        return tags
    if '名' in pos_text or '代' in pos_text:
        tags.extend(['noun', '名詞'])
    if any(k in pos_text for k in ['動', 'サ', '五', '上一', '下一', '自', '他']):
        tags.extend(['verb', '動詞'])
    if any(k in pos_text for k in ['形', 'ダナ', 'トタル']):
        tags.extend(['adjective', '形容詞'])
    if '副' in pos_text:
        tags.extend(['adverb', '副詞'])
    if '連体' in pos_text:
        tags.extend(['adj-pn', '連体詞'])
    if '感' in pos_text:
        tags.extend(['interjection', '感嘆詞'])
    if '助' in pos_text:
        tags.extend(['particle', '助詞'])
    return list(set(tags))


# ── Helpers ─────────────────────────────────────────────────────────────

def has_kanji(text):
    return any('\u4e00' <= ch <= '\u9faf' for ch in text)


def flush_batch(cur, entry_b, kanji_b, kana_b, sense_b, pos_b,
                gloss_b, antonym_b, xref_b, etym_b, misc_b, audit_b,
                def_tags_map):
    # Build enriched audit entries with def_tags
    enriched_audit = []
    for idseq, upd_date, upd_detl in audit_b:
        if idseq in def_tags_map and def_tags_map[idseq]:
            dt_str = ",".join(sorted(def_tags_map[idseq]))
            upd_detl += f",def_tag:{dt_str}"
        enriched_audit.append((idseq, upd_date, upd_detl))
    cur.executemany("INSERT OR IGNORE INTO Entry (idseq) VALUES (?)", entry_b)
    for idseq, text in kanji_b:
        cur.execute("INSERT INTO Kanji (idseq, text) VALUES (?, ?)", (idseq, text))
    for idseq, text, nokanji in kana_b:
        cur.execute("INSERT INTO Kana (idseq, text, nokanji) VALUES (?, ?, ?)", (idseq, text, nokanji))
    for sid, idseq in sense_b:
        cur.execute("INSERT INTO Sense (ID, idseq) VALUES (?, ?)", (sid, idseq))
    cur.executemany("INSERT INTO pos (sid, text) VALUES (?, ?)", pos_b)
    cur.executemany("INSERT INTO SenseGloss (sid, lang, gend, text) VALUES (?, ?, ?, ?)", gloss_b)
    cur.executemany("INSERT INTO antonym (sid, text) VALUES (?, ?)", antonym_b)
    cur.executemany("INSERT INTO xref (sid, text) VALUES (?, ?)", xref_b)
    cur.executemany("INSERT INTO Etym (idseq, text) VALUES (?, ?)", etym_b)
    cur.executemany("INSERT INTO misc (sid, text) VALUES (?, ?)", misc_b)
    cur.executemany("INSERT INTO Audit (idseq, upd_date, upd_detl) VALUES (?, ?, ?)", enriched_audit)


# ── Main conversion ─────────────────────────────────────────────────────

def convert():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    mono_dir = os.path.abspath(os.path.join(base_dir, '..'))

    # Locate the dictionary folder inside Dicts/ containing index.json (skip kanji dict "06")
    dict_dir = None
    dicts_dir = os.path.join(mono_dir, 'Dicts')
    if os.path.isdir(dicts_dir):
        for item in os.listdir(dicts_dir):
            if "06" in item:
                continue
            item_path = os.path.join(dicts_dir, item)
            if os.path.isdir(item_path) and os.path.exists(os.path.join(item_path, 'index.json')):
                dict_dir = item_path
                break
    if not dict_dir:
        print("Error: Could not find dictionary folder in ../Dicts/ containing index.json")
        sys.exit(1)
    print(f"Found dictionary folder: {dict_dir}")

    src_db = os.path.join(mono_dir, 'jamdict.db')
    dest_db = os.path.join(mono_dir, 'sankokudict.db')
    if not os.path.exists(src_db):
        print(f"Error: jamdict.db not found at {src_db}")
        sys.exit(1)
    if os.path.exists(dest_db):
        print("Removing existing sankokudict.db...")
        os.remove(dest_db)

    # Replicate schema from jamdict.db
    print("Replicating database schema and indexes...")
    src_conn = sqlite3.connect(src_db)
    src_cur = src_conn.cursor()
    src_cur.execute("SELECT name, sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
    tables = src_cur.fetchall()
    src_cur.execute("SELECT name, sql FROM sqlite_master WHERE type='index' AND sql IS NOT NULL")
    indexes = src_cur.fetchall()
    src_conn.close()

    dest_conn = sqlite3.connect(dest_db)
    dest_cur = dest_conn.cursor()
    dest_cur.execute("PRAGMA synchronous = OFF")
    dest_cur.execute("PRAGMA journal_mode = MEMORY")

    for name, sql in tables:
        dest_cur.execute(sql)
    for name, sql in indexes:
        dest_cur.execute(sql)
    dest_conn.commit()
    print("Schema replicated successfully.")

    # Meta entries
    dest_cur.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", ('jmdict.version', 'sankoku8'))
    dest_cur.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", ('jmdict.url', 'https://github.com/neocl/jamdict'))
    dest_cur.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", ('generator', 'sankoku-convert-v2'))

    # Process Yomichan term bank files
    term_files = sorted([f for f in os.listdir(dict_dir) if f.startswith('term_bank_') and f.endswith('.json')])

    entry_map = {}       # (expression, reading) -> idseq
    next_idseq = 5000000
    next_sid = 1
    etym_added = {}      # idseq -> bool (track if etymology has been stored)
    entry_def_tags = {}  # idseq -> set of def_tags accumulated

    # Batch insert buffers
    entry_b = []
    kanji_b = []
    kana_b = []
    sense_b = []
    pos_b = []
    gloss_b = []
    antonym_b = []
    xref_b = []
    etym_b = []
    misc_b = []
    audit_b = []

    print(f"Starting dictionary conversion of {len(term_files)} files...")

    for term_file in term_files:
        file_path = os.path.join(dict_dir, term_file)
        print(f"Processing {term_file}...")

        with open(file_path, 'r', encoding='utf-8') as f:
            entries = json.load(f)

        for entry in entries:
            expression = entry[0]
            reading = entry[1]
            def_tags_raw = entry[2] if len(entry) > 2 else ""
            rules_raw = entry[3] if len(entry) > 3 else ""
            raw_defs = entry[5] if len(entry) > 5 else []
            seq_num = entry[6] if len(entry) > 6 else 0
            term_tags_raw = entry[7] if len(entry) > 7 else ""

            key = (expression, reading)
            if key not in entry_map:
                idseq = next_idseq
                entry_map[key] = idseq
                next_idseq += 1
                entry_b.append((idseq,))

                is_kanji_word = has_kanji(expression) or (expression != reading)
                if is_kanji_word:
                    kanji_b.append((idseq, expression))
                    kana_b.append((idseq, reading, 0))
                else:
                    kana_b.append((idseq, reading, 1))

                entry_def_tags[idseq] = set()
                if def_tags_raw:
                    entry_def_tags[idseq].add(def_tags_raw)
                audit_b.append((idseq, None, f"seq:{seq_num}"))
                etym_added[idseq] = False
            else:
                idseq = entry_map[key]
                if def_tags_raw:
                    entry_def_tags[idseq].add(def_tags_raw)

            # ── Extract entry-level metadata from first structured-content block ──
            pos_text = ""
            entry_etym_text = ""
            first_block = next(
                (b for b in raw_defs if isinstance(b, dict) and b.get("type") == "structured-content"),
                None
            )
            if first_block:
                sc_content = first_block.get("content")
                if sc_content:
                    pos_text = extract_pos_from_node(sc_content)
                    for name, text, _ in collect_named_elements_filtered(sc_content, {"語源"}):
                        entry_etym_text = text
                        break

            # Process each top-level structured-content block
            for sc_block in raw_defs:
                if not isinstance(sc_block, dict):
                    continue

                block_type = sc_block.get("type")
                if block_type != "structured-content":
                    continue

                content = sc_block.get("content")
                if not content:
                    continue

                # ── Try to split by 語義 (individual senses) ──
                sense_nodes = find_sense_groups(content)
                if sense_nodes:
                    for sense_node in sense_nodes:
                        gloss_text = ""
                        antonyms = []
                        xrefs = []
                        usage_domains = []
                        notes = []

                        for name, text, _ in collect_named_elements(sense_node):
                            if name == "語釈":
                                gloss_text = text
                            elif name == "対義語":
                                antonyms.append(text)
                            elif name == "参照":
                                xrefs.append(text)
                            elif name == "使用域":
                                usage_domains.append(text)
                            elif name == "注記":
                                notes.append(text)

                        if not gloss_text.strip():
                            continue

                        sid = next_sid
                        next_sid += 1
                        sense_b.append((sid, idseq))
                        gloss_b.append((sid, 'jpn', None, gloss_text))

                        mapped_pos = map_pos_tags(pos_text)
                        for p in mapped_pos:
                            pos_b.append((sid, p))

                        for ant in antonyms:
                            antonym_b.append((sid, ant))
                        for xr in xrefs:
                            xref_b.append((sid, xr))
                        for ud in usage_domains:
                            misc_b.append((sid, f"domain:{ud}"))
                        for nt in notes:
                            misc_b.append((sid, f"note:{nt}"))
                else:
                    # ── Fallback: no 語義 found, use 解説部 text (skip header) ──
                    kaisetsu_text = extract_kaisetsubu_text(content)
                    if not kaisetsu_text.strip():
                        kaisetsu_text = extract_text(content)
                    if not kaisetsu_text.strip():
                        continue

                    sid = next_sid
                    next_sid += 1
                    sense_b.append((sid, idseq))
                    gloss_b.append((sid, 'jpn', None, kaisetsu_text))

                    mapped_pos = map_pos_tags(pos_text)
                    for p in mapped_pos:
                        pos_b.append((sid, p))

            # ── Store etymology once per entry ──
            if entry_etym_text and not etym_added[idseq]:
                etym_b.append((idseq, entry_etym_text))
                etym_added[idseq] = True

        # Batch flush
        if len(entry_b) >= 5000:
            flush_batch(dest_cur, entry_b, kanji_b, kana_b, sense_b, pos_b,
                        gloss_b, antonym_b, xref_b, etym_b, misc_b, audit_b,
                        entry_def_tags)
            dest_conn.commit()
            entry_b = []; kanji_b = []; kana_b = []; sense_b = []
            pos_b = []; gloss_b = []; antonym_b = []; xref_b = []
            etym_b = []; misc_b = []; audit_b = []

    # Final flush
    if entry_b:
        flush_batch(dest_cur, entry_b, kanji_b, kana_b, sense_b, pos_b,
                    gloss_b, antonym_b, xref_b, etym_b, misc_b, audit_b,
                    entry_def_tags)

    cur = dest_conn.cursor()
    cur.execute("SELECT COUNT(*) FROM Kana")
    kana_count = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM Kanji")
    kanji_count = cur.fetchone()[0]
    cur.close()
    dest_conn.commit()
    dest_conn.close()

    print("Database conversion completed successfully!")
    print(f"Generated output at: {dest_db}")
    print(f"  Kana entries: {kana_count}")
    print(f"  Kanji entries: {kanji_count}")
    print(f"  Total unique words: {kana_count + kanji_count}")


if __name__ == '__main__':
    convert()
