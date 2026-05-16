import type { Config, ConfigUpdates, ProfilesState, Status } from "../types/config";

async function jsonFetch<T>(
  url: string,
  init?: RequestInit,
): Promise<T> {
  const r = await fetch(url, init);
  if (!r.ok) {
    const text = await r.text().catch(() => r.statusText);
    throw new Error(`${r.status} ${text}`);
  }
  return (await r.json()) as T;
}

export async function getConfig(): Promise<Config> {
  return jsonFetch<Config>("/api/config");
}

export async function putConfig(updates: ConfigUpdates): Promise<Config> {
  return jsonFetch<Config>("/api/config", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(updates),
  });
}

export async function getStatus(): Promise<Status> {
  return jsonFetch<Status>("/api/status");
}

export type TestName =
  | "vibration"
  | "gear_shift"
  | "engine_sweep"
  | "brake_rumble"
  | "rev_limiter"
  | "wheel_slip";

export async function runTest(name: TestName): Promise<void> {
  await jsonFetch<unknown>(`/api/test/${name}`, { method: "POST" });
}

export async function listProfiles(): Promise<ProfilesState> {
  return jsonFetch<ProfilesState>("/api/profiles");
}

export async function createProfile(name: string, source?: string): Promise<ProfilesState> {
  return jsonFetch<ProfilesState>("/api/profiles", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, source }),
  });
}

export async function deleteProfile(name: string): Promise<ProfilesState> {
  return jsonFetch<ProfilesState>(`/api/profiles/${encodeURIComponent(name)}`, {
    method: "DELETE",
  });
}

export async function renameProfile(name: string, newName: string): Promise<ProfilesState> {
  return jsonFetch<ProfilesState>(`/api/profiles/${encodeURIComponent(name)}/rename`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ new_name: newName }),
  });
}

export async function activateProfile(name: string): Promise<ProfilesState> {
  return jsonFetch<ProfilesState>(`/api/profiles/${encodeURIComponent(name)}/activate`, {
    method: "POST",
  });
}

export async function setMute(muted: boolean): Promise<{ muted: boolean }> {
  return jsonFetch<{ muted: boolean }>("/api/mute", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ muted }),
  });
}

