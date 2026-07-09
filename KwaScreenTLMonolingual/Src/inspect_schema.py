import os, sqlite3

conn = sqlite3.connect(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'jamdict.db'))
cur = conn.cursor()
cur.execute("SELECT name, sql FROM sqlite_master WHERE type='table'")
for name, sql in cur.fetchall():
    print(f"Table: {name}")
    print(sql)
    print("-" * 50)
conn.close()
