import { useState } from "react";
import {
  activateProfile,
  createProfile,
  deleteProfile,
  renameProfile,
} from "../api/client";
import { DEFAULT_PROFILE_NAME, type ProfilesState } from "../types/config";

interface ProfileSelectorProps {
  state: ProfilesState;
  onChange: (state: ProfilesState) => void;
  onError: (message: string) => void;
}

export function ProfileSelector({ state, onChange, onError }: ProfileSelectorProps) {
  const [busy, setBusy] = useState(false);
  const isDefault = state.active === DEFAULT_PROFILE_NAME;

  const wrap = async <T,>(fn: () => Promise<T>): Promise<T | null> => {
    setBusy(true);
    try {
      return await fn();
    } catch (e) {
      onError(String(e));
      return null;
    } finally {
      setBusy(false);
    }
  };

  const handleActivate = async (name: string) => {
    const next = await wrap(() => activateProfile(name));
    if (next) onChange(next);
  };

  const handleCreate = async () => {
    const name = window.prompt("New profile name (cloned from current active):");
    if (!name) return;
    const next = await wrap(() => createProfile(name, state.active));
    if (next) {
      onChange(next);
      // Activate the new profile so the user can immediately tune it.
      const activated = await wrap(() => activateProfile(name));
      if (activated) onChange(activated);
    }
  };

  const handleRename = async () => {
    if (isDefault) return;
    const newName = window.prompt(`Rename "${state.active}" to:`, state.active);
    if (!newName || newName === state.active) return;
    const next = await wrap(() => renameProfile(state.active, newName));
    if (next) onChange(next);
  };

  const handleDelete = async () => {
    if (isDefault) return;
    if (!window.confirm(`Delete profile "${state.active}"?`)) return;
    const next = await wrap(() => deleteProfile(state.active));
    if (next) onChange(next);
  };

  return (
    <div className="flex flex-wrap items-center gap-2 text-sm">
      <span className="text-xs uppercase tracking-wide text-zinc-500">Profile</span>
      <select
        value={state.active}
        disabled={busy}
        onChange={(e) => handleActivate(e.target.value)}
        className="rounded border border-zinc-700 bg-zinc-900 px-2 py-1 font-mono text-zinc-100 focus:border-zinc-500 focus:outline-none disabled:opacity-50"
      >
        {state.names.map((n) => (
          <option key={n} value={n}>
            {n}
            {n === DEFAULT_PROFILE_NAME ? " (built-in)" : ""}
          </option>
        ))}
      </select>
      <button
        type="button"
        disabled={busy}
        onClick={handleCreate}
        title="Create a new profile cloned from the current one"
        className="rounded border border-zinc-700 bg-zinc-800 px-2 py-1 text-xs text-zinc-200 hover:border-zinc-500 hover:bg-zinc-700 disabled:opacity-50"
      >
        + New
      </button>
      <button
        type="button"
        disabled={busy || isDefault}
        onClick={handleRename}
        title={isDefault ? "Can't rename the built-in default" : "Rename this profile"}
        className="rounded border border-zinc-700 bg-zinc-800 px-2 py-1 text-xs text-zinc-200 hover:border-zinc-500 hover:bg-zinc-700 disabled:opacity-40"
      >
        Rename
      </button>
      <button
        type="button"
        disabled={busy || isDefault}
        onClick={handleDelete}
        title={isDefault ? "Can't delete the built-in default" : "Delete this profile"}
        className="rounded border border-rose-700/50 bg-rose-700/10 px-2 py-1 text-xs text-rose-200 hover:border-rose-500 hover:bg-rose-700/30 disabled:opacity-40"
      >
        Delete
      </button>
    </div>
  );
}
