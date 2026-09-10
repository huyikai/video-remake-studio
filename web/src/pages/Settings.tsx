import { useEffect, useState } from "react";
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
import { cn } from "../lib/utils";

type QualityForm = { workflow: string; megapixels: number; steps: number };

type SettingsView = {
  mode: "mock" | "real";
  mock_speed: "0.25x" | "1x" | "4x";
  mock_faults: Record<string, unknown>;
  comfy_base_url: string;
  gpu_memory_gb: number;
  hang_timeout_sec: number;
  generate_clip_timeout_sec: number;
  clip_restart_max: number;
  job_restart_max: number;
  smtp_enabled: boolean;
  smtp_to: string[];
  smtp_host: string;
  smtp_user: string;
  smtp_has_password: boolean;
  smtp_password_from_env: boolean;
  smtp_hint: string;
  douyin_cookie_set: boolean;
  douyin_cookie_from_env: boolean;
  douyin_cookie_expired: boolean;
  douyin_cookie_state?: "missing" | "ok" | "expired";
  douyin_cookie_status: string;
  douyin_cookie_hint: string;
  aspect_ratio: string;
  aspect_confirm: boolean;
  review_mode: string;
  vl_mode: string;
  esrgan: boolean;
  ass_burn: boolean;
  t2va: { draft: QualityForm; final: QualityForm };
  t2va_workflows: { id: string; label: string }[];
};

type Props = { onClose: () => void };

export default function SettingsPage({ onClose }: Props) {
  const [form, setForm] = useState<SettingsView | null>(null);
  const [msg, setMsg] = useState("");
  const [msgWarn, setMsgWarn] = useState(false);
  const [err, setErr] = useState("");
  const [saving, setSaving] = useState(false);
  const [confirmRealMode, setConfirmRealMode] = useState(false);
  const [faultText, setFaultText] = useState("{}");
  const [cookieDraft, setCookieDraft] = useState("");
  const [clearCookie, setClearCookie] = useState(false);
  const [importing, setImporting] = useState(false);
  const [replaceOpen, setReplaceOpen] = useState(false);
  const [smtpAuth, setSmtpAuth] = useState("");
  const [clearSmtpAuth, setClearSmtpAuth] = useState(false);
  const [smtpTesting, setSmtpTesting] = useState(false);
  const [smtpTestMsg, setSmtpTestMsg] = useState("");
  const [smtpTestOk, setSmtpTestOk] = useState<boolean | null>(null);

  useEffect(() => {
    api.settings().then((d) => {
      const next = d as SettingsView;
      setForm(next);
      setFaultText(JSON.stringify(next.mock_faults || {}, null, 2));
    }).catch((e: Error) => setErr(e.message));
  }, []);

  function set<K extends keyof SettingsView>(key: K, value: SettingsView[K]) {
    if (!form) return;
    setForm({ ...form, [key]: value });
  }

  async function save() {
    if (!form) return;
    setErr("");
    setSaving(true);
    try {
      let mockFaults: Record<string, unknown> = {};
      try {
        mockFaults = JSON.parse(faultText) as Record<string, unknown>;
      } catch {
        throw new Error("故障场景必须是有效 JSON");
      }
      const next = await api.patchSettings({
        mode: form.mode,
        mock_speed: form.mock_speed,
        mock_faults: mockFaults,
        comfy_base_url: form.comfy_base_url,
        gpu_memory_gb: form.gpu_memory_gb,
        hang_timeout_sec: form.hang_timeout_sec,
        generate_clip_timeout_sec: form.generate_clip_timeout_sec,
        clip_restart_max: form.clip_restart_max,
        job_restart_max: form.job_restart_max,
        smtp_enabled: form.smtp_enabled,
        smtp_to: form.smtp_to,
        smtp_user: form.smtp_user,
        ...(clearSmtpAuth ? { smtp_password: "" } : smtpAuth.trim() ? { smtp_password: smtpAuth.trim() } : {}),
        aspect_ratio: form.aspect_ratio,
        aspect_confirm: form.aspect_confirm,
        review_mode: form.review_mode,
        vl_mode: form.vl_mode,
        esrgan: form.esrgan,
        ass_burn: form.ass_burn,
        t2va: form.t2va,
        ...(clearCookie ? { douyin_cookie: "" } : cookieDraft.trim() ? { douyin_cookie: cookieDraft.trim() } : {}),
      });
      setForm(next as SettingsView);
      setFaultText(JSON.stringify((next as SettingsView).mock_faults || {}, null, 2));
      setCookieDraft("");
      setClearCookie(false);
      setReplaceOpen(false);
      setSmtpAuth("");
      setClearSmtpAuth(false);
      const savedAuth = Boolean(smtpAuth.trim() || clearSmtpAuth);
      if ((next as SettingsView).smtp_password_from_env && savedAuth) {
        setMsgWarn(true);
        setMsg("已写入本机配置，但当前仍使用环境变量 VRS_SMTP_PASSWORD，发信不会用到这次保存。请先清掉该变量。");
      } else if ((next as SettingsView).douyin_cookie_from_env && (cookieDraft.trim() || clearCookie)) {
        setMsgWarn(true);
        setMsg("已写入本机配置，但当前仍使用环境变量 VRS_DOUYIN_COOKIE，下载不会用到这次保存。请先清掉该变量。");
      } else {
        setMsgWarn(false);
        setMsg("已写入本机配置");
      }
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setSaving(false);
    }
  }

  async function testSmtp() {
    if (!form) return;
    setErr("");
    setSmtpTestMsg("");
    setSmtpTestOk(null);
    setSmtpTesting(true);
    try {
      const result = await api.testSmtp();
      setSmtpTestOk(result.ok);
      setSmtpTestMsg(result.detail);
    } catch (e) {
      setSmtpTestOk(false);
      setSmtpTestMsg((e as Error).message);
    } finally {
      setSmtpTesting(false);
    }
  }

  async function importCookie(forceWindow = false) {
    if (!form) return;
    setErr("");
    setMsg("");
    setMsgWarn(false);
    setImporting(true);
    try {
      const next = (await api.importDouyinCookie(forceWindow)) as SettingsView;
      setForm(next);
      setCookieDraft("");
      setClearCookie(false);
      setReplaceOpen(false);
      const via = (next as SettingsView & { douyin_cookie_imported_via?: string }).douyin_cookie_imported_via;
      if (next.douyin_cookie_from_env) {
        setMsgWarn(true);
        setMsg("已写入本机配置，但当前仍使用环境变量 VRS_DOUYIN_COOKIE，下载不会用到这次导入。请先清掉该变量。");
      } else {
        setMsgWarn(false);
        setMsg(via === "cdp" ? "已从正在运行的 Chrome 导入" : "已从登录窗口导入");
      }
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setImporting(false);
    }
  }

  return (
    <Dialog
      title="设置"
      onClose={onClose}
      footer={
        <div className="space-y-2">
          {msg ? <p className={cn("text-xs", msgWarn ? "text-warn" : "text-ok")}>{msg}</p> : null}
          {err ? <p className="text-bad">{err}</p> : null}
          <div className="flex justify-end gap-2">
            <button type="button" className="px-3 py-2 text-sm text-muted hover:text-text" onClick={onClose}>
              取消
            </button>
            <button type="button" className="rounded bg-tungsten px-4 py-2 text-ink disabled:opacity-40" disabled={saving} onClick={() => { if (form.mode === "real") { setConfirmRealMode(true); } else { void save(); } }}>
              {saving ? "保存中..." : "保存到本机配置"}
            </button>
          </div>
        </div>
      }
    >
      {!form ? (
        <p className="text-muted">{err || "加载设置…"}</p>
      ) : (
        <div className="space-y-3 text-sm">
          <section className="rounded-lg border border-tungsten/40 bg-tungsten/10 p-4">
            <div className="flex items-center justify-between gap-3">
              <div><p className="text-xs uppercase tracking-[0.16em] text-muted">运行环境</p><h3 className="mt-1 font-semibold">执行模式</h3><p className="mt-1 text-xs text-muted">模拟不调用真实模型，真实模式会检查本机推理环境。</p></div>
              <Select value={form.mode} onValueChange={(v) => set("mode", v as SettingsView["mode"])}>
                <SelectTrigger className="w-32"><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="mock">模拟</SelectItem>
                  <SelectItem value="real">真实</SelectItem>
                </SelectContent>
              </Select>
            </div>
            {form.mode === "mock" ? <div className="mt-4 space-y-3 border-t border-line/60 pt-4"><label className="block text-sm text-text">模拟速度<Select value={form.mock_speed} onValueChange={(v) => set("mock_speed", v as SettingsView["mock_speed"])}><SelectTrigger className="mt-1 w-full"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="0.25x">0.25x</SelectItem><SelectItem value="1x">1x</SelectItem><SelectItem value="4x">4x</SelectItem></SelectContent></Select></label><label className="block text-sm text-text">故障场景 JSON<textarea className="mt-1 h-20 w-full rounded border border-line bg-surface p-2 font-mono text-xs text-text" value={faultText} onChange={(e) => setFaultText(e.target.value)} /></label><p className="text-xs text-muted">示例：{"{\"generate\":{\"clip_id\":\"h3_01\",\"count\":1,\"type\":\"timeout\"}}"}</p></div> : null}
          </section>
            {form.t2va ? (
          <section className="rounded-lg border border-line p-4">
            <p className="text-xs uppercase tracking-[0.16em] text-muted">T2VA 默认</p>
            <h3 className="mt-1 font-semibold">试片 / 成片</h3>
            <p className="mt-1 text-xs text-muted">只作用于之后新建的 T2VA 任务。T2VA Turbo、I2VA、Ref2VA 仍走 yaml。创建时写入任务快照。</p>
            <div className="mt-4 grid gap-4 sm:grid-cols-2">
              {(["draft", "final"] as const).map((quality) => (
                <div key={quality} className="space-y-2">
                  <p className="text-sm text-text">{quality === "draft" ? "试片" : "成片"}</p>
                  <label className="block text-sm text-text">
                    工作流
                    <Select
                      value={form.t2va[quality].workflow}
                      onValueChange={(v) => set("t2va", { ...form.t2va, [quality]: { ...form.t2va[quality], workflow: v } })}
                    >
                      <SelectTrigger className="mt-1 w-full">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        {(form.t2va_workflows || []).map((item) => (
                          <SelectItem key={item.id} value={item.id}>{item.label}</SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  </label>
                  <label className="block text-sm text-text">
                    MP
                    <div className="mt-1 flex items-center gap-2">
                      <input
                        type="number"
                        step="0.01"
                        className="w-full rounded border border-line bg-surface p-2 text-text"
                        value={form.t2va[quality].megapixels}
                        onChange={(e) => set("t2va", { ...form.t2va, [quality]: { ...form.t2va[quality], megapixels: Number(e.target.value) } })}
                      />
                      <span className="text-xs text-muted">MP</span>
                    </div>
                  </label>
                  <label className="block text-sm text-text">
                    步数
                    <div className="mt-1 flex items-center gap-2">
                      <input
                        type="number"
                        className="w-full rounded border border-line bg-surface p-2 text-text"
                        value={form.t2va[quality].steps}
                        onChange={(e) => set("t2va", { ...form.t2va, [quality]: { ...form.t2va[quality], steps: Number(e.target.value) } })}
                      />
                      <span className="text-xs text-muted">步</span>
                    </div>
                  </label>
                </div>
              ))}
            </div>
          </section>
            ) : null}
          <section className="rounded-lg border border-line p-4">
            <p className="text-xs uppercase tracking-[0.16em] text-muted">抖音进料</p>
            <h3 className="mt-1 font-semibold">登录 Cookie</h3>
            <p className={cn("mt-1 text-xs", form.douyin_cookie_expired ? "text-bad" : form.douyin_cookie_set ? "text-ok" : "text-muted")}>
              {form.douyin_cookie_status || (form.douyin_cookie_set ? "已配置" : "未配置")}
            </p>
            {form.douyin_cookie_from_env ? <p className="mt-1 text-xs text-warn">当前实际使用环境变量 VRS_DOUYIN_COOKIE。本机配置改了也不生效，过期时请先清掉该变量。</p> : null}
            {form.douyin_cookie_set && !form.douyin_cookie_expired && !replaceOpen ? (
              <div className="mt-3 flex flex-wrap gap-2">
                <button type="button" className="rounded border border-line px-3 py-1.5 text-sm text-muted hover:text-text" onClick={() => setReplaceOpen(true)}>
                  更换
                </button>
                {!form.douyin_cookie_from_env ? (
                  <button
                    type="button"
                    className="text-xs text-bad hover:underline"
                    onClick={() => {
                      setCookieDraft("");
                      setClearCookie(true);
                      setReplaceOpen(true);
                    }}
                  >
                    {clearCookie ? "将在保存时清除" : "清除"}
                  </button>
                ) : null}
              </div>
            ) : (
              <>
                <div className="mt-3 flex flex-wrap gap-2">
                  <button
                    type="button"
                    className="rounded bg-tungsten px-3 py-1.5 text-sm text-ink disabled:opacity-40"
                    disabled={importing || saving}
                    onClick={() => void importCookie(false)}
                  >
                    {importing ? "等待登录…" : "一键导入"}
                  </button>
                  <button
                    type="button"
                    className="rounded border border-line px-3 py-1.5 text-sm text-muted hover:text-text disabled:opacity-40"
                    disabled={importing || saving}
                    onClick={() => void importCookie(true)}
                  >
                    打开登录窗口
                  </button>
                  {form.douyin_cookie_set && !form.douyin_cookie_expired ? (
                    <button type="button" className="rounded border border-line px-3 py-1.5 text-sm text-muted hover:text-text" onClick={() => { setReplaceOpen(false); setCookieDraft(""); setClearCookie(false); }}>
                      取消
                    </button>
                  ) : null}
                </div>
                <label className="mt-3 block text-sm text-text">
                  {form.douyin_cookie_set ? "粘贴新 Cookie（留空则不改）" : "粘贴 Cookie"}
                  <textarea
                    className="mt-1 h-24 w-full rounded border border-line bg-surface p-2 font-mono text-xs text-text"
                    autoComplete="off"
                    spellCheck={false}
                    value={cookieDraft}
                    placeholder={form.douyin_cookie_set ? "已保存，不会回显" : "sessionid=...; ..."}
                    onChange={(e) => {
                      setCookieDraft(e.target.value);
                      setClearCookie(false);
                    }}
                  />
                </label>
              </>
            )}
            <p className="mt-2 text-xs text-muted">{form.douyin_cookie_hint}</p>
          </section>
          <section className="rounded-lg border border-line p-4">
            <p className="text-xs uppercase tracking-[0.16em] text-muted">模型环境</p>
            <h3 className="mt-1 font-semibold">Comfy / 超时 / 重启</h3>
            <p className="mt-1 text-xs text-muted">ComfyUI 地址、显存档和任务超时。重启上限只针对单个 clip / job 内部的失败重试。</p>
            <div className="mt-4 space-y-3">
              <label className="block text-sm text-text">
                Comfy base_url
                <input className="mt-1 w-full rounded border border-line bg-surface p-2 text-text" value={form.comfy_base_url} onChange={(e) => set("comfy_base_url", e.target.value)} />
              </label>
              <div className="grid grid-cols-2 gap-3">
                <label className="block text-sm text-text">
                  显存档 gpu_memory_gb
                  <div className="mt-1 flex items-center gap-2">
                    <input type="number" className="w-full rounded border border-line bg-surface p-2 text-text" value={form.gpu_memory_gb} onChange={(e) => set("gpu_memory_gb", Number(e.target.value))} />
                    <span className="text-xs text-muted">GB</span>
                  </div>
                </label>
                <label className="block text-sm text-text">
                  看门狗 hang_timeout_sec
                  <div className="mt-1 flex items-center gap-2">
                    <input type="number" className="w-full rounded border border-line bg-surface p-2 text-text" value={form.hang_timeout_sec} onChange={(e) => set("hang_timeout_sec", Number(e.target.value))} />
                    <span className="text-xs text-muted">秒</span>
                  </div>
                </label>
              </div>
              <label className="block text-sm text-text">
                单段超时 generate_clip_timeout_sec
                <div className="mt-1 flex items-center gap-2">
                  <input type="number" className="w-full rounded border border-line bg-surface p-2 text-text" value={form.generate_clip_timeout_sec} onChange={(e) => set("generate_clip_timeout_sec", Number(e.target.value))} />
                  <span className="text-xs text-muted">秒</span>
                </div>
              </label>
              <div className="grid grid-cols-2 gap-3">
                <label className="block text-sm text-text">
                  clip 重启上限
                  <div className="mt-1 flex items-center gap-2">
                    <input type="number" className="w-full rounded border border-line bg-surface p-2 text-text" value={form.clip_restart_max} onChange={(e) => set("clip_restart_max", Number(e.target.value))} />
                    <span className="text-xs text-muted">次</span>
                  </div>
                </label>
                <label className="block text-sm text-text">
                  job 重启上限
                  <div className="mt-1 flex items-center gap-2">
                    <input type="number" className="w-full rounded border border-line bg-surface p-2 text-text" value={form.job_restart_max} onChange={(e) => set("job_restart_max", Number(e.target.value))} />
                    <span className="text-xs text-muted">次</span>
                  </div>
                </label>
              </div>
            </div>
          </section>
          <section className="rounded-lg border border-line p-4">
            <div className="flex items-start justify-between gap-3">
              <div><p className="text-xs uppercase tracking-[0.16em] text-muted">邮件提醒</p><h3 className="mt-1 font-semibold">SMTP</h3><p className="mt-1 text-xs text-muted">勾选后才能在新建任务时启用邮件通知。</p></div>
              <label className="flex shrink-0 items-center gap-2 pt-1">
                <input type="checkbox" checked={form.smtp_enabled} onChange={(e) => set("smtp_enabled", e.target.checked)} />
                <span className="text-sm">启用</span>
              </label>
            </div>
            <div className="mt-4 space-y-3 border-t border-line/60 pt-4">
              <label className="block text-sm text-text">
                收件人（逗号分隔）
                <input
                  className="mt-1 w-full rounded border border-line bg-surface p-2 text-text"
                  value={form.smtp_to.join(", ")}
                  onChange={(e) => set("smtp_to", e.target.value.split(",").map((x) => x.trim()).filter(Boolean))}
                />
              </label>
              <div className="grid grid-cols-2 gap-3">
                <label className="block text-sm text-text">
                  发件账号
                  <input
                    className="mt-1 w-full rounded border border-line bg-surface p-2 text-text"
                    value={form.smtp_user || ""}
                    autoComplete="username"
                    placeholder="例如 QQ 邮箱地址"
                    onChange={(e) => set("smtp_user", e.target.value)}
                  />
                </label>
                <label className="block text-sm text-text">
                  {form.smtp_has_password ? "新授权码（留空则不改）" : "授权码"}
                  <input
                    type="password"
                    className="mt-1 w-full rounded border border-line bg-surface p-2 text-text"
                    value={smtpAuth}
                    autoComplete="new-password"
                    spellCheck={false}
                    placeholder={form.smtp_has_password ? "已保存，不会回显" : "QQ 邮箱授权码，不是登录密码"}
                    onChange={(e) => {
                      setSmtpAuth(e.target.value);
                      setClearSmtpAuth(false);
                    }}
                  />
                </label>
              </div>
              {form.smtp_has_password && !form.smtp_password_from_env ? (
                <button
                  type="button"
                  className="text-xs text-bad hover:underline"
                  onClick={() => {
                    setSmtpAuth("");
                    setClearSmtpAuth(true);
                  }}
                >
                  {clearSmtpAuth ? "将在保存时清除授权码" : "清除授权码"}
                </button>
              ) : null}
              {form.smtp_password_from_env ? <p className="text-xs text-warn">当前实际使用环境变量 VRS_SMTP_PASSWORD。本机配置改了也不生效，请先清掉该变量。</p> : null}
              <p className="text-xs text-muted">
                主机 {form.smtp_host || "未配置"} · {form.smtp_has_password ? "已配置授权码" : "未配置授权码"}
                <br />
                {form.smtp_hint}
              </p>
              {form.smtp_enabled && (!(form.smtp_user || "").trim() || !form.smtp_has_password) ? (
                <p className="text-xs text-warn">启用了邮件但还缺邮箱或授权码。填齐后才能在勾选邮件的任务上开跑。</p>
              ) : null}
              <button
                type="button"
                className="rounded-md border border-line px-3 py-1.5 text-sm hover:border-tungsten/60 hover:text-text disabled:opacity-40"
                disabled={!form.smtp_enabled || !(form.smtp_user || "").trim() || !form.smtp_has_password || !(form.smtp_to || []).length || smtpTesting}
                onClick={() => void testSmtp()}
              >
                {smtpTesting ? "发送中…" : "发送测试"}
              </button>
              {smtpTestMsg ? <p className={cn("text-xs", smtpTestOk ? "text-ok" : "text-bad")}>{smtpTestMsg}</p> : null}
            </div>
          </section>
          <section className="rounded-lg border border-line p-4">
            <p className="text-xs uppercase tracking-[0.16em] text-muted">成片后处理</p>
            <h3 className="mt-1 font-semibold">画质 / 字幕</h3>
            <div className="mt-4 space-y-2">
              <label className="flex items-center gap-2 text-sm">
                <input type="checkbox" checked={form.esrgan} onChange={(e) => set("esrgan", e.target.checked)} />
                RealESRGAN 超分
              </label>
              <label className="flex items-center gap-2 text-sm">
                <input type="checkbox" checked={form.ass_burn} onChange={(e) => set("ass_burn", e.target.checked)} />
                烧 ASS 字幕
              </label>
            </div>
          </section>
        </div>
      )}

      <AlertDialog open={confirmRealMode} onOpenChange={(open) => { if (!open) setConfirmRealMode(false); }}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>切换到真实模式？</AlertDialogTitle>
            <AlertDialogDescription>真实模式会调用真实模型与 GPU，请确认环境已就绪。</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={saving}>取消</AlertDialogCancel>
            <AlertDialogAction
              disabled={saving}
              onClick={(event) => { event.preventDefault(); setConfirmRealMode(false); void save(); }}
            >
              {saving ? "保存中..." : "切换并保存"}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </Dialog>
  );
}
