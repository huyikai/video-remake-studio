import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import Dialog from "../components/Dialog";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "../components/ui/select";
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
import { api } from "../lib/api";

type Props = { onClose: () => void };

type QualityForm = { workflow: string; megapixels: number; steps: number };
type WorkflowOpt = { id: string; label: string };

const FALLBACK_DRAFT: QualityForm = { workflow: "video_minimax_h3_t2v_turbo.json", megapixels: 0.4, steps: 8 };
const FALLBACK_FINAL: QualityForm = { workflow: "video_minimax_h3_t2v.json", megapixels: 0.98, steps: 25 };
const FALLBACK_WORKFLOWS: WorkflowOpt[] = [
  { id: "video_minimax_h3_t2v_turbo.json", label: "T2VA Turbo" },
  { id: "video_minimax_h3_t2v.json", label: "T2VA 非 LoRA" },
];

export default function NewJob({ onClose }: Props) {
  const nav = useNavigate();
  const [source, setSource] = useState<"url" | "file">("url");
  const [url, setUrl] = useState("");
  const [filePath, setFilePath] = useState("");
  const [review, setReview] = useState("pause_draft");
  const [path, setPath] = useState("t2va");
  const [vl, setVl] = useState("both");
  const [smtp, setSmtp] = useState(false);
  const [smtpUser, setSmtpUser] = useState("");
  const [smtpHasPassword, setSmtpHasPassword] = useState(false);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const [running, setRunning] = useState<string | null>(null);
  const [gate, setGate] = useState<{ ok: boolean; reasons: string[] } | null>(null);
  const [draft, setDraft] = useState<QualityForm>(FALLBACK_DRAFT);
  const [finalQ, setFinalQ] = useState<QualityForm>(FALLBACK_FINAL);
  const [workflows, setWorkflows] = useState<WorkflowOpt[]>(FALLBACK_WORKFLOWS);
  const settingsT2va = useRef<{ draft: QualityForm; final: QualityForm } | null>(null);
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
      const rec = s as {
        review_mode?: string;
        vl_mode?: string;
        smtp_enabled?: boolean;
        smtp_user?: string;
        smtp_has_password?: boolean;
        t2va?: { draft: QualityForm; final: QualityForm };
        t2va_workflows?: WorkflowOpt[];
      };
      if (rec.review_mode) setReview(rec.review_mode);
      if (rec.vl_mode) setVl(rec.vl_mode);
      if (rec.smtp_enabled) setSmtp(true);
      setSmtpUser(rec.smtp_user || "");
      setSmtpHasPassword(Boolean(rec.smtp_has_password));
      if (rec.t2va?.draft && rec.t2va?.final) {
        settingsT2va.current = rec.t2va;
        setDraft(rec.t2va.draft);
        setFinalQ(rec.t2va.final);
      }
      if (rec.t2va_workflows?.length) setWorkflows(rec.t2va_workflows);
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
    if (smtp && (!(smtpUser || "").trim() || !smtpHasPassword)) {
      setErr("SMTP 已启用但未配置邮箱或授权码。可在设置里填齐，或取消勾选邮件后再新建。");
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
      if (path === "t2va") body.generate = { draft, final: finalQ };
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

  const smtpReady = Boolean((smtpUser || "").trim() && smtpHasPassword);
  const smtpBlocked = smtp && !smtpReady;
  const blocked = Boolean(running) || gate?.ok === false || smtpBlocked;

  return (
    <>
      <Dialog title="新建任务" onClose={busy ? () => undefined : onClose}>
        {running ? <p className="mb-3 text-sm text-warn">已有任务在跑，等它结束或先放弃再新建。</p> : null}
        {gate && !gate.ok ? <p className="mb-3 text-sm text-bad">{gate.reasons.join("；")}</p> : null}
        {smtpBlocked ? (
          <p className="mb-3 text-sm text-warn">邮件已勾选，但还没填邮箱或授权码。可去设置填齐，或取消勾选后再新建。</p>
        ) : null}
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
              className="w-full rounded border border-line bg-surface px-3 py-2"
              placeholder="https://…"
              value={url}
              onChange={(e) => setUrl(e.target.value)}
            />
          ) : (
            <input
              className="w-full rounded border border-line bg-surface px-3 py-2 font-mono text-sm"
              placeholder="D:\videos\clip.mp4"
              value={filePath}
              onChange={(e) => {
                setFilePath(e.target.value);
                setConfirmed(false);
              }}
            />
          )}
          <label className="block text-sm text-text">
            VL 模式
            <Select value={vl} onValueChange={setVl}>
              <SelectTrigger className="mt-1 w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="both">both（视频+抽帧）</SelectItem>
                <SelectItem value="video">只视频</SelectItem>
                <SelectItem value="frames">只抽帧</SelectItem>
              </SelectContent>
            </Select>
          </label>
          <label className="block text-sm text-text">
            生成路线
            <Select
              value={path}
              onValueChange={(next) => {
                setPath(next);
                if (next === "t2va" && settingsT2va.current) {
                  setDraft(settingsT2va.current.draft);
                  setFinalQ(settingsT2va.current.final);
                }
              }}
            >
              <SelectTrigger className="mt-1 w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="t2va">T2VA（试片 Turbo / 成片非 LoRA）</SelectItem>
                <SelectItem value="t2va_turbo">T2VA Turbo（试片、成片都是 Turbo）</SelectItem>
                <SelectItem value="i2va_turbo">I2VA Turbo</SelectItem>
                <SelectItem value="ref2va">Ref2VA</SelectItem>
              </SelectContent>
            </Select>
          </label>
          {path === "t2va" ? (
            <div className="grid gap-3 sm:grid-cols-2">
              {([
                ["draft", "试片", draft, setDraft],
                ["final", "成片", finalQ, setFinalQ],
              ] as const).map(([key, label, value, setter]) => (
                <div key={key} className="space-y-2 rounded-lg border border-line p-3">
                  <p className="text-sm text-text">{label}</p>
                  <label className="block text-xs text-muted">
                    工作流
                    <Select value={value.workflow} onValueChange={(v) => setter({ ...value, workflow: v })}>
                      <SelectTrigger className="mt-1 w-full">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        {workflows.map((item) => (
                          <SelectItem key={item.id} value={item.id}>{item.label}</SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  </label>
                  <label className="block text-xs text-muted">
                    MP
                    <input type="number" step="0.01" className="mt-1 w-full rounded border border-line bg-surface p-2 text-sm text-text" value={value.megapixels} onChange={(e) => setter({ ...value, megapixels: Number(e.target.value) })} />
                  </label>
                  <label className="block text-xs text-muted">
                    步数
                    <input type="number" className="mt-1 w-full rounded border border-line bg-surface p-2 text-sm text-text" value={value.steps} onChange={(e) => setter({ ...value, steps: Number(e.target.value) })} />
                  </label>
                </div>
              ))}
            </div>
          ) : null}
          <label className="block text-sm text-text">
            审片
            <Select value={review} onValueChange={setReview}>
              <SelectTrigger className="mt-1 w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="pause_draft">审片（停在试片）</SelectItem>
                <SelectItem value="full_auto">一条龙出成片</SelectItem>
              </SelectContent>
            </Select>
          </label>
          <label className="flex items-center gap-2 text-sm">
            <input type="checkbox" checked={smtp} onChange={(e) => setSmtp(e.target.checked)} />
            启用 SMTP（须已配置邮箱和授权码）
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
      <AlertDialog open={aspectDlg !== null} onOpenChange={(open) => { if (!open) setAspectDlg(null); }}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>原片比例不是 16:9</AlertDialogTitle>
            <AlertDialogDescription>
              探测到 {aspectDlg?.source_aspect}，默认输出 {aspectDlg?.default_aspect} 重构构图。
            </AlertDialogDescription>
          </AlertDialogHeader>
          <div className="space-y-2 text-sm">
            <label className="flex gap-2">
              <input type="radio" checked={aspectChoice === aspectDlg?.default_aspect} onChange={() => aspectDlg && setAspectChoice(aspectDlg.default_aspect)} />
              保持 {aspectDlg?.default_aspect} 重构
            </label>
            <label className="flex gap-2">
              <input type="radio" checked={aspectChoice === aspectDlg?.source_aspect} onChange={() => aspectDlg && setAspectChoice(aspectDlg.source_aspect)} />
              跟原片 {aspectDlg?.source_aspect}
            </label>
          </div>
          <AlertDialogFooter>
            <AlertDialogCancel onClick={() => setAspectDlg(null)}>取消</AlertDialogCancel>
            <AlertDialogAction
              onClick={(event) => {
                event.preventDefault();
                setConfirmed(true);
                setAspectDlg(null);
                void submit(true);
              }}
            >
              确认并开始
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  );
}
