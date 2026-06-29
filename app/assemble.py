"""
Cross-reference + assembly engine.

Takes the drawings extraction (legend + rooms + doors + rcp_heights) and produces
the final resolved room finish schedule with Painting Plus scope decisions.

Design (informed by testing):
  - The drawing FINISH LEGEND is the primary source for what a code means — testing
    showed it's rich and accurate (resolved SC-1, WB-1, WB-2, paint products directly).
  - Painting Plus SCOPE RULES are applied to each resolved finish. These rules are the
    same on every job; only the codes/products are job-specific.
  - Ceiling heights are joined to rooms BY ROOM NUMBER.
  - Missing/odd data is flagged VERIFY, never guessed.

Scope rules (from Jason):
  BASE: wood or MDF -> paints (include). rubber/vinyl/resilient -> exclude.
  SEALED CONCRETE (sealer/curing-sealing compound) -> include (our work).
  EPOXY coating (floor/wall) -> include.  epoxy grout/adhesive -> not scope.
  POLISHED / ground / densified concrete -> exclude (we don't polish floors).
  PAINT / STAIN / wall coatings -> include.
  carpet / tile / resilient flooring / walk-off mat / ceilings (ACT) -> exclude.
"""
import re

# ---- classify a resolved finish into a Painting Plus scope decision ----
def _scope_for(category, description, product):
    """Return (scope, reason). scope in: paint, stain, include, exclude, verify."""
    text = f"{category} {description} {product}".lower()

    # base materials
    if category == "base" or "base" in description.lower():
        if any(w in text for w in ["wood", "mdf", "medium density", "medium-density", "maple", "oak", "poplar"]):
            return ("stain" if "stain" in text else "paint",
                    "wood/MDF base — Painting Plus paints/stains")
        if any(w in text for w in ["rubber", "vinyl", "resilient", "thermoplastic", "johnsonite", "roppe", "tarkett"]):
            return ("exclude", "rubber/vinyl base — never painted")
        return ("verify", "base material unclear — confirm wood/MDF vs rubber/vinyl")

    # concrete: sealed (ours) vs polished (not ours)
    if "concret" in text or category == "floor" and "seal" in text:
        if any(w in text for w in ["polish", "ground", "densif", "burnish"]):
            return ("exclude", "polished/ground concrete — we do not polish floors")
        if any(w in text for w in ["seal", "curing", "sealer", "lumiseal"]):
            return ("include", "sealed concrete — Painting Plus applies sealer/curing-seal")

    # epoxy: coating (ours) vs grout/adhesive (not)
    if "epoxy" in text:
        if any(w in text for w in ["grout", "adhesive", "anchor", "mortar"]):
            return ("exclude", "epoxy grout/adhesive — not a coating")
        return ("include", "epoxy coating — Painting Plus scope")

    # paint / stain / wall coatings
    if category == "paint" or any(w in text for w in ["paint", "stain", "coating", "primer"]):
        if "stain" in text:
            return ("stain", "stain finish")
        return ("paint", "paint finish")

    # wallcovering / wall protection — sometimes painter, often specialty
    if category == "wallcovering" or any(w in text for w in ["wallcover", "wall covering", "wall protection", "acrovyn"]):
        return ("verify", "wallcovering/wall-protection — confirm painter vs installer")

    # floors and ceilings that aren't painter scope
    if category in ("floor", "ceiling") or any(w in text for w in
            ["carpet", "tile", "lvt", "resilient", "walk-off", "walk off", "acoustical", "acoustic"]):
        return ("exclude", f"{category or 'finish'} — not painter scope")

    return ("verify", "unrecognized finish — confirm scope")


def _legend_lookup(legend):
    """code -> {category, description, product, sheen}"""
    d = {}
    for item in legend:
        code = (item.get("code") or "").strip().upper()
        if code:
            d[code] = item
    return d


def _split_codes(cell):
    """A finish cell may hold multiple codes: 'T-2,3,PNT-1' or 'PNT-1/T-2'."""
    if not cell:
        return []
    parts = re.split(r"[\/,]", cell)
    out = []
    for p in parts:
        p = p.strip().upper()
        if not p:
            continue
        # expand shorthand like 'T-2,3' where '3' means 'T-3'
        if re.fullmatch(r"\d+", p) and out:
            prefix = re.match(r"([A-Z]+-)", out[-1])
            if prefix:
                p = prefix.group(1) + p
        out.append(p)
    return out


def _resolve_cell(cell, legend_map):
    """Resolve every code in a finish cell to product + scope decision."""
    resolved = []
    for code in _split_codes(cell):
        info = legend_map.get(code)
        if info:
            scope, reason = _scope_for(info.get("category", ""), info.get("description", ""),
                                       info.get("product", ""))
            resolved.append({"code": code, "product": info.get("product", ""),
                             "description": info.get("description", ""),
                             "scope": scope, "reason": reason})
        else:
            resolved.append({"code": code, "product": "",
                             "description": "not in drawing legend",
                             "scope": "verify", "reason": "code not found in legend — check spec book"})
    return resolved


def _height_map(rcp_heights):
    """room_number -> height info, cleaning obvious misreads.
    Guards against normalization collisions (e.g. a misread '109_1' overwriting
    the real '109') by never letting an entry without a valid height replace one
    that has it."""
    hm = {}
    for h in rcp_heights:
        rn_raw = (h.get("room_number") or "").strip()
        # strip odd suffixes the model sometimes adds (e.g. '109_1'); keep base number
        rn = re.sub(r"[_\-]\d+$", "", rn_raw)
        height = (h.get("ceiling_height") or "").strip()
        # a real ceiling height has feet; discard inch-only misreads like 11 3/8"
        if height and "'" not in height:
            height = ""  # implausible as a ceiling height
        entry = {"ceiling_height": height,
                 "other_heights": (h.get("other_heights") or "").strip(),
                 "ceiling_type": (h.get("ceiling_type") or "").strip()}
        if not rn:
            continue
        existing = hm.get(rn)
        # only overwrite if this entry has a valid height OR there's nothing yet.
        # never let a height-less/misread entry clobber a good one.
        if existing is None:
            hm[rn] = entry
        elif height and not existing.get("ceiling_height"):
            hm[rn] = entry
        # if both have heights and they differ, keep first but note the conflict
        elif height and existing.get("ceiling_height") and height != existing["ceiling_height"]:
            existing.setdefault("conflict", f"also read {height}")
    return hm


def assemble_schedule(drawings_result):
    """
    Input: the dict returned by analyze_drawings (legend, rooms, rcp_heights, doors).
    Output: resolved room-by-room finish schedule + painter scope summary.
    """
    legend_map = _legend_lookup(drawings_result.get("legend", []))
    heights = _height_map(drawings_result.get("rcp_heights", []))

    schedule = []
    for room in drawings_result.get("rooms", []):
        rn = (room.get("room_number") or "").strip()
        h = heights.get(rn, {})
        walls_resolved = _resolve_cell(room.get("walls", ""), legend_map)
        # detect partial-height (tile + paint on same wall) for takeoff accuracy
        wall_codes = [w["code"] for w in walls_resolved]
        has_tile = any(c.startswith("T-") for c in wall_codes)
        has_paint = any(w["scope"] in ("paint", "stain") for w in walls_resolved)
        partial_height = has_tile and has_paint
        row = {
            "room_number": rn,
            "room_name": room.get("room_name", ""),
            "floor": _resolve_cell(room.get("floor", ""), legend_map),
            "base": _resolve_cell(room.get("base", ""), legend_map),
            "walls": walls_resolved,
            "ceiling": _resolve_cell(room.get("ceiling", ""), legend_map),
            "ceiling_height": h.get("ceiling_height") or "VERIFY — not on RCP",
            "ceiling_type": h.get("ceiling_type", ""),
            "height_notes": h.get("other_heights", ""),
            "wall_notes": room.get("wall_notes", ""),
            "partial_height_walls": partial_height,
        }
        schedule.append(row)

    # painter scope summary: which rooms have paintable walls/base, accent walls, etc.
    paint_walls, accent_walls, paint_base, sealed_concrete, verify_items = [], [], [], [], []
    partial_height_rooms = []
    for row in schedule:
        rn = row["room_number"]
        for w in row["walls"]:
            if w["scope"] in ("paint", "stain") and "PNT-2" not in w["code"]:
                paint_walls.append(rn)
            if "PNT-2" in w["code"]:
                accent_walls.append(rn)
        if row.get("partial_height_walls"):
            partial_height_rooms.append(rn)
            verify_items.append(f"Room {rn}: tile + paint walls — confirm paint height (where tile stops)")
        for b in row["base"]:
            if b["scope"] in ("paint", "stain"):
                paint_base.append(f"{rn} ({b['code']})")
            if b["scope"] == "verify":
                verify_items.append(f"Room {rn} base {b['code']}: {b['reason']}")
        for f in row["floor"]:
            if f["scope"] == "include" and "seal" in f["reason"].lower():
                sealed_concrete.append(f"{rn} ({f['code']})")
        if "VERIFY" in row["ceiling_height"]:
            verify_items.append(f"Room {rn}: ceiling height not found on RCP")

    # door frames to paint (from door schedule + the PNT-3 rule)
    hm_frames_to_paint = []
    for d in drawings_result.get("doors", {}).get("doors", []):
        if (d.get("frame_material") or "").upper() in ("HM", "F"):
            hm_frames_to_paint.append(d.get("door_number", ""))

    summary = {
        "rooms_total": len(schedule),
        "wall_paint_rooms": sorted(set(paint_walls)),
        "accent_wall_rooms": sorted(set(accent_walls)),
        "paint_or_stain_base": sorted(set(paint_base)),
        "sealed_concrete_floors": sorted(set(sealed_concrete)),
        "partial_height_paint_rooms": sorted(set(partial_height_rooms)),
        "hm_frames_to_paint_PNT3": [x for x in hm_frames_to_paint if x],
        "door_finish_notes": drawings_result.get("doors", {}).get("finish_notes", []),
        "verify_flags": verify_items,
    }
    return {"schedule": schedule, "scope_summary": summary}
