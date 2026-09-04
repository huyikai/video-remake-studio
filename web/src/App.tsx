import { NavLink, Outlet, Route, Routes } from "react-router-dom";
import StatusBar from "./components/StatusBar";
import JobList from "./pages/JobList";
import JobDetail from "./pages/JobDetail";
import OpenDialog from "./pages/OpenDialog";
import { useUi } from "./ui";

function Shell() {
  const { setOpen } = useUi();
  return (
    <div className="flex h-full flex-col bg-bg text-text">
      <header className="flex min-h-16 items-center justify-between border-b border-line bg-surface/80 px-5 backdrop-blur">
        <NavLink to="/" className="font-semibold tracking-wide">
          Video Remake Studio
        </NavLink>
        <nav className="flex items-center gap-1 text-sm text-muted">
          <NavLink to="/" className={({ isActive }) => `rounded px-3 py-2 ${isActive ? "bg-panel text-text" : "hover:bg-panel hover:text-text"}`}>
            任务列表
          </NavLink>
          <button type="button" className="rounded px-3 py-2 hover:bg-panel hover:text-text" onClick={() => setOpen("new")}>
            新建任务
          </button>
          <button type="button" className="rounded px-3 py-2 hover:bg-panel hover:text-text" onClick={() => setOpen("settings")}>
            设置
          </button>
        </nav>
      </header>
      <StatusBar />
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
