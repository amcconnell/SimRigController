import { useCallback, useEffect, useRef, useState } from "react";

import { readSweep, startSweep } from "../api/client";
import type { SensorStatus, SweepCurvePoint, SweepStatus } from "../types/config";

/** Chart geometry. A viewBox rather than pixels so it scales to a phone held
 *  at the rig, which is where this gets read. */
const W = 640;
const H = 260;
const PAD = { top: 14, right: 14, bottom: 28, left: 44 };

const AXIS = "#3f3f46";      // zinc-700
const MUTED = "#71717a";     // zinc-500
const ISOLATION = "#34d399"; // emerald-400
const FRONT = "#60a5fa";     // blue-400
const REAR = "#f472b6";      // pink-400
const WORST = "#fbbf24";     // amber-400

const TICKS = [15, 20, 25, 30, 40, 50, 60, 80, 100, 120];

function db(v: number | null | undefined, digits = 1): string {
  if (v === null || v === undefined) return "—";
  return (v < 0 ? "−" : "+") + Math.abs(v).toFixed(digits);
}

interface Scale {
  x: (hz: number) => number;
  y: (dbv: number) => number;
  loHz: number;
  hiHz: number;
  loDb: number;
  hiDb: number;
}

/** Log in frequency, because structures behave geometrically: an octave is an
 *  octave whether it starts at 20 Hz or 80. Linear spacing would squeeze the
 *  octave the mount resonance lives in into the left margin. */
function makeScale(curve: SweepCurvePoint[]): Scale | null {
  const freqs = curve.map((c) => c.freq_hz).filter((f) => f > 0);
  if (freqs.length < 2) return null;
  const loHz = Math.min(...freqs);
  const hiHz = Math.max(...freqs);
  const values = curve
    .flatMap((c) => [c.isolation_db, c.front_to_rear_db, c.rear_to_front_db])
    .filter((v): v is number => v !== null && v !== undefined);
  const hiDb = Math.max(6, ...values);
  const loDb = Math.min(-30, ...values);
  const lx = Math.log(loHz);
  const rx = Math.log(hiHz);
  const innerW = W - PAD.left - PAD.right;
  const innerH = H - PAD.top - PAD.bottom;
  return {
    loHz,
    hiHz,
    loDb,
    hiDb,
    x: (hz) => PAD.left + ((Math.log(Math.max(hz, 1e-6)) - lx) / (rx - lx)) * innerW,
    y: (v) => PAD.top + ((hiDb - v) / (hiDb - loDb)) * innerH,
  };
}

function path(
  curve: SweepCurvePoint[],
  scale: Scale,
  pick: (c: SweepCurvePoint) => number | null,
): string {
  // Breaks the line at gaps rather than bridging them. A straight segment
  // drawn across three frequencies that were never measured is an invented
  // measurement, and on a log axis it looks entirely plausible.
  let d = "";
  let pen = false;
  for (const c of curve) {
    const v = pick(c);
    if (v === null || v === undefined) {
      pen = false;
      continue;
    }
    d += `${pen ? "L" : "M"}${scale.x(c.freq_hz).toFixed(1)},${scale.y(v).toFixed(1)} `;
    pen = true;
  }
  return d.trim();
}

function Chart({ curve, worstHz, bestHz }: {
  curve: SweepCurvePoint[];
  worstHz?: number;
  bestHz?: number;
}) {
  const scale = makeScale(curve);
  if (!scale) return null;

  const gridDb: number[] = [];
  for (let v = Math.ceil(scale.hiDb / 6) * 6; v >= scale.loDb; v -= 6) gridDb.push(v);

  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="w-full" role="img"
         aria-label="Isolation against frequency">
      {gridDb.map((v) => (
        <g key={v}>
          <line x1={PAD.left} x2={W - PAD.right} y1={scale.y(v)} y2={scale.y(v)}
                stroke={v === 0 ? MUTED : AXIS} strokeWidth={v === 0 ? 1 : 0.5} />
          <text x={PAD.left - 6} y={scale.y(v) + 3} textAnchor="end"
                fontSize="9" fill={MUTED}>{v}</text>
        </g>
      ))}

      {TICKS.filter((f) => f >= scale.loHz && f <= scale.hiHz).map((f) => (
        <g key={f}>
          <line x1={scale.x(f)} x2={scale.x(f)} y1={PAD.top} y2={H - PAD.bottom}
                stroke={AXIS} strokeWidth={0.5} />
          <text x={scale.x(f)} y={H - PAD.bottom + 12} textAnchor="middle"
                fontSize="9" fill={MUTED}>{f}</text>
        </g>
      ))}
      <text x={(W - PAD.left) / 2} y={H - 2} textAnchor="middle" fontSize="9" fill={MUTED}>
        Hz
      </text>

      {worstHz !== undefined && (
        <g>
          <line x1={scale.x(worstHz)} x2={scale.x(worstHz)}
                y1={PAD.top} y2={H - PAD.bottom}
                stroke={WORST} strokeWidth={1} strokeDasharray="4 3" />
          <text x={scale.x(worstHz) + 4} y={PAD.top + 10}
                fontSize="9" fill={WORST}>worst</text>
        </g>
      )}
      {bestHz !== undefined && (
        <g>
          <line x1={scale.x(bestHz)} x2={scale.x(bestHz)}
                y1={PAD.top} y2={H - PAD.bottom}
                stroke={ISOLATION} strokeWidth={1} strokeDasharray="2 4" />
          <text x={scale.x(bestHz) + 4} y={PAD.top + 10}
                fontSize="9" fill={ISOLATION}>best</text>
        </g>
      )}

      <path d={path(curve, scale, (c) => c.front_to_rear_db)} fill="none"
            stroke={FRONT} strokeWidth={1} opacity={0.55} />
      <path d={path(curve, scale, (c) => c.rear_to_front_db)} fill="none"
            stroke={REAR} strokeWidth={1} opacity={0.55} />
      <path d={path(curve, scale, (c) => c.isolation_db)} fill="none"
            stroke={ISOLATION} strokeWidth={2} />

      {curve.map((c) =>
        c.isolation_db === null ? null : (
          <circle key={c.freq_hz} cx={scale.x(c.freq_hz)} cy={scale.y(c.isolation_db)}
                  r={2.5} fill={c.floor_limited ? "none" : ISOLATION}
                  stroke={ISOLATION} strokeWidth={1} />
        ),
      )}
    </svg>
  );
}

interface SweepPanelProps {
  sensors: SensorStatus | null | undefined;
  onError: (message: string | null) => void;
}

/** Swept-frequency isolation — the curve, rather than one number at 40 Hz.
 *
 * Polls while a sweep runs because the measurement takes about a minute and the
 * person being measured is sitting still for all of it. Without progress on
 * screen there is no way to tell a working sweep from a hung one, and the
 * natural response to that doubt is to get up, which ruins the run.
 */
export function SweepPanel({ sensors, onError }: SweepPanelProps) {
  const [status, setStatus] = useState<SweepStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [showPoints, setShowPoints] = useState(false);
  const timer = useRef<number | null>(null);

  const poll = useCallback(async () => {
    try {
      setStatus(await readSweep());
    } catch (e) {
      onError(String(e));
    }
  }, [onError]);

  useEffect(() => {
    void poll();
  }, [poll]);

  useEffect(() => {
    if (!status?.running) {
      if (timer.current !== null) {
        window.clearInterval(timer.current);
        timer.current = null;
      }
      return;
    }
    if (timer.current === null) {
      timer.current = window.setInterval(() => void poll(), 1000);
    }
    return () => {
      if (timer.current !== null) {
        window.clearInterval(timer.current);
        timer.current = null;
      }
    };
  }, [status?.running, poll]);

  const run = useCallback(async () => {
    setBusy(true);
    onError(null);
    try {
      setStatus(await startSweep());
    } catch (e) {
      onError(String(e));
    } finally {
      setBusy(false);
    }
  }, [onError]);

  const present = sensors?.any_present ?? false;
  const result = status?.result ?? null;
  const summary = result?.summary;
  const curve = summary?.curve ?? [];
  const running = status?.running ?? false;
  const pct = running && status && status.total > 0
    ? Math.round((status.done / status.total) * 100)
    : 0;

  return (
    <div className="mb-4 rounded-lg border border-zinc-800 bg-zinc-900/50 p-4">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <span className="text-sm font-semibold uppercase tracking-wider text-zinc-200">
          Frequency sweep
        </span>
        <button
          type="button"
          onClick={() => void run()}
          disabled={busy || running || !present}
          className="rounded-md bg-zinc-800 px-3 py-1 text-xs font-semibold uppercase tracking-wide text-zinc-200 transition hover:bg-zinc-700 disabled:cursor-not-allowed disabled:opacity-40"
        >
          {running ? "Sweeping…" : "Run sweep"}
        </button>
      </div>

      {!present && (
        <p className="text-xs text-zinc-500">
          Both pods have to be detected. Nothing here works from one end of the rig.
        </p>
      )}

      {running && status && (
        <div className="mb-3">
          <div className="h-1.5 overflow-hidden rounded-full bg-zinc-800">
            <div className="h-full bg-emerald-500 transition-all"
                 style={{ width: `${pct}%` }} />
          </div>
          <p className="mt-2 text-xs text-zinc-400">
            Point {status.done} of {status.total}
            {status.remaining_s !== null && ` — about ${Math.ceil(status.remaining_s)}s left`}.
            {" "}Sit still and keep your feet off the pedals.
          </p>
        </div>
      )}

      {status?.error && (
        <p className="mb-2 font-mono text-xs text-rose-300">{status.error}</p>
      )}

      {result && !result.ok && (
        <p className="mb-2 text-xs text-amber-200">{result.reason}</p>
      )}

      {result && result.ok && summary && (
        <>
          <div className="mb-3 grid grid-cols-2 gap-x-4 gap-y-2 sm:grid-cols-4">
            <Stat label="Best" value={summary.best_hz !== undefined
              ? `${db(summary.best_isolation_db)} dB @ ${summary.best_hz} Hz` : "—"}
              hint="where the two channels stay furthest apart" />
            <Stat label="Worst" value={summary.worst_hz !== undefined
              ? `${db(summary.worst_isolation_db)} dB @ ${summary.worst_hz} Hz` : "—"}
              hint="a structural mode, not a property of the mounts" />
            <Stat label="Spread" value={summary.spread_db !== undefined
              ? `${summary.spread_db.toFixed(1)} dB` : "—"}
              hint="how much the band you choose is worth" />
            <Stat label="Usable" value={`${summary.usable_points} of ${summary.measured_points}`}
              hint="points that beat the noise floor at both ends" />
          </div>

          <Chart curve={curve} worstHz={summary.worst_hz} bestHz={summary.best_hz} />

          <div className="mt-2 flex flex-wrap gap-3 text-xs text-zinc-500">
            <Key color={ISOLATION} label="isolation (mean)" />
            <Key color={FRONT} label="front → rear" />
            <Key color={REAR} label="rear → front" />
            <span>hollow = at the noise floor, so the true figure is lower</span>
          </div>

          {result.warnings.map((w) => (
            <p key={w} className="mt-2 text-xs text-amber-200">{w}</p>
          ))}

          <p className="mt-3 text-xs leading-relaxed text-zinc-500">
            Lower is better: it is how much of one shaker arrives at the other end. Put the
            output bands where the line is lowest — on a rig whose isolation varies this much
            with frequency, that choice is worth more than most mechanical work.
          </p>

          <button
            type="button"
            onClick={() => setShowPoints((v) => !v)}
            className="mt-2 text-xs text-zinc-500 underline decoration-dotted"
          >
            {showPoints ? "hide" : "show"} every point
          </button>

          {showPoints && (
            <div className="mt-2 overflow-x-auto">
              <table className="w-full text-left font-mono text-[11px] text-zinc-400">
                <thead className="text-zinc-500">
                  <tr>
                    <th className="pr-3 font-normal">Hz</th>
                    <th className="pr-3 font-normal">drive</th>
                    <th className="pr-3 font-normal">near g</th>
                    <th className="pr-3 font-normal">far g</th>
                    <th className="pr-3 font-normal">floor g</th>
                    <th className="pr-3 font-normal">ratio</th>
                    <th className="font-normal">note</th>
                  </tr>
                </thead>
                <tbody>
                  {result.points.map((p) => (
                    <tr key={`${p.drive}-${p.freq_hz}`} className="border-t border-zinc-800/60">
                      <td className="pr-3">{p.freq_hz}</td>
                      <td className="pr-3">{p.drive}</td>
                      <td className="pr-3">{p.near_g.toFixed(4)}</td>
                      <td className="pr-3">{p.far_g.toFixed(4)}</td>
                      <td className="pr-3">{p.ambient_far_g.toFixed(4)}</td>
                      <td className="pr-3">{p.ratio_db === null ? "—" : db(p.ratio_db)}</td>
                      <td className="text-zinc-600">{p.note ?? ""}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}

      {!result && !running && present && (
        <p className="text-xs leading-relaxed text-zinc-500">
          Plays a tone at a time from 15 to 120 Hz on each channel and reads both pods
          narrowband, which rejects most of the room. About a minute, seated and still.
        </p>
      )}
    </div>
  );
}

function Stat({ label, value, hint }: { label: string; value: string; hint: string }) {
  return (
    <div>
      <div className="text-[10px] uppercase tracking-wider text-zinc-500">{label}</div>
      <div className="font-mono text-sm text-zinc-200">{value}</div>
      <div className="text-[10px] leading-tight text-zinc-600">{hint}</div>
    </div>
  );
}

function Key({ color, label }: { color: string; label: string }) {
  return (
    <span className="flex items-center gap-1">
      <span className="inline-block h-0.5 w-4" style={{ backgroundColor: color }} />
      {label}
    </span>
  );
}
