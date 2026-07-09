import os, sys, json, sqlite3, re

sys.stdout.reconfigure(encoding='utf-8')

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
MONO_DIR = os.path.abspath(os.path.join(PROJECT_DIR, '..'))
DB_PATH = os.path.join(MONO_DIR, "kankidict.db")

def extract_text(node):
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(extract_text(item) for item in node)
    if isinstance(node, dict):
        content = node.get("content")
        if content:
            return extract_text(content)
    return ""

def _walk_reading_divs(node):
    results = []
    if isinstance(node, dict) and node.get("tag") == "div":
        content = node.get("content", [])
        clist = content if isinstance(content, list) else [content]
        has_on = any(
            isinstance(x, dict) and x.get("tag") == "span"
            and extract_text(x.get("content", "")) == "音"
            for x in clist
        )
        has_kun = any(
            isinstance(x, dict) and x.get("tag") == "span"
            and extract_text(x.get("content", "")) == "訓"
            for x in clist
        )
        for x in clist:
            if isinstance(x, dict) and x.get("tag") == "span":
                st = x.get("style", {})
                if isinstance(st, dict) and st.get("fontWeight") == "bold":
                    r = extract_text(x.get("content", "")).strip()
                    if has_on and r:
                        results.append(("on", r))
                    elif has_kun and r:
                        results.append(("kun", r))
        for x in clist:
            results.extend(_walk_reading_divs(x))
    elif isinstance(node, list):
        for x in node:
            results.extend(_walk_reading_divs(x))
    return results

def find_reading_section(content_list):
    on_readings = []
    kun_readings = []
    for kind, text in _walk_reading_divs(content_list):
        if kind == "on" and text not in on_readings:
            on_readings.append(text)
        elif kind == "kun" and text not in kun_readings:
            kun_readings.append(text)
    return on_readings, kun_readings

def has_circled_numbers(text):
    return any(c in "①②③④⑤⑥⑦⑧⑨⑩" for c in text)

def find_gloss(content_list):
    if not isinstance(content_list, list):
        return ""

    parts = []

    # Pass 1: look for explicit "意味" section
    for item in content_list:
        if isinstance(item, dict) and item.get("tag") == "div":
            sub = item.get("content", [])
            sub_list = sub if isinstance(sub, list) else [sub]
            has_meaning = any(
                isinstance(x, dict) and x.get("tag") == "span"
                and extract_text(x.get("content", "")) == "意味"
                for x in sub_list
            )
            if has_meaning:
                for x in sub_list:
                    if isinstance(x, dict) and x.get("tag") == "div":
                        t = extract_text(x.get("content", "")).strip()
                        if t:
                            parts.append(t)
                return " ".join(parts)

    # Pass 2: look for divs with circled numbers (entries without 意味 label)
    def find_definition_divs(node, depth=0):
        found = []
        if isinstance(node, dict) and node.get("tag") == "div":
            text = extract_text(node.get("content", "")).strip()
            if text and has_circled_numbers(text):
                found.append(text)
                return found
            content = node.get("content", [])
            clist = content if isinstance(content, list) else [content]
            for x in clist:
                found.extend(find_definition_divs(x, depth+1))
        elif isinstance(node, list):
            for x in node:
                found.extend(find_definition_divs(x, depth+1))
        return found

    parts = find_definition_divs(content_list)
    if parts:
        gloss = " ".join(parts)
        # Strip leading reading prefix like "さち[幸]", "もの[者]"
        gloss = re.sub(r'^[^①②③④⑤⑥⑦⑧⑨⑩\[\]]+\[[^\[\]]+\]', '', gloss).strip()
        return gloss

    # Pass 3: fallback - all text
    fallback = extract_text(content_list).strip()
    if fallback:
        # Strip leading reading prefix
        fallback = re.sub(r'^[^①②③④⑤⑥⑦⑧⑨⑩\[\]]+\[[^\[\]]+\]', '', fallback).strip()
    return fallback

def main():
    kjdir = None
    dicts_dir = os.path.join(MONO_DIR, 'Dicts')
    if os.path.isdir(dicts_dir):
        for d in os.listdir(dicts_dir):
            dp = os.path.join(dicts_dir, d)
            if os.path.isdir(dp) and "06" in d:
                kjdir = dp
                break
    if not kjdir:
        print("Kanji dict directory not found in ../Dicts/")
        return

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("DROP TABLE IF EXISTS Kanki")
    cur.execute("""
        CREATE TABLE Kanki (
            character TEXT PRIMARY KEY,
            on_readings TEXT,
            kun_readings TEXT,
            gloss TEXT
        )
    """)

    # Collect all entries per character, then merge
    char_data = {}

    for fn in sorted(os.listdir(kjdir)):
        if not fn.startswith("term_bank_") or not fn.endswith(".json"):
            continue
        fp = os.path.join(kjdir, fn)
        with open(fp, "r", encoding="utf-8") as f:
            entries = json.load(f)
        for entry in entries:
            char = entry[0]
            content_list = entry[5][0]["content"] if entry[5] else []
            on_r, kun_r = find_reading_section(content_list)
            gloss = find_gloss(content_list)

            if char not in char_data:
                char_data[char] = {"on": set(), "kun": set(), "gloss": ""}

            # Merge readings
            char_data[char]["on"].update(on_r)
            char_data[char]["kun"].update(kun_r)

            # Merge gloss - prefer the one with circled numbers
            existing = char_data[char]["gloss"]
            if gloss:
                if has_circled_numbers(gloss):
                    if not has_circled_numbers(existing):
                        char_data[char]["gloss"] = gloss
                elif not existing:
                    char_data[char]["gloss"] = gloss

    total = 0
    for char, data in sorted(char_data.items()):
        cur.execute(
            "INSERT OR REPLACE INTO Kanki (character, on_readings, kun_readings, gloss) VALUES (?, ?, ?, ?)",
            (char, "・".join(sorted(data["on"])), "・".join(sorted(data["kun"])), data["gloss"])
        )
        total += 1

    conn.commit()
    conn.close()
    print(f"\nTotal kanji entries: {total}")

if __name__ == "__main__":
    main()
