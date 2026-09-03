import { useEffect, useState } from "react";
import Dialog from "../components/Dialog";
import { api } from "../lib/api";

type SettingsView = {
  comfy_base_url: string;
  gpu_memory_gb: number;
  hang_timeout_sec: number;
  generate_clip_timeout_sec: number;
  clip_restart_max: number;
  job_restart_max: number;
  smtp_enabled: boolean;
  smtp_to: string[];
  smtp_host: string;
  smtp_has_password: boolean;
  smtp_hint: string;
  aspect_ratio: string;
  aspect_confirm: boolean;
  review_mode: string;
  vl_mode: string;
  esrgan: boolean;
  ass_burn: boolean;
};

type Props = { onClose: () => void };

export default function SettingsPage({ onClose }: Props) {
  const [form, setForm] = useState<SettingsView | null>(null);
  const [msg, setMsg] = useState("");
  const [err, setErr] = useState("");
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    api.settings().then((d) => setForm(d as SettingsView)).catch((e: Error) => setErr(e.message));
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
      const next = await api.patchSettings({
        comfy_base_url: form.comfy_base_url,
        gpu_memory_gb: form.gpu_memory_gb,
        hang_timeout_sec: form.hang_timeout_sec,
        generate_clip_timeout_sec: form.generate_clip_timeout_sec,
        clip_restart_max: form.clip_restart_max,
        job_restart_max: form.job_restart_max,
        smtp_enabled: form.smtp_enabled,
        smtp_to: form.smtp_to,
        aspect_ratio: form.aspect_ratio,
        aspect_confirm: form.aspect_confirm,
        review_mode: form.review_mode,
        vl_mode: form.vl_mode,
        esrgan: form.esrgan,
        ass_burn: form.ass_burn,
      });
      setForm(next as SettingsView);
      setMsg("已写入 config/local.yaml");
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setSaving(false);
    }
  }

  return (
    <Dialog title="设置" onClose={onClose}>
      {!form ? (
        <p className="text-muted">{err || "加载设置…"}</p>
      ) : (
        <div className="space-y-3 text-sm">
          <label className="block text-muted">
            Comfy base_url
            <input className="mt-1 w-full rounded border border-line bg-ink p-2 text-text" value={form.comfy_base_url} onChange={(e) => set("comfy_base_url", e.target.value)} />
          </label>
          <label className="block text-muted">
            显存档 gpu_memory_gb
            <input type="number" className="mt-1 w-full rounded border border-line bg-ink p-2 text-text" value={form.gpu_memory_gb} onChange={(e) => set("gpu_memory_gb", Number(e.target.value))} />
          </label>
          <label className="block text-muted">
            看门狗 hang_timeout_sec
            <input type="number" className="mt-1 w-full rounded border border-line bg-ink p-2 text-text" value={form.hang_timeout_sec} onChange={(e) => set("hang_timeout_sec", Number(e.target.value))} />
          </label>
          <label className="block text-muted">
            单段超时 generate_clip_timeout_sec
            <input type="number" className="mt-1 w-full rounded border border-line bg-ink p-2 text-text" value={form.generate_clip_timeout_sec} onChange={(e) => set("generate_clip_timeout_sec", Number(e.target.value))} />
          </label>
          <div className="grid grid-cols-2 gap-3">
            <label className="block text-muted">
              clip 重启上限
              <input type="number" className="mt-1 w-full rounded border border-line bg-ink p-2 text-text" value={form.clip_restart_max} onChange={(e) => set("clip_restart_max", Number(e.target.value))} />
            </label>
            <label className="block text-muted">
              job 重启上限
              <input type="number" className="mt-1 w-full rounded border border-line bg-ink p-2 text-text" value={form.job_restart_max} onChange={(e) => set("job_restart_max", Number(e.target.value))} />
            </label>
          </div>
          <label className="flex items-center gap-2">
            <input type="checkbox" checked={form.smtp_enabled} onChange={(e) => set("smtp_enabled", e.target.checked)} />
            SMTP enabled
          </label>
          <label className="block text-muted">
            收件人（逗号分隔）
            <input
              className="mt-1 w-full rounded border border-line bg-ink p-2 text-text"
              value={form.smtp_to.join(", ")}
              onChange={(e) => set("smtp_to", e.target.value.split(",").map((x) => x.trim()).filter(Boolean))}
            />
          </label>
          <p className="text-xs text-muted">
            主机 {form.smtp_host || "—"} · {form.smtp_has_password ? "已配置密码" : "未配置密码"}
            <br />
            {form.smtp_hint}
          </p>
          <label className="flex items-center gap-2">
            <input type="checkbox" checked={form.esrgan} onChange={(e) => set("esrgan", e.target.checked)} />
            RealESRGAN
          </label>
          <label className="flex items-center gap-2">
            <input type="checkbox" checked={form.ass_burn} onChange={(e) => set("ass_burn", e.target.checked)} />
            烧 ASS
          </label>
          {msg ? <p className="text-ok">{msg}</p> : null}
          {err ? <p className="text-bad">{err}</p> : null}
          <div className="flex justify-end gap-2 pt-2">
            <button type="button" className="px-3 py-2 text-muted" onClick={onClose}>
              取消
            </button>
            <button type="button" className="rounded bg-tungsten px-4 py-2 text-ink disabled:opacity-40" disabled={saving} onClick={() => void save()}>
              {saving ? "保存中…" : "保存到本机配置"}
            </button>
          </div>
        </div>
      )}
    </Dialog>
  );
}
