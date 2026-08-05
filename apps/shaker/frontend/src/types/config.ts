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
  output_channels: number;
  rear_gain_trim: number;

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
  engine_rumble_min_hz: number;
  engine_rumble_max_hz: number;
  engine_rumble_bias: number;

  brake_rumble_enabled: boolean;
  brake_rumble_gain: number;
  brake_rumble_freq_hz: number;
  brake_rumble_threshold_pct: number;
  brake_rumble_bias: number;

  rev_limiter_enabled: boolean;
  rev_limiter_gain: number;
  rev_limiter_freq_hz: number;
  rev_limiter_trigger_pct: number;
  rev_limiter_bias: number;

  wheel_slip_enabled: boolean;
  wheel_slip_gain: number;
  wheel_slip_freq_hz: number;
  wheel_slip_threshold_mps: number;
  wheel_slip_scale_mps: number;
  wheel_slip_lock_freq_hz: number;

  gear_shift_enabled: boolean;
  gear_shift_gain: number;
  gear_shift_freq_hz: number;
  gear_shift_duration_ms: number;
  gear_shift_rpm_pct_low: number;
  gear_shift_rpm_pct_high: number;
  gear_shift_min_gain_pct: number;
  gear_shift_max_gain_pct: number;
  gear_shift_bias: number;
  drivetrain_routing_enabled: boolean;
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

// Raw per-corner fields straight off the latest packet, plus the two values
// they are read against. Mirrors _axle_diagnostics()["raw"] in web/app.py.
// Null until the first packet arrives.
export interface AxleRawStatus {
  speed_mps: number;
  current_gear: number;
  wheel_rps_FL: number;
  wheel_rps_FR: number;
  wheel_rps_RL: number;
  wheel_rps_RR: number;
  tire_radius_FL: number;
  tire_radius_FR: number;
  tire_radius_RL: number;
  tire_radius_RR: number;
  // rps * radius — compare against speed_mps at a steady cruise.
  wheel_surface_speed_FL: number;
  wheel_surface_speed_FR: number;
  wheel_surface_speed_RL: number;
  wheel_surface_speed_RR: number;
  suspension_FL: number;
  suspension_FR: number;
  suspension_RL: number;
  suspension_RR: number;
}

// Mirrors _axle_diagnostics() in web/app.py. Diagnostics only — nothing here
// is configurable, and nothing on the audio path reads the per-axle values yet.
export interface AxleStatus {
  // SIGNED m/s: positive = wheel faster than the car (spin), negative = slower
  // (lockup). Per axle, the corner with the largest magnitude keeps its sign.
  slip_front: number;
  slip_rear: number;
  suspension_activity_front: number;
  suspension_activity_rear: number;
  // Legacy whole-car scalars, kept visible so drift from the new values shows.
  slip_magnitude: number;
  suspension_activity: number;
  raw: AxleRawStatus | null;
}

export interface CarIdentity {
  code: number | null;
  layout: string | null;
  driven_axle: string | null;
  engine_position: string | null;
}

export interface Status {
  gt7: GT7Status;
  car: CarIdentity;
  telemetry: TelemetrySummary | null;
  muted: boolean;
  axle: AxleStatus;
}

export interface ProfilesState {
  active: string;
  names: string[];
}

export const DEFAULT_PROFILE_NAME = "default";
