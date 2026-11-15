// API client: thin fetch wrapper with bearer token + typed helpers.
import type {
  Batch, Clip, DashboardStats, ExportItem, Job, LMStudioModels, LogEntry,
  SettingsData, Source, Transcript, TokenResponse, User, VideoItem,
  DriveStatus, DriveFolderNode, DriveFileItem, WorkerStatus,
} from "./types";

const TOKEN_KEY = "arcusclips.token";

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY);
}
export function setToken(token: string) {
  localStorage.setItem(TOKEN_KEY, token);
}
export function clearToken() {
  localStorage.removeItem(TOKEN_KEY);
}

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = {};
  const token = getToken();
  if (token) headers["Authorization"] = `Bearer ${token}`;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const resp = await fetch(`/api${path}`, {
    method,
    headers,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (resp.status === 204) return undefined as T;
  const text = await resp.text();
  let data: unknown = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = text;
    }
  }
  if (!resp.ok) {
    const detail =
      data && typeof data === "object" && "detail" in data
        ? String((data as { detail: unknown }).detail)
        : String(text || resp.statusText);
    if (resp.status === 401) clearToken();
    throw new ApiError(resp.status, detail);
  }
  return data as T;
}

export const api = {
  // auth
  login: (username: string, password: string) =>
    request<TokenResponse>("POST", "/auth/login", { username, password }),
  me: () => request<User>("GET", "/auth/me"),

  // dashboard / system
  health: () =>
    request<{ status: string; app: string; ffmpeg: string; lmstudio_connected: boolean }>(
      "GET", "/health"
    ),
  dashboard: () => request<DashboardStats>("GET", "/dashboard"),
  workerStatus: () => request<WorkerStatus>("GET", "/worker/status"),
  logs: (limit = 200) => request<LogEntry[]>("GET", `/logs?limit=${limit}`),

  // settings
  getSettings: () => request<SettingsData>("GET", "/settings"),
  updateSettings: (body: Partial<SettingsData>) =>
    request<{ ok: boolean }>("PUT", "/settings", body),
  lmstudioModels: () => request<LMStudioModels>("GET", "/settings/lmstudio/models"),
  lmstudioTest: () => request<LMStudioModels>("POST", "/settings/lmstudio/test"),

  // sources
  listSources: () => request<Source[]>("GET", "/sources"),
  createSource: (body: { type: string; name?: string; url_or_id: string; folder_id?: string; config?: Record<string, unknown> }) =>
    request<Source>("POST", "/sources", body),
  deleteSource: (id: number) => request<{ ok: boolean }>("DELETE", `/sources/${id}`),
  scanSource: (id: number, driveId?: string) =>
    request<{ ok: boolean; discovered: number }>(
      "POST", `/sources/${id}/scan${driveId ? `?drive_id=${encodeURIComponent(driveId)}` : ""}`
    ),
  toggleMonitor: (id: number, monitoring: boolean) =>
    request<{ ok: boolean; monitoring: boolean }>("POST", `/sources/${id}/monitor?monitoring=${monitoring}`),

  // drive
  driveStatus: () => request<DriveStatus>("GET", "/drive/status"),
  driveAuthStart: () => request<{ auth_url: string }>("GET", "/drive/auth/start"),
  driveDisconnect: () => request<{ ok: boolean }>("POST", "/drive/disconnect"),
  driveRoots: () => request<DriveFolderNode[]>("GET", "/drive/roots"),
  driveFolder: (folderId: string, driveId?: string) =>
    request<{ folders: DriveFolderNode[]; video_files: DriveFileItem[] }>(
      "GET", `/drive/folders/${folderId}${driveId ? `?drive_id=${encodeURIComponent(driveId)}` : ""}`
    ),
  driveScan: (folderIds: string[], excludeIds: string[] = [], driveId?: string) =>
    request<{ ok: boolean; videos: number; stats: Record<string, number> }>(
      "POST", "/drive/scan",
      { folder_ids: folderIds, exclude_ids: excludeIds, ...(driveId ? { drive_id: driveId } : {}) }
    ),

  // videos
  listVideos: (params?: { source_id?: number; status?: string }) => {
    const qs = new URLSearchParams();
    if (params?.source_id) qs.set("source_id", String(params.source_id));
    if (params?.status) qs.set("status", params.status);
    const q = qs.toString();
    return request<VideoItem[]>("GET", `/videos${q ? `?${q}` : ""}`);
  },
  uploadVideo: (file: File) => {
    const form = new FormData();
    form.append("file", file);
    return fetch("/api/videos/upload", {
      method: "POST",
      headers: { Authorization: `Bearer ${getToken() ?? ""}` },
      body: form,
    }).then(async (r) => {
      if (!r.ok) throw new ApiError(r.status, (await r.text()).slice(0, 300));
      return (await r.json()) as VideoItem;
    });
  },
  transcript: (videoId: number) => request<Transcript>("GET", `/videos/${videoId}/transcript`),
  transcribe: (videoId: number) =>
    request<{ ok: boolean }>("POST", `/videos/${videoId}/transcribe`),

  // batches & jobs
  listBatches: () => request<Batch[]>("GET", "/batches"),
  createBatch: (options: Record<string, unknown>) =>
    request<Batch>("POST", "/batches", options),
  getBatch: (id: number) => request<Batch>("GET", `/batches/${id}`),
  batchJobs: (id: number) => request<Job[]>("GET", `/batches/${id}/jobs`),
  pauseBatch: (id: number) => request<{ ok: boolean }>("POST", `/batches/${id}/pause`),
  resumeBatch: (id: number) => request<{ ok: boolean }>("POST", `/batches/${id}/resume`),
  cancelBatch: (id: number) => request<{ ok: boolean }>("POST", `/batches/${id}/cancel`),
  retryJob: (jobId: number) => request<{ ok: boolean }>("POST", `/jobs/${jobId}/retry`),

  // clips
  listClips: (params?: { batch_id?: number; video_id?: number; status?: string }) => {
    const qs = new URLSearchParams();
    if (params?.batch_id) qs.set("batch_id", String(params.batch_id));
    if (params?.video_id) qs.set("video_id", String(params.video_id));
    if (params?.status) qs.set("status", params.status);
    const q = qs.toString();
    return request<Clip[]>("GET", `/clips${q ? `?${q}` : ""}`);
  },
  getClip: (id: number) => request<Clip>("GET", `/clips/${id}`),
  updateClip: (id: number, body: Partial<Clip>) => request<Clip>("PUT", `/clips/${id}`, body),
  renderClip: (id: number) => request<{ id: number; status: string; message: string }>("POST", `/clips/${id}/render`),
  deleteClip: (id: number) => request<{ ok: boolean }>("DELETE", `/clips/${id}`),
  clipDownloadUrl: (id: number) => `/api/clips/${id}/download`,
  clipThumbUrl: (id: number) => `/api/clips/${id}/thumbnail`,

  // exports
  listExports: () => request<ExportItem[]>("GET", "/exports"),
  createExport: (batchId: number) =>
    request<ExportItem>("POST", `/batches/${batchId}/export`),
  exportDownloadUrl: (id: number) => `/api/exports/${id}/download`,
};

export function fmtBytes(bytes: number): string {
  if (!bytes) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const i = Math.min(units.length - 1, Math.floor(Math.log(bytes) / Math.log(1024)));
  return `${(bytes / 1024 ** i).toFixed(i === 0 ? 0 : 1)} ${units[i]}`;
}

export function fmtDuration(seconds: number): string {
  const s = Math.max(0, Math.round(seconds));
  const m = Math.floor(s / 60);
  return `${m}:${String(s % 60).padStart(2, "0")}`;
}

export function fmtDate(iso: string | null): string {
  if (!iso) return "—";
  return new Date(iso + (iso.endsWith("Z") ? "" : "Z")).toLocaleString();
}
