# Trapper area piles

Live site: https://jburdick-m.github.io/trapper-area-piles/

Interactive map and disposal planner for the large burn piles left by fuels treatments in the Pike-Magnus, Graveyard, Sleighville and Alaska Peak units, Tahoe National Forest.

The page maps 210 piles 40 ft and wider: 205 from the first pass and 5 the automated pass below found that it missed. Sliders set packing ratio, wood density, grind-and-haul and carbonator rates, and the totals, project table and map update as they move. The "all project ground" figures add each project's unflown share of unit acres to its flown totals (flown × (1 + unflown fraction)), a simple allowance for piles outside the 2026 lidar.

## Data

| File | Contents |
| --- | --- |
| `data/piles.json` | One row per pile: project, unit, footprint, diameter, height, gross volume and 95% sampling interval, footprint outline. Rows with `added: true` (ids 1001+) come from the automated pass and carry a 90% volume range, pile probability, stability and pulse count |
| `data/piles_30_40ft.json` | The 13 piles 30–40 ft the automated pass found, same fields. Not shown on the page: too incomplete to count yet |
| `data/units.json` | Treatment units with acres and the share covered by the 2026 lidar |
| `data/layers.json` | Project boundaries, Feb 2026 lidar footprint (from the ASO tiles: 25 m cells with at least 0.5 points per m², gaps up to 200 m filled), National Forest land, analysis extent |
| `data/assumptions.json` | Slider defaults and ranges, with their sources |
| `data/hillshade.jpg` | Hillshade background from USGS 3DEP, 25 m, covering all four projects |
| `data/piles/pile_<id>.webp` | Lit 3-D view of each pile at true scale with a chip van and tractor beside it, shown when a pile is clicked |

Coordinates are NAD83 / UTM zone 10N (metres).

## Method

- Piles are opaque mounds in the Feb 4, 2026 ASO lidar that were not there in 2022, measured against the 2022 TNF 1 m bare-earth DEM and canopy height model.
- Volume is gross (including air space), the 2026 surface minus 2022 bare earth over each footprint.
- Dry tons = volume × packing ratio × wood density (Hardy 1996, PNW-GTR-364). Defaults: 0.20 and 380 kg/m³.
- Cost defaults come from the Siskiyou County biochar demonstration (2020–21), Earth Foundries (2024), the Central Sierra feedstock report (2023), Yoshioka et al. (2017) and CAL FIRE (2023). They are planning estimates, not quotes.

### What the costs include

| Grind and haul | Carbonator |
| --- | --- |
| Grind and load ($/dry t), chip-van haul (round trip + 1 h load/unload), contractor markup | Contract rate per run-hour (machine, operator, feeding loader, margin) |
| Grinder and loader move-in per project | Carbonator and loader move-in per project, per machine |
| Fire support (water tender, patrol) per working day | Fire support per machine per working day |
| Plant gate payment or tipping fee (default: no credit) | Biochar value not credited |

Grind-and-haul working days are set by the slower of the grinder (dry t/h × hours) and the trucks (whole round trips per 11 h driver day). Not included: agency contract preparation and oversight, road maintenance or reconstruction for chip vans, permits, and cleanup of pile sites.

### Chip destinations

Routed road miles from the units (OpenStreetMap routing):

| Destination | One-way |
| --- | --- |
| Rio Bravo Rocklin, Lincoln (default) | 69 mi |
| Sierra Pacific cogeneration, Quincy | 75 mi |
| Woodland Biomass, Woodland | 106 mi |
| Buena Vista Biomass, Ione | 112 mi |
| Honey Lake Power, Wendel | 150 mi |

### Pile views

`tools/pile_views.py` draws the image shown when a pile is clicked (no title; the panel heading names the pile). It grids the Feb 4, 2026 returns (`gs://aso-august-w1/pc_work_10212026/pike_view2/tiles`) against 2022 bare earth from the USGS CA_SierraNevada 2022 ground returns (public EPT), inside each pile's footprint. The 2026 points are shifted onto the 2022 ground by the Feb flight's ground offset (+0.18 m, as in the pile mapping), standing trees are filtered out, and heights are capped at the mapped pile height. The chip van is a 53 ft × 8.5 ft × 13.5 ft trailer behind a day-cab tractor, about 69 ft overall. Tree filtering and smoothing make the drawn surface a little low: image volumes run a median 8% under `vol_m3`. The 2022 ground for each pile is cached in `gs://aso-august-w1/pc_work_10212026/pile_views/ground22/` so the render runs from the bucket alone.

`tools/pile_views_vm.sh` renders all piles on a GCE VM in us-west1 (same region as the bucket) and writes them to `gs://aso-august-w1/pc_work_10212026/pile_views/out/`. Copy them into `data/piles/` and bump `IMG_V` in `index.html`.

Chip-van loads in the pile panel are the pile's scenario dry tons divided by the load size slider.

### Automated pass: piles 30 ft and up

The Feb 2026 flight is sparse where the piles are (0.8–2.2 returns per m² overall, about 0.3 per m² on 40–60 ft piles, half the 1 m cells empty), so `tools/pilefind.py` works on returns rather than a raster:

- Height of every return above 2022 bare earth (USGS CA_SierraNevada_B22 1 m DEM), less a smooth snow and datum offset from 2026 ground returns. ASO classed about half of the returns on pile tops as ground, so the offset comes from the lower mode of ground returns in 50 m blocks.
- Pile-like returns: 0.5–12 m up, the last return of their pulse, nothing more than 2 m higher within about 1.5 m, mostly opaque surroundings (5 × 5 m), and not a 2022 tree (`NY_22_CHM.tif`) of the same height. Pile tops are 71–80% single returns, against about 35% elsewhere.
- Pile-like returns within 3 m are joined into clusters; footprints follow a surface through them and the surrounding ground returns down to 0.5 m, and are split at saddles at least 1 m deep (neighbouring piles, a tree beside a pile).
- `tools/pile_detect.py` scores each cluster with a gradient-boosting classifier on density-independent evidence (share of pulses stopped above ground, ground returns seen versus expected, pass-through and overhang, 2022 canopy, shape, height profile). It was trained on 7 test tiles holding 148 mapped piles, at full density and thinned to 50, 30, 20 and 12% of pulses. Mapped piles are positives at any size; unmapped clusters are negatives only at 40 ft and up, where the first pass should be complete.
- A pile is added only if it also has a dome profile, little pass-through or overhang, a clear ground deficit, is at most 0.4 times as tall as it is wide, has at least 20 pulses on it, is found again in at least 7 of 10 half-density redraws, sits inside a unit, and touches no mapped pile. Each rule keeps about 90% of confirmed mapped piles.
- Footprint area and volume are corrected for this detector reading low on sparsely sampled piles (detected/mapped medians of 0.73–0.93 for area and 0.69–0.91 for volume, by pulses on the pile), with a 90% range from the spread of that ratio.

Validation on the test tiles: the cluster stage finds 146 of the 148 mapped piles; the full rule set keeps 63% of them (77% of piles with 200+ pulses, 12–35% of piles with 20–50 pulses) and makes no detections on a pile-free tile. Reviewed by eye from return profiles, 17 of the 18 additions are clean mounds 2–4 m tall; pile 1001 (Graveyard unit 103, 110 ft) is a large irregular mound with a lower tail, possibly a landing deck or two piles together, and is the one to field-check first.

`tools/pile_detect_vm.sh` runs the whole pass on a us-west1 VM (about 5 minutes on 22 cores); inputs, per-tile results and the training table are in `gs://aso-august-w1/pc_work_10212026/pile_detect/`, and the validation scripts and scored clusters in its `validation/` folder.

## Limits

- Only piles under the Feb 2026 flight are counted; ground without coverage is darkened and hatched on the map.
- The largest mapped pile (Sleighville unit 265) looks like a landing pad rather than slash and may overstate tons.
- Volumes carry a measured sampling error (median about ±7% for piles over 40 ft) plus unmeasured bias from rough pile tops, ground disturbance and the toe cutoff.
- Piles under 40 ft are not counted. The automated pass found 13 at 30–40 ft (`data/piles_30_40ft.json`), but at the pulse counts small piles get in this flight it finds roughly 15–50% of them, so the real number is likely several times that. Ground data or a denser flight would make them usable.

## Publish with GitHub Pages

Repository **Settings → Pages → Build and deployment → Deploy from a branch**, branch `main`, folder `/ (root)`. The site appears at `https://<user>.github.io/<repo>/` within a minute or two.

To view locally, serve the folder (the page fetches its data, so opening `index.html` directly does not work):

```
python -m http.server 8000
```

then open http://localhost:8000.
