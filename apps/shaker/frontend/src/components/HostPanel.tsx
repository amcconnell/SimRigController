import type { SystemStatus } from "../types/config";

function bar(pct: number | null): number {
  return Math.max(0, Math.min(100, pct ?? 0));
}

interface HostPanelProps {
  system: SystemStatus | null | undefined;
}

/** Temperature, fan, CPU and memory for the Pi itself.
 *
 * Here because the failure it warns about is not a crash. A hot Pi reduces its
 * clock, and a throttled Pi misses audio callbacks — which arrives in the seat
 * as intermittent glitching, indistinguishable from a DSP fault and far harder
 * to find. A rising temperature turns that from a mystery into a reading.
 */
export function HostPanel({ system }: HostPanelProps) {
  const s = system ?? null;
  const temp = s?.cpu_temp_c ?? null;
  const warn = s?.warn_temp_c ?? 60;
  const hot = s?.hot_temp_c ?? 75;

  const tempTone =
    temp === null ? "text-zinc-500" : temp >= hot ? "text-rose-300"
      : temp >= warn ? "text-amber-300" : "text-zinc-200";

  return (
    <div className="mb-4 rounded-lg border border-zinc-800 bg-zinc-900/50 p-4">
      <div className="mb-3 flex items-center justify-between">
        <span className="text-sm font-semibold uppercase tracking-wider text-zinc-200">
          Pi health
        </span>
        <span className="text-xs text-zinc-500">throttling costs audio callbacks</span>
      </div>

      <div className="grid gap-3 sm:grid-cols-4">
        <Reading
          label="temperature"
          value={temp === null ? "—" : temp.toFixed(1)}
          unit="°C"
          fill={temp === null ? 0 : (temp / 90) * 100}
          tone={tempTone}
        />
        <Reading
          label="fan"
          value={s?.fan_duty_pct === null || s?.fan_duty_pct === undefined
            ? "—" : String(s.fan_duty_pct)}
          unit="%"
          fill={bar(s?.fan_duty_pct ?? null)}
          tone="text-zinc-200"
        />
        <Reading
          label="cpu"
          value={s?.cpu_pct === null || s?.cpu_pct === undefined ? "—" : s.cpu_pct.toFixed(0)}
          unit="%"
          fill={bar(s?.cpu_pct ?? null)}
          tone="text-zinc-200"
        />
        <Reading
          label="memory"
          value={s?.mem_pct === null || s?.mem_pct === undefined ? "—" : s.mem_pct.toFixed(0)}
          unit="%"
          fill={bar(s?.mem_pct ?? null)}
          tone="text-zinc-200"
        />
      </div>

      <div className="mt-3 flex flex-wrap gap-x-4 gap-y-1 text-xs text-zinc-500">
        {s?.load_1m !== null && s?.load_1m !== undefined && (
          <span>
            load <span className="font-mono tabular-nums text-zinc-400">{s.load_1m.toFixed(2)}</span>
          </span>
        )}
        {s?.mem_used_mb !== null && s?.mem_used_mb !== undefined && (
          <span>
            <span className="font-mono tabular-nums text-zinc-400">
              {s.mem_used_mb} / {s.mem_total_mb} MB
            </span>
          </span>
        )}
        {s?.fan_duty_pct === null && (
          <span className="text-amber-300">
            fan unmanaged — on this case that means running flat out
          </span>
        )}
      </div>

      <p className="mt-3 text-xs leading-relaxed text-zinc-500">
        A Pi 4 starts reducing its clock around {warn} °C and throttles hard near 80. Audio is the
        first thing to suffer, so temperature is worth a glance when the rig starts glitching in a
        way the settings do not explain. Memory is measured against available rather than free —
        page cache is not pressure.
      </p>
    </div>
  );
}

function Reading({
  label,
  value,
  unit,
  fill,
  tone,
}: {
  label: string;
  value: string;
  unit: string;
  fill: number;
  tone: string;
}) {
  return (
    <div className="rounded border border-zinc-800/80 px-3 py-2">
      <div className="text-xs uppercase tracking-wider text-zinc-500">{label}</div>
      <div className={`font-mono text-2xl tabular-nums ${tone}`}>
        {value}
        <span className="ml-1 text-sm text-zinc-500">{unit}</span>
      </div>
      <div className="mt-2 h-1 overflow-hidden rounded-full bg-zinc-800">
        <div
          className="h-full rounded-full bg-zinc-400 transition-[width] duration-200"
          style={{ width: `${Math.max(0, Math.min(100, fill))}%` }}
        />
      </div>
    </div>
  );
}
