import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, fmtDate } from "../api";
import {
  EmptyState, ErrorState, Modal, Spinner, StatusBadge, Card, Field,
} from "../components/ui";
import type { Source, SourceType } from "../types";

const SOURCE_TYPES: { value: SourceType; label: string }[] = [
  { value: "video_url", label: "Video URL" },
  { value: "channel", label: "Creator channel" },
  { value: "playlist", label: "Playlist" },
  { value: "webpage", label: "Public webpage" },
  { value: "drive_folder", label: "Drive folder" },
];

export default function SourcesPage() {
  const qc = useQueryClient();
  const sources = useQuery({ queryKey: ["sources"], queryFn: api.listSources });
  const [open, setOpen] = useState(false);
  const [type, setType] = useState<SourceType>("video_url");
  const [name, setName] = useState("");
  const [url, setUrl] = useState("");

  const create = useMutation({
    mutationFn: () => api.createSource({ type, name: name || undefined, url_or_id: url }),
    onSuccess: () => {
      setOpen(false);
      setName("");
      setUrl("");
      qc.invalidateQueries({ queryKey: ["sources"] });
    },
  });

  const scan = useMutation({
    mutationFn: (id: number) => api.scanSource(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["sources"] }),
  });

  const toggle = useMutation({
    mutationFn: ({ id, monitoring }: { id: number; monitoring: boolean }) =>
      api.toggleMonitor(id, monitoring),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["sources"] }),
  });

  const remove = useMutation({
    mutationFn: (id: number) => api.deleteSource(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["sources"] }),
  });

  if (sources.isLoading) return <Spinner label="Loading sources…" />;
  if (sources.isError)
    return <ErrorState message={String(sources.error)} onRetry={() => sources.refetch()} />;

  const sourceRows = sources.data ?? [];

  return (
    <div className="space-y-5">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold text-ink-100">Sources</h1>
          <p className="text-sm text-ink-500">
            Connect Google Drive, add creator channels, playlists, video URLs and public webpages.
          </p>
        </div>
        <button className="btn-primary" onClick={() => setOpen(true)}>
          + Add source
        </button>
      </div>

      {sourceRows.length === 0 ? (
        <EmptyState
          title="No sources yet"
          hint="Add a video URL, channel, playlist or public webpage to start discovering content."
          action={<button className="btn-primary" onClick={() => setOpen(true)}>+ Add source</button>}
        />
      ) : (
        <div className="space-y-3">
          {sourceRows.map((s: Source) => (
            <Card key={s.id} className="flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
              <div className="min-w-0">
                <div className="flex items-center gap-2">
                  <span className="text-sm font-medium text-ink-100">{s.name}</span>
                  <StatusBadge status={s.scan_status} />
                  {!s.monitoring && <StatusBadge status="paused" />}
                </div>
                <div className="mt-1 truncate text-xs text-ink-500">
                  {SOURCE_TYPES.find((t) => t.value === s.type)?.label ?? s.type} · {s.url_or_id}
                </div>
                <div className="mt-1 text-xs text-ink-500">
                  {s.discovered_count} discovered · {s.imported_count} imported ·
                  last scanned {fmtDate(s.last_scanned_at)}
                </div>
                {s.scan_error && <div className="mt-1 text-xs text-danger">{s.scan_error}</div>}
              </div>
              <div className="flex shrink-0 gap-2">
                <button
                  className="btn-secondary"
                  disabled={scan.isPending}
                  onClick={() => scan.mutate(s.id)}
                >
                  Scan
                </button>
                <button
                  className="btn-secondary"
                  onClick={() => toggle.mutate({ id: s.id, monitoring: !s.monitoring })}
                >
                  {s.monitoring ? "Pause" : "Resume"}
                </button>
                <button
                  className="btn-danger"
                  onClick={() => remove.mutate(s.id)}
                >
                  Remove
                </button>
              </div>
            </Card>
          ))}
        </div>
      )}

      <Modal open={open} title="Add source" onClose={() => setOpen(false)}>
        <div className="space-y-4">
          <Field label="Type">
            <select className="input" value={type} onChange={(e) => setType(e.target.value as SourceType)}>
              {SOURCE_TYPES.map((t) => (
                <option key={t.value} value={t.value}>{t.label}</option>
              ))}
            </select>
          </Field>
          <Field label="Name (optional)">
            <input className="input" value={name} onChange={(e) => setName(e.target.value)}
                   placeholder="My YouTube channel" />
          </Field>
          <Field label="URL or identifier">
            <input className="input" value={url} onChange={(e) => setUrl(e.target.value)}
                   placeholder="https://… or Drive folder id" />
          </Field>
          <div className="flex justify-end gap-2">
            <button className="btn-secondary" onClick={() => setOpen(false)}>Cancel</button>
            <button
              className="btn-primary"
              disabled={!url || create.isPending}
              onClick={() => create.mutate()}
            >
              {create.isPending ? "Adding…" : "Add source"}
            </button>
          </div>
          {create.isError && <div className="text-sm text-danger">{String(create.error)}</div>}
        </div>
      </Modal>
    </div>
  );
}
