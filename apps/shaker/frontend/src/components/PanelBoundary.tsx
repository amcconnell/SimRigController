import { Component, type ErrorInfo, type ReactNode } from "react";

interface Props {
  label: string;
  children: ReactNode;
}

interface State {
  error: Error | null;
  componentStack: string | null;
}

/** Catches a render failure in one panel instead of losing the whole page.
 *
 * React unmounts the entire tree on an uncaught render error, so before this
 * existed a single bad field anywhere on the diagnostics screen produced a
 * blank page with no message, no clue which panel was at fault, and nothing to
 * report. That is the worst available failure: the screen whose job is to
 * explain what the rig is doing, explaining nothing.
 *
 * One boundary per panel rather than one around the screen, so a broken panel
 * costs you that panel and not its neighbours — the readings you were actually
 * looking at usually still work.
 *
 * Retry rather than reload, because status is polled twice a second: a value
 * that was briefly absent or malformed will have been replaced by the time you
 * press it, and a full reload would lose any measurement result on screen.
 */
export class PanelBoundary extends Component<Props, State> {
  state: State = { error: null, componentStack: null };

  static getDerivedStateFromError(error: Error): Partial<State> {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // Also to the console, so the stack survives a screenshot of the panel.
    console.error(`panel "${this.props.label}" failed to render:`, error, info.componentStack);
    this.setState({ componentStack: info.componentStack ?? null });
  }

  render(): ReactNode {
    const { error, componentStack } = this.state;
    if (!error) return this.props.children;

    return (
      <div className="mb-4 rounded-lg border border-rose-900/50 bg-rose-950/20 p-4">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <span className="text-sm font-semibold uppercase tracking-wider text-rose-200">
            {this.props.label} failed to render
          </span>
          <button
            type="button"
            onClick={() => this.setState({ error: null, componentStack: null })}
            className="rounded-md bg-zinc-800 px-3 py-1 text-xs font-semibold uppercase tracking-wide text-zinc-200 transition hover:bg-zinc-700"
          >
            Retry
          </button>
        </div>

        <p className="mt-2 font-mono text-xs leading-relaxed text-rose-200">
          {error.message || String(error)}
        </p>

        {componentStack && (
          <details className="mt-2">
            <summary className="cursor-pointer text-xs text-zinc-500">where</summary>
            <pre className="mt-1 overflow-x-auto whitespace-pre-wrap font-mono text-[11px] leading-relaxed text-zinc-500">
              {componentStack.trim()}
            </pre>
          </details>
        )}

        <p className="mt-2 text-xs leading-relaxed text-zinc-500">
          The rest of the page is unaffected, and the rig keeps running — this screen is
          read-only. Status refreshes twice a second, so Retry is usually enough.
        </p>
      </div>
    );
  }
}
