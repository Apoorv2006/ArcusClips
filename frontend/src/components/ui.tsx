import type { ReactNode } from "react";

export function StatusBadge({ status }: { status: string }) {
  const map: Record<string, string> = {
    connected: "bg-accent/20 text-accent",
    completed: "bg-accent/20 text-accent",
    done: "bg-accent/20 text-accent",
    rendered: "bg-accent/20 text-accent",
    processed: "bg-accent/20 text-accent",
    ok: "bg-accent/20 text-accent",
    queued: "bg-ink-600/40 text-ink-300",
    pending: "bg-ink-600/40 text-ink-300",
    running: "bg-warn/20 text-warn",
    scanning: "bg-warn/20 text-warn",
    downloading: "bg-warn/20 text-warn",
    transcribing: "bg-warn/20 text-warn",
    analyzing: "bg-warn/20 text-warn",
    selecting: "bg-warn/20 text-warn",
    rendering: "bg-warn/20 text-warn",
    building: "bg-warn/20 text-warn",
    processing: "bg-warn/20 text-warn",
    importing: "bg-warn/20 text-warn",
    paused: "bg-ink-600/40 text-ink-300",
    discovered: "bg-ink-600/40 text-ink-300",
    imported: "bg-ink-500/30 text-ink-200",
    selected: "bg-ink-500/30 text-ink-200",
    partial: "bg-warn/20 text-warn",
    failed: "bg-danger/20 text-danger",
    error: "bg-danger/20 text-danger",
    cancelled: "bg-danger/20 text-danger",
    disconnected: "bg-danger/20 text-danger",
  };
  return (
    <span className={`badge ${map[status] ?? "bg-ink-600/40 text-ink-300"}`}>
      {status}
    </span>
  );
}

export function ProgressBar({ value, className = "" }: { value: number; className?: string }) {
  return (
    <div className={`progress ${className}`}>
      <span style={{ width: `${Math.round(Math.max(0, Math.min(1, value)) * 100)}%` }} />
    </div>
  );
}

export function Card({ children, className = "" }: { children: ReactNode; className?: string }) {
  return <div className={`card p-5 ${className}`}>{children}</div>;
}

export function StatCard({
  label, value, hint, accent = false,
}: { label: string; value: ReactNode; hint?: string; accent?: boolean }) {
  return (
    <Card className={accent ? "border-accent/30" : ""}>
      <div className="text-xs font-medium uppercase tracking-wide text-ink-400">{label}</div>
      <div className="mt-1.5 text-2xl font-semibold text-ink-100">{value}</div>
      {hint && <div className="mt-0.5 text-xs text-ink-500">{hint}</div>}
    </Card>
  );
}

export function EmptyState({ title, hint, action }: { title: string; hint?: string; action?: ReactNode }) {
  return (
    <div className="card flex flex-col items-center justify-center gap-2 p-10 text-center">
      <div className="text-base font-medium text-ink-300">{title}</div>
      {hint && <div className="text-sm text-ink-500">{hint}</div>}
      {action && <div className="mt-2">{action}</div>}
    </div>
  );
}

export function ErrorState({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div className="card border-danger/30 p-5">
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="text-sm font-medium text-danger">Error</div>
          <div className="mt-1 text-sm text-ink-300 break-all">{message}</div>
        </div>
        {onRetry && (
          <button className="btn-secondary" onClick={onRetry}>
            Retry
          </button>
        )}
      </div>
    </div>
  );
}

export function Modal({
  open, title, onClose, children, wide = false,
}: { open: boolean; title: string; onClose: () => void; children: ReactNode; wide?: boolean }) {
  if (!open) return null;
  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center bg-black/60 p-6"
      onClick={onClose}
    >
      <div
        className={`card mt-16 w-full ${wide ? "max-w-3xl" : "max-w-lg"} p-6`}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="mb-4 flex items-center justify-between">
          <h3 className="text-lg font-semibold text-ink-100">{title}</h3>
          <button className="text-ink-400 hover:text-ink-100" onClick={onClose} aria-label="Close">
            ✕
          </button>
        </div>
        {children}
      </div>
    </div>
  );
}

export function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="block">
      <span className="label">{label}</span>
      {children}
    </label>
  );
}

export function Spinner({ label }: { label?: string }) {
  return (
    <div className="flex items-center gap-2 text-sm text-ink-400">
      <span className="inline-block h-4 w-4 animate-spin rounded-full border-2 border-ink-600 border-t-accent" />
      {label}
    </div>
  );
}
