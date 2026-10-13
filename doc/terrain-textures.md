# Terrain textures: `.ter` cells, materials and UVs

A `.mesh` carries **no terrain reference** — unlike a DSF, which names a terrain definition
per patch — so the orthophotos are rebuilt from the tile itself. Every ortho cell `.ter`
that Ortho4XP writes declares its own footprint:

```
LOAD_CENTER <lat> <lon> <size_m> <tex_px>     the cell this texture covers
BASE_TEX_NOWRAP ../textures/<cell>.dds        the orthophoto, mapped once
LOAD_CENTER_BORDER <lat> <lon> <size_m> 512   sea overlay border (optional)
BORDER_TEX ../textures/<cell>.png
```

So the cells are a spatial index: each face is assigned to the cell whose footprint
contains its centroid, gets that cell's material, and is UV-mapped once over that footprint
(cell centre at 0.5, 0.5, footprint edges at 0 and 1) — which is what X-Plane does when it
renders the same tile. The tile's `terrain/` folder is found next to the `.mesh`
automatically; override it with *Terrain folder*.

The scan produces **one cell per footprint**, with the `_sea_overlay.ter` variant winning over
the plain `.ter` for the same footprint (that is the definition Ortho4XP's own DSF uses for
those cells, and the overlay adds `WET`, `NO_SHADOW` and a border mask). `.ter` files the
importer cannot place are collected and reported instead of being dropped silently.
`terrain_tiles.uv_center()` is the documented `LOAD_CENTER` mapping
(`tercodec.uv_from_center`, shared with the DSF addon) — `PROJECTED` cells are the ones that
declare a repeating map instead.

## Details that matter on real tiles

* **The tiling comes from the observed lattice, not from `size_m`.** Measured on `+27+089`,
  the declared size converted to degrees is ~0.1 % narrow in longitude and ~0.6 % tall in
  latitude versus the actual cell-centre spacing, so metre-derived footprints leave
  accumulating bands of untextured faces along the borders of every finer-zoom patch (first
  attempt: only 83 % of faces covered). Each cell therefore keeps the area up to the midline
  between it and its same-zoom-level neighbours, which tiles exactly, and the cells are
  layered coarse → fine so a ZL19 patch overwrites the ZL17 grid beneath it.
* **Each bin keeps every cell whose footprint touches it**, and the assignment takes the
  finest of those that *really* contains the point. Keeping only the finest cell per bin
  (the first version) silently drops the coarse cell underneath a fine patch: on `+27+089`
  that stranded 637 faces in bins owned by ZL19 cells that did not contain them, and left
  the 5 ZL18 cells underneath used by no face at all, so their textures never reached
  Blender.
* **`_sea_overlay.ter` wins** over the plain `.ter` with the same footprint — see above.
* A tile that mixes zoom levels is the normal case: `+27+089` has 963 `.ter` files → 707
  distinct footprints (618 at ZL17, 27 at ZL18, 62 at ZL19; 256 of them sea overlays).
* Faces with no cell get a red `ter_none_uncovered` slot instead of being silently painted
  with a neighbour's texture (637 of 2.48 M faces on `+27+089`, 0.03 %, all at the tile edge).
* **Material slots:** a face has exactly one material index, so *Material slot per triangle
  attribute* is skipped when textures are imported — the orthophoto wins. `mesh_attr` is
  still written and still drives the export.
* The panel reports what happened (`cells used by faces`, `faces textured / without a cell`,
  `texture(s) loaded`, `sea overlay cell(s)`, the UV layer and loop counts). If the counts
  look wrong, the `.ter` folder that was scanned is printed right below them.

## Layers, and which ones are wired up

`base` textures drive Base Color, a `NORMAL_TEX` gets a normal-map node, and the remaining
layers (`lit`, `composite`, `border`) are imported as labelled but **unconnected** nodes:
blending them needs the per-vertex factor that lives in the DSF, which a `.mesh` does not
carry, so wiring them to an invented factor would be a lie.

## Memory (`tex_limit`, `tex_max_px`)

There is no cheap way to display 1000 orthophotos at once, so both knobs are explicit:

* **Blender loads lazily.** `bpy.data.images.load()` alone costs ~0.4 MB per image
  (measured); pixels are decoded only when something displays them, so importing all 707
  cells is cheap. Displaying the *whole* tile is what costs: a Workbench render of every
  cell's orthophoto measured **11.9 GB peak RSS** (macOS `maximum resident set size`), while
  a single full-resolution decode of one 4096×2560 DXT image costs ~248 MB on its own —
  Blender evidently keeps a reduced copy per displayed texture rather than 707 full decodes.
* **`tex_limit`** (default 0 = all) loads images for at most N cells, the ones owning the
  most faces first. Materials, `.ter` metadata and UVs are created for *every* used cell
  either way; only the image is skipped, so nothing is lost.
* **`tex_max_px`** decodes and downscales each image to at most N pixels: a 4096×2560
  orthophoto dropped to 1024×640 costs ~5 MB instead of ~248 MB (measured), which is what
  makes a whole-tile view practical. It is a quality trade-off, not free: at 1024 px a
  1085 m ZL19 cell is ~1 m/px, and a close-up rendered at that scale aliases badly
  (measured: the same 1200×1200 view of a 4096 px cell is crisp at native size and shows
  heavy horizontal moiré at 1024). Use 0 — or a higher cap — when inspecting detail, a small
  cap when viewing a whole tile.

> **Why the cap needs care:** writing `img.colorspace_settings.name` — *even to the value it
> already has* — invalidates Blender's ImBuf and makes it re-read the file at native size,
> silently discarding an earlier `Image.scale()` (measured: a 4096² DDS → scale to 1024 →
> set sRGB → back to 4096). The cap is therefore applied **after** the material wiring and
> then verified: the importer reports `images_over_cap` in its stats and warns if an image
> could not be brought down, and a whole-tile Workbench render leaves every image at or
> below the cap.

## Texture projection (`Fix to map` / `Projected UVs`)

Import can feed each texture from a **Z-axis projection of the vertex position** instead of
a baked UV layer (*Project textures along Z*, on by default). The projection is an affine
map per cell (`uv = position * scale + offset`), so the imagery stays put while you drag
vertices around — the designed property of this addon, and the reason a moved patch does not
smear its texture. The baked `XPlaneProjected` layer is still written and still equals
`terrain_tiles.uv_center`, but it goes stale as soon as geometry moves; the sidebar's
*Projected UVs* button recomputes it, and *Fix to map* / *Use UVs* switches a tile between
the two. `verify/zproject_test.py` checks all of this, including by rendering: see
[verification.md](verification.md).

## See also

* [ortho4xp-mesh-format.md](ortho4xp-mesh-format.md) — the `.mesh` format itself, including
  why the `.mesh` cannot name a terrain definition.
* [import-and-export.md](import-and-export.md) — the import options that control this
  (`Terrain textures`, `Terrain folder`, `Max textures`, `Max texture size`).