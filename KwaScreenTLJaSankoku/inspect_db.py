import json
import os
import sys
import re

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

def inspect():
    sys.stdout.reconfigure(encoding='utf-8')
    dict_dir = 'C:/GitRepos/KwaScreenTL/KwaScreenTLJaSankoku/[JA-JA] 三省堂国語辞典\u3000第八版'
    
    pos_patterns = set()
    
    with open(os.path.join(dict_dir, 'term_bank_1.json'), 'r', encoding='utf-8') as f:
        data = json.load(f)
        
    for entry in data:
        raw_def = entry[5]
        text_def = "".join(structured_content_to_text(d) for d in raw_def)
        # Find ｟...｠ pattern
        m = re.search(r'｟([^｠]+)｠', text_def[:30])
        if m:
            pos_patterns.add(m.group(1))
            
    print("Found POS tags in double brackets:", sorted(list(pos_patterns)))

if __name__ == '__main__':
    inspect()
