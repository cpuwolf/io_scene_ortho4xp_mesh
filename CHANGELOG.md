# Changelog

## 1.7.2

**New since 1.3.0** — the add-on now brings in an X-Plane tile's whole airport layer, not
just the editable mesh.

- **See the terrain X-Plane actually loads.** Open a scenery tile's DSF and the mountains,
  valleys and coastlines come in with their real heights and the tile's own textures —
  including the compressed tiles shipped with Global Scenery.
- **Airport pavement, on the ground where it belongs.** One import entry reads an
  `apt.dat` and lays the taxiways and aprons of the airports you name onto the tile you
  already have in the scene, following its surface. No tile open yet? The pavement still
  comes in, level with each airport.
- **Boundaries you can edit elsewhere.** The DSF's polygons — aprons, fences, painted
  lines, vegetation areas — arrive as a separate outlines object, and are saved next to the
  DSF as an `.osm` file, so you can open, compare against imagery and edit them in JOSM.
- **Smaller things.** Polygons-only scenery files (the airport overlay layers) now open
  instead of being refused, and picking which airports to bring in from the worldwide
  `apt.dat` is a one-field job.

## 1.3.0

Ortho4XP `.mesh` import and export with the tile's orthophotos; packaged as a Blender
extension for 4.2 and newer.
