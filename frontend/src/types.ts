// Shared API types matching the backend schemas.

export interface User {
  id: number;
  username: string;
  is_admin: boolean;
}

export interface TokenResponse {
  token: string;
  user_id: number;
  username: string;
  is_admin: boolean;
}

export type SourceType = "drive_folder" | "video_url" | "channel" | "playlist" | "webpage";

export interface Source {
  id: number;
  type: SourceType;
  name: string;
  url_or_id: string;
  folder_id: string;
  config: Record<string, unknown>;
  monitoring: boolean;
  last_scanned_at: string | null;
  scan_status: string;
  scan_error: string;
  discovered_count: number;
  imported_count: number;
  created_at: string;
}

export interface DriveStatus {
  connected: boolean;
  account_email: string;
  status: string;
}

export interface DriveFolderNode {
  id: string;
  name: string;
  kind: "folder" | "drive";
  is_root: boolean;
  file_count: number;
  folder_count: number;
}

export interface DriveFileItem {
  id: string;
  name: string;
  mime_type: string;
  size: number;
  modified_at: string;
}

export interface VideoItem {
  id: number;
  source_id: number | null;
  kind: string;
  name: string;
  origin_url: string;
  folder_path: string;
  local_path: string;
  duration: number;
  size: number;
  width: number;
  height: number;
  has_audio: boolean;
  media_valid: boolean;
  status: string;
  status_detail: string;
  created_at: string;
}

export interface Transcript {
  id: number;
  video_id: number;
  language: string;
  model_name: string;
  duration: number;
  segments: { start: number; end: number; text: string; words?: unknown[] }[];
  created_at: string;
}

export interface BatchOptions {
  name: string;
  clips_per_video: number;
  max_total_clips: number;
  duration: { min_seconds: number; max_seconds: number };
  quality: "strict" | "balanced" | "max_coverage";
  caption_style: string;
  layout: string;
  creator_handle: string;
  skip_processed: boolean;
  continue_on_error: boolean;
  source_ids: number[];
  video_ids: number[];
}

export interface Batch {
  id: number;
  name: string;
  status: string;
  options: BatchOptions;
  total_videos: number;
  completed_videos: number;
  failed_videos: number;
  clip_count: number;
  error: string;
  created_at: string;
  finished_at: string | null;
}

export interface Job {
  id: number;
  batch_id: number | null;
  video_id: number | null;
  kind: string;
  status: string;
  stage: string;
  progress: number;
  error: string;
  retry_count: number;
  started_at: string | null;
  finished_at: string | null;
}

export interface ClipVersion {
  id: number;
  version: number;
  render_path: string;
  thumbnail_path: string;
  duration: number;
  size: number;
  status: string;
  created_at: string;
}

export interface Clip {
  id: number;
  batch_id: number | null;
  video_id: number;
  video_name: string;
  folder_path: string;
  index: number;
  start_time: number;
  end_time: number;
  duration: number;
  hook: string;
  title: string;
  description: string;
  score: number;
  category: string;
  reason: string;
  caption_style: string;
  layout: string;
  handle: string;
  status: string;
  is_heuristic: boolean;
  current_version: number;
  versions: ClipVersion[];
  created_at: string;
}

export interface ExportItem {
  id: number;
  batch_id: number;
  zip_path: string;
  status: string;
  size: number;
  clip_count: number;
  error: string;
  created_at: string;
}

export interface DashboardStats {
  sources_connected: number;
  discovered_videos: number;
  imported_videos: number;
  processed_videos: number;
  clips_generated: number;
  active_batches: number;
  failed_jobs: number;
  storage_bytes: number;
  storage_limit: number;
  workers_available: boolean;
  lmstudio_connected: boolean;
}

export interface LMStudioModels {
  connected: boolean;
  models: string[];
  error: string;
}

export interface SettingsData {
  lmstudio: {
    base_url: string;
    model: string;
    api_key: string;
    request_timeout: number;
    max_tokens: number;
    temperature: number;
    max_context_tokens: number;
  };
  whisper: {
    model: string;
    model_dir: string;
    device: string;
    compute_type: string;
    language: string;
  };
  render: {
    width: number;
    height: number;
    fps: number;
    crf: number;
    preset: string;
  };
  processing: {
    max_processing_concurrency: number;
    max_render_concurrency: number;
    max_download_concurrency: number;
    allow_heuristic_fallback: boolean;
  };
  branding: {
    creator_handle: string;
    default_caption_style: string;
    default_layout: string;
    font_name: string;
  };
}

export interface WorkerStatus {
  running: boolean;
  active_jobs: number;
  capacity: number;
  poll_interval: number;
}

export interface LogEntry {
  id: number;
  level: string;
  source: string;
  message: string;
  created_at: string;
}

export const CAPTION_STYLES = ["bounce", "hype", "clean", "box", "karaoke", "ticker"];
export const LAYOUTS = ["full", "blur", "zoom"];
export const DURATION_PRESETS = [
  { label: "15–30s", min: 15, max: 30 },
  { label: "30–45s", min: 30, max: 45 },
  { label: "45–60s", min: 45, max: 60 },
  { label: "60–90s", min: 60, max: 90 },
];
export const CLIP_COUNT_PRESETS = [
  { label: "Best 3 clips", value: 3 },
  { label: "Best 5 clips", value: 5 },
  { label: "Best 10 clips", value: 10 },
];
