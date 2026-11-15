import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate, useParams } from "react-router-dom";
import { api, fmtBytes, fmtDuration, getToken } from "../api";
import { Card, ErrorState, Field, Spinner, StatusBadge } from "../components/ui";
import { CAPTION_STYLES, LAYOUTS } from "../types";

export default function ClipEditorPage() {
  const { clipId } = useParams();
  const id = Number(clipId);
  const qc = useQueryClient();
  const navigate = useNavigate();
  const clip = useQuery({ queryKey: ["clip", id], queryFn: () => api.getClip(id) });

  const [start, setStart] = useState<string>("");
  const [end, setEnd] = useState<string>("");
  const [hook, setHook] = useState("");
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [style, setStyle] = useState("");
  const [layout, setLayout] = useState("");
  const [handle, setHandle] = useState("");
  const [renderMsg, setRenderMsg] = useState("");

  // initialize draft from loaded clip
  useEffect(() => {
    if (!clip.data) return;
    setStart(String(clip.data.start_time));
    setEnd(String(clip.data.end_time));
    setHook(clip.data.hook);
    setTitle(clip.data.title);
    setDescription(clip.data.description);
    setStyle(clip.data.caption_style);
    setLayout(clip.data.layout);
    setHandle(clip.data.handle);
  }, [clip.data]);

  const save = useMutation({
    mutationFn: () =>
      api.updateClip(id, {
        start_time: parseFloat(start),
        end_time: parseFloat(end),
        hook, title, description, caption_style: style, layout, handle,
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["clip", id] }),
  });

  const render = useMutation({
    mutationFn: () => api.renderClip(id),
    onSuccess: (res) => {
      setRenderMsg(res.message);
      qc.invalidateQueries({ queryKey: ["clip", id] });
    },
  });

  const download = async () => {
    const resp = await fetch(api.clipDownloadUrl(id), {
      headers: { Authorization: `Bearer ${getToken() ?? ""}` },
    });
    const blob = await resp.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `clip_${id}.mp4`;
    a.click();
    URL.revokeObjectURL(url);
  };

  const remove = useMutation({
    mutationFn: () => api.deleteClip(id),
    onSuccess: () => navigate("/clips"),
  });

  if (clip.isLoading) return <Spinner label="Loading clip…" />;
  if (clip.isError)
    return <ErrorState message={String(clip.error)} onRetry={() => clip.refetch()} />;
  const c = clip.data!;
  const ver = c.versions[c.versions.length - 1];

  return (
    <div className="space-y-5">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold text-ink-100">Clip Editor</h1>
          <p className="text-sm text-ink-500">
            {c.title || `Clip ${c.index}`} · {c.video_name} ·{" "}
            <StatusBadge status={c.status} />
          </p>
        </div>
        <button className="btn-secondary" onClick={() => navigate("/clips")}>← Back to clips</button>
      </div>

      <div className="grid grid-cols-1 gap-5 lg:grid-cols-2">
        <Card>
          <h2 className="mb-3 text-sm font-semibold text-ink-100">Preview</h2>
          {ver?.render_path ? (
            <video
              src={api.clipDownloadUrl(id)}
              controls
              className="aspect-video w-full rounded-lg bg-black"
              poster={ver.thumbnail_path ? api.clipThumbUrl(id) : undefined}
            />
          ) : (
            <div className="aspect-video w-full rounded-lg bg-ink-850 flex items-center justify-center text-sm text-ink-500">
              Not rendered yet
            </div>
          )}
          <div className="mt-3 flex flex-wrap items-center gap-3 text-xs text-ink-400">
            <span>v{c.current_version}</span>
            <span>{fmtDuration(c.end_time - c.start_time)}</span>
            <span>{fmtBytes(ver?.size ?? 0)}</span>
            {c.is_heuristic && <span className="badge bg-warn/20 text-warn">heuristic selection</span>}
          </div>
          {renderMsg && <div className="mt-2 text-xs text-accent">{renderMsg}</div>}
          <div className="mt-3 flex gap-2">
            <button className="btn-secondary text-xs" onClick={() => void download()}>Download MP4</button>
            <button className="btn-danger text-xs" onClick={() => remove.mutate()}>Delete clip</button>
          </div>

          {c.versions.length > 0 && (
            <div className="mt-4">
              <div className="label">Version history</div>
              <ul className="space-y-1 text-xs">
                {c.versions.map((v) => (
                  <li key={v.id} className="flex items-center gap-2 text-ink-400">
                    <span className="badge bg-ink-700">v{v.version}</span>
                    <StatusBadge status={v.status} />
                    <span>{fmtDuration(v.duration)}</span>
                    <span>{fmtBytes(v.size)}</span>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </Card>

        <Card>
          <h2 className="mb-4 text-sm font-semibold text-ink-100">Adjust clip</h2>
          <div className="space-y-4">
            <div className="grid grid-cols-2 gap-3">
              <Field label="Start (seconds)">
                <input className="input" type="number" step="0.5" value={start}
                       onChange={(e) => setStart(e.target.value)} />
              </Field>
              <Field label="End (seconds)">
                <input className="input" type="number" step="0.5" value={end}
                       onChange={(e) => setEnd(e.target.value)} />
              </Field>
            </div>
            <Field label="Hook overlay">
              <input className="input" value={hook} onChange={(e) => setHook(e.target.value)} />
            </Field>
            <Field label="Title">
              <input className="input" value={title} onChange={(e) => setTitle(e.target.value)} />
            </Field>
            <Field label="Description">
              <textarea className="input" rows={3} value={description}
                        onChange={(e) => setDescription(e.target.value)} />
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
            <Field label="Creator handle">
              <input className="input" value={handle} onChange={(e) => setHandle(e.target.value)} />
            </Field>

            <div className="flex gap-2">
              <button className="btn-primary" onClick={() => save.mutate()} disabled={save.isPending}>
                {save.isPending ? "Saving…" : "Save changes"}
              </button>
              <button
                className="btn-secondary"
                onClick={() => render.mutate()}
                disabled={render.isPending || save.isPending}
              >
                {render.isPending ? "Rendering…" : "Regenerate clip"}
              </button>
            </div>
            {(save.isError || render.isError) && (
              <div className="text-sm text-danger">{String(save.error ?? render.error)}</div>
            )}
            <p className="text-xs text-ink-500">
              Each regenerate creates a new render version. Previous successful versions are preserved.
            </p>
          </div>
        </Card>
      </div>
    </div>
  );
}
