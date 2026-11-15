import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api";
import { Card, ErrorState, Field, Spinner, StatusBadge } from "../components/ui";
import { CAPTION_STYLES, LAYOUTS } from "../types";
import type { SettingsData } from "../types";

export default function SettingsPage() {
  const qc = useQueryClient();
  const settings = useQuery({ queryKey: ["settings"], queryFn: api.getSettings });
  const models = useQuery({ queryKey: ["lmstudio-models"], queryFn: api.lmstudioModels });
  const test = useMutation({
    mutationFn: api.lmstudioTest,
    onSuccess: (res) => {
      setTestResult(
        res.connected
          ? `Connected — using model ${res.models[0] ?? "selected"}`
          : `Not reachable: ${res.error}`
      );
      qc.invalidateQueries({ queryKey: ["lmstudio-models"] });
    },
  });

  const [form, setForm] = useState<SettingsData | null>(null);
  const [testResult, setTestResult] = useState<string>("");

  useEffect(() => {
    if (settings.data) setForm(settings.data);
  }, [settings.data]);

  const save = useMutation({
    mutationFn: () => api.updateSettings(form!),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["settings"] }),
  });

  const set = <K extends keyof SettingsData>(key: K, value: SettingsData[K]) =>
    setForm((f) => (f ? { ...f, [key]: value } : f));

  if (settings.isLoading) return <Spinner label="Loading settings…" />;
  if (settings.isError)
    return <ErrorState message={String(settings.error)} onRetry={() => settings.refetch()} />;
  if (!form) return <Spinner />;

  const availableModels = models.data?.models ?? [];

  return (
    <div className="space-y-5">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold text-ink-100">Settings</h1>
          <p className="text-sm text-ink-500">
            All processing runs locally. No transcripts or video data leave this machine.
          </p>
        </div>
        <button
          className="btn-primary"
          disabled={save.isPending}
          onClick={() => save.mutate()}
        >
          {save.isPending ? "Saving…" : "Save settings"}
        </button>
      </div>

      <Card>
        <div className="mb-4 flex items-center justify-between">
          <h2 className="text-sm font-semibold text-ink-100">LM Studio (local LLM)</h2>
          <div className="flex items-center gap-2">
            <StatusBadge status={models.data?.connected ? "connected" : "disconnected"} />
            <button
              className="btn-secondary text-xs"
              onClick={() => test.mutate()}
              disabled={test.isPending}
            >
              {test.isPending ? "Testing…" : "Test connection"}
            </button>
          </div>
        </div>

        {testResult && <div className="mb-3 text-sm text-ink-300">{testResult}</div>}

        {!models.data?.connected && (
          <p className="mb-4 rounded-lg bg-warn/10 p-3 text-xs text-warn">
            LM Studio is not reachable. Clip selection falls back to deterministic local
            heuristics, and generated clips are marked with <code>is_heuristic</code>.
          </p>
        )}

        <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
          <Field label="Base URL">
            <input className="input" value={form.lmstudio.base_url}
                   onChange={(e) => set("lmstudio", { ...form.lmstudio, base_url: e.target.value })} />
          </Field>
          <Field label="Model">
            <input className="input" value={form.lmstudio.model}
                   onChange={(e) => set("lmstudio", { ...form.lmstudio, model: e.target.value })} />
          </Field>
          <Field label="API key (optional)">
            <input className="input" type="password" value={form.lmstudio.api_key ?? ""}
                   onChange={(e) => set("lmstudio", { ...form.lmstudio, api_key: e.target.value })} />
          </Field>
          <div className="grid grid-cols-2 gap-3">
            <Field label="Max tokens">
              <input className="input" type="number" value={form.lmstudio.max_tokens}
                     onChange={(e) => set("lmstudio", { ...form.lmstudio, max_tokens: Number(e.target.value) })} />
            </Field>
            <Field label="Temperature">
              <input className="input" type="number" step="0.1" value={form.lmstudio.temperature}
                     onChange={(e) => set("lmstudio", { ...form.lmstudio, temperature: Number(e.target.value) })} />
            </Field>
          </div>
        </div>

        {availableModels.length > 0 && (
          <div className="mt-3">
            <div className="label">Available models</div>
            <div className="flex flex-wrap gap-1.5">
              {availableModels.map((m) => (
                <span key={m} className="badge bg-ink-700 text-ink-300">{m}</span>
              ))}
            </div>
          </div>
        )}
      </Card>

      <Card>
        <h2 className="mb-4 text-sm font-semibold text-ink-100">Whisper (local transcription)</h2>
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
          <Field label="Model">
            <select className="input" value={form.whisper.model}
                    onChange={(e) => set("whisper", { ...form.whisper, model: e.target.value })}>
              <option value="tiny">tiny</option>
              <option value="base">base</option>
              <option value="small">small</option>
              <option value="medium">medium</option>
              <option value="large-v2">large-v2</option>
              <option value="large-v3">large-v3</option>
            </select>
          </Field>
          <Field label="Language (auto if empty)">
            <input className="input" value={form.whisper.language}
                   onChange={(e) => set("whisper", { ...form.whisper, language: e.target.value })} />
          </Field>
          <Field label="Device">
            <select className="input" value={form.whisper.device}
                    onChange={(e) => set("whisper", { ...form.whisper, device: e.target.value })}>
              <option value="cpu">cpu</option>
              <option value="cuda">cuda</option>
              <option value="auto">auto</option>
            </select>
          </Field>
          <Field label="Compute type">
            <select className="input" value={form.whisper.compute_type}
                    onChange={(e) => set("whisper", { ...form.whisper, compute_type: e.target.value })}>
              <option value="int8">int8</option>
              <option value="int8_float16">int8_float16</option>
              <option value="float16">float16</option>
              <option value="float32">float32</option>
            </select>
          </Field>
        </div>
      </Card>

      <Card>
        <h2 className="mb-4 text-sm font-semibold text-ink-100">Render defaults</h2>
        <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
          <Field label="Width">
            <input className="input" type="number" value={form.render.width}
                   onChange={(e) => set("render", { ...form.render, width: Number(e.target.value) })} />
          </Field>
          <Field label="Height">
            <input className="input" type="number" value={form.render.height}
                   onChange={(e) => set("render", { ...form.render, height: Number(e.target.value) })} />
          </Field>
          <Field label="FPS">
            <input className="input" type="number" value={form.render.fps}
                   onChange={(e) => set("render", { ...form.render, fps: Number(e.target.value) })} />
          </Field>
          <Field label="CRF">
            <input className="input" type="number" value={form.render.crf}
                   onChange={(e) => set("render", { ...form.render, crf: Number(e.target.value) })} />
          </Field>
        </div>
      </Card>

      <Card>
        <h2 className="mb-4 text-sm font-semibold text-ink-100">Processing & branding</h2>
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
          <Field label="Max processing concurrency">
            <input className="input" type="number" value={form.processing.max_processing_concurrency}
                   onChange={(e) => set("processing", { ...form.processing, max_processing_concurrency: Number(e.target.value) })} />
          </Field>
          <Field label="Max render concurrency">
            <input className="input" type="number" value={form.processing.max_render_concurrency}
                   onChange={(e) => set("processing", { ...form.processing, max_render_concurrency: Number(e.target.value) })} />
          </Field>
          <Field label="Default caption style">
            <select className="input" value={form.branding.default_caption_style}
                    onChange={(e) => set("branding", { ...form.branding, default_caption_style: e.target.value })}>
              {CAPTION_STYLES.map((s) => <option key={s} value={s}>{s}</option>)}
            </select>
          </Field>
          <Field label="Default layout">
            <select className="input" value={form.branding.default_layout}
                    onChange={(e) => set("branding", { ...form.branding, default_layout: e.target.value })}>
              {LAYOUTS.map((s) => <option key={s} value={s}>{s}</option>)}
            </select>
          </Field>
          <Field label="Creator handle">
            <input className="input" value={form.branding.creator_handle}
                   onChange={(e) => set("branding", { ...form.branding, creator_handle: e.target.value })} />
          </Field>
          <div className="flex items-end">
            <label className="flex items-center gap-2 text-sm text-ink-300">
              <input
                type="checkbox"
                className="h-4 w-4 accent-emerald-500"
                checked={form.processing.allow_heuristic_fallback}
                onChange={(e) =>
                  set("processing", { ...form.processing, allow_heuristic_fallback: e.target.checked })
                }
              />
              Allow heuristic fallback when LM Studio is unavailable
            </label>
          </div>
        </div>
      </Card>

      {save.isError && <div className="text-sm text-danger">{String(save.error)}</div>}
    </div>
  );
}
