import { NavLink, Outlet, Route, Routes } from "react-router-dom";
import { cn } from "./lib/utils";
import StatusBar from "./components/StatusBar";
import JobList from "./pages/JobList";
import JobDetail from "./pages/JobDetail";
import OpenDialog from "./pages/OpenDialog";
import { useUi } from "./ui";

function Shell() {
  const { setOpen } = useUi();
  return (
    <div className="flex h-full flex-col bg-bg text-text">
      <header className="grid h-12 shrink-0 grid-cols-[1fr_auto_1fr] items-center gap-3 border-b border-line bg-surface/80 px-5">
        <NavLink
          to="/"
          end
          className={({ isActive }) =>
            cn(
              "justify-self-start rounded px-3 py-1.5 text-sm font-semibold tracking-wide hover:bg-panel",
              isActive ? "text-tungsten" : "text-text",
            )
          }
        >
          Video Remake Studio
        </NavLink>
        <StatusBar />
        <button type="button" className="justify-self-end rounded px-3 py-2 text-sm text-muted hover:bg-panel hover:text-text" onClick={() => setOpen("settings")}>
          设置
        </button>
      </header>
      <main className="min-h-0 flex-1 overflow-hidden">
        <Outlet />
      </main>
    </div>
  );
}

export default function App() {
  return (
    <Routes>
      <Route element={<Shell />}>
        <Route path="/" element={<JobList />} />
        <Route path="/new" element={<OpenDialog kind="new" />} />
        <Route path="/jobs/:id" element={<JobDetail />} />
        <Route path="/settings" element={<OpenDialog kind="settings" />} />
      </Route>
    </Routes>
  );
}
