import type { LimiterStatus } from "../types/config";

// Full scale for the reduction bars. Past this the limiter is not trimming
// peaks any more, it is setting the level, and the exact depth stops being the
// interesting part of the answer.
const FULL_SCALE_DB = 12;

/** Duty-cycle bands, in the order they are tested.
 *
 * The boundaries are judgement rather than measurement, and are deliberately
 * conservative: this readout exists to catch over-driving, so it says something
 * before the compression is severe enough to notice by feel. On this rig a lap
 * at master_gain 0.5 sits in the first band, with brief excursions into the
 * second on kerb strikes.
 */
const BANDS: { limit: number; label: string; tone: string; note: string }[] = [
  {
    limit: 2,
    label: "Clear",
    tone: "text-emerald-300",
    note: "The limiter is out of the way. Transients are reaching the shakers at full size.",
  },
  {
    limit: 15,
    label: "Catching peaks",
    tone: "text-emerald-300",
    note: "Occasional reduction on the biggest hits, which is the job. Nothing to change.",
  },
  {
    limit: 40,
    label: "Compressing",
    tone: "text-amber-300",
    note:
      "Reducing through most corners. Contrast between road texture and impacts is starting " +
      "to flatten — try lowering master gain a notch and raising the amplifier instead.",
  },
  {
    limit: Infinity,
    label: "Squashed",
    tone: "text-rose-300",
    note:
      "Reducing almost continuously. Everything is arriving at one level, which feels dull " +
      "rather than loud and invites turning it up again. Lower master gain.",
  },
];

function band(dutyPct: number) {
  return BANDS.find((b) => dutyPct < b.limit) ?? BANDS[BANDS.length - 1];
}

interface LimiterPanelProps {
  limiter: LimiterStatus | null | undefined;
}

/** Output limiter activity.
 *
 * The limiter's failure mode is silent, which is the whole reason this panel
 * exists. Driven hard enough it stops being a safety net and becomes a
 * compressor, and the symptom is not distortion but sameness — kerbs, shifts
 * and road texture all arriving at the same level. That reads as "the rig
 * feels flat", a complaint whose obvious remedy is more gain, which makes it
 * worse. A number breaks the loop.
 */
export function LimiterPanel({ limiter }: LimiterPanelProps) {
  const l = limiter ?? null;
  const duty = l?.duty_pct ?? 0;
  const b = band(duty);

  return (
    <div className="mb-4 rounded-lg border border-zinc-800 bg-zinc-900/50 p-4">
      <div className="mb-3 flex items-center justify-between">
        <span className="text-sm font-semibold uppercase tracking-wider text-zinc-200">
          Output limiter
        </span>
        <span className="text-xs text-zinc-500">gain reduction, dB</span>
      </div>

      <div className="grid gap-3 sm:grid-cols-3">
        <Reading
          label="now"
          value={l ? `${l.reduction_db.toFixed(1)}` : "—"}
          unit="dB"
          fill={(l?.reduction_db ?? 0) / FULL_SCALE_DB}
        />
        <Reading
          label="peak (recent)"
          value={l ? `${l.peak_reduction_db.toFixed(1)}` : "—"}
          unit="dB"
          fill={(l?.peak_reduction_db ?? 0) / FULL_SCALE_DB}
        />
        <Reading
          label="duty"
          value={l ? `${duty.toFixed(0)}` : "—"}
          unit="%"
          fill={duty / 100}
        />
      </div>

      <div className="mt-3 border-t border-zinc-800/80 pt-3">
        <div className={`text-sm font-semibold ${l ? b.tone : "text-zinc-500"}`}>
          {l ? b.label : "No data"}
        </div>
        <p className="mt-1 text-xs leading-relaxed text-zinc-500">
          {l
            ? b.note
            : "Waiting for the audio thread. If this stays empty the output stream failed to open."}
        </p>
      </div>

      <p className="mt-3 text-xs leading-relaxed text-zinc-500">
        <span className="text-zinc-300">Duty</span> is the share of the last few seconds spent
        reducing gain at all, so it keeps counting through the limiter's release tail — brief
        readings well above zero after a single hard hit are normal.{" "}
        <span className="text-zinc-300">Peak</span> is latched and decays slowly, so a reduction
        shorter than the half-second refresh is still visible.
      </p>
    </div>
  );
}

function Reading({
  label,
  value,
  unit,
  fill,
}: {
  label: string;
  value: string;
  unit: string;
  fill: number;
}) {
  const pct = Math.max(0, Math.min(1, fill)) * 100;
  return (
    <div className="rounded border border-zinc-800/80 px-3 py-2">
      <div className="text-xs uppercase tracking-wider text-zinc-500">{label}</div>
      <div className="font-mono text-2xl tabular-nums text-zinc-200">
        {value}
        <span className="ml-1 text-sm text-zinc-500">{unit}</span>
      </div>
      <div className="mt-2 h-1 overflow-hidden rounded-full bg-zinc-800">
        <div
          className="h-full rounded-full bg-zinc-400 transition-[width] duration-200"
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  );
}
