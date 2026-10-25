"""Write DSF polygon boundaries as OSM XML, so JOSM can open and edit them.

One OSM way per winding: a closed way for .pol / .fac / legacy class polygons, an open
way for .lin line features. The X-Plane class travels in the ``xplane_class`` tag, and
nested windings carry ``xplane_winding`` = outer / hole plus an ``xplane_polygon``
group index, so a round trip keeps the structure the DSF had.

Coordinates are WGS84 degrees at 7 decimals (~1 cm) -- X-Plane stores them in a
16-bit pool, always finer than what we write.
"""

import xml.sax.saxutils as sx

OSM_VERSION = "0.6"
GENERATOR = "io_scene_ortho4xp_mesh"


def _num(v):
    return ("%.7f" % float(v)).rstrip("0").rstrip(".") or "0"


def write_osm(path, polygons, note="", include_forests=False):
    """Write ``polygons`` (dsfcodec.Polygon objects) to ``path``.

    Returns (ways, nodes, bytes_written). Raises OSError if the path is not writable.
    """
    chosen = [p for p in polygons
              if include_forests or not (p.klass or "").lower().endswith(".for")]

    lines = ["<?xml version='1.0' encoding='UTF-8'?>",
             "<osm version='%s' generator='%s'>" % (OSM_VERSION, GENERATOR)]
    if note:
        lines.append("  <!-- %s -->" % sx.escape(note))

    # bounds first: JOSM zooms to them on open
    lats = [lat for p in chosen for r in p.rings for _lon, lat in r]
    lons = [lon for p in chosen for r in p.rings for lon, _lat in r]
    if lats and lons:
        lines.append("  <bounds minlat='%s' minlon='%s' maxlat='%s' maxlon='%s'/>"
                     % (_num(min(lats)), _num(min(lons)),
                        _num(max(lats)), _num(max(lons))))

    next_id = -1
    nodes = 0
    ways = 0
    for group, poly in enumerate(chosen):
        klass = poly.klass or ""
        open_line = klass.lower().endswith(".lin")
        nested = len(poly.rings) > 1
        for wind, ring in enumerate(poly.rings):
            pts = list(ring)
            if len(pts) < 2:
                continue
            if not open_line and pts[0] == pts[-1]:
                pts.pop()                      # OSM closes a way by repeating node 1
            ids = []
            for lon, lat in pts:
                nid = next_id
                next_id -= 1
                lines.append("  <node id='%d' lat='%s' lon='%s'/>"
                             % (nid, _num(lat), _num(lon)))
                ids.append(nid)
                nodes += 1
            wid = next_id
            next_id -= 1
            lines.append("  <way id='%d'>" % wid)
            for nid in ids:
                lines.append("    <nd ref='%d'/>" % nid)
            if not open_line and len(pts) > 2:
                lines.append("    <nd ref='%d'/>" % ids[0])
            lines.append("    <tag k='xplane_class' v='%s'/>" % sx.escape(klass))
            if nested:
                lines.append("    <tag k='xplane_winding' v='%s'/>"
                             % ("outer" if wind == 0 else "hole"))
                lines.append("    <tag k='xplane_polygon' v='%d'/>" % group)
            if poly.param:
                lines.append("    <tag k='xplane_param' v='%d'/>" % int(poly.param))
            if open_line:
                lines.append("    <tag k='xplane_linear' v='yes'/>")
            lines.append("  </way>")
            ways += 1

    lines.append("</osm>")
    text = "\n".join(lines) + "\n"
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return ways, nodes, len(text.encode("utf-8"))
