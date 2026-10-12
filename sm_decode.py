"""
ShotMarker e-target backup decoder.
Faithful port of decode_shot() from ShotMarker web app (build 122d).
"""
import json, zlib, tarfile, tempfile, os, glob, math

FPS   = 3.28084
INCH  = 0.03937
MIL   = 0.29089

def sign(e): return -1 if e < 0 else 1

def decode64(s):
    o = 0
    for ch in reversed(s): o = (o << 6) + (ord(ch) - 35)
    return o

def p(e, scale, exp, mid):
    return (abs(e - mid) / mid) ** exp * scale * sign(e - mid)

ERRORS = {
    1:"insufficient sensor timings",2:"invalid sensor timings",3:"no valid solution",
    4:"off target right",5:"off target left",6:"off target high",7:"off target low",
    8:"off target high right",9:"off target high left",10:"off target low right",
    11:"off target low left",12:"unrealistic low velocity",13:"unrealistic high velocity",
    14:"unrealistic incoming angle",15:"unrealistic measurement quality",
    16:"measured off target left",17:"measured off target right",
    18:"measured off target bottom",19:"measured off target top",
}
WARNINGS = {
    32:"measured off target left",33:"measured off target right",
    34:"measured off target bottom",35:"measured off target top",
    36:"quality",37:"angle",38:"velocity"
}

def decode_shot(t):
    o = [0]
    def i(n): o[0]+=n; return t[o[0]-n:o[0]]
    def nn(): o[0]+=1; return ord(t[o[0]-1])-35
    r = {}
    r['ts'] = 16777216 * decode64(i(3)) + decode64(i(4))
    s = nn()
    if 1&s: r['simulated']=True
    if 2&s: r['hide']=True
    if 4&s: r['sighter']=True
    if 8&s: r['off']=True
    s = nn()
    r['multi_assign']=7&s
    if 8&s:  r['fake']=True
    if 16&s: r['has_score_override']=True
    if r.get('has_score_override'):
        raw=i(1); val=decode64(raw)
        r['score_override']=val if 0<=val<=10 else raw
    if not r.get('fake'):
        r['temp']=nn()-20
        a=decode64(i(2))
        r['t']=[]
        for l in range(8):
            c=3 if (a>>(10-l)&1) else 2
            tv=decode64(i(c))
            if c==2 and tv==4095: tv=-1
            r['t'].append(tv)
        vc=7&a
        if vc==7:    r['v_count']=4
        elif vc<=2:  r['v_count']=vc
        else:        r['v_count']=3; r['v_missing']=vc-3
        d=nn()
        if d in ERRORS:   r['error']=ERRORS[d]
        elif d in WARNINGS: r['warning']=WARNINGS[d]
        elif d>0 and d<32:  r['error']='unrecognized error'
        elif d>=32:         r['warning']='unrecognized warning'
        if not r.get('error'):
            r['x']=p(decode64(i(2)),4000,1.6,2047)
            r['y']=p(decode64(i(2)),4000,1.6,2047)
            r['v']=(decode64(i(2))+1000)/FPS
            s2=decode64(i(3)); e2=511&s2
            r['theta']=p(s2>>9,0.4,1.3,255)
            r['phi']=p(e2,0.4,1.3,255)
            s3=nn(); r['err_v']=100*(s3/63)**1.5
            tail=i(2)
            if tail: r['err_xy']=200*(decode64(tail)/4095)**1.5
    return r

def decode_tar(tar_path):
    """Decode a ShotMarker .tar export. Returns list of session dicts."""
    tmp = tempfile.mkdtemp()
    with tarfile.open(tar_path) as tf:
        tf.extractall(tmp)
    arch_path = os.path.join(tmp, 'archive.txt')
    archive = json.load(open(arch_path)) if os.path.exists(arch_path) else {}
    sessions = []
    for zf in sorted(glob.glob(os.path.join(tmp, 'string-*.z'))):
        sid = os.path.basename(zf)[len('string-'):-2]
        sj  = json.loads(zlib.decompress(open(zf,'rb').read()).decode('utf-8'))
        meta = archive.get(sid, {})
        dist     = sj.get('dist', 0)
        dist_unit= sj.get('dist_unit','y')
        cal_x    = sj.get('cal_x', 0)
        cal_y    = sj.get('cal_y', 0)
        face_id  = sj.get('face_id','')
        bullet   = sj.get('bullet', 7.82)
        # MOA scale
        m = 0.29088822 * dist * (0.9144 if dist_unit=='y' else 1)
        # parse score_string
        score_entries = []
        if sj.get('score_string'):
            score_entries = [e for e in sj['score_string'].split(',') if e]
        encoded = sj.get('encoded', False)
        shots_out = []
        for idx, sh in enumerate(sj.get('shots',[])):
            if encoded and isinstance(sh, str):
                d = decode_shot(sh)
            else:
                d = dict(sh)
            if d.get('error'): continue
            label, score = '', ''
            if idx < len(score_entries) and ':' in score_entries[idx]:
                label, score = score_entries[idx].split(':',1)
            x_mm = round(d['x'] + cal_x, 1) if 'x' in d else None
            y_mm = round(d['y'] + cal_y, 1) if 'y' in d else None
            v_fps = round(d['v'] * FPS, 1) if 'v' in d else None
            shot = {
                'idx':       idx,
                'label':     label,
                'score':     score,
                'sighter':   bool(d.get('sighter')),
                'x_mm':      x_mm,
                'y_mm':      y_mm,
                'x_moa':     round(x_mm/m, 3) if x_mm is not None and m else None,
                'y_moa':     round(y_mm/m, 3) if y_mm is not None and m else None,
                'v_fps':     v_fps,
                'temp_c':    d.get('temp'),
                'ts':        d.get('ts'),
            }
            shots_out.append(shot)
        sessions.append({
            'session_id':  sid,
            'name':        sj.get('name',''),
            'target':      meta.get('target_name',''),
            'face_id':     face_id,
            'distance':    dist,
            'dist_unit':   dist_unit,
            'bullet_d_mm': bullet,
            'cal_x':       cal_x,
            'cal_y':       cal_y,
            'group_text':  meta.get('group_text',''),
            'shots':       shots_out,
        })
    return sessions

def compute_stats(shots):
    """Compute velocity stats and group measurements from a list of shot dicts.

    Returns:
      num_shots         - count of non-sighter shots
      avg_velocity      - mean fps
      std_dev           - velocity SD (fps)
      extreme_spread    - velocity ES (fps)
      group_size_mm     - max centre-to-centre distance between any two shots (mm)
      group_x_mm        - horizontal spread: max_x - min_x (mm)
      group_y_mm        - vertical spread:   max_y - min_y (mm)
    """
    rec = [s for s in shots if not s.get('sighter') and s.get('v_fps')]
    n = len(rec)
    if n == 0:
        return {
            'num_shots': 0, 'avg_velocity': None, 'std_dev': None,
            'extreme_spread': None, 'group_size_mm': None,
            'group_x_mm': None, 'group_y_mm': None,
        }

    # Velocity stats
    vs  = [s['v_fps'] for s in rec]
    avg = sum(vs) / n
    sd  = math.sqrt(sum((v - avg) ** 2 for v in vs) / n) if n > 1 else 0
    es_vel = round(max(vs) - min(vs), 1) if n > 1 else 0

    # Group measurements (mm) — only for shots with valid coordinates
    coord_shots = [s for s in rec if s.get('x_mm') is not None and s.get('y_mm') is not None]
    group_size_mm = None
    group_x_mm    = None
    group_y_mm    = None
    if len(coord_shots) > 1:
        xs = [s['x_mm'] for s in coord_shots]
        ys = [s['y_mm'] for s in coord_shots]
        group_x_mm = round(max(xs) - min(xs), 2)
        group_y_mm = round(max(ys) - min(ys), 2)
        # Max centre-to-centre distance between any two shots
        max_dist = 0.0
        for i in range(len(coord_shots)):
            for j in range(i + 1, len(coord_shots)):
                dx = coord_shots[i]['x_mm'] - coord_shots[j]['x_mm']
                dy = coord_shots[i]['y_mm'] - coord_shots[j]['y_mm']
                max_dist = max(max_dist, math.sqrt(dx * dx + dy * dy))
        group_size_mm = round(max_dist, 2)

    return {
        'num_shots':      n,
        'avg_velocity':   round(avg, 1),
        'std_dev':        round(sd, 1),
        'extreme_spread': es_vel,
        'group_size_mm':  group_size_mm,
        'group_x_mm':     group_x_mm,
        'group_y_mm':     group_y_mm,
    }


def mm_to_angular(mm, distance_yd, unit='MOA'):
    """Convert a group size in mm to MOA or Mil given distance in yards."""
    if mm is None or not distance_yd:
        return None
    distance_mm = float(distance_yd) * 914.4  # yards → mm
    rad = math.atan2(mm, distance_mm)          # radians
    if unit == 'Mil':
        return round(rad * 1000, 3)             # radians → milliradians
    return round(rad * (180 / math.pi) * 60, 3) # radians → MOA
