import os
import json
import sqlite3
import re
import sys

# Ensure output is UTF-8 encoded
sys.stdout.reconfigure(encoding='utf-8')

def structured_content_to_text(node):
    if isinstance(node, str):
        return node
    elif isinstance(node, list):
        return "".join(structured_content_to_text(item) for item in node)
    elif isinstance(node, dict):
        if node.get("tag") == "img":
            title = node.get("title")
            if title:
                return f"({title})"
            return ""
        content = node.get("content", "")
        return structured_content_to_text(content)
    return ""

def extract_pos_from_text(text):
    # Search for ｟...｠ in the first 100 characters of the definition text
    m = re.search(r'｟([^｠]+)｠', text[:100])
    if m:
        return m.group(1)
    return ""

def map_pos_tags(pos_text):
    tags = []
    if not pos_text:
        return tags
        
    # Map Japanese abbreviations in Sanseido to standard categories
    # noun
    if '名' in pos_text or '代' in pos_text:
        tags.extend(['noun', '名詞'])
    # verb
    if any(k in pos_text for k in ['動', 'サ', '五', '上一', '下一', '自', '他']):
        tags.extend(['verb', '動詞'])
    # adjective
    if any(k in pos_text for k in ['形', 'ダナ', 'トタル']):
        tags.extend(['adjective', '形容詞'])
    # adverb
    if '副' in pos_text:
        tags.extend(['adverb', '副詞'])
    # pre-noun adjectival
    if '連体' in pos_text:
        tags.extend(['adj-pn', '連体詞'])
    # interjection
    if '感' in pos_text:
        tags.extend(['interjection', '感嘆詞'])
    # particle
    if '助' in pos_text:
        tags.extend(['particle', '助詞'])
        
    return list(set(tags))

def convert():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    
    # 1. Locate the dictionary folder containing index.json
    dict_dir = None
    for item in os.listdir(base_dir):
        item_path = os.path.join(base_dir, item)
        if os.path.isdir(item_path):
            if os.path.exists(os.path.join(item_path, 'index.json')):
                dict_dir = item_path
                break
                
    if not dict_dir:
        print("Error: Could not find dictionary folder containing index.json")
        sys.exit(1)
        
    print(f"Found dictionary folder: {dict_dir}")
    
    src_db = os.path.join(base_dir, 'jamdict.db')
    dest_db = os.path.join(base_dir, 'sankokudict.db')
    
    if not os.path.exists(src_db):
        print(f"Error: jamdict.db not found at {src_db}")
        sys.exit(1)
        
    # Remove existing dest_db if it exists to start fresh
    if os.path.exists(dest_db):
        print("Removing existing sankokudict.db...")
        os.remove(dest_db)
        
    # 2. Copy schema and indexes from jamdict.db
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
    
    # Enable PRAGMA settings for faster inserts
    dest_cur.execute("PRAGMA synchronous = OFF")
    dest_cur.execute("PRAGMA journal_mode = MEMORY")
    
    for name, sql in tables:
        dest_cur.execute(sql)
        
    for name, sql in indexes:
        dest_cur.execute(sql)
        
    dest_conn.commit()
    print("Schema replicated successfully.")
    
    # 3. Populate meta table
    dest_cur.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", ('jmdict.version', 'sankoku8'))
    dest_cur.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", ('jmdict.url', 'https://github.com/neocl/jamdict'))
    dest_cur.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", ('generator', 'sankoku-convert'))
    
    # 4. Process Yomichan JSON term bank files
    term_files = sorted([f for f in os.listdir(dict_dir) if f.startswith('term_bank_') and f.endswith('.json')])
    
    # To keep track of expression-reading group mappings to reuse idseq
    # (expression, reading) -> idseq
    entry_map = {}
    next_idseq = 5000000
    
    # For bulk inserts to speed up sqlite
    kanji_inserts = []
    kana_inserts = []
    entry_inserts = []
    sense_inserts = [] # (sid, idseq)
    pos_inserts = [] # (sid, pos_text)
    gloss_inserts = [] # (sid, lang, gend, text)
    
    # We will generate sequential Sense IDs starting from 1
    next_sid = 1
    
    # Helper to check if a character is Kanji
    def has_kanji(text):
        return any('\u4e00' <= ch <= '\u9faf' for ch in text)
        
    print(f"Starting dictionary conversion of {len(term_files)} files...")
    
    for term_file in term_files:
        file_path = os.path.join(dict_dir, term_file)
        print(f"Processing {term_file}...")
        
        with open(file_path, 'r', encoding='utf-8') as f:
            entries = json.load(f)
            
        for entry in entries:
            expression = entry[0]
            reading = entry[1]
            definitions = entry[5]
            
            key = (expression, reading)
            if key not in entry_map:
                idseq = next_idseq
                entry_map[key] = idseq
                next_idseq += 1
                
                # Insert Entry
                entry_inserts.append((idseq,))
                
                # Insert Kanji / Kana
                # If expression differs from reading or contains kanji
                is_kanji_word = has_kanji(expression) or (expression != reading)
                if is_kanji_word:
                    # K_Form/Kanji form
                    kanji_inserts.append((idseq, expression))
                    # R_Form/Reading form
                    kana_inserts.append((idseq, reading, 0)) # nokanji = 0
                else:
                    # Kana-only form
                    kana_inserts.append((idseq, reading, 1)) # nokanji = 1
            else:
                idseq = entry_map[key]
                
            # Process definitions as Senses
            # Clean each definition to plain text
            flat_defs = []
            for d in definitions:
                flat_defs.append(structured_content_to_text(d))
                
            # Combine or add as separate senses?
            # Typically in JMDict, each distinct definition/gloss category is a Sense.
            # Yomichan definitions can contain multiple sub-senses in one definition text.
            # Let's add them as separate senses if there are multiple.
            for def_text in flat_defs:
                if not def_text.strip():
                    continue
                    
                sid = next_sid
                next_sid += 1
                
                sense_inserts.append((sid, idseq))
                
                # Extract and map Part of Speech
                pos_text = extract_pos_from_text(def_text)
                mapped_pos = map_pos_tags(pos_text)
                for p in mapped_pos:
                    pos_inserts.append((sid, p))
                    
                # Insert definition into glosses
                gloss_inserts.append((sid, 'jpn', None, def_text))
                
        # Perform periodic batch insertion to save memory and commit
        if len(entry_inserts) >= 5000:
            dest_cur.executemany("INSERT OR IGNORE INTO Entry (idseq) VALUES (?)", entry_inserts)
            # For Kanji/Kana we need to generate IDs, but we can let SQLite auto-generate them
            # Kanji has ID (primary key auto-increment or just integer primary key)
            for idseq, text in kanji_inserts:
                dest_cur.execute("INSERT INTO Kanji (idseq, text) VALUES (?, ?)", (idseq, text))
            for idseq, text, nokanji in kana_inserts:
                dest_cur.execute("INSERT INTO Kana (idseq, text, nokanji) VALUES (?, ?, ?)", (idseq, text, nokanji))
            for sid, idseq in sense_inserts:
                dest_cur.execute("INSERT INTO Sense (ID, idseq) VALUES (?, ?)", (sid, idseq))
                
            dest_cur.executemany("INSERT INTO pos (sid, text) VALUES (?, ?)", pos_inserts)
            dest_cur.executemany("INSERT INTO SenseGloss (sid, lang, gend, text) VALUES (?, ?, ?, ?)", gloss_inserts)
            
            dest_conn.commit()
            
            entry_inserts = []
            kanji_inserts = []
            kana_inserts = []
            sense_inserts = []
            pos_inserts = []
            gloss_inserts = []
            
    # Final batch insert
    if entry_inserts:
        dest_cur.executemany("INSERT OR IGNORE INTO Entry (idseq) VALUES (?)", entry_inserts)
        for idseq, text in kanji_inserts:
            dest_cur.execute("INSERT INTO Kanji (idseq, text) VALUES (?, ?)", (idseq, text))
        for idseq, text, nokanji in kana_inserts:
            dest_cur.execute("INSERT INTO Kana (idseq, text, nokanji) VALUES (?, ?, ?)", (idseq, text, nokanji))
        for sid, idseq in sense_inserts:
            dest_cur.execute("INSERT INTO Sense (ID, idseq) VALUES (?, ?)", (sid, idseq))
            
        dest_cur.executemany("INSERT INTO pos (sid, text) VALUES (?, ?)", pos_inserts)
        dest_cur.executemany("INSERT INTO SenseGloss (sid, lang, gend, text) VALUES (?, ?, ?, ?)", gloss_inserts)
        
    dest_conn.commit()
    dest_conn.close()
    
    print("Database conversion completed successfully!")
    print(f"Generated output at: {dest_db}")

if __name__ == '__main__':
    convert()
