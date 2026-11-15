import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api, fmtDuration } from "../api";
import { Card, EmptyState, Field, ProgressBar, Spinner, StatusBadge } from "../components/ui";
import {
  CAPTION_STYLES, CLIP_COUNT_PRESETS, DURATION_PRESETS, LAYOUTS,
} from "../types";
import type { Batch, Job } from "../types";

export default function BatchStudioPage() {
  const qc = useQueryClient();
  const videos = useQuery({
    queryKey: ["videos", "eligible"],
    queryFn: () => api.listVideos({ status: "discovered" }).then((a) => api.listVideos({ status: "imported" }).then((b) => [...a, ...b])),
  });
  const batches = useQuery({ queryKey: ["batches"], queryFn: api.listBatches });

  const [selectedVideos, setSelectedVideos] = useState<Set<number>>(new Set());
  const [clipsPerVideo, setClipsPerVideo] = useState(3);
  const [presetIdx, setPresetIdx] = useState(0);
  const [quality, setQuality] = useState<"strict" | "balanced" | "max_coverage">("balanced");
  const [style, setStyle] = useState("bounce");
  const [layout, setLayout] = useState("blur");
  const [handle, setHandle] = useState("");
  const [batchName, setBatchName] = useState("");
  const [activeBatchId, setActiveBatchId] = useState<number | null>(null);

  // auto-select active batch
  useEffect(() => {
    const active = batches.data?.find((b) => !["completed", "failed", "cancelled"].includes(b.status));
    if (active && !activeBatchId) setActiveBatchId(active.id);
  }, [batches.data, activeBatchId]);

  const createBatch = useMutation({
    mutationFn: () =>
      api.createBatch({
        name: batchName || undefined,
        clips_per_video: clipsPerVideo,
        duration: { min_seconds: DURATION_PRESETS[presetIdx].min, max_seconds: DURATION_PRESETS[presetIdx].max },
        quality, caption_style: style, layout, creator_handle: handle,
        skip_processed: true, continue_on_error: true,
        video_ids: [...selectedVideos],
      }),
    onSuccess: (b) => {
      qc.invalidateQueries({ queryKey: ["batches"] });
      setActiveBatchId(b.id);
      setSelectedVideos(new Set());
    },
  });

  const toggleVideo = (id: number) =>
    setSelectedVideos((prev) => {
      const next = new Set(prev);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });

  const eligibleVideos = (videos.data ?? []).filter(
    (v) => v.status === "discovered" || v.status === "imported"
  );

  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-xl font-semibold text-ink-100">Batch Studio</h1>
        <p className="text-sm text-ink-500">
          Configure autonomous clip generation and monitor bulk processing.
        </p>
      </div>

      <div className="grid grid-cols-1 gap-5 lg:grid-cols-2">
        <Card>
          <h2 className="mb-4 text-sm font-semibold text-ink-100">New batch</h2>
          <div className="space-y-4">
            <Field label="Batch name">
              <input className="input" value={batchName} onChange={(e) => setBatchName(e.target.value)}
                     placeholder="Podcasts — November" />
            </Field>

            <Field label="Clips per video">
              <div className="flex flex-wrap gap-2">
                {CLIP_COUNT_PRESETS.map((p) => (
                  <button
                    key={p.label}
                    className={`btn ${clipsPerVideo === p.value ? "btn-primary" : "btn-secondary"} px-2 py-1.5 text-xs`}
                    onClick={() => setClipsPerVideo(p.value)}
                  >
                    {p.label}
                  </button>
                ))}
              </div>
            </Field>

            <Field label="Clip duration">
              <div className="flex flex-wrap gap-2">
                {DURATION_PRESETS.map((p, i) => (
                  <button
                    key={p.label}
                    className={`btn ${presetIdx === i ? "btn-primary" : "btn-secondary"} px-2 py-1.5 text-xs`}
                    onClick={() => setPresetIdx(i)}
                  >
                    {p.label}
                  </button>
                ))}
              </div>
            </Field>

            <Field label="Selection quality">
              <select className="input" value={quality} onChange={(e) => setQuality(e.target.value as never)}>
                <option value="strict">Strict — only high-scoring moments</option>
                <option value="balanced">Balanced — quality and quantity</option>
                <option value="max_coverage">Maximum coverage — lower thresholds</option>
              </select>
            </Field>

            <div className="grid grid-cols-2 gap-3">
              <Field label="Caption style">
                <select className="input" value={style} onChange={(e) => setStyle(e.target.value)}>
                  {CAPTION_STYLES.map((s) => <option key={s} value={s}>{s}</option>)}
                </select>
              </Field>
              <Field label="Layout">
                <select className="input" value={layout} onChange={(e) => setLayout(e.target.value)}>
                  {LAYOUTS.map((s) => <option key={s} value={s}>{s}</option>)}
                </select>
              </Field>
            </div>

            <Field label="Creator handle (optional)">
              <input className="input" value={handle} onChange={(e) => setHandle(e.target.value)}
                     placeholder="@yourhandle" />
            </Field>

            <button
              className="btn-primary w-full"
              disabled={selectedVideos.size === 0 || createBatch.isPending}
              onClick={() => createBatch.mutate()}
            >
              {createBatch.isPending
                ? "Creating batch…"
                : `Generate clips — ${selectedVideos.size} video(s), ${clipsPerVideo} clips each`}
            </button>
            {createBatch.isError && <div className="text-sm text-danger">{String(createBatch.error)}</div>}
          </div>
        </Card>

        <Card>
          <h2 className="mb-4 text-sm font-semibold text-ink-100">
            Videos to process ({selectedVideos.size} selected)
          </h2>
          {videos.isLoading ? (
            <Spinner />
          ) : eligibleVideos.length === 0 ? (
            <EmptyState
              title="No eligible videos"
              hint="Scan sources or upload videos first."
              action={<Link className="btn-secondary" to="/videos">Go to Videos</Link>}
            />
          ) : (
            <div className="max-h-96 space-y-1 overflow-y-auto">
              {eligibleVideos.map((v) => (
                <label
                  key={v.id}
                  className="flex cursor-pointer items-center gap-2 rounded-lg px-2 py-2 hover:bg-ink-850"
                >
                  <input
                    type="checkbox"
                    checked={selectedVideos.has(v.id)}
                    onChange={() => toggleVideo(v.id)}
                    className="h-4 w-4 accent-emerald-500"
                  />
                  <span className="flex-1 truncate text-sm text-ink-200">{v.name}</span>
                  <span className="text-xs text-ink-500">{fmtDuration(v.duration) || v.status}</span>
                </label>
              ))}
            </div>
          )}
          <div className="mt-3 flex gap-2">
            <button
              className="btn-secondary text-xs"
              onClick={() => setSelectedVideos(new Set(eligibleVideos.map((v) => v.id)))}
            >
              Select all
            </button>
            <button className="btn-secondary text-xs" onClick={() => setSelectedVideos(new Set())}>
              Clear
            </button>
          </div>
        </Card>
      </div>

      <BatchMonitor
        activeBatchId={activeBatchId}
        onSelect={setActiveBatchId}
        batches={batches.data ?? []}
      />
    </div>
  );
}

function BatchMonitor({
  activeBatchId, onSelect, batches,
}: { activeBatchId: number | null; onSelect: (id: number) => void; batches: Batch[] }) {
  const qc = useQueryClient();
  const batch = useQuery({
    queryKey: ["batch", activeBatchId],
    queryFn: () => api.getBatch(activeBatchId!),
    enabled: activeBatchId !== null,
    refetchInterval: (q) => {
      const b = q.state.data as Batch | undefined;
      return b && ["completed", "failed", "cancelled"].includes(b.status) ? false : 3000;
    },
  });
  const jobs = useQuery({
    queryKey: ["batch-jobs", activeBatchId],
    queryFn: () => api.batchJobs(activeBatchId!),
    enabled: activeBatchId !== null,
    refetchInterval: 3000,
  });

  const pause = useMutation({
    mutationFn: (id: number) => api.pauseBatch(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["batch"] }),
  });
  const resume = useMutation({
    mutationFn: (id: number) => api.resumeBatch(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["batch"] }),
  });
  const cancel = useMutation({
    mutationFn: (id: number) => api.cancelBatch(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["batch"] }),
  });
  const retry = useMutation({
    mutationFn: (id: number) => api.retryJob(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["batch-jobs"] }),
  });

  if (batches.length === 0) return null;

  return (
    <div className="space-y-4">
      <Card>
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-xs font-medium uppercase tracking-wide text-ink-400">Batches</span>
          {batches.map((b) => (
            <button
              key={b.id}
              onClick={() => onSelect(b.id)}
              className={`badge cursor-pointer ${activeBatchId === b.id ? "bg-accent/20 text-accent" : "bg-ink-700 text-ink-300"}`}
            >
              #{b.id} {b.name}
            </button>
          ))}
        </div>
      </Card>

      {activeBatchId !== null && (
        <Card>
          {batch.isLoading ? (
            <Spinner />
          ) : (
            <>
              <div className="flex flex-wrap items-center justify-between gap-3">
                <div>
                  <h2 className="text-sm font-semibold text-ink-100">
                    Batch #{activeBatchId} — {batch.data?.name}
                  </h2>
                  <div className="mt-1 flex items-center gap-3 text-xs text-ink-500">
                    <StatusBadge status={batch.data?.status ?? "queued"} />
                    <span>
                      {batch.data?.completed_videos ?? 0}/{batch.data?.total_videos ?? 0} done ·
                      {batch.data?.failed_videos ?? 0} failed ·
                      {batch.data?.clip_count ?? 0} clips
                    </span>
                  </div>
                </div>
                <div className="flex gap-2">
                  <button className="btn-secondary text-xs" onClick={() => pause.mutate(activeBatchId)}>
                    Pause
                  </button>
                  <button className="btn-secondary text-xs" onClick={() => resume.mutate(activeBatchId)}>
                    Resume
                  </button>
                  <button className="btn-danger text-xs" onClick={() => cancel.mutate(activeBatchId)}>
                    Cancel
                  </button>
                  <Link className="btn-secondary text-xs" to="/clips">View clips</Link>
                </div>
              </div>
              <div className="mt-3">
                <ProgressBar
                  value={
                    ((batch.data?.completed_videos ?? 0) + (batch.data?.failed_videos ?? 0)) /
                    Math.max(1, batch.data?.total_videos ?? 1)
                  }
                />
              </div>
              {batch.data?.error && (
                <div className="mt-2 text-xs text-danger">{batch.data.error}</div>
              )}

              <div className="mt-4">
                {jobs.isLoading ? (
                  <Spinner />
                ) : (
                  <div className="space-y-2">
                    {(jobs.data ?? []).map((j: Job) => (
                      <div key={j.id} className="rounded-lg bg-ink-850 p-3">
                        <div className="flex items-center justify-between text-sm">
                          <span className="text-ink-200">
                            Video #{j.video_id} · <StatusBadge status={j.status} />
                            <span className="ml-2 text-xs text-ink-500">{j.stage}</span>
                          </span>
                          <span className="text-xs text-ink-500">
                            {Math.round(j.progress * 100)}%
                          </span>
                        </div>
                        <div className="mt-1.5">
                          <ProgressBar value={j.progress} />
                        </div>
                        {j.status === "failed" && (
                          <div className="mt-1.5 flex items-center justify-between">
                            <span className="truncate text-xs text-danger">{j.error}</span>
                            <button
                              className="btn-secondary ml-3 shrink-0 px-2 py-1 text-xs"
                              onClick={() => retry.mutate(j.id)}
                            >
                              Retry
                            </button>
                          </div>
                        )}
                      </div>
                    ))}
                  </div>
                )}
              </div>
              <p className="mt-3 text-xs text-ink-500">
                Progress and job state persist across restarts. One failed video never
                stops the rest of the batch.
              </p>
            </>
          )}
        </Card>
      )}
    </div>
  );
}
