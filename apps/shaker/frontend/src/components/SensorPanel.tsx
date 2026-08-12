import { useCallback, useState } from "react";

import { measureCrosstalk } from "../api/client";
import type { CrosstalkResult, PodStatus, SensorStatus } from "../types/config";

function g(v: number, digits = 3): string {
  return (v < 0 ? "−" : "+") + Math.abs(v).toFixed(digits);
}

/** Bar for a vibration magnitude. 1 g is far more than a shaker rig produces
 *  at a mounting point, so the scale is set to something the eye can use. */
const FULL_SCALE_G = 0.5;

interface SensorPanelProps {
  sensors: SensorStatus | null | undefined;
}

/** Accelerometer pods — what the rig delivers, rather than what it was sent.
 *
 * Written for installation as much as for measurement. While you are under the
 * seat with a pod in one hand, the questions are: is it detected, is it the
 * right way up, and does it see me tapping the frame. All three are answered
 * here without needing a terminal, and pods are re-probed on a timer so one
 * plugged in with the app running simply appears.
 */
export function SensorPanel({ sensors }: SensorPanelProps) {
  const s = sensors ?? null;

  if (!s || !s.enabled) {
    return (
      <div className="mb-4 rounded-lg border border-zinc-800 bg-zinc-900/50 p-4">
        <div className="text-sm font-semibold uppercase tracking-wider text-zinc-200">
          Accelerometer pods
        </div>
        <p className="mt-2 text-xs text-zinc-500">
          Disabled. Turn on <span className="font-mono">sensors.enabled</span> once the pods are
          wired — restart-required, so the app bounces itself.
        </p>
      </div>
    );
  }

  return (
    <div className="mb-4 rounded-lg border border-zinc-800 bg-zinc-900/50 p-4">
      <div className="mb-3 flex items-center justify-between">
        <span className="text-sm font-semibold uppercase tracking-wider text-zinc-200">
          Accelerometer pods
        </span>
        <span className="font-mono text-xs text-zinc-500">{s.bus}</span>
      </div>

      {!s.available && (
        <div className="mb-3 rounded border border-amber-900/50 bg-amber-950/20 p-3">
          <div className="text-xs font-semibold text-amber-200">Bus unavailable</div>
          <p className="mt-1 font-mono text-xs leading-relaxed text-zinc-400">{s.error}</p>
        </div>
      )}

      <div className="grid gap-3 sm:grid-cols-2">
        {s.pods.map((p) => (
          <PodCard key={p.name} pod={p} />
        ))}
      </div>

      <p className="mt-3 text-xs leading-relaxed text-zinc-500">
        <span className="text-zinc-300">Tilt</span> is the gravity vector and should read 1.000 g
        on a rig at rest — a figure well off that means the pod is loose or still moving.{" "}
        <span className="text-zinc-300">Orientation</span> is derived from it, so it tells you
        which way a pod ended up without having to read a silkscreen under a seat. Vibration has
        gravity removed, so tapping the frame should move it and standing still should not.
      </p>

      <Crosstalk enabled={s.any_present} />
    </div>
  );
}

const BANDS: Record<string, { label: string; tone: string }> = {
  good: { label: "Well separated", tone: "text-emerald-300" },
  usable: { label: "Some bleed", tone: "text-amber-300" },
  poor: { label: "Effectively mono", tone: "text-rose-300" },
};

/** Front/rear isolation, measured rather than argued about.
 *
 * Below about 100 Hz the body cannot localise a source — it reports which part
 * of itself is loaded, feet or back. So energy from the pedal-deck shaker that
 * reaches the seat is felt as rear, which is exactly the cue the two-channel
 * split exists to carry. Some coupling is realistic; the ratio decides.
 */
function Crosstalk({ enabled }: { enabled: boolean }) {
  const [result, setResult] = useState<CrosstalkResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const run = useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      setResult(await measureCrosstalk());
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }, []);

  const band = result?.ok ? BANDS[result.verdict] : undefined;

  return (
    <div className="mt-3 border-t border-zinc-800/80 pt-3">
      <div className="flex flex-wrap items-center gap-3">
        <button
          type="button"
          onClick={run}
          disabled={busy || !enabled}
          className="rounded-md bg-zinc-800 px-4 py-2 text-xs font-semibold uppercase tracking-wide text-zinc-200 transition hover:bg-zinc-700 disabled:opacity-50"
        >
          {busy ? "Measuring…" : "Measure crosstalk"}
        </button>
        <span className="text-xs text-zinc-500">
          {busy
            ? "pulsing each shaker alone — about five seconds"
            : "how much of each shaker reaches the other pod"}
        </span>
      </div>

      {error && <p className="mt-2 text-xs text-rose-300">{error}</p>}

      {result && !result.ok && (
        <p className="mt-3 text-xs leading-relaxed text-amber-300">{result.reason}</p>
      )}

      {result?.ok && (
        <>
          <div className="mt-3 grid gap-3 sm:grid-cols-2">
            <Ratio label="front reaching rear" db={result.front_to_rear_db} />
            <Ratio label="rear reaching front" db={result.rear_to_front_db} />
          </div>
          <div className={`mt-3 text-sm font-semibold ${band?.tone ?? "text-zinc-300"}`}>
            {band?.label}
          </div>
          <p className="mt-1 text-xs leading-relaxed text-zinc-500">{result.detail}</p>
          {result.warnings.map((w) => (
            <p key={w} className="mt-1 text-xs leading-relaxed text-zinc-600">
              {w}
            </p>
          ))}
        </>
      )}
    </div>
  );
}

function Ratio({ label, db }: { label: string; db: number }) {
  return (
    <div className="rounded border border-zinc-800/80 px-3 py-2">
      <div className="text-xs uppercase tracking-wider text-zinc-500">{label}</div>
      <div className="font-mono text-2xl tabular-nums text-zinc-200">
        {db.toFixed(1)}
        <span className="ml-1 text-sm text-zinc-500">dB</span>
      </div>
    </div>
  );
}

function PodCard({ pod }: { pod: PodStatus }) {
  const rmsPct = Math.min(1, pod.vibration_rms_g / FULL_SCALE_G) * 100;
  const peakPct = Math.min(1, pod.vibration_peak_g / FULL_SCALE_G) * 100;
  const tiltOk = pod.present && Math.abs(pod.tilt_g - 1.0) < 0.1;

  return (
    <div className="rounded border border-zinc-800/80 px-3 py-2">
      <div className="flex items-baseline justify-between">
        <span className="text-xs uppercase tracking-wider text-zinc-300">{pod.name}</span>
        <span className="font-mono text-[10px] text-zinc-600">{pod.address}</span>
      </div>

      {!pod.present ? (
        <>
          <div className="mt-1 text-sm text-zinc-500">Not detected</div>
          <p className="mt-1 text-xs leading-relaxed text-zinc-600">
            {pod.error ?? "Re-probed every couple of seconds — plug it in and it will appear."}
          </p>
        </>
      ) : (
        <>
          <div className="mt-1 grid grid-cols-3 gap-1 font-mono text-sm tabular-nums text-zinc-300">
            <span>x {g(pod.x, 2)}</span>
            <span>y {g(pod.y, 2)}</span>
            <span>z {g(pod.z, 2)}</span>
          </div>

          <div className="mt-2 flex items-center justify-between text-xs">
            <span className="text-zinc-500">
              tilt{" "}
              <span className={`font-mono tabular-nums ${tiltOk ? "text-zinc-300" : "text-amber-300"}`}>
                {pod.tilt_g.toFixed(3)} g
              </span>
            </span>
            <span className="text-zinc-500">
              facing <span className="font-mono text-zinc-300">{pod.orientation}</span>
            </span>
          </div>

          <div className="mt-2">
            <div className="flex justify-between text-[10px] uppercase tracking-wider text-zinc-600">
              <span>vibration</span>
              <span className="font-mono tabular-nums text-zinc-400">
                {pod.vibration_rms_g.toFixed(3)} g rms · {pod.vibration_peak_g.toFixed(2)} pk
              </span>
            </div>
            <div className="mt-1 h-1.5 overflow-hidden rounded-full bg-zinc-800">
              <div
                className="h-full rounded-full bg-zinc-500 transition-[width] duration-150"
                style={{ width: `${Math.max(rmsPct, peakPct * 0.15)}%` }}
              />
            </div>
          </div>

          <div className="mt-2 flex justify-between text-[10px] text-zinc-600">
            <span className="font-mono tabular-nums">{pod.rate_hz.toFixed(0)} Hz actual</span>
            <span className="font-mono tabular-nums">
              {pod.samples.toLocaleString()} samples
            </span>
          </div>
        </>
      )}
    </div>
  );
}
