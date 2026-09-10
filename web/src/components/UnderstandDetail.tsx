import { useState, type ReactNode } from "react";
import { cn, fmtDur } from "../lib/utils";

export type UnderstandDocs = {
  shots: number;
  events: number;
  speech: number;
  windows: number;
  duration?: number;
  understanding: Record<string, any> | null;
  shotsDoc: Record<string, any> | null;
  eventsDoc: Record<string, any> | null;
  dialogueDoc: Record<string, any> | null;
  beatsDoc: Record<string, any> | null;
  raw: string;
};

type TabId = "summary" | "speech" | "events" | "beats" | "json";

const TABS: { id: TabId; label: string }[] = [
  { id: "summary", label: "摘要" },
  { id: "speech", label: "对白" },
  { id: "events", label: "事件" },
  { id: "beats", label: "拍表" },
  { id: "json", label: "JSON" },
];

const SOURCE_LABEL: Record<string, string> = {
  asr: "语音",
  ocr: "画面文字",
  "asr-extra": "语音补段",
};

function Empty() {
  return <p className="text-sm text-muted">尚未生成</p>;
}

function emotionLabel(value: unknown): string {
  if (value == null || value === "") return "—";
  if (typeof value === "string") return value;
  if (typeof value === "object" && "label" in (value as object)) {
    const rec = value as { label?: string };
    return rec.label || "—";
  }
  return "—";
}

function vlRate(docs: UnderstandDocs): string {
  const summary = docs.understanding;
  if (summary && typeof summary.visual_success_rate === "number") {
    return `${Math.round(summary.visual_success_rate * 100)}%`;
  }
  const ok = Number(summary?.visual_windows_ok);
  const total = Number(summary?.visual_windows_total || summary?.windows);
  if (Number.isFinite(ok) && Number.isFinite(total) && total > 0) return `${Math.round((ok / total) * 100)}%`;
  const windows = Array.isArray(docs.beatsDoc?.windows) ? docs.beatsDoc.windows : [];
  if (!windows.length) return "—";
  const good = windows.filter((win: { error?: string; raw?: string }) => !win?.error && String(win?.raw || "").trim()).length;
  return `${Math.round((good / windows.length) * 100)}%`;
}

function ExpandRow({ head, children }: { head: ReactNode; children: ReactNode }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="border-b border-line/60">
      <button type="button" className="flex w-full items-start justify-between gap-3 py-2 text-left" onClick={() => setOpen((v) => !v)}>
        <span className="min-w-0 flex-1">{head}</span>
        <span className="shrink-0 text-xs text-muted">{open ? "收起" : "展开"}</span>
      </button>
      {open ? <div className="pb-3 text-xs leading-5 text-muted">{children}</div> : null}
    </div>
  );
}

function SummaryTab({ docs }: { docs: UnderstandDocs }) {
  const has =
    docs.understanding ||
    docs.shots > 0 ||
    docs.events > 0 ||
    docs.speech > 0 ||
    docs.windows > 0 ||
    docs.duration != null;
  if (!has) return <Empty />;
  const vl = String(docs.understanding?.vl_model || "").trim() || "—";
  const llm = String(docs.understanding?.llm_model || "").trim() || "—";
  return (
    <div className="grid gap-2 sm:grid-cols-2">
      <Metric label="时长" value={fmtDur(docs.duration)} />
      <Metric label="镜头" value={String(docs.shots || "—")} />
      <Metric label="事件" value={String(docs.events || "—")} />
      <Metric label="对白" value={String(docs.speech || "—")} />
      <Metric label="拍表成功率" value={vlRate(docs)} />
      <Metric label="视觉模型" value={vl} />
      <Metric label="文本模型" value={llm} />
    </div>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-lg border border-line bg-panel/60 p-3">
      <p className="text-[11px] uppercase tracking-[0.14em] text-muted">{label}</p>
      <p className="mt-1 break-all text-sm font-medium">{value}</p>
    </div>
  );
}

function SpeechTab({ docs }: { docs: UnderstandDocs }) {
  const lines = Array.isArray(docs.dialogueDoc?.speech) ? docs.dialogueDoc.speech : [];
  if (!lines.length) return <Empty />;
  return (
    <div className="space-y-2">
      {lines.map((line: Record<string, any>, index: number) => (
        <div key={`${line.t0}-${index}`} className="rounded-md border border-line/70 px-3 py-2 text-sm">
          <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
            <span className="font-mono text-xs text-muted">
              {fmtDur(line.t0)}–{fmtDur(line.t1)}
            </span>
            <span className="text-xs text-muted">{SOURCE_LABEL[String(line.source || "")] || line.source || "—"}</span>
            <span className="text-xs text-muted">{emotionLabel(line.vocal_emotion)}</span>
          </div>
          <p className="mt-1 leading-6">{String(line.text || "").trim() || "—"}</p>
        </div>
      ))}
    </div>
  );
}

function EventsTab({ docs }: { docs: UnderstandDocs }) {
  const events = Array.isArray(docs.eventsDoc?.events) ? docs.eventsDoc.events : [];
  if (!events.length) return <Empty />;
  return (
    <div>
      {events.map((event: Record<string, any>, index: number) => {
        const title = String(event.summary || event.id || event.kind || `事件 ${index + 1}`).trim();
        return (
          <ExpandRow
            key={String(event.id || index)}
            head={
              <span className="flex min-w-0 flex-wrap items-baseline gap-x-3 gap-y-1 text-sm">
                <span className="font-mono text-xs text-muted">
                  {fmtDur(event.t0)}–{fmtDur(event.t1)}
                </span>
                <span className="font-medium">{title}</span>
              </span>
            }
          >
            <p>{String(event.summary || "").trim() || "没有摘要"}</p>
            <p className="mt-1">
              {event.id ? `编号 ${event.id}` : null}
              {event.kind ? ` · ${event.kind}` : ""}
              {event.cast_reset ? " · 换人" : ""}
            </p>
          </ExpandRow>
        );
      })}
    </div>
  );
}

function BeatsTab({ docs }: { docs: UnderstandDocs }) {
  const windows = Array.isArray(docs.beatsDoc?.windows) ? docs.beatsDoc.windows : [];
  if (!windows.length) return <Empty />;
  return (
    <div>
      {windows.map((win: Record<string, any>, index: number) => {
        const failed = Boolean(win.error) || !String(win.raw || "").trim();
        const flags = Array.isArray(win.must_open) ? win.must_open.filter(Boolean).join("、") : "";
        return (
          <ExpandRow
            key={`${win.start}-${index}`}
            head={
              <span className="flex min-w-0 flex-wrap items-baseline gap-x-3 gap-y-1 text-sm">
                <span className="font-mono text-xs text-muted">
                  {fmtDur(win.start)}–{fmtDur(win.end)}
                </span>
                {failed ? <span className="text-xs text-bad">失败</span> : null}
                {flags ? <span className="text-xs text-warn">{flags}</span> : null}
              </span>
            }
          >
            <p>
              成人 {win.adults ?? "—"} · 儿童 {win.children ?? "—"}
              {win.error ? ` · ${win.error}` : ""}
            </p>
            {flags ? <p className="mt-1">开图：{flags}</p> : null}
            {String(win.action || "").trim() ? <p className="mt-1">{String(win.action).trim()}</p> : null}
            {String(win.raw || "").trim() ? (
              <details className="mt-2">
                <summary className="cursor-pointer text-muted">原始 VL</summary>
                <pre className="mt-1 max-h-48 overflow-auto whitespace-pre-wrap rounded-md bg-panel p-2 font-mono text-[11px] leading-4">{String(win.raw)}</pre>
              </details>
            ) : null}
          </ExpandRow>
        );
      })}
    </div>
  );
}

export default function UnderstandDetail({ docs }: { docs: UnderstandDocs | null }) {
  const [tab, setTab] = useState<TabId>("summary");
  return (
    <div>
      <div className="mb-4 flex flex-wrap gap-1 rounded-md border border-line bg-panel/60 p-1">
        {TABS.map((item) => (
          <button
            key={item.id}
            type="button"
            className={cn("rounded px-3 py-1.5 text-sm", tab === item.id ? "bg-surface text-text shadow-sm" : "text-muted")}
            onClick={() => setTab(item.id)}
          >
            {item.label}
          </button>
        ))}
      </div>
      {!docs ? (
        <Empty />
      ) : tab === "summary" ? (
        <SummaryTab docs={docs} />
      ) : tab === "speech" ? (
        <SpeechTab docs={docs} />
      ) : tab === "events" ? (
        <EventsTab docs={docs} />
      ) : tab === "beats" ? (
        <BeatsTab docs={docs} />
      ) : docs.understanding || docs.shotsDoc || docs.eventsDoc || docs.dialogueDoc || docs.beatsDoc ? (
        <pre className="max-h-[55vh] overflow-auto whitespace-pre-wrap rounded-md bg-panel p-3 font-mono text-xs leading-5 text-muted">{docs.raw}</pre>
      ) : (
        <Empty />
      )}
    </div>
  );
}
