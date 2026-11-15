import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, fmtBytes } from "../api";
import { Card, EmptyState, Spinner, StatusBadge } from "../components/ui";

interface Selection {
  id: string;
  name: string;
  path: string;
}

export default function DriveStudioPage() {
  const qc = useQueryClient();
  const status = useQuery({ queryKey: ["drive-status"], queryFn: api.driveStatus });
  const roots = useQuery({
    queryKey: ["drive-roots"],
    queryFn: api.driveRoots,
    enabled: status.data?.connected ?? false,
  });

  const [openFolders, setOpenFolders] = useState<Record<string, boolean>>({});
  const [childrenCache, setChildrenCache] = useState<Record<string, { folders: { id: string; name: string }[]; files: { id: string; name: string; size: number; modified_at: string }[] }>>({});
  const [selected, setSelected] = useState<Selection[]>([]);
  const [scanResult, setScanResult] = useState<{ videos: number; stats: Record<string, number> } | null>(null);

  const connect = useMutation({
    mutationFn: async () => {
      const { auth_url } = await api.driveAuthStart();
      window.open(auth_url, "_blank", "noopener");
    },
    onSuccess: () => {
      const t = setInterval(() => {
        qc.invalidateQueries({ queryKey: ["drive-status"] }).then(() => {
          const s = status.data;
          if (s?.connected) {
            clearInterval(t);
            qc.invalidateQueries({ queryKey: ["drive-roots"] });
          }
        });
      }, 2000);
      // best-effort polling guard; cleared on unmount via timeout
      setTimeout(() => clearInterval(t), 120_000);
    },
  });

  const disconnect = useMutation({
    mutationFn: api.driveDisconnect,
    onSuccess: () => qc.invalidateQueries({ queryKey: ["drive-status"] }),
  });

  const expand = async (folderId: string, name: string, path: string) => {
    const next = { ...openFolders, [folderId]: !openFolders[folderId] };
    setOpenFolders(next);
    if (next[folderId] && !childrenCache[folderId]) {
      const data = await api.driveFolder(folderId);
      setChildrenCache((c) => ({
        ...c,
        [folderId]: {
          folders: data.folders.map((f) => ({ id: f.id, name: f.name })),
          files: data.video_files.map((f) => ({ id: f.id, name: f.name, size: f.size, modified_at: f.modified_at })),
        },
      }));
    }
    void name;
    void path;
  };

  const scan = useMutation({
    mutationFn: async () => {
      const res = await api.driveScan(selected.map((s) => s.id));
      setScanResult({ videos: res.videos, stats: res.stats });
      return res;
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ["videos"] }),
  });

  const toggleSelected = (sel: Selection) => {
    setSelected((prev) =>
      prev.some((s) => s.id === sel.id)
        ? prev.filter((s) => s.id !== sel.id)
        : [...prev, sel]
    );
  };

  if (status.isLoading) return <Spinner label="Checking Drive connection…" />;

  if (!status.data?.connected) {
    return (
      <div className="space-y-5">
        <div>
          <h1 className="text-xl font-semibold text-ink-100">Drive Studio</h1>
          <p className="text-sm text-ink-500">Browse Google Drive folders and import videos.</p>
        </div>
        <Card className="flex flex-col items-center gap-4 p-10 text-center">
          <StatusBadge status="disconnected" />
          <p className="text-sm text-ink-300">Google Drive is not connected.</p>
          <button className="btn-primary" onClick={() => connect.mutate()} disabled={connect.isPending}>
            {connect.isPending ? "Opening authorization…" : "Connect Google Drive"}
          </button>
          {connect.isError && <div className="text-sm text-danger">{String(connect.error)}</div>}
          <p className="max-w-md text-xs text-ink-500">
            Read-only access is requested ({`drive.readonly`}). Configure
            <code className="mx-1">ARCUS_GOOGLE_OAUTH_CLIENT_ID</code> /
            <code className="mx-1">ARCUS_GOOGLE_OAUTH_CLIENT_SECRET</code> in the backend.
          </p>
        </Card>
      </div>
    );
  }

  const renderNode = (id: string, name: string, path: string, isRoot = false) => {
    const children = childrenCache[id];
    const open = openFolders[id];
    const sel: Selection = { id, name, path };
    return (
      <div key={id}>
        <div className="flex items-center gap-2 rounded-lg px-2 py-1.5 hover:bg-ink-850">
          <input
            type="checkbox"
            checked={selected.some((s) => s.id === id)}
            onChange={() => toggleSelected(sel)}
            className="h-4 w-4 accent-emerald-500"
          />
          <button
            className="flex flex-1 items-center gap-2 text-left text-sm text-ink-200"
            onClick={() => expand(id, name, path)}
            disabled={isRoot && roots.isLoading}
          >
            <span className="text-ink-500">{isRoot ? "📁" : open ? "▾" : "▸"}</span>
            <span className="truncate">{name}</span>
          </button>
        </div>
        {open && children && (
          <div className="ml-6 space-y-1 border-l border-ink-700 pl-2">
            {children.folders.map((f) =>
              renderNode(f.id, f.name, `${path}/${f.name}`)
            )}
            {children.files.map((f) => (
              <div key={f.id} className="flex items-center gap-2 px-2 py-1.5 text-sm text-ink-400">
                <span>🎬</span>
                <span className="truncate">{f.name}</span>
                <span className="ml-auto text-xs text-ink-500">{fmtBytes(f.size)}</span>
              </div>
            ))}
            {children.folders.length === 0 && children.files.length === 0 && (
              <div className="px-2 py-1 text-xs text-ink-500">Empty folder</div>
            )}
          </div>
        )}
      </div>
    );
  };

  return (
    <div className="space-y-5">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold text-ink-100">Drive Studio</h1>
          <p className="text-sm text-ink-500">
            Connected as {status.data.account_email || "—"} · read-only access
          </p>
        </div>
        <button className="btn-secondary" onClick={() => disconnect.mutate()}>Disconnect</button>
      </div>

      {roots.isLoading ? (
        <Spinner label="Loading Drive roots…" />
      ) : roots.data && roots.data.length > 0 ? (
        <Card>
          <div className="flex items-center justify-between pb-3">
            <h2 className="text-sm font-semibold text-ink-100">Folder browser</h2>
            <div className="text-xs text-ink-500">{selected.length} folders selected</div>
          </div>
          <div className="space-y-1">
            {roots.data.map((r) => renderNode(r.id, r.name, "", true))}
          </div>
        </Card>
      ) : (
        <EmptyState title="No accessible Drive roots" hint="Check folder permissions." />
      )}

      {selected.length > 0 && (
        <Card>
          <h2 className="mb-3 text-sm font-semibold text-ink-100">Selection</h2>
          <ul className="mb-3 space-y-1 text-sm text-ink-300">
            {selected.map((s) => (
              <li key={s.id} className="flex items-center gap-2">
                <span className="truncate">{s.path || s.name}</span>
                <button className="text-ink-500 hover:text-danger" onClick={() => toggleSelected(s)}>✕</button>
              </li>
            ))}
          </ul>
          <button className="btn-primary" onClick={() => scan.mutate()} disabled={scan.isPending}>
            {scan.isPending ? "Scanning…" : `Scan ${selected.length} folder(s) recursively`}
          </button>
          {scanResult && (
            <div className="mt-3 text-sm text-ink-300">
              Scan complete — <span className="font-semibold text-accent">{scanResult.videos} videos</span> discovered.
              <div className="mt-1 text-xs text-ink-500">
                {Object.entries(scanResult.stats).map(([k, v]) => `${k}: ${v}`).join(" · ")}
              </div>
            </div>
          )}
          {scan.isError && <div className="mt-3 text-sm text-danger">{String(scan.error)}</div>}
        </Card>
      )}
    </div>
  );
}
