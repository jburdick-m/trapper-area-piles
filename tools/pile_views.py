#!/usr/bin/env python3
"""Lit 3-D view of each pile at true scale, with a chip van (53 ft trailer and tractor) beside it for scale.

Height = Feb 4 2026 ASO surface minus 2022 bare earth (USGS CA_SierraNevada 2022
ground returns, read from the public EPT), inside the pile footprint. The 2026
points are shifted onto the 2022 ground by the Feb flight's ground offset (+0.18 m),
the same one the pile mapping used.

  pile_views.py piles.json TILE_DIR OUT_DIR [pile_id ...]
  WORKERS=22 pile_views.py ...

TILE_DIR holds the 2 km Feb 2026 tiles t<E km>_<N km>.laz (NAD83 UTM 10N, NAVD88).
Writes OUT_DIR/pile_<id>.webp and OUT_DIR/summary.json.
"""
import json
import math
import os
import random
import sys
import tempfile
import time
import traceback
from multiprocessing import get_context

os.environ.setdefault('PROJ_DATA', os.path.join(sys.prefix, 'share', 'proj'))
import numpy as np
import laspy
import pdal
from pyproj import Transformer
from scipy.interpolate import griddata
from scipy.spatial import cKDTree
from scipy.ndimage import median_filter
from matplotlib.path import Path
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.colors import LightSource, LinearSegmentedColormap
from mpl_toolkits.mplot3d.art3d import Poly3DCollection, Line3DCollection
from PIL import Image, ImageChops

VAN_L, VAN_W, VAN_H, VAN_DECK = 16.15, 2.59, 4.11, 1.2   # 53 ft x 8.5 ft x 13.5 ft, floor 4 ft up
TRACTOR_L, TRACTOR_OVERLAP = 6.3, 1.5                     # day cab; fifth wheel sits under the trailer nose
RIG_L = VAN_L - TRACTOR_OVERLAP + TRACTOR_L               # ~69 ft overall
CELL = 0.5
FEB_OFFSET = 0.18                                        # Feb 4 2026 ground vs 2022 bare earth, flight-wide
EPTS = ['CA_SierraNevada_6_2022', 'CA_SierraNevada_4_2022']
TILE_KM = 2
TO_3857 = Transformer.from_crs(26910, 3857, always_xy=True)
CMAP = LinearSegmentedColormap.from_list('pile', ['#8a6a4a', '#c08850', '#e8b070', '#f6dcae'])
GROUND = np.array([0.80, 0.78, 0.72])

_tiles = {}


def tile_points(tile_dir, key):
    if key not in _tiles:
        if len(_tiles) >= 3:
            _tiles.pop(next(iter(_tiles)))
        fn = os.path.join(tile_dir, f't{key[0]}_{key[1]}.laz')
        if os.path.exists(fn):
            f = laspy.read(fn)
            _tiles[key] = np.column_stack([f.x, f.y, f.z, np.asarray(f.classification)])
        else:
            _tiles[key] = np.empty((0, 4))
    return _tiles[key]


def feb_points(tile_dir, x0, y0, x1, y1):
    keys = {(TILE_KM * math.floor(x / 1000 / TILE_KM), TILE_KM * math.floor(y / 1000 / TILE_KM))
            for x in (x0, x1) for y in (y0, y1)}
    out = []
    for k in sorted(keys):
        p = tile_points(tile_dir, k)
        m = (p[:, 0] >= x0) & (p[:, 0] <= x1) & (p[:, 1] >= y0) & (p[:, 1] <= y1)
        # tiles overlap by 60 m: keep each tile's points only inside its own 2 km cell
        kx, ky = k[0] * 1000, k[1] * 1000
        m &= (p[:, 0] >= kx) & (p[:, 0] < kx + TILE_KM * 1000) & (p[:, 1] >= ky) & (p[:, 1] < ky + TILE_KM * 1000)
        out.append(p[m])
    return np.vstack(out)


def ground_2022(x0, y0, x1, y1):
    xs, ys = TO_3857.transform([x0, x1, x0, x1], [y0, y0, y1, y1])
    bounds = f'([{min(xs) - 5},{max(xs) + 5}],[{min(ys) - 5},{max(ys) + 5}])'
    stages = [{'type': 'readers.ept',
               'filename': f'https://s3-us-west-2.amazonaws.com/usgs-lidar-public/{e}/ept.json',
               'bounds': bounds, 'threads': 2} for e in EPTS]
    stages += [{'type': 'filters.merge'},
               {'type': 'filters.range', 'limits': 'Classification[2:2]'},
               {'type': 'filters.reprojection', 'in_srs': 'EPSG:3857', 'out_srs': 'EPSG:26910'},
               {'type': 'filters.crop', 'bounds': f'([{x0},{x1}],[{y0},{y1}])'}]
    # many workers at once can get a short read from S3 ("Invalid number of points"): retry
    for attempt in range(6):
        try:
            pl = pdal.Pipeline(json.dumps({'pipeline': stages}))
            pl.execute()
            break
        except RuntimeError:
            if attempt == 5:
                raise
            time.sleep(2 ** attempt + random.random() * 2)
    a = np.concatenate([arr for arr in pl.arrays]) if pl.arrays else None
    if a is None or not len(a):
        return np.empty((0, 3))
    return np.column_stack([a['X'], a['Y'], a['Z']])


def grid_min(pts, gx, gy, cell):
    """Lowest point per cell, then linear TIN onto the grid, nearest beyond the hull."""
    ix = ((pts[:, 0] - gx[0]) / cell).astype(int)
    iy = ((pts[:, 1] - gy[0]) / cell).astype(int)
    k = iy * len(gx) + ix
    order = np.lexsort((pts[:, 2], k))
    first = np.r_[True, np.diff(k[order]) != 0]
    p = pts[order][first]
    X, Y = np.meshgrid(gx, gy)
    Z = griddata(p[:, :2], p[:, 2], (X, Y), method='linear')
    bad = np.isnan(Z)
    if bad.any():
        Z[bad] = griddata(p[:, :2], p[:, 2], (X[bad], Y[bad]), method='nearest')
    return Z


def footprint_mask(rings, X, Y):
    inside = np.zeros(X.shape, bool)
    xy = np.column_stack([X.ravel(), Y.ravel()])
    for r in rings:  # even-odd, as on the map
        inside ^= Path(np.asarray(r)).contains_points(xy).reshape(X.shape)
    return inside


def box_faces(xa, xb, ya, yb, za, zb, rgb, alpha):
    """Six faces of a box, each shaded as if lit from the north-west."""
    faces = [
        ([(xa, ya, za), (xb, ya, za), (xb, yb, za), (xa, yb, za)], 0.40),   # floor
        ([(xa, yb, za), (xb, yb, za), (xb, yb, zb), (xa, yb, zb)], 0.60),   # north
        ([(xa, ya, za), (xa, yb, za), (xa, yb, zb), (xa, ya, zb)], 0.80),   # west
        ([(xb, ya, za), (xb, yb, za), (xb, yb, zb), (xb, ya, zb)], 0.55),   # east
        ([(xa, ya, za), (xb, ya, za), (xb, ya, zb), (xa, ya, zb)], 0.70),   # south
        ([(xa, ya, zb), (xb, ya, zb), (xb, yb, zb), (xa, yb, zb)], 0.95),   # top
    ]
    return [f for f, _ in faces], [(*(np.asarray(rgb) * s), alpha) for _, s in faces]


def wheel(xc, y, r=0.5):
    th = np.linspace(0, 2 * np.pi, 20)
    return [(xc + r * np.cos(t), y, r + r * np.sin(t)) for t in th]


def rig(x0, y0):
    """Chip van and tractor along +x from x0: 53 ft trailer, day-cab tractor in front (east)."""
    x1, y1 = x0 + VAN_L, y0 + VAN_W
    faces, cols = [], []
    def add(*a):
        f, c = box_faces(*a)
        faces.extend(f)
        cols.extend(c)
    add(x0, x1, y0, y1, VAN_DECK, VAN_H, (0.20, 0.42, 0.72), 0.30)                       # trailer
    t0 = x1 - TRACTOR_OVERLAP                                                            # tractor rear, under the kingpin
    add(t0, t0 + TRACTOR_L, y0 + 0.4, y1 - 0.4, 0.9, 1.2, (0.25, 0.25, 0.25), 0.9)        # frame
    add(t0 + 2.6, t0 + 4.6, y0 + 0.05, y1 - 0.05, 1.0, 3.8, (0.62, 0.20, 0.16), 0.55)     # cab
    add(t0 + 4.6, t0 + TRACTOR_L, y0 + 0.15, y1 - 0.15, 1.0, 2.5, (0.62, 0.20, 0.16), 0.55)  # hood
    ribs = []
    for xr in np.arange(x0, x1 + 0.01, VAN_L / 12):
        ribs += [[(xr, y0, VAN_DECK), (xr, y0, VAN_H)], [(xr, y0, VAN_H), (xr, y1, VAN_H)]]
    wheels = [wheel(xa, yw) for xa in (x0 + 1.2, x0 + 2.5, t0 + 0.6, t0 + 1.9, t0 + 5.4) for yw in (y0, y1)]
    return faces, cols, ribs, wheels


def render(job):
    try:
        return render_one(*job)
    except Exception as e:  # one bad pile should not stop the rest
        return {'pile_id': int(job[0]['pile_id']), 'ok': False, 'error': repr(e), 'trace': traceback.format_exc()[-600:]}


def render_one(pile, tile_dir, out_dir):
    pid = int(pile['pile_id'])
    hmax_m = float(pile['hmax_ft']) * 0.3048
    rings = json.loads(pile['geom']) if isinstance(pile['geom'], str) else pile['geom']
    allxy = np.vstack([np.asarray(r) for r in rings])
    fx0, fy0 = allxy.min(0)
    fx1, fy1 = allxy.max(0)
    cx = (fx0 + fx1) / 2
    size = max(fx1 - fx0, fy1 - fy0)
    pad, gap = max(5.0, 0.12 * size), 3.0
    vx0, vy0 = cx - RIG_L / 2, fy0 - gap - VAN_W
    x0 = math.floor(min(fx0, vx0) - pad)
    x1 = math.ceil(max(fx1, vx0 + RIG_L) + pad)
    y0 = math.floor(vy0 - pad)
    y1 = math.ceil(fy1 + pad)

    # 2022 ground is cached per pile (GROUND_DIR), so the render can run from the bucket alone
    gdir = os.environ.get('GROUND_DIR')
    gfile = os.path.join(gdir, f'ground22_{pid}.npy') if gdir else None
    if gfile and os.path.exists(gfile):
        g22 = np.load(gfile)
    else:
        g22 = ground_2022(x0 - 3, y0 - 3, x1 + 3, y1 + 3)
        if gfile:
            os.makedirs(gdir, exist_ok=True)
            np.save(gfile, g22.astype(np.float64))
    if os.environ.get('FETCH_ONLY'):
        return {'pile_id': pid, 'ok': len(g22) >= 10, 'n_g22': int(len(g22))}
    feb = feb_points(tile_dir, x0 - 3, y0 - 3, x1 + 3, y1 + 3)
    if len(feb) < 10 or len(g22) < 10:
        return {'pile_id': pid, 'ok': False, 'n_feb': len(feb), 'n_g22': len(g22)}

    gx = np.arange(x0, x1 + CELL / 2, CELL)
    gy = np.arange(y0, y1 + CELL / 2, CELL)
    X, Y = np.meshgrid(gx, gy)
    dtm = grid_min(g22, gx, gy, CELL)
    inside = footprint_mask(rings, X, Y)

    # ground offset: 2026 ground returns 3 m+ outside the footprint vs 2022 bare earth
    def dtm_at(p):
        ix = np.clip(np.round((p[:, 0] - gx[0]) / CELL).astype(int), 0, len(gx) - 1)
        iy = np.clip(np.round((p[:, 1] - gy[0]) / CELL).astype(int), 0, len(gy) - 1)
        return dtm[iy, ix], ix, iy
    d, ix, iy = dtm_at(feb)
    near = np.zeros(X.shape, bool)
    r = int(round(3 / CELL))
    for dy in range(-r, r + 1, 2):
        for dx in range(-r, r + 1, 2):
            near |= np.roll(np.roll(inside, dy, 0), dx, 1)
    # the local offset includes snow on the ground around the pile, so it is reported only;
    # heights use the flight-wide offset, as the pile mapping did
    out_ring = ~near[iy, ix] & (feb[:, 3] == 2)
    local = float(np.median(feb[out_ring, 2] - d[out_ring])) if out_ring.sum() >= 15 else float('nan')
    offset = FEB_OFFSET

    # 2026 surface on the pile: top return per 1 m cell, TIN onto the grid
    on = inside[iy, ix] | near[iy, ix]
    p = feb[on & (feb[:, 2] - d - offset < 40)]
    H = np.zeros(X.shape)
    if len(p) >= 4:
        k = ((p[:, 1] - y0) // 1).astype(int) * 10000 + ((p[:, 0] - x0) // 1).astype(int)
        order = np.lexsort((-p[:, 2], k))
        first = np.r_[True, np.diff(k[order]) != 0]
        top = p[order][first]
        # drop standing trees and stray high returns: a top more than 1.5 m over the
        # median of its 3 m neighbourhood is not slash surface
        ht = top[:, 2] - dtm_at(top)[0] - offset
        nb = cKDTree(top[:, :2]).query_ball_point(top[:, :2], 3.0)
        med = np.array([np.median(ht[n]) for n in nb])
        top = top[ht <= med + 1.5]
        S = griddata(top[:, :2], top[:, 2], (X, Y), method='linear')
        h = S - dtm - offset
        # never taller than the mapped pile height
        H = np.where(inside & np.isfinite(h), np.clip(h, 0, hmax_m + 0.3), 0.0)
        # a 2.5 m median takes out the tree clumps the point filter keeps at the edges
        H = np.where(inside, median_filter(H, size=5, mode='nearest'), 0.0)
    vol = float(H.sum() * CELL * CELL)

    # ---- figure
    zt = max(H.max(), VAN_H) + 1.0
    fig = plt.figure(figsize=(10, 6.6), dpi=120)
    ax = fig.add_subplot(projection='3d', computed_zorder=False)
    ls = LightSource(azdeg=315, altdeg=40)
    rgb = ls.shade(H, cmap=CMAP, vert_exag=1, blend_mode='soft', vmin=0, vmax=max(H.max(), 1))
    rgb[~inside, :3] = GROUND * ls.hillshade(H, vert_exag=1)[~inside, None] * 0.25 + GROUND * 0.75
    Xr, Yr = X - cx, Y - (fy0 + fy1) / 2
    ax.plot_surface(Xr, Yr, H, facecolors=rgb, rstride=1, cstride=1, linewidth=0, antialiased=False, shade=False, zorder=1)

    faces, cols, ribs, wheels = rig(vx0 - cx, vy0 - (fy0 + fy1) / 2)
    ax.add_collection3d(Poly3DCollection(faces, facecolors=cols, edgecolors='#1d2b3a', linewidths=0.9, zorder=5))
    ax.add_collection3d(Line3DCollection(ribs, colors='#173a66', linewidths=0.45, alpha=0.7, zorder=6))
    ax.add_collection3d(Poly3DCollection(wheels, facecolors='#222222', edgecolors='#111111', linewidths=0.5, zorder=6))

    ax.set_xlim(Xr.min(), Xr.max())
    ax.set_ylim(Yr.min(), Yr.max())
    ax.set_zlim(0, zt)
    ax.set_box_aspect((np.ptp(Xr), np.ptp(Yr), zt), zoom=1.05)
    ax.view_init(elev=32, azim=-62)
    for a in (ax.xaxis, ax.yaxis, ax.zaxis):
        a.set_pane_color((0.97, 0.97, 0.96, 1.0))
    ax.set_xlabel('E (m)', labelpad=6)
    ax.set_ylabel('N (m)', labelpad=6)
    ax.set_zlabel('m', labelpad=2)
    ax.tick_params(labelsize=8, pad=1)
    zt_ticks = [t for t in (0, 2, 5, 10, 15, 20, 30) if t <= zt]
    ax.set_zticks(zt_ticks)
    fig.subplots_adjust(left=0, right=1, bottom=0, top=1)
    png = os.path.join(out_dir, f'pile_{pid}.png')
    fig.savefig(png, facecolor='white')
    plt.close(fig)
    im = Image.open(png).convert('RGB')
    bbox = ImageChops.difference(im, Image.new('RGB', im.size, 'white')).getbbox()
    if bbox:
        im = im.crop((max(0, bbox[0] - 12), max(0, bbox[1] - 12), min(im.width, bbox[2] + 12), min(im.height, bbox[3] + 12)))
    im.save(os.path.join(out_dir, f'pile_{pid}.webp'), quality=82, method=6)
    os.remove(png)
    return {'pile_id': pid, 'ok': True, 'n_feb': int(len(feb)), 'n_g22': int(len(g22)), 'local_offset_m': round(local, 2),
            'vol_img_m3': round(vol, 1), 'vol_m3': pile['vol_m3'], 'hmax_img_m': round(float(H.max()), 2)}


def worker_init():
    d = tempfile.mkdtemp(prefix=f'pv{os.getpid()}_')
    os.environ['TMPDIR'] = d
    tempfile.tempdir = d


def main():
    piles_fn, tile_dir, out_dir = sys.argv[1:4]
    want = {int(a) for a in sys.argv[4:]}
    os.makedirs(out_dir, exist_ok=True)
    piles = [p for p in json.load(open(piles_fn)) if not want or int(p['pile_id']) in want]
    piles.sort(key=lambda p: (TILE_KM * math.floor(p['x'] / 1000 / TILE_KM), TILE_KM * math.floor(p['y'] / 1000 / TILE_KM)))
    jobs = [(p, tile_dir, out_dir) for p in piles]
    results = []
    # spawned workers, each with its own temp dir: forked ones share PDAL's random temp
    # names and delete each other's EPT tiles
    ctx = get_context('spawn')
    with ctx.Pool(int(os.environ.get('WORKERS', '4')), initializer=worker_init, maxtasksperchild=40) as pool:
        for r in pool.imap_unordered(render, jobs, chunksize=2):
            print(json.dumps(r), flush=True)
            results.append(r)
    results.sort(key=lambda r: r['pile_id'])
    json.dump(results, open(os.path.join(out_dir, 'summary.json'), 'w'), indent=1)


if __name__ == '__main__':
    main()
