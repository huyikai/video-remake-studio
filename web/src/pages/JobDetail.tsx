import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { useDefaultLayout } from "react-resizable-panels";
import Dialog from "../components/Dialog";
import UnderstandDetail, { type UnderstandDocs } from "../components/UnderstandDetail";
import { ResizableHandle, ResizablePanel, ResizablePanelGroup } from "../components/ui/resizable";
import { api } from "../lib/api";
import { dispatchPrimaryAction, primaryActionDisabled, primaryActionFromJob, primaryActionLabel, showCornerPrimary } from "../lib/pipeline";
import { cn, fileUrl, fmtDur, fmtTime, generatePathLabel, jobNoteText, jobStateLabel, modeLabel, stageStatusLabel } from "../lib/utils";

const STAGES = ["download", "pagemeta", "understand", "script", "precheck", "draft", "clips", "finish"] as const;
type StageId = (typeof STAGES)[number];

const STAGE_LABEL: Record<string, string> = {
  download: "下载",
  pagemeta: "页面信息",
  understand: "视频理解",
  script: "脚本生成",
  precheck: "预检",
  draft: "试片",
  clips: "成片",
  finish: "拼接成片",
};

type StageRecord = { status: string; error?: string | null };
type MediaRecord = { status: string; dirty: boolean; file?: string | null };
type ClipRow = {
  id: string;
  t0: number;
  t1: number;
  source_seconds?: number;
  h3_seconds: number;
  padded?: boolean;
  has_script?: boolean;
  draft: MediaRecord;
  final: MediaRecord;
};
type SpeechLine = { t0: number; t1: number; text: string };
type UnderstandStep = {
  id: string;
  label: string;
  status: string;
  window?: number;
  windows?: number;
  window_t0?: number | null;
  window_t1?: number | null;
  window_failed?: number | null;
};
type UnderstandProgress = {
  step?: string;
  chip?: string;
  wait?: string | null;
  detail?: string;
  window?: number | null;
  windows?: number | null;
  window_t0?: number | null;
  window_t1?: number | null;
  window_failed?: number | null;
  step_started_at?: string | null;
  steps?: UnderstandStep[];
};
type JobDetailData = {
  id: string;
  mode?: "mock" | "real";
  state: string;
  stage: string;
  updated_at?: string;
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
  drafts_ready?: boolean;
  finals_ready?: boolean;
  next_action?: string;
  media?: { source?: string | null; draft?: string | null; final?: string | null; cover?: string | null; ass?: string | null };
  events?: { at: string; kind: string; clip_id?: string; detail?: string; quality?: string }[];
  precheck?: { ok?: boolean; errors?: string[]; warnings?: string[] };
  finish_report?: { ok?: boolean; clips?: number; concat?: boolean; ass_burn?: boolean; ass_events?: number; cover?: string | null };
  generate_progress?: { last_clip?: string | null; last_quality?: string | null };
  download_auth_cookie?: boolean;
  douyin_cookie_from_env?: boolean;
  douyin_cookie_expired?: boolean;
  understand_progress?: UnderstandProgress;
};

type PreviewKind = "source" | "draft" | "final";
type BatchKind = "scripts" | "ai" | "videos" | null;
type ClipBiz = "ready" | "pending" | "generating" | "failed" | "stale";
type DetailKind = "understand" | "precheck" | "progress" | null;
type RewPreview = {
  clip_id: string;
  script_zh: string;
  prompt_txt: string;
  review_md: string;
  prompt_json: Record<string, unknown>;
  h3_seconds: number;
};

function stageTone(status?: string) {
  const key = (status || "").toLowerCase();
  if (key === "done" || key === "skipped") return "text-ok";
  if (key === "running") return "text-tungsten";
  if (key === "failed" || key === "error") return "text-bad";
  if (key === "waiting" || key === "dirty" || key === "paused" || key === "pause") return "text-warn";
  return "text-muted";
}

function clipQualityForStage(stage: StageId | "draft" | "clips"): "draft" | "final" {
  if (stage === "clips" || stage === "finish") return "final";
  return "draft";
}

function clipBiz(clip: ClipRow, quality: "draft" | "final"): ClipBiz {
  const rec = clip[quality];
  if (rec.status === "running") return "generating";
  if (rec.status === "error") return "failed";
  if (clip.has_script === false) return rec.status === "done" ? "stale" : "pending";
  if (rec.status === "done") {
    if (rec.dirty || (quality === "final" && clip.draft.dirty)) return "stale";
    return "ready";
  }
  return "pending";
}

function clipBizLabel(status: ClipBiz) {
  if (status === "ready") return "已生成";
  if (status === "generating") return "生成中";
  if (status === "failed") return "失败";
  if (status === "stale") return "脚本已改";
  return "待生成";
}

function clipBizTone(status: ClipBiz) {
  if (status === "ready") return "text-ok";
  if (status === "pending" || status === "stale") return "text-warn";
  if (status === "failed") return "text-bad";
  return "text-tungsten";
}

function clipNeedsDraft(clip: ClipRow) {
  return clip.draft.status !== "done" || clip.draft.dirty;
}

function clipNeedsFinal(clip: ClipRow) {
  return !clipNeedsDraft(clip) && (clip.final.status !== "done" || clip.final.dirty);
}

function clipsReadyToAssemble(job: JobDetailData) {
  const rows = job.clips || [];
  return rows.length > 0 && rows.every((clip) => clip.final.status === "done" && !clip.final.dirty && !clip.draft.dirty);
}

function draftsReady(job: JobDetailData) {
  if (typeof job.drafts_ready === "boolean") return job.drafts_ready;
  const clips = job.clips || [];
  return clips.length > 0 && clips.every((clip) => clip.draft.status === "done");
}

function finalsReady(job: JobDetailData) {
  if (typeof job.finals_ready === "boolean") return job.finals_ready;
  const clips = job.clips || [];
  return clips.length > 0 && clips.every((clip) => clip.final.status === "done");
}

function currentProgressStage(job: JobDetailData): StageId {
  const finish = job.stages?.finish?.status || "";
  const generate = job.stages?.generate?.status || "";
  const lastQuality = job.generate_progress?.last_quality;
  if (finish === "running" || finish === "waiting") return "finish";
  if (generate === "running" || generate === "waiting") {
    return lastQuality === "final" || draftsReady(job) ? "clips" : "draft";
  }
  const earlyRunning = (["download", "pagemeta", "understand", "script", "precheck"] as const).find((stage) => {
    const status = job.stages?.[stage]?.status;
    return status === "running" || status === "waiting" || status === "failed";
  });
  if (earlyRunning) return earlyRunning;
  for (const name of ["download", "pagemeta", "understand", "script", "precheck"] as const) {
    const status = job.stages?.[name]?.status || "";
    if (status !== "done" && status !== "skipped") return name;
  }
  const rows = job.clips || [];
  if (rows.some((clip) => clipNeedsDraft(clip))) return "draft";
  if (rows.some((clip) => clipNeedsFinal(clip))) return "clips";
  if (finish === "done" || finish === "failed" || finalsReady(job)) return "finish";
  if (generate === "failed") return draftsReady(job) ? "clips" : "draft";
  if (draftsReady(job) || job.stages?.precheck?.status === "done") return "draft";
  if (STAGES.includes(job.stage as StageId)) return job.stage as StageId;
  return "download";
}

function understandChip(job: JobDetailData, status?: string) {
  const key = (status || "").toLowerCase();
  if (key === "running" || key === "waiting") {
    const chip = job.understand_progress?.chip;
    if (chip) return chip;
  }
  return stageStatusLabel(status);
}

function stageDisplayStatus(job: JobDetailData, stage: StageId) {
  const rows = job.clips || [];
  if (stage === "script" && (job.dirty_clip_ids || []).length) return "dirty";
  if (stage === "draft") {
    const gen = job.stages?.generate?.status || "";
    if (rows.some((clip) => clipNeedsDraft(clip))) {
      if ((gen === "running" || gen === "waiting") && job.generate_progress?.last_quality !== "final") return gen;
      if (gen === "failed") return "failed";
      return "pending";
    }
    if (draftsReady(job)) return "done";
    return "pending";
  }
  if (stage === "clips") {
    const gen = job.stages?.generate?.status || "";
    if (rows.some((clip) => clipNeedsFinal(clip) || clipNeedsDraft(clip))) {
      if ((gen === "running" || gen === "waiting") && (job.generate_progress?.last_quality === "final" || draftsReady(job))) return gen;
      if (gen === "failed" && draftsReady(job)) return "failed";
      return "pending";
    }
    if (finalsReady(job)) return "done";
    return "pending";
  }
  if (stage === "finish") {
    if (rows.some((clip) => clipNeedsDraft(clip) || clipNeedsFinal(clip))) return "pending";
    return job.stages?.finish?.status || "pending";
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
  const [scriptZh, setScriptZh] = useState("");
  const [savedZh, setSavedZh] = useState("");
  const [txt, setTxt] = useState("");
  const [seconds, setSeconds] = useState("");
  const [speech, setSpeech] = useState<SpeechLine[]>([]);
  const [aiOpen, setAiOpen] = useState(false);
  const [aiPrompt, setAiPrompt] = useState("");
  const [aiScope, setAiScope] = useState<string[]>([]);
  const [rewPreview, setRewPreview] = useState<RewPreview | null>(null);
  const [rewBusy, setRewBusy] = useState(false);
  const [saveBusy, setSaveBusy] = useState(false);
  const [err, setErr] = useState("");
  const [msg, setMsg] = useState("");
  const [connection, setConnection] = useState<"connecting" | "live" | "offline">("connecting");
  const [reconnectKey, setReconnectKey] = useState(0);
  const [busyAction, setBusyAction] = useState("");
  const [confirmRisk, setConfirmRisk] = useState(false);
  const [detail, setDetail] = useState<DetailKind>(null);
  const [understand, setUnderstand] = useState<UnderstandDocs | null>(null);
  const batchRef = useRef<HTMLDivElement>(null);
  const clipIdRef = useRef<string | null>(null);
  const rewReqRef = useRef(0);
  clipIdRef.current = clipId;

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
    let stale = false;
    setScriptZh("");
    setSavedZh("");
    setTxt("");
    setSeconds("");
    setSpeech([]);
    setRewPreview(null);
    api.clip(id, clipId).then((raw) => {
      if (stale) return;
      const data = raw as { script_zh: string; prompt_txt: string; review_md: string; clip: { h3_seconds: number }; speech?: SpeechLine[] };
      setScriptZh(data.script_zh || "");
      setSavedZh(data.script_zh || "");
      setTxt(data.prompt_txt || "");
      setSeconds(String(data.clip?.h3_seconds ?? ""));
      setSpeech(data.speech || []);
    }).catch((error: Error) => {
      if (!stale) setErr(error.message);
    });
    return () => {
      stale = true;
    };
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
    const watching = (viewStage ?? job?.stage) === "understand" || detail === "understand";
    if (!id || !watching) return;
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
        understanding: summary,
        shotsDoc: shots,
        eventsDoc: events,
        dialogueDoc: dialogue,
        beatsDoc: beats,
        raw: JSON.stringify({ understanding: summary, shots, events, dialogue, beats }, null, 2),
      });
    }).catch(() => {
      if (!cancelled) setUnderstand(null);
    });
    return () => {
      cancelled = true;
    };
  }, [id, job?.stage, job?.updated_at, viewStage, detail]);

  const dirty = job?.dirty_clip_ids || [];
  const clips = job?.clips || [];
  const current = clips.find((clip) => clip.id === clipId) || null;
  const progressStage = job ? currentProgressStage(job) : "download";
  const shownStage = job?.running ? progressStage : (viewStage ?? progressStage);
  const unsaved = Boolean(clipId) && scriptZh !== savedZh;
  const sourceReady = Boolean(job?.media?.source);

  useEffect(() => {
    if (job?.running) setViewStage(null);
  }, [job?.running]);

  const prevStateRef = useRef<string | null>(null);
  useEffect(() => {
    const current = (job?.state || "").toLowerCase() || null;
    if (current === "done" && prevStateRef.current !== "done") {
      setViewStage("finish");
    }
    prevStateRef.current = current;
  }, [job?.state]);

  useEffect(() => {
    if (shownStage === "draft" && preview === "final") setPreview("draft");
  }, [shownStage, preview]);

  const pendingClips = clips.filter((clip) => clipBiz(clip, "draft") === "pending");
  const failedClips = clips.filter((clip) => clipBiz(clip, "draft") === "failed");
  const missingScripts = clips.filter((clip) => clip.has_script === false);
  const understandDone = ["done", "skipped"].includes(job?.stages?.understand?.status || "");
  const precheckDone = job?.stages?.precheck?.status === "done";
  const canBatchScripts = understandDone && missingScripts.length > 0;
  const canBatchAi = clips.some((clip) => clip.has_script);
  const canBatchVideos = precheckDone;
  const showBatch = canBatchScripts || canBatchAi || canBatchVideos;
  const generateLocked = Boolean(job?.running) || busyAction === "primary" || busyAction === "batch" || busyAction === "clip";

  useEffect(() => {
    if (!showBatch) setBatchOpen(false);
  }, [showBatch, shownStage]);

  const previewRel = useMemo(() => {
    if (!job) return null;
    if (current && preview !== "source") return current[preview]?.file || null;
    return job.media?.[preview] || (preview === "source" ? job.media?.source : null);
  }, [current, job, preview]);
  const mediaSrc = job && previewRel ? fileUrl(job.id, previewRel) : undefined;

  function recommendedIds(kind: Exclude<BatchKind, null>, target = videoTarget) {
    if (kind === "scripts") return missingScripts.map((clip) => clip.id);
    if (kind === "ai") {
      const needWork = clips.filter((clip) => {
        const status = clipBiz(clip, "draft");
        return status === "pending" || status === "stale" || clip.draft.dirty || clip.final.dirty;
      });
      return (needWork.length ? needWork : clips.filter((clip) => clip.has_script)).map((clip) => clip.id);
    }
    if (kind === "videos") {
      if (target === "final") return clips.filter(clipNeedsFinal).map((clip) => clip.id);
      return clips.filter(clipNeedsDraft).map((clip) => clip.id);
    }
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
    if (job?.running) return;
    setViewStage(stage);
    setNavOpen(false);
    setEditing(stage === "script" && Boolean(clipId));
  }

  function selectClip(nextId: string) {
    if (clipId === nextId && (shownStage === "draft" || shownStage === "clips")) {
      setClipId(null);
      setNavOpen(false);
      return;
    }
    const clip = clips.find((item) => item.id === nextId);
    if (!clip) return;
    setClipId(nextId);
    setNavOpen(false);
    if (shownStage === "finish") {
      return;
    }
    if (job?.running) {
      if (shownStage === "clips") {
        setPreview(clip.final.status === "done" ? "final" : clip.draft.status === "done" ? "draft" : "source");
      } else if (shownStage === "draft") {
        setPreview(clip.draft.status === "done" ? "draft" : "source");
      }
      return;
    }
    const selectingFromScript = shownStage === "script";
    if (selectingFromScript) {
      setViewStage("script");
      setEditing(true);
      return;
    }
    if (shownStage === "draft" || shownStage === "clips") {
      setViewStage(shownStage);
      setEditing(false);
      if (shownStage === "clips") {
        setPreview(clip.final.status === "done" ? "final" : clip.draft.status === "done" ? "draft" : "source");
      } else {
        setPreview(clip.draft.status === "done" ? "draft" : "source");
      }
      return;
    }
    const status = clipBiz(clip, "draft");
    if (status === "pending" || status === "failed") {
      setViewStage("script");
      setEditing(true);
      return;
    }
    setViewStage(clip.final.status === "done" ? "clips" : "draft");
    setEditing(false);
    setPreview(clip.final.status === "done" ? "final" : clip.draft.status === "done" ? "draft" : "source");
  }

  function editScript() {
    setViewStage("script");
    setEditing(true);
  }

  async function previewRewrite(payload: Record<string, unknown>) {
    const targetClip = clipId;
    if (!targetClip) return;
    const h3s = Number(seconds);
    if (!Number.isFinite(h3s) || h3s <= 0) {
      setErr("H3 时长必须是大于 0 的数字");
      return;
    }
    const req = ++rewReqRef.current;
    setRewBusy(true);
    setErr("");
    setMsg("");
    try {
      const data = (await api.rewriteClip(id, targetClip, { ...payload, h3_seconds: h3s })) as RewPreview;
      if (req !== rewReqRef.current || clipIdRef.current !== targetClip) return;
      setRewPreview({ ...data, clip_id: data.clip_id || targetClip });
    } catch (error) {
      if (req !== rewReqRef.current || clipIdRef.current !== targetClip) return;
      setErr((error as Error).message);
      setRewPreview(null);
    } finally {
      if (req === rewReqRef.current) setRewBusy(false);
    }
  }

  async function confirmPreview() {
    const targetClip = clipId;
    if (!targetClip || !rewPreview) return;
    if (rewPreview.clip_id && rewPreview.clip_id !== targetClip) {
      setErr("预览已过期，请重新生成英文脚本");
      setRewPreview(null);
      return;
    }
    const preview = rewPreview;
    setSaveBusy(true);
    setErr("");
    setMsg("");
    try {
      const result = (await api.saveClip(id, targetClip, { prompt_json: preview.prompt_json, h3_seconds: preview.h3_seconds })) as {
        ok?: boolean;
        error?: string;
        precheck?: { ok?: boolean; errors?: string[] };
      };
      if (result.ok === false) {
        if (clipIdRef.current === targetClip) {
          setErr(result.error || "保存失败");
          if (result.precheck?.errors?.length) setDetail("precheck");
        }
        return;
      }
      if (clipIdRef.current !== targetClip) {
        await refresh();
        return;
      }
      if (result.precheck && !result.precheck.ok) {
        setDetail("precheck");
        setMsg("脚本已更新，但预检未通过，请查看预检报告。");
      } else {
        setMsg("已保存新的英文脚本，该片段待重新出试片");
      }
      setScriptZh(preview.script_zh);
      setSavedZh(preview.script_zh);
      setTxt(preview.prompt_txt);
      setSeconds(String(preview.h3_seconds));
      setRewPreview(null);
      await refresh();
    } catch (error) {
      if (clipIdRef.current === targetClip) setErr((error as Error).message);
    } finally {
      setSaveBusy(false);
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
        if (videoTarget === "final") await api.finals(id, picked);
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

  async function produceDrafts(ids: string[]) {
    if (!ids.length) return;
    setBusyAction("clip");
    setErr("");
    try {
      await api.draft(id, ids);
      await refresh();
    } catch (error) {
      setErr((error as Error).message);
    } finally {
      setBusyAction("");
    }
  }

  async function produceFinals(ids: string[]) {
    if (!ids.length) return;
    setBusyAction("clip");
    setErr("");
    try {
      await api.finals(id, ids);
      await refresh();
    } catch (error) {
      setErr((error as Error).message);
    } finally {
      setBusyAction("");
    }
  }

  async function assembleFilm() {
    setBusyAction("clip");
    setErr("");
    try {
      await api.assemble(id);
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
  const batchInFooter = shownStage === "download" || shownStage === "pagemeta" || shownStage === "understand" || shownStage === "precheck";
  const batchMenu = showBatch ? (
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
  ) : null;
  const workspaceBatch = batchInFooter ? null : batchMenu;

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
              {job.state === "done" ? (
                <span className="rounded border border-ok/50 bg-ok/10 px-2 py-0.5 text-[11px] font-medium text-ok">✅ 已完成</span>
              ) : (
                <span className={cn("font-medium", stageTone(job.state))}>{jobStateLabel(job.state)}</span>
              )}
              {dirty.length ? <span className="text-warn">未应用 {dirty.length}</span> : null}
              {job.running ? <button type="button" className="text-tungsten hover:underline" onClick={() => setDetail("progress")}>处理中</button> : null}
              {err ? <span className="max-w-xs truncate text-bad" title={err}>{err}</span> : null}
              {msg ? <span className="max-w-xs truncate text-ok" title={msg}>{msg}</span> : null}
              {job.running ? (
                <button type="button" className="text-bad hover:underline" onClick={() => { if (!window.confirm("打断任务并放弃？已有产物会保留。")) return; setErr(""); void api.cancel(id).then(refresh).catch((error: Error) => setErr(error.message)); }}>放弃</button>
              ) : null}
            </div>
          </div>
        </div>
        <div className="overflow-x-auto border-t border-line px-4 py-2.5 sm:px-6">
          <div className="flex min-w-max items-center gap-2">
            {STAGES.map((stage) => {
              const status = stageDisplayStatus(job, stage);
              const isCurrent = progressStage === stage;
              const isSelected = shownStage === stage;
              return (
                <button key={stage} type="button" onClick={() => selectStage(stage)} className={cn("flex h-9 min-w-36 flex-1 items-center justify-between gap-3 rounded-md border px-3 text-left whitespace-nowrap", isSelected ? "border-tungsten bg-tungsten/10" : "border-line bg-transparent hover:border-tungsten/40")}>
                  <span className="flex min-w-0 items-center gap-2">
                    <span className={cn("size-1.5 shrink-0 rounded-full", isSelected ? "bg-tungsten" : "bg-line")} />
                    <span className={cn("truncate text-sm", isSelected && "font-medium")}>{STAGE_LABEL[stage]}</span>
                  </span>
                  <span className="flex shrink-0 items-center gap-2 text-[11px] leading-none">
                    {isCurrent ? <span className="rounded bg-tungsten px-1.5 py-0.5 font-medium text-ink">当前</span> : null}
                    <span className={stageTone(status)}>{stage === "understand" ? understandChip(job, status) : stageStatusLabel(status)}</span>
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
              const status = clipBiz(clip, clipQualityForStage(shownStage));
              return (
                <button key={clip.id} type="button" disabled={rewBusy || saveBusy} title={rewBusy || saveBusy ? "正在处理英文脚本，请稍候" : undefined} className={cn("mb-1.5 w-full rounded-lg border px-3 py-2.5 text-left disabled:cursor-not-allowed disabled:opacity-50", clip.id === clipId ? "border-tungsten/60 bg-tungsten/10" : "border-transparent hover:border-line hover:bg-surface")} onClick={() => selectClip(clip.id)}>
                  <div className="flex items-center justify-between gap-2">
                    <span className="font-mono text-xs">{clip.id}</span>
                    <span className={cn("text-[11px]", clipBizTone(status))}>{clipBizLabel(status)}</span>
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
              {current && shownStage === "finish" ? <span className="shrink-0 font-mono text-xs text-muted">整片定位 {current.id}</span> : null}
              {current && shownStage !== "finish" ? <span className="shrink-0 font-mono text-xs text-muted">{shownStage === "draft" || shownStage === "clips" ? `${current.id} 监视区` : current.id}</span> : null}
              {jobNoteText(job.note) ? <p className="min-w-0 truncate text-xs text-muted">{jobNoteText(job.note)}</p> : null}
            </div>
            <div className="flex shrink-0 items-center gap-2">
              {unsaved ? <span className="text-xs text-warn">有未保存修改</span> : null}
              {batchInFooter ? batchMenu : null}
              {(() => {
                const action = primaryActionFromJob(job);
                if (!["download", "pagemeta", "understand", "script", "precheck", "retry"].includes(action)) return null;
                return (
                  <button type="button" className="rounded-md bg-tungsten px-3 py-1.5 text-sm font-medium text-ink disabled:opacity-50" disabled={primaryDisabled} onClick={() => void runPrimary()}>{primaryActionLabel(job)}</button>
                );
              })()}
            </div>
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
                scriptZh={scriptZh}
                txt={txt}
                seconds={seconds}
                speech={speech}
                mediaSrc={current?.draft.file ? fileUrl(job.id, current.draft.file) : sourceSrc}
                setScriptZh={setScriptZh}
                setSeconds={setSeconds}
                onEdit={editScript}
                onAi={() => { setAiScope(current ? [current.id] : []); setAiPrompt(""); setAiOpen(true); }}
                onPreview={(payload) => void previewRewrite(payload)}
                rewPreview={rewPreview}
                rewBusy={rewBusy}
                saveBusy={saveBusy}
                onConfirm={() => void confirmPreview()}
                onDiscardPreview={() => { if (!rewBusy && !saveBusy) setRewPreview(null); }}
                busy={Boolean(busyAction)}
                unsaved={unsaved}
                produceLocked={generateLocked}
                onProduceDrafts={(ids) => void produceDrafts(ids)}
                onProduceFinals={(ids) => void produceFinals(ids)}
                batch={workspaceBatch}
              />
            ) : null}
            {shownStage === "precheck" ? (
              <PrecheckWorkspace job={job} clips={clips} status={stageDisplayStatus(job, "precheck")} onDetail={() => setDetail("precheck")} />
            ) : null}
            {shownStage === "draft" || shownStage === "clips" ? (
              current ? (
                <VideoMonitor
                  job={job}
                  current={current}
                  preview={preview}
                  setPreview={setPreview}
                  mediaSrc={mediaSrc}
                  sourceReady={sourceReady}
                  tabs={shownStage === "clips" ? (["source", "draft", "final"] as const) : (["source", "draft"] as const)}
                  onEdit={editScript}
                  allowFinal={shownStage === "clips"}
                  produceLocked={generateLocked}
                  onProduceDrafts={(ids) => void produceDrafts(ids)}
                  onProduceFinals={(ids) => void produceFinals(ids)}
                  batch={workspaceBatch}
                />
              ) : (
                <GenerateOverview
                  job={job}
                  clips={clips}
                  phase={shownStage === "clips" ? "clips" : "draft"}
                  produceLocked={generateLocked}
                  onProduceDrafts={(ids) => void produceDrafts(ids)}
                  onProduceFinals={(ids) => void produceFinals(ids)}
                  batch={workspaceBatch}
                />
              )
            ) : null}
            {shownStage === "finish" ? (
              <FinishWorkspace
                job={job}
                clips={clips}
                clipId={clipId}
                finalSrc={finalSrc}
                coverSrc={coverSrc}
                status={stageDisplayStatus(job, "finish")}
                produceLocked={generateLocked}
                onAssemble={() => void assembleFilm()}
                batch={workspaceBatch}
              />
            ) : null}
          </section>
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
          <ClipPicker clips={clips} picked={picked} setPicked={setPicked} recommended={recommendedIds(batchKind)} showOthers={showOthers} setShowOthers={setShowOthers} target={batchKind === "videos" ? videoTarget : "draft"} kind={batchKind} />
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
          <p className="mb-3 text-sm text-muted">描述你希望怎么改。确认后先生成预览，核对无误再保存；不会立刻覆盖原脚本，也不会自动生成视频。</p>
          <textarea className="h-32 w-full rounded-md border border-line bg-panel p-3 text-sm" value={aiPrompt} onChange={(event) => setAiPrompt(event.target.value)} placeholder="例如：加快镜头节奏，保留人物外观和原对白。" />
          {aiScope.length > 1 ? <p className="mt-3 text-xs text-warn">批量 AI 修改尚未接入，请先对单个片段操作。</p> : null}
          <div className="mt-5 flex justify-end gap-2">
            <button type="button" className="rounded-md px-3 py-2 text-sm text-muted" onClick={() => setAiOpen(false)}>取消</button>
            <button
              type="button"
              className="rounded-md bg-tungsten px-4 py-2 text-sm text-ink disabled:opacity-50"
              disabled={!aiPrompt.trim() || aiScope.length > 1 || rewBusy}
              onClick={() => { setAiOpen(false); if (clipId) void previewRewrite({ requirement: aiPrompt }); }}
            >
              {rewBusy ? "生成中..." : "生成预览"}
            </button>
          </div>
        </Dialog>
      ) : null}

      {detail === "understand" ? (
        <Dialog title="视频理解详情" onClose={() => setDetail(null)} className="max-w-4xl">
          <UnderstandDetail docs={understand} />
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
          <p className="text-sm text-muted">{jobNoteText(job.note) || "任务正在处理。"}</p>
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
  const [importing, setImporting] = useState(false);
  const [importMsg, setImportMsg] = useState("");
  const [importMsgWarn, setImportMsgWarn] = useState(false);
  const [importErr, setImportErr] = useState("");
  const authFail = Boolean(job.download_auth_cookie);

  async function importCookie() {
    setImportErr("");
    setImportMsg("");
    setImportMsgWarn(false);
    setImporting(true);
    try {
      const next = (await api.importDouyinCookie(false)) as {
        douyin_cookie_from_env?: boolean;
        douyin_cookie_imported_via?: string;
      };
      if (next.douyin_cookie_from_env) {
        setImportMsgWarn(true);
        setImportMsg("已写入本机配置，但当前仍使用环境变量，下载不会用到这次导入。请先清掉 VRS_DOUYIN_COOKIE 再重试。");
      } else {
        setImportMsg("Cookie 已导入。请点「重试」继续下载，不会自动续跑。");
      }
    } catch (e) {
      setImportErr((e as Error).message);
    } finally {
      setImporting(false);
    }
  }

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
      {job.stages?.download?.error ? <p className="mt-3 shrink-0 rounded-md border border-bad/40 bg-bad/10 p-3 text-xs text-bad">{job.stages.download.error}</p> : null}
      {authFail ? (
        <div className="mt-3 shrink-0 space-y-2">
          {job.douyin_cookie_from_env ? <p className="text-xs text-warn">当前实际使用环境变量 VRS_DOUYIN_COOKIE，一键导入写到本机配置不会覆盖它。</p> : null}
          <button
            type="button"
            className="rounded bg-tungsten px-3 py-1.5 text-sm text-ink disabled:opacity-40"
            disabled={importing}
            onClick={() => void importCookie()}
          >
            {importing ? "等待登录…" : "一键导入 Cookie"}
          </button>
          {importMsg ? <p className={cn("text-xs", importMsgWarn ? "text-warn" : "text-ok")}>{importMsg}</p> : null}
          {importErr ? <p className="text-xs text-bad">{importErr}</p> : null}
        </div>
      ) : null}
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

function useNow(active: boolean) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return;
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, [active]);
  return now;
}

function stepElapsed(startedAt: string | null | undefined, now: number) {
  if (!startedAt) return "";
  const t = Date.parse(startedAt);
  if (!Number.isFinite(t)) return "";
  return fmtDur((now - t) / 1000);
}

function UnderstandWorkspace({
  job, summary, status, onDetail,
}: {
  job: JobDetailData;
  summary: UnderstandDocs | null;
  status: string;
  onDetail: () => void;
}) {
  const live = ["running", "waiting", "failed"].includes((status || "").toLowerCase());
  const steps = job.understand_progress?.steps || [];
  const showChecklist = live && steps.length > 0;
  const current = steps.find((step) => ["active", "waiting", "failed"].includes(step.status));
  const progressDetail = (job.understand_progress?.detail || "").trim();
  const detail = progressDetail || (current?.status === "failed" ? (job.stages?.understand?.error || "").trim() : "");
  const now = useNow(showChecklist);
  const elapsed = stepElapsed(job.understand_progress?.step_started_at, now);
  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-hidden p-5">
      <div className="flex items-center justify-between gap-3">
        <h3 className="font-medium">{showChecklist ? "理解进度" : "理解摘要"}</h3>
        <div className="flex items-center gap-3">
          <span className={cn("text-xs", stageTone(status))}>{understandChip(job, status)}</span>
          <button type="button" className="text-xs text-tungsten hover:underline" onClick={onDetail}>查看详情</button>
        </div>
      </div>
      {showChecklist ? (
        <ol className="mt-4 space-y-2">
          {steps.map((step) => {
            const active = current?.id === step.id;
            const count = step.windows != null ? `${step.window ?? 0}/${step.windows}` : "";
            const bits: string[] = [];
            if (active && elapsed) bits.push(`已用 ${elapsed}`);
            if (active && step.id === "beats" && step.window_t0 != null && step.window_t1 != null) {
              bits.push(`${fmtDur(step.window_t0)}–${fmtDur(step.window_t1)}`);
            }
            if (active && step.id === "beats" && step.window_failed != null) bits.push(`失败 ${step.window_failed}`);
            return (
              <li key={step.id} className="rounded-md border border-line/70 px-3 py-2">
                <div className="flex items-center gap-2 text-sm">
                  <span
                    className={cn(
                      "size-1.5 shrink-0 rounded-full",
                      step.status === "done" && "bg-ok",
                      step.status === "active" && "bg-tungsten",
                      step.status === "waiting" && "bg-warn",
                      step.status === "failed" && "bg-bad",
                      step.status === "pending" && "bg-line",
                    )}
                  />
                  <span className={cn("font-medium", step.status === "pending" ? "text-muted" : "text-text")}>{step.label}</span>
                  {count ? <span className="font-mono text-xs text-muted">{count}</span> : null}
                  {step.status === "waiting" ? <span className="text-xs text-warn">等待</span> : null}
                  {step.status === "failed" ? <span className="text-xs text-bad">失败</span> : null}
                </div>
                {active && bits.length ? (
                  <p className="mt-1.5 font-mono text-xs leading-5 text-muted">{bits.join(" · ")}</p>
                ) : null}
                {active && detail ? (
                  <p className={cn("mt-1.5 text-xs leading-5", step.status === "failed" ? "text-bad" : "text-muted")}>{detail}</p>
                ) : null}
              </li>
            );
          })}
        </ol>
      ) : (
        <>
          <div className="mt-4 grid shrink-0 gap-2 sm:grid-cols-2 lg:grid-cols-4">
            <Metric label="时长" value={fmtDur(summary?.duration ?? job.source?.probe?.duration)} />
            <Metric label="镜头" value={String(summary?.shots ?? "—")} />
            <Metric label="事件" value={String(summary?.events ?? "—")} />
            <Metric label="对白" value={String(summary?.speech ?? "—")} />
          </div>
          <p className="mt-4 text-sm leading-6 text-muted">完整拍表、识别结果和 JSON 放在详情弹窗里，不在中间工作区展开。</p>
          {job.stages?.understand?.error ? <p className="mt-3 rounded-md border border-bad/40 bg-bad/10 p-3 text-xs text-bad">{job.stages.understand.error}</p> : null}
        </>
      )}
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

function WorkspaceActionBar({ batch, extra, primary }: { batch?: ReactNode; extra?: ReactNode; primary?: ReactNode }) {
  if (!batch && !extra && !primary) return null;
  return (
    <div className="flex shrink-0 flex-wrap items-center justify-end gap-2">
      {batch}
      {extra}
      {primary}
    </div>
  );
}

function ClipProduceButtons({
  clip,
  allowFinal,
  locked,
  blockedReason,
  onDraft,
  onFinal,
}: {
  clip: ClipRow;
  allowFinal: boolean;
  locked: boolean;
  blockedReason?: string;
  onDraft: (ids: string[]) => void;
  onFinal: (ids: string[]) => void;
}) {
  const needDraft = clipNeedsDraft(clip);
  const needFinal = clipNeedsFinal(clip);
  const draftGood = clip.draft.status === "done" && !clip.draft.dirty;
  const finalGood = clip.final.status === "done" && !clip.final.dirty;
  function redoDraft() {
    if (!window.confirm("这段试片已经可用，重做会覆盖现有文件。确定继续？")) return;
    onDraft([clip.id]);
  }
  function redoFinal() {
    if (!window.confirm("这段成片已经可用，重做会覆盖现有文件。确定继续？")) return;
    onFinal([clip.id]);
  }
  return (
    <div className="flex flex-wrap items-center gap-2">
      {needDraft ? (
        <button type="button" className="rounded-md bg-tungsten px-3 py-1.5 text-sm font-medium text-ink disabled:opacity-50" disabled={locked} title={blockedReason} onClick={() => onDraft([clip.id])}>生成本段试片</button>
      ) : draftGood ? (
        <button type="button" className="rounded-md border border-line px-3 py-1.5 text-sm text-muted hover:border-tungsten/60 hover:text-text disabled:opacity-50" disabled={locked} title={blockedReason} onClick={redoDraft}>重做本段试片</button>
      ) : null}
      {allowFinal && needFinal ? (
        <button type="button" className="rounded-md bg-tungsten px-3 py-1.5 text-sm font-medium text-ink disabled:opacity-50" disabled={locked} title={blockedReason} onClick={() => onFinal([clip.id])}>生成本段成片</button>
      ) : null}
      {allowFinal && finalGood ? (
        <button type="button" className="rounded-md border border-line px-3 py-1.5 text-sm text-muted hover:border-tungsten/60 hover:text-text disabled:opacity-50" disabled={locked} title={blockedReason} onClick={redoFinal}>重做本段成片</button>
      ) : null}
    </div>
  );
}

function GenerateOverview({
  job, clips, phase, produceLocked, onProduceDrafts, onProduceFinals, batch,
}: {
  job: JobDetailData;
  clips: ClipRow[];
  phase: "draft" | "clips";
  produceLocked: boolean;
  onProduceDrafts: (ids: string[]) => void;
  onProduceFinals: (ids: string[]) => void;
  batch?: ReactNode;
}) {
  const quality = phase === "clips" ? "final" : "draft";
  const pending = clips.filter((clip) => clipBiz(clip, quality) === "pending").length;
  const ready = clips.filter((clip) => clipBiz(clip, quality) === "ready").length;
  const failed = clips.filter((clip) => clipBiz(clip, quality) === "failed").length;
  const stale = clips.filter((clip) => clipBiz(clip, quality) === "stale").length;
  const needIds = phase === "draft" ? clips.filter(clipNeedsDraft).map((clip) => clip.id) : clips.filter(clipNeedsFinal).map((clip) => clip.id);
  const confirmHint = phase === "draft" && clips.length > 0 && clips.every((clip) => !clipNeedsDraft(clip))
    ? "各段试片已齐。到「成片」页生成各段成片，或点左侧片段逐段确认。"
    : phase === "clips" && clipsReadyToAssemble(job)
      ? "各段成片已齐。到「拼接成片」页拼接整片。"
      : null;
  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-hidden">
      <div className="flex min-h-0 flex-1 flex-col overflow-hidden p-5 pb-3">
        <h3 className="font-medium">{phase === "clips" ? "成片概览" : "试片概览"}</h3>
        <div className="mt-4 grid shrink-0 gap-2 sm:grid-cols-4">
          <Metric label="已生成" value={String(ready)} />
          <Metric label="待生成" value={String(pending)} />
          <Metric label="脚本已改" value={String(stale)} />
          <Metric label="失败" value={String(failed)} />
        </div>
        {confirmHint ? <p className="mt-4 rounded-md border border-tungsten/40 bg-tungsten/10 p-3 text-sm text-tungsten">{confirmHint}</p> : null}
        <p className="mt-4 text-sm text-muted">点左侧片段进入该段监视区，再点一次可回到全部概览。需要只处理某几段时，用下方「批量操作」。</p>
        <div className="mt-4 min-h-0 flex-1 overflow-y-auto">
          {clips.map((clip) => {
            const status = clipBiz(clip, quality);
            return (
              <div key={clip.id} className="mb-2 flex items-center justify-between rounded-lg border border-line px-3 py-2 text-sm">
                <span className="font-mono text-xs">{clip.id}</span>
                <span className="text-xs text-muted">试片 {stageStatusLabel(clip.draft.status)} · 成片 {stageStatusLabel(clip.final.status)}</span>
                <span className={cn("text-xs", clipBizTone(status))}>{clipBizLabel(status)}</span>
              </div>
            );
          })}
          {!clips.length ? <p className="text-sm text-muted">{jobNoteText(job.note) || "还没有片段。"}</p> : null}
        </div>
      </div>
      <div className="shrink-0 border-t border-line px-5 py-2">
        <WorkspaceActionBar
          batch={batch}
          primary={(
            <button
              type="button"
              className="rounded-md bg-tungsten px-4 py-2 text-sm font-medium text-ink disabled:opacity-50"
              disabled={produceLocked || !needIds.length}
              title={!needIds.length ? (phase === "clips" ? "各段成片已齐，或还有试片未就绪" : "各段试片已齐") : undefined}
              onClick={() => (phase === "clips" ? onProduceFinals(needIds) : onProduceDrafts(needIds))}
            >
              {phase === "clips" ? "生成全部成片" : "生成全部试片"}
            </button>
          )}
        />
      </div>
    </div>
  );
}

function VideoMonitor({
  job, current, preview, setPreview, mediaSrc, sourceReady, tabs, onEdit, allowFinal, produceLocked, onProduceDrafts, onProduceFinals, batch,
}: {
  job: JobDetailData;
  current: ClipRow;
  preview: PreviewKind;
  setPreview: (value: PreviewKind) => void;
  mediaSrc?: string;
  sourceReady: boolean;
  tabs: readonly PreviewKind[];
  onEdit: () => void;
  allowFinal: boolean;
  produceLocked: boolean;
  onProduceDrafts: (ids: string[]) => void;
  onProduceFinals: (ids: string[]) => void;
  batch?: ReactNode;
}) {
  const stale = current.draft.dirty || current.final.dirty;
  return (
    <div className="flex min-h-0 flex-1 flex-col p-4">
      <div className="mb-3 flex shrink-0 items-center gap-1 rounded-lg bg-panel p-1">
        {tabs.map((kind) => {
          const available = kind === "source" ? sourceReady : Boolean(current[kind]?.file);
          return <button key={kind} type="button" disabled={!available} className={cn("rounded-md px-3 py-1.5 text-sm", preview === kind ? "bg-surface text-text shadow-sm" : "text-muted", !available && "opacity-40")} onClick={() => setPreview(kind)}>{kind === "source" ? "原片" : kind === "draft" ? "试片" : "成片"}</button>;
        })}
      </div>
      <div className="min-h-0 flex-1 overflow-hidden rounded-lg bg-black">
        {mediaSrc ? <video key={mediaSrc} className="h-full w-full object-contain" controls src={mediaSrc} /> : <div className="flex h-full items-center justify-center px-6 text-center text-sm text-muted">{preview === "source" ? "原片还没有准备好" : preview === "draft" ? "试片还没有生成" : "成片还没有生成"}</div>}
      </div>
      <div className="mt-3 flex shrink-0 flex-wrap items-center gap-2 text-xs text-muted">
        <span>{fmtTime(current.t0)} - {fmtTime(current.t1)} · 生成 {fmtTime(current.h3_seconds)}</span>
        <span>试片 {stageStatusLabel(current.draft.status)}{tabs.includes("final") ? ` · 成片 ${stageStatusLabel(current.final.status)}` : ""}{stale ? " · 当前视频仍是旧版本" : ""}</span>
        {job.options?.review_mode === "full_auto" ? <span>自动交付</span> : null}
      </div>
      <div className="mt-3 shrink-0">
        <WorkspaceActionBar
          batch={batch}
          extra={<button type="button" className="rounded-md border border-line px-3 py-1.5 text-sm text-muted hover:border-tungsten/60 hover:text-text" onClick={onEdit}>编辑脚本</button>}
          primary={<ClipProduceButtons clip={current} allowFinal={allowFinal} locked={produceLocked} onDraft={onProduceDrafts} onFinal={onProduceFinals} />}
        />
      </div>
    </div>
  );
}

function clipPlaySeconds(clip: ClipRow) {
  const source = Number(clip.source_seconds);
  const h3 = Number(clip.h3_seconds);
  const span = Number(clip.t1) - Number(clip.t0);
  if (!Number.isFinite(source) || source <= 0.05) return Number.isFinite(h3) && h3 > 0 ? h3 : Math.max(0, span);
  if (Number.isFinite(h3) && h3 > 0) return Math.min(source, h3);
  return source;
}

function concatStart(clips: ClipRow[], clipId: string | null) {
  let offset = 0;
  for (const clip of clips) {
    const play = clipPlaySeconds(clip);
    if (clip.id === clipId) return offset;
    offset += play;
  }
  return 0;
}

function FinishWorkspace({
  job, clips, clipId, finalSrc, coverSrc, status, produceLocked, onAssemble, batch,
}: {
  job: JobDetailData;
  clips: ClipRow[];
  clipId: string | null;
  finalSrc?: string;
  coverSrc?: string;
  status: string;
  produceLocked: boolean;
  onAssemble: () => void;
  batch?: ReactNode;
}) {
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const report = job.finish_report;
  const concatDone = status === "done" || Boolean(report?.concat);
  const assCount = Number(report?.ass_events || 0);
  useEffect(() => {
    const node = videoRef.current;
    if (!node || !finalSrc || !clipId) return;
    const start = concatStart(clips, clipId);
    const seek = () => {
      try {
        node.currentTime = start;
      } catch {
        /* ignore seek before metadata */
      }
    };
    if (node.readyState >= 1) seek();
    else node.addEventListener("loadedmetadata", seek, { once: true });
    return () => node.removeEventListener("loadedmetadata", seek);
  }, [clipId, clips, finalSrc]);
  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-hidden">
      <div className="flex min-h-0 flex-1 flex-col overflow-hidden p-4 pb-3">
        <div className="mb-3 flex shrink-0 items-center justify-between gap-3">
          <h3 className="font-medium">拼接成片</h3>
          <span className={cn("text-xs", stageTone(status))}>{stageStatusLabel(status)}</span>
        </div>
        <div className="mb-3 grid shrink-0 gap-2 sm:grid-cols-3">
          <Metric label="裁切拼接" value={concatDone ? `${report?.clips ?? clips.length} 段` : "未开始"} />
          <Metric label="烧字" value={status === "done" ? (report?.ass_burn ? `${assCount} 条` : "未烧字") : "未开始"} />
          <Metric label="封面" value={coverSrc ? String(report?.cover || "已生成") : "未开始"} />
        </div>
        <div className="grid min-h-0 flex-1 gap-3 overflow-hidden lg:grid-cols-[minmax(0,1fr)_12rem]">
          <div className="min-h-0 overflow-hidden rounded-lg bg-black">
            {finalSrc ? <video ref={videoRef} key={finalSrc} className="h-full w-full object-contain" controls src={finalSrc} /> : <div className="flex h-full items-center justify-center px-6 text-center text-sm text-muted">各段成片确认后，点「拼接成片」才会出现整片。</div>}
          </div>
          <div className="hidden min-h-0 flex-col overflow-hidden lg:flex">
            {coverSrc ? <img src={coverSrc} alt="封面" className="h-full w-full rounded-lg object-cover" /> : <div className="flex h-full items-center justify-center rounded-lg border border-dashed border-line text-xs text-muted">暂无封面</div>}
          </div>
        </div>
      </div>
      <div className="shrink-0 border-t border-line px-4 py-2">
        <WorkspaceActionBar
          batch={batch}
          extra={(
            <>
              {job.media?.final ? <a className="rounded-md border border-line bg-surface px-3 py-1.5 text-sm hover:underline" href={fileUrl(job.id, job.media.final)}>下载整片</a> : null}
              {job.media?.cover ? <a className="rounded-md border border-line bg-surface px-3 py-1.5 text-sm hover:underline" href={fileUrl(job.id, job.media.cover)}>下载封面</a> : null}
              {job.media?.ass ? <a className="rounded-md border border-line bg-surface px-3 py-1.5 text-sm hover:underline" href={fileUrl(job.id, job.media.ass)}>下载字幕</a> : null}
            </>
          )}
          primary={
            job.state === "done" ? (
              <button
                type="button"
                className="rounded-md border border-line bg-surface px-4 py-2 text-sm font-medium hover:underline disabled:opacity-50"
                disabled={produceLocked}
                onClick={onAssemble}
                title="会重新跑一遍拼接"
              >
                重新拼接
              </button>
            ) : (
              <button
                type="button"
                className="rounded-md bg-tungsten px-4 py-2 text-sm font-medium text-ink disabled:opacity-50"
                disabled={produceLocked || !clipsReadyToAssemble(job)}
                title={!clipsReadyToAssemble(job) ? "各段成片就绪且脚本未改动后才能拼接" : undefined}
                onClick={onAssemble}
              >
                拼接成片
              </button>
            )
          }
        />
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
  current, editing, scriptZh, txt, seconds, speech, mediaSrc, setScriptZh, setSeconds, onEdit, onAi, onPreview, rewPreview, rewBusy, saveBusy, onConfirm, onDiscardPreview, busy, unsaved, produceLocked, onProduceDrafts, onProduceFinals, batch,
}: {
  current: ClipRow | null;
  editing: boolean;
  scriptZh: string;
  txt: string;
  seconds: string;
  speech: SpeechLine[];
  mediaSrc?: string;
  setScriptZh: (value: string) => void;
  setSeconds: (value: string) => void;
  onEdit: () => void;
  onAi: () => void;
  onPreview: (payload: Record<string, unknown>) => void;
  rewPreview: RewPreview | null;
  rewBusy: boolean;
  saveBusy: boolean;
  onConfirm: () => void;
  onDiscardPreview: () => void;
  busy: boolean;
  unsaved: boolean;
  produceLocked: boolean;
  onProduceDrafts: (ids: string[]) => void;
  onProduceFinals: (ids: string[]) => void;
  batch?: ReactNode;
}) {
  const wide = useWideLayout();
  const layoutId = wide ? "vrs-script-h" : "vrs-script-v";
  const { defaultLayout, onLayoutChanged } = useDefaultLayout({ id: layoutId });

  if (!current) {
    return (
      <div className="flex min-h-0 flex-1 flex-col overflow-hidden">
        <div className="flex min-h-0 flex-1 items-center justify-center p-6 text-sm text-muted">从左侧选择一个片段后，这里显示该片段的脚本。</div>
        {batch ? (
          <div className="shrink-0 border-t border-line px-4 py-2">
            <WorkspaceActionBar batch={batch} />
          </div>
        ) : null}
      </div>
    );
  }
  if (!editing) {
    return (
      <div className="flex min-h-0 flex-1 flex-col overflow-hidden">
        <div className="flex min-h-0 flex-1 flex-col items-start justify-center p-6">
          <p className="text-sm text-muted">{current.id} 当前是已生成结果。要改脚本请进入编辑，不会立刻重新生成视频。</p>
        </div>
        <div className="shrink-0 border-t border-line px-4 py-2">
          <WorkspaceActionBar
            batch={batch}
            extra={<button type="button" className="rounded-md border border-line px-3 py-1.5 text-sm text-muted hover:border-tungsten/60 hover:text-text" onClick={onEdit}>编辑脚本</button>}
            primary={<ClipProduceButtons clip={current} allowFinal locked={produceLocked} onDraft={onProduceDrafts} onFinal={onProduceFinals} />}
          />
        </div>
      </div>
    );
  }

  const working = rewBusy || saveBusy;
  const editorLocked = working || Boolean(rewPreview);
  const generateLabel = rewBusy ? "生成中..." : "生成英文脚本";

  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-hidden" aria-busy={working}>
      <div className="flex shrink-0 items-center justify-between border-b border-line px-4 py-2">
        <div>
          <p className="font-mono text-sm">{current.id}</p>
          <p className="text-xs text-muted">{fmtTime(current.t0)} - {fmtTime(current.t1)}{current.draft.dirty ? " · 当前视频仍是旧版本" : ""}</p>
        </div>
        <div className="flex gap-2">
          <button type="button" className="rounded-md border border-line px-3 py-1.5 text-sm text-muted disabled:cursor-not-allowed disabled:opacity-50" disabled={busy || editorLocked} onClick={onAi}>AI 修改脚本</button>
          <button type="button" className="inline-flex items-center gap-2 rounded-md bg-tungsten px-3 py-1.5 text-sm text-ink disabled:cursor-wait disabled:opacity-70" disabled={busy || editorLocked} onClick={() => onPreview({ script_zh: scriptZh })}>
            {rewBusy ? <span className="size-3.5 shrink-0 animate-spin rounded-full border-2 border-ink/25 border-t-ink" aria-hidden /> : null}
            {generateLabel}
          </button>
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
            <div className="grid min-h-0 flex-1 grid-rows-[auto_minmax(0,1fr)_minmax(0,0.55fr)] gap-3 overflow-hidden p-4">
              <label className="block text-xs text-muted">H3 时长（秒）<input className="mt-1 w-full rounded-md border border-line bg-panel p-2 text-sm text-text disabled:cursor-not-allowed disabled:opacity-60" value={seconds} disabled={editorLocked} onChange={(event) => setSeconds(event.target.value)} /></label>
              <label className="relative flex min-h-0 flex-col text-xs text-muted">
                {rewBusy ? "中文脚本（生成中，已锁定）" : saveBusy ? "中文脚本（保存中，已锁定）" : rewPreview ? "中文脚本（请先确认英文预览）" : "中文脚本（唯一可编辑）"}
                <textarea
                  className={cn("mt-1 min-h-0 flex-1 resize-none rounded-md border border-line bg-panel p-3 text-sm leading-5 text-text", editorLocked ? "cursor-not-allowed opacity-60" : "")}
                  value={scriptZh}
                  readOnly={editorLocked}
                  disabled={editorLocked}
                  onChange={(event) => { if (!editorLocked) setScriptZh(event.target.value); }}
                />
                {working ? (
                  <div className="absolute inset-x-0 bottom-0 top-5 z-10 flex items-center justify-center rounded-md bg-panel/80 text-sm text-text">
                    <span className="mr-2 size-4 animate-spin rounded-full border-2 border-line border-t-tungsten" aria-hidden />
                    {rewBusy ? "正在根据中文脚本生成英文…" : "正在保存英文脚本…"}
                  </div>
                ) : null}
              </label>
              <div className="flex min-h-0 flex-col text-xs text-muted">喂给 H3 的英文（由程序生成，只读）
                <div className="mt-1 min-h-0 flex-1 overflow-auto rounded-md border border-line bg-panel/50 p-3 font-mono text-xs leading-5 text-muted">
                  <pre className="whitespace-pre-wrap">{rewPreview ? rewPreview.prompt_txt : txt}</pre>
                </div>
              </div>
            </div>
            <div className="max-h-20 shrink-0 overflow-y-auto border-t border-line px-4 py-2 text-xs text-muted">
              {speech.length ? speech.map((line) => <p key={`${line.t0}-${line.text}`}>{fmtTime(line.t0)} {line.text}</p>) : "本段没有对白"}
            </div>
          </div>
        </ResizablePanel>
      </ResizablePanelGroup>

      <div className="shrink-0 border-t border-line px-4 py-2">
        <WorkspaceActionBar
          batch={batch}
          primary={(
            <ClipProduceButtons
              clip={current}
              allowFinal
              locked={produceLocked || working || Boolean(rewPreview) || unsaved}
              blockedReason={unsaved ? "请先保存脚本" : rewPreview ? "请先确认或放弃英文预览" : undefined}
              onDraft={onProduceDrafts}
              onFinal={onProduceFinals}
            />
          )}
        />
      </div>

      {rewPreview ? (
        <div className="shrink-0 border-t border-warn/40 bg-warn/10 px-4 py-3">
          <p className="text-sm font-medium text-warn">已生成新的英文脚本，请预览中文对照后确认</p>
          <div className="mt-2 max-h-48 overflow-y-auto rounded-md border border-line bg-surface p-3">
            <p className="mb-2 text-xs font-medium text-muted">中文脚本（确认后写入编辑区）</p>
            <pre className="whitespace-pre-wrap text-xs leading-5 text-text">{rewPreview.script_zh}</pre>
          </div>
          <div className="mt-3 flex justify-end gap-2">
            <button type="button" className="rounded-md px-3 py-2 text-sm text-muted disabled:opacity-50" disabled={working} onClick={onDiscardPreview}>放弃</button>
            <button type="button" className="inline-flex items-center gap-2 rounded-md bg-tungsten px-4 py-2 text-sm text-ink disabled:cursor-wait disabled:opacity-70" disabled={busy || working} onClick={onConfirm}>
              {saveBusy ? <span className="size-3.5 shrink-0 animate-spin rounded-full border-2 border-ink/25 border-t-ink" aria-hidden /> : null}
              {saveBusy ? "保存中..." : "确认并保存"}
            </button>
          </div>
        </div>
      ) : null}
    </div>
  );
}

function ClipPicker({
  clips, picked, setPicked, recommended, showOthers, setShowOthers, target, kind,
}: {
  clips: ClipRow[];
  picked: string[];
  setPicked: (ids: string[]) => void;
  recommended: string[];
  showOthers: boolean;
  setShowOthers: (value: boolean) => void;
  target: PreviewKind;
  kind: Exclude<BatchKind, null>;
}) {
  const quality: "draft" | "final" = target === "final" ? "final" : "draft";
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
    const status = clipBiz(clip, quality);
    if (recommended.includes(clip.id) && status === "stale") return "脚本已改，推荐重新生成";
    if (recommended.includes(clip.id) && status === "pending") return "还没有对应视频，推荐生成";
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
          <span className="flex items-center justify-between gap-2"><span className="font-mono text-sm">{clip.id}</span><span className={cn("text-xs", recItem ? "text-tungsten" : "text-muted")}>{clipBizLabel(clipBiz(clip, quality))}</span></span>
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
