"""Burn pile detection in sparse snow-on lidar (Feb 4 2026 ASO) against 2022 bare earth.

Shared by tools/pile_detect.py (full run) and the validation scripts. The detector works on returns,
not on a 1 m raster, because half the 1 m cells in this flight are empty:

1. Height h of every return above 2022 bare earth (USGS CA_SierraNevada_B22 1 m DEM), less a smooth
   ground offset (snow + datum) from 2026 ground returns. ASO classified about half of the returns on
   pile tops as ground, so the offset is taken from the lower mode of the ground returns in each
   50 m block, not their median.
2. Pile-like returns: 0.5-12 m up, the last return of their pulse (a pile stops the pulse), nothing
   more than 2 m higher within ~1.5 m (open sky above), and not a 2022 tree of the same height.
3. Pile-like returns within 3 m of each other (and within 3 m in height) are joined into clusters.
4. Each cluster's footprint is where a surface through its pile-like returns and the surrounding
   ground returns stands at least 0.5 m above 2022 ground; area, height and volume are measured on it.
5. Evidence features per cluster, normalised by the pulses that hit it so they read the same at any
   density: share of pulses stopped above ground, ground returns seen versus expected, returns
   passing through, overhanging returns, 2022 canopy, shape, height profile, intensity.
"""
import json
import math
import os

import numpy as np
import laspy
import rasterio
from rasterio.enums import Resampling
from rasterio.windows import from_bounds
from scipy import ndimage as ndi
from scipy.interpolate import griddata
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components
from scipy.spatial import cKDTree

TILE = 2000           # m, view tile size; tiles are named t<E km>_<N km> by their SW corner
BUF = 40              # m read around each tile so piles on tile edges are whole
DEM_URL = ('/vsicurl/https://prd-tnm.s3.amazonaws.com/StagedProducts/Elevation/1m/Projects/'
           'CA_SierraNevada_B22/TIFF/USGS_1M_10_x{x}y{y}_CA_SierraNevada_B22.tif')
CHM_URL = os.environ.get('CHM22', '/vsigs/snowpalm-w1/Input_Data/NY_22_CHM.tif')

H_LO, H_HI = 0.5, 12.0   # m, pile-like return heights
GROUND_H = 0.4           # m, at or below this a return is on the ground
LINK_R, LINK_DZ = 3.0, 3.0
TOE = 0.5                # m, footprint edge
FT = 0.3048


# ---------------------------------------------------------------- inputs
def tile_origin(tile):
    kx, ky = (int(v) * 1000 for v in tile[1:].split('_'))
    return kx, ky


def tile_box(tile):
    kx, ky = tile_origin(tile)
    return kx - BUF, ky - BUF, kx + TILE + BUF, ky + TILE + BUF


def read_dem(x0, y0, x1, y1):
    """2022 bare earth on the 1 m grid [x0, x1) x [y0, y1), north up, from the 10 km USGS tiles."""
    W, H = x1 - x0, y1 - y0
    out = np.full((H, W), np.nan, np.float32)
    for tx in range(x0 // 10000, (x1 - 1) // 10000 + 1):
        for ty in range(y0 // 10000 + 1, (y1 - 1) // 10000 + 2):
            with rasterio.open(DEM_URL.format(x=tx, y=ty)) as d:
                bx0, by0 = max(x0, math.ceil(d.bounds.left)), max(y0, math.ceil(d.bounds.bottom))
                bx1, by1 = min(x1, math.floor(d.bounds.right)), min(y1, math.floor(d.bounds.top))
                if bx1 <= bx0 or by1 <= by0:
                    continue
                a = d.read(1, window=from_bounds(bx0, by0, bx1, by1, d.transform), out_shape=(by1 - by0, bx1 - bx0),
                           resampling=Resampling.bilinear).astype(np.float32)
                a[a < -1000] = np.nan
                sub = out[y1 - by1:y1 - by0, bx0 - x0:bx1 - x0]
                np.copyto(sub, a, where=np.isnan(sub))
    return out


def read_chm(x0, y0, x1, y1):
    """2022 canopy height, 1 m cells, highest 0.75 m cell in each."""
    with rasterio.open(CHM_URL) as d:
        a = d.read(1, window=from_bounds(x0, y0, x1, y1, d.transform), out_shape=(y1 - y0, x1 - x0),
                   resampling=Resampling.max, boundless=True, fill_value=np.nan).astype(np.float32)
    a[(a < -1) | (a > 120)] = np.nan
    return a


def rasters(tile, cache_dir=None):
    fn = os.path.join(cache_dir, f'{tile}_rasters.npz') if cache_dir else None
    if fn and os.path.exists(fn):
        z = np.load(fn)
        return z['dem'], z['chm']
    x0, y0, x1, y1 = tile_box(tile)
    dem, chm = read_dem(x0, y0, x1, y1), read_chm(x0, y0, x1, y1)
    if fn:
        os.makedirs(cache_dir, exist_ok=True)
        np.savez_compressed(fn, dem=dem, chm=chm)
    return dem, chm


def thin_mask(gps, keep, seed):
    """Keep whole pulses (all returns sharing a GPS time) with probability keep."""
    if keep >= 1:
        return np.ones(len(gps), bool)
    u, inv = np.unique(gps, return_inverse=True)
    rng = np.random.default_rng(seed)
    return (rng.random(len(u)) < keep)[inv]


class Tile:
    """Points of one 2 km tile (plus BUF), with heights above 2022 ground."""

    def __init__(self, tile, tile_dir, cache_dir=None, keep=1.0, seed=0):
        self.tile = tile
        self.x0, self.y0, self.x1, self.y1 = tile_box(tile)
        self.W, self.H = self.x1 - self.x0, self.y1 - self.y0
        f = laspy.read(os.path.join(tile_dir, tile + '.laz'))
        x, y = np.asarray(f.x), np.asarray(f.y)
        m = (x >= self.x0) & (x < self.x1) & (y >= self.y0) & (y < self.y1)
        m &= thin_mask(np.asarray(f.gps_time), keep, seed)
        self.x, self.y, self.z = x[m], y[m], np.asarray(f.z)[m]
        self.cls = np.asarray(f.classification)[m]
        self.rn, self.nr = np.asarray(f.return_number)[m], np.asarray(f.number_of_returns)[m]
        self.inten = np.asarray(f.intensity)[m].astype(np.float32)
        self.gps = np.asarray(f.gps_time)[m]
        self.dem, self.chm = rasters(tile, cache_dir)
        self.r = np.clip((self.y1 - self.y).astype(int), 0, self.H - 1)
        self.c = np.clip((self.x - self.x0).astype(int), 0, self.W - 1)
        self.g = self.z - self._sample(self.dem, self.x, self.y)
        self.off_grid = self._ground_offset()
        self.h = self.g - self._offset_at(self.x, self.y)
        ok = np.isfinite(self.h)
        for k in ('x', 'y', 'z', 'cls', 'rn', 'nr', 'inten', 'gps', 'r', 'c', 'g', 'h'):
            setattr(self, k, getattr(self, k)[ok])
        self.first = self.rn == 1
        self.last = self.rn == self.nr

    def subsample(self, keep, seed):
        """The same tile with only a share of its pulses (ground offset kept from the full data)."""
        m = thin_mask(self.gps, keep, seed)
        s = object.__new__(Tile)
        s.__dict__.update(self.__dict__)
        for k in ('x', 'y', 'z', 'cls', 'rn', 'nr', 'inten', 'gps', 'r', 'c', 'g', 'h', 'first', 'last'):
            setattr(s, k, getattr(self, k)[m])
        return s

    def _sample(self, a, x, y):
        """Bilinear sample of a 1 m raster (cell centres) at map coordinates."""
        cc = x - self.x0 - 0.5
        rr = self.y1 - y - 0.5
        v = ndi.map_coordinates(np.nan_to_num(a, nan=-9999), [rr, cc], order=1, mode='nearest')
        bad = ndi.map_coordinates(np.isnan(a).astype(np.float32), [rr, cc], order=1, mode='nearest') > 0
        v[bad] = np.nan
        return v

    def _ground_offset(self, B=50):
        """Snow + datum per 50 m block from the lower mode of 2026 ground returns; smooth."""
        m = (self.cls == 2) & np.isfinite(self.g) & (np.abs(self.g) < 3)
        nbr, nbc = self.H // B + 1, self.W // B + 1
        key = (self.r[m] // B) * nbc + self.c[m] // B
        gv = self.g[m]
        off = np.full(nbr * nbc, np.nan)
        order = np.argsort(key, kind='stable')
        ks, gs = key[order], gv[order]
        starts = np.r_[0, np.flatnonzero(np.diff(ks)) + 1]
        for s, e in zip(starts, np.r_[starts[1:], len(ks)]):
            if e - s >= 10:
                v = gs[s:e]
                q = np.percentile(v, 15)
                off[ks[s]] = np.median(v[(v >= q - 0.3) & (v <= q + 0.8)])
        off = off.reshape(nbr, nbc)
        glob = np.nanmedian(off) if np.isfinite(off).any() else 0.18
        for _ in range(4):
            miss = np.isnan(off)
            if not miss.any():
                break
            pad = np.pad(off, 1, constant_values=np.nan)
            stack = np.stack([pad[i:i + nbr, j:j + nbc] for i in range(3) for j in range(3)])
            with np.errstate(all='ignore'):
                fill = np.nanmedian(stack, axis=0)
            off[miss] = fill[miss]
        off[np.isnan(off)] = glob
        return ndi.median_filter(off, size=3, mode='nearest')

    def _offset_at(self, x, y, B=50):
        rr = (self.y1 - y) / B - 0.5
        cc = (x - self.x0) / B - 0.5
        return ndi.map_coordinates(self.off_grid, [rr, cc], order=1, mode='nearest')

    # ------------------------------------------------------------ return classes
    def pile_like(self):
        """Returns that could be on a pile surface."""
        top = np.full((self.H, self.W), -np.inf, np.float32)
        np.maximum.at(top, (self.r, self.c), self.h.astype(np.float32))
        above = ndi.maximum_filter(top, size=3)[self.r, self.c] > self.h + 2.0   # something over it within ~1.5 m
        chm = self.chm[self.r, self.c]
        same_tree = np.isfinite(chm) & (chm >= 2) & (np.abs(chm - self.h) <= 2.0)
        # local opacity over 5 x 5 m: of the returns there, the share that stop above ground (a pile)
        # rather than reach the ground or pass on through (crowns, brush)
        stop = (self.h > GROUND_H) & self.last
        reach = (self.h <= GROUND_H) | ((self.h > GROUND_H) & ~self.last)
        key = self.r.astype(np.int64) * self.W + self.c
        win = lambda m: ndi.uniform_filter(np.bincount(key[m], minlength=self.H * self.W).reshape(self.H, self.W)
                                           .astype(np.float32), size=5, mode='constant') * 25
        a, b = win(stop), win(reach)
        tot = (a + b)[self.r, self.c]
        opaque = (tot < 4) | (a[self.r, self.c] >= 0.6 * tot)
        return (self.h >= H_LO) & (self.h <= H_HI) & self.last & ~above & ~same_tree & opaque


# ---------------------------------------------------------------- clusters
def clusters(t, pl, min_pts=4):
    idx = np.flatnonzero(pl)
    if len(idx) < min_pts:
        return []
    xy = np.column_stack([t.x[idx], t.y[idx]])
    pairs = cKDTree(xy).query_pairs(LINK_R, output_type='ndarray')
    if len(pairs):
        pairs = pairs[np.abs(t.h[idx[pairs[:, 0]]] - t.h[idx[pairs[:, 1]]]) <= LINK_DZ]
    n = len(idx)
    A = coo_matrix((np.ones(len(pairs)), (pairs[:, 0], pairs[:, 1])), shape=(n, n)) if len(pairs) else coo_matrix((n, n))
    k, lab = connected_components(A, directed=False)
    order = np.argsort(lab, kind='stable')
    bounds = np.r_[0, np.flatnonzero(np.diff(lab[order])) + 1, n]
    out = []
    for s, e in zip(bounds[:-1], bounds[1:]):
        if e - s >= min_pts:
            out.append(idx[order[s:e]])
    return out


def disk(r):
    y, x = np.ogrid[-r:r + 1, -r:r + 1]
    return x * x + y * y <= r * r


def footprint(t, members, pl, grid_idx):
    """Footprint mask (1 m), surface heights, and the window it sits in, for one cluster."""
    cx, cy = t.x[members], t.y[members]
    pad = 8
    c0 = max(int(cx.min() - t.x0) - pad, 0)
    c1 = min(int(cx.max() - t.x0) + pad + 1, t.W)
    r0 = max(int(t.y1 - cy.max()) - pad, 0)
    r1 = min(int(t.y1 - cy.min()) + pad + 1, t.H)
    h, w = r1 - r0, c1 - c0
    core = np.zeros((h, w), bool)
    core[t.r[members] - r0, t.c[members] - c0] = True
    core = ndi.binary_fill_holes(ndi.binary_dilation(core, disk(2)))
    reach = ndi.binary_dilation(core, disk(3))
    # surface: the cluster's returns, plus ground-level returns in the window
    win = grid_idx(r0, r1, c0, c1)
    gsel = win[(t.h[win] <= GROUND_H)]
    pts = np.r_[members, gsel]
    vals = np.r_[t.h[members], np.minimum(t.h[gsel], 0.0)]
    gy, gx = np.mgrid[r0:r1, c0:c1]
    P = np.column_stack([t.x[pts] - t.x0 - c0, t.y1 - t.y[pts] - r0])   # window coordinates, cell centres at +0.5
    Q = np.column_stack([(gx - c0 + 0.5).ravel(), (gy - r0 + 0.5).ravel()])
    S = griddata(P, vals, Q, method='linear').reshape(h, w) if len(pts) >= 4 else np.full((h, w), np.nan)
    S = np.where(np.isfinite(S), S, 0.0)
    m = (S >= TOE) & reach
    lab, n = ndi.label(m)
    if n == 0:
        return None
    keep = np.unique(lab[core & m])
    keep = keep[keep > 0]
    m = ndi.binary_fill_holes(np.isin(lab, keep))
    S = np.clip(S, 0, None)
    return split(m, S), S, (r0, r1, c0, c1)


def split(m, S, dyn=1.0, min_area=15):
    """Split a footprint between peaks separated by a dip of at least dyn metres (neighbouring piles,
    a small tree beside a pile). Returns a list of masks."""
    from skimage.morphology import h_maxima
    from skimage.segmentation import watershed
    Ss = ndi.gaussian_filter(np.where(m, S, 0.0), 1.0)
    pk, n = ndi.label(h_maxima(np.where(m, Ss, 0.0), dyn) & m)
    if n <= 1:
        return [m]
    seg = watershed(-Ss, pk, mask=m)
    parts = [seg == i for i in range(1, n + 1)]
    big = [p for p in parts if p.sum() >= min_area]
    if len(big) <= 1:
        return [m]
    # crumbs too small to stand alone go back to the part they touch most
    for p in parts:
        if p.sum() < min_area:
            grown = ndi.binary_dilation(p)
            j = int(np.argmax([(grown & q).sum() for q in big]))
            big[j] = big[j] | p
    return big


class GridIndex:
    """Return indices by 1 m cell window."""

    def __init__(self, t):
        self.t = t
        key = t.r.astype(np.int64) * t.W + t.c
        self.order = np.argsort(key, kind='stable')
        self.keys = key[self.order]

    def __call__(self, r0, r1, c0, c1):
        out = []
        W = self.t.W
        for rr in range(r0, r1):
            a = np.searchsorted(self.keys, rr * W + c0)
            b = np.searchsorted(self.keys, rr * W + c1)
            if b > a:
                out.append(self.order[a:b])
        return np.concatenate(out) if out else np.zeros(0, np.int64)


# ---------------------------------------------------------------- features
def measure(t, members, pl, gi, keep_geom=True):
    """Measures and evidence for each pile a cluster holds (usually one)."""
    fp = footprint(t, members, pl, gi)
    if fp is None:
        return []
    parts, S, (r0, r1, c0, c1) = fp
    out = []
    for m in parts:
        sub = members[m[t.r[members] - r0, t.c[members] - c0]]
        if len(sub) < 3:
            continue
        row = measure_part(t, sub, m, S, (r0, r1, c0, c1), gi, keep_geom)
        if row is not None:
            row['_members'] = sub
            out.append(row)
    return out


def measure_part(t, members, m, S, box, gi, keep_geom):
    r0, r1, c0, c1 = box
    area = float(m.sum())
    if area < 20:
        return None
    win = gi(r0, r1, c0, c1)
    inside = m[t.r[win] - r0, t.c[win] - c0]
    ins = win[inside]
    rr, cc = np.nonzero(m)
    cy, cx = rr.mean(), cc.mean()
    X, Y = t.x0 + c0 + cx + 0.5, t.y1 - (r0 + cy) - 0.5
    d2 = (rr - cy) ** 2 + (cc - cx) ** 2
    rmax = math.sqrt(d2.max()) + 0.5
    cov = np.cov(np.vstack([rr, cc])) if len(rr) > 2 else np.eye(2)
    ev = np.sort(np.linalg.eigvalsh(cov))
    hS = S[m]
    hp = t.h[members]
    n_first = int(t.first[ins].sum())
    n_ret = int(len(ins))
    stopped = ins[t.last[ins] & (t.h[ins] > GROUND_H)]
    n_gnd = int((t.h[ins] <= GROUND_H).sum())
    n_pass = int(((t.h[ins] > GROUND_H) & ~t.last[ins]).sum())
    n_over = int((t.h[ins] > (S[t.r[ins] - r0, t.c[ins] - c0] + 2.5)).sum())
    # ring 5-20 m around the footprint: how often pulses reach the ground there
    R0, R1, C0, C1 = max(r0 - 14, 0), min(r1 + 14, t.H), max(c0 - 14, 0), min(c1 + 14, t.W)
    mb = np.zeros((R1 - R0, C1 - C0), bool)
    mb[r0 - R0:r1 - R0, c0 - C0:c1 - C0] = m
    ring = ndi.binary_dilation(mb, disk(20)) & ~ndi.binary_dilation(mb, disk(5))
    wb = gi(R0, R1, C0, C1)
    rsel = wb[ring[t.r[wb] - R0, t.c[wb] - C0]]
    r_first = t.first[rsel].sum()
    gnd_share_ring = float(((t.h[rsel] <= GROUND_H) & t.last[rsel]).sum() / max(r_first, 1))
    tall_ring = float((t.h[rsel] > 5).sum() / max(len(rsel), 1))
    pulse_dens = float(r_first / max(ring.sum(), 1))
    exp_gnd = gnd_share_ring * max(n_first, 1)
    chm = t.chm[r0:r1, c0:c1][m]
    chm = chm[np.isfinite(chm)]
    gi_int = t.inten[rsel][t.h[rsel] <= GROUND_H]
    rd = np.sqrt((t.c[members] - c0 - cx) ** 2 + (t.r[members] - r0 - cy) ** 2)
    dome = float(np.corrcoef(rd, hp)[0, 1]) if len(hp) >= 5 and rd.std() > 0 and hp.std() > 0 else 0.0
    vol = float(hS.sum())
    row = {
        'x': round(float(X), 1), 'y': round(float(Y), 1),
        'area_m2': area, 'diam_ft': round(2 * math.sqrt(area / math.pi) / FT, 1),
        'hmax_m': round(float(np.percentile(hS, 99)), 2), 'h90_m': round(float(np.percentile(hp, 90)), 2),
        'vol_m3': round(vol, 1),
        'n_ret': n_ret, 'n_pl': int(len(members)), 'n_first': n_first,
        # evidence, normalised by the pulses that hit the footprint
        'stop_share': float(len(stopped) / max(n_first, 1)),
        'single_share': float((t.nr[ins] == 1).mean()) if n_ret else 0.0,
        'gnd_share': float(n_gnd / max(n_first, 1)),
        'gnd_deficit_z': float((exp_gnd - n_gnd) / math.sqrt(max(exp_gnd, 1.0))),
        'pass_share': float(n_pass / max(n_ret, 1)),
        'over_share': float(n_over / max(n_ret, 1)),
        'gnd_share_ring': gnd_share_ring, 'tall_ring': tall_ring, 'pulse_dens': pulse_dens,
        'pl_dens': float(len(members) / area),
        'chm22_p50': float(np.median(chm)) if len(chm) else 0.0,
        'chm22_open': float((chm < 1).mean()) if len(chm) else 1.0,
        'chm22_minus_h': float((np.median(chm) if len(chm) else 0.0) - np.median(hp)),
        'compact': float(area / (math.pi * rmax * rmax)),
        'elong': float(math.sqrt(ev[1] / max(ev[0], 1e-6))),
        'h_ratio': float(np.percentile(hp, 90) / (2 * math.sqrt(area / math.pi))),
        'h_cv': float(hp.std() / max(hp.mean(), 1e-6)),
        'dome': dome,
        'int_ratio': float(np.median(t.inten[members]) / max(np.median(gi_int), 1.0)) if len(gi_int) else 1.0,
        'snow_m': round(float(np.median(t._offset_at(np.array([X]), np.array([Y])))), 2),
    }
    if keep_geom:
        row['_mask'] = (m, r0, c0)
    return row


def mask_rings(t, m, r0, c0):
    """Footprint outline(s) in map coordinates, as the dashboard draws them."""
    import contourpy
    pad = np.pad(m, 1).astype(float)
    rings = []
    for ln in contourpy.contour_generator(z=pad).lines(0.5):
        if len(ln) >= 4:
            rings.append([[round(t.x0 + c0 + px - 1 + 0.5, 1), round(t.y1 - (r0 + py - 1) - 0.5, 1)] for px, py in ln])
    return rings


def detect(t, min_diam_ft=25.0):
    """All clusters on a tile with their measures and evidence; centres inside the tile's own 2 km."""
    kx, ky = tile_origin(t.tile)
    pl = t.pile_like()
    gi = GridIndex(t)
    rows = []
    for members in clusters(t, pl):
        # too small to be a 25 ft pile even with its toe
        if np.ptp(t.x[members]) < 4 and np.ptp(t.y[members]) < 4:
            continue
        for row in measure(t, members, pl, gi):
            if row['diam_ft'] < min_diam_ft or row['area_m2'] > 8000:
                continue
            if not (kx <= row['x'] < kx + TILE and ky <= row['y'] < ky + TILE):
                continue
            rows.append(row)
    return rows, pl, gi


FEATURES = ['stop_share', 'single_share', 'gnd_share', 'gnd_deficit_z', 'pass_share', 'over_share',
            'gnd_share_ring', 'tall_ring', 'pl_dens', 'chm22_p50', 'chm22_open', 'chm22_minus_h',
            'compact', 'elong', 'h_ratio', 'h_cv', 'dome', 'int_ratio', 'h90_m']


# ---------------------------------------------------------------- known piles
def known_label_grid(t, piles):
    """1 m grid of mapped pile ids on this tile (0 = none)."""
    from matplotlib.path import Path
    lab = np.zeros((t.H, t.W), np.int32)
    for p in piles:
        g = json.loads(p['geom']) if isinstance(p['geom'], str) else p['geom']
        a = np.vstack(g)
        if a[:, 0].max() < t.x0 or a[:, 0].min() > t.x1 or a[:, 1].max() < t.y0 or a[:, 1].min() > t.y1:
            continue
        c0, c1 = max(int(a[:, 0].min() - t.x0), 0), min(int(a[:, 0].max() - t.x0) + 2, t.W)
        r0, r1 = max(int(t.y1 - a[:, 1].max()), 0), min(int(t.y1 - a[:, 1].min()) + 2, t.H)
        cc, rr = np.meshgrid(np.arange(c0, c1), np.arange(r0, r1))
        pts = np.column_stack([t.x0 + cc.ravel() + 0.5, t.y1 - rr.ravel() - 0.5])
        ins = np.zeros(len(pts), bool)
        for ring in g:
            ins ^= Path(np.asarray(ring)).contains_points(pts)
        lab[rr.ravel()[ins], cc.ravel()[ins]] = int(p['pile_id'])
    return lab
