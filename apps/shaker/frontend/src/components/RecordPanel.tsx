import { useCallback, useEffect, useState } from "react";

import { listRecordings, startRecording, stopRecording } from "../api/client";
import type { RecordingStatus, SessionFile } from "../types/config";

function mb(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} kB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function clock(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

interface RecordPanelProps {
  recording: RecordingStatus | null | undefined;
  onError: (message: string) => void;
}

/** Capture a lap, so a tuning change can be judged against the same input twice.
 *
 * Every tuning decision here has been made against a memory of a previous
 * session — different track, different car, days old. A recording plus an
 * offline replay is what turns "feels more refined" into a comparison rather
 * than an impression, and it is the clock the accelerometers will be aligned
 * against when they arrive.
 *
 * Live status comes from the shared /api/status poll rather than a timer of its
 * own; the file list is fetched only when it can have changed.
 */
export function RecordPanel({ recording, onError }: RecordPanelProps) {
  const [sessions, setSessions] = useState<SessionFile[]>([]);
  const [busy, setBusy] = useState(false);
  const active = recording?.recording ?? false;

  const refresh = useCallback(() => {
    listRecordings()
      .then((r) => setSessions(r.sessions))
      .catch(() => {});
  }, []);

  // Refetch when recording stops (a new file exists) and once on mount.
  useEffect(() => {
    if (!active) refresh();
  }, [active, refresh]);

  const toggle = useCallback(async () => {
    setBusy(true);
    try {
      if (active) {
        await stopRecording();
      } else {
        await startRecording();
      }
    } catch (e) {
      onError(String(e));
    } finally {
      setBusy(false);
      refresh();
    }
  }, [active, onError, refresh]);

  return (
    <div className="mb-4 rounded-lg border border-zinc-800 bg-zinc-900/50 p-4">
      <div className="mb-3 flex items-center justify-between">
        <span className="text-sm font-semibold uppercase tracking-wider text-zinc-200">
          Session recording
        </span>
        <span className="text-xs text-zinc-500">every packet, full rate</span>
      </div>

      {recording === null ? (
        <p className="text-xs text-zinc-500">
          No recorder attached to this process.
        </p>
      ) : (
        <>
          <div className="flex flex-wrap items-center gap-4">
            <button
              type="button"
              onClick={toggle}
              disabled={busy}
              className={`rounded-md px-4 py-2 text-xs font-semibold uppercase tracking-wide transition disabled:opacity-50 ${
                active
                  ? "bg-rose-900/70 text-rose-100 hover:bg-rose-900"
                  : "bg-zinc-800 text-zinc-200 hover:bg-zinc-700"
              }`}
            >
              {active ? "Stop" : "Record"}
            </button>

            {active && (
              <span className="flex items-center gap-2 text-xs text-zinc-400">
                <span className="h-2 w-2 animate-pulse rounded-full bg-rose-400" />
                <span className="font-mono tabular-nums">{clock(recording?.seconds ?? 0)}</span>
                <span className="text-zinc-600">·</span>
                <span className="font-mono tabular-nums">
                  {(recording?.packets ?? 0).toLocaleString()} packets
                </span>
                <span className="text-zinc-600">·</span>
                <span className="font-mono tabular-nums">{mb(recording?.bytes ?? 0)}</span>
              </span>
            )}

            {!active && recording?.name && (
              <span className="font-mono text-xs text-zinc-500">
                last: {recording.name} ({(recording.packets ?? 0).toLocaleString()} packets)
              </span>
            )}
          </div>

          {recording?.error && (
            <p className="mt-3 text-xs text-amber-300">
              Recording stopped: {recording.error}
            </p>
          )}

          {active && (recording?.packets ?? 0) === 0 && (
            <p className="mt-3 text-xs text-zinc-500">
              Armed, but no packets yet — nothing is arriving from the PS5.
            </p>
          )}

          <p className="mt-3 text-xs leading-relaxed text-zinc-500">
            Captures whole packets at the rate they arrive, including the menu and paused frames
            the app itself discards, so a replay can exercise the same gates the live path does.
            Stops itself at 256 MB — roughly three hours, and the card the Pi boots from is the
            one being written to.
          </p>

          {sessions.length > 0 && (
            <div className="mt-3 border-t border-zinc-800/80 pt-3">
              <div className="mb-1 text-[10px] uppercase tracking-wider text-zinc-600">
                On disk
              </div>
              <ul className="space-y-0.5">
                {sessions.slice(0, 6).map((s) => (
                  <li key={s.name} className="flex justify-between font-mono text-xs text-zinc-500">
                    <span className="truncate">{s.name}</span>
                    <span className="ml-3 shrink-0 tabular-nums">{mb(s.bytes)}</span>
                  </li>
                ))}
              </ul>
              {sessions.length > 6 && (
                <p className="mt-1 text-xs text-zinc-600">
                  and {sessions.length - 6} more
                </p>
              )}
            </div>
          )}
        </>
      )}
    </div>
  );
}
