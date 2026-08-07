import { useCallback, useEffect, useRef, useState } from "react";
import { getConfig, getStatus, listProfiles, putConfig, setMute } from "./api/client";
import type {
  AudioConfig,
  Config,
  ConfigUpdates,
  GT7Config,
  ProfilesState,
  Status,
  WebConfig,
} from "./types/config";
import { DEFAULT_PROFILE_NAME } from "./types/config";
import { StatusBar } from "./components/StatusBar";
import { EffectCard, SubFieldset } from "./components/EffectCard";
import { TestButton } from "./components/TestButton";
import { BoolField, NumberField, TextField } from "./components/ConfigField";
import { ProfileSelector } from "./components/ProfileSelector";
import { MuteButton } from "./components/MuteButton";
import { AxlePanel } from "./components/AxlePanel";
import { MotionPanel } from "./components/MotionPanel";
import { LimiterPanel } from "./components/LimiterPanel";
import { RecordPanel } from "./components/RecordPanel";

type SaveState = "idle" | "saving" | "saved" | "error";
type View = "tuning" | "rig" | "diagnostics";

export function App() {
  const [config, setConfig] = useState<Config | null>(null);
  const [status, setStatus] = useState<Status | null>(null);
  const [profiles, setProfiles] = useState<ProfilesState | null>(null);
  const [saveState, setSaveState] = useState<SaveState>("idle");
  const [view, setView] = useState<View>("tuning");
  const [error, setError] = useState<string | null>(null);
  // We hold a debounced PUT so per-keystroke fields don't spam the API.
  const pendingTimer = useRef<number | null>(null);
  const pendingUpdates = useRef<ConfigUpdates>({});

  useEffect(() => {
    getConfig().then(setConfig).catch((e) => setError(String(e)));
    listProfiles().then(setProfiles).catch((e) => setError(String(e)));
  }, []);

  useEffect(() => {
    const tick = () =>
      getStatus()
        .then(setStatus)
        .catch(() => {});
    tick();
    const id = window.setInterval(tick, 500);
    return () => window.clearInterval(id);
  }, []);

  const toggleMute = useCallback(async () => {
    const next = !(status?.muted ?? false);
    try {
      const result = await setMute(next);
      setStatus((s) => (s ? { ...s, muted: result.muted } : s));
    } catch (e) {
      setError(String(e));
    }
  }, [status?.muted]);

  // After a profile activation, audio config may have changed → refetch.
  const handleProfilesChange = useCallback((next: ProfilesState) => {
    setProfiles(next);
    getConfig().then(setConfig).catch((e) => setError(String(e)));
  }, []);

  const flush = useCallback(async () => {
    pendingTimer.current = null;
    const updates = pendingUpdates.current;
    pendingUpdates.current = {};
    if (Object.keys(updates).length === 0) return;
    setSaveState("saving");
    try {
      const fresh = await putConfig(updates);
      setConfig(fresh);
      setSaveState("saved");
      window.setTimeout(() => setSaveState((s) => (s === "saved" ? "idle" : s)), 1200);
    } catch (e) {
      setError(String(e));
      setSaveState("error");
    }
  }, []);

  const update = useCallback(
    <S extends keyof Config, K extends keyof Config[S]>(section: S, key: K, value: Config[S][K]) => {
      if (!config) return;
      setConfig({ ...config, [section]: { ...config[section], [key]: value } });
      const sectionPatch = (pendingUpdates.current[section] ?? {}) as Partial<Config[S]>;
      sectionPatch[key] = value;
      pendingUpdates.current[section] = sectionPatch as ConfigUpdates[S];
      if (pendingTimer.current !== null) window.clearTimeout(pendingTimer.current);
      pendingTimer.current = window.setTimeout(flush, 300);
    },
    [config, flush],
  );

  const a = (k: keyof AudioConfig) => (v: AudioConfig[typeof k]) => update("audio", k, v);
  const g = (k: keyof GT7Config) => (v: GT7Config[typeof k]) => update("gt7", k, v);
  const w = (k: keyof WebConfig) => (v: WebConfig[typeof k]) => update("web", k, v);

  if (!config) {
    return (
      <div className="flex h-screen items-center justify-center text-zinc-500">
        {error ?? "Loading config…"}
      </div>
    );
  }

  const A = config.audio;
  const isDefaultProfile = profiles?.active === DEFAULT_PROFILE_NAME;
  // Placement controls only exist on a two-channel rig; on one channel they
  // would be knobs that provably do nothing.
  const stereo = A.output_channels === 2;

  return (
    <>
      <StatusBar
        status={status}
        trailing={<MuteButton muted={status?.muted ?? false} onToggle={toggleMute} />}
      />
      <main className="mx-auto max-w-5xl px-4 py-6 pb-24">
        <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
          <h1 className="text-2xl font-bold tracking-tight text-zinc-100">SimRig Shaker</h1>
          <div className="flex items-center gap-3">
            <SaveIndicator state={saveState} error={error} />
            <nav className="flex rounded-lg border border-zinc-800 p-0.5" role="tablist">
              {(["tuning", "rig", "diagnostics"] as View[]).map((v) => (
                <button
                  key={v}
                  type="button"
                  role="tab"
                  aria-selected={view === v}
                  onClick={() => setView(v)}
                  className={`rounded-md px-3 py-1 text-xs font-medium uppercase tracking-wide transition ${
                    view === v
                      ? "bg-zinc-800 text-zinc-100"
                      : "text-zinc-500 hover:text-zinc-300"
                  }`}
                >
                  {v}
                </button>
              ))}
            </nav>
          </div>
        </div>

        {view === "diagnostics" && (
          <>
            <p className="mb-4 text-xs text-zinc-500">
              Read-only. These measure what the rig is actually doing — whether the output is
              being compressed, and whether the protocol assumptions still hold from the
              driver's seat. Nothing here changes what the rig does.
            </p>
            <RecordPanel recording={status?.recording} onError={setError} />
            <LimiterPanel limiter={status?.limiter} />
            <MotionPanel motion={status?.motion} />
            <AxlePanel axle={status?.axle} />
          </>
        )}

        {view === "rig" && (
        <>
        <p className="mb-4 text-xs text-zinc-500">
          Facts about the machine rather than how you want it to feel — which card, how it is
          buffered, how many amplifier channels are wired, and where the PS5 is. Profiles
          neither store nor restore any of it (see <span className="font-mono">RIG_FIELDS</span>
          {" "}in <span className="font-mono">config.py</span>), so switching profiles leaves this
          screen alone and nothing here is locked by the read-only default profile.
        </p>

        <section className="mb-6 rounded-lg border border-zinc-800 bg-zinc-900/50 p-4">
          <div className="mb-3 flex flex-wrap items-baseline justify-between gap-2">
            <h2 className="text-sm font-semibold uppercase tracking-wider text-zinc-200">
              Audio hardware
            </h2>
            <span className="text-xs text-zinc-500">restart-required</span>
          </div>
          <div className="grid gap-x-8 gap-y-1 sm:grid-cols-2">
            <TextField label="Audio device" value={A.device} placeholder="default" onChange={a("device")}
              hint='ALSA / CoreAudio device name. "default" uses the system default; otherwise enter a substring match (e.g. "External Headphones"). Restart-required.' />
            <NumberField label="Sample rate" value={A.sample_rate} step={1000} min={8000} onChange={a("sample_rate")}
              hint="Audio sample rate in Hz. 48000 is standard. Changing this restarts the audio stream." />
            <NumberField label="Buffer" unit="ms" value={A.buffer_ms} step={1} min={1} max={200} onChange={a("buffer_ms")}
              hint="Audio callback buffer length. Smaller = lower latency but more CPU and risk of underruns. Restart-required." />
            <NumberField label="Output channels" value={A.output_channels} step={1} min={1} max={2} onChange={a("output_channels")}
              hint="1 = single mono output, exactly as before. 2 = left drives the front shaker, right drives the rear. Needs two amp channels actually wired — nothing can detect that for you, so run the wiring check after switching. Restart-required." />
            {stereo && (
              <NumberField label="Rear trim" value={A.rear_gain_trim} step={0.05} min={0} max={2} onChange={a("rear_gain_trim")}
                hint="Level correction for the rear shaker only. A seat transmits far more of what it is given than a pedal deck does, so equal electrical power will not feel equal — fix that here and leave the per-effect placement sliders for taste. Belongs to the rig, so it does not move when you switch profiles." />
            )}
          </div>
          {stereo && (
            <div className="mt-3 flex items-center gap-3 border-t border-zinc-800 pt-3">
              <TestButton label="wiring" test="wiring" />
              <p className="text-xs text-zinc-500">
                Pulses front, pauses, then pulses rear. Bypasses every effect <em>and</em> master
                gain, rear trim and the limiter — both pulses are identical in software, so any
                difference you feel is your rig, not your settings. That makes it the right way to
                judge rear trim, but it will not show the trim you set: verify that with Test rev
                limit, which is centred and goes through the full chain. If the order is reversed,
                swap the speaker leads at the amp rather than inverting the sliders.
              </p>
            </div>
          )}
        </section>

        <details className="rounded-lg border border-zinc-800 bg-zinc-900/50 p-4 open:pb-3">
          <summary className="cursor-pointer text-sm font-semibold uppercase tracking-wider text-zinc-200">
            Network
          </summary>
          <div className="mt-3 grid gap-x-8 gap-y-1 sm:grid-cols-2">
            <TextField label="PS5 IP override" value={config.gt7.ps5_ip ?? ""} placeholder="(autodiscover)" onChange={(v) => g("ps5_ip")(v === "" ? null : v)}
              hint="Skip autodiscovery and target this IP. Leave empty to broadcast and use the first PS5 that responds." />
            <NumberField label="Heartbeat" unit="s" value={config.gt7.heartbeat_interval_s} step={0.5} min={0.5} max={30} onChange={g("heartbeat_interval_s")}
              hint="How often (in seconds) we ping the PS5 to keep telemetry flowing. GT7 stops sending if heartbeats stop." />
            <NumberField label="Discovery timeout" unit="s" value={config.gt7.discovery_timeout_s} step={1} min={1} max={120} onChange={g("discovery_timeout_s")}
              hint="How long broadcast discovery runs before logging a timeout. Discovery itself never stops, this just controls the warning." />
            <TextField label="Web host" value={config.web.host} onChange={w("host")}
              hint='Bind address. "0.0.0.0" exposes the UI to the LAN; "127.0.0.1" restricts to localhost. Restart-required.' />
            <NumberField label="Web port" value={config.web.port} step={1} min={1} max={65535} onChange={w("port")}
              hint="HTTP server port. 80 needs CAP_NET_BIND_SERVICE (already granted via the systemd unit). Restart-required." />
          </div>
        </details>
        </>
        )}

        {view === "tuning" && profiles && (
          <div className="mb-4 rounded-lg border border-zinc-800 bg-zinc-900/50 p-3">
            <ProfileSelector state={profiles} onChange={handleProfilesChange} onError={setError} />
            {isDefaultProfile && (
              <p className="mt-2 text-xs text-zinc-500">
                The <span className="font-mono">default</span> profile is built-in and read-only.
                Create a new profile to tune values.
              </p>
            )}
          </div>
        )}

        {view === "tuning" && (
        <>
        <fieldset disabled={isDefaultProfile} className="space-y-6 disabled:opacity-60">

        <section className="rounded-lg border border-zinc-800 bg-zinc-900/50 p-4">
          <h2 className="mb-3 text-sm font-semibold uppercase tracking-wider text-zinc-200">Master</h2>
          <div className="grid gap-x-8 gap-y-1 sm:grid-cols-2">
            <NumberField label="Master gain" value={A.master_gain} step={0.05} min={0} max={2} onChange={a("master_gain")}
              hint="Overall output multiplier applied after mixing all effects, before the limiter. Watch Diagnostics → Output limiter rather than guessing: if it is reducing through most corners, this is too high and the rig is being compressed rather than driven." />
          </div>
        </section>

        <div className="space-y-3">
          <EffectCard
            title="Road vibration"
            hint="Bandpass noise driven by suspension high-pass content. Bumps and surface texture."
            enabled={A.vibration_enabled}
            gain={A.vibration_gain}
            onEnabledChange={a("vibration_enabled")}
            onGainChange={a("vibration_gain")}
            testButton={<TestButton label="vibration" test="vibration" variant="header" />}
          >
            <SubFieldset legend="Response filter">
              <NumberField label="Input gain" unit="%" value={A.vibration_input_gain_pct} step={5} min={0} max={400} onChange={a("vibration_input_gain_pct")}
                hint="Multiplier on the suspension-derived signal before threshold/gamma shaping. 100% = no change. Above 100% boosts faint bumps." />
              <NumberField label="Threshold" unit="%" value={A.vibration_threshold_pct} step={1} min={0} max={100} onChange={a("vibration_threshold_pct")}
                hint="Suspension activity below this percentage produces silence. Raise to gate out idle noise; lower to feel small ripples." />
              <NumberField label="Min force" unit="%" value={A.vibration_min_force_pct} step={1} min={0} max={100} onChange={a("vibration_min_force_pct")}
                hint="Output floor once threshold is cleared. Lifts faint bumps to felt level so they don't fade in from imperceptible." />
              <NumberField label="Gamma" value={A.vibration_gamma} step={0.05} min={0.1} max={4} onChange={a("vibration_gamma")}
                hint="Response curve shape. 1 = linear. Less than 1 emphasizes mild inputs (everything feels alive). Greater than 1 only lets big hits through." />
            </SubFieldset>
            <SubFieldset legend="Speed blend (high-band mix-in)">
              <NumberField label="Blend start" unit="m/s" value={A.vibration_speed_blend_low_mps} step={1} min={0} max={120} onChange={a("vibration_speed_blend_low_mps")}
                hint="Below this speed only the low band (44–50 Hz) plays. Default 20 m/s ≈ 72 km/h." />
              <NumberField label="Blend full" unit="m/s" value={A.vibration_speed_blend_high_mps} step={1} min={0} max={120} onChange={a("vibration_speed_blend_high_mps")}
                hint="At or above this speed the high band (60–80 Hz) is fully mixed in. Linear blend between start and full." />
            </SubFieldset>
          </EffectCard>

          <EffectCard
            title="Engine rumble"
            hint="Continuous-phase sine derived from engine RPM. Amplitude follows throttle — the chassis thrum of a running engine."
            enabled={A.engine_rumble_enabled}
            gain={A.engine_rumble_gain}
            onEnabledChange={a("engine_rumble_enabled")}
            onGainChange={a("engine_rumble_gain")}
            bias={stereo ? A.engine_rumble_bias : undefined}
            onBiasChange={a("engine_rumble_bias")}
            biasHint="Always yours — engine placement is deliberately not taken from the car database, even though it knows where the engine sits. Measured on a front-engine car with a seat shaker: routing by engine position sends the most continuous effect in the mix to the pedals, and it belongs in the seat. Thrum reaches you through the floor and seat back whatever end the engine is at."
            testButton={<TestButton label="sweep" test="engine_sweep" variant="header" />}
          >
            <NumberField label="RPM divisor" value={A.engine_rumble_rpm_divisor} step={5} min={10} max={240} onChange={a("engine_rumble_rpm_divisor")}
              hint="Output frequency = engine_rpm / divisor. Default 60 means 100 Hz at 6000 RPM, 25 Hz at 1500 RPM. Lower divisor = higher pitch." />
            <SubFieldset legend="Frequency limits">
              <NumberField label="Min" unit="Hz" value={A.engine_rumble_min_hz} step={1} min={10} max={80} onChange={a("engine_rumble_min_hz")}
                hint="Floor for the derived frequency. Below ~20 Hz a shaker moves but you feel almost nothing, so low-RPM lugging just wastes headroom." />
              <NumberField label="Max" unit="Hz" value={A.engine_rumble_max_hz} step={1} min={40} max={160} onChange={a("engine_rumble_max_hz")}
                hint="Ceiling for the derived frequency. Above ~120 Hz the shaker stops shaking and starts buzzing audibly through the rig frame — at divisor 60 that's every pull past 7000 RPM." />
            </SubFieldset>
          </EffectCard>

          <EffectCard
            title="Brake rumble"
            hint="Low-frequency hum whose amplitude ramps with brake pressure above a threshold."
            enabled={A.brake_rumble_enabled}
            gain={A.brake_rumble_gain}
            onEnabledChange={a("brake_rumble_enabled")}
            onGainChange={a("brake_rumble_gain")}
            bias={stereo ? A.brake_rumble_bias : undefined}
            onBiasChange={a("brake_rumble_bias")}
            biasHint="Strongly front, and this one is physical: pad judder, ABS pulsing and tyre scrub reach a real driver through the brake pedal, so a pedal-deck shaker reproduces the actual channel rather than approximating it."
            testButton={<TestButton label="brake" test="brake_rumble" variant="header" />}
          >
            <NumberField label="Frequency" unit="Hz" value={A.brake_rumble_freq_hz} step={1} min={10} max={200} onChange={a("brake_rumble_freq_hz")}
              hint="Sine wave frequency. 25–35 Hz feels chest-thumping; 40+ Hz starts to buzz." />
            <NumberField label="Threshold" unit="%" value={A.brake_rumble_threshold_pct} step={1} min={0} max={100} onChange={a("brake_rumble_threshold_pct")}
              hint="Brake input below this percentage is silent. Raise to ignore light taps and trail-braking." />
          </EffectCard>

          <EffectCard
            title="Rev limiter"
            hint="Distinct buzz when engine_rpm / max_alert_rpm crosses the trigger threshold."
            enabled={A.rev_limiter_enabled}
            gain={A.rev_limiter_gain}
            onEnabledChange={a("rev_limiter_enabled")}
            onGainChange={a("rev_limiter_gain")}
            bias={stereo ? A.rev_limiter_bias : undefined}
            onBiasChange={a("rev_limiter_bias")}
            biasHint="Centred, and it should probably stay there — RPM over redline is a scalar ratio with no spatial content, so placing it anywhere in particular is inventing information."
            testButton={<TestButton label="rev limit" test="rev_limiter" variant="header" />}
          >
            <NumberField label="Frequency" unit="Hz" value={A.rev_limiter_freq_hz} step={1} min={10} max={200} onChange={a("rev_limiter_freq_hz")}
              hint="Sine wave frequency. Higher than engine rumble's typical range so the limiter is distinct." />
            <NumberField label="Trigger" unit="%" value={A.rev_limiter_trigger_pct} step={1} min={80} max={100} onChange={a("rev_limiter_trigger_pct")}
              hint="Fires when engine_rpm / max_alert_rpm crosses this. 95% catches the approach to redline; 99% only the bouncing-off-limiter feel." />
          </EffectCard>

          <EffectCard
            title="Wheel slip / lockup"
            hint="Buzz when any wheel's surface speed diverges from vehicle speed — catches wheelspin and lockup with one effect."
            enabled={A.wheel_slip_enabled}
            gain={A.wheel_slip_gain}
            onEnabledChange={a("wheel_slip_enabled")}
            onGainChange={a("wheel_slip_gain")}
            testButton={<TestButton label="slip" test="wheel_slip" variant="header" />}
          >
            <NumberField label="Frequency" unit="Hz" value={A.wheel_slip_freq_hz} step={1} min={10} max={200} onChange={a("wheel_slip_freq_hz")}
              hint="Sharper than engine/brake bands so spin and lockup feel like a distinct signal." />
            <NumberField label="Lockup frequency" unit="Hz" value={A.wheel_slip_lock_freq_hz} step={1} min={10} max={200} onChange={a("wheel_slip_lock_freq_hz")}
              hint="Locking wheels get their own, lower voice than spinning ones — a locked tyre grinds where a spinning one scrabbles. On two channels the axle that slipped picks the shaker, so a front lockup and a rear break-away become two distinct events instead of one buzz." />
            <NumberField label="Threshold" unit="% slip" value={A.wheel_slip_threshold_pct} step={0.5} min={0} max={50} onChange={a("wheel_slip_threshold_pct")}
              hint="How far a wheel's speed may diverge from the car's, as a percentage of vehicle speed, before this fires. A ratio rather than an absolute speed because that is what a tyre responds to — peak grip sits near 10-20% whatever the speed. 8% gives warning just before the limit; lower it for earlier warning at the cost of firing during hard-but-controlled driving." />
            <NumberField label="Scale" unit="% slip" value={A.wheel_slip_scale_pct} step={0.5} min={0.5} max={50} onChange={a("wheel_slip_scale_pct")}
              hint="Slip ratio above the threshold at which the effect reaches full strength. Smaller = a steeper ramp, so early slip is felt more strongly. 12% means full effect once you are 12 points past the threshold." />
            <NumberField label="Minimum slip" unit="m/s" value={A.wheel_slip_threshold_mps} step={0.1} min={0} max={5} onChange={a("wheel_slip_threshold_mps")}
              hint="Absolute floor applied on top of the ratio. Near a standstill the ratio denominator collapses and any wheel twitch reads as a huge slide, so this keeps a parked car and the pit lane quiet. Rarely needs changing." />
          </EffectCard>

          <EffectCard
            title="Gear shift"
            hint="Short percussive thump on each upshift or downshift (forward gears 1–8 and reverse)."
            enabled={A.gear_shift_enabled}
            gain={A.gear_shift_gain}
            onEnabledChange={a("gear_shift_enabled")}
            onGainChange={a("gear_shift_gain")}
            bias={stereo ? A.gear_shift_bias : undefined}
            onBiasChange={a("gear_shift_bias")}
            biasHint="Driveline shock reacts through the driven axle. NOTE: with \u201cUse car drivetrain\u201d on, the database picks the end and only the *magnitude* of this slider is used — so dragging it toward Front will not move a rear-drive car forward. Turn that option off if you want this slider to be authoritative."
            testButton={<TestButton label="shift" test="gear_shift" variant="header" />}
          >
            <NumberField label="Frequency" unit="Hz" value={A.gear_shift_freq_hz} step={1} min={10} max={200} onChange={a("gear_shift_freq_hz")}
              hint="Thump frequency. Lower = deeper, higher = sharper click." />
            <NumberField label="Duration" unit="ms" value={A.gear_shift_duration_ms} step={5} min={10} max={500} onChange={a("gear_shift_duration_ms")}
              hint="How long the thump lasts. Squared-decay envelope inside that window." />
            <BoolField label="Use car drivetrain" value={A.drivetrain_routing_enabled} onChange={a("drivetrain_routing_enabled")}
              hint="Look the car up by its telemetry car code and place the GEAR SHIFT by which axle is driven — its bias slider then sets strength only, not side. Engine placement is never routed and always follows its own slider. Unrecognised cars fall back to the sliders exactly as set." />
            <SubFieldset legend="RPM-based gain modulation">
              <NumberField label="RPM ramp start" unit="%" value={A.gear_shift_rpm_pct_low} step={1} min={0} max={100} onChange={a("gear_shift_rpm_pct_low")}
                hint="Below this RPM%, gear-shift gain is at the low-RPM value." />
              <NumberField label="RPM ramp end" unit="%" value={A.gear_shift_rpm_pct_high} step={1} min={0} max={100} onChange={a("gear_shift_rpm_pct_high")}
                hint="Above this RPM%, gear-shift gain is at the high-RPM value. Linear ramp between start and end." />
              <NumberField label="Gain at low RPM" unit="%" value={A.gear_shift_min_gain_pct} step={1} min={0} max={200} onChange={a("gear_shift_min_gain_pct")}
                hint="Multiplier on gear_shift_gain when below the ramp start." />
              <NumberField label="Gain at high RPM" unit="%" value={A.gear_shift_max_gain_pct} step={1} min={0} max={200} onChange={a("gear_shift_max_gain_pct")}
                hint="Multiplier on gear_shift_gain when above the ramp end. Lets high-rev shifts feel meatier." />
            </SubFieldset>
          </EffectCard>
        </div>

        </fieldset>

        </>
        )}
      </main>
    </>
  );
}

function SaveIndicator({ state, error }: { state: SaveState; error: string | null }) {
  if (state === "saving") return <span className="text-xs text-zinc-500">saving…</span>;
  if (state === "saved") return <span className="text-xs text-emerald-400">saved</span>;
  if (state === "error") return <span className="text-xs text-rose-400" title={error ?? undefined}>error</span>;
  return null;
}
