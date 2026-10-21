// Frontend tests: checks the page script compiles, then runs its pure functions on real data.
// Needs only Node (no packages):   node tests/frontend.test.js
//
// Functions are lifted out of static/index.html by name, so the page itself is never executed.

// Real data from the 2026-08-15 range session ("C" string, 600 yd, 22 shots) and its Garmin file.
const GARMIN_TIMES = ['10:32:44','10:33:36','10:35:22','10:36:00','10:36:32','10:37:06','10:38:12','10:38:44','10:39:24','10:40:17','10:40:56','10:41:26','10:41:55','10:42:29','10:43:11','10:43:46','10:44:14','10:44:50','10:45:14','10:45:42','10:46:24','10:47:08','10:58:29','10:59:20','10:59:42','11:00:17'];
const GARMIN_FPS = [2623.3,2613.5,2599.6,2594.2,2586.3,2601.7,2613.1,2612.0,2629.1,2640.4,2591.0,2631.1,2599.4,2589.9,2603.4,2621.7,2599.3,2615.6,2636.7,2616.1,2612.1,2596.5,2622.0,2608.4,2600.2,2601.3];
const SM_TS = [1786803860075,1786803912425,1786804018185,1786804055598,1786804087939,1786804121741,1786804187525,1786804219850,1786804260121,1786804313115,1786804352347,1786804382102,1786804410449,1786804444644,1786804487004,1786804521867,1786804549151,1786804585986,1786804610219,1786804637970,1786804679816,1786804723694];
const SM_FPS = [1421,1405,1390,1380,1377,1388,1402,1405,1410,1425,1402,1422,1405,1326,1398,1422,1409,1364,1380,1406,1389,1391];
// Scores exactly as ShotMarker shows them (S1, S2 are sighters), total 191-5X
const SM_SCORES = ['7','7','9','X','8','X','10','10','X','10','10','X','10','8','10','10','10','X','10','9','8','9'];

function runTests(scripts) {
  const log = [];
  let passed = 0, failed = 0;
  const check = (name, cond, extra) => {
    if (cond) passed++; else failed++;
    log.push((cond ? 'PASS ' : 'FAIL ') + name + (!cond && extra !== undefined ? '  -> ' + JSON.stringify(extra) : ''));
  };

  // 1. Every inline script in the page has to compile (the page has more than one)
  const list = [].concat(scripts);
  list.forEach((s, i) => {
    try { new Function(s); check('page script ' + (i + 1) + ' of ' + list.length + ' compiles', true); }
    catch (e) { check('page script ' + (i + 1) + ' of ' + list.length + ' compiles', false, String(e)); }
  });
  if (failed) return { log, passed, failed, total: passed + failed };
  const js = list.join('\n');

  // Pull a function out of the source by name (matching its braces)
  function grab(name) {
    const m = new RegExp('(?:async\\s+)?function\\s+' + name + '\\s*\\(').exec(js);
    if (!m) throw new Error('function not found in page: ' + name);
    let i = m.index + m[0].length, depth = 1;
    while (depth && i < js.length) { const ch = js[i++]; if (ch === '(') depth++; else if (ch === ')') depth--; }
    i = js.indexOf('{', i); depth = 0;
    for (; i < js.length; i++) {
      if (js[i] === '{') depth++;
      else if (js[i] === '}') { depth--; if (!depth) { i++; break; } }
    }
    return js.slice(m.index, i);
  }
  function load(names) {
    const body = 'let pfSmSelected=null, pfChrono=null;\n' +
      'const SM_FACES={NRA_MR1:{score:[10,"X"]},IFX600y:{score:[6,"X"]},IF600y:{score:[5,"V"]},ISSFC200:{score:[10]},TRD:{score:[1]},' +
      'TRD_OLD:{score:[5,"V"]}};\nconst TIMING_TIP="tip";\n' + names.map(grab).join('\n') +
      '\nreturn {' + names.join(',') + ', set(sm,ch){pfSmSelected=sm;pfChrono=ch;}};';
    return new Function(body)();
  }
  const f = load(['scoreOf', 'parseTwist', 'hmsToSec', 'mmToAngular', 'shotShort', 'shotName', 'pearson',
                  'pfPlanFromOffset', 'pfChronoBestOffset', 'pfChronoTimeMatch', 'pfChronoRecommend', 'pfTimingDisagrees', 'escHtml', 'chronoStats', 'importScore',
                   'groupCenter', 'centerText', 'stringTiming', 'fmtSec', 'timingVerdict', 'timingSpans', 'timingNote']);

  // Group center: where the middle of the ticked shots sits against the point of aim
  const gc = f.groupCenter([{ x_mm: 10, y_mm: -20 }, { x_mm: 30, y_mm: -40 }, { x_mm: null, y_mm: 5 }]);
  check('group center is the average position of the shots that have one', gc && gc.x === 20 && gc.y === -30 && gc.n === 2, gc);
  check('one shot (or none) has no group center', f.groupCenter([{ x_mm: 1, y_mm: 1 }]) === null && f.groupCenter([]) === null);
  const ct = f.centerText({ x: 20, y: -30 }, 600, 'MOA');
  check('center text says how far low and right in MOA', /^\d\.\d\d MOA low · \d\.\d\d MOA right$/.test(ct), ct);
  check('center text reads high and left for the other signs, and in mil', /high/.test(f.centerText({ x: -20, y: 30 }, 600, 'MOA')) && /left/.test(f.centerText({ x: -20, y: 30 }, 600, 'MOA'))
        && /mil low/.test(f.centerText({ x: 20, y: -30 }, 600, 'mil')));
  check('a center on the aiming point reads on center, and no distance gives nothing', f.centerText({ x: 0.01, y: -0.01 }, 600, 'MOA') === 'on center' && f.centerText({ x: 20, y: 20 }, 0, 'MOA') === '' && f.centerText(null, 600, 'MOA') === '');
  const bigX = Number(/(\d\.\d\d) MOA right/.exec(f.centerText({ x: 100, y: -30 }, 100, 'MOA'))[1]);
  check('center in MOA matches the angular conversion (100 mm at 100 yd is about 3.76 MOA)', Math.abs(bigX - 3.76) < 0.01, bigX);

  // String timing runs first shot to last shot: the start command is not recorded
  const tmg = f.stringTiming([{ ts: 1010000 }, { ts: 1000000, sighter: true }, { ts: 1004000 }, { ts: 1000500 }]);
  check('string timing is first to last record shot, sighters left out, in any order',
        tmg && tmg.n === 3 && tmg.seconds === 9.5 && tmg.gaps.join() === '3.5,6' && tmg.avg === 4.75 && tmg.min === 3.5 && tmg.max === 6, tmg);
  check('timing needs two shots with times', f.stringTiming([{ ts: 5 }]) === null && f.stringTiming([{ ts: null }, { ts: undefined }]) === null && f.stringTiming([]) === null);
  const real = f.stringTiming(SM_TS.map((ts, i) => ({ ts, sighter: i < 2 })));
  check('the real 20-shot string took about 705 s first to last (a slow-fire string)', real && real.n === 20 && Math.abs(real.seconds - 705.509) < 0.001, real && real.seconds);
  check('times read as seconds, or minutes and seconds from two minutes', f.fmtSec(54.24) === '54.2 s' && f.fmtSec(125) === '2 m 05 s' && f.fmtSec(119.9) === '119.9 s');
  const v2 = f.timingVerdict(63.1, 60);
  check('only going over the limit is stated; being under it proves nothing',
        f.timingVerdict(54.2, 60) === null && v2.over && Math.abs(v2.by - 3.1) < 1e-9 && f.timingVerdict(60, 60) === null && f.timingVerdict(50, null) === null);
  check('a stage line states first to last and the limit, never "within the limit"',
        f.timingNote(54.2, 60) === '54.2 s first to last shot · limit 60 s' && f.timingNote(63.1, 60) === '63.1 s first to last shot · over the 60 s limit'
        && f.timingNote(54.2, null) === '54.2 s first to last shot' && f.timingNote(null, 60) === 'Limit 60 s' && f.timingNote(null, null) === '');
  const t10 = f.stringTiming([{ ts: 0 }, { ts: 54200 }]), tOver = f.stringTiming([{ ts: 0 }, { ts: 63100 }]);
  const sp = f.timingSpans(t10, 60, 2), spOver = f.timingSpans(tOver, 60, 2), spShort = f.timingSpans(t10, 60, 10);
  check('the timing row shows the plain limit under it, flags going over, and flags a short string',
        /First to last shot/.test(sp) && /Stage limit <b>60 s<\/b>/.test(sp) && !/had to come|Over the|recorded/.test(sp)
        && /Over the 60 s limit by 3\.1 s before counting/.test(spOver) && /2 of 10 shots recorded/.test(spShort) && !/recorded/.test(f.timingSpans(t10, 60)));
  check('no limit means no limit text in the timing row', !/Limit|Over the/.test(f.timingSpans(f.stringTiming([{ ts: 0 }, { ts: 54200 }]), null)) && f.timingSpans(null, 60) === '');

  // Import screens: the device's own score next to a total for the ticked shots, sighters never counted
  const imp = { face_id: 'NRA_MR1', group_text: '191-5X', shots: SM_SCORES.map((s, i) => ({ score: s, sighter: i < 2 })) };
  const impAll = f.importScore(imp, imp.shots);
  check('import score: SM score and the checked total agree when everything is ticked', impAll.all === '191-5X' && impAll.checked === '191-5X', impAll);
  const impTicked = f.importScore(imp, imp.shots.slice(2, 12));
  check('import score: the checked total follows the ticked shots', impTicked.all === '191-5X' && impTicked.checked === '97-4X', impTicked);
  const impSighters = f.importScore(imp, imp.shots.slice(0, 2));
  check('import score: sighters ticked on their own score nothing', impSighters.all === '191-5X' && impSighters.checked === null, impSighters);
  check('import score: ticking the sighters too does not change the total', f.importScore(imp, imp.shots).checked === f.importScore(imp, imp.shots.slice(2)).checked);
  check('import score: without the device text it is worked out from the shots', f.importScore({ ...imp, group_text: '' }, imp.shots).all === '191-5X');
  check('import score: nothing to show without scores or a session', f.importScore({ face_id: 'NRA_MR1', shots: [{ score: '' }] }, []) === null && f.importScore(null, []) === null);

  // Garmin-only viewer maths: population SD like the Garmin's own summary
  const cs = f.chronoStats([2, 4, 4, 4, 5, 5, 7, 9]);
  check('chronograph stats: avg 5, population SD 2, ES 7', cs.avg === 5 && cs.sd === 2 && cs.es === 7, cs);
  const cs22 = f.chronoStats(GARMIN_FPS.slice(0, 22));
  check('chronograph stats on the real string are sane', cs22.avg > 2600 && cs22.avg < 2620 && cs22.sd > 10 && cs22.sd < 20, cs22);

  // Escaping: text from the database must never reach innerHTML raw
  check('escHtml neutralises tags, quotes and ampersands',
        f.escHtml('<b onclick="x()">Tom\'s & co</b>') === '&lt;b onclick=&quot;x()&quot;&gt;Tom&#39;s &amp; co&lt;/b&gt;', f.escHtml('<b>'));
  // (table and c.key are fixed names from the page itself, not database text)
  const handlerRe = /on(click|change)="[^"]*'\$\{(?!table\}|c\.key\})[^}]*\}/;
  check('no inline handler splices data inside quotes (use data-* instead)', !handlerRe.test(js), (handlerRe.exec(js) || [])[0]);
  // snack() and textContent assignments are safe, so only template strings that become HTML are checked
  const rawRe = /\$\{(item|r|f)\.(name|brand|notes|lot_number|manufacturer|firearm_name)\}/;
  const rawLine = js.split('\n').find((l) => rawRe.test(l) && !/textContent/.test(l));
  check('names, brands and lots are escaped where cards and tables render them', !rawLine, rawLine && rawLine.trim());

  // 2. Scoring: same rules as the server
  const shots = SM_SCORES.map((s, i) => ({ score: s, sighter: i < 2 }));
  check('score of the real string is 191-5X', f.scoreOf(shots, 'NRA_MR1') === '191-5X', f.scoreOf(shots, 'NRA_MR1'));
  check('sighters never count', f.scoreOf([{ score: '10', sighter: true }, { score: '9', sighter: false }], 'NRA_MR1') === '9-0X');
  check('X is worth 6 on F-Class X faces', f.scoreOf([{ score: 'X' }, { score: '5' }], 'IFX600y') === '11-1X');
  check('V counts 5 and is labelled V', f.scoreOf([{ score: 'V' }, { score: '4' }], 'IF600y') === '9-1V');
  check('no scores gives null', f.scoreOf([{ score: '' }], 'NRA_MR1') === null);
  check('a face with no X or V shows just the total (Target Rifle D/F, ISSF C200)',
        f.scoreOf([{ score: '1' }, { score: '1' }, { score: '0' }], 'TRD') === '2' && f.scoreOf([{ score: '10' }, { score: '9' }], 'ISSFC200') === '19');
  check('faces with an X or V keep the 191-5X style', f.scoreOf([{ score: 'X' }, { score: '9' }], 'NRA_MR1') === '19-1X' && f.scoreOf([{ score: 'V' }, { score: '4' }], 'TRD_OLD') === '9-1V');

  // 3. Twist, time and angle helpers
  const tw = (s) => f.parseTwist(s);
  check('twist: 1:10 -> 10', tw('1:10') === '10');
  check('twist: 1-8 -> 8', tw('1-8') === '8');
  check('twist: "1 in 10" -> 10', tw('1 in 10') === '10');
  check('twist: 1:7.5 -> 7.5', tw('1:7.5') === '7.5');
  check('twist: text without a number -> empty', tw('fast') === '' && tw('') === '' && tw(null) === '');
  check('hh:mm:ss to seconds', f.hmsToSec('10:32:44') === 37964 && f.hmsToSec('nope') === null);
  const mil = f.mmToAngular(100, 100, 'mil'), moa = f.mmToAngular(100, 100, 'MOA');
  check('angle unit "mil" (lowercase) really gives mil', Math.abs(mil - 1.094) < 0.002 && Math.abs(f.mmToAngular(100, 100, 'Mil') - mil) < 1e-9, mil);
  check('angle unit MOA', Math.abs(moa - 3.76) < 0.01, moa);
  check('sighters keep the S1 shorthand and "Sighter 1" long form',
    f.shotShort({ label: 'S1', sighter: true }, 0) === 'S1' && f.shotName({ label: 'S1', sighter: true }, 0) === 'Sighter 1');
  check('record shots read "Shot 3"', f.shotName({ label: '3' }, 2) === 'Shot 3' && f.shotShort({ label: '3' }, 2) === '3');
  check('an unlabelled sighter still gets an S', f.shotShort({ sighter: true }, 0) === 'S1');

  // 4. Aligning a chronograph file with a ShotMarker string, on the real session
  const garmin = (n) => ({ shots: GARMIN_TIMES.slice(0, n).map((t, i) => ({ idx: i + 1, time: t, speed_fps: GARMIN_FPS[i], sm_idx: null, unpaired: false })) });
  const sm = (pick) => ({ shots: pick.map((k, j) => ({ idx: j, label: String(j), v_fps: SM_FPS[k], ts: SM_TS[k] })) });
  const all22 = Array.from({ length: 22 }, (_, k) => k);

  f.set(sm(all22), garmin(26));
  let tm = f.pfChronoTimeMatch();
  check('timing: all 22 ShotMarker shots matched', tm && tm.matched === 22, tm && tm.matched);
  check('timing: the 4 shots after the string are left out', tm && tm.outside === 4 && tm.insideUnmatched === 0 && [...tm.plan.outside].join() === '22,23,24,25', tm && [...tm.plan.outside]);
  check('timing: judged strong', tm && tm.strong === true);
  check('timing pairs shot i with shot i', tm && tm.plan.map.slice(0, 22).every((v, i) => v === i));

  const missing = all22.filter((k) => k !== 8 && k !== 15);               // two shots ShotMarker dropped
  f.set(sm(missing), garmin(26));
  tm = f.pfChronoTimeMatch();
  check('timing copes with two shots missing mid-string', tm && tm.matched === 20 && tm.insideUnmatched === 2 && tm.outside === 4 && tm.strong, tm && [tm.matched, tm.insideUnmatched, tm.outside]);

  // Equal counts: shot order is trusted unless timing is convincing and disagrees
  f.set(sm(all22), garmin(22));
  check('equal counts, same order: timing agrees, no warning', !f.pfTimingDisagrees(f.pfChronoTimeMatch()));
  const shifted = { shots: GARMIN_TIMES.slice(4, 26).map((t, i) => ({ idx: i + 1, time: t, speed_fps: GARMIN_FPS[i + 4], sm_idx: null, unpaired: false })) };
  f.set(sm(all22), shifted);
  tm = f.pfChronoTimeMatch();
  check('equal counts, but the Garmin string is shifted by 4: timing disagrees', f.pfTimingDisagrees(tm), tm && [tm.matched, tm.strong]);
  f.set({ shots: sm(all22).shots.map((s) => ({ ...s, ts: null })) }, garmin(22));
  check('equal counts without timestamps: no warning', !f.pfTimingDisagrees(f.pfChronoTimeMatch()));
  check('no timing result: no warning', !f.pfTimingDisagrees(null));

  f.set({ shots: sm(all22).shots.map((s) => ({ ...s, ts: null })) }, garmin(26));
  check('no ShotMarker timestamps: no timing result', f.pfChronoTimeMatch() === null);

  f.set(sm(all22), garmin(26));
  const vel = f.pfChronoBestOffset();
  check('velocity pattern also finds offset 0 on the real data', vel && vel.offset === 0 && Math.abs(vel.r - 0.52) < 0.03, vel);
  const rec = f.pfChronoRecommend(vel, f.pfChronoTimeMatch());
  check('timing is recommended when it is convincing', rec && rec.source === 'timing' && !rec.weak, rec && rec.source);

  const plan = f.pfPlanFromOffset(3);                                   // 26 chronograph shots, 22 ShotMarker shots
  check('start/end plan: shifted pairing and outside shots', plan.map[3] === 0 && plan.map[24] === 21 && plan.map[25] == null && [...plan.outside].join() === '0,1,2,25', [plan.map[3], plan.map[24], [...plan.outside]]);

  // 5. Matches: totals, session suggestions, automatic linking, personal bests
  const mt = load(['parseXCount', 'matchTotals', 'dayNum', 'sameDist', 'sessionSuggestions', 'autoAssignSessions', 'stageKey', 'stageRank', 'fmtScore', 'computeRecords', 'lifeStatus', 'fmtN',
                   'sessionMatchScore', 'sessionScoreText', 'shotScoreLabel']);
  // Re-scored sessions: ShotMarker's score stays, the re-score is shown beside it and used for matches
  const dev = { score_total: 191, score_text: '191-5X', use_score: 1 };
  check('a match stage takes ShotMarker\'s score when there is no re-score', JSON.stringify(mt.sessionMatchScore(dev)) === '{"score":191,"x":5}');
  check('a match stage takes the re-score when the face was changed',
        JSON.stringify(mt.sessionMatchScore({ ...dev, rescore_total: 188, rescore_text: '188-4X' })) === '{"score":188,"x":4}');
  check('the re-score is used even when ShotMarker\'s score was switched off',
        mt.sessionMatchScore({ ...dev, use_score: 0, rescore_total: 188, rescore_text: '188-4X' }).score === 188);
  check('a switched-off score with no re-score gives nothing to copy', mt.sessionMatchScore({ ...dev, use_score: 0 }) === null && mt.sessionMatchScore({}) === null);
  check('session pickers mark a re-score only when it differs',
        mt.sessionScoreText({ ...dev, rescore_text: '188-4X' }) === '188-4X (re-scored)' && mt.sessionScoreText({ ...dev, rescore_text: '191-5X' }) === '191-5X'
        && mt.sessionScoreText(dev) === '191-5X' && mt.sessionScoreText({}) === '');
  check('a shot shows ShotMarker\'s score, then the re-score after an arrow when it differs',
        mt.shotScoreLabel('10', '9', true) === '10 → 9' && mt.shotScoreLabel('X', '10', true) === 'X → 10'
        && mt.shotScoreLabel('10', '10', true) === '10' && mt.shotScoreLabel('10', null, true) === '10' && mt.shotScoreLabel('10', '', true) === '10');
  check('with the score switched off a shot shows only its re-score',
        mt.shotScoreLabel('10', '9', false) === '9' && mt.shotScoreLabel('10', null, false) === '' && mt.shotScoreLabel('', null, true) === '');
  check('barrel life: under 80% is fine, 80% warns, 100% and over is an error',
        mt.lifeStatus(1000, 3000).level === '' && mt.lifeStatus(2400, 3000).level === 'warn' && mt.lifeStatus(2999, 3000).level === 'warn'
        && mt.lifeStatus(3000, 3000).level === 'err' && mt.lifeStatus(3500, 3000).level === 'err');
  check('barrel life: the percentage used', Math.abs(mt.lifeStatus(750, 3000).pct - 25) < 1e-9);
  check('barrel life: with no expected life there is nothing to warn about', mt.lifeStatus(9999, null).pct === null && mt.lifeStatus(9999, 0).level === '');
  check('round counts get thousands separators', mt.fmtN(1234) === '1,234' && mt.fmtN(0) === '0' && mt.fmtN(null) === '0');
  check('scores read like ShotMarker: 95 points and 3 X is 95-3X', mt.fmtScore(95, 3) === '95-3X' && mt.fmtScore(194, 12) === '194-12X');
  check('a missing or empty X count reads 0X', mt.fmtScore(88, null) === '88-0X' && mt.fmtScore(88, '') === '88-0X' && mt.fmtScore(88, 0) === '88-0X');
  check('X count read from a ShotMarker score', mt.parseXCount('191-5X') === 5 && mt.parseXCount('37-1V') === 1 && mt.parseXCount('95-0X') === 0);
  check('X count: anything else gives null', mt.parseXCount('') === null && mt.parseXCount(null) === null && mt.parseXCount('95') === null && mt.parseXCount('abc') === null);
  const part = mt.matchTotals({ stages: [{ shots: 10, score: 95, x_count: 3 }, { shots: 10, score: '', x_count: '' }, { shots: 20, score: 188, x_count: 6 }] });
  check('match totals only count scored stages', part.score === 283 && part.max === 300 && part.x === 9 && part.scored === 2 && !part.complete && Math.abs(part.pct - 94.333) < 0.01, part);
  const full = mt.matchTotals({ stages: [{ shots: 10, score: 0, x_count: 0 }, { shots: 20, score: 200, x_count: 20 }] });
  check('a zero score still counts as scored', full.complete && full.score === 200 && full.max === 300 && full.x === 20, full);
  check('an empty match has no percent', mt.matchTotals({ stages: [] }).pct === null && !mt.matchTotals({ stages: [] }).complete);

  const sess = [
    { id: 1, date: '2026-09-05', distance: 200, distance_unit: 'yd' },
    { id: 2, date: '2026-09-05', distance: 600, distance_unit: 'yd' },
    { id: 3, date: '2026-09-06', distance: 600, distance_unit: 'yd' },
    { id: 4, date: '2026-08-01', distance: 600, distance_unit: 'yd' },
    { id: 5, date: '2026-09-05', distance: 600, distance_unit: 'yd' },
  ];
  const sg = mt.sessionSuggestions(sess, '2026-09-05', new Set([5]), { distance: 600, distance_unit: 'yd' });
  check('suggestions: within a day of the match, same distance first, taken sessions left out',
        sg.suggested.map((p) => p.id).join() === '2,1,3' && sg.other.map((p) => p.id).join() === '4', [sg.suggested.map((p) => p.id), sg.other.map((p) => p.id)]);
  check('suggestions: no match date means no suggestions', mt.sessionSuggestions(sess, '', new Set()).suggested.length === 0);
  const stg = (d, u) => ({ distance: d, distance_unit: u || 'yd', performance_id: null });
  const nmcAssign = mt.autoAssignSessions([stg(200), stg(200), stg(300), stg(600)], [
    { id: 11, date: '2026-09-05', distance: 600, distance_unit: 'yd' }, { id: 12, date: '2026-09-05', distance: 200, distance_unit: 'yd' },
    { id: 13, date: '2026-09-05', distance: 300, distance_unit: 'yd' }, { id: 14, date: '2026-09-05', distance: 200, distance_unit: 'yd' }], new Set());
  check('auto-link pairs each stage with a session at its own distance, earliest first', nmcAssign.join() === '12,14,13,11', nmcAssign);
  const three = mt.autoAssignSessions([stg(600), stg(600), stg(600)], [
    { id: 22, date: '2026-09-05', distance: 600, distance_unit: 'yd' }, { id: 21, date: '2026-09-05', distance: 600, distance_unit: 'yd' }], new Set());
  check('auto-link leaves a stage empty when no session fits', three.join() === '21,22,' && three[2] === null, three);
  const keep = mt.autoAssignSessions([{ ...stg(600), performance_id: 21 }, stg(600)], [
    { id: 21, date: '2026-09-05', distance: 600, distance_unit: 'yd' }, { id: 22, date: '2026-09-05', distance: 600, distance_unit: 'yd' }], new Set());
  check('auto-link keeps existing links and does not reuse them', keep.join() === '21,22', keep);
  check('auto-link skips sessions already in another match', mt.autoAssignSessions([stg(600)], [{ id: 31, date: '2026-09-05', distance: 600, distance_unit: 'yd' }], new Set([31]))[0] === null);

  const stage = (name, d, shots, score, x) => ({ name, distance: d, distance_unit: 'yd', shots, score, x_count: x });
  const mk = (id, date, course, stages) => ({ id, name: 'M' + id, date, course_name: course, stages });
  const recs = mt.computeRecords([
    mk(1, '2026-06-01', 'National Match Course', [stage('SFS', 200, 10, 90, 2), stage('SFP', 600, 20, 180, 5)]),
    mk(2, '2026-07-01', 'National Match Course', [stage('SFS', 200, 10, 95, 3), stage('SFP', 600, 20, 188, 6)]),
    mk(3, '2026-08-01', 'President\'s Rifle Course', [stage('SFP', 600, 10, 99, 4), stage('SFS', 200, 10, 95, 5)]),
    mk(4, '2026-08-15', 'National Match Course', [stage('SFS', 200, 10, 70, 0), stage('SFP', 600, 20, '', '')]),
  ]);
  const byKey = (n, d, sh) => recs.stages.find((s) => s.name === n && s.distance === d && s.shots === sh);
  check('stage records: best score keeps its match', byKey('SFS', 200, 10).best.score === 95 && byKey('SFS', 200, 10).best.x === 5 && byKey('SFS', 200, 10).best.matchName === 'M3', byKey('SFS', 200, 10));
  check('stage records: equal scores are split by X count', byKey('SFS', 200, 10).best.x === 5);
  check('stage records: 20-shot and 10-shot 600 yd stages are kept apart', byKey('SFP', 600, 20).count === 2 && byKey('SFP', 600, 10).count === 1);
  check('stage records: average, last and series', Math.abs(byKey('SFP', 600, 20).avg - 184) < 1e-9 && byKey('SFP', 600, 20).last.score === 188 && byKey('SFP', 600, 20).series.length === 2);
  check('stage records: unscored stages are ignored', byKey('SFS', 200, 10).count === 4 && recs.stages.every((s) => s.count > 0));
  const linked = mt.computeRecords([
    mk(1, '2026-06-01', 'NMC', [{ ...stage('SFP', 600, 20, 180, 5), performance_id: 41 }]),
    mk(2, '2026-07-01', 'NMC', [{ ...stage('SFP', 600, 20, 185, 5), performance_id: null }]),   // typed in by hand
  ]).stages[0];
  check('stage records remember the session a best came from', linked.best.score === 185 && linked.best.pid === null && linked.series.length === 2);
  check('a best that came from a saved session carries its id', mt.computeRecords([mk(1, '2026-06-01', 'NMC', [{ ...stage('SFP', 600, 20, 190, 6), performance_id: 41 }])]).stages[0].best.pid === 41);
  const firing = mt.computeRecords([mk(1, '2026-06-01', 'NMC', [
    stage('Slow Fire Prone', 600, 20, 180, 5), stage('Rapid Fire Prone', 300, 10, 95, 2), stage('Rapid Fire Sitting', 200, 10, 94, 2),
    stage('Slow Fire Standing', 200, 10, 90, 1), stage('Slow Fire Prone', 300, 10, 96, 3)])]).stages.map((s) => s.distance + ' ' + s.name);
  check('stage records: shortest distance first, then firing order (standing, sitting, rapid, slow)',
        firing.join('|') === '200 Slow Fire Standing|200 Rapid Fire Sitting|300 Rapid Fire Prone|300 Slow Fire Prone|600 Slow Fire Prone', firing);
  check('stage rank: unknown stage names go last', mt.stageRank('Offhand') === 4 && mt.stageRank('Standing') === 0 && mt.stageRank('Kneeling') === 1);
  const nm = recs.courses.find((c) => c.name === 'National Match Course');
  check('course records: only fully scored matches count', nm.count === 2 && nm.best.matchName === 'M2' && Math.abs(nm.best.pct - 94.333) < 0.01, nm);
  check('course records: average and best X', Math.abs(nm.avgPct - ((270 / 300 * 100) + (283 / 300 * 100)) / 2) < 1e-9 && nm.avgX === 8, nm);

  return { log, passed, failed, total: passed + failed };
}

if (typeof require !== 'undefined' && typeof module !== 'undefined' && require.main === module) {
  const fs = require('fs'), path = require('path');
  const html = fs.readFileSync(path.join(__dirname, '..', 'static', 'index.html'), 'utf8');
  const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map((m) => m[1]);
  if (!scripts.length) { console.error('could not find any inline script in static/index.html'); process.exit(1); }
  const res = runTests(scripts);
  res.log.forEach((l) => console.log(l));
  console.log('\n' + res.passed + '/' + res.total + ' passed');
  process.exit(res.failed ? 1 : 0);
}
