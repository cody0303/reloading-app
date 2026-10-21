from flask import Flask, jsonify, request, send_from_directory, Response
import sqlite3, os, csv, io, json, datetime, random, string, secrets, tempfile, re
from reportlab.lib import colors
from reportlab.lib.pagesizes import inch
from reportlab.pdfgen import canvas as rl_canvas
from sm_decode import decode_tar, compute_stats, mm_to_angular

app = Flask(__name__, static_folder="static")
DB_PATH = os.environ.get("DB_PATH", "reloading.db")

# Trust proxy headers from nginx
from werkzeug.middleware.proxy_fix import ProxyFix
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

LOAD_COLUMNS = [
    "lot_number","date","cartridge","bullet_manufacturer","bullet_type",
    "bullet_weight","case_type","number_of_loads","rounds_made",
    "powder_type","powder_weight","cartridge_length","brass_length",
    "annealed","primer","base_to_ogive"
]

# Highpower courses of fire (stage = name, distance, shots; 10 points a shot, X counted separately).
# These only seed an empty courses table; edit them in Settings.
def _stage(name, dist, shots, limit=None):
    st = {"name": name, "distance": dist, "distance_unit": "yd", "shots": shots}
    if limit:
        st["time_limit"] = limit          # seconds allowed for the stage
    return st

_SFS, _RFSIT, _RFP, _SFP = "Slow Fire Standing", "Rapid Fire Sitting", "Rapid Fire Prone", "Slow Fire Prone"
DEFAULT_COURSES = [
    ("Highpower Rifle Standard Course", [_stage(_SFS, 200, 20), _stage(_RFSIT, 200, 20, 60), _stage(_RFP, 300, 20, 70), _stage(_SFP, 600, 20)]),
    ("Highpower Rifle Long Course",     [_stage(_SFS, 200, 20), _stage(_RFSIT, 200, 20, 60), _stage(_RFP, 300, 20, 70), _stage(_SFP, 600, 40)]),
    ("National Match Course",           [_stage(_SFS, 200, 10), _stage(_RFSIT, 200, 10, 60), _stage(_RFP, 300, 10, 70), _stage(_SFP, 600, 20)]),
    ("President's Rifle Course",        [_stage(_SFS, 200, 10), _stage(_RFP, 300, 10, 70), _stage(_SFP, 600, 10)]),
    ("Mid-Range 300/500/600 Match",     [_stage(_SFP, 300, 20), _stage(_SFP, 500, 20), _stage(_SFP, 600, 20)]),
    ("Mid-Range 3x600 Match",           [_stage(_SFP, 600, 20), _stage(_SFP, 600, 20), _stage(_SFP, 600, 20)]),
]

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS loads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lot_number TEXT UNIQUE,
            date TEXT, cartridge TEXT,
            bullet_manufacturer TEXT, bullet_type TEXT, bullet_weight REAL,
            case_type TEXT, number_of_loads INTEGER, rounds_made INTEGER,
            powder_type TEXT, powder_weight REAL,
            cartridge_length REAL, brass_length REAL,
            annealed TEXT, primer TEXT, base_to_ogive REAL
        );
        CREATE TABLE IF NOT EXISTS inventory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT NOT NULL,
            name TEXT NOT NULL,
            brand TEXT,
            quantity REAL NOT NULL DEFAULT 0,
            unit TEXT NOT NULL,
            caliber TEXT,
            weight_grains REAL,
            notes TEXT
        );
        CREATE TABLE IF NOT EXISTS firearms (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            caliber TEXT,
            manufacturer TEXT,
            barrel_length REAL,
            barrel_length_unit TEXT DEFAULT 'in',
            twist_rate TEXT,
            notes TEXT
        );
        CREATE TABLE IF NOT EXISTS performance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lot_number TEXT NOT NULL,
            firearm_id INTEGER,
            date TEXT,
            distance REAL,
            distance_unit TEXT DEFAULT 'yd',
            avg_velocity REAL,
            std_dev REAL,
            extreme_spread REAL,
            group_size REAL,
            group_x REAL,
            group_y REAL,
            group_unit TEXT DEFAULT 'MOA',
            num_shots INTEGER,
            notes TEXT,
            FOREIGN KEY(firearm_id) REFERENCES firearms(id)
        );
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        CREATE TABLE IF NOT EXISTS shotmarker_shots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            performance_id INTEGER NOT NULL,
            idx INTEGER,
            label TEXT,
            score TEXT,
            sighter INTEGER DEFAULT 0,
            x_mm REAL, y_mm REAL,
            x_moa REAL, y_moa REAL,
            v_fps REAL, temp_c REAL, ts INTEGER,
            face_id TEXT, distance REAL, dist_unit TEXT, bullet_d_mm REAL,
            FOREIGN KEY(performance_id) REFERENCES performance(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS chrono_shots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            performance_id INTEGER NOT NULL,
            idx INTEGER,
            speed_fps REAL,
            time TEXT,
            clean_bore INTEGER DEFAULT 0,
            cold_bore INTEGER DEFAULT 0,
            notes TEXT,
            ke REAL, power_factor REAL,
            sm_idx INTEGER,
            FOREIGN KEY(performance_id) REFERENCES performance(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS courses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            stages TEXT NOT NULL DEFAULT '[]'
        );
        CREATE TABLE IF NOT EXISTS matches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            date TEXT,
            location TEXT,
            entry_type TEXT,
            course_id INTEGER,
            course_name TEXT,
            firearm_id INTEGER,
            notes TEXT,
            FOREIGN KEY(firearm_id) REFERENCES firearms(id) ON DELETE SET NULL
        );
        CREATE TABLE IF NOT EXISTS match_stages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            match_id INTEGER NOT NULL,
            idx INTEGER,
            name TEXT NOT NULL,
            distance REAL,
            distance_unit TEXT DEFAULT 'yd',
            shots INTEGER NOT NULL,
            score INTEGER,
            x_count INTEGER,
            performance_id INTEGER,
            FOREIGN KEY(match_id) REFERENCES matches(id) ON DELETE CASCADE,
            FOREIGN KEY(performance_id) REFERENCES performance(id) ON DELETE SET NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS ux_match_stage_session
            ON match_stages(performance_id) WHERE performance_id IS NOT NULL;
        CREATE TABLE IF NOT EXISTS round_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            firearm_id INTEGER NOT NULL,
            date TEXT,
            rounds INTEGER NOT NULL,
            note TEXT,
            FOREIGN KEY(firearm_id) REFERENCES firearms(id) ON DELETE CASCADE
        );
    """)
    # Courses of fire: seeded once, then yours to edit in Settings
    if not conn.execute("SELECT 1 FROM courses LIMIT 1").fetchone():
        for cname, stages in DEFAULT_COURSES:
            conn.execute("INSERT INTO courses(name, stages) VALUES(?,?)", (cname, json.dumps(stages)))
    # Default settings
    defaults = {
        "label_width_mm": "50",
        "label_height_mm": "30",
        "label_units": "mm",
        "label_name": "",
        # Per-field label toggles
        "label_show_cartridge":           "1",
        "label_show_lot_number":          "1",
        "label_show_bullet_manufacturer": "1",
        "label_show_bullet_type":         "1",
        "label_show_bullet_weight":       "1",
        "label_show_powder_type":         "1",
        "label_show_powder_weight":       "1",
        "label_show_cartridge_length":    "1",
        "label_show_primer":              "1",
        "label_show_date":                "1",
        "label_show_case_type":           "1",
        "label_show_annealed":            "1",
        "label_show_number_of_loads":     "1",
        "label_show_rounds_made":         "0",
        "auto_decrement": "1",
    }
    for k, v in defaults.items():
        conn.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (k, v))

    # ── Migrations: safely add columns that may not exist yet ──
    migrations = [
        ("loads",    "rounds_made",    "INTEGER"),
        ("performance", "group_x",     "REAL"),
        ("performance", "group_y",     "REAL"),
        ("loads",    "lot_number",     "TEXT"),
        ("loads",    "base_to_ogive",  "REAL"),
        ("firearms", "manufacturer",   "TEXT"),
        ("firearms", "barrel_length_unit", "TEXT DEFAULT 'in'"),
        ("firearms", "start_rounds",       "INTEGER DEFAULT 0"),
        ("firearms", "life_rounds",        "INTEGER"),
        ("performance", "position",        "TEXT"),
        ("performance", "muzzle_avg_velocity",    "REAL"),
        ("performance", "muzzle_std_dev",         "REAL"),
        ("performance", "muzzle_extreme_spread",  "REAL"),
        ("performance", "score_text",             "TEXT"),
        ("performance", "score_total",            "INTEGER"),
        ("performance", "use_score",              "INTEGER DEFAULT 1"),
        ("performance", "orig_face",              "TEXT"),
        ("performance", "rescore_text",           "TEXT"),
        ("performance", "rescore_total",          "INTEGER"),
        ("performance", "rescore_face",           "TEXT"),
        ("shotmarker_shots", "rescore",           "TEXT"),
        ("match_stages", "time_limit",            "INTEGER"),
    ]
    for table, col, coldef in migrations:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coldef}")
        except Exception:
            pass  # column already exists

    # Sessions saved before the score column existed get theirs worked out from their saved shots
    for r in conn.execute(
        "SELECT DISTINCT s.performance_id FROM shotmarker_shots s "
        "JOIN performance p ON p.id = s.performance_id WHERE p.score_text IS NULL"
    ).fetchall():
        _store_score(conn, r["performance_id"])

    # Remember the face each session was scored on. One whose score was already switched off had its face changed
    # (or its distance) after import, so its original face is unknown ("?") and it is re-scored on the current one.
    for r in conn.execute(
        "SELECT DISTINCT p.id, p.use_score FROM performance p JOIN shotmarker_shots s ON s.performance_id = p.id "
        "WHERE p.orig_face IS NULL"
    ).fetchall():
        face = conn.execute("SELECT face_id FROM shotmarker_shots WHERE performance_id=? LIMIT 1", (r["id"],)).fetchone()["face_id"]
        if r["use_score"] == 0:
            conn.execute("UPDATE performance SET orig_face='?' WHERE id=?", (r["id"],))
            _apply_rescore(conn, r["id"])
        else:
            conn.execute("UPDATE performance SET orig_face=? WHERE id=?", (face, r["id"]))

    # One-time: the usual rapid-fire limits (sitting 60 s, prone 70 s) for courses and match stages that predate them.
    # Edit or clear them afterwards in Settings; this does not run again.
    if not conn.execute("SELECT 1 FROM settings WHERE key='rapid_times_seeded'").fetchone():
        limits = {"Rapid Fire Sitting": 60, "Rapid Fire Prone": 70}
        for r in conn.execute("SELECT id, stages FROM courses").fetchall():
            stages, changed = json.loads(r["stages"] or "[]"), False
            for st in stages:
                lim = limits.get(st.get("name"))
                if lim and not st.get("time_limit"):
                    st["time_limit"] = lim
                    changed = True
            if changed:
                conn.execute("UPDATE courses SET stages=? WHERE id=?", (json.dumps(stages), r["id"]))
        for name, lim in limits.items():
            conn.execute("UPDATE match_stages SET time_limit=? WHERE name=? AND time_limit IS NULL", (lim, name))
        conn.execute("INSERT INTO settings(key, value) VALUES('rapid_times_seeded', '1')")

    conn.commit()
    conn.close()

# ── Score ─────────────────────────────────────────────────────
def compute_score(shots, face_id=""):
    """Total and special-ring count of the record shots, in ShotMarker's own '191-5X' style.
    What an X or V counts is the face's own definition in the device software (the `score` of each face in
    static/faces.js: 10 on NRA/ISSF faces, 6 for the X on the ICFRA 'IFX' faces, 5 for the V on the 'IF'/'IS' faces),
    checked against all 58 faces. Faces not in that table fall back to the same values by face name.
    Returns (total, text) or (None, None)."""
    total = count = n = 0
    kind = "X"
    for s in shots:
        if s.get("sighter"): continue
        sc = str(s.get("score") or "").strip().upper()
        if sc in ("X", "V"):
            total += _special_value(face_id, sc); count += 1; n += 1; kind = sc
        elif sc.isdigit(): total += int(sc); n += 1
    if not n:
        return None, None
    # A face with no X or V ring (ISSF C200, Target Rifle D/F) shows just the total, as the device does
    fd = FACES.get(face_id)
    if fd and len(fd.get("score") or []) < 2:
        return total, str(total)
    return total, f"{total}-{count}{kind}"

def _special_value(face_id, marker):
    """Points for an X (or V) hit: the face's own value from faces.js, or the usual one if the face isn't listed."""
    sc = (FACES.get(face_id) or {}).get("score") or []
    if len(sc) > 1 and str(sc[1]).upper() == marker and isinstance(sc[0], (int, float)):
        return int(sc[0])
    if marker == "V":
        return 5
    return 6 if str(face_id or "").startswith("IFX") else 10

def _store_score(conn, pid):
    rows = [dict(r) for r in conn.execute(
        "SELECT score, sighter, face_id FROM shotmarker_shots WHERE performance_id=? ORDER BY idx", (pid,)).fetchall()]
    total, text = compute_score(rows, rows[0]["face_id"] if rows else "")
    conn.execute("UPDATE performance SET score_total=?, score_text=? WHERE id=?", (total, text, pid))

# ── Re-scoring on another target face ────────────────────────
# ShotMarker scores each shot on the face it was shot at. If you change the face afterwards, the shots are scored
# again here from their saved positions and kept apart (shotmarker_shots.rescore, performance.rescore_*) so the
# device's own scores are never overwritten. The rule is a copy of calc_shot_score in the ShotMarker software
# (version 122d): a shot takes the first ring, working outwards, for which centre distance < ring radius + bullet
# radius; outside every ring it scores the board's value (0 on most faces, 1 on the F-Class and TR faces); off the
# board it scores 0. On the real strings in tests/fixtures it reproduces the device's scores exactly.
try:
    _HERE = os.path.dirname(os.path.abspath(__file__))
except NameError:                      # exec'd without a file (the tests set __file__, this is only a fallback)
    _HERE = os.getcwd()
try:
    # static/faces.js is the one copy of the ring tables: the page loads it as a script, and it is strict JSON
    # after the "const SM_FACES=" prefix, so the same file serves both
    with open(os.path.join(_HERE, "static", "faces.js"), encoding="utf-8") as _f:
        _txt = _f.read()
    FACES = json.loads(_txt[_txt.index("{", _txt.index("SM_FACES")):_txt.rindex("}") + 1])
except (OSError, ValueError):
    FACES = {}

def ring_score(x_mm, y_mm, bullet_d_mm, face):
    """'X', 'V', or the ring value as text; '0' for a miss. None when the position isn't known.
    `face` is an entry of FACES (rings, board). Positions are measured from the centre of the target."""
    if x_mm is None or y_mm is None:
        return None
    half = (bullet_d_mm or 7.82) / 2.0                 # the device assumes 7.82 mm when a shot has no bullet size
    board = face.get("board") or {}
    w, h = board.get("w"), board.get("h")
    if w and h and (abs(x_mm) > w / 2.0 + half or abs(y_mm) > h / 2.0 + half):
        return "0"                                     # off the board altogether
    dist = (x_mm ** 2 + y_mm ** 2) ** 0.5
    polys = face.get("polys") or []
    # The device checks, in this order: shapes marked "top", the rings, the other shapes, then the board
    for p in polys:
        if p.get("top") and _hits_poly(x_mm, y_mm, half + (p.get("line") or 0) / 2.0, p["pts"]):
            return str(p["score"])
    for ring in sorted(face["rings"], key=lambda r: r["r"]):
        if dist < ring["r"] + half:
            return str(ring["score"])
    for p in polys:
        if not p.get("top") and _hits_poly(x_mm, y_mm, half + (p.get("line") or 0) / 2.0, p["pts"]):
            return str(p["score"])
    return str(board.get("score") or 0)                # on the board, outside every ring

def _hits_poly(x, y, radius, pts):
    """The bullet (a circle of `radius` at x, y) touches a shape: its centre is inside, or closer than `radius` to an
    edge. Same test as circleIntersectsPolygon in the device software (edges include the one closing the shape)."""
    inside, n = False, len(pts)
    j = n - 1
    for i in range(n):
        cx, cy = pts[i]
        fx, fy = pts[j]
        if (y < cy) != (y < fy) and x < (fx - cx) * (y - cy) / (fy - cy) + cx:
            inside = not inside
        j = i
    if inside:
        return True
    for i in range(n):
        ax, ay = pts[i - 1]                            # i = 0 pairs the last point with the first
        bx, by = pts[i]
        dx, dy = bx - ax, by - ay
        seg = dx * dx + dy * dy
        t = 0.0 if not seg else ((x - ax) * dx + (y - ay) * dy) / seg
        t = 0.0 if t < 0 else 1.0 if t > 1 else t
        if ((x - (ax + t * dx)) ** 2 + (y - (ay + t * dy)) ** 2) ** 0.5 < radius:
            return True
    return False

def _clear_rescore(conn, pid):
    conn.execute("UPDATE shotmarker_shots SET rescore=NULL WHERE performance_id=?", (pid,))
    conn.execute("UPDATE performance SET rescore_text=NULL, rescore_total=NULL, rescore_face=NULL WHERE id=?", (pid,))

def _apply_rescore(conn, pid):
    """Re-score a session's saved shots on the face they are on now, unless that is the face they were imported on."""
    shots = conn.execute(
        "SELECT id, x_mm, y_mm, bullet_d_mm, sighter, face_id FROM shotmarker_shots WHERE performance_id=? ORDER BY idx",
        (pid,)).fetchall()
    if not shots:
        return
    face = shots[0]["face_id"]
    orig = conn.execute("SELECT orig_face FROM performance WHERE id=?", (pid,)).fetchone()["orig_face"]
    face_def = FACES.get(face)
    # Unknown faces, and any flagged rescore:false (scored with polygons, which rings can't describe; no face in
    # faces.js is), can't be re-scored from rings
    if not face_def or face_def.get("rescore") is False or (orig and orig == face):
        _clear_rescore(conn, pid)
        return
    scored = []
    for sh in shots:
        sc = ring_score(sh["x_mm"], sh["y_mm"], sh["bullet_d_mm"], face_def)
        conn.execute("UPDATE shotmarker_shots SET rescore=? WHERE id=?", (sc, sh["id"]))
        scored.append({"score": sc, "sighter": sh["sighter"]})
    total, text = compute_score(scored, face)
    conn.execute("UPDATE performance SET rescore_text=?, rescore_total=?, rescore_face=? WHERE id=?", (text, total, face, pid))

def get_settings():
    conn = get_db()
    rows = conn.execute("SELECT key, value FROM settings").fetchall()
    conn.close()
    return {r["key"]: r["value"] for r in rows}

# ── Lot number generation ─────────────────────────────────────
def generate_lot_number(conn):
    today = datetime.date.today().strftime("%Y%m%d")
    rows = conn.execute(
        "SELECT lot_number FROM loads WHERE lot_number LIKE ? ORDER BY lot_number DESC",
        (f"{today}-%",)
    ).fetchall()
    if not rows:
        return f"{today}-001"
    last = rows[0]["lot_number"]
    try:
        seq = int(last.split("-")[1]) + 1
    except (IndexError, ValueError):
        seq = len(rows) + 1
    return f"{today}-{seq:03d}"

# ── Frontend ──────────────────────────────────────────────────
@app.route("/")
@app.route("/<path:page>")
def index(page=None):
    return send_from_directory("static", "index.html")

# ── Loads ─────────────────────────────────────────────────────
@app.route("/api/loads")
def list_loads():
    search   = request.args.get("search","").strip()
    cart     = request.args.get("cartridge","").strip()
    powder   = request.args.get("powder_type","").strip()
    bwt      = request.args.get("bullet_weight","").strip()
    annealed = request.args.get("annealed","").strip()
    sort_col = request.args.get("sort","id")
    sort_dir = request.args.get("dir","desc").upper()
    if sort_col not in ["id"]+LOAD_COLUMNS: sort_col="id"
    if sort_dir not in ("ASC","DESC"): sort_dir="DESC"
    where, params = [], []
    if search:
        clauses = " OR ".join(f"COALESCE({c},'') LIKE ?" for c in LOAD_COLUMNS)
        where.append(f"({clauses})"); params.extend([f"%{search}%"]*len(LOAD_COLUMNS))
    if cart:    where.append("cartridge LIKE ?");                    params.append(f"%{cart}%")
    if powder:  where.append("powder_type LIKE ?");                  params.append(f"%{powder}%")
    if bwt:     where.append("CAST(bullet_weight AS TEXT) LIKE ?");  params.append(f"%{bwt}%")
    if annealed:where.append("annealed = ?");                        params.append(annealed)
    sql = "SELECT * FROM loads"
    if where: sql += " WHERE " + " AND ".join(where)
    sql += f" ORDER BY {sort_col} {sort_dir}"
    conn = get_db()
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/loads/new-lot")
def new_lot():
    conn = get_db()
    lot = generate_lot_number(conn)
    conn.close()
    return jsonify({"lot_number": lot})

@app.route("/api/loads", methods=["POST"])
def create_load():
    data = request.json or {}
    conn = get_db()
    if not data.get("lot_number"):
        data["lot_number"] = generate_lot_number(conn)
    vals = {c: data.get(c) or None for c in LOAD_COLUMNS}
    cols = list(vals.keys())
    cur = conn.execute(
        f"INSERT INTO loads ({','.join(cols)}) VALUES ({','.join(['?']*len(cols))})",
        list(vals.values())
    )
    conn.commit()

    # Auto-decrement inventory
    s = get_settings()
    if s.get("auto_decrement","1") == "1":
        rounds = int(data.get("rounds_made") or 0)
        if rounds > 0:
            _decrement_inventory(conn, data, rounds)
            conn.commit()

    row = conn.execute("SELECT * FROM loads WHERE id=?", (cur.lastrowid,)).fetchone()
    conn.close()
    return jsonify(dict(row)), 201

@app.route("/api/loads/<int:lid>", methods=["PUT"])
def update_load(lid):
    data = request.json or {}
    vals = {c: data.get(c) or None for c in LOAD_COLUMNS}
    sets = ", ".join(f"{c}=?" for c in vals)
    conn = get_db()
    conn.execute(f"UPDATE loads SET {sets} WHERE id=?", [*vals.values(), lid])
    conn.commit()
    row = conn.execute("SELECT * FROM loads WHERE id=?", (lid,)).fetchone()
    conn.close()
    return jsonify(dict(row))

@app.route("/api/loads/<int:lid>", methods=["DELETE"])
def delete_load(lid):
    conn = get_db()
    conn.execute("DELETE FROM loads WHERE id=?", (lid,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})

GRAINS_PER_UNIT = {"lb": 7000.0, "oz": 437.5, "g": 15.4324}

def _to_float(v):
    try: return float(v)
    except (TypeError, ValueError): return 0.0

def _load_components(conn, data, rounds):
    """Resolve each component of a load to its inventory row and the amount
    this load consumes, in that row's own unit (row is None if not in inventory)."""
    def s(key): return str(data.get(key) or "").strip()

    def find(category, names):
        for n in names:
            if not n: continue
            row = conn.execute(
                "SELECT * FROM inventory WHERE category=? AND LOWER(name)=LOWER(?)",
                (category, n)).fetchone()
            if row: return row
        return None

    comps = []
    if s("bullet_type"):
        names = [f"{s('bullet_manufacturer')} {s('bullet_type')}".strip(), s("bullet_type")]
        comps.append(("Bullets", s("bullet_type"), s("bullet_manufacturer"), names, float(rounds)))
    if s("primer"):
        comps.append(("Primers", s("primer"), "", [s("primer")], float(rounds)))
    if s("case_type"):
        comps.append(("Brass/Cases", s("case_type"), "", [s("case_type")], float(rounds)))
    if s("powder_type"):
        comps.append(("Powder", s("powder_type"), "", [s("powder_type")], None))

    out = []
    for category, name, brand, names, needed in comps:
        row = find(category, names)
        if category == "Powder":
            grains = _to_float(data.get("powder_weight")) * rounds
            # powder_weight is grains per round; convert to the item's unit (lb if unknown)
            needed = grains / GRAINS_PER_UNIT.get(row["unit"] if row else "lb", 7000.0) if grains else None
        out.append({"category": category, "name": name, "brand": brand, "row": row, "needed": needed})
    return out

def _decrement_inventory(conn, data, rounds):
    """Decrement inventory based on a load record."""
    for c in _load_components(conn, data, rounds):
        if c["row"] and c["needed"] and c["needed"] > 0:
            conn.execute(
                "UPDATE inventory SET quantity = MAX(0, quantity - ?) WHERE id=?",
                (c["needed"], c["row"]["id"]))

# ── Inventory ─────────────────────────────────────────────────
@app.route("/api/inventory")
def list_inventory():
    cat = request.args.get("category","")
    conn = get_db()
    if cat:
        rows = conn.execute("SELECT * FROM inventory WHERE category=? ORDER BY category,name", (cat,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM inventory ORDER BY category,name").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/inventory", methods=["POST"])
def create_inventory():
    data = request.json or {}
    cols = ["category","name","brand","quantity","unit","caliber","weight_grains","notes"]
    vals = {c: data.get(c) or None for c in cols}
    if not vals["category"] or not vals["name"]:
        return jsonify({"error":"category and name required"}), 400
    if vals["quantity"] is None: vals["quantity"] = 0
    conn = get_db()
    cur = conn.execute(
        f"INSERT INTO inventory ({','.join(cols)}) VALUES ({','.join(['?']*len(cols))})",
        list(vals.values())
    )
    conn.commit()
    row = conn.execute("SELECT * FROM inventory WHERE id=?", (cur.lastrowid,)).fetchone()
    conn.close()
    return jsonify(dict(row)), 201

@app.route("/api/inventory/<int:iid>", methods=["PUT"])
def update_inventory(iid):
    data = request.json or {}
    cols = ["category","name","brand","quantity","unit","caliber","weight_grains","notes"]
    vals = {c: data.get(c) for c in cols}
    sets = ", ".join(f"{c}=?" for c in vals)
    conn = get_db()
    conn.execute(f"UPDATE inventory SET {sets} WHERE id=?", [*vals.values(), iid])
    conn.commit()
    row = conn.execute("SELECT * FROM inventory WHERE id=?", (iid,)).fetchone()
    conn.close()
    return jsonify(dict(row))

@app.route("/api/inventory/<int:iid>", methods=["DELETE"])
def delete_inventory(iid):
    conn = get_db()
    conn.execute("DELETE FROM inventory WHERE id=?", (iid,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})

@app.route("/api/inventory/<int:iid>/add", methods=["POST"])
def add_inventory_stock(iid):
    """Add to an item's quantity, leaving every other field as it is."""
    data = request.json or {}
    amount = _to_float(data.get("amount"))
    if amount <= 0:
        return jsonify({"error": "amount must be greater than 0"}), 400
    conn = get_db()
    cur = conn.execute("UPDATE inventory SET quantity = ROUND(quantity + ?, 4) WHERE id=?", (amount, iid))
    if cur.rowcount == 0:
        conn.close()
        return jsonify({"error": "Not found"}), 404
    conn.commit()
    row = conn.execute("SELECT * FROM inventory WHERE id=?", (iid,)).fetchone()
    conn.close()
    return jsonify(dict(row))

@app.route("/api/inventory/check", methods=["POST"])
def check_inventory():
    """Report load components that are missing from inventory, or that this
    load would use more of than is in stock."""
    data = request.json or {}
    rounds = int(_to_float(data.get("rounds_made")))
    check_stock = rounds > 0 and get_settings().get("auto_decrement", "1") == "1"
    conn = get_db()
    issues = []
    for c in _load_components(conn, data, rounds):
        row = c["row"]
        if not row:
            issues.append({"status": "missing", "category": c["category"],
                           "name": c["name"], "brand": c["brand"]})
        elif check_stock and c["needed"] and (row["quantity"] or 0) < c["needed"]:
            available = row["quantity"] or 0
            issues.append({"status": "short", "category": c["category"],
                           "name": row["name"], "brand": row["brand"] or "",
                           "inventory_id": row["id"], "unit": row["unit"],
                           "available": round(available, 4),
                           "needed": round(c["needed"], 4),
                           "shortfall": round(c["needed"] - available, 4)})
    conn.close()
    return jsonify(issues)

# ── Firearms ──────────────────────────────────────────────────
@app.route("/api/firearms")
def list_firearms():
    conn = get_db()
    rows = conn.execute("SELECT * FROM firearms ORDER BY name").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["rounds_total"] = _firearm_rounds(conn, d["id"], d)["total"]
        out.append(d)
    conn.close()
    return jsonify(out)

FIREARM_COLS = ["name","caliber","manufacturer","barrel_length","barrel_length_unit","twist_rate","notes",
                "start_rounds","life_rounds"]

def _clean_round_counts(vals):
    """start_rounds / life_rounds are optional whole numbers. Returns an error message or None."""
    for key, label, minimum in (("start_rounds", "rounds before tracking", 0), ("life_rounds", "expected barrel life", 1)):
        v = vals.get(key)
        if v is None:
            continue
        n = _to_int(v)
        if n is None or n < minimum:
            return f"{label} must be a whole number of at least {minimum}"
        vals[key] = n
    return None

TWIST_RE = re.compile(r"^1:\d+(\.\d+)?$")

def _bad_twist(vals):
    """Twist rate is stored as '1:x'; the unit is the firearm's barrel length unit."""
    t = vals["twist_rate"]
    return t is not None and not TWIST_RE.match(str(t))

@app.route("/api/firearms", methods=["POST"])
def create_firearm():
    data = request.json or {}
    cols = FIREARM_COLS
    vals = {c: data.get(c) or None for c in cols}
    if _bad_twist(vals):
        return jsonify({"error": "twist_rate must look like 1:10"}), 400
    err = _clean_round_counts(vals)
    if err:
        return jsonify({"error": err}), 400
    conn = get_db()
    cur = conn.execute(
        f"INSERT INTO firearms ({','.join(cols)}) VALUES ({','.join(['?']*len(cols))})",
        list(vals.values())
    )
    conn.commit()
    row = conn.execute("SELECT * FROM firearms WHERE id=?", (cur.lastrowid,)).fetchone()
    conn.close()
    return jsonify(dict(row)), 201

@app.route("/api/firearms/<int:fid>", methods=["PUT"])
def update_firearm(fid):
    data = request.json or {}
    cols = FIREARM_COLS
    vals = {c: data.get(c) or None for c in cols}
    if _bad_twist(vals):
        return jsonify({"error": "twist_rate must look like 1:10"}), 400
    err = _clean_round_counts(vals)
    if err:
        return jsonify({"error": err}), 400
    sets = ", ".join(f"{c}=?" for c in vals)
    conn = get_db()
    conn.execute(f"UPDATE firearms SET {sets} WHERE id=?", [*vals.values(), fid])
    conn.commit()
    row = conn.execute("SELECT * FROM firearms WHERE id=?", (fid,)).fetchone()
    conn.close()
    return jsonify(dict(row))

@app.route("/api/firearms/<int:fid>", methods=["DELETE"])
def delete_firearm(fid):
    conn = get_db()
    conn.execute("DELETE FROM firearms WHERE id=?", (fid,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})

# ── Rounds fired ──────────────────────────────────────────────
# Each firearm entry is a barrel (fitting a new barrel means adding a new entry), so its round count is just:
# the rounds it had before the app (start_rounds) + its performance sessions (the most of the typed count, the
# saved ShotMarker shots including sighters, and the Garmin shots) + match stages with a score but no linked
# session (a linked one is already in its session) + the round log, for anything fired outside the app.
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

def _firearm_rounds(conn, fid, row=None):
    row = row or conn.execute("SELECT * FROM firearms WHERE id=?", (fid,)).fetchone()
    sessions = 0
    for r in conn.execute("""SELECT p.num_shots,
            (SELECT COUNT(*) FROM shotmarker_shots s WHERE s.performance_id = p.id) AS sm,
            (SELECT COUNT(*) FROM chrono_shots c WHERE c.performance_id = p.id) AS ch
        FROM performance p WHERE p.firearm_id=?""", (fid,)).fetchall():
        sessions += max(_to_int(r["num_shots"]) or 0, r["sm"], r["ch"])
    matches = conn.execute("""SELECT COALESCE(SUM(s.shots), 0) FROM match_stages s JOIN matches m ON m.id = s.match_id
        WHERE m.firearm_id=? AND s.performance_id IS NULL AND s.score IS NOT NULL""", (fid,)).fetchone()[0]
    logged = conn.execute("SELECT COALESCE(SUM(rounds), 0) FROM round_log WHERE firearm_id=?", (fid,)).fetchone()[0]
    start = (row["start_rounds"] if row else 0) or 0
    return {"start": start, "sessions": sessions, "matches": matches, "logged": logged,
            "total": start + sessions + matches + logged}

def _rounds_payload(conn, fid):
    row = conn.execute("SELECT * FROM firearms WHERE id=?", (fid,)).fetchone()
    info = _firearm_rounds(conn, fid, row)
    info["firearm_id"] = fid
    info["life_rounds"] = row["life_rounds"]
    info["log"] = [dict(r) for r in conn.execute(
        "SELECT * FROM round_log WHERE firearm_id=? ORDER BY date DESC, id DESC", (fid,)).fetchall()]
    return info

@app.route("/api/firearms/<int:fid>/rounds")
def firearm_rounds(fid):
    conn = get_db()
    if not conn.execute("SELECT 1 FROM firearms WHERE id=?", (fid,)).fetchone():
        conn.close()
        return jsonify({"error": "firearm not found"}), 404
    out = _rounds_payload(conn, fid)
    conn.close()
    return jsonify(out)

@app.route("/api/firearms/<int:fid>/log", methods=["POST"])
def add_round_log(fid):
    data = request.json or {}
    rounds = _to_int(data.get("rounds"))
    if not rounds or rounds < 1:
        return jsonify({"error": "rounds must be at least 1"}), 400
    date = data.get("date") or datetime.date.today().isoformat()
    if not DATE_RE.match(str(date)):
        return jsonify({"error": "date must be YYYY-MM-DD"}), 400
    conn = get_db()
    if not conn.execute("SELECT 1 FROM firearms WHERE id=?", (fid,)).fetchone():
        conn.close()
        return jsonify({"error": "firearm not found"}), 404
    conn.execute("INSERT INTO round_log (firearm_id, date, rounds, note) VALUES (?,?,?,?)",
                 (fid, date, rounds, data.get("note") or None))
    conn.commit()
    out = _rounds_payload(conn, fid)
    conn.close()
    return jsonify(out), 201

@app.route("/api/round-log/<int:lid>", methods=["DELETE"])
def delete_round_log(lid):
    conn = get_db()
    row = conn.execute("SELECT firearm_id FROM round_log WHERE id=?", (lid,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "entry not found"}), 404
    conn.execute("DELETE FROM round_log WHERE id=?", (lid,))
    conn.commit()
    out = _rounds_payload(conn, row["firearm_id"])
    conn.close()
    return jsonify(out)

# ── Performance ───────────────────────────────────────────────
# A timed stage can be shot as several strings (rapid fire: two strings with a magazine change between). A gap longer
# than this starts a new string. The page reads it from each timed session's row (string_gap_s), so it lives only here.
STRING_GAP_S = 20

def _string_times(conn, pid):
    """Seconds from first to last shot of each string in a session, or None when it has no timed shots."""
    ts = sorted(r["ts"] for r in conn.execute(
        "SELECT ts FROM shotmarker_shots WHERE performance_id=? AND sighter=0 AND ts IS NOT NULL", (pid,)))
    if len(ts) < 2:
        return None
    out, start = [], 0
    for i in range(1, len(ts) + 1):
        if i == len(ts) or (ts[i] - ts[i - 1]) / 1000.0 > STRING_GAP_S:
            out.append(round((ts[i - 1] - ts[start]) / 1000.0, 3))
            start = i
    return out

def _perf_dicts(conn, rows):
    """Performance rows as dicts; a session on a timed stage also gets its per-string times."""
    out = []
    for r in rows:
        d = dict(r)
        d["string_times"] = _string_times(conn, d["id"]) if d.get("stage_time_limit") else None
        d["string_gap_s"] = STRING_GAP_S if d.get("stage_time_limit") else None
        out.append(d)
    return out

@app.route("/api/performance")
def list_performance():
    lot = request.args.get("lot_number","")
    conn = get_db()
    if lot:
        rows = conn.execute("""
            SELECT p.*, f.name as firearm_name,
              (SELECT face_id FROM shotmarker_shots WHERE performance_id=p.id LIMIT 1) AS face_id,
              (SELECT (MAX(ts) - MIN(ts)) / 1000.0 FROM shotmarker_shots WHERE performance_id=p.id AND sighter=0) AS string_seconds,
              (SELECT time_limit FROM match_stages WHERE performance_id=p.id) AS stage_time_limit,
              (SELECT shots FROM match_stages WHERE performance_id=p.id) AS stage_shots FROM performance p
            LEFT JOIN firearms f ON p.firearm_id=f.id
            WHERE p.lot_number=? ORDER BY p.id DESC
        """, (lot,)).fetchall()
    else:
        rows = conn.execute("""
            SELECT p.*, f.name as firearm_name,
              (SELECT face_id FROM shotmarker_shots WHERE performance_id=p.id LIMIT 1) AS face_id,
              (SELECT (MAX(ts) - MIN(ts)) / 1000.0 FROM shotmarker_shots WHERE performance_id=p.id AND sighter=0) AS string_seconds,
              (SELECT time_limit FROM match_stages WHERE performance_id=p.id) AS stage_time_limit,
              (SELECT shots FROM match_stages WHERE performance_id=p.id) AS stage_shots FROM performance p
            LEFT JOIN firearms f ON p.firearm_id=f.id
            ORDER BY p.id DESC
        """).fetchall()
    out = _perf_dicts(conn, rows)
    conn.close()
    return jsonify(out)

@app.route("/api/performance", methods=["POST"])
def create_performance():
    data = request.json or {}
    cols = ["lot_number","firearm_id","date","distance","distance_unit",
            "avg_velocity","std_dev","extreme_spread","group_size","group_x","group_y","group_unit","num_shots","position","notes",
            "muzzle_avg_velocity","muzzle_std_dev","muzzle_extreme_spread"]
    vals = {c: data.get(c) or None for c in cols}
    conn = get_db()
    cur = conn.execute(
        f"INSERT INTO performance ({','.join(cols)}) VALUES ({','.join(['?']*len(cols))})",
        list(vals.values())
    )
    conn.commit()
    row = conn.execute("""
        SELECT p.*, f.name as firearm_name,
              (SELECT face_id FROM shotmarker_shots WHERE performance_id=p.id LIMIT 1) AS face_id,
              (SELECT (MAX(ts) - MIN(ts)) / 1000.0 FROM shotmarker_shots WHERE performance_id=p.id AND sighter=0) AS string_seconds,
              (SELECT time_limit FROM match_stages WHERE performance_id=p.id) AS stage_time_limit,
              (SELECT shots FROM match_stages WHERE performance_id=p.id) AS stage_shots FROM performance p
        LEFT JOIN firearms f ON p.firearm_id=f.id WHERE p.id=?
    """, (cur.lastrowid,)).fetchone()
    conn.close()
    return jsonify(dict(row)), 201

@app.route("/api/performance/<int:pid>", methods=["PUT"])
def update_performance(pid):
    data = request.json or {}
    cols = ["lot_number","firearm_id","date","distance","distance_unit",
            "avg_velocity","std_dev","extreme_spread","group_size","group_x","group_y","group_unit","num_shots","position","notes",
            "muzzle_avg_velocity","muzzle_std_dev","muzzle_extreme_spread"]
    vals = {c: data.get(c) or None for c in cols}
    sets = ", ".join(f"{c}=?" for c in vals)
    conn = get_db()
    old = conn.execute(
        "SELECT distance, distance_unit, group_unit, group_size, group_x, group_y FROM performance WHERE id=?",
        (pid,)).fetchone()
    conn.execute(f"UPDATE performance SET {sets} WHERE id=?", [*vals.values(), pid])
    # Group sizes are stored in MOA/mil, so changing the distance or unit leaves them stale.
    # Re-derive them from the saved shots, unless the group values were edited by hand here.
    if old:
        distance_changed = (not _same_num(old["distance"], vals["distance"])
                            or (old["distance_unit"] or "yd") != (vals["distance_unit"] or "yd"))
        changed = distance_changed or (old["group_unit"] or "MOA") != (vals["group_unit"] or "MOA")
        group_edited = any(not _same_num(old[k], vals[k]) for k in ("group_size", "group_x", "group_y"))
        if distance_changed:
            _sync_shot_geometry(conn, pid)   # the saved shots carry their own copy of the distance
        if changed and not group_edited:
            _refresh_group_angles(conn, pid)
    # Target face lives on the saved shots. Positions (mm) are measured, so they are kept as they are.
    face = str(data.get("face_id") or "").strip()
    if face:
        conn.execute("UPDATE shotmarker_shots SET face_id=? WHERE performance_id=?", (face, pid))
        _apply_rescore(conn, pid)
    if "use_score" in data:
        conn.execute("UPDATE performance SET use_score=? WHERE id=?",
                     (1 if data["use_score"] in (True, 1, "1", "true") else 0, pid))
    conn.commit()
    row = conn.execute("""
        SELECT p.*, f.name as firearm_name,
              (SELECT face_id FROM shotmarker_shots WHERE performance_id=p.id LIMIT 1) AS face_id,
              (SELECT (MAX(ts) - MIN(ts)) / 1000.0 FROM shotmarker_shots WHERE performance_id=p.id AND sighter=0) AS string_seconds,
              (SELECT time_limit FROM match_stages WHERE performance_id=p.id) AS stage_time_limit,
              (SELECT shots FROM match_stages WHERE performance_id=p.id) AS stage_shots FROM performance p
        LEFT JOIN firearms f ON p.firearm_id=f.id WHERE p.id=?
    """, (pid,)).fetchone()
    out = _perf_dicts(conn, [row])[0]
    conn.close()
    return jsonify(out)

def _same_num(a, b):
    try: return float(a or 0) == float(b or 0)
    except (TypeError, ValueError): return a == b

def _sync_shot_geometry(conn, pid):
    """Copy a session's current distance onto its saved shots and redo their per-shot MOA."""
    perf = conn.execute("SELECT distance, distance_unit FROM performance WHERE id=?", (pid,)).fetchone()
    d = _to_float(perf["distance"]) if perf else 0.0
    if not d:
        return
    unit = "m" if perf["distance_unit"] == "m" else "y"
    per_moa = 0.29088822 * d * (0.9144 if unit == "y" else 1)   # mm per MOA at this distance
    for r in conn.execute("SELECT id, x_mm, y_mm FROM shotmarker_shots WHERE performance_id=?", (pid,)).fetchall():
        conn.execute(
            "UPDATE shotmarker_shots SET distance=?, dist_unit=?, x_moa=?, y_moa=? WHERE id=?",
            (d, unit,
             round(r["x_mm"] / per_moa, 3) if r["x_mm"] is not None else None,
             round(r["y_mm"] / per_moa, 3) if r["y_mm"] is not None else None, r["id"]))

def _refresh_group_angles(conn, pid):
    shots = [dict(r) for r in conn.execute(
        "SELECT * FROM shotmarker_shots WHERE performance_id=? ORDER BY idx", (pid,)).fetchall()]
    stats = compute_stats(shots) if shots else None
    if not stats or not stats["num_shots"]:
        return
    perf = conn.execute("SELECT distance, distance_unit, group_unit FROM performance WHERE id=?", (pid,)).fetchone()
    dist_yd = float(perf["distance"]) if perf["distance"] else None
    if dist_yd and perf["distance_unit"] == "m":
        dist_yd *= 1.09361
    unit = perf["group_unit"] or "MOA"
    conn.execute(
        "UPDATE performance SET group_size=?, group_x=?, group_y=? WHERE id=?",
        (mm_to_angular(stats["group_size_mm"], dist_yd, unit),
         mm_to_angular(stats["group_x_mm"], dist_yd, unit),
         mm_to_angular(stats["group_y_mm"], dist_yd, unit), pid))

@app.route("/api/performance/<int:pid>", methods=["DELETE"])
def delete_performance(pid):
    conn = get_db()
    conn.execute("DELETE FROM performance WHERE id=?", (pid,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})

# ── Courses of fire ───────────────────────────────────────────
def _to_int(v):
    try:
        if v is None or v == "": return None
        return int(float(v))
    except (TypeError, ValueError):
        return None

def _clean_time_limit(raw, name):
    """Seconds allowed for a stage: blank means no limit. Returns (seconds or None, error or None)."""
    if raw is None or raw == "":
        return None, None
    n = _to_int(raw)
    if n is None or n < 1:
        return None, f"stage '{name}': the time limit must be a whole number of seconds, at least 1"
    return n, None

def _clean_course_stages(stages):
    """Stage list from the client -> (clean list, None) or (None, error text)."""
    if not isinstance(stages, list):
        return None, "stages must be a list"
    out = []
    for st in stages:
        st = st or {}
        name = str(st.get("name") or "").strip()
        shots = _to_int(st.get("shots"))
        if not name:
            return None, "every stage needs a name"
        if not shots or shots < 1:
            return None, f"stage '{name}' needs a shot count above 0"
        limit, err = _clean_time_limit(st.get("time_limit"), name)
        if err:
            return None, err
        out.append({"name": name, "distance": _to_float(st.get("distance")) or None,
                    "distance_unit": "m" if st.get("distance_unit") == "m" else "yd", "shots": shots,
                    "time_limit": limit})
    return out, None

def _course_json(row):
    d = dict(row)
    d["stages"] = json.loads(d["stages"] or "[]")
    return d

@app.route("/api/courses")
def list_courses():
    conn = get_db()
    rows = conn.execute("SELECT * FROM courses ORDER BY id").fetchall()
    conn.close()
    return jsonify([_course_json(r) for r in rows])

def _save_course(cid):
    data = request.json or {}
    name = str(data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400
    stages, err = _clean_course_stages(data.get("stages", []))
    if err:
        return jsonify({"error": err}), 400
    conn = get_db()
    try:
        if cid is None:
            cid = conn.execute("INSERT INTO courses(name, stages) VALUES(?,?)", (name, json.dumps(stages))).lastrowid
        else:
            conn.execute("UPDATE courses SET name=?, stages=? WHERE id=?", (name, json.dumps(stages), cid))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        return jsonify({"error": "a course with that name already exists"}), 409
    row = conn.execute("SELECT * FROM courses WHERE id=?", (cid,)).fetchone()
    conn.close()
    return jsonify(_course_json(row)), (201 if request.method == "POST" else 200)

@app.route("/api/courses", methods=["POST"])
def create_course():
    return _save_course(None)

@app.route("/api/courses/<int:cid>", methods=["PUT"])
def update_course(cid):
    return _save_course(cid)

@app.route("/api/courses/<int:cid>", methods=["DELETE"])
def delete_course(cid):
    # Matches keep their own copy of the stages and the course name, so they are unaffected
    conn = get_db()
    conn.execute("DELETE FROM courses WHERE id=?", (cid,))
    conn.execute("UPDATE matches SET course_id=NULL WHERE course_id=?", (cid,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})

# ── Matches ───────────────────────────────────────────────────
def _clean_match_stages(stages):
    """Validate a match's stage rows. Score is at most 10 a shot; X count at most the shot count."""
    if not isinstance(stages, list):
        return None, "stages must be a list"
    out, seen = [], set()
    for st in stages:
        st = st or {}
        name = str(st.get("name") or "").strip()
        shots = _to_int(st.get("shots"))
        if not name:
            return None, "every stage needs a name"
        if not shots or shots < 1:
            return None, f"stage '{name}' needs a shot count above 0"
        score, xc = _to_int(st.get("score")), _to_int(st.get("x_count"))
        if score is not None and not 0 <= score <= shots * 10:
            return None, f"stage '{name}': score must be between 0 and {shots * 10}"
        if xc is not None and not 0 <= xc <= shots:
            return None, f"stage '{name}': X count must be between 0 and {shots}"
        limit, err = _clean_time_limit(st.get("time_limit"), name)
        if err:
            return None, err
        pid = _to_int(st.get("performance_id"))
        if pid is not None:
            if pid in seen:
                return None, "a session can only be linked to one stage"
            seen.add(pid)
        out.append({"name": name, "distance": _to_float(st.get("distance")) or None,
                    "distance_unit": "m" if st.get("distance_unit") == "m" else "yd",
                    "shots": shots, "score": score, "x_count": xc, "performance_id": pid, "time_limit": limit})
    return out, None

def _match_json(conn, row):
    m = dict(row)
    m["stages"] = [dict(r) for r in conn.execute("""
        SELECT s.*, p.lot_number, p.date AS session_date, p.score_text, p.score_total, p.use_score,
               p.rescore_text, p.rescore_total,
               (SELECT (MAX(ts) - MIN(ts)) / 1000.0 FROM shotmarker_shots WHERE performance_id = s.performance_id AND sighter = 0) AS string_seconds
        FROM match_stages s LEFT JOIN performance p ON p.id = s.performance_id
        WHERE s.match_id=? ORDER BY s.idx, s.id""", (m["id"],)).fetchall()]
    for st in m["stages"]:
        st["string_times"] = _string_times(conn, st["performance_id"]) if st["performance_id"] and st["time_limit"] else None
    f = conn.execute("SELECT name FROM firearms WHERE id=?", (m["firearm_id"],)).fetchone() if m["firearm_id"] else None
    m["firearm_name"] = f["name"] if f else None
    return m

def _write_match(conn, mid, data, stages):
    cols = ["name", "date", "location", "entry_type", "course_id", "course_name", "firearm_id", "notes"]
    vals = {c: (data.get(c) or None) for c in cols}
    vals["course_id"] = _to_int(vals["course_id"])
    vals["firearm_id"] = _to_int(vals["firearm_id"])
    if vals["course_id"] and not vals["course_name"]:
        c = conn.execute("SELECT name FROM courses WHERE id=?", (vals["course_id"],)).fetchone()
        vals["course_name"] = c["name"] if c else None
    if mid is None:
        marks = ",".join(["?"] * len(cols))
        mid = conn.execute(f"INSERT INTO matches ({','.join(cols)}) VALUES ({marks})", list(vals.values())).lastrowid
    else:
        sets = ", ".join(c + "=?" for c in cols)
        conn.execute(f"UPDATE matches SET {sets} WHERE id=?", [*vals.values(), mid])
        conn.execute("DELETE FROM match_stages WHERE match_id=?", (mid,))
    for i, st in enumerate(stages):
        conn.execute(
            "INSERT INTO match_stages (match_id, idx, name, distance, distance_unit, shots, score, x_count, performance_id, time_limit) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (mid, i, st["name"], st["distance"], st["distance_unit"], st["shots"], st["score"], st["x_count"],
             st["performance_id"], st["time_limit"]))
    return mid

def _save_match(mid):
    data = request.json or {}
    if not str(data.get("name") or "").strip():
        return jsonify({"error": "name is required"}), 400
    conn = get_db()
    stages = data.get("stages")
    if stages is None:       # new match from a course: copy that course's stages
        c = conn.execute("SELECT name, stages FROM courses WHERE id=?", (_to_int(data.get("course_id")),)).fetchone()
        stages = json.loads(c["stages"]) if c else []
    stages, err = _clean_match_stages(stages)
    if err:
        conn.close()
        return jsonify({"error": err}), 400
    # A session belongs to at most one match
    for st in stages:
        if st["performance_id"] is None:
            continue
        other = conn.execute("SELECT m.name FROM match_stages s JOIN matches m ON m.id=s.match_id "
                             "WHERE s.performance_id=? AND s.match_id IS NOT ?", (st["performance_id"], mid)).fetchone()
        if other:
            conn.close()
            return jsonify({"error": f"that session is already part of '{other['name']}'"}), 409
    try:
        mid = _write_match(conn, mid, data, stages)
        conn.commit()
    except sqlite3.IntegrityError as e:
        conn.close()
        return jsonify({"error": str(e)}), 400
    out = _match_json(conn, conn.execute("SELECT * FROM matches WHERE id=?", (mid,)).fetchone())
    conn.close()
    return jsonify(out), (201 if request.method == "POST" else 200)

@app.route("/api/matches")
def list_matches():
    conn = get_db()
    rows = conn.execute("SELECT * FROM matches ORDER BY date DESC, id DESC").fetchall()
    out = [_match_json(conn, r) for r in rows]
    conn.close()
    return jsonify(out)

@app.route("/api/matches", methods=["POST"])
def create_match():
    return _save_match(None)

@app.route("/api/matches/<int:mid>", methods=["PUT"])
def update_match(mid):
    return _save_match(mid)

@app.route("/api/matches/<int:mid>", methods=["DELETE"])
def delete_match(mid):
    conn = get_db()
    conn.execute("DELETE FROM matches WHERE id=?", (mid,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})

# ── Version ───────────────────────────────────────────────────
@app.route("/api/version")
def app_version():
    return jsonify({"version": os.environ.get("APP_VERSION", "dev")})

# ── Stats ─────────────────────────────────────────────────────
@app.route("/api/stats")
def stats():
    conn = get_db()
    total = conn.execute("SELECT COUNT(*) FROM loads").fetchone()[0]
    carts = conn.execute("SELECT COUNT(DISTINCT cartridge) FROM loads WHERE cartridge IS NOT NULL").fetchone()[0]
    inv   = conn.execute("SELECT COUNT(*) FROM inventory").fetchone()[0]
    guns  = conn.execute("SELECT COUNT(*) FROM firearms").fetchone()[0]
    conn.close()
    return jsonify({"total":total,"cartridges":carts,"inventory":inv,"firearms":guns})

# ── Autocomplete ──────────────────────────────────────────────
@app.route("/api/distinct/<column>")
def distinct_values(column):
    if column not in LOAD_COLUMNS: return jsonify([])
    conn = get_db()
    rows = conn.execute(
        f"SELECT DISTINCT {column} FROM loads WHERE {column} IS NOT NULL ORDER BY {column}"
    ).fetchall()
    conn.close()
    return jsonify([r[0] for r in rows])

@app.route("/api/inventory/names/<category>")
def inventory_names(category):
    conn = get_db()
    rows = conn.execute(
        "SELECT name, brand FROM inventory WHERE category=? ORDER BY name", (category,)
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

# ── Settings ──────────────────────────────────────────────────
@app.route("/api/settings")
def get_settings_api():
    return jsonify(get_settings())

@app.route("/api/settings", methods=["POST"])
def save_settings():
    data = request.json or {}
    conn = get_db()
    for k, v in data.items():
        conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (k, str(v)))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})

# ── Backup ────────────────────────────────────────────────────
@app.route("/api/backup")
def backup_db():
    import tempfile, os
    src = get_db()
    buf = io.BytesIO()
    dst_path = None
    try:
        fd, dst_path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        dst = sqlite3.connect(dst_path)
        src.backup(dst)
        dst.close()
        with open(dst_path, 'rb') as f:
            buf.write(f.read())
    finally:
        src.close()
        if dst_path:
            try: os.unlink(dst_path)
            except: pass
    buf.seek(0)
    filename = f"reloading_backup_{datetime.date.today().strftime('%Y%m%d')}.db"
    return Response(
        buf.read(),
        mimetype="application/octet-stream",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )

# ── Export/Import ─────────────────────────────────────────────
@app.route("/api/export")
def export_csv():
    conn = get_db()
    rows = conn.execute("SELECT * FROM loads ORDER BY id DESC").fetchall()
    conn.close()
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["id"]+LOAD_COLUMNS)
    for r in rows: w.writerow([r["id"]]+[r[c] for c in LOAD_COLUMNS])
    return Response(out.getvalue(), mimetype="text/csv",
        headers={"Content-Disposition":"attachment; filename=reloading_log.csv"})

@app.route("/api/import", methods=["POST"])
def import_csv():
    file = request.files.get("file")
    if not file: return jsonify({"error":"No file"}), 400
    reader = csv.DictReader(io.StringIO(file.stream.read().decode("utf-8")))
    count = 0
    conn = get_db()
    for row in reader:
        vals = {c: row.get(c) or None for c in LOAD_COLUMNS}
        cols = list(vals.keys())
        conn.execute(
            f"INSERT INTO loads ({','.join(cols)}) VALUES ({','.join(['?']*len(cols))})",
            list(vals.values()))
        count += 1
    conn.commit()
    conn.close()
    return jsonify({"imported": count})

# ── Label PDF ─────────────────────────────────────────────────
@app.route("/api/loads/<int:load_id>/label")
def print_label(load_id):
    s = get_settings()
    # Require a matching key for unauthenticated label access
    expected_key = s.get("label_key", "")
    provided_key = request.args.get("key", "")
    if expected_key and provided_key != expected_key:
        return jsonify({"error": "Unauthorized"}), 401

    conn = get_db()
    row = conn.execute("SELECT * FROM loads WHERE id=?", (load_id,)).fetchone()
    conn.close()
    if not row: return jsonify({"error":"Not found"}), 404
    r = dict(row)

    # 203 DPI thermal: 1pt = 203/72 = 2.819 dots. Keep values on clean dot boundaries.
    MM = 2.8346
    units = s.get("label_units", "mm")
    if units == "in":
        W = float(s.get("label_width_mm",  "50")) * inch
        H = float(s.get("label_height_mm", "30")) * inch
    else:
        W = float(s.get("label_width_mm",  "50")) * MM
        H = float(s.get("label_height_mm", "30")) * MM

    PAD      = 4
    HDR_FONT = 8
    VAL_FONT = 7
    LBL_FONT = 6
    FTR_FONT = 6

    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=(W, H))

    def val(key):
        v = r.get(key)
        return str(v) if v not in (None, "", "None") else ""

    def show(key):
        return s.get(f"label_show_{key}", "1") == "1"

    def clip(text, max_pts, font, size):
        while len(text) > 1 and c.stringWidth(text, font, size) > max_pts:
            text = text[:-1]
        return text

    # ── Header bar — taller for two lines ───────────────────────
    custom_name = s.get("label_name", "").strip()
    lot_str = val("lot_number")
    # Use taller header when we have two lines to show
    HDR_H = HDR_FONT + 14 if (custom_name and lot_str) else HDR_FONT + 7

    c.setFillColor(colors.black)
    c.rect(0, H - HDR_H, W, HDR_H, fill=1, stroke=0)
    c.setFillColor(colors.white)

    if show("cartridge"):
        cart = clip(val("cartridge") or "Unknown", W * 0.62 - PAD * 2, "Helvetica-Bold", HDR_FONT)
        c.setFont("Helvetica-Bold", HDR_FONT)
        # Vertically centre cartridge in header
        c.drawString(PAD, H - HDR_H + (HDR_H - HDR_FONT) / 2 - 1, cart)

    right_width = W * 0.40 - PAD
    if custom_name and lot_str:
        # Two lines: name on top, lot below
        name_y = H - HDR_H + (HDR_H - LBL_FONT) * 0.72
        lot_y  = H - HDR_H + (HDR_H - LBL_FONT) * 0.22
        c.setFont("Helvetica-Bold", LBL_FONT)
        c.drawRightString(W - PAD, name_y, clip(custom_name, right_width, "Helvetica-Bold", LBL_FONT))
        c.setFont("Helvetica", LBL_FONT)
        c.drawRightString(W - PAD, lot_y, clip(lot_str, right_width, "Helvetica", LBL_FONT))
    elif custom_name:
        c.setFont("Helvetica-Bold", LBL_FONT)
        c.drawRightString(W - PAD, H - HDR_H + (HDR_H - LBL_FONT) / 2 - 1, clip(custom_name, right_width, "Helvetica-Bold", LBL_FONT))
    elif lot_str:
        c.setFont("Helvetica", LBL_FONT)
        c.drawRightString(W - PAD, H - HDR_H + (HDR_H - LBL_FONT) / 2 - 1, clip(lot_str, right_width, "Helvetica", LBL_FONT))

    # ── Footer bar ───────────────────────────────────────────────
    FTR_H = FTR_FONT + 6   # ~12pt

    footer_fields = [
        ("date",            val("date")),
        ("case_type",       val("case_type")),
        ("annealed",        f"Ann:{val('annealed')}" if val("annealed") else ""),
        ("number_of_loads", f"Loads:{val('number_of_loads')}" if val("number_of_loads") else ""),
        ("rounds_made",     f"{val('rounds_made')}rds" if val("rounds_made") else ""),
    ]
    footer_parts = [text for key, text in footer_fields if show(key) and text]

    c.setFillColor(colors.HexColor("#DDDDDD"))
    c.rect(0, 0, W, FTR_H, fill=1, stroke=0)
    footer_text = clip("  |  ".join(footer_parts), W - PAD * 2, "Helvetica", FTR_FONT)
    c.setFillColor(colors.black)
    c.setFont("Helvetica", FTR_FONT)
    c.drawString(PAD, FTR_H - FTR_FONT - 1, footer_text)

    # ── Body ─────────────────────────────────────────────────────
    body_fields = []
    if show("bullet_manufacturer") or show("bullet_type"):
        parts = []
        if show("bullet_manufacturer") and val("bullet_manufacturer"):
            parts.append(val("bullet_manufacturer"))
        if show("bullet_type") and val("bullet_type"):
            parts.append(val("bullet_type"))
        body_fields.append(("BULLET", " ".join(parts) or "—"))
    if show("bullet_weight"):
        body_fields.append(("WT gr", val("bullet_weight")))
    if show("powder_type"):
        body_fields.append(("POWDER", val("powder_type")))
    if show("powder_weight"):
        body_fields.append(("CHG gr", val("powder_weight")))
    # COAL and BTO are alternate ways to give the same dimension, so they share one cell.
    # Older settings only had the COAL on/off switch, so fall back to it when no mode is saved.
    dims_mode = s.get("label_dims_mode") or ("auto" if show("cartridge_length") else "off")
    if dims_mode != "off":
        coal, bto = val("cartridge_length"), val("base_to_ogive")
        if dims_mode == "coal":   dims = [("COAL", coal)]
        elif dims_mode == "bto":  dims = [("BTO", bto)]
        else:                     dims = [(lbl, v) for lbl, v in (("COAL", coal), ("BTO", bto)) if v]
        if not dims: dims = [("COAL", "")]
        body_fields.append((" / ".join(d[0] for d in dims), " / ".join(d[1] for d in dims if d[1])))
    if show("primer"):
        body_fields.append(("PRIMER", val("primer")))

    if not body_fields:
        c.save(); buf.seek(0)
        return Response(buf.read(), mimetype="application/pdf",
            headers={"Content-Disposition": f"inline; filename=label_{load_id}.pdf"})

    # Pair into rows of 2
    pairs = []
    for i in range(0, len(body_fields), 2):
        left  = body_fields[i]
        right = body_fields[i+1] if i+1 < len(body_fields) else None
        pairs.append((left, right))

    BODY_TOP = H - HDR_H - 2
    BODY_BOT = FTR_H + 2
    BODY_H   = BODY_TOP - BODY_BOT
    ROW_H    = BODY_H / len(pairs)
    half     = (W - PAD * 3) / 2

    # ── Divider lines ─────────────────────────────────────────────
    c.setStrokeColor(colors.black)
    c.setLineWidth(0.75)
    # Horizontal between rows
    for i in range(1, len(pairs)):
        y = BODY_BOT + i * ROW_H
        c.line(0, y, W, y)
    # Vertical between left/right cells
    for i, (left, right) in enumerate(pairs):
        if right:
            y0 = BODY_BOT + i * ROW_H
            c.line(half + PAD * 1.5, y0, half + PAD * 1.5, y0 + ROW_H)

    # ── Draw cell contents ───────────────────────────────────────
    def draw_cell(label, value, x, y, w, rh):
        v = clip(value if value else "—", w - 2, "Helvetica-Bold", VAL_FONT)
        c.setFillColor(colors.black)
        c.setFont("Helvetica", LBL_FONT)
        c.drawString(x, y + rh - LBL_FONT - 0.5, label)
        c.setFillColor(colors.black)
        c.setFont("Helvetica-Bold", VAL_FONT)
        c.drawString(x, y + 1, v)

    for i, (left, right) in enumerate(reversed(pairs)):
        y = BODY_BOT + i * ROW_H
        if right:
            draw_cell(left[0],  left[1],  PAD,        y, half, ROW_H)
            draw_cell(right[0], right[1], PAD*2+half, y, half, ROW_H)
        else:
            draw_cell(left[0], left[1], PAD, y, W - PAD*2, ROW_H)

    c.save(); buf.seek(0)
    return Response(buf.read(), mimetype="application/pdf",
        headers={"Content-Disposition": f"inline; filename=label_{load_id}.pdf"})

# ── ShotMarker ────────────────────────────────────────────────

@app.route("/api/shotmarker/upload", methods=["POST"])
def shotmarker_upload():
    """Accept a .tar export, decode it, return session data (no DB write)."""
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "No file"}), 400
    tmp = tempfile.NamedTemporaryFile(suffix=".tar", delete=False)
    f.save(tmp.name)
    try:
        sessions = decode_tar(tmp.name)
    except Exception as e:
        return jsonify({"error": str(e)}), 400
    finally:
        os.unlink(tmp.name)
    return jsonify(sessions)

@app.route("/api/performance/<int:pid>/shots", methods=["GET"])
def get_perf_shots(pid):
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM shotmarker_shots WHERE performance_id=? ORDER BY idx",
        (pid,)
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/performance/<int:pid>/shots", methods=["POST"])
def save_perf_shots(pid):
    """Save selected shots for a performance record, auto-update perf stats."""
    data = request.json or {}
    shots    = data.get("shots", [])
    face_id  = data.get("face_id", "")
    distance = data.get("distance")
    dist_unit= data.get("dist_unit", "y")
    bullet   = data.get("bullet_d_mm", 7.82)

    conn = get_db()
    # Verify performance record exists
    row = conn.execute("SELECT id FROM performance WHERE id=?", (pid,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "Performance record not found"}), 404

    # Replace existing shots
    conn.execute("DELETE FROM shotmarker_shots WHERE performance_id=?", (pid,))
    cols = ["performance_id","idx","label","score","sighter",
            "x_mm","y_mm","x_moa","y_moa","v_fps","temp_c","ts",
            "face_id","distance","dist_unit","bullet_d_mm"]
    for s in shots:
        conn.execute(
            f"INSERT INTO shotmarker_shots ({','.join(cols)}) VALUES ({','.join(['?']*len(cols))})",
            [pid, s.get("idx"), s.get("label"), s.get("score"),
             1 if s.get("sighter") else 0,
             s.get("x_mm"), s.get("y_mm"), s.get("x_moa"), s.get("y_moa"),
             s.get("v_fps"), s.get("temp_c"), s.get("ts"),
             face_id, distance, dist_unit, bullet]
        )

    # A fresh import means the score is the device's original again, so it's trusted
    _store_score(conn, pid)
    conn.execute("UPDATE performance SET use_score=1, orig_face=?, rescore_text=NULL, rescore_total=NULL, rescore_face=NULL WHERE id=?",
                 (face_id or None, pid))

    # Auto-update performance stats from selected record shots
    stats = compute_stats(shots)
    sets, vals = [], []
    if stats["num_shots"]:
        # Get current performance record to retrieve distance and unit for conversion
        perf = conn.execute("SELECT distance, distance_unit, group_unit FROM performance WHERE id=?", (pid,)).fetchone()
        dist_yd  = float(perf["distance"]) if perf and perf["distance"] else None
        dist_unit = perf["distance_unit"] if perf else "yd"
        grp_unit  = perf["group_unit"] if perf else "MOA"

        # Convert distance to yards for angular conversion
        if dist_unit == "m" and dist_yd:
            dist_yd = dist_yd * 1.09361

        # Convert group measurements from mm to angular units
        group_size = mm_to_angular(stats["group_size_mm"], dist_yd, grp_unit)
        group_x    = mm_to_angular(stats["group_x_mm"],   dist_yd, grp_unit)
        group_y    = mm_to_angular(stats["group_y_mm"],   dist_yd, grp_unit)

        sets += ["num_shots=?","avg_velocity=?","std_dev=?","extreme_spread=?",
                 "group_size=?","group_x=?","group_y=?"]
        vals += [stats["num_shots"], stats["avg_velocity"],
                 stats["std_dev"], stats["extreme_spread"],
                 group_size, group_x, group_y]

    if sets:
        conn.execute(f"UPDATE performance SET {','.join(sets)} WHERE id=?", vals+[pid])

    conn.commit()
    conn.close()
    return jsonify({"ok": True, "stats": stats})

@app.route("/api/performance/<int:pid>/shots", methods=["DELETE"])
def delete_perf_shots(pid):
    conn = get_db()
    conn.execute("DELETE FROM shotmarker_shots WHERE performance_id=?", (pid,))
    conn.execute("UPDATE performance SET score_text=NULL, score_total=NULL, orig_face=NULL, "
                 "rescore_text=NULL, rescore_total=NULL, rescore_face=NULL WHERE id=?", (pid,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


# ── Chronograph (Garmin CSV) → muzzle velocity ────────────────
def parse_garmin_csv(text):
    """Parse a Garmin Xero session export into shots (speeds stored as fps)."""
    rows = [[c.strip() for c in r] for r in csv.reader(io.StringIO(text.lstrip("﻿")), skipinitialspace=True)]
    title = rows[0][0] if rows and rows[0] else ""
    head_i = next((i for i, r in enumerate(rows) if r and r[0] == "#"), None)
    if head_i is None:
        raise ValueError("This doesn't look like a Garmin chronograph export")

    cols, to_fps = {}, 1.0
    for j, h in enumerate(rows[head_i]):
        h = h.upper()
        if h.startswith("SPEED"):
            cols["speed"] = j
            if "M/S" in h or "MPS" in h: to_fps = 3.28084
        elif h.startswith("KE"):            cols["ke"] = j
        elif h.startswith("POWER FACTOR"):  cols["pf"] = j
        elif h == "TIME":                   cols["time"] = j
        elif h == "CLEAN BORE":             cols["clean"] = j
        elif h == "COLD BORE":              cols["cold"] = j
        elif h == "SHOT NOTES":             cols["notes"] = j
    if "speed" not in cols:
        raise ValueError("No speed column found in this file")

    def cell(r, key):
        j = cols.get(key)
        return r[j] if j is not None and j < len(r) else ""
    def flag(v): return v.lower() not in ("", "0", "no", "false", "-")
    def num(v):
        f = _to_float(v)
        return f if f else None

    shots = []
    for r in rows[head_i + 1:]:
        if not r or not r[0].isdigit():
            break
        speed = _to_float(cell(r, "speed")) * to_fps
        if speed <= 0:
            continue
        shots.append({
            "idx": int(r[0]), "speed_fps": round(speed, 1), "time": cell(r, "time"),
            "clean_bore": flag(cell(r, "clean")), "cold_bore": flag(cell(r, "cold")),
            "notes": cell(r, "notes"), "ke": num(cell(r, "ke")), "power_factor": num(cell(r, "pf")),
        })
    if not shots:
        raise ValueError("No shots found in this file")

    date = None
    for r in rows:
        if r and r[0].upper() == "DATE" and len(r) > 1:
            try: date = datetime.datetime.strptime(r[1], "%B %d, %Y at %H:%M").date().isoformat()
            except ValueError: pass
    return {"title": title, "date": date, "shots": shots}

def _update_muzzle_stats(conn, pid):
    shots = [{"v_fps": r["speed_fps"]} for r in conn.execute(
        "SELECT speed_fps FROM chrono_shots WHERE performance_id=? ORDER BY idx", (pid,)).fetchall()]
    stats = compute_stats(shots)
    conn.execute(
        "UPDATE performance SET muzzle_avg_velocity=?, muzzle_std_dev=?, muzzle_extreme_spread=? WHERE id=?",
        (stats["avg_velocity"], stats["std_dev"], stats["extreme_spread"], pid))
    return stats

@app.route("/api/chrono/upload", methods=["POST"])
def chrono_upload():
    """Read a Garmin CSV and return its shots (no DB write)."""
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "No file"}), 400
    try:
        return jsonify(parse_garmin_csv(f.read().decode("utf-8-sig", errors="replace")))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

@app.route("/api/performance/<int:pid>/chrono", methods=["GET"])
def get_perf_chrono(pid):
    conn = get_db()
    rows = conn.execute("SELECT * FROM chrono_shots WHERE performance_id=? ORDER BY idx", (pid,)).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/performance/<int:pid>/chrono", methods=["POST"])
def save_perf_chrono(pid):
    """Replace a session's chronograph shots and recompute its muzzle velocity stats."""
    shots = [s for s in (request.json or {}).get("shots", []) if _to_float(s.get("speed_fps")) > 0]
    conn = get_db()
    if not conn.execute("SELECT id FROM performance WHERE id=?", (pid,)).fetchone():
        conn.close()
        return jsonify({"error": "Performance record not found"}), 404
    conn.execute("DELETE FROM chrono_shots WHERE performance_id=?", (pid,))
    for n, s in enumerate(shots, 1):
        conn.execute(
            """INSERT INTO chrono_shots (performance_id, idx, speed_fps, time, clean_bore, cold_bore,
                                         notes, ke, power_factor, sm_idx)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (pid, s.get("idx") or n, _to_float(s["speed_fps"]), s.get("time"),
             1 if s.get("clean_bore") else 0, 1 if s.get("cold_bore") else 0,
             s.get("notes"), s.get("ke"), s.get("power_factor"), s.get("sm_idx")))
    stats = _update_muzzle_stats(conn, pid)
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "stats": stats})

@app.route("/api/performance/<int:pid>/chrono", methods=["DELETE"])
def delete_perf_chrono(pid):
    conn = get_db()
    conn.execute("DELETE FROM chrono_shots WHERE performance_id=?", (pid,))
    _update_muzzle_stats(conn, pid)
    conn.commit()
    conn.close()
    return jsonify({"ok": True})

init_db()
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
