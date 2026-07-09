import os, sqlite3, sys

sys.stdout.reconfigure(encoding='utf-8')

conn = sqlite3.connect(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'jamdict.db'))
cur = conn.cursor()
cur.execute("SELECT * FROM xref LIMIT 10")
print("xref:")
for row in cur.fetchall():
    print(row)

cur.execute("SELECT * FROM misc LIMIT 10")
print("\nmisc:")
for row in cur.fetchall():
    print(row)

cur.execute("SELECT * FROM SenseInfo LIMIT 10")
print("\nSenseInfo:")
for row in cur.fetchall():
    print(row)

conn.close()
