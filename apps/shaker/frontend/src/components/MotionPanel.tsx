import type { MotionStatus } from "../types/config";

function signed(n: number, digits = 2): string {
  return (n < 0 ? "-" : "+") + Math.abs(n).toFixed(digits);
}

function tone(v: number, deadband = 0.3): string {
  if (Math.abs(v) < deadband) return "text-zinc-400";
  return v > 0 ? "text-amber-300" : "text-sky-300";
}

interface MotionPanelProps {
  motion: MotionStatus | null | undefined;
}

/** Body-motion fields alongside independently derived references.
 *
 * All three are body-frame accelerations in m/s^2 with gravity excluded,
 * identified on a live console (see protocol.py). The references stay on
 * screen because they remain a cross-check rather than an experiment: surge
 * should track d(speed)/dt in a straight line and sway should mirror
 * v x yaw rate. If a GT7 update moved the offsets, that agreement breaks here
 * instead of silently corrupting whatever is built on top.
 */
export function MotionPanel({ motion }: MotionPanelProps) {
  const m = motion ?? null;

  if (m && !m.has_motion) {
    return (
      <div className="mb-4 rounded-lg border border-amber-900/50 bg-amber-950/20 p-4">
        <div className="text-sm font-semibold uppercase tracking-wider text-amber-200">
          Body motion unavailable
        </div>
        <p className="mt-2 text-xs text-zinc-400">
          This session is serving the short packet layout, which has no
          sway/heave/surge. GT7 locks the format to whichever heartbeat it sees
          first, so another tool on the LAN may have claimed it. Restart GT7
          with nothing else listening.
        </p>
      </div>
    );
  }

  return (
    <div className="mb-4 rounded-lg border border-zinc-800 bg-zinc-900/50 p-4">
      <div className="mb-3 flex items-center justify-between">
        <span className="text-sm font-semibold uppercase tracking-wider text-zinc-200">
          Body motion
        </span>
        <span className="text-xs text-zinc-500">body frame, m/s², gravity excluded</span>
      </div>

      <div className="grid gap-3 sm:grid-cols-3">
        <Pair label="surge" raw={m?.surge} ref={m?.long_accel} refLabel="d(speed)/dt" />
        <Pair label="sway" raw={m?.sway} ref={m?.lat_accel} refLabel="v x yaw rate" />
        <Pair label="heave" raw={m?.heave} ref={undefined} refLabel="no reference" />
      </div>

      <p className="mt-3 text-xs leading-relaxed text-zinc-500">
        <span className="text-zinc-300">surge</span> tracks{" "}
        <span className="text-zinc-300">d(speed)/dt</span> in a straight line and reads low in
        corners, where the reference is path-tangential and surge is body-longitudinal.{" "}
        <span className="text-zinc-300">sway</span> mirrors{" "}
        <span className="text-zinc-300">v x yaw rate</span> with the opposite sign — that is
        GT7's convention, not an error. Persistent disagreement would mean the packet offsets
        have moved.
      </p>
    </div>
  );
}

function Pair({
  label,
  raw,
  ref: reference,
  refLabel,
}: {
  label: string;
  raw: number | undefined;
  ref: number | undefined;
  refLabel: string;
}) {
  return (
    <div className="rounded border border-zinc-800/80 px-3 py-2">
      <div className="text-xs uppercase tracking-wider text-zinc-500">{label}</div>
      <div className={`font-mono text-2xl tabular-nums ${tone(raw ?? 0)}`}>
        {raw === undefined ? "—" : signed(raw)}
      </div>
      <div className="mt-2 border-t border-zinc-800/80 pt-1">
        <div className="text-[10px] uppercase tracking-wider text-zinc-600">{refLabel}</div>
        <div className="font-mono text-sm tabular-nums text-zinc-400">
          {reference === undefined ? "—" : `${signed(reference)} m/s²`}
        </div>
      </div>
    </div>
  );
}
