# Trapper area piles

Live site: https://jburdick-m.github.io/trapper-area-piles/

Interactive map and disposal planner for the large burn piles left by fuels treatments in the Pike-Magnus, Graveyard, Sleighville and Alaska Peak units, Tahoe National Forest.

The page maps 205 piles 40 ft and wider. Sliders set packing ratio, wood density, grind-and-haul and carbonator rates, and the totals, project table and map update as they move. The "all project ground" figures add each project's unflown share of unit acres to its flown totals (flown × (1 + unflown fraction)), a simple allowance for piles outside the 2026 lidar.

## Data

| File | Contents |
| --- | --- |
| `data/piles.json` | One row per pile: project, unit, footprint, diameter, height, gross volume and 95% sampling interval, footprint outline |
| `data/units.json` | Treatment units with acres and the share covered by the 2026 lidar |
| `data/layers.json` | Project boundaries, lidar coverage, National Forest land, analysis extent |
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

## Limits

- Only piles under the Feb 2026 flight are counted; ground without coverage is darkened and hatched on the map.
- The largest mapped pile (Sleighville unit 265) looks like a landing pad rather than slash and may overstate tons.
- Volumes carry a measured sampling error (median about ±7% for piles over 40 ft) plus unmeasured bias from rough pile tops, ground disturbance and the toe cutoff.

## Publish with GitHub Pages

Repository **Settings → Pages → Build and deployment → Deploy from a branch**, branch `main`, folder `/ (root)`. The site appears at `https://<user>.github.io/<repo>/` within a minute or two.

To view locally, serve the folder (the page fetches its data, so opening `index.html` directly does not work):

```
python -m http.server 8000
```

then open http://localhost:8000.
