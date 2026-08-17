"""sqlite-vec가 이 플랫폼에서 동작하는지 확인. 실패해도 numpy 폴백으로 앱은 동작한다."""
import sqlite3, struct

try:
    import sqlite_vec
except ImportError:
    print("sqlite-vec 미설치 -> numpy 폴백 사용")
    raise SystemExit(0)

db = sqlite3.connect(":memory:")
db.enable_load_extension(True)
sqlite_vec.load(db)
db.enable_load_extension(False)
db.execute("CREATE VIRTUAL TABLE v USING vec0(id INTEGER PRIMARY KEY, embedding float[4])")
db.execute("INSERT INTO v(id, embedding) VALUES (1, ?)", (struct.pack("4f", 1, 0, 0, 0),))
row = db.execute(
    "SELECT id, distance FROM v WHERE embedding MATCH ? ORDER BY distance LIMIT 1",
    (struct.pack("4f", 1, 0, 0, 0),),
).fetchone()
assert row[0] == 1, row
print(f"sqlite-vec OK (version {db.execute('SELECT vec_version()').fetchone()[0]})")
