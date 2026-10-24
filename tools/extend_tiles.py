#!/usr/bin/env python3
"""Cut 2 km view tiles from a raw ASO tile, aligned to the existing view tiles.

  extend_tiles.py RAW.las OLD_TILE_DIR OUT_DIR t678_4376 t680_4376 ...

The raw ASO tiles are in Albers (WGS84) with ellipsoid heights; the view tiles are NAD83 UTM 10N with
NAVD88 heights. Points are projected with pyproj, then shifted by the median offset between the same
pulses (matched on GPS time and return number) in the raw tile and in the existing view tiles, which
carries the geoid and any datum shift. Each output tile holds its 2 km square plus 60 m around it,
like the existing ones.
"""
import json
import os
import sys

import numpy as np
import laspy
from pyproj import Transformer

ALB = "+proj=aea +lat_0=42 +lon_0=-112 +lat_1=38.5 +lat_2=45.5 +x_0=0 +y_0=0 +datum=WGS84 +units=m"
PAD = 60


def main():
    raw_fn, old_dir, out_dir = sys.argv[1:4]
    tiles = sys.argv[4:]
    os.makedirs(out_dir, exist_ok=True)
    boxes = {t: (int(t[1:4]) * 1000, int(t[5:]) * 1000) for t in tiles}
    x0 = min(b[0] for b in boxes.values()) - PAD
    x1 = max(b[0] for b in boxes.values()) + 2000 + PAD
    y0 = min(b[1] for b in boxes.values()) - PAD
    y1 = max(b[1] for b in boxes.values()) + 2000 + PAD
    tf = Transformer.from_crs(ALB, 26910, always_xy=True)
    keep = []
    with laspy.open(raw_fn) as f:
        hdr = f.header
        for ch in f.chunk_iterator(5_000_000):
            X, Y = tf.transform(np.asarray(ch.x), np.asarray(ch.y))
            m = (X >= x0) & (X < x1) & (Y >= y0) & (Y < y1)
            if m.any():
                sub = ch[m]
                keep.append((sub, X[m], Y[m]))
    X = np.concatenate([k[1] for k in keep]); Y = np.concatenate([k[2] for k in keep])
    Z = np.concatenate([np.asarray(k[0].z) for k in keep])
    gps = np.concatenate([np.asarray(k[0].gps_time) for k in keep])
    rn = np.concatenate([np.asarray(k[0].return_number) for k in keep])
    print(f'raw points in the strip: {len(X):,}', flush=True)
    # alignment: same pulses in the existing view tiles
    dx, dy, dz = [], [], []
    for t in tiles:
        fn = os.path.join(old_dir, t + '.laz')
        if not os.path.exists(fn):
            continue
        o = laspy.read(fn)
        key_o = np.round(np.asarray(o.gps_time), 6) * 10 + np.asarray(o.return_number)
        key_r = np.round(gps, 6) * 10 + rn
        common, io, ir = np.intersect1d(key_o, key_r, return_indices=True)
        if len(common):
            dx.append(np.asarray(o.x)[io] - X[ir]); dy.append(np.asarray(o.y)[io] - Y[ir]); dz.append(np.asarray(o.z)[io] - Z[ir])
    if not dx:
        sys.exit('no pulses matched the existing view tiles: cannot align')
    dx, dy, dz = (np.concatenate(v) for v in (dx, dy, dz))
    sx, sy, sz = float(np.median(dx)), float(np.median(dy)), float(np.median(dz))
    spread = [float(np.percentile(np.abs(v - np.median(v)), 95)) for v in (dx, dy, dz)]
    print(f'matched pulses {len(dx):,}: shift x {sx:.3f} y {sy:.3f} z {sz:.3f} m; 95% spread {spread}', flush=True)
    X, Y, Z = X + sx, Y + sy, Z + sz
    for t, (bx, by) in boxes.items():
        m = (X >= bx - PAD) & (X < bx + 2000 + PAD) & (Y >= by - PAD) & (Y < by + 2000 + PAD)
        h = laspy.LasHeader(point_format=keep[0][0].point_format.id, version='1.4')
        h.offsets = [bx, by, 0.0]
        h.scales = [0.01, 0.01, 0.01]
        las = laspy.LasData(h)
        las.x, las.y, las.z = X[m], Y[m], Z[m]
        for dim in ('intensity', 'return_number', 'number_of_returns', 'classification', 'gps_time', 'user_data',
                    'scan_angle', 'point_source_id'):
            try:
                las[dim] = np.concatenate([np.asarray(k[0][dim]) for k in keep])[m]
            except Exception:
                pass
        las.write(os.path.join(out_dir, t + '.laz'))
        print(f'{t}: {m.sum():,} points', flush=True)
    json.dump({'shift_m': [sx, sy, sz], 'spread95_m': spread, 'matched': int(len(dx))}, open(os.path.join(out_dir, 'align.json'), 'w'))


if __name__ == '__main__':
    main()
