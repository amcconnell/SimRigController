# SimRigController

A Raspberry Pi appliance that turns Gran Turismo 7 telemetry from a PS5 into haptic feedback through bass shakers mounted to a sim-racing rig. The Pi listens for GT7's UDP telemetry on the LAN, derives audio effects (road bumps, engine rumble, brake/rev-limiter/wheel-slip buzz, gear-shift thumps) from packet contents in real time, and outputs to a shaker amp through a 3.5 mm jack (or DAC). A small web UI on the Pi lets you tune everything live from a browser or phone.

Originally built to replace a Windows tablet running SimHub just for shaker effects.

---

## How it works

```
PS5 (GT7)  ──UDP/33740──►  Pi 4 (shaker app)  ──audio──►  Fosi TP-02  ──speaker wire──►  bass shaker(s)
                                  ▲
                                  │ HTTP :80
                                  ▼
                            Browser / phone
                            (http://simrig-pi.local)
```

Inside the Pi, one Python process runs everything: an asyncio UDP client that decrypts (Salsa20) and parses GT7 packets, a FastAPI web app that hosts the tuning UI, and a PortAudio thread that mixes the configured effects.

Output is mono by default. Set `output_channels = 2` and the mix splits front/rear —
channel 0 (left) drives a front shaker, channel 1 (right) a rear one. Road vibration and
wheel slip are split by axle from real per-corner telemetry; the remaining effects are
placed with a per-effect bias slider. Setting it back to 1 reproduces the single-channel
output exactly, so it is a genuine escape hatch rather than a re-tune.

---

## Hardware

- **Raspberry Pi 4** (other Pi models work, but the deploy targets aarch64).
- **Bass shaker amp** — anything taking a line-level input and driving a shaker works.
  For front/rear you need *two amplifier channels*. Count the **speaker binding posts, not
  the input jacks**: mono subwoofer amps commonly have two RCA inputs that are summed
  internally to one channel, which merges front and rear back together.

  The rig here runs a **Fosi Audio V1.0G**: 2 channels, 50 W + 50 W, 2–8 Ω, 20 Hz–20 kHz
  ±1 dB, **input sensitivity ≤ 280 mV**, 19 V 4.74 A supply (12–24 V accepted),
  130 × 100 × 35 mm. Note the sensitivity — see [Audio output](#audio-output).
- **One or two bass shakers.** Two only helps if they are mounted somewhere your body can
  tell apart — a pedal deck and a seat work well, since that is feet versus back rather
  than asking you to localise a 50 Hz wave in space. On a bridged (BTL) stereo amp, wire
  **one shaker per channel**; two in parallel halves the impedance below what most boards
  tolerate.
- **A USB DAC** — see [Audio output](#audio-output). Worth having.
- **Ethernet or Wi-Fi** on the Pi, on the same LAN as the PS5.

---

## Audio output

The Pi's 3.5 mm jack is a **PWM-derived headphone output at roughly 0.4 V RMS**, where a
line output is about 2 V. A **class-compliant USB DAC** (~$10–30) gives you the latter, and
one with RCA outputs also removes the 3.5 mm splitter from the chain, which is worth doing
on its own.

On this rig the DAC was what finally produced usable output. Worth saying plainly, because
the datasheets do not predict that: the V1.0G is specified to reach rated output from
280 mV and the Pi's jack nominally clears that, so on paper the converter should have been
the only thing that changed. It was not. Treat published sensitivity figures as best-case
numbers and the Pi's PWM level as approximate — neither survives contact with a real rig,
and the amplifier's own volume control is the only calibration that counts.

Two things do come free with the swap. Getting off the PWM output improves low-level
linearity, which is where quiet road texture lives; and a DAC with **RCA outputs** deletes
the 3.5 mm splitter, a part that already cost an evening here when one conductor turned out
to be dead and presented as the software failing to separate channels.

Set the device by name substring in the UI (`aplay -l` to find it). Restart-required, so
the app bounces itself.

**USB adapters routinely enumerate attenuated.** The C-Media dongle used here comes up at
**-20 dB** — quieter than the Pi's own jack, and indistinguishable from "the DAC made no
difference", because nothing in the app can see it. The `simrig-alsa` unit installed by the
deploy sets output levels at boot, before the shaker service starts; see
`simrig_alsa_levels` in the `shaker_app` role. If a new adapter is quiet, check
`amixer -c <card> scontrols` first.

**Gain structure**, in order:

1. **Source at unity** — the DAC's own mixer at 100%, so the full converter range is used.
2. **Absolute level at the amplifier.** Set it once and mark the knob: everything tuned in
   software afterwards is relative to it.
3. **`master_gain` below the limiter.** If peaks sit at the ceiling the limiter clips the
   top off every transient, and the contrast between road texture and a kerb strike — the
   thing the effects exist to convey — goes flat. Around 0.5 suits a line-level source.
   **Diagnostics → Output limiter** measures this rather than leaving it to feel: it reports
   gain reduction in dB and the share of recent time spent reducing. Occasional reduction on
   the biggest hits is the limiter doing its job; reducing through most corners means the
   rig is being compressed, and the fix is less `master_gain` and more amplifier.
4. **`rear_gain_trim`** for the mechanical asymmetry between the two mounting points, then
   the per-effect bias sliders for placement.

Judge trim with **Test wiring**: both its pulses are identical in software and bypass
master gain, trim and the limiter, so any difference you feel is the rig rather than the
settings. Verify the trim you then set with **Test rev limit**, which is centred and goes
through the full chain.

**Run Test wiring before trusting any routing.** Nothing in software can detect reversed
speaker leads, and a swapped pair inverts every routing decision in a way that reads as
"feels subtly wrong" rather than as a fault.

---

## Pi setup (one-time)

Flash a fresh SD card with **Raspberry Pi Imager**:

1. Choose **Raspberry Pi OS Lite (64-bit, Bookworm)**.
2. Open advanced settings (gear icon) and set:
   - **Hostname**: `simrig-pi`
   - **Username**: `simrig`
   - **Password**: anything you'll remember (you'll need it for `sudo` during `ansible-deploy`).
   - **Wi-Fi** credentials (or use Ethernet).
   - **SSH**: enabled, with your Mac's public key authorized.
3. Boot the Pi, then from the Mac:

   ```sh
   ssh simrig@simrig-pi.local
   ```

   Confirm it logs in without a password prompt. Exit.

> If you use a different hostname or username, edit `ansible/inventory.yml` accordingly — `simrig-pi.local` and `simrig` are baked-in defaults the playbook assumes.

The Pi only needs SSH access and password-protected `sudo`. The deploy creates a separate unprivileged `shaker` user that the service runs as.

---

## Controller (Mac) setup

```sh
brew install ansible uv node
make ansible-deps        # one-time: pulls ansible.posix collection
make frontend-install    # one-time: installs npm deps for the UI
```

---

## Deploy

```sh
make ansible-deploy
```

This will:
- Install system packages (`libportaudio2`, `rsync`, etc.) on the Pi.
- Install [uv](https://docs.astral.sh/uv/) and pin it to a known version.
- Build the React UI on the Mac and rsync the bundle to the Pi.
- Create the `shaker` system user and `/opt/simrig/shaker/`.
- Run `uv sync --frozen` on the Pi to build a Python venv.
- Install and start the `shaker.service` systemd unit.

You'll be prompted once for the Pi's `sudo` password. The deploy is idempotent — re-running it is the normal way to ship code changes.

When it finishes, open **http://simrig-pi.local** on any LAN device.

### Useful targets

| Command                    | What it does                                                    |
| -------------------------- | --------------------------------------------------------------- |
| `make ansible-ping`        | Confirms SSH/Python connectivity to the Pi.                     |
| `make ansible-check`       | Dry-run with diff, no changes applied.                          |
| `make ansible-deploy`      | The real thing.                                                 |
| `make frontend-build`      | Build the UI bundle locally (chained by `ansible-deploy`).      |
| `make frontend-dev`        | Vite dev server, proxies `/api/*` to the live Pi.               |
| `make dev`                 | Run the Python service locally on the Mac (for backend hacking).|
| `make test` / `make lint`  | Python tests / ruff.                                            |

---

## Using the app

1. Make sure GT7 is **running on the PS5** and you're in an active session.
2. Open **http://simrig-pi.local** — the status pill should switch from `discovering` to `connected` within a few seconds.
3. The default profile is read-only — click **+ New** to create a tunable profile.
4. Drag sliders to taste; changes save automatically (~300 ms debounce).
5. The test buttons at the top of each effect fire a synthetic input so you can verify the audio path without driving — useful for setting a baseline gain.
6. On two channels, run **Test wiring** first — it pulses the front shaker, pauses, then the
   rear, bypassing every effect. Nothing in software can detect reversed speaker leads, and a
   swapped pair inverts every routing decision in a way that reads as "feels subtly wrong"
   rather than as a fault.

**Profiles** are named snapshots of the audio config. The built-in `default` profile lives in code and can't be edited or deleted. Use profiles to keep separate tunings per car or per driver — switching activates a profile's settings instantly (the service restarts only if you change the audio device, sample rate, or buffer size).

**Mute** toggles output without changing any config — useful for phone calls. It's in-memory only and resets if the service restarts.

---

## Configuration

The Pi stores two config files in `/opt/simrig/shaker/config/`:

- **`shaker.toml`** — the live config (`[gt7]`, `[web]`, `[audio]` sections). The audio section is overwritten when you activate a profile. Edits via the web UI write here.
- **`profiles.json`** — your named profiles and the currently-active one. Created on first deploy, never overwritten on re-deploy.

You can SSH in and edit `shaker.toml` directly; the service watches the file and reloads on save. Restart-required changes (web host/port, audio device/sample-rate/buffer) trigger a clean systemd restart.

---

## Repo layout

```
SimRigController/
├── ansible/                 # Idempotent deployment to the Pi
│   ├── inventory.yml
│   ├── site.yml
│   └── roles/{base,python_runtime,shaker_app}/
├── apps/shaker/             # The Python app
│   ├── src/shaker/
│   │   ├── audio/           # Effect generators + mixer
│   │   ├── gt7/             # Salsa20 decrypt + packet parser + UDP client
│   │   ├── web/             # FastAPI + built React bundle
│   │   ├── config.py        # AudioConfig / Config dataclasses
│   │   ├── profiles.py      # Profile CRUD
│   │   └── runtime.py       # asyncio orchestrator
│   ├── tests/
│   ├── frontend/            # Vite + React + TS + Tailwind UI source
│   └── config/shaker.toml   # Default values shipped with the repo
└── Makefile
```

---

## Credits

The GT7 telemetry protocol (UDP port, Salsa20 key, packet layout) is community reverse-engineering; the Python port here is based on prior work from [gt_telem](https://github.com/snipem/gt-telem) and PDTools.

---

## License

MIT — see [LICENSE](LICENSE).
