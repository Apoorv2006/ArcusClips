import { describe, it, expect, beforeEach, vi } from "vitest";
import { api, ApiError, setToken } from "./api";
import type { VideoItem } from "./types";

function mockFetch(status: number, body: unknown) {
  return vi.fn().mockResolvedValue({
    status,
    ok: status >= 200 && status < 300,
    text: () => Promise.resolve(typeof body === "string" ? body : JSON.stringify(body)),
  });
}

describe("api request helper", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    localStorage.clear();
  });

  it("sends a bearer token when present", async () => {
    setToken("tok123");
    const fetchMock = mockFetch(200, { ok: true });
    vi.stubGlobal("fetch", fetchMock);

    await api.updateSettings({});

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    const headers = init.headers as Record<string, string>;
    expect(headers["Authorization"]).toBe("Bearer tok123");
  });

  it("parses JSON responses", async () => {
    const video: VideoItem = {
      id: 1, source_id: null, kind: "upload", name: "a.mp4", origin_url: "",
      folder_path: "", local_path: "", duration: 10, size: 100, width: 1080,
      height: 1920, has_audio: true, media_valid: true, status: "imported",
      status_detail: "", created_at: "2025-01-01T00:00:00",
    };
    vi.stubGlobal("fetch", mockFetch(200, [video]));
    const res = await api.listVideos();
    expect(res[0].name).toBe("a.mp4");
  });

  it("throws ApiError with detail on failure", async () => {
    vi.stubGlobal("fetch", mockFetch(422, { detail: "bad input" }));
    await expect(api.createSource({ type: "video_url", url_or_id: "x" })).rejects.toBeInstanceOf(ApiError);
    try {
      await api.createSource({ type: "video_url", url_or_id: "x" });
    } catch (e) {
      expect(e).toBeInstanceOf(ApiError);
      expect((e as ApiError).status).toBe(422);
      expect((e as ApiError).message).toBe("bad input");
    }
  });

  it("clears token on 401", async () => {
    setToken("expired");
    vi.stubGlobal("fetch", mockFetch(401, { detail: "unauthorized" }));
    await expect(api.me()).rejects.toBeInstanceOf(ApiError);
    expect(getTokenSafe()).toBeNull();
  });
});

function getTokenSafe(): string | null {
  return localStorage.getItem("arcusclips.token");
}
