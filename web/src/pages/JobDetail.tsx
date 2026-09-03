import { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { Panel, PanelGroup, PanelResizeHandle } from "react-resizable-panels";
import { api } from "../lib/api";
import { cn, fileUrl, fmtTime } from "../lib/utils";

const STAGES = ["download", "pagemeta", "understand", "script", "precheck", "generate", "finish"] as const;
const STAGE_LABEL: Record<string, string> = {
  download: "下载",
  pagemeta: "页面",
  understand: "理解",
  script: "脚本",
  precheck: "预检",
  generate: "生成",
  finish: "交付",
};

type ClipRow = {
  id: string;
  t0: number;
  t1: number;
  h3_seconds: number;
  padded?: boolean;
  draft: { status: string; dirty: boolean; file?: string | null };
  final: { status: string; dirty: boolean };
};

type JobDetailData = {
  id: string;
  state: string;
  stage: string;
  note?: string;
  running?: boolean;
  need_aspect_confirm?: boolean;
  source_aspect?: string;
  options?: { aspect_ratio?: string; review_mode?: string; generate_path?: string };
  stages?: Record<string, { status: string; error?: string | null }>;
  clips?: ClipRow[];
  dirty_clip_ids?: string[];
  media?: { source?: string | null; draft?: string | null; final?: string | null };
  events?: { at: string; kind: string; clip_id?: string; detail?: string; quality?: string }[];
  precheck?: { ok?: boolean; errors?: string[]; warnings?: string[] };
  clips_json?: unknown;
};

export default function JobDetail() {
  const { id = "" } = useParams();
  const nav = useNavigate();
  const [job, setJob] = useState<JobDetailData | null>(null);
  const [clipId, setClipId] = useState<string | null>(null);
  const [editor, setEditor] = useState<Record<string, unknown> | null>(null);
  const [tab, setTab] = useState<"form" | "json" | "all">("form");
  const [preview, setPreview] = useState<"source" | "draft" | "final">("source");
  const [jsonText, setJsonText] = useState("");
  const [allText, setAllText] = useState("");
  const [txt, setTxt] = useState("");
  const [md, setMd] = useState("");
  const [seconds, setSeconds] = useState("");
  const [err, setErr] = useState("");
  const [msg, setMsg] = useState("");

  const refresh = useCallback(async () => {
    const data = (await api.job(id)) as JobDetailData;
    setJob(data);
    setClipId((cur) => cur || data.clips?.[0]?.id || null);
  }, [id]);

  useEffect(() => {
    refresh().catch((e: Error) => setErr(e.message));
  }, [refresh]);

  useEffect(() => {
    if (!id) return;
    const es = new EventSource(`/api/jobs/${id}/events`);
    es.onmessage = (ev) => {
      try {
        const data = JSON.parse(ev.data) as JobDetailData;
        setJob(data);
      } catch {
        /* ignore */
      }
    };
    return () => es.close();
  }, [id]);

  useEffect(() => {
    if (!id || !clipId) return;
    api
      .clip(id, clipId)
      .then((raw) => {
        const data = raw as {
          prompt_json: unknown;
          prompt_txt: string;
          review_md: string;
          clip: { h3_seconds: number };
        };
        setEditor(raw as Record<string, unknown>);
        setTxt(data.prompt_txt || "");
        setMd(data.review_md || "");
        setSeconds(String(data.clip?.h3_seconds ?? ""));
        setJsonText(JSON.stringify(data.prompt_json ?? {}, null, 2));
      })
      .catch((e: Error) => setErr(e.message));
  }, [id, clipId]);

  useEffect(() => {
    if (job?.clips_json) setAllText(JSON.stringify(job.clips_json, null, 2));
  }, [job?.clips_json]);

  const mediaSrc = useMemo(() => {
    if (!job) return undefined;
    const rel = job.media?.[preview];
    return fileUrl(job.id, rel || (preview === "source" ? job.media?.source : null));
  }, [job, preview]);

  const current = job?.clips?.find((c) => c.id === clipId);
  const dirty = job?.dirty_clip_ids || [];
  const draftsReady = Boolean(job?.clips?.length && job.clips.every((c) => c.draft.status === "done"));

  async function save() {
    if (!clipId) return;
    setErr("");
    setMsg("");
    try {
      if (tab === "all") {
        await api.saveScript(id, { clips: JSON.parse(allText) });
      } else if (tab === "json") {
        await api.saveClip(id, clipId, { prompt_json: JSON.parse(jsonText), review_md: md, h3_seconds: Number(seconds) });
      } else {
        await api.saveClip(id, clipId, {
          prompt_txt: txt,
          review_md: md,
          h3_seconds: Number(seconds),
        });
      }
      setMsg("已保存并预检");
      await refresh();
    } catch (e) {
      setErr((e as Error).message);
    }
  }

  if (!job) return <div className="p-6 text-muted">{err || "加载任务…"}</div>;

  return (
    <div className="flex h-full flex-col">
      {job.need_aspect_confirm ? (
        <div className="border-b border-warn/40 bg-warn/10 px-4 py-2 text-sm">
          原片 {job.source_aspect}，默认输出 {job.options?.aspect_ratio || "16:9"}。
          <button className="ml-3 text-tungsten" onClick={() => api.aspect(id, false).then(() => api.resume(id)).then(refresh)}>
            保持 16:9
          </button>
          <button className="ml-3 text-tungsten" onClick={() => api.aspect(id, true).then(() => api.resume(id)).then(refresh)}>
            跟原片
          </button>
        </div>
      ) : null}
      <div className="flex items-center justify-between border-b border-line px-4 py-2 text-sm">
        <div>
          <span className="font-mono">{job.id}</span>
          <span className="ml-3 text-muted">
            {job.state} · {STAGE_LABEL[job.stage] || job.stage}
          </span>
        </div>
        <p className="max-w-xl truncate text-xs text-muted" title={job.note}>
          {job.note}
        </p>
      </div>
      <PanelGroup orientation="horizontal" className="min-h-0 flex-1">
        <Panel defaultSize={22} minSize={16} className="overflow-y-auto border-r border-line p-3">
          <h2 className="mb-2 text-xs tracking-wide text-muted">阶段</h2>
          <ol className="mb-4 space-y-1 text-sm">
            {STAGES.map((name) => {
              const rec = job.stages?.[name];
              return (
                <li key={name} className="flex justify-between">
                  <span>{STAGE_LABEL[name]}</span>
                  <span className="text-muted">{rec?.status || "pending"}</span>
                </li>
              );
            })}
          </ol>
          {(job.events || []).length ? (
            <>
              <h2 className="mb-2 text-xs tracking-wide text-muted">看门狗</h2>
              <ul className="mb-4 space-y-1 text-xs text-muted">
                {(job.events || [])
                  .filter((e) => e.kind === "comfy_restart")
                  .map((e, i) => (
                    <li key={i}>
                      {e.at?.slice(11, 19)} 重启 {e.clip_id} {e.detail}
                    </li>
                  ))}
              </ul>
            </>
          ) : null}
          <h2 className="mb-2 text-xs tracking-wide text-muted">片段</h2>
          <ul className="space-y-1">
            {(job.clips || []).map((clip) => (
              <li key={clip.id}>
                <button
                  className={cn(
                    "w-full rounded px-2 py-1 text-left text-sm",
                    clip.id === clipId ? "bg-line text-text" : "text-muted hover:text-text",
                  )}
                  onClick={() => setClipId(clip.id)}
                >
                  <span className="font-mono">{clip.id}</span>
                  <span className="ml-2 text-xs">
                    {clip.draft.status}
                    {clip.draft.dirty ? " ·脏" : ""}
                  </span>
                  <span className="block text-xs text-muted">
                    {fmtTime(clip.t0)}–{fmtTime(clip.t1)} → {fmtTime(clip.h3_seconds)}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        </Panel>
        <PanelResizeHandle className="w-1 bg-line hover:bg-tungsten" />
        <Panel defaultSize={40} minSize={24} className="flex flex-col p-3">
          <div className="mb-2 flex gap-3 text-sm">
            {(["source", "draft", "final"] as const).map((key) => (
              <button key={key} className={preview === key ? "text-tungsten" : "text-muted"} onClick={() => setPreview(key)}>
                {key === "source" ? "原片" : key === "draft" ? "草稿" : "成片"}
              </button>
            ))}
          </div>
          {mediaSrc ? (
            <video key={mediaSrc} className="max-h-[70%] w-full rounded bg-black" controls src={mediaSrc} />
          ) : (
            <div className="flex flex-1 items-center justify-center rounded border border-dashed border-line text-muted">
              还没有 {preview === "source" ? "原片" : preview === "draft" ? "草稿合片" : "成片"}
            </div>
          )}
          {current?.draft.file ? (
            <a className="mt-2 text-xs text-tungsten" href={fileUrl(job.id, current.draft.file)} target="_blank" rel="noreferrer">
              本段草稿
            </a>
          ) : null}
        </Panel>
        <PanelResizeHandle className="w-1 bg-line hover:bg-tungsten" />
        <Panel defaultSize={38} minSize={22} className="flex min-h-0 flex-col p-3">
          <div className="mb-2 flex gap-3 text-sm">
            <button className={tab === "form" ? "text-tungsten" : "text-muted"} onClick={() => setTab("form")}>
              表单
            </button>
            <button className={tab === "json" ? "text-tungsten" : "text-muted"} onClick={() => setTab("json")}>
              本段 JSON
            </button>
            <button className={tab === "all" ? "text-tungsten" : "text-muted"} onClick={() => setTab("all")}>
              全脚本 JSON
            </button>
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto">
            {tab === "form" ? (
              <div className="space-y-2 text-sm">
                <label className="block text-muted">
                  时长网格（秒）
                  <input className="mt-1 w-full rounded border border-line bg-ink p-2 text-text" value={seconds} onChange={(e) => setSeconds(e.target.value)} />
                </label>
                <p className="text-xs text-muted">对白（只读，改提示词里的 &lt;d&gt;）</p>
                <ul className="max-h-24 overflow-y-auto text-xs text-muted">
                  {((editor?.speech as { t0: number; t1: number; text: string }[]) || []).map((s, i) => (
                    <li key={i}>
                      {fmtTime(s.t0)} {s.text}
                    </li>
                  ))}
                </ul>
                <label className="block text-muted">
                  提示词 .txt（喂给 H3；若有 JSON，请改「本段 JSON」再保存以免改岔）
                  <textarea className="mt-1 h-40 w-full rounded border border-line bg-ink p-2 font-mono text-xs text-text" value={txt} onChange={(e) => setTxt(e.target.value)} />
                </label>
                <label className="block text-muted">
                  中文对照 .md
                  <textarea className="mt-1 h-28 w-full rounded border border-line bg-ink p-2 font-mono text-xs text-text" value={md} onChange={(e) => setMd(e.target.value)} />
                </label>
              </div>
            ) : null}
            {tab === "json" ? (
              <textarea className="h-full min-h-64 w-full rounded border border-line bg-ink p-2 font-mono text-xs" value={jsonText} onChange={(e) => setJsonText(e.target.value)} />
            ) : null}
            {tab === "all" ? (
              <textarea className="h-full min-h-64 w-full rounded border border-line bg-ink p-2 font-mono text-xs" value={allText} onChange={(e) => setAllText(e.target.value)} />
            ) : null}
          </div>
          {job.precheck?.errors?.length ? (
            <ul className="mt-2 max-h-20 overflow-y-auto text-xs text-bad">
              {job.precheck.errors.slice(0, 8).map((e) => (
                <li key={e}>{e}</li>
              ))}
            </ul>
          ) : null}
          {err ? <p className="mt-2 text-xs text-bad">{err}</p> : null}
          {msg ? <p className="mt-2 text-xs text-ok">{msg}</p> : null}
          <div className="mt-3 flex flex-wrap gap-2">
            <button
              className="rounded bg-line px-3 py-1.5 text-sm"
              disabled={job.running}
              onClick={() => api.resume(id).then(refresh).catch((e: Error) => setErr(e.message))}
            >
              继续
            </button>
            <button className="rounded bg-line px-3 py-1.5 text-sm" onClick={save} disabled={job.running}>
              保存并预检
            </button>
            <button
              className="rounded bg-line px-3 py-1.5 text-sm"
              disabled={job.running || !job.clips?.length}
              onClick={() => api.draft(id, dirty).then(refresh).catch((e: Error) => setErr(e.message))}
            >
              再出草稿{dirty.length ? `（${dirty.length}）` : ""}
            </button>
            <button
              className="rounded bg-tungsten px-3 py-1.5 text-sm text-ink"
              disabled={job.running || !draftsReady}
              onClick={() => api.finals(id).then(refresh).catch((e: Error) => setErr(e.message))}
            >
              出成片
            </button>
            <button
              className="rounded px-3 py-1.5 text-sm text-bad"
              onClick={() => {
                if (!confirm("打断 Comfy 并放弃？产物保留。")) return;
                api.cancel(id).then(refresh);
              }}
            >
              放弃任务
            </button>
            <button className="text-sm text-muted" onClick={() => nav("/")}>
              返回列表
            </button>
          </div>
        </Panel>
      </PanelGroup>
    </div>
  );
}
