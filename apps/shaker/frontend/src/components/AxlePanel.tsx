import type { AxleStatus } from "../types/config";

// Below this the wheel is tracking the car closely enough that the sign is
// just noise, so it reads as neutral rather than as spin or lockup.
const SLIP_DEADBAND_MPS = 0.5;

function signed(n: number, digits = 2): string {
  return (n < 0 ? "-" : "+") + Math.abs(n).toFixed(digits);
}

function slipTone(v: number): string {
  if (Math.abs(v) < SLIP_DEADBAND_MPS) return "text-zinc-400";
  return v > 0 ? "text-amber-300" : "text-sky-300";
}

function slipWord(v: number): string {
  if (Math.abs(v) < SLIP_DEADBAND_MPS) return "tracking";
  return v > 0 ? "spin" : "lock";
}

interface AxlePanelProps {
  axle: AxleStatus | null | undefined;
}

/** Read-only front/rear diagnostics, collapsed by default.
 *
 * Exists to settle three unverified protocol assumptions from the driver's
 * seat: wheel_rps units/sign, the FL/FR/RL/RR corner order, and whether
 * speed_mps is signed. That is why the raw per-corner numbers are here and not
 * just the derived pair — every reduction in the audio path is an
 * order-invariant max(), so a permuted mapping is invisible in the derived
 * values alone.
 */
export function AxlePanel({ axle }: AxlePanelProps) {
  const raw = axle?.raw ?? null;

  return (
    <details className="mb-4 rounded-lg border border-zinc-800 bg-zinc-900/50 p-4 open:pb-3">
      <summary className="cursor-pointer text-sm font-semibold uppercase tracking-wider text-zinc-200">
        Axle diagnostics (front / rear)
      </summary>

      {!axle ? (
        <p className="mt-3 text-xs text-zinc-500">Waiting for status…</p>
      ) : (
        <div className="mt-3 space-y-3 font-mono">
          <div className="grid grid-cols-2 gap-3">
            <AxleCard
              label="Front (pedal deck)"
              slip={axle.slip_front}
              activity={axle.suspension_activity_front}
            />
            <AxleCard
              label="Rear (seat)"
              slip={axle.slip_rear}
              activity={axle.suspension_activity_rear}
            />
          </div>

          {/* The values the audio path actually reads today, so any drift
              between them and the per-axle split is visible side by side. */}
          <div className="flex flex-wrap items-baseline gap-x-6 gap-y-1 rounded border border-zinc-800/80 px-3 py-2 text-xs">
            <span className="uppercase tracking-wider text-zinc-500">Legacy (drives audio)</span>
            <Pair label="slip mag" value={`${axle.slip_magnitude.toFixed(2)} m/s`} />
            <Pair label="susp" value={axle.suspension_activity.toFixed(4)} />
          </div>

          {raw === null ? (
            <p className="text-xs text-zinc-500">No packet yet — raw corner values unavailable.</p>
          ) : (
            <div className="rounded border border-zinc-800/80 px-3 py-2">
              <div className="flex flex-wrap items-baseline gap-x-6 gap-y-1 pb-2 text-xs">
                <Pair label="speed" value={`${signed(raw.speed_mps)} m/s`} />
                <Pair label="gear" value={String(raw.current_gear)} />
                <span className="text-[10px] leading-snug text-zinc-500">
                  surface speed should equal speed at a steady cruise
                </span>
              </div>
              {/* Sized to fit a 375 px phone without sideways scrolling — the
                  point of this table is being readable at a glance mid-lap.
                  overflow-x-auto is the fallback for narrower screens. */}
              <div className="overflow-x-auto">
                <div className="grid grid-cols-[auto_repeat(4,minmax(0,1fr))] gap-x-1.5 text-[11px] sm:gap-x-2 sm:text-xs">
                  <CornerHeader />
                  <CornerRow
                    label="rps"
                    values={[raw.wheel_rps_FL, raw.wheel_rps_FR, raw.wheel_rps_RL, raw.wheel_rps_RR]}
                    digits={2}
                    showSign
                  />
                  <CornerRow
                    label="radius m"
                    values={[
                      raw.tire_radius_FL,
                      raw.tire_radius_FR,
                      raw.tire_radius_RL,
                      raw.tire_radius_RR,
                    ]}
                    digits={3}
                  />
                  <CornerRow
                    label="surf m/s"
                    values={[
                      raw.wheel_surface_speed_FL,
                      raw.wheel_surface_speed_FR,
                      raw.wheel_surface_speed_RL,
                      raw.wheel_surface_speed_RR,
                    ]}
                    digits={2}
                    showSign
                  />
                  <CornerRow
                    label="susp m"
                    values={[
                      raw.suspension_FL,
                      raw.suspension_FR,
                      raw.suspension_RL,
                      raw.suspension_RR,
                    ]}
                    digits={4}
                  />
                </div>
              </div>
            </div>
          )}
        </div>
      )}
    </details>
  );
}

function AxleCard({ label, slip, activity }: { label: string; slip: number; activity: number }) {
  return (
    <div className="rounded border border-zinc-800/80 px-3 py-2">
      <div className="text-[10px] uppercase tracking-wider text-zinc-500">{label}</div>
      <div className={`mt-1 text-2xl tabular-nums ${slipTone(slip)}`}>{signed(slip)}</div>
      <div className="text-[10px] uppercase tracking-wider text-zinc-500">
        m/s slip · {slipWord(slip)}
      </div>
      <div className="mt-2 flex items-baseline gap-2 text-xs">
        <span className="text-[10px] uppercase tracking-wider text-zinc-500">susp</span>
        <span className="tabular-nums text-zinc-100">{activity.toFixed(4)}</span>
      </div>
    </div>
  );
}

function Pair({ label, value }: { label: string; value: string }) {
  return (
    <span className="flex items-baseline gap-2">
      <span className="text-[10px] uppercase tracking-wider text-zinc-500">{label}</span>
      <span className="tabular-nums text-zinc-100">{value}</span>
    </span>
  );
}

function CornerHeader() {
  return (
    <>
      <span />
      {["FL", "FR", "RL", "RR"].map((c) => (
        <span key={c} className="text-right uppercase tracking-wider text-zinc-500">
          {c}
        </span>
      ))}
    </>
  );
}

function CornerRow({
  label,
  values,
  digits,
  showSign = false,
}: {
  label: string;
  values: number[];
  digits: number;
  showSign?: boolean;
}) {
  return (
    <>
      <span className="whitespace-nowrap uppercase tracking-wider text-zinc-500">{label}</span>
      {values.map((v, i) => (
        <span key={i} className="text-right tabular-nums text-zinc-100">
          {showSign ? signed(v, digits) : v.toFixed(digits)}
        </span>
      ))}
    </>
  );
}
