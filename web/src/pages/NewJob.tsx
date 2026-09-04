import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import Dialog from "../components/Dialog";
import { api } from "../lib/api";

type Props = { onClose: () => void };

export default function NewJob({ onClose }: Props) {
  const nav = useNavigate();
  const [source, setSource] = useState<"url" | "file">("url");
  const [url, setUrl] = useState("");
  const [filePath, setFilePath] = useState("");
  const [review, setReview] = useState("pause_draft");
  const [path, setPath] = useState("t2va_turbo");
  const [vl, setVl] = useState("both");
  const [smtp, setSmtp] = useState(false);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const [running, setRunning] = useState<string | null>(null);
  const [gate, setGate] = useState<{ ok: boolean; reasons: string[] } | null>(null);
  const [aspectDlg, setAspectDlg] = useState<{
    source_aspect: string;
    default_aspect: string;
  } | null>(null);
  const [aspectChoice, setAspectChoice] = useState("16:9");
  const [confirmed, setConfirmed] = useState(false);

  useEffect(() => {
    api.jobs().then((d) => setRunning(d.running_job_id)).catch(() => undefined);
    api.env("download").then((e) => setGate(e.gate_new_job)).catch(() => undefined);
    api.settings().then((s) => {
      const rec = s as { review_mode?: string; vl_mode?: string; smtp_enabled?: boolean };
      if (rec.review_mode) setReview(rec.review_mode);
      if (rec.vl_mode) setVl(rec.vl_mode);
      if (rec.smtp_enabled) setSmtp(true);
    });
  }, []);

  async function checkFileAspect() {
    if (source !== "file" || !filePath.trim()) return true;
    const probe = await api.probe(filePath.trim());
    if (probe.mismatch) {
      setAspectDlg({ source_aspect: probe.source_aspect, default_aspect: probe.default_aspect });
      setAspectChoice(probe.default_aspect);
      setConfirmed(false);
      return false;
    }
    setConfirmed(true);
    setAspectChoice(probe.default_aspect);
    return true;
  }

  async function submit(alreadyConfirmed = false) {
    setErr("");
    if (running) {
      setErr(`已有任务在跑：${running}`);
      return;
    }
    if (!gate?.ok) {
      setErr(gate?.reasons.join("；") || "进料检查未通过");
      return;
    }
    try {
      if (source === "file" && !confirmed && !alreadyConfirmed) {
        const ok = await checkFileAspect();
        if (!ok) return;
      }
      setBusy(true);
      const body: Record<string, unknown> = {
        review_mode: review,
        generate_path: path,
        vl_mode: vl,
        smtp,
        aspect_ratio: aspectChoice,
        aspect_confirmed: source === "file" || alreadyConfirmed,
      };
      if (source === "url") body.url = url.trim();
      else body.file_path = filePath.trim();
      const job = await api.create(body);
      onClose();
      nav(`/jobs/${job.id}`);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  const blocked = Boolean(running) || gate?.ok === false;

  return (
    <>
      <Dialog title="新建任务" onClose={busy ? () => undefined : onClose}>
        {running ? <p className="mb-3 text-sm text-warn">已有任务在跑，等它结束或先放弃再新建。</p> : null}
        {gate && !gate.ok ? <p className="mb-3 text-sm text-bad">{gate.reasons.join("；")}</p> : null}
        <div className="space-y-4">
          <div className="flex gap-3 text-sm">
            <button type="button" className={source === "url" ? "text-tungsten" : "text-muted"} onClick={() => setSource("url")}>
              URL
            </button>
            <button type="button" className={source === "file" ? "text-tungsten" : "text-muted"} onClick={() => setSource("file")}>
              本机绝对路径
            </button>
          </div>
          {source === "url" ? (
            <input
              className="w-full rounded border border-line bg-ink px-3 py-2"
              placeholder="https://…"
              value={url}
              onChange={(e) => setUrl(e.target.value)}
            />
          ) : (
            <input
              className="w-full rounded border border-line bg-ink px-3 py-2 font-mono text-sm"
              placeholder="D:\videos\clip.mp4"
              value={filePath}
              onChange={(e) => {
                setFilePath(e.target.value);
                setConfirmed(false);
              }}
            />
          )}
          <label className="block text-sm text-muted">
            VL 模式
            <select className="mt-1 w-full rounded border border-line bg-ink p-2 text-text" value={vl} onChange={(e) => setVl(e.target.value)}>
              <option value="both">both（视频+抽帧）</option>
              <option value="video">只视频</option>
              <option value="frames">只抽帧</option>
            </select>
          </label>
          <label className="block text-sm text-muted">
            生成路线
            <select className="mt-1 w-full rounded border border-line bg-ink p-2 text-text" value={path} onChange={(e) => setPath(e.target.value)}>
              <option value="t2va_turbo">T2VA Turbo</option>
              <option value="i2va_turbo">I2VA Turbo</option>
              <option value="ref2va">Ref2VA</option>
            </select>
          </label>
          <label className="block text-sm text-muted">
            审片
            <select className="mt-1 w-full rounded border border-line bg-ink p-2 text-text" value={review} onChange={(e) => setReview(e.target.value)}>
              <option value="pause_draft">审片（停在试片）</option>
              <option value="full_auto">一条龙出成片</option>
            </select>
          </label>
          <label className="flex items-center gap-2 text-sm">
            <input type="checkbox" checked={smtp} onChange={(e) => setSmtp(e.target.checked)} />
            启用 SMTP（须已能登录）
          </label>
          {err ? <p className="text-bad">{err}</p> : null}
          <div className="flex justify-end gap-2">
            <button type="button" className="px-3 py-2 text-sm text-muted" onClick={onClose} disabled={busy}>
              取消
            </button>
            <button
              type="button"
              disabled={busy || blocked}
              onClick={() => void submit()}
              className="rounded-md bg-tungsten px-4 py-2 font-medium text-ink disabled:opacity-40"
            >
              {busy ? "提交中…" : "开始"}
            </button>
          </div>
        </div>
      </Dialog>
      {aspectDlg ? (
        <div className="fixed inset-0 z-[70] flex items-center justify-center bg-black/60">
          <div className="w-[28rem] rounded-lg border border-line bg-panel p-5">
            <h2 className="mb-2 font-semibold">原片比例不是 16:9</h2>
            <p className="mb-4 text-sm text-muted">
              探测到 {aspectDlg.source_aspect}，默认输出 {aspectDlg.default_aspect} 重构构图。
            </p>
            <div className="mb-4 space-y-2 text-sm">
              <label className="flex gap-2">
                <input type="radio" checked={aspectChoice === aspectDlg.default_aspect} onChange={() => setAspectChoice(aspectDlg.default_aspect)} />
                保持 {aspectDlg.default_aspect} 重构
              </label>
              <label className="flex gap-2">
                <input type="radio" checked={aspectChoice === aspectDlg.source_aspect} onChange={() => setAspectChoice(aspectDlg.source_aspect)} />
                跟原片 {aspectDlg.source_aspect}
              </label>
            </div>
            <div className="flex justify-end gap-2">
              <button type="button" className="text-muted" onClick={() => setAspectDlg(null)}>
                取消
              </button>
              <button
                type="button"
                className="rounded bg-tungsten px-3 py-1 text-ink"
                onClick={() => {
                  setConfirmed(true);
                  setAspectDlg(null);
                  void submit(true);
                }}
              >
                确认并开始
              </button>
            </div>
          </div>
        </div>
      ) : null}
    </>
  );
}
