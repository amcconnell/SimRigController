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

Inside the Pi, one Python process runs everything: an asyncio UDP client that decrypts (Salsa20) and parses GT7 packets, a FastAPI web app that hosts the tuning UI, and a PortAudio thread that mixes the configured effects into a single mono channel.

---

## Hardware

- **Raspberry Pi 4** (other Pi models work, but the deploy targets aarch64).
- **Bass shaker amp** — the project's current setup uses a Fosi Audio TP-02 mono amp; anything that takes a line-level input and drives a shaker works.
- **One or more bass shakers**, wired to the amp.
- **3.5 mm to RCA cable** from the Pi's analog jack to the amp.
- **Ethernet or Wi-Fi** on the Pi, on the same LAN as the PS5.

A USB DAC isn't required — the Pi's onboard analog jack is fine for shakers.

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

No license declared. If you'd like to use this, open an issue.
