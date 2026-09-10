import { useEffect, useRef, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { api, type EnvPayload, type JobSummary, type MetricsPayload } from "../lib/api";
import { dispatchPrimaryAction, primaryActionDisabled, primaryActionFromJob, primaryActionHint, primaryActionLabel, showCornerPrimary } from "../lib/pipeline";
import { cn, deleteJobsConfirmMessage, fmtDur, generatePathLabel, jobDeletable, jobNoteText, jobStateLabel, modeLabel } from "../lib/utils";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "../components/ui/alert-dialog";
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
  draft: "试片",
  clips: "成片",
  finish: "拼接成片",
};

export default function JobList() {
  const [jobs, setJobs] = useState<JobSummary[]>([]);
  const [running, setRunning] = useState<string | null>(null);
  const [err, setErr] = useState("");
  const [acting, setActing] = useState(false);
  const nav = useNavigate();
  const location = useLocation();
  const requestedId = (location.state as { selectedJobId?: string } | null)?.selectedJobId;
  const [selectedId, setSelectedId] = useState<string | null>(requestedId ?? null);
  const [runtimeMode, setRuntimeMode] = useState<"mock" | "real">("real");
  const [env, setEnv] = useState<EnvPayload | null>(null);
  const [metrics, setMetrics] = useState<MetricsPayload | null>(null);
  const [checked, setChecked] = useState<string[]>([]);
  const [deleting, setDeleting] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [confirmAbandon, setConfirmAbandon] = useState(false);
  const overviewRef = useRef<HTMLElement>(null);
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

  useEffect(() => {
    let stop = false;
    const tick = async () => {
      try {
        const [settings, nextEnv, nextMetrics] = await Promise.all([api.settings(), api.env(), api.metrics()]);
        if (stop) return;
        setRuntimeMode(settings.mode);
        setEnv(nextEnv);
        setMetrics(nextMetrics);
      } catch {
        /* 监控面板失败不打断列表 */
      }
    };
    tick();
    const id = window.setInterval(tick, 5000);
    return () => {
      stop = true;
      window.clearInterval(id);
    };
  }, []);

  useEffect(() => {
    if (requestedId) setSelectedId(requestedId);
  }, [requestedId]);

  useEffect(() => {
    if (!jobs.length) {
      setSelectedId(null);
      return;
    }
    setSelectedId((current) => {
      if (current && jobs.some((job) => job.id === current)) return current;
      if (running && jobs.some((job) => job.id === running)) return running;
      return jobs[0].id;
    });
  }, [jobs, running]);

  useEffect(() => {
    const allowed = new Set(
      jobs.filter((job) => jobDeletable(job.state, job.id === running)).map((job) => job.id),
    );
    setChecked((current) => {
      const next = current.filter((id) => allowed.has(id));
      return next.length === current.length ? current : next;
    });
  }, [jobs, running]);

  function selectJob(id: string) {
    setSelectedId(id);
    if (window.matchMedia("(max-width: 1023px)").matches) {
      overviewRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
    }
  }

  const active = jobs.find((job) => job.id === selectedId) || null;
  const gpu = metrics?.gpu;
  const isMock = runtimeMode === "mock";
  const occupied = Boolean(running) || acting;
  const activeJob = active ? { ...active, running: occupied } : null;
  const deletableIds = jobs.filter((job) => jobDeletable(job.state, job.id === running)).map((job) => job.id);

  function toggleChecked(id: string) {
    if (!deletableIds.includes(id)) return;
    setChecked((current) => (current.includes(id) ? current.filter((item) => item !== id) : [...current, id]));
  }

  const allSelected = deletableIds.length > 0 && checked.length === deletableIds.length;
  const partialSelected = checked.length > 0 && checked.length < deletableIds.length;
  function toggleSelectAllDeletable() {
    if (allSelected) {
      setChecked([]);
    } else {
      setChecked(deletableIds);
    }
  }

  async function deleteChecked() {
    const ids = checked.filter((id) => deletableIds.includes(id));
    if (!ids.length) return;
    setDeleting(true);
    setErr("");
    let ok = 0;
    const failures: string[] = [];
    for (const id of ids) {
      try {
        await api.remove(id);
        ok += 1;
      } catch (error) {
        failures.push(`${id}: ${(error as Error).message}`);
      }
    }
    setChecked([]);
    await load();
    if (failures.length) setErr(`${ok} 成功 / ${failures.length} 失败。${failures.join("；")}`);
    setDeleting(false);
  }

  async function abandonRunning() {
    if (!running || cancelling) return;
    setConfirmAbandon(true);
  }

  async function runAbandon() {
    if (!running || cancelling) return;
    setCancelling(true);
    setConfirmAbandon(false);
    setErr("");
    try {
      await api.cancel(running);
      await load();
    } catch (error) {
      setErr((error as Error).message);
    } finally {
      setCancelling(false);
    }
  }

  async function runActivePrimary() {
    if (!active) return;
    const current = { ...active, running: occupied };
    if (primaryActionDisabled(current)) return;
    setActing(true);
    setErr("");
    try {
      const detail = (await api.job(active.id)) as {
        next_action?: string;
        state?: string;
        running?: boolean;
        dirty_clip_ids?: string[];
        clips?: { id: string; draft: { status: string } }[];
        stages?: Record<string, { status?: string }>;
      };
      const merged = { ...current, ...detail, running: occupied || Boolean(detail.running) };
      const action = primaryActionFromJob(merged);
      const clipIds = action === "redraft"
        ? detail.dirty_clip_ids || []
        : (detail.clips || []).filter((clip) => clip.draft.status !== "done").map((clip) => clip.id);
      await dispatchPrimaryAction(active.id, merged, clipIds);
      await load();
    } catch (error) {
      setErr((error as Error).message);
    } finally {
      setActing(false);
    }
  }
  return (
    <div className="grid h-full min-h-0 grid-cols-1 overflow-y-auto lg:grid-cols-[minmax(19rem,25rem)_minmax(0,1fr)_minmax(17rem,22rem)] lg:overflow-hidden">
      <aside className="min-h-0 overflow-y-auto border-r border-line bg-panel/50 p-4">
        <div className="mb-5 flex items-start justify-between gap-3">
          <h1 className="text-2xl font-semibold tracking-tight">任务列表</h1>
          <button type="button" onClick={() => setOpen("new")} className="rounded-md bg-tungsten px-3 py-2 text-sm font-medium text-ink hover:opacity-90">
            新建
          </button>
        </div>
        {deletableIds.length ? (
          <div className="mb-4 flex flex-wrap items-center gap-2 rounded-md border border-line bg-surface px-3 py-2 text-xs">
            <button
              type="button"
              aria-checked={allSelected ? "true" : partialSelected ? "mixed" : "false"}
              role="checkbox"
              className="flex items-center gap-2 rounded border border-line px-2 py-1 text-muted hover:border-tungsten/60 hover:text-text disabled:opacity-40"
              disabled={deleting}
              onClick={toggleSelectAllDeletable}
            >
              <span
                aria-hidden="true"
                className={cn(
                  "relative inline-block size-3.5 shrink-0 rounded border bg-surface",
                  allSelected ? "border-tungsten bg-tungsten" : "border-line",
                  partialSelected && "border-tungsten bg-tungsten",
                )}
              >
                {allSelected ? (
                  <span className="absolute inset-0 flex items-center justify-center text-[10px] leading-none text-ink">✓</span>
                ) : partialSelected ? (
                  <span className="absolute inset-x-0 top-1/2 h-[2px] -translate-y-1/2 bg-ink" />
                ) : null}
              </span>
              {allSelected ? "取消全选" : "全选"}
            </button>
            <span className="text-muted">已选 {checked.length} / 共 {deletableIds.length}</span>
            <span className="flex-1" />
            {checked.length ? (
              <button
                type="button"
                className="rounded border border-bad/40 px-2 py-1 text-bad hover:bg-bad/10 disabled:opacity-40"
                disabled={deleting}
                onClick={() => setConfirmDelete(true)}
              >
                {deleting ? "删除中..." : `删除 (${checked.length})`}
              </button>
            ) : null}
          </div>
        ) : null}
        {running ? (
          <div className="mb-4 flex items-center gap-2 rounded-md border border-warn/40 bg-warn/10 p-3">
            <p className="min-w-0 flex-1 text-xs text-warn">已有任务运行中。完成或放弃后才能新建。</p>
            <button
              type="button"
              className="shrink-0 rounded border border-bad/40 px-2 py-1 text-xs text-bad hover:bg-bad/10 disabled:opacity-40"
              disabled={cancelling}
              onClick={() => setConfirmAbandon(true)}
            >
              {cancelling ? "放弃中..." : "放弃"}
            </button>
          </div>
        ) : null}
        {err ? <p className="mb-4 rounded-md border border-bad/40 bg-bad/10 p-3 text-xs text-bad">{err}</p> : null}
        <div className="space-y-2">
          {jobs.map((job) => {
            const deletable = jobDeletable(job.state, job.id === running);
            const isChecked = checked.includes(job.id);
            return (
              <div
                key={job.id}
                className={cn("flex gap-2 rounded-lg border border-line bg-surface p-3 hover:border-tungsten/60", active?.id === job.id && "border-tungsten/70 bg-tungsten/10 dark:border-tungsten/40 dark:bg-tungsten/[0.08] dark:ring-1 dark:ring-tungsten/30 dark:ring-inset")}
              >
                <JobCheckbox
                  checked={isChecked}
                  disabled={!deletable || deleting}
                  title={deletable ? "勾选后可批量删除" : "运行中的任务不能删除"}
                  onChange={() => toggleChecked(job.id)}
                  onClick={(event) => event.stopPropagation()}
                />
                <button type="button" className="min-w-0 flex-1 text-left" onClick={() => selectJob(job.id)}>
                  <div className="flex items-center justify-between gap-2">
                    <span className="truncate font-mono text-xs">{job.id}</span>
                    <span className={cn("rounded px-2 py-0.5 text-[11px]", STATE[job.state?.toLowerCase()] || STATE.pending)}>{jobStateLabel(job.state)}</span>
                  </div>
                  <p className="mt-2 truncate text-xs text-muted">{job.source?.url || job.source?.kind || "未命名输入"}</p>
                  <div className="mt-3 flex items-center justify-between text-xs text-muted">
                    <span>{STAGE[job.stage] || job.stage}</span>
                    <span className="tabular-nums">{fmtDur(job.elapsed_sec)}</span>
                  </div>
                  {jobNoteText(job.note) ? <p className="mt-2 line-clamp-2 text-xs text-muted">{jobNoteText(job.note)}</p> : null}
                </button>
              </div>
            );
          })}
        </div>
      </aside>
      <section ref={overviewRef} className="min-h-0 overflow-y-auto p-6">
        {active ? (
          <div className="mx-auto max-w-3xl">
            <h2 className="text-3xl font-semibold tracking-tight">{STAGE[active.stage] || active.stage}</h2>
            <p className="mt-2 max-w-xl text-sm leading-6 text-muted">{active.note || "选择左侧任务查看详细进度"}</p>
            <div className="mt-8 rounded-xl border border-line bg-surface p-5">
              <div className="flex items-center justify-between text-sm"><span>任务进度</span><span className={cn("rounded-md px-2 py-1 text-xs font-medium", active.state === "done" ? "bg-ok/20 text-ok" : "bg-panel text-tungsten")}>{active.state === "done" ? "已完成" : (active.sub_progress ? `${STAGE[active.stage] || active.stage} · ${active.sub_progress}` : (STAGE[active.stage] || active.stage))}</span></div>
              <div className="mt-5 flex flex-wrap gap-2 text-xs text-muted"><span className="rounded border border-line px-2 py-1">{active.source?.kind === "url" ? "URL 输入" : "本地输入"}</span><span className="rounded border border-line px-2 py-1">{generatePathLabel(String(active.options?.generate_path || "t2va"))}</span><span className="rounded border border-tungsten/40 bg-tungsten/10 px-2 py-1 text-tungsten">{modeLabel(active.mode)}</span></div>
            </div>
            <div className="mt-8 grid gap-3 sm:grid-cols-2">
              <button type="button" className="rounded-lg border border-line bg-surface p-4 text-left hover:border-tungsten/50" onClick={() => nav(`/jobs/${active.id}`)}>
                <span className="text-sm font-medium">打开任务详情</span>
                <span className="mt-1 block text-xs text-muted">查看片段、脚本、预览和日志</span>
              </button>
              {activeJob && showCornerPrimary(activeJob) ? (
                <button
                  type="button"
                  disabled={primaryActionDisabled(activeJob)}
                  className="rounded-lg border border-line bg-surface p-4 text-left hover:border-tungsten/50 disabled:cursor-not-allowed disabled:opacity-50"
                  onClick={() => void runActivePrimary()}
                >
                  <span className="text-sm font-medium">{acting ? "处理中..." : primaryActionLabel(activeJob)}</span>
                  <span className="mt-1 block text-xs text-muted">{primaryActionHint(activeJob)}</span>
                </button>
              ) : null}
            </div>
          </div>
        ) : jobs.length === 0 ? (
          <div className="mx-auto flex max-w-xl flex-col items-center pt-16 text-center">
            <h2 className="text-2xl font-semibold tracking-tight">开始第一个任务</h2>
            <p className="mt-3 max-w-md text-sm leading-6 text-muted">粘贴视频链接或选择本地文件，几分钟后得到分镜、脚本和试片。</p>
            <button
              type="button"
              onClick={() => setOpen("new")}
              className="mt-6 rounded-md bg-tungsten px-4 py-2 text-sm font-medium text-ink hover:opacity-90"
            >
              新建任务
            </button>
          </div>
        ) : <div className="flex h-full items-center justify-center text-muted">从左侧选择一个任务</div>}
      </section>
      <aside className="min-h-0 overflow-y-auto border-l border-line bg-panel/50 p-4">
        <p className="text-xs uppercase tracking-[0.18em] text-muted">运行环境</p>
        <h2 className="mt-1 text-lg font-semibold">运行监控</h2>
        <div className="mt-5 space-y-3 text-sm">
          <div className="flex items-center justify-between border-b border-line pb-3">
            <span className="text-muted">执行模式</span>
            <span className="text-tungsten">{modeLabel(runtimeMode)}</span>
          </div>
          <div className="flex items-center justify-between border-b border-line pb-3">
            <span className="text-muted">素材</span>
            <span>{isMock ? "内置 Fixture" : "URL / 本地视频"}</span>
          </div>
          <div className="flex items-center justify-between border-b border-line pb-3">
            <span className="text-muted">模型调用</span>
            <span className={isMock ? "text-ok" : "text-tungsten"}>{isMock ? "已模拟" : "真实模型"}</span>
          </div>
          <div className="flex items-center justify-between">
            <span className="text-muted">GPU</span>
            <span className="text-muted">
              {isMock ? "无需" : gpu ? `${gpu.name.replace("NVIDIA GeForce ", "")} · ${Math.round(gpu.memory_used_mb)}/${Math.round(gpu.memory_total_mb)} MB` : "未检测到"}
            </span>
          </div>
        </div>
        <div className="mt-8 rounded-lg border border-line bg-surface p-4">
          <p className="text-xs font-medium">快速说明</p>
          <p className="mt-2 text-xs leading-5 text-muted">
            {isMock
              ? "Mock 会按真实阶段推进，不访问外部模型。进入设置可切换速度与故障场景。"
              : env?.summary || "真实模式会调用本机 ASR、VL、LLM 与 ComfyUI。环境未就绪时可在设置中检查依赖。"}
          </p>
        </div>
      </aside>

      <AlertDialog open={confirmDelete} onOpenChange={(open) => { if (!open) setConfirmDelete(false); }}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>删除 {checked.length} 个任务？</AlertDialogTitle>
            <AlertDialogDescription>{deleteJobsConfirmMessage(checked.length)}</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={deleting}>取消</AlertDialogCancel>
            <AlertDialogAction
              className="bg-bad text-ink hover:opacity-90"
              disabled={deleting}
              onClick={(event) => { event.preventDefault(); void deleteChecked().then(() => setConfirmDelete(false)); }}
            >
              {deleting ? "删除中..." : "删除"}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <AlertDialog open={confirmAbandon} onOpenChange={(open) => { if (!open) setConfirmAbandon(false); }}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>放弃运行中的任务？</AlertDialogTitle>
            <AlertDialogDescription>打断任务并放弃。已有产物会保留。</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={cancelling}>取消</AlertDialogCancel>
            <AlertDialogAction
              className="bg-bad text-ink hover:opacity-90"
              disabled={cancelling}
              onClick={(event) => { event.preventDefault(); void runAbandon(); }}
            >
              {cancelling ? "放弃中..." : "放弃"}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}

function JobCheckbox({
  checked,
  disabled,
  title,
  onChange,
  onClick,
}: {
  checked: boolean;
  disabled: boolean;
  title: string;
  onChange: () => void;
  onClick: (event: React.MouseEvent<HTMLInputElement>) => void;
}) {
  return (
    <input
      type="checkbox"
      className="mt-1 size-4 shrink-0 accent-tungsten disabled:opacity-30"
      checked={checked}
      disabled={disabled}
      title={title}
      onChange={onChange}
      onClick={onClick}
    />
  );
}
