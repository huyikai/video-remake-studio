import { useEffect, useState } from "react";
import Dialog from "../components/Dialog";
import { api } from "../lib/api";

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
  smtp_has_password: boolean;
  smtp_hint: string;
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
  const [err, setErr] = useState("");
  const [saving, setSaving] = useState(false);
  const [faultText, setFaultText] = useState("{}");

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
    if (form.mode === "real" && !window.confirm("真实模式会调用真实模型与 GPU，确定切换吗？")) return;
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
        aspect_ratio: form.aspect_ratio,
        aspect_confirm: form.aspect_confirm,
        review_mode: form.review_mode,
        vl_mode: form.vl_mode,
        esrgan: form.esrgan,
        ass_burn: form.ass_burn,
        t2va: form.t2va,
      });
      setForm(next as SettingsView);
      setFaultText(JSON.stringify((next as SettingsView).mock_faults || {}, null, 2));
      setMsg("已写入本机运行时配置");
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
          <section className="rounded-lg border border-tungsten/40 bg-tungsten/10 p-4">
            <div className="flex items-center justify-between gap-3">
              <div><p className="text-xs uppercase tracking-[0.16em] text-muted">运行环境</p><h3 className="mt-1 font-semibold">执行模式</h3><p className="mt-1 text-xs text-muted">模拟不调用真实模型，真实模式会检查本机推理环境。</p></div>
              <select className="rounded border border-line bg-surface p-2 text-sm text-text" value={form.mode} onChange={(e) => set("mode", e.target.value as SettingsView["mode"])}><option value="mock">模拟</option><option value="real">真实</option></select>
            </div>
            {form.mode === "mock" ? <div className="mt-4 space-y-3 border-t border-line/60 pt-4"><label className="block text-muted">模拟速度<select className="mt-1 w-full rounded border border-line bg-surface p-2 text-text" value={form.mock_speed} onChange={(e) => set("mock_speed", e.target.value as SettingsView["mock_speed"])}><option value="0.25x">0.25x</option><option value="1x">1x</option><option value="4x">4x</option></select></label><label className="block text-muted">故障场景 JSON<textarea className="mt-1 h-20 w-full rounded border border-line bg-surface p-2 font-mono text-xs text-text" value={faultText} onChange={(e) => setFaultText(e.target.value)} /></label><p className="text-xs text-muted">示例：{"{\"generate\":{\"clip_id\":\"h3_01\",\"count\":1,\"type\":\"timeout\"}}"}</p></div> : null}
          </section>
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
            主机 {form.smtp_host || "未配置"} · {form.smtp_has_password ? "已配置密码" : "未配置密码"}
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
            {form.t2va ? (
          <section className="rounded-lg border border-line p-4">
            <p className="text-xs uppercase tracking-[0.16em] text-muted">T2VA 默认</p>
            <h3 className="mt-1 font-semibold">试片 / 成片</h3>
            <p className="mt-1 text-xs text-muted">只作用于之后新建的 T2VA 任务。T2VA Turbo、I2VA、Ref2VA 仍走 yaml。创建时写入任务快照。</p>
            <div className="mt-4 grid gap-4 sm:grid-cols-2">
              {(["draft", "final"] as const).map((quality) => (
                <div key={quality} className="space-y-2">
                  <p className="text-sm text-text">{quality === "draft" ? "试片" : "成片"}</p>
                  <label className="block text-muted">
                    工作流
                    <select
                      className="mt-1 w-full rounded border border-line bg-surface p-2 text-text"
                      value={form.t2va[quality].workflow}
                      onChange={(e) => set("t2va", { ...form.t2va, [quality]: { ...form.t2va[quality], workflow: e.target.value } })}
                    >
                      {(form.t2va_workflows || []).map((item) => (
                        <option key={item.id} value={item.id}>{item.label}</option>
                      ))}
                    </select>
                  </label>
                  <label className="block text-muted">
                    MP
                    <input
                      type="number"
                      step="0.01"
                      className="mt-1 w-full rounded border border-line bg-ink p-2 text-text"
                      value={form.t2va[quality].megapixels}
                      onChange={(e) => set("t2va", { ...form.t2va, [quality]: { ...form.t2va[quality], megapixels: Number(e.target.value) } })}
                    />
                  </label>
                  <label className="block text-muted">
                    步数
                    <input
                      type="number"
                      className="mt-1 w-full rounded border border-line bg-ink p-2 text-text"
                      value={form.t2va[quality].steps}
                      onChange={(e) => set("t2va", { ...form.t2va, [quality]: { ...form.t2va[quality], steps: Number(e.target.value) } })}
                    />
                  </label>
                </div>
              ))}
            </div>
          </section>
            ) : null}
          {msg ? <p className="text-ok">{msg}</p> : null}
          {err ? <p className="text-bad">{err}</p> : null}
          <div className="flex justify-end gap-2 pt-2">
            <button type="button" className="px-3 py-2 text-muted" onClick={onClose}>
              取消
            </button>
            <button type="button" className="rounded bg-tungsten px-4 py-2 text-ink disabled:opacity-40" disabled={saving} onClick={() => void save()}>
              {saving ? "保存中..." : "保存到本机配置"}
            </button>
          </div>
        </div>
      )}
    </Dialog>
  );
}
