#!/usr/bin/env python3
"""Find burn piles 30 ft+ across that the mapped inventory (data/piles.json, 40 ft+) does not have.

  pile_detect.py TRAIN_ROWS PILES UNITS TILE_DIR OUT_DIR [tile ...]   # detect, one JSON per tile
  pile_detect.py --merge PILES OUT_DIR NEW_PILES                      # collect the additions
  WORKERS=22 DRAWS=10 pile_detect.py ...

Per tile (tools/pilefind.py does the detection):
1. Every cluster gets a classifier probability. The classifier (gradient boosting on evidence features
   that read the same at any density) is trained on TRAIN_ROWS: clusters from 7 test tiles detected at
   full density and thinned to 50, 30, 20 and 12% of pulses. Mapped piles are positives at any size;
   unmapped clusters are negatives only at 40 ft+, where the mapped inventory should be complete.
2. Acceptance rules, set to keep about 90% of confirmed mapped piles: probability >= 0.85, a dome
   (heights fall off from the centre), few returns passing through or hanging over, a clear deficit of
   ground returns, at most 0.4 times as tall as it is wide, compact, and at least 20 pulses on it.
3. Anything touching a mapped pile (within 3 m), or with its centre outside the treatment units, is set
   aside.
4. Stability: the tile is re-detected on 10 random half-density draws (whole pulses kept or dropped);
   a pile is added only if a likely-pile cluster (probability >= 0.5) covers at least 30% of it in 7 or
   more draws. 90-100% of confirmed mapped piles pass this at every pulse count.
5. Size and volume are corrected for this detector reading low on sparsely sampled piles (calibrated
   against the mapped piles at matched pulse counts), with a 90% range. Additions 30 ft+ (after the
   correction) are kept, including the few 40 ft+ piles the mapped inventory missed.
"""
import json
import math
import os
import pickle
import sys
from multiprocessing import get_context

import numpy as np
from scipy import ndimage as ndi
from matplotlib.path import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pilefind as pf  # noqa: E402

FEATS = [f for f in pf.FEATURES if f != 'h90_m']
P_MIN, DOME_MAX, PASS_MAX, OVER_MAX, GDZ_MIN = 0.85, -0.3, 0.20, 0.12, 2.0
H90_MIN, HRATIO_MAX, COMPACT_MIN, SINGLE_MIN, PULSES_MIN = 1.5, 0.40, 0.30, 0.6, 20
STAB_MIN, DIAM_MIN, DIAM_MAX = 0.7, 30.0, 40.0
DRAWS = int(os.environ.get('DRAWS', 10))
MIN_Y = float(os.environ.get('MIN_Y', 0))          # only piles north of this (a strip added later)
ID_START = int(os.environ.get('ID_START', 1001))
# detected / mapped, by pulses on the pile: area (median), volume (5th, 50th, 95th percentile)
CALIB = [(10, 30, 0.850, 0.431, 0.731, 1.025), (30, 60, 0.732, 0.448, 0.688, 1.066),
         (60, 120, 0.838, 0.434, 0.741, 1.328), (120, 250, 0.904, 0.541, 0.859, 1.157),
         (250, 1e12, 0.925, 0.631, 0.906, 1.065)]


def calib(n):
    for lo, hi, a, v05, v50, v95 in CALIB:
        if n < hi:
            return a, v05, v50, v95
    return CALIB[-1][2:]


def accept(r, pulses_min=PULSES_MIN):
    return (r['p'] >= P_MIN and r['dome'] <= DOME_MAX and r['pass_share'] <= PASS_MAX and r['over_share'] <= OVER_MAX
            and r['gnd_deficit_z'] >= GDZ_MIN and r['h90_m'] >= H90_MIN and r['h_ratio'] <= HRATIO_MAX
            and r['compact'] >= COMPACT_MIN and r['single_share'] >= SINGLE_MIN and r['n_first'] >= pulses_min)


def train(rows_fn, out_fn):
    from sklearn.ensemble import HistGradientBoostingClassifier
    rows = json.load(open(rows_fn))
    use = [r for r in rows if (r['pos'] or (r['neg'] and r['diam_ft'] >= 40)) and r['h90_m'] >= 1.5]
    X = np.array([[r[f] for f in FEATS] for r in use], float)
    y = np.array([r['pos'] for r in use])
    clf = HistGradientBoostingClassifier(max_iter=300, learning_rate=0.05, max_leaf_nodes=15, min_samples_leaf=20,
                                         l2_regularization=1.0, class_weight='balanced', random_state=0)
    clf.fit(X, y)
    pickle.dump(clf, open(out_fn, 'wb'))
    print(f'classifier: {len(use)} training clusters, {int(y.sum())} on mapped piles', flush=True)


def unit_polys(units):
    out = []
    for u in units:
        g = json.loads(u['geom']) if isinstance(u['geom'], str) else u['geom']
        out.append((u['project'], u.get('unit') or '', [Path(np.asarray(r)) for r in g if len(r) >= 3]))
    return out


def unit_of(x, y, polys):
    for project, unit, paths in polys:
        inside = False
        for p in paths:
            inside ^= p.contains_point((x, y))
        if inside:
            return project, unit
    return None, None


_W = {}


def init(model_fn, piles_fn, units_fn, tile_dir, out_dir):
    _W.update(clf=pickle.load(open(model_fn, 'rb')), piles=json.load(open(piles_fn)),
              polys=unit_polys(json.load(open(units_fn))), tile_dir=tile_dir, out_dir=out_dir)


def score(rows):
    if rows:
        X = np.array([[r[f] for f in FEATS] for r in rows], float)
        for r, p in zip(rows, _W['clf'].predict_proba(X)[:, 1]):
            r['p'] = float(p)


def run_tile(tile):
    try:
        return detect_tile(tile)
    except Exception as e:
        import traceback
        return {'tile': tile, 'error': repr(e), 'trace': traceback.format_exc()[-1500:]}


def detect_tile(tile):
    t = pf.Tile(tile, _W['tile_dir'], os.environ.get('RASTER_CACHE'))
    rows, pl, gi = pf.detect(t)
    score(rows)
    mapped = ndi.binary_dilation(pf.known_label_grid(t, _W['piles']) > 0, pf.disk(3))
    cands, extra40, diag = [], [], []
    for r in rows:
        if not accept(r):
            continue
        m, r0, c0 = r['_mask']
        if mapped[r0:r0 + m.shape[0], c0:c0 + m.shape[1]][m].any():
            if os.environ.get('DIAG'):          # mapped piles, to calibrate the stability test
                r['diam_cal'] = r['diam_ft']
                diag.append(r)
            continue
        project, unit = unit_of(r['x'], r['y'], _W['polys'])
        if project is None or r['y'] < MIN_Y:
            continue
        a, v05, v50, v95 = calib(r['n_first'])
        r['area_cal'] = r['area_m2'] / a
        r['diam_cal'] = 2 * math.sqrt(r['area_cal'] / math.pi) / pf.FT
        r['vol_cal'], r['vol_lo'], r['vol_hi'] = r['vol_m3'] / v50, r['vol_m3'] / v95, r['vol_m3'] / v05
        r['project'], r['unit'] = project, unit
        if DIAM_MIN <= r['diam_cal'] < DIAM_MAX:
            cands.append(r)
        elif r['diam_cal'] >= DIAM_MAX:
            extra40.append(r)
    # stability over half-density draws
    allc = cands + extra40 + diag
    hits = np.zeros(len(allc))
    dvol = [[] for _ in allc]
    for d in range(DRAWS if allc else 0):
        sub = t.subsample(0.5, seed=1000 + d)
        rd, _, _ = pf.detect(sub)
        score(rd)
        grid = np.zeros((t.H, t.W), np.int32)
        for j, q in enumerate(rd, start=1):
            if q['p'] >= 0.5 and q['h90_m'] >= H90_MIN:
                m, r0, c0 = q['_mask']
                sl = grid[r0:r0 + m.shape[0], c0:c0 + m.shape[1]]
                sl[m] = j
        for i, r in enumerate(allc):
            m, r0, c0 = r['_mask']
            g = grid[r0:r0 + m.shape[0], c0:c0 + m.shape[1]][m]
            if (g > 0).mean() >= 0.3:
                hits[i] += 1
                j = np.bincount(g[g > 0]).argmax()
                dvol[i].append(rd[j - 1]['vol_m3'] / max(r['vol_m3'], 1e-6) - 1)
    out = []
    for i, r in enumerate(allc):
        r['stability'] = float(hits[i] / max(DRAWS, 1))
        r['dens_sens'] = round(100 * float(np.mean(dvol[i])), 1) if dvol[i] else None
        m, r0, c0 = r.pop('_mask')
        r.pop('_members', None)
        r['geom'] = pf.mask_rings(t, m, r0, c0)
        r['tile'] = tile
        r['kind'] = 'add' if i < len(cands) else ('extra40' if i < len(cands) + len(extra40) else 'mapped')
        r.setdefault('project', None)
        out.append(r)
    json.dump({'tile': tile, 'clusters': len(rows), 'piles': out}, open(os.path.join(_W['out_dir'], tile + '.json'), 'w'))
    return {'tile': tile, 'clusters': len(rows), 'adds': sum(1 for r in out if r['kind'] == 'add' and r['stability'] >= STAB_MIN),
            'extra40': sum(1 for r in out if r['kind'] == 'extra40' and r['stability'] >= STAB_MIN)}


def merge(piles_fn, out_dir, new_fn):
    import glob
    adds = []
    for fn in sorted(glob.glob(os.path.join(out_dir, 't*.json'))):
        for r in json.load(open(fn))['piles']:
            if r['stability'] >= STAB_MIN and r['kind'] in ('add', 'extra40'):
                adds.append(r)
    adds.sort(key=lambda r: -r['vol_cal'])
    new = []
    for i, r in enumerate(adds):
        new.append({
            'pile_id': ID_START + i, 'project': r['project'], 'unit': r['unit'],
            'basis': 'open_2022' if r['chm22_p50'] < 2 else 'canopy_removed',
            'size_cls': 'medium 30-40ft' if r['diam_cal'] < DIAM_MAX else 'large 40ft+',
            'area_m2': round(r['area_cal'], 1), 'diam_ft': round(r['diam_cal'], 1), 'hmax_ft': round(r['hmax_m'] / pf.FT, 1),
            'vol_m3': round(r['vol_cal'], 1), 'vol_lo95': round(r['vol_lo'], 1), 'vol_hi95': round(r['vol_hi'], 1),
            'ci95_pct': round((r['vol_hi'] - r['vol_lo']) / 2 / r['vol_cal'] * 100, 1), 'ci_level': 90,
            'dens_sens': r['dens_sens'], 'snow_m': r['snow_m'], 'chm22_p50': round(r['chm22_p50'], 1),
            'n_ret': r['n_ret'], 'n_pulses': r['n_first'], 'p_pile': round(r['p'], 3), 'stability': r['stability'],
            'x': r['x'], 'y': r['y'], 'geom': json.dumps(r['geom']), 'added': True,
        })
    json.dump(new, open(new_fn, 'w'))
    n40 = sum(1 for p in new if p['diam_ft'] >= DIAM_MAX)
    print(f'added piles: {len(new)} ({len(new) - n40} at 30-40 ft, {n40} at 40 ft+)')
    by = {}
    for p in new:
        by[p['project']] = by.get(p['project'], 0) + 1
    print('by project:', by)


def main():
    if sys.argv[1] == '--merge':
        merge(*sys.argv[2:5])
        return
    train_fn, piles_fn, units_fn, tile_dir, out_dir = sys.argv[1:6]
    os.makedirs(out_dir, exist_ok=True)
    model_fn = os.path.join(out_dir, 'model.pkl')
    train(train_fn, model_fn)
    tiles = sys.argv[6:] or sorted(f[:-4] for f in os.listdir(tile_dir) if f.endswith('.laz'))
    # biggest tiles first so the long ones do not finish last
    tiles.sort(key=lambda t: -os.path.getsize(os.path.join(tile_dir, t + '.laz')))
    with get_context('spawn').Pool(int(os.environ.get('WORKERS', 4)), initializer=init,
                                   initargs=(model_fn, piles_fn, units_fn, tile_dir, out_dir)) as pool:
        for res in pool.imap_unordered(run_tile, tiles):
            print(json.dumps(res), flush=True)


if __name__ == '__main__':
    main()
