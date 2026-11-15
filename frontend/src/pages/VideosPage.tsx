import { useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, fmtBytes, fmtDuration } from "../api";
import { Card, EmptyState, ErrorState, Modal, Spinner, StatusBadge } from "../components/ui";
import type { VideoItem, Transcript } from "../types";

export default function VideosPage() {
  const qc = useQueryClient();
  const videos = useQuery({ queryKey: ["videos"], queryFn: () => api.listVideos() });
  const fileRef = useRef<HTMLInputElement>(null);
  const [uploading, setUploading] = useState(false);
  const [transcriptFor, setTranscriptFor] = useState<VideoItem | null>(null);

  const upload = useMutation({
    mutationFn: async (file: File) => {
      setUploading(true);
      try {
        return await api.uploadVideo(file);
      } finally {
        setUploading(false);
      }
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ["videos"] }),
  });

  const transcribe = useMutation({
    mutationFn: (id: number) => api.transcribe(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["videos"] }),
  });

  if (videos.isLoading) return <Spinner label="Loading videos…" />;
  if (videos.isError)
    return <ErrorState message={String(videos.error)} onRetry={() => videos.refetch()} />;

  const videoRows = videos.data ?? [];

  return (
    <div className="space-y-5">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold text-ink-100">Videos</h1>
          <p className="text-sm text-ink-500">
            Discovered, imported and processed source material.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <input
            ref={fileRef}
            type="file"
            accept="video/*"
            className="hidden"
            onChange={(e) => {
              const f = e.target.files?.[0];
              if (f) upload.mutate(f);
              e.target.value = "";
            }}
          />
          <button className="btn-secondary" onClick={() => fileRef.current?.click()} disabled={uploading}>
            {uploading ? "Uploading…" : "Upload video"}
          </button>
        </div>
      </div>

      {videoRows.length === 0 ? (
        <EmptyState
          title="No videos yet"
          hint="Connect Drive, scan sources, or upload a local video file."
        />
      ) : (
        <Card className="overflow-hidden p-0">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-ink-700 text-left text-xs uppercase tracking-wide text-ink-500">
                <th className="px-4 py-3">Name</th>
                <th className="px-4 py-3">Source</th>
                <th className="px-4 py-3">Duration</th>
                <th className="px-4 py-3">Size</th>
                <th className="px-4 py-3">Status</th>
                <th className="px-4 py-3">Actions</th>
              </tr>
            </thead>
            <tbody>
              {videoRows.map((v) => (
                <tr key={v.id} className="table-row hover:bg-ink-850/60">
                  <td className="px-4 py-3">
                    <div className="font-medium text-ink-100">{v.name}</div>
                    <div className="text-xs text-ink-500">{v.folder_path || v.origin_url || v.kind}</div>
                  </td>
                  <td className="px-4 py-3 text-ink-400">{v.kind}</td>
                  <td className="px-4 py-3 text-ink-300">{fmtDuration(v.duration)}</td>
                  <td className="px-4 py-3 text-ink-400">{fmtBytes(v.size)}</td>
                  <td className="px-4 py-3"><StatusBadge status={v.status} /></td>
                  <td className="px-4 py-3">
                    <div className="flex gap-2">
                      <button className="btn-secondary px-2 py-1 text-xs" onClick={() => setTranscriptFor(v)}>
                        Transcript
                      </button>
                      <button
                        className="btn-secondary px-2 py-1 text-xs"
                        onClick={() => transcribe.mutate(v.id)}
                        disabled={transcribe.isPending}
                      >
                        Transcribe
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
      )}

      <TranscriptModal video={transcriptFor} onClose={() => setTranscriptFor(null)} />
    </div>
  );
}

function TranscriptModal({ video, onClose }: { video: VideoItem | null; onClose: () => void }) {
  const transcript = useQuery({
    queryKey: ["transcript", video?.id],
    queryFn: () => api.transcript(video!.id),
    enabled: !!video,
  });

  return (
    <Modal open={!!video} title={`Transcript — ${video?.name ?? ""}`} onClose={onClose} wide>
      {!transcript.data ? (
        transcript.isLoading ? (
          <Spinner />
        ) : transcript.isError ? (
          <ErrorState message={String(transcript.error)} onRetry={() => transcript.refetch()} />
        ) : (
          <p className="text-sm text-ink-500">
            No transcript yet. Run Transcribe, or start a batch to transcribe automatically.
          </p>
        )
      ) : (
        <TranscriptView transcript={transcript.data} />
      )}
    </Modal>
  );
}

function TranscriptView({ transcript }: { transcript: Transcript }) {
  return (
    <div>
      <div className="mb-3 text-xs text-ink-500">
        {transcript.language} · {transcript.model_name} · {fmtDuration(transcript.duration)} ·
        {transcript.segments.length} segments
      </div>
      <ol className="max-h-96 space-y-2 overflow-y-auto">
        {transcript.segments.map((seg, i) => (
          <li key={i} className="flex gap-3 rounded-lg bg-ink-850 p-2 text-sm">
            <span className="shrink-0 font-mono text-xs text-ink-500">
              {fmtDuration(seg.start)}–{fmtDuration(seg.end)}
            </span>
            <span className="text-ink-200">{seg.text}</span>
          </li>
        ))}
      </ol>
    </div>
  );
}
