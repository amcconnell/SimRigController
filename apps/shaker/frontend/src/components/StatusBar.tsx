import type { ReactNode } from "react";
import type { Status } from "../types/config";

const STATE_COLORS: Record<string, string> = {
  connected: "bg-emerald-500/20 text-emerald-300 border-emerald-500/40",
  discovering: "bg-amber-500/20 text-amber-300 border-amber-500/40",
  starting: "bg-amber-500/20 text-amber-300 border-amber-500/40",
  stale: "bg-rose-500/20 text-rose-300 border-rose-500/40",
};

function fmt(n: number | null | undefined, digits = 0, unit = ""): string {
  if (n === null || n === undefined) return "—";
  return n.toFixed(digits) + unit;
}

interface StatusBarProps {
  status: Status | null;
  trailing?: ReactNode;
}

export function StatusBar({ status, trailing }: StatusBarProps) {
  const g = status?.gt7;
  const t = status?.telemetry;
  const stateClass = g
    ? (STATE_COLORS[g.state] ?? "bg-zinc-700 text-zinc-300 border-zinc-600")
    : "bg-zinc-800 text-zinc-500 border-zinc-700";

  return (
    <header className="sticky top-0 z-20 border-b border-zinc-800 bg-zinc-950/85 backdrop-blur">
      <div className="mx-auto flex max-w-5xl flex-wrap items-center gap-x-6 gap-y-2 px-4 py-3 text-sm font-mono">
        <div className="flex items-center gap-2">
          <span className={`rounded-full border px-2 py-0.5 text-xs uppercase tracking-wide ${stateClass}`}>
            {g?.state ?? "—"}
          </span>
          <span className="text-zinc-400">{g?.ps5_ip ?? "no PS5"}</span>
        </div>

        <Metric label="Rate" value={`${fmt(g?.packets_per_sec, 0)} /s`} />
        <Metric label="Speed" value={fmt(t?.speed_kph, 0, " kph")} />
        <Metric label="RPM" value={fmt(t?.engine_rpm, 0)} />
        <Metric label="Gear" value={t?.current_gear?.toString() ?? "—"} />
        <Metric
          label="T/B"
          value={t ? `${t.throttle.toString().padStart(3, " ")} / ${t.brake.toString().padStart(3, " ")}` : "—"}
        />
        <Metric label="Lap" value={t?.lap_count?.toString() ?? "—"} />

        {trailing && <div className="ml-auto flex items-center gap-2">{trailing}</div>}
      </div>
    </header>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-baseline gap-2">
      <span className="text-xs uppercase tracking-wide text-zinc-500">{label}</span>
      <span className="tabular-nums text-zinc-100">{value}</span>
    </div>
  );
}
