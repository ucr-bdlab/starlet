# Starlet Guide: using it in a project & styling the tiles

A practical cookbook for two things the [README](../README.md) only touches on:

1. **[Using Starlet inside another project](#part-1--using-starlet-inside-another-project)** — as a build step, from the Python API, or as an embedded tile server.
2. **[Styling the vector tiles with MapLibre GL](#part-2--styling-the-tiles-with-maplibre-gl)** — categorical, gradient, and label recipes you can paste into a map.

> New to Starlet? Start with the [README](../README.md) (`pip install starlet`, two-command quick start). Full CLI/config reference: [CONFIGURATION.md](CONFIGURATION.md). Full Python API: [PUBLIC_API.md](PUBLIC_API.md).

---

# Part 1 — Using Starlet inside another project

Starlet is both a CLI (`starlet …`) and an importable library (`import starlet`). Pick whichever fits how your project is wired together.

## 1a. As a build step (shell / Makefile)

The simplest integration: shell out to the CLI from your build tooling. `build` runs the whole pipeline (partition → histograms → vector tiles) into a dataset directory.

```bash
# build.sh — turn a data file into a served dataset
set -euo pipefail
starlet build --input data/counties.parquet --outdir datasets/counties --zoom 8
starlet info  --dir  datasets/counties          # sanity-check what got built
```

```makefile
# Makefile
DATA    := data/counties.parquet
DATASET := datasets/counties

$(DATASET)/histograms/global_prefix.npy: $(DATA)
	starlet build --input $< --outdir $(DATASET) --zoom 8

tiles: $(DATASET)/histograms/global_prefix.npy   ## rebuild tiles when the source changes
serve: tiles
	starlet serve --dir datasets --port 8765
.PHONY: tiles serve
```

Reuse-once settings (worker count, partition size, zoom, …) can live in a `starlet.toml` next to your project instead of being repeated on every command — see [CONFIGURATION.md](CONFIGURATION.md).

## 1b. From Python via subprocess

If you just want to trigger a build from Python without importing the geo stack:

```python
import subprocess

subprocess.run(
    ["starlet", "build", "--input", "data/counties.parquet",
     "--outdir", "datasets/counties", "--zoom", "8"],
    check=True,
)
```

## 1c. Via the Python API (recommended for apps)

Every CLI subcommand maps to a public function in `starlet`. Imports inside these functions are lazy, so `import starlet` stays cheap.

```python
import starlet

# Full pipeline — returns (TileResult, MVTResult, pmtiles_path)
tile_result, mvt_result, _ = starlet.build(
    input="data/counties.parquet",
    outdir="datasets/counties",
    zoom=8,
    threshold=0,          # generate every non-empty tile (no dropped edges)
)
print(tile_result.num_files, "parquet tiles;", mvt_result.tile_count, "MVT tiles")

# Or run the stages separately
starlet.tile(input="data/counties.parquet", outdir="datasets/counties")
starlet.generate_mvt(tile_dir="datasets/counties", zoom=8)

# One-file archive (PMTiles) for static hosting / CDN
starlet.export_pmtiles(mvt_dir="datasets/counties/mvt",
                       output_path="datasets/counties/tiles.pmtiles")
```

Key signatures (all keyword-only after the first args):

```python
build(input, outdir, *, zoom=None, threshold=None, partition_size=None,
      pmtiles=None, feature_capacity=None, extent=None, buffer=None, **tile_kwargs)
generate_mvt(tile_dir, *, zoom=None, threshold=None, feature_capacity=None,
             extent=None, buffer=None, pmtiles=None, outdir=None)
create_app(data_dir, cache_size=None, extent=None, buffer=None)  # -> Flask app
export_pmtiles(mvt_dir, output_path, tile_type=None, compression=None)  # -> path
```

## 1d. Embed the tile server in your own app

`create_app()` returns a plain **Flask** app, so you can run it under any WSGI server or mount it inside a larger app.

**Standalone with gunicorn:**

```python
# wsgi.py
import starlet
app = starlet.create_app(data_dir="datasets")
```

```bash
gunicorn -w 4 -b 0.0.0.0:8000 wsgi:app
```

**Mounted under an existing app** (e.g. serve tiles at `/tiles/…`):

```python
from werkzeug.middleware.dispatcher import DispatcherMiddleware
import starlet
from myproject import app as main_app          # your existing Flask/WSGI app

tiles = starlet.create_app(data_dir="datasets")
application = DispatcherMiddleware(main_app, {"/tiles": tiles})
```

Tiles are then available at `GET /tiles/<dataset>/<z>/<x>/<y>.mvt`. The server has a
three-tier lookup — in-memory cache → pre-generated `.mvt`/PMTiles on disk →
generated on the fly from the Parquet tiles — so it serves zoom levels you never
pre-generated.

## 1e. Read tiles & features programmatically

You don't need the HTTP server to get data out of a dataset.

```python
import starlet

# What datasets exist, and what's in one?
starlet.list_datasets("datasets")                     # -> ['counties', ...]
md = starlet.get_dataset_metadata("datasets/counties")
md["bbox"], md["zoom_levels"], md["mvt_tile_count"]   # bbox, [0..8], 548, ...

# Raw vector tile bytes for a z/x/y (generates on the fly if not pre-built)
mvt_bytes = starlet.get_tile("datasets/counties", 5, 6, 13)   # -> bytes

# Pull features intersecting an area as GeoDataFrame batches.
# geometry can be a [minx, miny, maxx, maxy] bbox, a GeoJSON dict, or a shapely geom.
for gdf in starlet.query_dataset("datasets/counties",
                                 [-106, 30, -101, 33],        # west Texas bbox
                                 geometry_crs="EPSG:4326"):
    print(len(gdf), "features:", list(gdf["NAME"]))
```

`get_dataset_metadata` returns: `name, path, exists, size_bytes, parquet_tile_count,
parquet_has_bbox, parquet_crs, bbox, zoom_levels, mvt_tile_count, has_histograms,
histogram_resolution, has_mvt, has_pmtiles, pmtiles_path, has_stats, missing`.

## 1f. Ingest datasets on demand

Build a new dataset from a source file at runtime (e.g. an upload):

```python
import starlet

# Blocking build into your datasets root
starlet.add_dataset("uploads/new.geojson", "datasets",
                    name="new_layer", overwrite=True, zoom=10)

# Non-blocking: returns a handle you can poll (see AsyncDatasetHandle)
handle = starlet.add_dataset_async("uploads/big.parquet", "datasets", name="big")
```

## 1g. In CI

Build tiles as a pipeline artifact:

```yaml
# .github/workflows/tiles.yml
- run: pip install starlet
- run: starlet build --input data/counties.parquet --outdir dist/counties --pmtiles
- uses: actions/upload-artifact@v4
  with: { name: tiles, path: dist/counties/tiles.pmtiles }
```

---

# Part 2 — Styling the tiles with MapLibre GL

Starlet serves standard **Mapbox Vector Tiles**, so any MVT renderer works. The
examples below use [MapLibre GL JS](https://maplibre.org/). Two facts you need for
every style:

- **Tile URL:** `http://<host>/<dataset>/{z}/{x}/{y}.mvt`
- **Source layer:** every Starlet tile puts its features in a single layer named **`layer0`** — so every style layer needs `"source-layer": "layer0"`.

Features carry your dataset's original attributes as properties (`starlet info --dir …`
or `get_dataset_metadata` to see them; or inspect a tile). Pick the layer **type** to
match the geometry: `fill`/`line` for polygons, `line` for lines, `circle` for points.
If a dataset mixes geometry types, split them with
`"filter": ["==", ["geometry-type"], "Polygon"]`.

## The base map

A minimal, self-contained MapLibre page pointing at a running `starlet serve`. Drop
your style layers into the `layers` array.

```html
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <link href="https://unpkg.com/maplibre-gl@3.6.0/dist/maplibre-gl.css" rel="stylesheet" />
  <script src="https://unpkg.com/maplibre-gl@3.6.0/dist/maplibre-gl.js"></script>
  <style>html,body,#map{height:100%;margin:0}</style>
</head>
<body>
  <div id="map"></div>
  <script>
    const DATASET = "counties";                 // your dataset name
    const HOST = location.origin;               // where starlet serve is running
    const map = new maplibregl.Map({
      container: "map",
      center: [-100, 38], zoom: 4,
      style: {
        version: 8,
        // Required for text labels (Part 2c). Self-host for production.
        glyphs: "https://demotiles.maplibre.org/font/{fontstack}/{range}.pbf",
        sources: {
          osm: { type: "raster", tiles: ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"], tileSize: 256, attribution: "© OpenStreetMap" },
          starlet: { type: "vector", tiles: [`${HOST}/${DATASET}/{z}/{x}/{y}.mvt`], minzoom: 0, maxzoom: 14 }
        },
        layers: [
          { id: "basemap", type: "raster", source: "osm" },
          // 👇 paste the recipe layers from below here
        ]
      }
    });
  </script>
</body>
</html>
```

## 2a. Categorical — color by a category attribute

Use a MapLibre **`match`** expression to map discrete attribute values to colors. Here,
a land-cover polygon layer colored by its `GEN_DESCRI` class (the last color is the
fallback for anything unmatched):

```js
{
  id: "landcover-fill",
  type: "fill",
  source: "starlet",
  "source-layer": "layer0",
  paint: {
    "fill-color": [
      "match", ["get", "GEN_DESCRI"],
      "Wetlands",     "#4292c6",
      "Woodland",     "#238b45",
      "Grassland",    "#addd8e",
      "Desert",       "#fee391",
      "Agriculture",  "#fdae6b",
      "Urban",        "#969696",
      /* fallback */  "#dddddd"
    ],
    "fill-opacity": 0.7,
    "fill-outline-color": "#00000022"
  }
}
```

Same idea for admin boundaries colored by a category — e.g. provinces by `type`
(`["match", ["get", "type"], "State", "#…", "Province", "#…", "#ccc"]`), or counties
by state FIPS `["get", "STATEFP"]`.

## 2b. Gradient / choropleth — color by a numeric attribute

Use **`interpolate`** to blend colors across a numeric range. Here counties shaded by
land area `ALAND` (square meters); stops go from small → large:

```js
{
  id: "counties-choropleth",
  type: "fill",
  source: "starlet",
  "source-layer": "layer0",
  paint: {
    "fill-color": [
      "interpolate", ["linear"], ["get", "ALAND"],
      0,       "#fff7ec",
      1.0e9,   "#fdd49e",
      5.0e9,   "#fc8d59",
      2.0e10,  "#d7301f",
      6.0e10,  "#7f0000"
    ],
    "fill-opacity": 0.75
  }
}
```

Tips:
- For skewed data (population, area), interpolate on a log with
  `["interpolate", ["linear"], ["log10", ["max", 1, ["get", "ALAND"]]], 6, "#fff7ec", 10.8, "#7f0000"]`.
- Prefer hard class breaks? Use **`step`** instead of `interpolate`:
  `["step", ["get", "ALAND"], "#fff7ec", 1e9, "#fdd49e", 5e9, "#fc8d59", 2e10, "#d7301f"]`.
- Data-driven width/radius works the same way — e.g. `"line-width": ["interpolate", ["linear"], ["zoom"], 4, 0.5, 12, 3]`.

## 2c. Labels — draw text from an attribute

Labels are a **`symbol`** layer with a `text-field`. MapLibre needs a `glyphs` URL in
the style root (set in the base map above) **and** the `text-font` you name must exist
in that glyph source. Missing glyphs is the #1 reason labels don't appear.

```js
{
  id: "county-labels",
  type: "symbol",
  source: "starlet",
  "source-layer": "layer0",
  minzoom: 5,
  layout: {
    "text-field": ["get", "NAME"],              // e.g. "Franklin"
    "text-font": ["Open Sans Regular"],         // must exist in the glyphs source
    "text-size": ["interpolate", ["linear"], ["zoom"], 5, 10, 10, 14],
    "text-anchor": "center",
    "text-allow-overlap": false                 // let MapLibre de-clutter
  },
  paint: {
    "text-color": "#222",
    "text-halo-color": "#ffffff",
    "text-halo-width": 1.4
  }
}
```

- On polygons, MapLibre places one label near each feature's center automatically.
- Combine strings with `format`: `["format", ["get", "NAME"], {}, "\n", {}, ["get", "STATEFP"], {"font-scale": 0.8}]`.
- The `glyphs` URL above is MapLibre's public demo endpoint (has *Open Sans* / *Noto
  Sans*). For production, host your own font PBFs and point `glyphs` at them.

## 2d. Putting it together

A common polygon style is three layers stacked — gradient fill, crisp outline, labels:

```js
layers: [
  { id: "basemap", type: "raster", source: "osm" },
  /* fill   */ { id: "fill",   type: "fill",   source: "starlet", "source-layer": "layer0",
                 paint: { "fill-color": ["interpolate", ["linear"], ["get", "ALAND"],
                          0, "#fff7ec", 2e10, "#d7301f"], "fill-opacity": 0.7 } },
  /* stroke */ { id: "stroke", type: "line",   source: "starlet", "source-layer": "layer0",
                 paint: { "line-color": "#00000055", "line-width": 0.5 } },
  /* label  */ { id: "label",  type: "symbol", source: "starlet", "source-layer": "layer0",
                 minzoom: 5,
                 layout: { "text-field": ["get", "NAME"], "text-font": ["Open Sans Regular"], "text-size": 12 },
                 paint: { "text-color": "#222", "text-halo-color": "#fff", "text-halo-width": 1.4 } }
]
```

Interactivity (click to inspect) is standard MapLibre — `map.on("click", "fill", e =>
{ … e.features[0].properties … })`.

---

**See also:** [README](../README.md) · [CONFIGURATION.md](CONFIGURATION.md) · [PUBLIC_API.md](PUBLIC_API.md) · [DEPLOYMENT.md](DEPLOYMENT.md) · [DEVELOPMENT.md](../DEVELOPMENT.md)
