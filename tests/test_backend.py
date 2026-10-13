"""Backend tests. No installs needed: Flask and reportlab are replaced with tiny stubs, app.py is
run against a throwaway SQLite file, and its functions are called directly.

    python tests/test_backend.py
"""
import io, os, sys, types, shutil, sqlite3, tempfile

sys.dont_write_bytecode = True
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIX = os.path.join(ROOT, "tests", "fixtures")
TMP = tempfile.mkdtemp()
os.environ["DB_PATH"] = os.path.join(TMP, "test.db")
sys.path.insert(0, ROOT)


def mod(name, **attrs):
    m = types.ModuleType(name)
    m.__dict__.update(attrs)
    sys.modules[name] = m
    return m


class FakeApp:
    def __init__(self, *a, **k): self.wsgi_app = None
    def route(self, *a, **k): return lambda f: f

class Req:
    json = None
    files = {}
    args = {}

request = Req()
mod("flask", Flask=FakeApp, jsonify=lambda o=None, **k: o, request=request,
    send_from_directory=lambda *a, **k: None, Response=object)
mod("werkzeug"); mod("werkzeug.middleware")
mod("werkzeug.middleware.proxy_fix", ProxyFix=lambda app, **k: app)
mod("reportlab"); mod("reportlab.lib", colors=None); mod("reportlab.lib.pagesizes", inch=72)
mod("reportlab.pdfgen", canvas=None)

ns = {"__name__": "app_under_test"}
exec(compile(open(os.path.join(ROOT, "app.py"), encoding="utf-8").read(), "app.py", "exec"), ns)

results = []
def check(name, cond, extra=""):
    results.append(bool(cond))
    print(("PASS " if cond else "FAIL ") + name + (("  -> " + str(extra)) if (extra and not cond) else ""))

def call(fn, body=None, *a):
    request.json = body
    r = fn(*a)
    return r[0] if isinstance(r, tuple) else r

con = sqlite3.connect(os.environ["DB_PATH"])
con.row_factory = sqlite3.Row
cols = lambda t: [r["name"] for r in con.execute("PRAGMA table_info(%s)" % t)]
row = lambda pid: dict(con.execute("SELECT * FROM performance WHERE id=?", (pid,)).fetchone())

# ── database setup ────────────────────────────────────────────
check("migration: muzzle columns", {"muzzle_avg_velocity", "muzzle_std_dev", "muzzle_extreme_spread"} <= set(cols("performance")))
check("migration: score columns", {"score_text", "score_total", "use_score"} <= set(cols("performance")))
check("migration: chrono_shots table", "sm_idx" in cols("chrono_shots"))
ns["init_db"]()
check("init_db is idempotent", True)

# ── real ShotMarker archive vs the device's own screen ────────
sessions = ns["decode_tar"](os.path.join(FIX, "SM_export_Aug_27.tar"))
check("archive decodes to 3 strings", len(sessions) == 3, len(sessions))
c = [s for s in sessions if len(s["shots"]) == 22][0]
DEVICE_SCORES = ['7', '7', '9', 'X', '8', 'X', '10', '10', 'X', '10', '10', 'X', '10', '8', '10', '10', '10', 'X', '10', '9', '8', '9']
check("decoded scores match the ShotMarker screen", [s["score"] for s in c["shots"]] == DEVICE_SCORES)
check("sighters are labelled S1, S2", [s["label"] for s in c["shots"] if s["sighter"]] == ["S1", "S2"])
check("last shot is 9 at 1391 fps (as on the device)", c["shots"][-1]["score"] == "9" and c["shots"][-1]["v_fps"] == 1391.0)
check("timestamps run forward", all(a["ts"] < b["ts"] for a, b in zip(c["shots"], c["shots"][1:])))
for s in sessions:
    total, text = ns["compute_score"](s["shots"], s["face_id"])
    check("score text equals ShotMarker's (%s)" % s["group_text"], text == s["group_text"], text)

# ── scoring rules ─────────────────────────────────────────────
cs = ns["compute_score"]
mk = lambda *sc, sighters=0: [{"score": x, "sighter": i < sighters} for i, x in enumerate(sc)]
check("score: sighters out, X counts as 10", cs(mk("7", "7", "9", "X", "10", "X", sighters=2), "NRA_MR1") == (39, "39-2X"))
check("score: X is 6 on F-Class X-ring faces", cs(mk("X", "5"), "IFX600y") == (11, "11-1X"))
check("score: V counts 5 and is labelled V", cs(mk("V", "4"), "IF600y") == (9, "9-1V"))
check("score: no scores -> None", cs(mk("", None)) == (None, None))

# ── session edits: distance, face, use_score ──────────────────
pa = call(ns["create_performance"], {"lot_number": "S1", "distance": 600, "distance_unit": "yd", "group_unit": "MOA"})
sid = pa["id"]
shots = [{"idx": i, "label": str(i), "score": sc, "sighter": i == 0, "x_mm": float(x), "y_mm": float(y), "v_fps": 1400.0}
         for i, (sc, x, y) in enumerate([("7", 5, 5), ("10", 0, 0), ("X", 30, 10), ("9", -20, 40), ("8", 10, -30)])]
call(ns["save_perf_shots"], {"shots": shots, "face_id": "NRA_MR1", "distance": 600, "dist_unit": "y"}, sid)
r = row(sid)
check("import stores score text/total and trusts it", (r["score_text"], r["score_total"], r["use_score"]) == ("37-1X", 37, 1), r)
lst = [x for x in call(ns["list_performance"]) if x["id"] == sid][0]
check("list includes the target face", lst["face_id"] == "NRA_MR1")
g600 = r["group_size"]
check("group size saved at 600 yd", g600 and g600 > 0, g600)

body = dict(r); body.pop("id"); body.update(distance=300)
call(ns["update_performance"], body, sid)
r2 = row(sid)
sh = con.execute("SELECT distance, dist_unit, x_mm, x_moa FROM shotmarker_shots WHERE performance_id=? AND idx=2", (sid,)).fetchone()
check("distance 600->300 doubles the angular group", abs(r2["group_size"] - g600 * 2) < 0.03, (g600, r2["group_size"]))
check("distance change: shots synced, measured mm kept", sh["distance"] == 300 and sh["dist_unit"] == "y" and sh["x_mm"] == 30.0
      and abs(sh["x_moa"] - 30 / (0.29088822 * 300 * 0.9144)) < 0.001, dict(sh))
check("distance change alone leaves the score alone", r2["score_text"] == "37-1X" and r2["use_score"] == 1)

body = dict(r2); body.pop("id"); body.update(group_unit="Mil")
call(ns["update_performance"], body, sid)
check("group unit MOA->Mil recomputed", row(sid)["group_size"] < r2["group_size"] / 3)

body = dict(row(sid)); body.pop("id"); body.update(distance=100, group_size=9.99)
call(ns["update_performance"], body, sid)
check("a hand-typed group size is not overwritten", row(sid)["group_size"] == 9.99)
body = dict(row(sid)); body.pop("id"); body.update(notes="hello")
call(ns["update_performance"], body, sid)
check("unrelated edit leaves values alone", row(sid)["group_size"] == 9.99)

body = dict(row(sid)); body.pop("id"); body.update(distance_unit="m", use_score=False)
call(ns["update_performance"], body, sid)
check("unit change synced to shots", con.execute("SELECT dist_unit FROM shotmarker_shots WHERE performance_id=? AND idx=2", (sid,)).fetchone()[0] == "m")
check("use_score=false is saved", row(sid)["use_score"] == 0)

body = dict(row(sid)); body.pop("id"); body.update(face_id="NRA_MR31", use_score=False)
out = call(ns["update_performance"], body, sid)
faces = {x[0] for x in con.execute("SELECT DISTINCT face_id FROM shotmarker_shots WHERE performance_id=?", (sid,))}
check("face change applied to saved shots", faces == {"NRA_MR31"} and out["face_id"] == "NRA_MR31", faces)
check("face change keeps measured positions", con.execute("SELECT x_mm FROM shotmarker_shots WHERE performance_id=? AND idx=3", (sid,)).fetchone()[0] == -20.0)

body = dict(row(sid)); body.pop("id"); body.update(use_score=True)
call(ns["update_performance"], body, sid)
check("use_score can be turned back on", row(sid)["use_score"] == 1)
body = dict(row(sid)); body.pop("id"); body.update(use_score=False)
call(ns["update_performance"], body, sid)
call(ns["save_perf_shots"], {"shots": shots, "face_id": "NRA_MR1", "distance": 600, "dist_unit": "y"}, sid)
check("a fresh import trusts the score again", row(sid)["use_score"] == 1)

pn = call(ns["create_performance"], {"lot_number": "S2", "distance": 100})
body = dict(row(pn["id"])); body.pop("id"); body.update(face_id="NRA_MR1", distance=200)
call(ns["update_performance"], body, pn["id"])
check("session without shots: no score, no crash", row(pn["id"])["score_text"] is None)

call(ns["delete_perf_shots"], None, sid)
check("deleting shots clears the score", row(sid)["score_text"] is None)

pb = call(ns["create_performance"], {"lot_number": "S3", "distance": 600})
for i, sc in enumerate(("9", "X")):
    con.execute("INSERT INTO shotmarker_shots (performance_id, idx, score, sighter, face_id) VALUES (?,?,?,?,?)", (pb["id"], i, sc, 0, "NRA_MR1"))
con.commit()
ns["init_db"]()
check("startup backfills missing scores", row(pb["id"])["score_text"] == "19-1X")

# ── twist validation ──────────────────────────────────────────
good = call(ns["create_firearm"], {"name": "R1", "twist_rate": "1:8"})
check("firearm: 1:x twist accepted", good["twist_rate"] == "1:8")
r400 = ns["create_firearm"]  # request.json is still set from the last call; set it fresh
request.json = {"name": "R2", "twist_rate": "1-8"}
bad = r400()
check("firearm: other twist formats rejected (400)", isinstance(bad, tuple) and bad[1] == 400)

# ── Garmin chronograph ────────────────────────────────────────
class F:
    def __init__(self, data): self.data = data
    def read(self): return self.data

request.files = {"file": F(open(os.path.join(FIX, "garmin_sample.csv"), "rb").read())}
up = call(ns["chrono_upload"])
check("Garmin CSV parses (26 shots, dated 2026-08-15)", isinstance(up, dict) and len(up["shots"]) == 26 and up["date"] == "2026-08-15")
sp = [s["speed_fps"] for s in up["shots"]]
check("first/last speeds read correctly", sp[0] == 2623.3 and sp[-1] == 2601.3)
request.files = {"file": F(b"not,a,garmin\nfile")}
bad = ns["chrono_upload"]()
check("non-Garmin file rejected (400)", isinstance(bad, tuple) and bad[1] == 400)

pc = call(ns["create_performance"], {"lot_number": "C1", "distance": 600})["id"]
sel = up["shots"][:22]
for k, s in enumerate(sel): s["sm_idx"] = k
call(ns["save_perf_chrono"], {"shots": sel}, pc)
r = row(pc)
check("muzzle stats are Garmin's (2610.3 / 15.2 / 54.1)", (r["muzzle_avg_velocity"], r["muzzle_std_dev"], r["muzzle_extreme_spread"]) == (2610.3, 15.2, 54.1), r)
check("chrono shots stored with their pairing", con.execute("SELECT COUNT(*), MIN(sm_idx), MAX(sm_idx) FROM chrono_shots WHERE performance_id=?", (pc,)).fetchone()[:] == (22, 0, 21))
check("GET returns the shots", len(call(ns["get_perf_chrono"], None, pc)) == 22)
call(ns["save_perf_chrono"], {"shots": up["shots"][:5]}, pc)
check("saving again replaces the shots", con.execute("SELECT COUNT(*) FROM chrono_shots WHERE performance_id=?", (pc,)).fetchone()[0] == 5)
r404 = ns["save_perf_chrono"](99999)
check("unknown session -> 404", isinstance(r404, tuple) and r404[1] == 404)
call(ns["delete_perf_chrono"], None, pc)
check("delete clears shots and muzzle stats", con.execute("SELECT COUNT(*) FROM chrono_shots WHERE performance_id=?", (pc,)).fetchone()[0] == 0 and row(pc)["muzzle_avg_velocity"] is None)
p2 = call(ns["create_performance"], {"lot_number": "L2", "muzzle_avg_velocity": 2700, "muzzle_std_dev": 8.5})
check("hand-typed muzzle values are saved", p2["muzzle_avg_velocity"] == 2700)
call(ns["save_perf_chrono"], {"shots": up["shots"][:3]}, p2["id"])
call(ns["delete_performance"], None, p2["id"])
check("deleting a session removes its chrono shots", con.execute("SELECT COUNT(*) FROM chrono_shots").fetchone()[0] == 0)

# ── inventory check / add stock ───────────────────────────────
call(ns["create_inventory"], {"category": "Primers", "name": "BR-2", "quantity": 100, "unit": "count"})
iss = call(ns["check_inventory"], {"primer": "BR-2", "rounds_made": 250, "bullet_type": "ELD-M"})
kinds = {(i["category"], i["status"]) for i in iss}
check("inventory check: short primers and missing bullets", kinds == {("Primers", "short"), ("Bullets", "missing")}, iss)
short = [i for i in iss if i["status"] == "short"][0]
check("shortfall is needed minus available", short["needed"] == 250 and short["available"] == 100 and short["shortfall"] == 150)
out = call(ns["add_inventory_stock"], {"amount": 200}, short["inventory_id"])
check("add stock changes only the quantity", out["quantity"] == 300 and out["name"] == "BR-2" and out["unit"] == "count")

shutil.rmtree(TMP, ignore_errors=True)
print("\n%d/%d passed" % (sum(results), len(results)))
sys.exit(0 if all(results) else 1)
