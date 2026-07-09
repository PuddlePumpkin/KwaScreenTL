"""
Comprehensive diagnostic for the Sanseido (三省堂国語辞典) dictionary database.
Runs all the individual inspection/scans in one place.
"""

import json
import os
import sqlite3
import sys
from collections import Counter, defaultdict

sys.stdout.reconfigure(encoding='utf-8')

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MONO_DIR = os.path.abspath(os.path.join(BASE_DIR, '..'))
DICT_DIR = None
dicts_dir = os.path.join(MONO_DIR, 'Dicts')
if os.path.isdir(dicts_dir):
    for item in os.listdir(dicts_dir):
        item_path = os.path.join(dicts_dir, item)
        if os.path.isdir(item_path) and os.path.exists(os.path.join(item_path, 'index.json')):
            DICT_DIR = item_path
            break

DB_PATH = os.path.join(MONO_DIR, 'sankokudict.db')


def section(title):
    print(f"\n{'=' * 72}")
    print(f"  {title}")
    print(f"{'=' * 72}")


def subsection(title):
    print(f"\n  --- {title} ---")


# ─── 1. Database summary ────────────────────────────────────────────────

def db_summary(conn):
    section("DATABASE SUMMARY")
    cur = conn.cursor()
    tables = [
        "Entry", "Kanji", "Kana", "Sense", "SenseGloss", "pos",
        "antonym", "xref", "Etym", "misc", "Audit", "meta",
        "dialect", "field", "SenseInfo", "Link", "Bib"
    ]
    for table in tables:
        try:
            cur.execute(f"SELECT COUNT(*) FROM {table}")
            c = cur.fetchone()[0]
            print(f"  {table:15s} : {c:>8,} rows")
        except Exception:
            print(f"  {table:15s} : (not populated)")

    cur.execute("SELECT key, value FROM meta")
    print(f"\n  Metadata:")
    for k, v in cur.fetchall():
        print(f"    {k}: {v}")


# ─── 2. Sample entries ──────────────────────────────────────────────────

def sample_entries(conn):
    section("SAMPLE ENTRIES")
    cur = conn.cursor()

    # Show 3 entries: one kana-only, one kanji, one with many senses
    queries = [
        ("Kana-only entry", """
            SELECT e.idseq, k.text as kanji, ka.text as kana
            FROM Entry e
            JOIN Kana ka ON e.idseq = ka.idseq
            LEFT JOIN Kanji k ON e.idseq = k.idseq
            WHERE k.text IS NULL
            LIMIT 1
        """),
        ("Kanji entry", """
            SELECT e.idseq, k.text as kanji, ka.text as kana
            FROM Entry e
            JOIN Kanji k ON e.idseq = k.idseq
            JOIN Kana ka ON e.idseq = ka.idseq
            LIMIT 1
        """),
        ("Entry with many senses", """
            SELECT e.idseq, k.text as kanji, ka.text as kana, COUNT(*) as sense_count
            FROM Entry e
            JOIN Kana ka ON e.idseq = ka.idseq
            LEFT JOIN Kanji k ON e.idseq = k.idseq
            JOIN Sense s ON e.idseq = s.idseq
            GROUP BY e.idseq
            ORDER BY sense_count DESC
            LIMIT 1
        """),
    ]

    for label, query in queries:
        cur.execute(query)
        row = cur.fetchone()
        if not row:
            continue
        idseq = row[0]
        kanji = row[1] or "(none)"
        kana = row[2]
        print(f"\n  [{label}] {kanji} ({kana})  idseq={idseq}")
        if len(row) > 3:
            print(f"    Senses: {row[3]}")

        cur.execute("""
            SELECT s.ID as sid, sg.text as gloss
            FROM Sense s JOIN SenseGloss sg ON s.ID = sg.sid
            WHERE s.idseq = ? ORDER BY s.ID
        """, (idseq,))
        for sid, gloss in cur.fetchall():
            print(f"    [{sid}] {gloss[:80]}")

        cur.execute("SELECT text FROM pos WHERE sid = (SELECT ID FROM Sense WHERE idseq = ? ORDER BY ID LIMIT 1)", (idseq,))
        pos_tags = [r[0] for r in cur.fetchall()]
        print(f"    POS: {pos_tags}")

        cur.execute("SELECT text FROM antonym WHERE sid IN (SELECT ID FROM Sense WHERE idseq = ?)", (idseq,))
        ants = [r[0] for r in cur.fetchall()]
        if ants:
            print(f"    Antonyms: {ants}")

        cur.execute("SELECT text FROM xref WHERE sid IN (SELECT ID FROM Sense WHERE idseq = ?)", (idseq,))
        xrefs = [r[0] for r in cur.fetchall()]
        if xrefs:
            print(f"    Xrefs: {xrefs}")

        cur.execute("SELECT text FROM misc WHERE sid IN (SELECT ID FROM Sense WHERE idseq = ?)", (idseq,))
        miscs = [r[0] for r in cur.fetchall()]
        if miscs:
            print(f"    Misc: {miscs[:6]}{'...' if len(miscs) > 6 else ''}")

        cur.execute("SELECT upd_detl FROM Audit WHERE idseq = ?", (idseq,))
        audit = cur.fetchone()
        if audit:
            print(f"    Audit: {audit[0]}")

        cur.execute("SELECT text FROM Etym WHERE idseq = ?", (idseq,))
        etym = cur.fetchone()
        if etym:
            print(f"    Etymology: {etym[0][:80]}")


# ─── 3. POS distribution ────────────────────────────────────────────────

def pos_distribution(conn):
    section("POS DISTRIBUTION")
    cur = conn.cursor()
    cur.execute("SELECT text, COUNT(*) as cnt FROM pos GROUP BY text ORDER BY cnt DESC")
    for text, cnt in cur.fetchall():
        print(f"  {text:20s} : {cnt:>8,}")


# ─── 4. Definition tags (entry types) ───────────────────────────────────

def def_tag_analysis(conn):
    section("DEFINITION TAGS (entry types from Audit)")
    cur = conn.cursor()
    cur.execute("""
        SELECT upd_detl FROM Audit WHERE instr(upd_detl, 'def_tag') > 0
    """)
    tag_counter = Counter()
    for (detl,) in cur.fetchall():
        parts = detl.split(",")
        for p in parts:
            if p.startswith("def_tag:"):
                tag_counter[p[8:]] += 1

    print(f"  Total entries with def_tags: {sum(tag_counter.values())}")
    for tag, count in tag_counter.most_common():
        print(f"    {tag!r}: {count}")


# ─── 5. Sequence number analysis ────────────────────────────────────────

def seq_analysis(conn):
    section("SEQUENCE NUMBER ANALYSIS")
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM Audit")
    total = cur.fetchone()[0]
    cur.execute("SELECT COUNT(DISTINCT substr(upd_detl, 5)) FROM Audit WHERE upd_detl LIKE 'seq:%'")
    unique = cur.fetchone()[0]
    print(f"  Total audit entries: {total}")
    print(f"  Unique sequence numbers: {unique}")

    cur.execute("""
        SELECT upd_detl, COUNT(*) as cnt FROM Audit
        GROUP BY upd_detl HAVING cnt > 1 ORDER BY cnt DESC LIMIT 10
    """)
    rows = cur.fetchall()
    if rows:
        print(f"  Shared sequence numbers (same seq, different entries):")
        for detl, cnt in rows:
            print(f"    {detl}: {cnt} entries")


# ─── 6. Antonym / Xref / Etymology / Misc breakdown ─────────────────────

def metadata_breakdown(conn):
    section("METADATA BREAKDOWN")
    cur = conn.cursor()

    cur.execute("SELECT text, COUNT(*) FROM antonym GROUP BY text ORDER BY COUNT(*) DESC LIMIT 20")
    print(f"\n  Top antonyms:")
    for text, cnt in cur.fetchall():
        print(f"    {text:20s} : {cnt}")

    cur.execute("SELECT text, COUNT(*) FROM xref GROUP BY text ORDER BY COUNT(*) DESC LIMIT 20")
    print(f"\n  Top cross-references:")
    for text, cnt in cur.fetchall():
        print(f"    {text:20s} : {cnt}")

    cur.execute("SELECT COUNT(*) FROM Etym")
    print(f"\n  Etymology entries: {cur.fetchone()[0]}")

    cur.execute("SELECT text, COUNT(*) FROM misc GROUP BY text ORDER BY COUNT(*) DESC LIMIT 20")
    print(f"\n  Top misc entries:")
    for text, cnt in cur.fetchall():
        print(f"    {text:30s} : {cnt}")

    # Show misc type breakdown (domain vs note vs ...)
    cur.execute("SELECT text FROM misc")
    type_counter = Counter()
    for (text,) in cur.fetchall():
        if ":" in text:
            type_counter[text.split(":")[0]] += 1
        else:
            type_counter["other"] += 1
    print(f"\n  Misc types:")
    for t, cnt in type_counter.most_common():
        print(f"    {t}: {cnt}")


# ─── 7. Source JSON profile (from term bank files) ──────────────────────

def source_profile():
    section("SOURCE JSON PROFILE")
    if not DICT_DIR:
        print("  (Dictionary folder not found)")
        return

    term_files = sorted([f for f in os.listdir(DICT_DIR) if f.startswith('term_bank_') and f.endswith('.json')])
    print(f"  Term bank files: {len(term_files)}")

    total_entries = 0
    total_defs = 0
    with_tags = 0
    tag_counter = Counter()

    for tf in term_files[:3]:  # sample first 3
        with open(os.path.join(DICT_DIR, tf), 'r', encoding='utf-8') as f:
            data = json.load(f)
        total_entries += len(data)
        for entry in data:
            total_defs += len(entry[5])
            if len(entry) > 2 and entry[2]:
                with_tags += 1
                tag_counter[entry[2]] += 1

    print(f"  Sampled entries (3 files): {total_entries}")
    print(f"  Sampled definitions (entry[5]): {total_defs}")
    print(f"  Avg defs per entry: {total_defs / total_entries:.2f}")

    # Count all entries across all files
    all_total = 0
    for tf in term_files:
        with open(os.path.join(DICT_DIR, tf), 'r', encoding='utf-8') as f:
            data = json.load(f)
        all_total += len(data)
    print(f"  Total entries across all files: {all_total}")

    # Tag bank
    tag_bank_path = os.path.join(DICT_DIR, 'tag_bank_1.json')
    if os.path.exists(tag_bank_path):
        with open(tag_bank_path, 'r', encoding='utf-8') as f:
            tags = json.load(f)
        print(f"  Tag bank entries: {len(tags)}")
        for t in tags:
            print(f"    tag: {t[0]}, type: {t[1]}, name: {t[3]}")


# ─── 8. Structured content element inventory ───────────────────────────

def structured_content_inventory():
    section("STRUCTURED CONTENT ELEMENT INVENTORY")
    if not DICT_DIR:
        return

    term_files = sorted([f for f in os.listdir(DICT_DIR) if f.startswith('term_bank_') and f.endswith('.json')])

    name_counter = Counter()
    img_counter = Counter()

    def scan_node(node):
        if isinstance(node, dict):
            data = node.get("data")
            if isinstance(data, dict):
                name = data.get("name")
                if name:
                    name_counter[name] += 1
            if node.get("tag") == "img":
                title = node.get("title")
                if title:
                    img_counter[title] += 1
            content = node.get("content")
            if content:
                scan_node(content)
        elif isinstance(node, list):
            for item in node:
                scan_node(item)

    for tf in term_files[:3]:
        with open(os.path.join(DICT_DIR, tf), 'r', encoding='utf-8') as f:
            data = json.load(f)
        for entry in data:
            scan_node(entry[5])

    print(f"  Top data.name elements (sampled 3 files):")
    for name, count in name_counter.most_common(30):
        print(f"    {name:20s} : {count}")
    print(f"\n  Top img titles:")
    for title, count in img_counter.most_common(20):
        print(f"    {title:30s} : {count}")


# ─── 9. Sense group analysis ────────────────────────────────────────────

def sense_group_analysis(conn):
    section("SENSE GROUP ANALYSIS")
    cur = conn.cursor()

    cur.execute("""
        SELECT COUNT(*) as sense_count, COUNT(*) / MAX(1, (SELECT COUNT(*) FROM Entry)) as avg
        FROM Sense
    """)
    total, avg = cur.fetchone()
    print(f"  Total senses: {total}")
    print(f"  Avg senses per entry: {total / max(1, 84745):.2f}")

    cur.execute("""
        SELECT COUNT(*), AVG(cnt) FROM (
            SELECT COUNT(*) as cnt FROM Sense GROUP BY idseq
        )
    """)
    entries_with_senses, avg_senses = cur.fetchone()
    print(f"  Entries with senses: {entries_with_senses}")
    print(f"  Avg senses per entry (non-zero): {avg_senses:.2f}")

    cur.execute("""
        SELECT cnt, COUNT(*) FROM (
            SELECT COUNT(*) as cnt FROM Sense GROUP BY idseq
        ) GROUP BY cnt ORDER BY cnt LIMIT 10
    """)
    print(f"  Sense count distribution (lower end):")
    for cnt, count in cur.fetchall():
        print(f"    {cnt} sense(s): {count} entries")


# ─── Main ───────────────────────────────────────────────────────────────

def main():
    print(f"Sanseido Dictionary Diagnostic")
    print(f"  DB: {DB_PATH}")
    print(f"  Dict: {DICT_DIR}")

    if not os.path.exists(DB_PATH):
        print("\nERROR: sankokudict.db not found. Run convert_dict.py first.")
        sys.exit(1)

    conn = sqlite3.connect(DB_PATH)

    db_summary(conn)
    sample_entries(conn)
    pos_distribution(conn)
    def_tag_analysis(conn)
    seq_analysis(conn)
    metadata_breakdown(conn)
    sense_group_analysis(conn)
    source_profile()
    structured_content_inventory()

    conn.close()
    print(f"\n{'=' * 72}")
    print("  Diagnostic complete.")
    print(f"{'=' * 72}")


if __name__ == '__main__':
    main()
