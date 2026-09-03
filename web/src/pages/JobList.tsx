import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api, type JobSummary } from "../lib/api";
import { cn, fmtDur } from "../lib/utils";
import { useUi } from "../ui";

const STATE: Record<string, string> = {
  running: "bg-tungsten/20 text-tungsten",
  paused: "bg-warn/15 text-warn",
  failed: "bg-bad/20 text-bad",
  cancelled: "bg-muted/20 text-muted",
  done: "bg-ok/20 text-ok",
  pending: "bg-line text-muted",
};

const STAGE: Record<string, string> = {
  download: "下载",
  pagemeta: "页面",
  understand: "理解",
  script: "脚本",
  precheck: "预检",
  generate: "生成",
  finish: "交付",
};

export default function JobList() {
  const [jobs, setJobs] = useState<JobSummary[]>([]);
  const [running, setRunning] = useState<string | null>(null);
  const [err, setErr] = useState("");
  const nav = useNavigate();
  const { setOpen } = useUi();

  const load = () =>
    api
      .jobs()
      .then((d) => {
        setJobs(d.jobs);
        setRunning(d.running_job_id);
      })
      .catch((e: Error) => setErr(e.message));

  useEffect(() => {
    load();
    const id = window.setInterval(load, 3000);
    return () => window.clearInterval(id);
  }, []);

  return (
    <div className="h-full overflow-y-auto p-6">
      <div className="mb-5 flex items-center justify-between">
        <h1 className="text-xl font-semibold">任务</h1>
        <button
          type="button"
          onClick={() => setOpen("new")}
          className={cn(
            "rounded-md bg-tungsten px-3 py-1.5 text-sm font-medium text-ink",
            running && "opacity-40",
          )}
        >
          新建任务
        </button>
      </div>
      {running ? <p className="mb-3 text-sm text-warn">正在跑 {running}，新建会等它结束或先放弃。</p> : null}
      {err ? <p className="text-bad">{err}</p> : null}
      <div className="grid gap-3">
        {jobs.map((job) => (
          <article key={job.id} className="flex items-stretch overflow-hidden rounded-lg border border-line bg-panel">
            <div
              className={cn(
                "w-1.5",
                job.state === "running" && "bg-tungsten",
                job.state === "done" && "bg-ok",
                job.state === "failed" && "bg-bad",
                job.state === "paused" && "bg-warn",
                job.state === "cancelled" && "bg-muted",
              )}
            />
            <div className="flex flex-1 flex-wrap items-center gap-3 p-4">
              <div className="min-w-0 flex-1">
                <Link to={`/jobs/${job.id}`} className="font-mono text-sm hover:text-tungsten">
                  {job.id}
                </Link>
                <p className="truncate text-xs text-muted">{job.source?.url || job.source?.kind || ""}</p>
                <p className="mt-1 line-clamp-2 text-xs text-muted">{job.note}</p>
              </div>
              <span className={cn("rounded px-2 py-0.5 text-xs", STATE[job.state] || STATE.pending)}>
                {job.state}
              </span>
              <span className="text-sm text-muted">{STAGE[job.stage] || job.stage}</span>
              <span className="text-sm tabular-nums text-muted">{fmtDur(job.elapsed_sec)}</span>
              <div className="flex gap-2">
                <button className="text-sm text-tungsten" onClick={() => nav(`/jobs/${job.id}`)}>
                  打开
                </button>
                <button
                  className="text-sm text-muted hover:text-text"
                  disabled={Boolean(running) && running !== job.id}
                  onClick={() => api.resume(job.id).then(load).catch((e: Error) => setErr(e.message))}
                >
                  恢复
                </button>
                <button
                  className="text-sm text-bad"
                  onClick={() => {
                    if (!confirm("删除整个任务目录（含拷贝的原片）？")) return;
                    api.remove(job.id).then(load).catch((e: Error) => setErr(e.message));
                  }}
                >
                  删除
                </button>
              </div>
            </div>
          </article>
        ))}
        {!jobs.length ? <p className="text-muted">还没有任务。</p> : null}
      </div>
    </div>
  );
}
