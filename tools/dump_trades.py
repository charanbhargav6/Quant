import sqlite3
db = sqlite3.connect("d:/Desktop/engine/Database/crave.db")
db.row_factory = sqlite3.Row
rows = db.execute("SELECT * FROM trades").fetchall()
for r in rows:
    print(dict(r))
