import { useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, fmtBytes, fmtDuration, getToken } from "../api";
import { Card, EmptyState, ErrorState, Spinner, StatusBadge } from "../components/ui";
import type { Clip } from "../types";

export default function ClipsPage() {
  const qc = useQueryClient();
  const [batchFilter, setBatchFilter] = useState<string>("");
  const [selected, setSelected] = useState<Set<number>>(new Set());

  const clips = useQuery({ queryKey: ["clips"], queryFn: () => api.listClips() });
  const batches = useQuery({ queryKey: ["batches"], queryFn: api.listBatches });

  const filtered = clips.data?.filter(
    (c) => !batchFilter || String(c.batch_id) === batchFilter
  ) ?? [];

  const toggle = (id: number) =>
    setSelected((prev) => {
      const next = new Set(prev);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });

  const download = async (clip: Clip) => {
    const resp = await fetch(api.clipDownloadUrl(clip.id), {
      headers: { Authorization: `Bearer ${getToken() ?? ""}` },
    });
    if (!resp.ok) throw new Error("Download failed");
    const blob = await resp.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `clip_${clip.id}.mp4`;
    a.click();
    URL.revokeObjectURL(url);
  };

  const remove = useMutation({
    mutationFn: (id: number) => api.deleteClip(id),
    onMutate: (id: number) => {
      setSelected((prev) => {
        const next = new Set(prev);
        next.delete(id);
        return next;
      });
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["clips"] });
    },
  });

  if (clips.isLoading) return <Spinner label="Loading clips…" />;
  if (clips.isError)
    return <ErrorState message={String(clips.error)} onRetry={() => clips.refetch()} />;

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold text-ink-100">Clips</h1>
          <p className="text-sm text-ink-500">Generated short-form videos, ready to review.</p>
        </div>
        <div className="flex items-center gap-2">
          <select className="input w-44" value={batchFilter} onChange={(e) => setBatchFilter(e.target.value)}>
            <option value="">All batches</option>
            {batches.data?.map((b) => (
              <option key={b.id} value={String(b.id)}>{b.name}</option>
            ))}
          </select>
          {selected.size > 0 && (
            <button
              className="btn-secondary"
              onClick={() => selected.forEach((id) => void download(clips.data!.find((c) => c.id === id)!))}
            >
              Download {selected.size} selected
            </button>
          )}
        </div>
      </div>

      {filtered.length === 0 ? (
        <EmptyState
          title="No clips yet"
          hint="Create a batch in Batch Studio to generate clips from your videos."
          action={<Link className="btn-primary" to="/batches">Open Batch Studio</Link>}
        />
      ) : (
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {filtered.map((clip) => (
            <Card key={clip.id} className="flex flex-col gap-3">
              <div className="flex items-start gap-3">
                <input
                  type="checkbox"
                  checked={selected.has(clip.id)}
                  onChange={() => toggle(clip.id)}
                  className="mt-1 h-4 w-4 accent-emerald-500"
                />
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2">
                    <span className="truncate text-sm font-semibold text-ink-100">{clip.title || `Clip ${clip.index}`}</span>
                  </div>
                  <div className="mt-0.5 text-xs text-ink-500">{clip.video_name}</div>
                </div>
              </div>

              <div className="flex items-center gap-3 text-xs text-ink-400">
                <StatusBadge status={clip.status} />
                <span>{fmtDuration(clip.duration)}</span>
                <span>score {clip.score.toFixed(1)}</span>
                <span>{clip.caption_style}</span>
                <span>{clip.layout}</span>
              </div>

              {clip.is_heuristic && (
                <div className="badge bg-warn/20 text-warn">
                  heuristic selection (LM Studio unavailable)
                </div>
              )}
              {clip.hook && (
                <div className="rounded-lg bg-ink-850 p-2 text-xs text-ink-300">
                  <span className="text-ink-500">Hook: </span>
                  {clip.hook}
                </div>
              )}

              <video
                src={api.clipDownloadUrl(clip.id)}
                controls
                className="aspect-video w-full rounded-lg bg-black"
                poster={clip.versions[0]?.thumbnail_path ? api.clipThumbUrl(clip.id) : undefined}
              />

              <div className="flex flex-wrap gap-2">
                <Link className="btn-secondary px-2 py-1 text-xs" to={`/clips/${clip.id}`}>
                  Edit
                </Link>
                <button className="btn-secondary px-2 py-1 text-xs" onClick={() => void download(clip)}>
                  Download MP4
                </button>
                <button
                  className="btn-danger px-2 py-1 text-xs"
                  onClick={() => remove.mutate(clip.id)}
                >
                  Delete
                </button>
                <span className="ml-auto self-center text-xs text-ink-500">
                  v{clip.current_version} · {fmtBytes(clip.versions[0]?.size ?? 0)}
                </span>
              </div>
            </Card>
          ))}
        </div>
      )}
    </div>
  );
}
