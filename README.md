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
| `data/hillshade.jpg` | 2022 hillshade background, 25 m |

Coordinates are NAD83 / UTM zone 10N (metres).

## Method

- Piles are opaque mounds in the Feb 4, 2026 ASO lidar that were not there in 2022, measured against the 2022 TNF 1 m bare-earth DEM and canopy height model.
- Volume is gross (including air space), the 2026 surface minus 2022 bare earth over each footprint.
- Dry tons = volume × packing ratio × wood density (Hardy 1996, PNW-GTR-364). Defaults: 0.20 and 380 kg/m³.
- Cost defaults come from the Siskiyou County biochar demonstration (2020–21), Earth Foundries (2024) and the Central Sierra feedstock report (2023). They are planning estimates, not quotes.

## Limits

- Only piles under the Feb 2026 flight are counted; unit area without coverage is shaded on the map.
- The largest mapped pile (Sleighville unit 265) looks like a landing pad rather than slash and may overstate tons.
- Volumes carry a measured sampling error (median about ±7% for piles over 40 ft) plus unmeasured bias from rough pile tops, ground disturbance and the toe cutoff.

## Publish with GitHub Pages

Repository **Settings → Pages → Build and deployment → Deploy from a branch**, branch `main`, folder `/ (root)`. The site appears at `https://<user>.github.io/<repo>/` within a minute or two.

To view locally, serve the folder (the page fetches its data, so opening `index.html` directly does not work):

```
python -m http.server 8000
```

then open http://localhost:8000.
