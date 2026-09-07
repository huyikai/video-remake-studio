import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { useDefaultLayout } from "react-resizable-panels";
import Dialog from "../components/Dialog";
import { ResizableHandle, ResizablePanel, ResizablePanelGroup } from "../components/ui/resizable";
import { api } from "../lib/api";
import { dispatchPrimaryAction, primaryActionDisabled, primaryActionLabel } from "../lib/pipeline";
import { cn, deleteJobsConfirmMessage, fileUrl, fmtDur, fmtTime, generatePathLabel, jobDeletable, jobStateLabel, modeLabel, stageStatusLabel } from "../lib/utils";

const STAGES = ["download", "pagemeta", "understand", "script", "precheck", "generate", "finish"] as const;
type StageId = (typeof STAGES)[number];

const STAGE_LABEL: Record<string, string> = {
  download: "下载",
  pagemeta: "页面信息",
  understand: "视频理解",
  script: "脚本生成",
  precheck: "预检",
  generate: "视频生成",
  finish: "交付成片",
};

type StageRecord = { status: string; error?: string | null };
type MediaRecord = { status: string; dirty: boolean; file?: string | null };
type ClipRow = {
  id: string;
  t0: number;
  t1: number;
  h3_seconds: number;
  padded?: boolean;
  has_script?: boolean;
  draft: MediaRecord;
  final: MediaRecord;
};
type SpeechLine = { t0: number; t1: number; text: string };
type JobDetailData = {
  id: string;
  mode?: "mock" | "real";
  state: string;
  stage: string;
  note?: string;
  running?: boolean;
  generate_path?: string;
  need_aspect_confirm?: boolean;
  source_aspect?: string;
  source?: { kind?: string; url?: string | null; original_path?: string | null; probe?: { duration?: number; width?: number; height?: number } };
  options?: { aspect_ratio?: string; review_mode?: string; generate_path?: string };
  stages?: Record<string, StageRecord>;
  clips?: ClipRow[];
  dirty_clip_ids?: string[];
  next_action?: string;
  media?: { source?: string | null; draft?: string | null; final?: string | null; cover?: string | null; ass?: string | null };
  events?: { at: string; kind: string; clip_id?: string; detail?: string; quality?: string }[];
  precheck?: { ok?: boolean; errors?: string[]; warnings?: string[] };
};

type PreviewKind = "source" | "draft" | "final";
type BatchKind = "scripts" | "ai" | "videos" | null;
type ClipBiz = "ready" | "pending" | "generating" | "failed";
type DetailKind = "understand" | "precheck" | "progress" | null;

function stageTone(status?: string) {
  const key = (status || "").toLowerCase();
  if (key === "done" || key === "skipped") return "text-ok";
  if (key === "running") return "text-tungsten";
  if (key === "failed" || key === "error") return "text-bad";
  if (key === "waiting" || key === "dirty" || key === "paused" || key === "pause") return "text-warn";
  return "text-muted";
}

function clipBiz(clip: ClipRow, dirty: string[]): ClipBiz {
  if (clip.draft.status === "running" || clip.final.status === "running") return "generating";
  if (clip.draft.status === "error" || clip.final.status === "error") return "failed";
  if (dirty.includes(clip.id) || clip.has_script === false) return "pending";
  if (clip.draft.status === "done" || clip.final.status === "done") return "ready";
  return "pending";
}

function clipBizLabel(status: ClipBiz) {
  if (status === "ready") return "已生成";
  if (status === "generating") return "生成中";
  if (status === "failed") return "失败";
  return "待生成";
}

function currentProgressStage(job: JobDetailData): StageId {
  if ((job.dirty_clip_ids || []).length) return "script";
  const running = STAGES.find((stage) => job.stages?.[stage]?.status === "running");
  if (running) return running;
  const waiting = STAGES.find((stage) => job.stages?.[stage]?.status === "waiting" || job.stages?.[stage]?.status === "failed");
  if (waiting) return waiting;
  if (STAGES.includes(job.stage as StageId)) return job.stage as StageId;
  return "download";
}

function stageDisplayStatus(job: JobDetailData, stage: StageId) {
  if ((job.dirty_clip_ids || []).length && ["script", "precheck", "generate", "finish"].includes(stage)) {
    if (stage === "script") return "dirty";
    return "pending";
  }
  return job.stages?.[stage]?.status || "pending";
}

async function fetchJobJson(jobId: string, rel: string): Promise<Record<string, any> | null> {
  const res = await fetch(`/api/jobs/${jobId}/files/${rel}`);
  if (!res.ok) return null;
  try {
    return await res.json();
  } catch {
    return null;
  }
}

function DetailSkeleton() {
  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="h-28 shrink-0 border-b border-line bg-surface" />
      <div className="grid min-h-0 flex-1 grid-cols-[16rem_minmax(0,1fr)]">
        <div className="border-r border-line bg-panel/50" />
        <div className="p-6"><div className="h-full animate-pulse rounded-xl bg-panel" /></div>
      </div>
    </div>
  );
}

export default function JobDetail() {
  const { id = "" } = useParams();
  const nav = useNavigate();
  const [job, setJob] = useState<JobDetailData | null>(null);
  const [loading, setLoading] = useState(true);
  const [clipId, setClipId] = useState<string | null>(null);
  const [viewStage, setViewStage] = useState<StageId | null>(null);
  const [editing, setEditing] = useState(false);
  const [navOpen, setNavOpen] = useState(false);
  const [batchOpen, setBatchOpen] = useState(false);
  const [batchKind, setBatchKind] = useState<BatchKind>(null);
  const [videoTarget, setVideoTarget] = useState<PreviewKind>("draft");
  const [picked, setPicked] = useState<string[]>([]);
  const [showOthers, setShowOthers] = useState(false);
  const [preview, setPreview] = useState<PreviewKind>("source");
  const [txt, setTxt] = useState("");
  const [md, setMd] = useState("");
  const [seconds, setSeconds] = useState("");
  const [speech, setSpeech] = useState<SpeechLine[]>([]);
  const [savedTxt, setSavedTxt] = useState("");
  const [aiOpen, setAiOpen] = useState(false);
  const [aiPrompt, setAiPrompt] = useState("");
  const [aiScope, setAiScope] = useState<string[]>([]);
  const [err, setErr] = useState("");
  const [msg, setMsg] = useState("");
  const [connection, setConnection] = useState<"connecting" | "live" | "offline">("connecting");
  const [reconnectKey, setReconnectKey] = useState(0);
  const [busyAction, setBusyAction] = useState("");
  const [confirmRisk, setConfirmRisk] = useState(false);
  const [detail, setDetail] = useState<DetailKind>(null);
  const [understand, setUnderstand] = useState<{ shots: number; events: number; speech: number; windows: number; duration?: number; raw: string } | null>(null);
  const batchRef = useRef<HTMLDivElement>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const data = (await api.job(id)) as JobDetailData;
      setJob(data);
      setClipId((current) => (current && data.clips?.some((clip) => clip.id === current) ? current : null));
      setErr("");
    } catch (error) {
      setErr((error as Error).message);
    } finally {
      setLoading(false);
    }
  }, [id]);

  useEffect(() => {
    setViewStage(null);
    setClipId(null);
    setEditing(false);
    void refresh();
  }, [id, refresh]);

  useEffect(() => {
    if (!id) return;
    setConnection("connecting");
    const source = new EventSource(`/api/jobs/${id}/events`);
    source.onopen = () => setConnection("live");
    source.onmessage = (event) => {
      try {
        setJob(JSON.parse(event.data) as JobDetailData);
        setConnection("live");
      } catch {
        setConnection("offline");
      }
    };
    source.onerror = () => setConnection("offline");
    return () => source.close();
  }, [id, reconnectKey]);

  useEffect(() => {
    if (!id || !clipId) return;
    api.clip(id, clipId).then((raw) => {
      const data = raw as { prompt_txt: string; review_md: string; clip: { h3_seconds: number }; speech?: SpeechLine[] };
      setTxt(data.prompt_txt || "");
      setSavedTxt(data.prompt_txt || "");
      setMd(data.review_md || "");
      setSeconds(String(data.clip?.h3_seconds ?? ""));
      setSpeech(data.speech || []);
    }).catch((error: Error) => setErr(error.message));
  }, [id, clipId]);

  useEffect(() => {
    if (!batchOpen) return;
    const onDown = (event: MouseEvent) => {
      if (!batchRef.current?.contains(event.target as Node)) setBatchOpen(false);
    };
    window.addEventListener("mousedown", onDown);
    return () => window.removeEventListener("mousedown", onDown);
  }, [batchOpen]);

  useEffect(() => {
    if (!id || (viewStage ?? job?.stage) !== "understand") return;
    let cancelled = false;
    Promise.all([
      fetchJobJson(id, "understanding.json"),
      fetchJobJson(id, "shots.json"),
      fetchJobJson(id, "events.json"),
      fetchJobJson(id, "dialogue.json"),
      fetchJobJson(id, "beats.json"),
    ]).then(([summary, shots, events, dialogue, beats]) => {
      if (cancelled) return;
      const shotCount = Array.isArray(shots?.shots) ? shots.shots.length : Number(summary?.shots || 0);
      const eventCount = Array.isArray(events?.events) ? events.events.length : Number(summary?.events || 0);
      const speechCount = Array.isArray(dialogue?.speech)
        ? dialogue.speech.length
        : Array.isArray(dialogue?.segments)
          ? dialogue.segments.length
          : Number(summary?.speech_segments || 0);
      const windowCount = Array.isArray(beats?.windows) ? beats.windows.length : Number(summary?.windows || 0);
      setUnderstand({
        shots: shotCount,
        events: eventCount,
        speech: speechCount,
        windows: windowCount,
        duration: summary?.duration,
        raw: JSON.stringify({ understanding: summary, shots, events, dialogue, beats }, null, 2),
      });
    }).catch(() => {
      if (!cancelled) setUnderstand(null);
    });
    return () => {
      cancelled = true;
    };
  }, [id, job?.stage, viewStage]);

  const dirty = job?.dirty_clip_ids || [];
  const clips = job?.clips || [];
  const current = clips.find((clip) => clip.id === clipId) || null;
  const progressStage = job ? currentProgressStage(job) : "download";
  const shownStage = viewStage ?? progressStage;
  const unsaved = Boolean(clipId) && txt !== savedTxt;
  const sourceReady = Boolean(job?.media?.source);
  const pendingClips = clips.filter((clip) => clipBiz(clip, dirty) === "pending");
  const failedClips = clips.filter((clip) => clipBiz(clip, dirty) === "failed");
  const missingScripts = clips.filter((clip) => clip.has_script === false);
  const understandDone = ["done", "skipped"].includes(job?.stages?.understand?.status || "");
  const precheckDone = job?.stages?.precheck?.status === "done";
  const canBatchScripts = understandDone && missingScripts.length > 0;
  const canBatchAi = clips.some((clip) => clip.has_script);
  const canBatchVideos = precheckDone;
  const showBatch = canBatchScripts || canBatchAi || canBatchVideos;
  const generateLocked = Boolean(job?.running) || busyAction === "primary" || busyAction === "batch";

  useEffect(() => {
    if (!showBatch) setBatchOpen(false);
  }, [showBatch]);

  const previewRel = useMemo(() => {
    if (!job) return null;
    if (current && preview !== "source") return current[preview]?.file || job.media?.[preview] || null;
    return job.media?.[preview] || (preview === "source" ? job.media?.source : null);
  }, [current, job, preview]);
  const mediaSrc = job && previewRel ? fileUrl(job.id, previewRel) : undefined;

  function recommendedIds(kind: Exclude<BatchKind, null>, target = videoTarget) {
    if (kind === "scripts") return missingScripts.map((clip) => clip.id);
    if (kind === "ai") return (pendingClips.length ? pendingClips : clips.filter((clip) => clip.has_script)).map((clip) => clip.id);
    if (target === "final") return clips.filter((clip) => clip.draft.status === "done" && (dirty.includes(clip.id) || clip.final.status !== "done")).map((clip) => clip.id);
    return [...pendingClips, ...failedClips].map((clip) => clip.id);
  }

  function openBatch(kind: Exclude<BatchKind, null>) {
    setBatchOpen(false);
    setBatchKind(kind);
    setConfirmRisk(false);
    const rec = recommendedIds(kind);
    setPicked(rec);
    setShowOthers(!rec.length);
  }

  function selectStage(stage: StageId) {
    setViewStage(stage);
    setNavOpen(false);
    setEditing(stage === "script" && Boolean(clipId));
  }

  function selectClip(nextId: string) {
    const clip = clips.find((item) => item.id === nextId);
    if (!clip) return;
    setClipId(nextId);
    setNavOpen(false);
    const status = clipBiz(clip, dirty);
    if (status === "pending" || status === "failed") {
      setViewStage("script");
      setEditing(true);
      return;
    }
    setViewStage("generate");
    setEditing(false);
    setPreview(clip.final.status === "done" ? "final" : clip.draft.status === "done" ? "draft" : "source");
  }

  function editScript() {
    setViewStage("script");
    setEditing(true);
  }

  async function save() {
    if (!clipId) return;
    setErr("");
    setMsg("");
    try {
      await api.saveClip(id, clipId, { prompt_txt: txt, review_md: md, h3_seconds: Number(seconds) });
      setSavedTxt(txt);
      setMsg("脚本已保存，该片段待生成");
      await refresh();
    } catch (error) {
      setErr((error as Error).message);
    }
  }

  async function runBatch() {
    setBusyAction("batch");
    setErr("");
    try {
      if (batchKind === "ai") {
        setAiScope(picked);
        setBatchKind(null);
        setAiOpen(true);
        return;
      }
      if (batchKind === "videos") {
        if (videoTarget === "final") await api.finals(id);
        else await api.draft(id, picked);
      } else {
        await api.resume(id);
      }
      setBatchKind(null);
      setConfirmRisk(false);
      await refresh();
    } catch (error) {
      setErr((error as Error).message);
    } finally {
      setBusyAction("");
    }
  }

  async function runPrimary() {
    if (!job) return;
    setBusyAction("primary");
    setErr("");
    try {
      const clipIds = (job.dirty_clip_ids || []).length
        ? job.dirty_clip_ids || []
        : clips.filter((clip) => clip.draft.status !== "done").map((clip) => clip.id);
      await dispatchPrimaryAction(id, job, clipIds);
      await refresh();
    } catch (error) {
      setErr((error as Error).message);
    } finally {
      setBusyAction("");
    }
  }

  if (loading && !job) return <DetailSkeleton />;
  if (!job) {
    return (
      <div className="flex h-full items-center justify-center p-6">
        <div className="max-w-md rounded-xl border border-bad/40 bg-bad/10 p-5">
          <p className="font-medium text-bad">任务详情加载失败</p>
          <p className="mt-2 text-sm text-muted">{err || "找不到这个任务"}</p>
          <button type="button" className="mt-4 rounded-md bg-tungsten px-3 py-2 text-sm text-ink" onClick={() => void refresh()}>重新连接</button>
        </div>
      </div>
    );
  }

  const riskyPicked = batchKind === "videos" && videoTarget === "final"
    ? picked.filter((clipIdValue) => clips.find((clip) => clip.id === clipIdValue)?.draft.status !== "done")
    : [];
  const sourceSrc = fileUrl(job.id, job.media?.source);
  const finalSrc = fileUrl(job.id, job.media?.final);
  const coverSrc = fileUrl(job.id, job.media?.cover);
  const primaryDisabled = Boolean(busyAction) || primaryActionDisabled(job);

  return (
    <div className="flex h-full min-h-0 flex-col overflow-hidden bg-bg">
      {navOpen ? <button type="button" className="fixed inset-0 z-20 bg-black/40 lg:hidden" aria-label="关闭片段列表" onClick={() => setNavOpen(false)} /> : null}

      <header className="shrink-0 border-b border-line bg-surface">
        <div className="flex min-h-11 items-center gap-3 px-4 py-1.5 sm:px-6">
          <button type="button" className="rounded-md border border-line px-2.5 py-1 text-sm text-muted hover:border-tungsten/60 hover:text-text" onClick={() => nav("/", { state: { selectedJobId: job.id } })}>返回</button>
          <button type="button" className="rounded-md border border-line px-2.5 py-1 text-sm text-muted lg:hidden" onClick={() => setNavOpen(true)}>片段</button>
          <div className="min-w-0 flex-1">
            <div className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1">
              <h1 className="font-semibold tracking-tight">任务详情</h1>
              <span className="truncate font-mono text-xs text-muted">{job.id}</span>
              <span className="rounded border border-tungsten/50 bg-tungsten/10 px-2 py-0.5 text-[11px] text-tungsten">{modeLabel(job.mode)}</span>
              <span className="truncate text-xs text-muted">{job.source?.kind === "url" ? "URL 输入" : "本地输入"} · {generatePathLabel(job.generate_path || job.options?.generate_path)} · {job.options?.aspect_ratio || "16:9"}</span>
            </div>
          </div>
          <div className="flex shrink-0 items-center gap-3">
            <div className="flex items-center gap-2 text-xs">
              <span className={cn("font-medium", stageTone(job.state))}>{jobStateLabel(job.state)}</span>
              {dirty.length ? <span className="text-warn">未应用 {dirty.length}</span> : null}
              {job.running ? <button type="button" className="text-tungsten hover:underline" onClick={() => setDetail("progress")}>处理中</button> : null}
              <span className={connection === "live" ? "text-ok" : connection === "offline" ? "text-bad" : "text-warn"}>{connection === "live" ? "已连接" : connection === "offline" ? "连接中断" : "连接中"}</span>
              {connection === "offline" ? <button type="button" className="text-tungsten hover:underline" onClick={() => setReconnectKey((value) => value + 1)}>重连</button> : null}
            </div>
          </div>
        </div>
        <div className="overflow-x-auto border-t border-line px-4 py-2.5 sm:px-6">
          <div className="flex min-w-max items-center gap-2">
            {STAGES.map((stage) => {
              const status = stageDisplayStatus(job, stage);
              const isCurrent = progressStage === stage;
              const isViewing = shownStage === stage;
              return (
                <button key={stage} type="button" onClick={() => selectStage(stage)} className={cn("flex h-9 min-w-36 flex-1 items-center justify-between gap-3 rounded-md border px-3 text-left whitespace-nowrap", isCurrent ? "border-tungsten bg-tungsten/10" : isViewing ? "border-line bg-panel" : "border-line bg-transparent hover:border-tungsten/40")}>
                  <span className="flex min-w-0 items-center gap-2">
                    <span className={cn("size-1.5 shrink-0 rounded-full", isCurrent ? "bg-tungsten" : "bg-line")} />
                    <span className="truncate text-sm">{STAGE_LABEL[stage]}</span>
                  </span>
                  <span className="flex shrink-0 items-center gap-2 text-[11px] leading-none">
                    {isCurrent ? <span className="text-tungsten">当前</span> : isViewing ? <span className="text-muted">查看中</span> : null}
                    <span className={stageTone(status)}>{stageStatusLabel(status)}</span>
                  </span>
                </button>
              );
            })}
          </div>
        </div>
      </header>

      {job.need_aspect_confirm ? (
        <div className="shrink-0 border-b border-warn/40 bg-warn/10 px-4 py-2 text-sm text-warn">
          原片比例为 {job.source_aspect}，请选择输出比例。
          <button className="ml-3 text-tungsten hover:underline" onClick={() => api.aspect(id, false).then(() => api.resume(id)).then(refresh)}>保持 16:9</button>
          <button className="ml-3 text-tungsten hover:underline" onClick={() => api.aspect(id, true).then(() => api.resume(id)).then(refresh)}>跟随原片</button>
        </div>
      ) : null}

      <div className="relative grid min-h-0 flex-1 grid-cols-1 overflow-hidden lg:grid-cols-[16rem_minmax(0,1fr)]">
        <aside className={cn("z-30 min-h-0 flex-col overflow-hidden border-r border-line bg-panel/50", navOpen ? "absolute inset-y-0 left-0 flex w-[min(88vw,18rem)] bg-surface shadow-2xl lg:static lg:shadow-none" : "hidden lg:flex")}>
          <div className="flex shrink-0 items-center justify-between px-4 py-3">
            <div>
              <p className="text-[11px] uppercase tracking-[0.16em] text-muted">片段</p>
              <h2 className="mt-1 font-semibold">切换查看</h2>
            </div>
            <span className="font-mono text-xs text-muted">{clips.length}</span>
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto px-3 pb-4">
            {clips.map((clip) => {
              const status = clipBiz(clip, dirty);
              return (
                <button key={clip.id} type="button" className={cn("mb-1.5 w-full rounded-lg border px-3 py-2.5 text-left", clip.id === clipId ? "border-tungsten/60 bg-tungsten/10" : "border-transparent hover:border-line hover:bg-surface")} onClick={() => selectClip(clip.id)}>
                  <div className="flex items-center justify-between gap-2">
                    <span className="font-mono text-xs">{clip.id}</span>
                    <span className={cn("text-[11px]", status === "ready" ? "text-ok" : status === "pending" ? "text-warn" : status === "failed" ? "text-bad" : "text-tungsten")}>{clipBizLabel(status)}</span>
                  </div>
                  <p className="mt-1 text-xs text-muted">{fmtTime(clip.t0)} - {fmtTime(clip.t1)}</p>
                </button>
              );
            })}
            {!clips.length ? <p className="rounded-lg border border-dashed border-line p-3 text-xs leading-5 text-muted">脚本完成后，片段会出现在这里。</p> : null}
          </div>
        </aside>

        <main className="flex min-h-0 flex-col overflow-hidden px-4 py-2">
          {connection === "offline" ? <div className="mb-2 shrink-0 rounded-lg border border-bad/40 bg-bad/10 px-3 py-2 text-xs text-bad">实时更新已断开。<button type="button" className="ml-2 text-tungsten hover:underline" onClick={() => setReconnectKey((value) => value + 1)}>重新连接</button></div> : null}
          <div className="mb-2 flex min-h-7 shrink-0 items-center justify-between gap-3">
            <div className="flex min-w-0 items-center gap-2 text-sm">
              <h2 className="shrink-0 font-medium">{STAGE_LABEL[shownStage]}</h2>
              {current ? <span className="shrink-0 font-mono text-xs text-muted">{shownStage === "generate" ? `${current.id} 监视区` : current.id}</span> : null}
              {job.note ? <p className="min-w-0 truncate text-xs text-muted">{job.note}</p> : null}
            </div>
            {unsaved ? <span className="shrink-0 text-xs text-warn">有未保存修改</span> : null}
          </div>

          <section className="flex min-h-0 flex-1 flex-col overflow-hidden rounded-xl border border-line bg-surface">
            {shownStage === "download" ? (
              <DownloadWorkspace job={job} mediaSrc={sourceSrc} status={stageDisplayStatus(job, "download")} />
            ) : null}
            {shownStage === "pagemeta" ? (
              <PageMetaWorkspace job={job} status={stageDisplayStatus(job, "pagemeta")} />
            ) : null}
            {shownStage === "understand" ? (
              <UnderstandWorkspace job={job} summary={understand} status={stageDisplayStatus(job, "understand")} onDetail={() => setDetail("understand")} />
            ) : null}
            {shownStage === "script" ? (
              <ScriptWorkspace
                current={current}
                editing={editing}
                txt={txt}
                md={md}
                seconds={seconds}
                speech={speech}
                mediaSrc={current?.draft.file ? fileUrl(job.id, current.draft.file) : sourceSrc}
                setTxt={setTxt}
                setMd={setMd}
                setSeconds={setSeconds}
                onEdit={editScript}
                onAi={() => { setAiScope(current ? [current.id] : []); setAiOpen(true); }}
                onSave={() => void save()}
                busy={Boolean(busyAction)}
              />
            ) : null}
            {shownStage === "precheck" ? (
              <PrecheckWorkspace job={job} clips={clips} status={stageDisplayStatus(job, "precheck")} onDetail={() => setDetail("precheck")} />
            ) : null}
            {shownStage === "generate" ? (
              current ? (
                <VideoMonitor
                  job={job}
                  current={current}
                  preview={preview}
                  setPreview={setPreview}
                  mediaSrc={mediaSrc}
                  sourceReady={sourceReady}
                  onEdit={editScript}
                />
              ) : (
                <GenerateOverview job={job} clips={clips} dirty={dirty} />
              )
            ) : null}
            {shownStage === "finish" ? (
              <FinishWorkspace job={job} finalSrc={finalSrc} coverSrc={coverSrc} status={stageDisplayStatus(job, "finish")} />
            ) : null}
          </section>

          <div className="mt-2 flex shrink-0 items-center justify-between gap-3">
            <p className="min-w-0 truncate text-xs text-bad">{err}</p>
            <p className="min-w-0 truncate text-xs text-ok">{msg}</p>
            <div className="ml-auto flex items-center gap-2">
              {showBatch ? (
                <div className="relative" ref={batchRef}>
                  <button type="button" className="rounded-md border border-line px-3 py-2 text-sm text-muted hover:border-tungsten/60 hover:text-text" onClick={() => setBatchOpen((open) => !open)}>批量操作</button>
                  {batchOpen ? (
                    <div className="absolute right-0 bottom-full z-40 mb-2 w-52 rounded-lg border border-line bg-surface p-1 shadow-xl">
                      {canBatchScripts ? (
                        <button type="button" disabled={generateLocked} className="w-full rounded px-3 py-2 text-left text-sm text-muted hover:bg-panel hover:text-text disabled:opacity-40" title={generateLocked ? "任务正在处理" : undefined} onClick={() => openBatch("scripts")}>{generateLocked ? "批量生成脚本（处理中）" : "批量生成脚本"}</button>
                      ) : null}
                      {canBatchAi ? (
                        <button type="button" className="w-full rounded px-3 py-2 text-left text-sm text-muted hover:bg-panel hover:text-text" onClick={() => openBatch("ai")}>批量修改脚本</button>
                      ) : null}
                      {canBatchVideos ? (
                        <button type="button" disabled={generateLocked} className="w-full rounded px-3 py-2 text-left text-sm text-muted hover:bg-panel hover:text-text disabled:opacity-40" title={generateLocked ? "任务正在处理" : undefined} onClick={() => openBatch("videos")}>{generateLocked ? "批量生成视频（处理中）" : "批量生成视频"}</button>
                      ) : null}
                    </div>
                  ) : null}
                </div>
              ) : null}
              <button type="button" className="rounded-md border border-line px-3 py-2 text-sm text-bad" onClick={() => { if (window.confirm("打断任务并放弃？已有产物会保留。")) void api.cancel(id).then(refresh); }}>放弃任务</button>
              <button
                type="button"
                className="rounded-md border border-bad/40 px-3 py-2 text-sm text-bad disabled:opacity-40"
                disabled={!jobDeletable(job.state, Boolean(job.running)) || Boolean(busyAction)}
                title={job.running ? "任务正在处理，请先放弃" : undefined}
                onClick={() => {
                  if (!window.confirm(deleteJobsConfirmMessage(1))) return;
                  setBusyAction("delete");
                  setErr("");
                  void api.remove(id)
                    .then(() => nav("/"))
                    .catch((error: Error) => setErr(error.message))
                    .finally(() => setBusyAction(""));
                }}
              >
                {busyAction === "delete" ? "删除中..." : "删除任务"}
              </button>
              <button type="button" className="rounded-md bg-tungsten px-4 py-2 text-sm font-medium text-ink disabled:opacity-50" disabled={primaryDisabled} onClick={() => void runPrimary()}>{busyAction === "primary" || job.running ? "处理中..." : primaryActionLabel(job)}</button>
            </div>
          </div>
        </main>
      </div>

      {batchKind ? (
        <Dialog title={batchKind === "scripts" ? "批量生成脚本" : batchKind === "ai" ? "批量修改脚本" : "批量生成视频"} onClose={() => setBatchKind(null)} className="max-w-2xl">
          {batchKind === "videos" ? (
            <div className="mb-4">
              <p className="mb-2 text-sm font-medium">生成目标</p>
              <div className="grid gap-2 sm:grid-cols-2">
                <label className={cn("rounded-lg border p-3 text-sm", videoTarget === "draft" ? "border-tungsten bg-tungsten/10" : "border-line")}>
                  <input type="radio" className="mr-2" checked={videoTarget === "draft"} onChange={() => { setVideoTarget("draft"); const rec = recommendedIds("videos", "draft"); setPicked(rec); setShowOthers(!rec.length); }} />试片
                  <span className="mt-1 block text-xs text-muted">用于快速预览和审阅</span>
                </label>
                <label className={cn("rounded-lg border p-3 text-sm", videoTarget === "final" ? "border-tungsten bg-tungsten/10" : "border-line")}>
                  <input type="radio" className="mr-2" checked={videoTarget === "final"} onChange={() => { setVideoTarget("final"); const rec = recommendedIds("videos", "final"); setPicked(rec); setShowOthers(!rec.length); }} />成片
                  <span className="mt-1 block text-xs text-muted">用于最终交付</span>
                </label>
              </div>
            </div>
          ) : null}
          <p className="mb-3 text-sm text-muted">
            {batchKind === "scripts" ? "只会为还没有脚本的片段生成，已有脚本会被跳过。" : batchKind === "ai" ? "选中的片段会使用同一条修改需求，确认后先预览，不会自动生成视频。" : `${videoTarget === "final" ? "成片" : "试片"}推荐项已默认选中。`}
            已选择 {picked.length} 个。
          </p>
          <ClipPicker clips={clips} dirty={dirty} picked={picked} setPicked={setPicked} recommended={recommendedIds(batchKind)} showOthers={showOthers} setShowOthers={setShowOthers} target={batchKind === "videos" ? videoTarget : "draft"} kind={batchKind} />
          {confirmRisk ? <p className="mt-3 rounded-md border border-warn/40 bg-warn/10 p-3 text-xs text-warn">有 {riskyPicked.length} 个片段不满足推荐条件，可能混用旧版本。再次点击仍会继续。</p> : null}
          <div className="mt-5 flex justify-end gap-2">
            <button type="button" className="rounded-md px-3 py-2 text-sm text-muted" onClick={() => setBatchKind(null)}>取消</button>
            <button
              type="button"
              className="rounded-md bg-tungsten px-4 py-2 text-sm text-ink disabled:opacity-50"
              disabled={(batchKind !== "scripts" && !picked.length) || Boolean(busyAction) || (batchKind !== "ai" && generateLocked)}
              onClick={() => { if (riskyPicked.length && !confirmRisk) { setConfirmRisk(true); return; } void runBatch(); }}
            >
              {busyAction === "batch" || (batchKind !== "ai" && generateLocked) ? "处理中..." : batchKind === "videos" ? (videoTarget === "final" ? "生成成片" : "生成试片") : batchKind === "ai" ? "填写修改需求" : "生成脚本"}
            </button>
          </div>
        </Dialog>
      ) : null}

      {aiOpen ? (
        <Dialog title={aiScope.length > 1 ? `AI 修改脚本 · ${aiScope.length} 个片段` : `AI 修改脚本${aiScope[0] ? ` · ${aiScope[0]}` : ""}`} onClose={() => setAiOpen(false)}>
          <p className="mb-3 text-sm text-muted">描述你希望怎么改。确认后先预览，不会立刻覆盖原脚本，也不会自动生成视频。</p>
          <textarea className="h-32 w-full rounded-md border border-line bg-panel p-3 text-sm" value={aiPrompt} onChange={(event) => setAiPrompt(event.target.value)} placeholder="例如：加快镜头节奏，保留人物外观和原对白。" />
          <p className="mt-3 text-xs text-muted">当前版本请先在脚本工作区手动修改并保存。LLM 改写接入后，会在这里显示修改前后对比。</p>
          <div className="mt-5 flex justify-end gap-2">
            <button type="button" className="rounded-md px-3 py-2 text-sm text-muted" onClick={() => setAiOpen(false)}>取消</button>
            <button type="button" className="rounded-md bg-tungsten px-4 py-2 text-sm text-ink" onClick={() => { setAiOpen(false); setMsg("已记录修改需求，请先手动保存脚本。"); }}>确认</button>
          </div>
        </Dialog>
      ) : null}

      {detail === "understand" ? (
        <Dialog title="视频理解详情" onClose={() => setDetail(null)} className="max-w-3xl">
          <pre className="max-h-[60vh] overflow-auto whitespace-pre-wrap rounded-md bg-panel p-3 font-mono text-xs leading-5 text-muted">{understand?.raw || "还没有理解结果。"}</pre>
        </Dialog>
      ) : null}
      {detail === "precheck" ? (
        <Dialog title="预检报告" onClose={() => setDetail(null)}>
          <p className="text-sm">{job.precheck?.ok ? "预检通过" : "预检未通过或尚未完成"}</p>
          <div className="mt-3 max-h-[50vh] overflow-y-auto text-sm">
            {(job.precheck?.errors || []).map((item) => <p key={item} className="mb-2 text-bad">{item}</p>)}
            {(job.precheck?.warnings || []).map((item) => <p key={item} className="mb-2 text-warn">{item}</p>)}
            {!job.precheck?.errors?.length && !job.precheck?.warnings?.length ? <p className="text-muted">没有详细条目。</p> : null}
          </div>
        </Dialog>
      ) : null}
      {detail === "progress" ? (
        <Dialog title="处理进度" onClose={() => setDetail(null)}>
          <p className="text-sm text-muted">{job.note || "任务正在处理。"}</p>
          <div className="mt-3 max-h-[50vh] overflow-y-auto text-xs text-muted">
            {(job.events || []).slice().reverse().map((event, index) => (
              <p key={`${event.at}-${index}`} className="mb-2">{event.at} {event.kind} {event.clip_id || ""} {event.detail || ""}</p>
            ))}
            {!job.events?.length ? <p>还没有事件记录。</p> : null}
          </div>
        </Dialog>
      ) : null}
    </div>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-lg border border-line bg-panel/60 p-3">
      <p className="text-[11px] uppercase tracking-[0.14em] text-muted">{label}</p>
      <p className="mt-1 text-sm font-medium">{value}</p>
    </div>
  );
}

function DownloadWorkspace({ job, mediaSrc, status }: { job: JobDetailData; mediaSrc?: string; status: string }) {
  return (
    <div className="flex min-h-0 flex-1 flex-col p-4">
      <div className="mb-3 flex shrink-0 items-center justify-between">
        <h3 className="font-medium">输入与原片</h3>
        <span className={cn("text-xs", stageTone(status))}>{stageStatusLabel(status)}</span>
      </div>
      <div className="mb-3 grid shrink-0 gap-2 sm:grid-cols-3">
        <Metric label="输入" value={job.source?.kind === "url" ? "URL" : "本地文件"} />
        <Metric label="时长" value={fmtDur(job.source?.probe?.duration)} />
        <Metric label="分辨率" value={job.source?.probe?.width && job.source?.probe?.height ? `${job.source.probe.width}×${job.source.probe.height}` : "—"} />
      </div>
      <div className="min-h-0 flex-1 overflow-hidden rounded-lg bg-black">
        {mediaSrc ? <video key={mediaSrc} className="h-full w-full object-contain" controls src={mediaSrc} /> : <div className="flex h-full items-center justify-center px-6 text-center text-sm text-muted">原片还没有准备好</div>}
      </div>
      <p className="mt-3 shrink-0 truncate text-xs text-muted">{job.source?.url || job.source?.original_path || "未记录输入路径"}</p>
    </div>
  );
}

function PageMetaWorkspace({ job, status }: { job: JobDetailData; status: string }) {
  const skipped = job.source?.kind !== "url" || status === "skipped";
  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-hidden p-5">
      <div className="flex items-center justify-between">
        <h3 className="font-medium">页面信息</h3>
        <span className={cn("text-xs", stageTone(status))}>{stageStatusLabel(status)}</span>
      </div>
      <div className="mt-4 min-h-0 flex-1 overflow-y-auto text-sm leading-6 text-muted">
        {skipped ? <p>本地文件会跳过页面抓取，只保留输入路径和探测结果。</p> : <p>已记录 URL。Mock 模式不会访问网络，只保存用户填写的地址。</p>}
        <p className="mt-3 break-all">{job.source?.url || job.source?.original_path || "没有页面地址"}</p>
        {job.stages?.pagemeta?.error ? <p className="mt-3 rounded-md border border-bad/40 bg-bad/10 p-3 text-xs text-bad">{job.stages.pagemeta.error}</p> : null}
      </div>
    </div>
  );
}

function UnderstandWorkspace({
  job, summary, status, onDetail,
}: {
  job: JobDetailData;
  summary: { shots: number; events: number; speech: number; windows: number; duration?: number } | null;
  status: string;
  onDetail: () => void;
}) {
  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-hidden p-5">
      <div className="flex items-center justify-between gap-3">
        <h3 className="font-medium">理解摘要</h3>
        <div className="flex items-center gap-3">
          <span className={cn("text-xs", stageTone(status))}>{stageStatusLabel(status)}</span>
          <button type="button" className="text-xs text-tungsten hover:underline" onClick={onDetail}>查看详情</button>
        </div>
      </div>
      <div className="mt-4 grid shrink-0 gap-2 sm:grid-cols-2 lg:grid-cols-4">
        <Metric label="时长" value={fmtDur(summary?.duration ?? job.source?.probe?.duration)} />
        <Metric label="镜头" value={String(summary?.shots ?? "—")} />
        <Metric label="事件" value={String(summary?.events ?? "—")} />
        <Metric label="对白" value={String(summary?.speech ?? "—")} />
      </div>
      <p className="mt-4 text-sm leading-6 text-muted">完整拍表、识别结果和 JSON 放在详情弹窗里，不在中间工作区展开。</p>
      {job.stages?.understand?.error ? <p className="mt-3 rounded-md border border-bad/40 bg-bad/10 p-3 text-xs text-bad">{job.stages.understand.error}</p> : null}
    </div>
  );
}

function PrecheckWorkspace({
  job, clips, status, onDetail,
}: {
  job: JobDetailData;
  clips: ClipRow[];
  status: string;
  onDetail: () => void;
}) {
  const errors = job.precheck?.errors || [];
  const warnings = job.precheck?.warnings || [];
  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-hidden p-5">
      <div className="flex items-center justify-between gap-3">
        <h3 className="font-medium">预检结果</h3>
        <div className="flex items-center gap-3">
          <span className={cn("text-xs", stageTone(status))}>{stageStatusLabel(status)}</span>
          <button type="button" className="text-xs text-tungsten hover:underline" onClick={onDetail}>查看详细报告</button>
        </div>
      </div>
      <div className="mt-4 grid shrink-0 gap-2 sm:grid-cols-3">
        <Metric label="结果" value={job.precheck?.ok ? "通过" : errors.length ? "未通过" : "未完成"} />
        <Metric label="错误" value={String(errors.length)} />
        <Metric label="提示" value={String(warnings.length)} />
      </div>
      <div className="mt-4 min-h-0 flex-1 overflow-y-auto text-sm">
        {errors.slice(0, 6).map((item) => <p key={item} className="mb-2 text-bad">{item}</p>)}
        {warnings.slice(0, 4).map((item) => <p key={item} className="mb-2 text-warn">{item}</p>)}
        {!errors.length && !warnings.length ? <p className="text-muted">完成预检后，这里会显示错误、警告和受影响片段。当前共 {clips.length} 个片段。</p> : null}
      </div>
    </div>
  );
}

function GenerateOverview({ job, clips, dirty }: { job: JobDetailData; clips: ClipRow[]; dirty: string[] }) {
  const pending = clips.filter((clip) => clipBiz(clip, dirty) === "pending").length;
  const ready = clips.filter((clip) => clipBiz(clip, dirty) === "ready").length;
  const failed = clips.filter((clip) => clipBiz(clip, dirty) === "failed").length;
  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-hidden p-5">
      <h3 className="font-medium">生成概览</h3>
      <div className="mt-4 grid shrink-0 gap-2 sm:grid-cols-3">
        <Metric label="已生成" value={String(ready)} />
        <Metric label="待生成" value={String(pending)} />
        <Metric label="失败" value={String(failed)} />
      </div>
      <p className="mt-4 text-sm text-muted">需要批量时，用右下角「批量操作」。左侧点击片段后，这里会进入该片段的视频监视区。</p>
      <div className="mt-4 min-h-0 flex-1 overflow-y-auto">
        {clips.map((clip) => {
          const status = clipBiz(clip, dirty);
          return (
            <div key={clip.id} className="mb-2 flex items-center justify-between rounded-lg border border-line px-3 py-2 text-sm">
              <span className="font-mono text-xs">{clip.id}</span>
              <span className="text-xs text-muted">试片 {stageStatusLabel(clip.draft.status)} · 成片 {stageStatusLabel(clip.final.status)}</span>
              <span className={cn("text-xs", status === "ready" ? "text-ok" : status === "pending" ? "text-warn" : status === "failed" ? "text-bad" : "text-tungsten")}>{clipBizLabel(status)}</span>
            </div>
          );
        })}
        {!clips.length ? <p className="text-sm text-muted">{job.note || "还没有片段。"}</p> : null}
      </div>
    </div>
  );
}

function VideoMonitor({
  job, current, preview, setPreview, mediaSrc, sourceReady, onEdit,
}: {
  job: JobDetailData;
  current: ClipRow;
  preview: PreviewKind;
  setPreview: (value: PreviewKind) => void;
  mediaSrc?: string;
  sourceReady: boolean;
  onEdit: () => void;
}) {
  const stale = current.draft.dirty || current.final.dirty;
  return (
    <div className="flex min-h-0 flex-1 flex-col p-4">
      <div className="mb-3 flex shrink-0 flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-1 rounded-lg bg-panel p-1">
          {(["source", "draft", "final"] as const).map((kind) => {
            const available = kind === "source" ? sourceReady : Boolean(current[kind]?.file);
            return <button key={kind} type="button" disabled={!available} className={cn("rounded-md px-3 py-1.5 text-sm", preview === kind ? "bg-surface text-text shadow-sm" : "text-muted", !available && "opacity-40")} onClick={() => setPreview(kind)}>{kind === "source" ? "原片" : kind === "draft" ? "试片" : "成片"}</button>;
          })}
        </div>
        <button type="button" className="rounded-md border border-line px-3 py-1.5 text-sm text-muted hover:border-tungsten/60 hover:text-text" onClick={onEdit}>编辑脚本</button>
      </div>
      <div className="min-h-0 flex-1 overflow-hidden rounded-lg bg-black">
        {mediaSrc ? <video key={mediaSrc} className="h-full w-full object-contain" controls src={mediaSrc} /> : <div className="flex h-full items-center justify-center px-6 text-center text-sm text-muted">{preview === "source" ? "原片还没有准备好" : preview === "draft" ? "试片还没有生成" : "成片还没有生成"}</div>}
      </div>
      <div className="mt-3 flex shrink-0 flex-wrap items-center justify-between gap-2 text-xs text-muted">
        <span>{fmtTime(current.t0)} - {fmtTime(current.t1)} · 生成 {fmtTime(current.h3_seconds)}</span>
        <span>试片 {stageStatusLabel(current.draft.status)} · 成片 {stageStatusLabel(current.final.status)}{stale ? " · 当前视频仍是旧版本" : ""}</span>
        {job.options?.review_mode === "full_auto" ? <span>自动交付</span> : null}
      </div>
    </div>
  );
}

function FinishWorkspace({
  job, finalSrc, coverSrc, status,
}: {
  job: JobDetailData;
  finalSrc?: string;
  coverSrc?: string;
  status: string;
}) {
  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-hidden p-4">
      <div className="mb-3 flex shrink-0 items-center justify-between">
        <h3 className="font-medium">成片交付</h3>
        <span className={cn("text-xs", stageTone(status))}>{stageStatusLabel(status)}</span>
      </div>
      <div className="grid min-h-0 flex-1 gap-3 overflow-hidden lg:grid-cols-[minmax(0,1fr)_12rem]">
        <div className="min-h-0 overflow-hidden rounded-lg bg-black">
          {finalSrc ? <video key={finalSrc} className="h-full w-full object-contain" controls src={finalSrc} /> : <div className="flex h-full items-center justify-center px-6 text-center text-sm text-muted">成片还没有生成</div>}
        </div>
        <div className="hidden min-h-0 flex-col overflow-hidden lg:flex">
          {coverSrc ? <img src={coverSrc} alt="封面" className="h-full w-full rounded-lg object-cover" /> : <div className="flex h-full items-center justify-center rounded-lg border border-dashed border-line text-xs text-muted">暂无封面</div>}
        </div>
      </div>
      <div className="mt-3 flex shrink-0 flex-wrap gap-3 text-sm">
        {job.media?.final ? <a className="text-tungsten hover:underline" href={fileUrl(job.id, job.media.final)}>下载成片</a> : null}
        {job.media?.cover ? <a className="text-tungsten hover:underline" href={fileUrl(job.id, job.media.cover)}>下载封面</a> : null}
        {job.media?.ass ? <a className="text-tungsten hover:underline" href={fileUrl(job.id, job.media.ass)}>下载字幕</a> : null}
        {!job.media?.final && !job.media?.cover ? <span className="text-xs text-muted">交付完成后可在这里下载成片、封面和字幕。</span> : null}
      </div>
    </div>
  );
}

function useWideLayout() {
  const [wide, setWide] = useState(() => (typeof window === "undefined" ? true : window.matchMedia("(min-width: 1024px)").matches));
  useEffect(() => {
    const media = window.matchMedia("(min-width: 1024px)");
    const onChange = () => setWide(media.matches);
    onChange();
    media.addEventListener("change", onChange);
    return () => media.removeEventListener("change", onChange);
  }, []);
  return wide;
}

function ScriptWorkspace({
  current, editing, txt, md, seconds, speech, mediaSrc, setTxt, setMd, setSeconds, onEdit, onAi, onSave, busy,
}: {
  current: ClipRow | null;
  editing: boolean;
  txt: string;
  md: string;
  seconds: string;
  speech: SpeechLine[];
  mediaSrc?: string;
  setTxt: (value: string) => void;
  setMd: (value: string) => void;
  setSeconds: (value: string) => void;
  onEdit: () => void;
  onAi: () => void;
  onSave: () => void;
  busy: boolean;
}) {
  const wide = useWideLayout();
  const layoutId = wide ? "vrs-script-h" : "vrs-script-v";
  const { defaultLayout, onLayoutChanged } = useDefaultLayout({ id: layoutId });

  if (!current) {
    return <div className="flex min-h-0 flex-1 items-center justify-center p-6 text-sm text-muted">从左侧选择一个片段后，这里显示该片段的脚本。</div>;
  }
  if (!editing) {
    return (
      <div className="flex min-h-0 flex-1 flex-col items-start justify-center p-6">
        <p className="text-sm text-muted">{current.id} 当前是已生成结果。要改脚本请进入编辑，不会立刻重新生成视频。</p>
        <button type="button" className="mt-4 rounded-md bg-tungsten px-4 py-2 text-sm text-ink" onClick={onEdit}>编辑脚本</button>
      </div>
    );
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-hidden">
      <div className="flex shrink-0 items-center justify-between border-b border-line px-4 py-2">
        <div>
          <p className="font-mono text-sm">{current.id}</p>
          <p className="text-xs text-muted">{fmtTime(current.t0)} - {fmtTime(current.t1)}{current.draft.dirty ? " · 当前视频仍是旧版本" : ""}</p>
        </div>
        <div className="flex gap-2">
          <button type="button" className="rounded-md border border-line px-3 py-1.5 text-sm text-muted" onClick={onAi}>AI 修改脚本</button>
          <button type="button" className="rounded-md bg-tungsten px-3 py-1.5 text-sm text-ink disabled:opacity-50" disabled={busy} onClick={onSave}>保存脚本</button>
        </div>
      </div>
      <ResizablePanelGroup
        key={layoutId}
        id={layoutId}
        orientation={wide ? "horizontal" : "vertical"}
        defaultLayout={defaultLayout}
        onLayoutChanged={onLayoutChanged}
        className="min-h-0 flex-1"
      >
        <ResizablePanel id="monitor" defaultSize={wide ? "58%" : "46%"} minSize={wide ? "32%" : "28%"} className="min-h-0">
          <div className="h-full min-h-0 bg-black">
            {mediaSrc ? <video key={mediaSrc} className="h-full w-full object-contain" controls src={mediaSrc} /> : <div className="flex h-full items-center justify-center p-6 text-center text-sm text-muted">没有参考视频</div>}
          </div>
        </ResizablePanel>
        <ResizableHandle withHandle />
        <ResizablePanel id="script" defaultSize={wide ? "42%" : "54%"} minSize="28%" className="min-h-0">
          <div className="flex h-full min-h-0 flex-col">
            <div className="grid min-h-0 flex-1 grid-rows-[auto_minmax(0,1fr)_minmax(0,0.75fr)] gap-3 overflow-hidden p-4">
              <label className="block text-xs text-muted">H3 时长（秒）<input className="mt-1 w-full rounded-md border border-line bg-panel p-2 text-sm text-text" value={seconds} onChange={(event) => setSeconds(event.target.value)} /></label>
              <label className="flex min-h-0 flex-col text-xs text-muted">英文提示词<textarea className="mt-1 min-h-0 flex-1 resize-none rounded-md border border-line bg-panel p-3 font-mono text-xs leading-5 text-text" value={txt} onChange={(event) => setTxt(event.target.value)} /></label>
              <label className="flex min-h-0 flex-col text-xs text-muted">中文对照<textarea className="mt-1 min-h-0 flex-1 resize-none rounded-md border border-line bg-panel p-3 font-mono text-xs leading-5 text-text" value={md} onChange={(event) => setMd(event.target.value)} /></label>
            </div>
            <div className="max-h-20 shrink-0 overflow-y-auto border-t border-line px-4 py-2 text-xs text-muted">
              {speech.length ? speech.map((line) => <p key={`${line.t0}-${line.text}`}>{fmtTime(line.t0)} {line.text}</p>) : "本段没有对白"}
            </div>
          </div>
        </ResizablePanel>
      </ResizablePanelGroup>
    </div>
  );
}

function ClipPicker({
  clips, dirty, picked, setPicked, recommended, showOthers, setShowOthers, target, kind,
}: {
  clips: ClipRow[];
  dirty: string[];
  picked: string[];
  setPicked: (ids: string[]) => void;
  recommended: string[];
  showOthers: boolean;
  setShowOthers: (value: boolean) => void;
  target: PreviewKind;
  kind: Exclude<BatchKind, null>;
}) {
  const rec = clips.filter((clip) => recommended.includes(clip.id));
  const others = clips.filter((clip) => !recommended.includes(clip.id));
  function toggle(id: string, disabled: boolean) {
    if (disabled) return;
    setPicked(picked.includes(id) ? picked.filter((item) => item !== id) : [...picked, id]);
  }
  function blocked(clip: ClipRow) {
    if (kind === "scripts") return clip.has_script === true;
    if (kind === "ai") return clip.has_script === false;
    return false;
  }
  function note(clip: ClipRow) {
    if (kind === "scripts" && clip.has_script) return "已有脚本，批量生成会跳过";
    if (kind === "scripts") return "还没有脚本，推荐生成";
    if (kind === "ai" && !clip.has_script) return "还没有脚本，无法修改";
    const status = clipBiz(clip, dirty);
    if (recommended.includes(clip.id) && status === "pending") return "推荐重新生成，当前视频对应旧脚本";
    if (recommended.includes(clip.id) && status === "failed") return "上次生成失败，建议重试";
    if (target === "final" && clip.draft.status !== "done") return "尚未生成新试片，请确认是否继续";
    if (clip.draft.status === "done") return "当前脚本和视频一致，可按需重生成";
    return "可按需加入本次处理";
  }
  function row(clip: ClipRow, recItem: boolean) {
    const disabled = blocked(clip);
    return (
      <label key={clip.id} className={cn("mb-2 flex items-start gap-3 rounded-lg border p-3", disabled ? "cursor-not-allowed opacity-50" : "cursor-pointer", picked.includes(clip.id) ? "border-tungsten/50 bg-tungsten/10" : "border-line")}>
        <input type="checkbox" checked={picked.includes(clip.id)} disabled={disabled} onChange={() => toggle(clip.id, disabled)} className="mt-1" />
        <span className="min-w-0 flex-1">
          <span className="flex items-center justify-between gap-2"><span className="font-mono text-sm">{clip.id}</span><span className={cn("text-xs", recItem ? "text-tungsten" : "text-muted")}>{clipBizLabel(clipBiz(clip, dirty))}</span></span>
          <span className="mt-1 block text-xs leading-5 text-muted">{note(clip)}</span>
        </span>
      </label>
    );
  }
  return (
    <div>
      {rec.map((clip) => row(clip, true))}
      {others.length ? (
        <div className="mt-3">
          <button type="button" className="text-xs text-tungsten hover:underline" onClick={() => setShowOthers(!showOthers)}>{showOthers ? "收起其他片段" : `其他片段 ${others.length}`}</button>
          {showOthers ? <div className="mt-2">{others.map((clip) => row(clip, false))}</div> : null}
        </div>
      ) : null}
      {!clips.length ? <p className="rounded-md border border-dashed border-line p-3 text-sm text-muted">当前还没有片段。点「生成脚本」后会出现片段。</p> : null}
    </div>
  );
}
