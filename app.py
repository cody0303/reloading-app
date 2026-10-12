from flask import Flask, jsonify, request, send_from_directory, Response
import sqlite3, os, csv, io, json, datetime, random, string, secrets, tempfile
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
    """)
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
        "label_show_base_to_ogive":       "0",
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
        ("performance", "position",        "TEXT"),
    ]
    for table, col, coldef in migrations:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coldef}")
        except Exception:
            pass  # column already exists

    conn.commit()
    conn.close()

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

def _decrement_inventory(conn, data, rounds):
    """Decrement inventory based on a load record."""
    def deduct(category, name, qty):
        if not name or qty <= 0: return
        conn.execute("""
            UPDATE inventory SET quantity = MAX(0, quantity - ?)
            WHERE category=? AND LOWER(name)=LOWER(?)
        """, (qty, category, name))

    deduct("Bullets",      data.get("bullet_manufacturer","")+" "+data.get("bullet_type",""), rounds)
    # Also try just by bullet_type
    if data.get("bullet_type"):
        conn.execute("""
            UPDATE inventory SET quantity = MAX(0, quantity - ?)
            WHERE category='Bullets' AND LOWER(name)=LOWER(?)
        """, (rounds, data.get("bullet_type","")))
    if data.get("primer"):
        deduct("Primers", data.get("primer",""), rounds)
    if data.get("case_type"):
        deduct("Brass/Cases", data.get("case_type",""), rounds)
    if data.get("powder_type") and data.get("powder_weight"):
        # powder_weight is grains per round; convert total grains → lbs
        total_grains = float(data["powder_weight"]) * rounds
        total_lbs    = total_grains / 7000.0
        conn.execute("""
            UPDATE inventory SET quantity = MAX(0, quantity - ?)
            WHERE category='Powder' AND LOWER(name)=LOWER(?)
        """, (total_lbs, data.get("powder_type","")))

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

@app.route("/api/inventory/check", methods=["POST"])
def check_inventory():
    """Check which components from a load are missing from inventory."""
    data = request.json or {}
    missing = []
    conn = get_db()
    def exists(cat, name):
        if not name: return True
        r = conn.execute("SELECT id FROM inventory WHERE category=? AND LOWER(name)=LOWER(?)", (cat, name)).fetchone()
        return r is not None
    if not exists("Bullets", data.get("bullet_type","")):
        missing.append({"category":"Bullets","name":data.get("bullet_type",""),"brand":data.get("bullet_manufacturer","")})
    if not exists("Primers", data.get("primer","")):
        missing.append({"category":"Primers","name":data.get("primer","")})
    if not exists("Brass/Cases", data.get("case_type","")):
        missing.append({"category":"Brass/Cases","name":data.get("case_type","")})
    if not exists("Powder", data.get("powder_type","")):
        missing.append({"category":"Powder","name":data.get("powder_type","")})
    conn.close()
    return jsonify(missing)

# ── Firearms ──────────────────────────────────────────────────
@app.route("/api/firearms")
def list_firearms():
    conn = get_db()
    rows = conn.execute("SELECT * FROM firearms ORDER BY name").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/firearms", methods=["POST"])
def create_firearm():
    data = request.json or {}
    cols = ["name","caliber","manufacturer","barrel_length","barrel_length_unit","twist_rate","notes"]
    vals = {c: data.get(c) or None for c in cols}
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
    cols = ["name","caliber","manufacturer","barrel_length","barrel_length_unit","twist_rate","notes"]
    vals = {c: data.get(c) or None for c in cols}
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

# ── Performance ───────────────────────────────────────────────
@app.route("/api/performance")
def list_performance():
    lot = request.args.get("lot_number","")
    conn = get_db()
    if lot:
        rows = conn.execute("""
            SELECT p.*, f.name as firearm_name FROM performance p
            LEFT JOIN firearms f ON p.firearm_id=f.id
            WHERE p.lot_number=? ORDER BY p.id DESC
        """, (lot,)).fetchall()
    else:
        rows = conn.execute("""
            SELECT p.*, f.name as firearm_name FROM performance p
            LEFT JOIN firearms f ON p.firearm_id=f.id
            ORDER BY p.id DESC
        """).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/performance", methods=["POST"])
def create_performance():
    data = request.json or {}
    cols = ["lot_number","firearm_id","date","distance","distance_unit",
            "avg_velocity","std_dev","extreme_spread","group_size","group_x","group_y","group_unit","num_shots","position","notes"]
    vals = {c: data.get(c) or None for c in cols}
    conn = get_db()
    cur = conn.execute(
        f"INSERT INTO performance ({','.join(cols)}) VALUES ({','.join(['?']*len(cols))})",
        list(vals.values())
    )
    conn.commit()
    row = conn.execute("""
        SELECT p.*, f.name as firearm_name FROM performance p
        LEFT JOIN firearms f ON p.firearm_id=f.id WHERE p.id=?
    """, (cur.lastrowid,)).fetchone()
    conn.close()
    return jsonify(dict(row)), 201

@app.route("/api/performance/<int:pid>", methods=["PUT"])
def update_performance(pid):
    data = request.json or {}
    cols = ["lot_number","firearm_id","date","distance","distance_unit",
            "avg_velocity","std_dev","extreme_spread","group_size","group_x","group_y","group_unit","num_shots","position","notes"]
    vals = {c: data.get(c) or None for c in cols}
    sets = ", ".join(f"{c}=?" for c in vals)
    conn = get_db()
    conn.execute(f"UPDATE performance SET {sets} WHERE id=?", [*vals.values(), pid])
    conn.commit()
    row = conn.execute("""
        SELECT p.*, f.name as firearm_name FROM performance p
        LEFT JOIN firearms f ON p.firearm_id=f.id WHERE p.id=?
    """, (pid,)).fetchone()
    conn.close()
    return jsonify(dict(row))

@app.route("/api/performance/<int:pid>", methods=["DELETE"])
def delete_performance(pid):
    conn = get_db()
    conn.execute("DELETE FROM performance WHERE id=?", (pid,))
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
    if show("cartridge_length"):
        body_fields.append(("COAL", val("cartridge_length")))
    if show("primer"):
        body_fields.append(("PRIMER", val("primer")))
    if show("base_to_ogive"):
        body_fields.append(("BTO", val("base_to_ogive")))

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
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/performance/recalculate", methods=["POST"])
def recalculate_performance():
    conn = get_db()
    perf_ids = [r["performance_id"] for r in conn.execute(
        "SELECT DISTINCT performance_id FROM shotmarker_shots"
    ).fetchall()]
    updated = 0
    for pid in perf_ids:
        shots = [dict(r) for r in conn.execute(
            "SELECT * FROM shotmarker_shots WHERE performance_id=? ORDER BY idx", (pid,)
        ).fetchall()]
        if not shots: continue
        stats = compute_stats(shots)
        if not stats["num_shots"]: continue
        perf = conn.execute(
            "SELECT distance, distance_unit, group_unit FROM performance WHERE id=?", (pid,)
        ).fetchone()
        dist_yd = float(perf["distance"]) if perf and perf["distance"] else None
        if perf and perf["distance_unit"] == "m" and dist_yd:
            dist_yd = dist_yd * 1.09361
        grp_unit = perf["group_unit"] if perf else "MOA"
        group_size = mm_to_angular(stats["group_size_mm"], dist_yd, grp_unit)
        group_x    = mm_to_angular(stats["group_x_mm"],   dist_yd, grp_unit)
        group_y    = mm_to_angular(stats["group_y_mm"],   dist_yd, grp_unit)
        conn.execute("""UPDATE performance SET
            num_shots=?, avg_velocity=?, std_dev=?, extreme_spread=?,
            group_size=?, group_x=?, group_y=? WHERE id=?""",
            [stats["num_shots"], stats["avg_velocity"], stats["std_dev"],
             stats["extreme_spread"], group_size, group_x, group_y, pid])
        updated += 1
    conn.commit(); conn.close()
    return jsonify({"ok": True, "updated": updated})

init_db()
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
