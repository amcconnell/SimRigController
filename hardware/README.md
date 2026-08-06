# Hardware

Printable parts for the rig.

## Which files to edit

**`ADXL_Pod_PD.FCStd` is the model.** It is a PartDesign document: open it in
FreeCAD, edit the `Params` spreadsheet, and every sketch follows. Sketches are
constrained and bound to spreadsheet aliases by expression, and each attaches
to an origin plane rather than to a face of an earlier feature — face
references are what make PartDesign models break when something upstream
changes size.

`build_adxl_pod_pd.py` built it once and is kept only to show the layout. It is
not re-run; the `.FCStd` is the source now.

`adxl_pod.py` is the earlier Part-primitive implementation. Kept as a
reference for the geometry rationale and because its assertions document the
two collisions found during design. **Read-only** — edits go in the PartDesign
model. Regenerate its own output, if ever needed, with
`freecadcmd hardware/adxl_pod.py`.

Both produce the same part: pod 34.31 cm^3 in each, lid within 0.01 cm^3.

After editing the model, re-export `ADXL_Pod_PD.stl` and `ADXL_Pod_PD_Lid.stl`
for slicing.

## ADXL345 sensor pod

Bolt-on enclosure for a GY-291 ADXL345 breakout, with an RJ45 keystone jack for
a Cat5 drop back to the Pi. One pod per contact point — seat pan and pedal deck.

Part of the accelerometer feedback loop: the shaker app has no input, so every
level it reports is the buffer it computed, upstream of the DAC, amp and
transducer. It reads identically whether a shaker is working or its cable is
cut. An accelerometer on the frame is what turns "does this feel balanced?"
into a number.

| | |
| --- | --- |
| Outer | 66 x 40 x 36 mm, ~43 g pod + ~12 g lid in PLA |
| Rig bolt | M5, counterbored so the head finishes below the board |
| Board | GY-291, 15.0 mm hole spacing, M2.5 heat-set inserts |
| Jack | AMPCOM HKJ-801M keystone, 14.7 x 19.6 aperture, 2 mm local wall |
| Lid | Snap-fit, 0.4 mm bead with a 0.45 mm lead-in ramp, pry notch |

### Assembly

1. Heat-set two M2.5 inserts into the pedestal.
2. Punch the keystone down onto short **solid-core** pigtails — IDC terminals
   bite solid wire, and stranded works loose under vibration. Snap the jack in
   from outside.
3. Bolt the pod to the frame **before** fitting the board; it covers the
   counterbore.
4. Screw the ADXL345 down, wire it to the pigtails, snap the lid on.

Second pod ties SDO high for I2C address `0x1D`; both share one bus.

### Things that are load-bearing, not stylistic

- **The pedestal.** Mount stiffness *is* the measurement. A compliant path adds
  its own resonance, and if that lands under ~120 Hz it reads as rig behaviour.
  The walls are a shell and carry nothing.
- **The counterbore.** It exists so the board can straddle the bolt rather than
  stand off above its head — which would rebuild the compliance the pedestal is
  there to avoid.
- **The 2 mm wall at the aperture.** Keystone latches are made for wall-plate
  stock and will not grip 3 mm.
- **The tight snap groove.** A rattling lid is a noise source on a part built to
  measure vibration. If it still rattles once printed, damp it with tape.
- **The bead's lead-in ramp.** Insertion and removal should not be the same
  fight: the ramp deflects the lip gradually on the way in, while the square
  top shoulder still does the retaining. If the lid ends up too loose, raise
  `BEAD` rather than removing the ramp — that restores grip without making it
  hard to fit again.

### Before printing 55 g

The keystone snap and the lid snap both come from drawings rather than calipers,
and both sit within FDM tolerance of not fitting. Slice the first 5 mm in X (the
keystone wall) and a short length of lid rim, and check the fits first.
