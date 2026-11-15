import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api, fmtBytes } from "../api";
import { Card, ErrorState, Spinner, StatCard, StatusBadge } from "../components/ui";

export default function DashboardPage() {
  const stats = useQuery({ queryKey: ["dashboard"], queryFn: api.dashboard });
  const worker = useQuery({ queryKey: ["worker"], queryFn: api.workerStatus });
  const logs = useQuery({ queryKey: ["logs"], queryFn: () => api.logs(30) });

  if (stats.isLoading) return <Spinner label="Loading dashboard…" />;
  if (stats.isError)
    return <ErrorState message={String(stats.error)} onRetry={() => stats.refetch()} />;
  const s = stats.data!;

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold text-ink-100">Dashboard</h1>
        <p className="text-sm text-ink-500">Local-first AI video clipping studio</p>
      </div>

      <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
        <StatCard label="Connected sources" value={s.sources_connected} />
        <StatCard label="Discovered videos" value={s.discovered_videos} />
        <StatCard label="Imported videos" value={s.imported_videos} />
        <StatCard label="Processed videos" value={s.processed_videos} />
        <StatCard label="Clips generated" value={s.clips_generated} accent />
        <StatCard label="Active batches" value={s.active_batches} />
        <StatCard label="Failed jobs" value={s.failed_jobs} />
        <StatCard
          label="Storage"
          value={fmtBytes(s.storage_bytes)}
          hint={`of ${fmtBytes(s.storage_limit)}`}
        />
      </div>

      <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
        <Card>
          <div className="text-xs font-medium uppercase tracking-wide text-ink-400">Local AI</div>
          <div className="mt-2 flex items-center gap-3">
            <StatusBadge status={s.lmstudio_connected ? "connected" : "disconnected"} />
            <span className="text-sm text-ink-300">
              LM Studio at 127.0.0.1:1234
            </span>
          </div>
          <p className="mt-2 text-xs text-ink-500">
            {s.lmstudio_connected
              ? "Connected — clip selection uses your local model."
              : "Not reachable — moment selection will use deterministic local heuristics (flagged in outputs)."}
          </p>
          <Link to="/settings" className="btn-secondary mt-4">
            Configure LM Studio
          </Link>
        </Card>

        <Card>
          <div className="text-xs font-medium uppercase tracking-wide text-ink-400">Worker</div>
          <div className="mt-2 flex items-center gap-3">
            <StatusBadge status={worker.data?.running ? "running" : "disconnected"} />
            <span className="text-sm text-ink-300">
              {worker.data
                ? `${worker.data.active_jobs} / ${worker.data.capacity} concurrent jobs`
                : ""}
            </span>
          </div>
          <p className="mt-2 text-xs text-ink-500">
            Persistent DB-backed job queue with restart recovery.
          </p>
          <Link to="/batches" className="btn-secondary mt-4">
            Open Batch Studio
          </Link>
        </Card>

        <Card>
          <div className="text-xs font-medium uppercase tracking-wide text-ink-400">Quick start</div>
          <ol className="mt-3 space-y-2 text-sm text-ink-300">
            <li>1. Connect Google Drive or add video URLs in Sources</li>
            <li>2. Verify LM Studio in Settings</li>
            <li>3. Create a batch in Batch Studio and monitor it</li>
            <li>4. Review clips, edit, and export a ZIP</li>
          </ol>
        </Card>
      </div>

      <Card>
        <div className="mb-3 flex items-center justify-between">
          <h2 className="text-sm font-semibold text-ink-100">Recent activity</h2>
          <button className="text-xs text-ink-400 hover:text-ink-100" onClick={() => logs.refetch()}>
            Refresh
          </button>
        </div>
        {logs.isLoading ? (
          <Spinner />
        ) : logs.data && logs.data.length > 0 ? (
          <ul className="divide-y divide-ink-700/60">
            {logs.data.slice(0, 15).map((log) => (
              <li key={log.id} className="flex gap-3 py-2 text-sm">
                <span
                  className={`badge shrink-0 ${
                    log.level === "error" ? "bg-danger/20 text-danger" :
                    log.level === "warning" ? "bg-warn/20 text-warn" : "bg-ink-600/40 text-ink-300"
                  }`}
                >
                  {log.level}
                </span>
                <span className="text-ink-300">{log.message}</span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-sm text-ink-500">No events yet.</p>
        )}
      </Card>
    </div>
  );
}
