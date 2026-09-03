import { NavLink, Outlet, Route, Routes } from "react-router-dom";
import StatusBar from "./components/StatusBar";
import JobList from "./pages/JobList";
import JobDetail from "./pages/JobDetail";
import OpenDialog from "./pages/OpenDialog";
import { useUi } from "./ui";

function Shell() {
  const { setOpen } = useUi();
  return (
    <div className="flex h-full flex-col bg-ink text-text">
      <header className="flex items-center justify-between border-b border-line px-5 py-2">
        <NavLink to="/" className="font-semibold tracking-wide text-tungsten">
          Video Remake Studio
        </NavLink>
        <nav className="flex gap-5 text-sm text-muted">
          <NavLink to="/" className={({ isActive }) => (isActive ? "text-text" : "hover:text-text")}>
            任务
          </NavLink>
          <button type="button" className="hover:text-text" onClick={() => setOpen("new")}>
            新建
          </button>
          <button type="button" className="hover:text-text" onClick={() => setOpen("settings")}>
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
