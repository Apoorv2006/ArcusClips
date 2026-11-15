import { NavLink, Route, Routes, useNavigate } from "react-router-dom";
import { useAuth } from "./auth";
import { Spinner } from "./components/ui";
import LoginPage from "./pages/LoginPage";
import DashboardPage from "./pages/DashboardPage";
import SourcesPage from "./pages/SourcesPage";
import DriveStudioPage from "./pages/DriveStudioPage";
import VideosPage from "./pages/VideosPage";
import ClipsPage from "./pages/ClipsPage";
import BatchStudioPage from "./pages/BatchStudioPage";
import ClipEditorPage from "./pages/ClipEditorPage";
import ExportsPage from "./pages/ExportsPage";
import SettingsPage from "./pages/SettingsPage";

const NAV = [
  { to: "/", label: "Dashboard", icon: "◎" },
  { to: "/sources", label: "Sources", icon: "🔗" },
  { to: "/drive", label: "Drive Studio", icon: "🗂" },
  { to: "/videos", label: "Videos", icon: "🎞" },
  { to: "/clips", label: "Clips", icon: "✂" },
  { to: "/batches", label: "Batch Studio", icon: "⚙" },
  { to: "/exports", label: "Exports", icon: "📦" },
  { to: "/settings", label: "Settings", icon: "🔧" },
];

function Shell({ children }: { children: React.ReactNode }) {
  const { user, logout } = useAuth();
  const navigate = useNavigate();
  return (
    <div className="flex h-full">
      <aside className="flex w-56 shrink-0 flex-col border-r border-ink-700 bg-ink-900">
        <div className="flex items-center gap-2 px-5 py-5">
          <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-accent-deep text-sm font-bold text-ink-950">
            A
          </div>
          <div>
            <div className="text-sm font-semibold text-ink-100">ArcusClips</div>
            <div className="text-[10px] uppercase tracking-widest text-ink-500">Local AI Clipping</div>
          </div>
        </div>
        <nav className="flex-1 space-y-1 px-3">
          {NAV.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.to === "/"}
              className={({ isActive }) =>
                `flex items-center gap-3 rounded-lg px-3 py-2 text-sm transition-colors ${
                  isActive
                    ? "bg-ink-800 text-ink-100"
                    : "text-ink-400 hover:bg-ink-850 hover:text-ink-200"
                }`
              }
            >
              <span className="text-xs">{item.icon}</span>
              {item.label}
            </NavLink>
          ))}
        </nav>
        <div className="border-t border-ink-700 p-3">
          <div className="px-2 pb-2 text-xs text-ink-500">
            {user ? `Signed in as ${user.username}` : ""}
          </div>
          <button
            className="btn-secondary w-full"
            onClick={() => {
              logout();
              navigate("/login");
            }}
          >
            Sign out
          </button>
        </div>
      </aside>
      <main className="flex-1 overflow-y-auto">
        <div className="mx-auto max-w-7xl p-6">{children}</div>
      </main>
    </div>
  );
}

export default function App() {
  const { user, loading } = useAuth();
  if (loading) {
    return (
      <div className="flex h-full items-center justify-center">
        <Spinner label="Loading ArcusClips…" />
      </div>
    );
  }
  if (!user) {
    return (
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route path="*" element={<LoginPage />} />
      </Routes>
    );
  }
  return (
    <Shell>
      <Routes>
        <Route path="/" element={<DashboardPage />} />
        <Route path="/login" element={<DashboardPage />} />
        <Route path="/sources" element={<SourcesPage />} />
        <Route path="/drive" element={<DriveStudioPage />} />
        <Route path="/videos" element={<VideosPage />} />
        <Route path="/clips" element={<ClipsPage />} />
        <Route path="/clips/:clipId" element={<ClipEditorPage />} />
        <Route path="/batches" element={<BatchStudioPage />} />
        <Route path="/exports" element={<ExportsPage />} />
        <Route path="/settings" element={<SettingsPage />} />
      </Routes>
    </Shell>
  );
}
