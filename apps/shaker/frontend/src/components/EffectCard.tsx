import { useState, type ReactNode } from "react";
import { BoolField, NumberField } from "./ConfigField";
import { Hint } from "./Hint";

interface EffectCardProps {
  title: string;
  hint?: string;
  enabled: boolean;
  gain: number;
  onEnabledChange: (value: boolean) => void;
  onGainChange: (value: number) => void;
  testButton?: ReactNode;
  /** Detailed knobs shown when expanded. */
  children: ReactNode;
}

export function EffectCard({
  title,
  hint,
  enabled,
  gain,
  onEnabledChange,
  onGainChange,
  testButton,
  children,
}: EffectCardProps) {
  const [expanded, setExpanded] = useState(false);

  return (
    <section className={`rounded-lg border ${enabled ? "border-zinc-700" : "border-zinc-800 opacity-70"} bg-zinc-900/50`}>
      <header className="flex items-center gap-3 px-4 py-3">
        <button
          type="button"
          onClick={() => setExpanded((v) => !v)}
          className="flex w-6 items-center justify-center text-zinc-400 hover:text-zinc-100"
          aria-label={expanded ? "Collapse" : "Expand"}
        >
          <span className="font-mono text-xs">{expanded ? "▾" : "▸"}</span>
        </button>
        <h3 className="flex flex-1 items-center gap-1.5 text-sm font-semibold uppercase tracking-wider text-zinc-200">
          {title}
          {hint && <Hint text={hint} />}
        </h3>
        {testButton}
      </header>

      <div className="grid gap-x-8 gap-y-1 px-4 pb-3 sm:grid-cols-2">
        <BoolField label="Enabled" value={enabled} onChange={onEnabledChange} />
        <NumberField label="Gain" value={gain} step={0.05} min={0} max={4} onChange={onGainChange}
          hint="Overall output level for this effect, multiplied by the master gain at the end of the mix." />
      </div>

      {expanded && (
        <div className="border-t border-zinc-800 px-4 py-3">
          <div className="grid gap-x-8 gap-y-1 sm:grid-cols-2">{children}</div>
        </div>
      )}
    </section>
  );
}

interface SubFieldsetProps {
  legend: string;
  children: ReactNode;
}

export function SubFieldset({ legend, children }: SubFieldsetProps) {
  return (
    <div className="col-span-full mt-3 rounded border border-zinc-800/80 px-3 pb-2 pt-1">
      <div className="text-xs uppercase tracking-wider text-zinc-500">{legend}</div>
      <div className="mt-1 grid gap-x-8 gap-y-1 sm:grid-cols-2">{children}</div>
    </div>
  );
}
