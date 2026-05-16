import { useId } from "react";
import { Hint } from "./Hint";

interface NumberFieldProps {
  label: string;
  value: number;
  step?: number;
  min?: number;
  max?: number;
  unit?: string;
  hint?: string;
  onChange: (value: number) => void;
}

export function NumberField({
  label,
  value,
  step = 0.05,
  min,
  max,
  unit,
  hint,
  onChange,
}: NumberFieldProps) {
  const id = useId();
  // Slider is shown only when both endpoints are defined — open-ended ranges
  // (e.g., sample rate) fall back to the number input alone.
  const hasSlider = typeof min === "number" && typeof max === "number";

  const commit = (raw: string) => {
    if (raw === "") return;
    const v = Number(raw);
    if (!Number.isNaN(v)) onChange(v);
  };

  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1 py-1 text-sm">
      <label
        htmlFor={id}
        className="flex w-36 shrink-0 items-center gap-1.5 text-zinc-300"
      >
        <span className="truncate">{label}</span>
        {unit && <span className="shrink-0 text-zinc-500">({unit})</span>}
        {hint && <Hint text={hint} />}
      </label>
      {hasSlider && (
        <input
          type="range"
          value={value}
          step={step}
          min={min}
          max={max}
          onChange={(e) => commit(e.target.value)}
          className="min-w-0 flex-1 accent-emerald-500"
        />
      )}
      <input
        id={id}
        type="number"
        value={value}
        step={step}
        min={min}
        max={max}
        onChange={(e) => commit(e.target.value)}
        className="w-20 rounded border border-zinc-700 bg-zinc-900 px-2 py-1 text-right font-mono tabular-nums text-zinc-100 focus:border-zinc-500 focus:outline-none"
      />
    </div>
  );
}

interface BoolFieldProps {
  label: string;
  value: boolean;
  hint?: string;
  onChange: (value: boolean) => void;
}

export function BoolField({ label, value, hint, onChange }: BoolFieldProps) {
  return (
    <div className="flex items-center justify-between gap-3 py-1 text-sm">
      <span className="flex items-center gap-1.5 text-zinc-300">
        {label}
        {hint && <Hint text={hint} />}
      </span>
      <button
        type="button"
        role="switch"
        aria-checked={value}
        aria-label={label}
        onClick={() => onChange(!value)}
        className={`relative inline-flex h-6 w-11 shrink-0 cursor-pointer items-center rounded-full px-0.5 transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500 focus-visible:ring-offset-2 focus-visible:ring-offset-zinc-950 ${
          value ? "bg-emerald-500" : "bg-zinc-700"
        }`}
      >
        <span
          aria-hidden
          className={`block h-5 w-5 rounded-full bg-white shadow transition-transform ${
            value ? "translate-x-5" : "translate-x-0"
          }`}
        />
      </button>
    </div>
  );
}

interface TextFieldProps {
  label: string;
  value: string;
  placeholder?: string;
  hint?: string;
  onChange: (value: string) => void;
}

export function TextField({
  label,
  value,
  placeholder,
  hint,
  onChange,
}: TextFieldProps) {
  const id = useId();
  return (
    <div className="flex items-center gap-3 py-1 text-sm">
      <label htmlFor={id} className="flex w-36 shrink-0 items-center gap-1.5 text-zinc-300">
        <span>{label}</span>
        {hint && <Hint text={hint} />}
      </label>
      <input
        id={id}
        type="text"
        value={value}
        placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)}
        className="flex-1 rounded border border-zinc-700 bg-zinc-900 px-2 py-1 font-mono text-zinc-100 focus:border-zinc-500 focus:outline-none"
      />
    </div>
  );
}
