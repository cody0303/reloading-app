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

ns = {"__name__": "app_under_test", "__file__": os.path.join(ROOT, "app.py")}
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

# ── matches and courses of fire ───────────────────────────────
courses = call(ns["list_courses"])
nmc = [c for c in courses if c["name"] == "National Match Course"][0]
check("six courses are seeded", len(courses) == 6, len(courses))
check("National Match Course is 4 stages and 50 shots", len(nmc["stages"]) == 4 and sum(s["shots"] for s in nmc["stages"]) == 50)
check("Long Course has 40 shots at 600", [c for c in courses if c["name"].endswith("Long Course")][0]["stages"][-1]["shots"] == 40)
call(ns["init_db"])
check("seeding does not run twice", len(call(ns["list_courses"])) == 6)

request.method = "POST"
m1 = call(ns["create_match"], {"name": "Club match", "date": "2026-09-05", "location": "Home range",
                               "entry_type": "Service Rifle", "course_id": nmc["id"]})
check("a new match copies the course's stages", len(m1["stages"]) == 4 and m1["course_name"] == "National Match Course"
      and [s["shots"] for s in m1["stages"]] == [10, 10, 10, 20], m1)
check("a new match starts with no scores", all(s["score"] is None and s["x_count"] is None for s in m1["stages"]))

request.json = {"name": "x", "stages": [{"name": "SF", "shots": 10, "score": 101}]}
r = ns["create_match"]()
check("a score above 10 a shot is refused", isinstance(r, tuple) and r[1] == 400 and "score" in r[0]["error"], r)
request.json = {"name": "x", "stages": [{"name": "SF", "shots": 10, "score": 90, "x_count": 11}]}
r = ns["create_match"]()
check("an X count above the shot count is refused", isinstance(r, tuple) and r[1] == 400, r)
request.json = {"name": "", "stages": []}
check("a match needs a name", ns["create_match"]()[1] == 400)

pa = call(ns["create_performance"], {"lot_number": "M1", "distance": 200, "distance_unit": "yd", "date": "2026-09-05"})["id"]
pb = call(ns["create_performance"], {"lot_number": "M2", "distance": 600, "distance_unit": "yd", "date": "2026-09-05"})["id"]
con.execute("UPDATE performance SET score_text='95-3X', score_total=95 WHERE id=?", (pa,)); con.commit()
st = m1["stages"]
st[0].update(score=95, x_count=3, performance_id=pa)
st[3].update(score=188, x_count=6, performance_id=pb)
request.method = "PUT"
m1b = call(ns["update_match"], {"name": "Club match", "date": "2026-09-05", "course_id": nmc["id"],
                                "course_name": "National Match Course", "stages": st}, m1["id"])
check("scores and linked sessions are saved",
      [(s["score"], s["x_count"]) for s in m1b["stages"]] == [(95, 3), (None, None), (None, None), (188, 6)]
      and m1b["stages"][0]["lot_number"] == "M1" and m1b["stages"][0]["score_text"] == "95-3X", m1b["stages"][0])

request.method = "POST"
request.json = {"name": "Other", "stages": [{"name": "SF", "shots": 10, "performance_id": pa}]}
r = ns["create_match"]()
check("a session cannot be in two matches", isinstance(r, tuple) and r[1] == 409, r)
request.json = {"name": "Dup", "stages": [{"name": "A", "shots": 10, "performance_id": pa}, {"name": "B", "shots": 10, "performance_id": pa}]}
check("a session cannot fill two stages", ns["create_match"]()[1] == 400)

request.method = "PUT"
call(ns["update_course"], {"name": "National Match Course", "stages": [{"name": "Only", "distance": 100, "shots": 5}]}, nmc["id"])
check("editing a course leaves old matches alone", len(call(ns["list_matches"])[0]["stages"]) == 4)
request.method = "POST"
request.json = {"name": "National Match Course", "stages": []}
r = ns["create_course"]()
check("duplicate course names are refused", isinstance(r, tuple) and r[1] == 409, r)
request.json = {"name": "Bad", "stages": [{"name": "SF", "shots": 0}]}
check("a course stage needs shots", ns["create_course"]()[1] == 400)

call(ns["delete_performance"], None, pb)
stages_now = call(ns["list_matches"])[0]["stages"]
check("deleting a session unlinks it but keeps the score",
      len([s for s in stages_now if s["performance_id"]]) == 1 and stages_now[3]["score"] == 188 and stages_now[3]["performance_id"] is None)
call(ns["delete_course"], None, nmc["id"])
mm = call(ns["list_matches"])[0]
check("deleting a course keeps the match and its stages", mm["course_id"] is None and mm["course_name"] == "National Match Course" and len(mm["stages"]) == 4)
call(ns["delete_match"], None, m1["id"])
check("deleting a match deletes its stages", con.execute("SELECT COUNT(*) FROM match_stages").fetchone()[0] == 0)

# ── rounds fired per firearm (each firearm entry is a barrel) ──
request.method = "POST"
gid = call(ns["create_firearm"], {"name": "Rounds test AR", "barrel_length": 20, "start_rounds": 500, "life_rounds": 3000})["id"]
other = call(ns["create_firearm"], {"name": "Other rifle"})["id"]
fa = [f for f in call(ns["list_firearms"]) if f["id"] == gid][0]
check("a firearm keeps its starting rounds and expected life", fa["start_rounds"] == 500 and fa["life_rounds"] == 3000, fa)
check("a firearm with nothing fired shows its starting rounds", fa["rounds_total"] == 500, fa["rounds_total"])
s1 = call(ns["create_performance"], {"lot_number": "R1", "firearm_id": gid, "date": "2026-03-01", "num_shots": 20})["id"]
for i in range(30):          # 30 saved ShotMarker shots (some sighters) beat the typed 20
    con.execute("INSERT INTO shotmarker_shots (performance_id, idx, label, score, sighter) VALUES (?,?,?,?,?)", (s1, i, str(i), "10", 1 if i < 2 else 0))
con.commit()
s2 = call(ns["create_performance"], {"lot_number": "R2", "firearm_id": gid, "date": "2026-03-05", "num_shots": 10})["id"]
for i in range(26):          # 26 Garmin shots beat the 10 typed
    con.execute("INSERT INTO chrono_shots (performance_id, idx, speed_fps) VALUES (?,?,?)", (s2, i, 2600))
con.commit()
call(ns["create_performance"], {"lot_number": "R3", "firearm_id": other, "date": "2026-03-06", "num_shots": 99})
call(ns["create_performance"], {"lot_number": "R4", "firearm_id": gid, "date": "2026-03-07", "num_shots": 12})
info = ns["firearm_rounds"](gid)
check("a session counts its largest figure: typed, saved shots with sighters, or Garmin shots",
      info["sessions"] == 30 + 26 + 12, info["sessions"])
check("sessions on another rifle don't count, and the total includes the starting rounds", info["total"] == 500 + 68, info["total"])

mm = call(ns["create_match"], {"name": "Rounds match", "date": "2026-03-10", "firearm_id": gid,
          "stages": [{"name": "A", "shots": 10, "score": 95, "x_count": 2, "performance_id": None},
                     {"name": "B", "shots": 20, "score": 180, "x_count": 3, "performance_id": s1},
                     {"name": "C", "shots": 10, "score": None, "x_count": None, "performance_id": None}]})
info = ns["firearm_rounds"](gid)
check("a typed match stage counts; a linked or unscored one does not", info["matches"] == 10 and info["total"] == 500 + 68 + 10, info)

lg = call(ns["add_round_log"], {"date": "2026-02-01", "rounds": 100, "note": "load development"}, gid)
check("a logged entry adds to the total", lg["logged"] == 100 and lg["total"] == 678 and lg["log"][0]["note"] == "load development", lg)
request.json = {"rounds": 0}
check("a log entry needs at least one round", ns["add_round_log"](gid)[1] == 400)
request.json = {"rounds": 5, "date": "yesterday"}
check("a log entry needs a real date", ns["add_round_log"](gid)[1] == 400)
call(ns["delete_round_log"], None, lg["log"][0]["id"])
check("deleting a log entry takes it off the total", ns["firearm_rounds"](gid)["total"] == 578)

fl = {f["id"]: f for f in call(ns["list_firearms"])}
check("the firearms list carries each rifle's total", fl[gid]["rounds_total"] == 578 and fl[other]["rounds_total"] == 99, (fl[gid]["rounds_total"], fl[other]["rounds_total"]))

request.method = "PUT"
up = call(ns["update_firearm"], {"name": "Rounds test AR", "start_rounds": 0, "life_rounds": 2500}, gid)
check("start rounds and life can be changed", up["life_rounds"] == 2500 and ns["firearm_rounds"](gid)["total"] == 78, ns["firearm_rounds"](gid))
request.json = {"name": "Bad", "life_rounds": -1}
check("expected life can't be negative", ns["update_firearm"](gid)[1] == 400)
request.json = {"name": "Bad", "start_rounds": -5}
check("starting rounds can't be negative", ns["update_firearm"](gid)[1] == 400)
request.json = {"name": "Bad", "start_rounds": "lots"}
check("starting rounds must be a number", ns["update_firearm"](gid)[1] == 400)
check("an unknown rifle is a 404", ns["firearm_rounds"](99999)[1] == 404)

request.method = "POST"
spare = call(ns["create_firearm"], {"name": "Spare upper"})["id"]
call(ns["add_round_log"], {"rounds": 5}, spare)
call(ns["delete_firearm"], None, spare)
check("deleting a rifle clears its round log", con.execute("SELECT COUNT(*) FROM round_log WHERE firearm_id=?", (spare,)).fetchone()[0] == 0)

# ── re-scoring on another target face ─────────────────────────
import json, math, re as _re
FACES = ns["FACES"]
page = open(os.path.join(ROOT, "static", "index.html"), encoding="utf-8").read()
check("the ring tables load from static/faces.js: 58 faces, each named with rings", len(FACES) == 58 and all(f.get("name") and f.get("rings") for f in FACES.values()), len(FACES))
check("the page loads faces.js and carries no copy of its own", '<script src="/static/faces.js"></script>' in page and "const SM_FACES={" not in page)
check("faces.js is the JS the page needs: it defines SM_FACES", open(os.path.join(ROOT, "static", "faces.js"), encoding="utf-8").read().count("const SM_FACES={") == 1)
check("every face lists its rings from the inside out", all([r["r"] for r in f["rings"]] == sorted(r["r"] for r in f["rings"]) for f in FACES.values()))
check("every ring value is X, V or a whole number", all(r["score"] in ("X", "V") or isinstance(r["score"], int) for f in FACES.values() for r in f["rings"]))

rs = ns["ring_score"]
mr1 = FACES["NRA_MR1"]
check("a shot whose bullet edge just touches the X ring is an X", rs(79.04, 0, 5.69, mr1) == "X" and rs(0, 0, 5.69, mr1) == "X")
check("a hair outside that is a 10", rs(79.06, 0, 5.69, mr1) == "10")
check("far outside every ring is a miss", rs(5000, 0, 5.69, mr1) == "0")
check("a shot with no position can't be scored", rs(None, 3, 5.69, mr1) is None)

# The rules below are the device's own (calc_shot_score in ShotMarker software 122d): strict "<" on the ring edge,
# the board's value outside every ring, and nothing off the board.
toy = {"rings": [{"r": 10, "score": "X"}, {"r": 20, "score": 9}], "board": {"w": 100, "h": 100, "score": 1}}
check("a shot exactly touching a ring's outside edge takes the lower value (the device uses <)",
      rs(11.99, 0, 4, toy) == "X" and rs(12.0, 0, 4, toy) == "9" and rs(21.99, 0, 4, toy) == "9" and rs(22.0, 0, 4, toy) == "1")
check("outside every ring but on the board scores the board's value", rs(30, 0, 4, toy) == "1" and rs(0, -45, 4, toy) == "1")
check("beyond the board edge plus the bullet radius it scores nothing", rs(52.0, 0, 4, toy) == "1" and rs(52.01, 0, 4, toy) == "0" and rs(0, 60, 4, toy) == "0")
check("a board worth nothing gives 0", rs(30, 0, 4, {**toy, "board": {"w": 100, "h": 100, "score": 0}}) == "0")
check("with no bullet size the device's 7.82 mm is assumed", rs(13.9, 0, None, toy) == "X" and rs(13.92, 0, None, toy) == "9")
fc600 = FACES["IF600y"]
check("on the F-Class face a hit outside the last ring but on the board scores 1, and off the board 0",
      rs(470, 0, 5.69, fc600) == "1" and rs(905, 0, 5.69, fc600) == "0", (fc600["board"], fc600["rings"][-1]))
check("every face carries its board and the value of its X or V",
      all(f.get("board", {}).get("w") and f["board"].get("h") and f.get("score") for f in FACES.values()))
bad_sp = [fid for fid, f in FACES.items() if len(f["score"]) > 1
          and ns["compute_score"]([{"score": f["score"][1], "sighter": 0}], fid)[0] != f["score"][0]]
check("what an X or V counts agrees with the device's definition on all 58 faces", not bad_sp, bad_sp)
check("an X counts 6 on the ICFRA 'IFX' faces and 10 on the NRA F-Class faces; a V counts 5",
      ns["compute_score"]([{"score": "X"}], "IFX600y")[0] == 6 and ns["compute_score"]([{"score": "X"}], "NRA_MR1FC")[0] == 10
      and ns["compute_score"]([{"score": "V"}], "IF600y")[0] == 5)
check("the faces the device scores with polygons are marked so they are never re-scored",
      sorted(fid for fid, f in FACES.items() if f.get("rescore") is False) == ["FCSA_MR1", "IBS1000BR", "IBS600BR"])
check("the position can be on either axis", rs(0, -79.04, 5.69, mr1) == "X" and rs(55.88, 55.88, 5.69, mr1) == "X" and rs(55.95, 55.95, 5.69, mr1) == "10")

mism = []
for sess in sessions:                       # the three real strings decoded from the ShotMarker export
    face_def = FACES[sess["face_id"]]
    for sh in sess["shots"]:
        if rs(sh["x_mm"], sh["y_mm"], sess["bullet_d_mm"], face_def) != str(sh["score"]):
            mism.append((sess["session_id"], sh["label"]))
check("re-scoring the real shots on their own face reproduces ShotMarker's scores exactly", not mism and sum(len(x["shots"]) for x in sessions) == 44, mism)

realC = [x for x in sessions if len(x["shots"]) == 22][0]
rp = call(ns["create_performance"], {"lot_number": "RS1", "distance": 600, "distance_unit": "yd", "group_unit": "MOA"})["id"]
request.method = "POST"
call(ns["save_perf_shots"], {"shots": realC["shots"], "face_id": "NRA_MR1", "distance": 600, "dist_unit": "y",
                             "bullet_d_mm": realC["bullet_d_mm"]}, rp)
r0 = row(rp)
check("an import records the face it was scored on and has no re-score", r0["orig_face"] == "NRA_MR1" and r0["rescore_text"] is None and r0["score_text"] == "191-5X", r0)

request.method = "PUT"
up = call(ns["update_performance"], {"lot_number": "RS1", "distance": 600, "distance_unit": "yd", "group_unit": "MOA", "face_id": "NRA_MR1FC"}, rp)
r1 = row(rp)
check("changing to a smaller face re-scores the shots and keeps the device score",
      r1["score_text"] == "191-5X" and r1["rescore_face"] == "NRA_MR1FC" and r1["rescore_total"] is not None and r1["rescore_total"] < 191, r1)
dev_scores = [x["score"] for x in realC["shots"]]
kept = [r["score"] for r in con.execute("SELECT score FROM shotmarker_shots WHERE performance_id=? ORDER BY idx", (rp,))]
check("each shot keeps ShotMarker's own score", kept == dev_scores)
rescored = [r["rescore"] for r in con.execute("SELECT rescore FROM shotmarker_shots WHERE performance_id=? ORDER BY idx", (rp,))]
check("each shot gets a re-score, and the smaller face is never kinder", all(x is not None for x in rescored)
      and sum(1 for a, b in zip(rescored, dev_scores) if (10 if a == "X" else int(a)) > (10 if b == "X" else int(b))) == 0)
check("the re-score keeps sighters out of the total", r1["rescore_text"] == ns["compute_score"](
      [{"score": a, "sighter": s["sighter"]} for a, s in zip(rescored, realC["shots"])], "NRA_MR1FC")[1], r1["rescore_text"])
check("the list carries the re-score", [x for x in call(ns["list_performance"]) if x["id"] == rp][0]["rescore_text"] == r1["rescore_text"])

call(ns["update_performance"], {"lot_number": "RS1", "distance": 600, "distance_unit": "yd", "group_unit": "MOA", "face_id": "NRA_MR1"}, rp)
r2 = row(rp)
check("changing back to the face it was imported on removes the re-score",
      r2["rescore_text"] is None and r2["rescore_total"] is None and r2["rescore_face"] is None
      and con.execute("SELECT COUNT(*) FROM shotmarker_shots WHERE performance_id=? AND rescore IS NOT NULL", (rp,)).fetchone()[0] == 0)

call(ns["update_performance"], {"lot_number": "RS1", "distance": 300, "distance_unit": "yd", "group_unit": "MOA"}, rp)
check("a distance change alone does not re-score", row(rp)["rescore_text"] is None)
call(ns["update_performance"], {"lot_number": "RS1", "distance": 300, "distance_unit": "yd", "group_unit": "MOA", "face_id": "NRA_MR1FC"}, rp)
check("a face change on a session at another distance re-scores", row(rp)["rescore_text"] is not None)
call(ns["save_perf_shots"], {"shots": realC["shots"], "face_id": "NRA_MR1", "distance": 600, "dist_unit": "y",
                             "bullet_d_mm": realC["bullet_d_mm"]}, rp)
check("importing again starts clean", row(rp)["rescore_text"] is None and row(rp)["orig_face"] == "NRA_MR1")
call(ns["update_performance"], {"lot_number": "RS1", "distance": 600, "distance_unit": "yd", "group_unit": "MOA", "face_id": "NOT_A_FACE"}, rp)
check("an unknown face is not scored", row(rp)["rescore_text"] is None)
call(ns["update_performance"], {"lot_number": "RS1", "distance": 600, "distance_unit": "yd", "group_unit": "MOA", "face_id": "NRA_MR1FC"}, rp)
call(ns["delete_perf_shots"], None, rp)
check("deleting the shots clears the re-score too", row(rp)["rescore_text"] is None and row(rp)["orig_face"] is None)

# Sessions already switched off before this feature get re-scored once on their current face
op = call(ns["create_performance"], {"lot_number": "RS2", "distance": 600, "distance_unit": "yd"})["id"]
request.method = "POST"
call(ns["save_perf_shots"], {"shots": realC["shots"], "face_id": "NRA_MR1FC", "distance": 600, "dist_unit": "y",
                             "bullet_d_mm": realC["bullet_d_mm"]}, op)
con.execute("UPDATE performance SET use_score=0, orig_face=NULL, rescore_text=NULL, rescore_total=NULL, rescore_face=NULL WHERE id=?", (op,))
con.commit()
call(ns["init_db"])
ro = row(op)
check("an older session with its score switched off is re-scored on its current face", ro["orig_face"] == "?" and ro["rescore_text"] is not None and ro["rescore_face"] == "NRA_MR1FC", ro)
ok = call(ns["create_performance"], {"lot_number": "RS3", "distance": 600, "distance_unit": "yd"})["id"]
call(ns["save_perf_shots"], {"shots": realC["shots"], "face_id": "NRA_MR1", "distance": 600, "dist_unit": "y",
                             "bullet_d_mm": realC["bullet_d_mm"]}, ok)
con.execute("UPDATE performance SET orig_face=NULL WHERE id=?", (ok,)); con.commit()
call(ns["init_db"])
check("an older session with its score still on keeps its face as the original and no re-score", row(ok)["orig_face"] == "NRA_MR1" and row(ok)["rescore_text"] is None)

shutil.rmtree(TMP, ignore_errors=True)
print("\n%d/%d passed" % (sum(results), len(results)))
sys.exit(0 if all(results) else 1)
