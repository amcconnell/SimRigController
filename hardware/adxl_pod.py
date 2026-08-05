#!/usr/bin/env freecadcmd
"""Sensor pod for an ADXL345 accelerometer, bolted to the rig frame.

Part of the accelerometer feedback loop: the shaker app is deaf — every meter
it has reads the buffer upstream of the DAC, amp and transducer, so it cannot
tell a working shaker from a cut cable. Bolting an accelerometer to the rig
closes that loop and turns "does this feel balanced?" into a measurement.

One pod per contact point (seat pan, pedal deck), each on a Cat5 drop back to
the Pi. Both sensors share one I2C bus; the second board ties SDO high for
address 0x1D.

    freecadcmd hardware/adxl_pod.py          # writes .FCStd + two .stl here

DESIGN NOTES — the non-obvious constraints, so nobody "simplifies" them away:

* The sensor sits on a short, fat, solid pedestal integral with the bolted
  face. Mount stiffness *is* the measurement: a compliant path adds its own
  resonance, and if that lands in band it reads as rig behaviour. The walls
  are a shell and deliberately carry no measurement load.
* The bolt is counterbored so its head finishes below the board. That is what
  lets the two board screws straddle the bolt instead of standing the sensor
  off above the head on long standoffs — which would rebuild the compliance
  the pedestal exists to avoid. Assembly order is therefore bolt first, board
  second.
* Local wall thickness at the keystone aperture is 2 mm, not 3. Keystone
  latches are designed for wall-plate stock and will not grip 3 mm.
* The lid's snap groove hugs the bead within 0.2 mm. A loose lid rattles, and
  on a pod built to measure vibration a rattle is a noise source that shows up
  in the data as rig response. If it still rattles when printed, damp it.

MEASURED, not assumed: GY-291 hole spacing 15.0 mm; AMPCOM HKJ-801M keystone
37.3 mm deep with a 14.38 x 19.39 snap body. The bolt is M5 to suit the rig
extrusion. Everything else is derived from those.
"""

import os

import FreeCAD as App
import Mesh
import Part

# ---- Parameters ----------------------------------------------------------
L, W, H = 66.0, 40.0, 36.0        # outer envelope
WALL, FLOOR = 3.0, 6.0

BOLT_D, CBORE_D = 5.5, 9.5        # M5 clearance / socket head 8.5 + slop
CBORE_Z = 7.0                     # head sits from here up to the board
BOLT_X = 52.0                     # far enough back to clear the jack body
BOLT_Y = W / 2.0

PED_X, PED_Y, PED_TOP = 16.0, 22.0, 14.0
BOARD_SPACING = 15.0              # GY-291, measured
INSERT_D, INSERT_DEPTH = 3.5, 8.0  # M2.5 heat-set (self-tap: 2.1)

KEY_W, KEY_H = 14.7, 19.6         # snap body 14.38 x 19.39 + clearance
KEY_Z, PANEL_T = 8.0, 2.0
JACK_DEPTH = 37.3                 # AMPCOM drawing, overall

LIP_H, LIP_T, LIP_GAP = 6.0, 1.5, 0.2
BEAD, GROOVE, PLATE_T = 0.4, 0.5, 3.0

CX, CY = L - 2 * WALL, W - 2 * WALL


def build():
    """Return (pod, lid) shapes."""
    pod = Part.makeBox(L, W, H)
    pod = pod.cut(Part.makeBox(CX, CY, H - FLOOR + 1, App.Vector(WALL, WALL, FLOOR)))

    # Stiff column from the bolted face to the sensor.
    pod = pod.fuse(Part.makeBox(
        PED_X, PED_Y, PED_TOP - FLOOR,
        App.Vector(BOLT_X - PED_X / 2, BOLT_Y - PED_Y / 2, FLOOR)))

    pod = pod.cut(Part.makeCylinder(BOLT_D / 2, PED_TOP + 2, App.Vector(BOLT_X, BOLT_Y, -1)))
    pod = pod.cut(Part.makeCylinder(CBORE_D / 2, PED_TOP - CBORE_Z + 1,
                                    App.Vector(BOLT_X, BOLT_Y, CBORE_Z)))
    for dy in (-BOARD_SPACING / 2, BOARD_SPACING / 2):
        pod = pod.cut(Part.makeCylinder(
            INSERT_D / 2, INSERT_DEPTH + 1,
            App.Vector(BOLT_X, BOLT_Y + dy, PED_TOP - INSERT_DEPTH)))

    # Keystone: thin the wall locally, then punch the aperture.
    pod = pod.cut(Part.makeBox(
        WALL - PANEL_T + 0.01, KEY_W + 10, KEY_H + 8,
        App.Vector(PANEL_T, BOLT_Y - (KEY_W + 10) / 2, KEY_Z - 4)))
    pod = pod.cut(Part.makeBox(WALL + 2, KEY_W, KEY_H,
                               App.Vector(-1, BOLT_Y - KEY_W / 2, KEY_Z)))

    # Snap groove, sized to the bead with 0.2 mm total play.
    gz = H - LIP_H + 0.4
    ring = Part.makeBox(CX + 2 * GROOVE, CY + 2 * GROOVE, 1.2,
                        App.Vector(WALL - GROOVE, WALL - GROOVE, gz))
    ring = ring.cut(Part.makeBox(CX, CY, 4.0, App.Vector(WALL, WALL, gz - 1)))
    pod = pod.cut(ring)

    # Pry notch — a flush snap lid with no purchase cannot be opened, and this
    # one gets opened repeatedly while comparing mounting methods.
    pod = pod.cut(Part.makeBox(WALL + 1, 14.0, 4.0,
                               App.Vector(L - WALL, W / 2 - 7.0, H - 4.0)))

    # ---- Lid --------------------------------------------------------------
    lo_x, lo_y = CX - 2 * LIP_GAP, CY - 2 * LIP_GAP
    ox, oy = WALL + LIP_GAP, WALL + LIP_GAP
    hollow = Part.makeBox(lo_x - 2 * LIP_T, lo_y - 2 * LIP_T, LIP_H + 2,
                          App.Vector(ox + LIP_T, oy + LIP_T, H - LIP_H - 1))

    lid = Part.makeBox(L, W, PLATE_T, App.Vector(0, 0, H))
    lid = lid.fuse(Part.makeBox(lo_x, lo_y, LIP_H, App.Vector(ox, oy, H - LIP_H)).cut(hollow))
    lid = lid.fuse(Part.makeBox(lo_x + 2 * BEAD, lo_y + 2 * BEAD, 1.0,
                                App.Vector(ox - BEAD, oy - BEAD, H - LIP_H + 0.5)).cut(hollow))
    return pod, lid


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    doc = App.newDocument("ADXL_Pod")
    pod_shape, lid_shape = build()

    pod = doc.addObject("Part::Feature", "Pod")
    pod.Shape = pod_shape
    lid = doc.addObject("Part::Feature", "Lid")
    lid.Shape = lid_shape
    doc.recompute()

    assert pod_shape.isValid() and lid_shape.isValid(), "invalid solid"
    assert (BOLT_X - PED_X / 2) > JACK_DEPTH, "pedestal collides with the jack body"
    assert (H - LIP_H) > (KEY_Z + KEY_H), "lid lip collides with the jack body"

    doc.saveAs(os.path.join(here, "ADXL_Pod.FCStd"))
    Mesh.export([pod], os.path.join(here, "ADXL_Pod.stl"))
    Mesh.export([lid], os.path.join(here, "ADXL_Pod_Lid.stl"))

    print(f"pod {pod_shape.Volume / 1000:.1f} cm^3  lid {lid_shape.Volume / 1000:.1f} cm^3")
    print(f"jack clearance {(BOLT_X - PED_X / 2) - JACK_DEPTH:.1f} mm, "
          f"lid clearance {(H - LIP_H) - (KEY_Z + KEY_H):.1f} mm")
    print("wrote ADXL_Pod.FCStd, ADXL_Pod.stl, ADXL_Pod_Lid.stl")


main()
