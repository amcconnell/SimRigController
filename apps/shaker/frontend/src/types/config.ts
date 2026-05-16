// Mirrors AudioConfig / GT7Config / WebConfig in apps/shaker/src/shaker/config.py.
// Keep field names identical to the server; the form auto-marshals via these keys.

export interface GT7Config {
  ps5_ip: string | null;
  heartbeat_interval_s: number;
  discovery_timeout_s: number;
}

export interface WebConfig {
  host: string;
  port: number;
}

export interface AudioConfig {
  device: string;
  sample_rate: number;
  buffer_ms: number;
  master_gain: number;

  vibration_enabled: boolean;
  vibration_gain: number;
  vibration_input_gain_pct: number;
  vibration_threshold_pct: number;
  vibration_min_force_pct: number;
  vibration_gamma: number;
  vibration_speed_blend_low_mps: number;
  vibration_speed_blend_high_mps: number;

  engine_rumble_enabled: boolean;
  engine_rumble_gain: number;
  engine_rumble_rpm_divisor: number;

  brake_rumble_enabled: boolean;
  brake_rumble_gain: number;
  brake_rumble_freq_hz: number;
  brake_rumble_threshold_pct: number;

  rev_limiter_enabled: boolean;
  rev_limiter_gain: number;
  rev_limiter_freq_hz: number;
  rev_limiter_trigger_pct: number;

  wheel_slip_enabled: boolean;
  wheel_slip_gain: number;
  wheel_slip_freq_hz: number;
  wheel_slip_threshold_mps: number;
  wheel_slip_scale_mps: number;

  gear_shift_enabled: boolean;
  gear_shift_gain: number;
  gear_shift_freq_hz: number;
  gear_shift_duration_ms: number;
  gear_shift_rpm_pct_low: number;
  gear_shift_rpm_pct_high: number;
  gear_shift_min_gain_pct: number;
  gear_shift_max_gain_pct: number;
}

export interface Config {
  gt7: GT7Config;
  web: WebConfig;
  audio: AudioConfig;
}

export type ConfigUpdates = {
  [K in keyof Config]?: Partial<Config[K]>;
};

export interface GT7Status {
  state: "starting" | "discovering" | "connected" | "stale";
  ps5_ip: string | null;
  packet_count: number;
  packets_per_sec: number;
  last_packet_age_s: number | null;
  discovery_elapsed_s: number;
}

export interface TelemetrySummary {
  engine_rpm: number;
  speed_kph: number;
  throttle: number;
  brake: number;
  current_gear: number;
  lap_count: number;
  packet_id: number;
}

export interface Status {
  gt7: GT7Status;
  telemetry: TelemetrySummary | null;
  muted: boolean;
}

export interface ProfilesState {
  active: string;
  names: string[];
}

export const DEFAULT_PROFILE_NAME = "default";
