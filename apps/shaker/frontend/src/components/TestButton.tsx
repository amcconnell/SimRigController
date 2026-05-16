import { useState } from "react";
import { runTest, type TestName } from "../api/client";

interface TestButtonProps {
  label: string;
  test: TestName;
  variant?: "default" | "header";
}

export function TestButton({ label, test, variant = "default" }: TestButtonProps) {
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handle = async () => {
    setError(null);
    setPending(true);
    try {
      await runTest(test);
    } catch (e) {
      setError(String(e));
    } finally {
      setPending(false);
    }
  };

  const baseClass =
    variant === "header"
      ? "rounded border border-zinc-700 bg-zinc-800 px-2 py-1 text-xs font-medium uppercase tracking-wide text-zinc-200 hover:border-zinc-500 hover:bg-zinc-700 disabled:opacity-50"
      : "rounded border border-emerald-700/50 bg-emerald-700/20 px-3 py-1.5 text-sm font-medium text-emerald-200 hover:border-emerald-500 hover:bg-emerald-700/30 disabled:opacity-50";

  return (
    <button
      type="button"
      onClick={handle}
      disabled={pending}
      title={error ?? undefined}
      className={baseClass}
    >
      {pending ? "…" : `Test ${label}`}
    </button>
  );
}
