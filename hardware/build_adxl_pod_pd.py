#!/usr/bin/env freecadcmd
"""Generate the ADXL345 pod as an editable PartDesign model.

This is scaffolding, run once. Unlike adxl_pod.py — which *is* the source for
the Part-primitive version and must be re-run after every change — the file
this produces is the source. Edit ADXL_Pod_PD.FCStd in the GUI from then on;
this script is kept only to show how the model was laid out.

Design choices that matter for editing it later:

* Every dimension lives in the `Params` spreadsheet with a named alias, and
  every sketch constraint is bound to one by expression. Change a number in
  the sheet and the whole model follows. Nothing is typed twice.
* Every sketch attaches to an origin plane with an offset, never to a face of
  a previous feature. Face references are what make PartDesign models explode
  when an earlier feature changes size — the notorious topological naming
  problem. Origin planes cannot break.
* Constraints are named, so the GUI shows `width` rather than `Constraint7`.

Geometry rationale (why the part is shaped as it is) lives in adxl_pod.py and
hardware/README.md, and is not repeated here.

    freecadcmd hardware/build_adxl_pod_pd.py
"""

import os

import FreeCAD as App
import Part
import Sketcher

DOC = "ADXL_Pod_PD"

# W and H are rejected as aliases: FreeCAD's expression engine reads them as
# the unit symbols for watt and henry. Spelled out, which reads better anyway.
PARAMS = [
    ("LENGTH", 66.0, "outer length"),
    ("WIDTH", 40.0, "outer width"),
    ("HEIGHT", 36.0, "outer height"),
    ("WALL", 3.0, "wall thickness"),
    ("FLOOR", 6.0, "base plate thickness"),
    ("BOLT_D", 5.5, "M5 clearance"),
    ("CBORE_D", 9.5, "socket head clearance"),
    ("CBORE_Z", 7.0, "counterbore floor"),
    ("BOLT_X", 52.0, "bolt centre, clears the jack body"),
    ("PED_X", 16.0, "pedestal length"),
    ("PED_Y", 22.0, "pedestal width"),
    ("PED_TOP", 14.0, "board seating height"),
    ("BOARD_SPACING", 15.0, "GY-291 hole spacing, measured"),
    ("INSERT_D", 3.5, "M2.5 heat-set"),
    ("INSERT_DEPTH", 8.0, "insert depth"),
    ("KEY_W", 14.7, "keystone aperture width"),
    ("KEY_H", 19.6, "keystone aperture height"),
    ("KEY_Z", 8.0, "aperture bottom"),
    ("PANEL_T", 2.0, "local wall at the aperture, for the latch"),
    ("LIP_H", 6.0, "lid lip depth"),
    ("LIP_T", 1.5, "lid lip thickness"),
    ("LIP_GAP", 0.25, "lip clearance per side"),
    ("BEAD", 0.4, "snap interference"),
    ("BEAD_RAMP", 0.5, "lead-in height"),
    ("GROOVE", 0.5, "groove depth"),
    ("PLATE_T", 3.0, "lid plate thickness"),
    ("NOTCH_W", 14.0, "pry notch width"),
    ("NOTCH_H", 4.0, "pry notch height"),
]


def make_params(doc):
    sheet = doc.addObject("Spreadsheet::Sheet", "Params")
    sheet.set("A1", "parameter")
    sheet.set("B1", "value")
    sheet.set("C1", "note")
    for i, (name, value, note) in enumerate(PARAMS, start=2):
        sheet.set(f"A{i}", name)
        sheet.set(f"B{i}", str(value))
        sheet.set(f"C{i}", note)
        sheet.setAlias(f"B{i}", name)
    doc.recompute()
    return sheet


def _plane(doc, which):
    return doc.getObject(which)


def new_sketch(doc, body, name, plane, offset=None, rot=None):
    sk = doc.addObject("Sketcher::SketchObject", name)
    body.addObject(sk)
    sk.AttachmentSupport = [(_plane(doc, plane), "")]
    sk.MapMode = "FlatFace"
    if offset is not None or rot is not None:
        sk.AttachmentOffset = App.Placement(
            App.Vector(0, 0, offset or 0.0), App.Rotation(0, 0, 0, 1)
        )
    return sk


def rect(sk, x, y, w, h, tag):
    """Constrained rectangle. Returns nothing; constraints are named
    <tag>_x/_y/_w/_h so they can be driven by expression."""
    n = len(sk.Geometry)
    pts = [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
    for i in range(4):
        a, b = pts[i], pts[(i + 1) % 4]
        sk.addGeometry(
            Part.LineSegment(App.Vector(*a, 0), App.Vector(*b, 0)), False
        )
    for i in range(4):
        sk.addConstraint(
            Sketcher.Constraint("Coincident", n + i, 2, n + (i + 1) % 4, 1)
        )
    sk.addConstraint(Sketcher.Constraint("Horizontal", n + 0))
    sk.addConstraint(Sketcher.Constraint("Horizontal", n + 2))
    sk.addConstraint(Sketcher.Constraint("Vertical", n + 1))
    sk.addConstraint(Sketcher.Constraint("Vertical", n + 3))
    iw = sk.addConstraint(Sketcher.Constraint("DistanceX", n + 0, 1, n + 0, 2, w))
    ih = sk.addConstraint(Sketcher.Constraint("DistanceY", n + 1, 1, n + 1, 2, h))
    ix = sk.addConstraint(Sketcher.Constraint("DistanceX", -1, 1, n + 0, 1, x))
    iy = sk.addConstraint(Sketcher.Constraint("DistanceY", -1, 1, n + 0, 1, y))
    for idx, suffix in ((iw, "w"), (ih, "h"), (ix, "x"), (iy, "y")):
        sk.renameConstraint(idx, f"{tag}_{suffix}")
    return {"w": f"{tag}_w", "h": f"{tag}_h", "x": f"{tag}_x", "y": f"{tag}_y"}


def circle(sk, x, y, r, tag):
    n = len(sk.Geometry)
    sk.addGeometry(Part.Circle(App.Vector(x, y, 0), App.Vector(0, 0, 1), r), False)
    ir = sk.addConstraint(Sketcher.Constraint("Radius", n, r))
    ix = sk.addConstraint(Sketcher.Constraint("DistanceX", -1, 1, n, 3, x))
    iy = sk.addConstraint(Sketcher.Constraint("DistanceY", -1, 1, n, 3, y))
    for idx, suffix in ((ir, "r"), (ix, "x"), (iy, "y")):
        sk.renameConstraint(idx, f"{tag}_{suffix}")
    return {"r": f"{tag}_r", "x": f"{tag}_x", "y": f"{tag}_y"}


def bind(sk, names, exprs):
    for key, expr in exprs.items():
        sk.setExpression(f".Constraints.{names[key]}", expr)


def pad(doc, body, sketch, length_expr, name, reversed_=False, midplane=False):
    f = doc.addObject("PartDesign::Pad", name)
    body.addObject(f)
    f.Profile = sketch
    f.Length = 10.0
    f.setExpression("Length", length_expr)
    f.Reversed = reversed_
    f.Midplane = midplane
    doc.recompute()
    return f


def pocket(doc, body, sketch, name, length_expr=None, through=False, reversed_=False):
    f = doc.addObject("PartDesign::Pocket", name)
    body.addObject(f)
    f.Profile = sketch
    if through:
        f.Type = 1
    else:
        f.Length = 5.0
        f.setExpression("Length", length_expr)
    f.Reversed = reversed_
    doc.recompute()
    return f


def build_pod(doc):
    body = doc.addObject("PartDesign::Body", "Pod")
    doc.recompute()

    sk = new_sketch(doc, body, "Sk_Outer", "XY_Plane")
    n = rect(sk, 0, 0, 66, 40, "outer")
    bind(sk, n, {"w": "Params.LENGTH", "h": "Params.WIDTH", "x": "0", "y": "0"})
    pad(doc, body, sk, "Params.HEIGHT", "Block")

    sk = new_sketch(doc, body, "Sk_Cavity", "XY_Plane", offset=6.0)
    sk.setExpression("AttachmentOffset.Base.z", "Params.FLOOR")
    n = rect(sk, 3, 3, 60, 34, "cav")
    bind(sk, n, {"w": "Params.LENGTH - 2 * Params.WALL", "h": "Params.WIDTH - 2 * Params.WALL",
                 "x": "Params.WALL", "y": "Params.WALL"})
    pocket(doc, body, sk, "Cavity", through=True, reversed_=True)

    sk = new_sketch(doc, body, "Sk_Pedestal", "XY_Plane", offset=6.0)
    sk.setExpression("AttachmentOffset.Base.z", "Params.FLOOR")
    n = rect(sk, 44, 9, 16, 22, "ped")
    bind(sk, n, {"w": "Params.PED_X", "h": "Params.PED_Y",
                 "x": "Params.BOLT_X - Params.PED_X / 2",
                 "y": "Params.WIDTH / 2 - Params.PED_Y / 2"})
    pad(doc, body, sk, "Params.PED_TOP - Params.FLOOR", "Pedestal")

    sk = new_sketch(doc, body, "Sk_Bolt", "XY_Plane")
    n = circle(sk, 52, 20, 2.75, "bolt")
    bind(sk, n, {"r": "Params.BOLT_D / 2", "x": "Params.BOLT_X", "y": "Params.WIDTH / 2"})
    pocket(doc, body, sk, "BoltHole", through=True, reversed_=True)

    sk = new_sketch(doc, body, "Sk_Cbore", "XY_Plane", offset=7.0)
    sk.setExpression("AttachmentOffset.Base.z", "Params.CBORE_Z")
    n = circle(sk, 52, 20, 4.75, "cb")
    bind(sk, n, {"r": "Params.CBORE_D / 2", "x": "Params.BOLT_X", "y": "Params.WIDTH / 2"})
    pocket(doc, body, sk, "Counterbore", "Params.PED_TOP - Params.CBORE_Z", reversed_=True)

    sk = new_sketch(doc, body, "Sk_Inserts", "XY_Plane", offset=14.0)
    sk.setExpression("AttachmentOffset.Base.z", "Params.PED_TOP")
    a = circle(sk, 52, 12.5, 1.75, "insA")
    bind(sk, a, {"r": "Params.INSERT_D / 2", "x": "Params.BOLT_X",
                 "y": "Params.WIDTH / 2 - Params.BOARD_SPACING / 2"})
    b = circle(sk, 52, 27.5, 1.75, "insB")
    bind(sk, b, {"r": "Params.INSERT_D / 2", "x": "Params.BOLT_X",
                 "y": "Params.WIDTH / 2 + Params.BOARD_SPACING / 2"})
    pocket(doc, body, sk, "InsertHoles", "Params.INSERT_DEPTH")

    # Keystone: sketches on YZ, so sketch-X is global Y and sketch-Y is global Z.
    sk = new_sketch(doc, body, "Sk_PanelRecess", "YZ_Plane", offset=2.0)
    sk.setExpression("AttachmentOffset.Base.z", "Params.PANEL_T")
    n = rect(sk, 7.65, 4.0, 24.7, 27.6, "rec")
    bind(sk, n, {"w": "Params.KEY_W + 10", "h": "Params.KEY_H + 8",
                 "x": "Params.WIDTH / 2 - (Params.KEY_W + 10) / 2",
                 "y": "Params.KEY_Z - 4"})
    pocket(doc, body, sk, "PanelRecess", "Params.WALL - Params.PANEL_T", reversed_=True)

    sk = new_sketch(doc, body, "Sk_Keystone", "YZ_Plane")
    n = rect(sk, 12.65, 8.0, 14.7, 19.6, "key")
    bind(sk, n, {"w": "Params.KEY_W", "h": "Params.KEY_H",
                 "x": "Params.WIDTH / 2 - Params.KEY_W / 2", "y": "Params.KEY_Z"})
    pocket(doc, body, sk, "KeystoneAperture", "Params.WALL", reversed_=True)

    sk = new_sketch(doc, body, "Sk_Groove", "XY_Plane", offset=30.4)
    sk.setExpression("AttachmentOffset.Base.z",
                     "Params.HEIGHT - Params.LIP_H + Params.BEAD_RAMP - 0.1")
    o = rect(sk, 2.5, 2.5, 61, 35, "grvO")
    bind(sk, o, {"w": "Params.LENGTH - 2 * Params.WALL + 2 * Params.GROOVE",
                 "h": "Params.WIDTH - 2 * Params.WALL + 2 * Params.GROOVE",
                 "x": "Params.WALL - Params.GROOVE", "y": "Params.WALL - Params.GROOVE"})
    i = rect(sk, 3, 3, 60, 34, "grvI")
    bind(sk, i, {"w": "Params.LENGTH - 2 * Params.WALL", "h": "Params.WIDTH - 2 * Params.WALL",
                 "x": "Params.WALL", "y": "Params.WALL"})
    pocket(doc, body, sk, "SnapGroove", "1.2", reversed_=True)

    sk = new_sketch(doc, body, "Sk_PryNotch", "XY_Plane", offset=32.0)
    sk.setExpression("AttachmentOffset.Base.z", "Params.HEIGHT - Params.NOTCH_H")
    n = rect(sk, 63, 13, 3, 14, "notch")
    bind(sk, n, {"w": "Params.WALL", "h": "Params.NOTCH_W",
                 "x": "Params.LENGTH - Params.WALL",
                 "y": "Params.WIDTH / 2 - Params.NOTCH_W / 2"})
    pocket(doc, body, sk, "PryNotch", through=True, reversed_=True)
    return body


def build_lid(doc):
    body = doc.addObject("PartDesign::Body", "Lid")
    doc.recompute()

    sk = new_sketch(doc, body, "Sk_Plate", "XY_Plane", offset=36.0)
    sk.setExpression("AttachmentOffset.Base.z", "Params.HEIGHT")
    n = rect(sk, 0, 0, 66, 40, "plate")
    bind(sk, n, {"w": "Params.LENGTH", "h": "Params.WIDTH", "x": "0", "y": "0"})
    pad(doc, body, sk, "Params.PLATE_T", "Plate")

    # Lip: outer at the cavity minus clearance, inner LIP_T in from that.
    sk = new_sketch(doc, body, "Sk_Lip", "XY_Plane", offset=30.0)
    sk.setExpression("AttachmentOffset.Base.z", "Params.HEIGHT - Params.LIP_H")
    o = rect(sk, 3.25, 3.25, 59.5, 33.5, "lipO")
    bind(sk, o, {"w": "Params.LENGTH - 2 * Params.WALL - 2 * Params.LIP_GAP",
                 "h": "Params.WIDTH - 2 * Params.WALL - 2 * Params.LIP_GAP",
                 "x": "Params.WALL + Params.LIP_GAP", "y": "Params.WALL + Params.LIP_GAP"})
    i = rect(sk, 4.75, 4.75, 56.5, 30.5, "lipI")
    bind(sk, i, {"w": "Params.LENGTH - 2 * Params.WALL - 2 * Params.LIP_GAP - 2 * Params.LIP_T",
                 "h": "Params.WIDTH - 2 * Params.WALL - 2 * Params.LIP_GAP - 2 * Params.LIP_T",
                 "x": "Params.WALL + Params.LIP_GAP + Params.LIP_T",
                 "y": "Params.WALL + Params.LIP_GAP + Params.LIP_T"})
    pad(doc, body, sk, "Params.LIP_H", "Lip")

    # Bead lead-in. A lofted ring, not a tapered pad: PartDesign's TaperAngle
    # silently does nothing on a profile with an inner wire, which produced a
    # square-shouldered bead that looked right in the tree and was wrong in the
    # solid. The loft is explicit — bottom ring flush with the lip, top ring
    # BEAD proud of it — and stays editable by dragging either sketch.
    def bead_ring(name, z_expr, outer_expr_w, outer_expr_h, outer_expr_x, outer_expr_y):
        sk = new_sketch(doc, body, name, "XY_Plane", offset=30.0)
        sk.setExpression("AttachmentOffset.Base.z", z_expr)
        o = rect(sk, 3.25, 3.25, 59.5, 33.5, name + "O")
        bind(sk, o, {"w": outer_expr_w, "h": outer_expr_h,
                     "x": outer_expr_x, "y": outer_expr_y})
        i = rect(sk, 4.75, 4.75, 56.5, 30.5, name + "I")
        bind(sk, i, {
            "w": "Params.LENGTH - 2 * Params.WALL - 2 * Params.LIP_GAP - 2 * Params.LIP_T",
            "h": "Params.WIDTH - 2 * Params.WALL - 2 * Params.LIP_GAP - 2 * Params.LIP_T",
            "x": "Params.WALL + Params.LIP_GAP + Params.LIP_T",
            "y": "Params.WALL + Params.LIP_GAP + Params.LIP_T"})
        return sk

    flush = ("Params.LENGTH - 2 * Params.WALL - 2 * Params.LIP_GAP",
             "Params.WIDTH - 2 * Params.WALL - 2 * Params.LIP_GAP",
             "Params.WALL + Params.LIP_GAP", "Params.WALL + Params.LIP_GAP")
    proud = ("Params.LENGTH - 2 * Params.WALL - 2 * Params.LIP_GAP + 2 * Params.BEAD",
             "Params.WIDTH - 2 * Params.WALL - 2 * Params.LIP_GAP + 2 * Params.BEAD",
             "Params.WALL + Params.LIP_GAP - Params.BEAD",
             "Params.WALL + Params.LIP_GAP - Params.BEAD")

    sk_lo = bead_ring("Sk_BeadBottom", "Params.HEIGHT - Params.LIP_H", *flush)
    sk_hi = bead_ring("Sk_BeadTop",
                      "Params.HEIGHT - Params.LIP_H + Params.BEAD_RAMP", *proud)
    loft = doc.addObject("PartDesign::AdditiveLoft", "BeadRamp")
    body.addObject(loft)
    loft.Profile = sk_lo
    loft.Sections = [sk_hi]
    loft.Ruled = True
    doc.recompute()

    sk = bead_ring("Sk_BeadShoulder",
                   "Params.HEIGHT - Params.LIP_H + Params.BEAD_RAMP", *proud)
    pad(doc, body, sk, "1.0", "BeadShoulder")
    return body


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    doc = App.newDocument(DOC)
    make_params(doc)
    pod = build_pod(doc)
    lid = build_lid(doc)
    doc.recompute()

    print(f"pod  valid={pod.Shape.isValid()}  {pod.Shape.Volume/1000:.1f} cm^3  "
          f"solids={len(pod.Shape.Solids)}")
    print(f"lid  valid={lid.Shape.isValid()}  {lid.Shape.Volume/1000:.1f} cm^3  "
          f"solids={len(lid.Shape.Solids)}")
    lip_face = 3.0 + 0.25
    base_z = 36.0 - 6.0
    def outer_x(z):
        x = 2.40
        while x < 4.2 and not lid.Shape.isInside(App.Vector(x, 20, z), 1e-6, True):
            x += 0.01
        return x
    ramp = [outer_x(base_z + dz) for dz in (0.05, 0.25, 0.45)]
    assert ramp[0] > ramp[1] > ramp[2], f"bead lead-in missing or inverted: {ramp}"
    assert abs(ramp[0] - lip_face) < 0.1, f"ramp does not start flush with the lip: {ramp[0]}"
    assert lid.Shape.BoundBox.ZMax > 38.0, "plate padded the wrong way"

    errs = [o.Name for o in doc.Objects if getattr(o, "State", None) and "Invalid" in o.State]
    print("objects in error:", errs or "none")
    doc.saveAs(os.path.join(here, f"{DOC}.FCStd"))
    print(f"wrote {DOC}.FCStd")


main()
