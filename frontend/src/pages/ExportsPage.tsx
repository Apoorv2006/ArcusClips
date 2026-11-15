import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, fmtBytes, fmtDate, getToken } from "../api";
import { Card, EmptyState, ErrorState, Modal, Spinner, StatusBadge } from "../components/ui";

export default function ExportsPage() {
  const qc = useQueryClient();
  const exports = useQuery({ queryKey: ["exports"], queryFn: api.listExports });
  const batches = useQuery({ queryKey: ["batches"], queryFn: api.listBatches });
  const [batchId, setBatchId] = useState<string>("");
  const [open, setOpen] = useState(false);

  const create = useMutation({
    mutationFn: () => api.createExport(Number(batchId)),
    onSuccess: () => {
      setOpen(false);
      qc.invalidateQueries({ queryKey: ["exports"] });
    },
  });

  const download = async (id: number) => {
    const resp = await fetch(api.exportDownloadUrl(id), {
      headers: { Authorization: `Bearer ${getToken() ?? ""}` },
    });
    const blob = await resp.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `arcusclips-batch-${id}.zip`;
    a.click();
    URL.revokeObjectURL(url);
  };

  if (exports.isLoading) return <Spinner label="Loading exports…" />;
  if (exports.isError)
    return <ErrorState message={String(exports.error)} onRetry={() => exports.refetch()} />;

  const exportRows = exports.data ?? [];

  return (
    <div className="space-y-5">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold text-ink-100">Exports</h1>
          <p className="text-sm text-ink-500">
            ZIP archives of finished batches with MP4s, thumbnails and a manifest.
          </p>
        </div>
        <button
          className="btn-primary"
          onClick={() => setOpen(true)}
          disabled={!batches.data || batches.data.length === 0}
        >
          + Export batch
        </button>
      </div>

      {exportRows.length === 0 ? (
        <EmptyState
          title="No exports yet"
          hint="Export a completed batch to download all its clips as a ZIP with a manifest."
          action={<button className="btn-primary" onClick={() => setOpen(true)}>+ Export batch</button>}
        />
      ) : (
        <Card className="overflow-hidden p-0">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-ink-700 text-left text-xs uppercase tracking-wide text-ink-500">
                <th className="px-4 py-3">Batch</th>
                <th className="px-4 py-3">Clips</th>
                <th className="px-4 py-3">Size</th>
                <th className="px-4 py-3">Status</th>
                <th className="px-4 py-3">Created</th>
                <th className="px-4 py-3">Action</th>
              </tr>
            </thead>
            <tbody>
              {exportRows.map((e) => (
                <tr key={e.id} className="table-row hover:bg-ink-850/60">
                  <td className="px-4 py-3">#{e.batch_id}</td>
                  <td className="px-4 py-3 text-ink-300">{e.clip_count}</td>
                  <td className="px-4 py-3 text-ink-400">{fmtBytes(e.size)}</td>
                  <td className="px-4 py-3"><StatusBadge status={e.status} /></td>
                  <td className="px-4 py-3 text-ink-400">{fmtDate(e.created_at)}</td>
                  <td className="px-4 py-3">
                    {e.status === "ready" ? (
                      <button className="btn-secondary px-2 py-1 text-xs" onClick={() => void download(e.id)}>
                        Download ZIP
                      </button>
                    ) : e.status === "failed" ? (
                      <span className="text-xs text-danger">{e.error}</span>
                    ) : (
                      <span className="text-xs text-ink-500">pending</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
      )}

      <Modal open={open} title="Export batch" onClose={() => setOpen(false)}>
        <div className="space-y-4">
          <label className="block">
            <span className="label">Batch</span>
            <select className="input" value={batchId} onChange={(e) => setBatchId(e.target.value)}>
              <option value="">Select a batch…</option>
              {batches.data?.map((b) => (
                <option key={b.id} value={String(b.id)}>
                  #{b.id} — {b.name} ({b.clip_count} clips)
                </option>
              ))}
            </select>
          </label>
          <p className="text-xs text-ink-500">
            Exports finished clips as a ZIP: <code>clips/</code>, <code>thumbnails/</code>,
            and <code>metadata/</code> with <code>manifest.csv</code> and
            <code> batch_summary.json</code>.
          </p>
          <div className="flex justify-end gap-2">
            <button className="btn-secondary" onClick={() => setOpen(false)}>Cancel</button>
            <button
              className="btn-primary"
              disabled={!batchId || create.isPending}
              onClick={() => create.mutate()}
            >
              {create.isPending ? "Exporting…" : "Create export"}
            </button>
          </div>
          {create.isError && <div className="text-sm text-danger">{String(create.error)}</div>}
        </div>
      </Modal>
    </div>
  );
}
