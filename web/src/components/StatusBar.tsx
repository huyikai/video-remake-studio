import { useEffect, useMemo, useState } from "react";
import { api, type EnvItem, type EnvPayload, type MetricsPayload } from "../lib/api";
import { cn, modeLabel } from "../lib/utils";

const DOT = {
  ok: "bg-ok",
  red: "bg-bad",
  yellow: "bg-warn",
};

function Dot({ item }: { item?: EnvItem }) {
  const cls = item ? DOT[item.status as keyof typeof DOT] || "bg-muted" : "bg-muted";
  return <span className={cn("inline-block size-2 shrink-0 rounded-full", cls)} title={item?.detail} />;
}

function fmtGb(n: number) {
  return (n / 1024 ** 3).toFixed(1);
}

function StatusDot({ item, label }: { item?: EnvItem; label: string }) {
  return (
    <span className="inline-flex shrink-0 items-center gap-1" title={item?.detail || label}>
      <Dot item={item} />
      {label}
    </span>
  );
}

export default function StatusBar() {
  const [env, setEnv] = useState<EnvPayload | null>(null);
  const [metrics, setMetrics] = useState<MetricsPayload | null>(null);
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let stop = false;
    const tick = async () => {
      try {
        const jobs = await api.jobs();
        if (stop) return;
        setBusy(Boolean(jobs.running_job_id));
        const [e, m] = await Promise.all([api.env(), api.metrics()]);
        if (stop) return;
        setEnv(e);
        setMetrics(m);
      } catch {
        /* 状态栏失败不打断页面 */
      }
    };
    tick();
    const id = window.setInterval(tick, busy ? 2000 : 5000);
    return () => {
      stop = true;
      window.clearInterval(id);
    };
  }, [busy]);

  const byId = useMemo(() => {
    const map = new Map<string, EnvItem>();
    for (const item of [...(env?.install || []), ...(env?.live || [])]) map.set(item.id, item);
    return map;
  }, [env]);

  const gpu = metrics?.gpu;
  const ram = metrics?.ram;
  const h3 = metrics?.h3;
  const mode = env?.mode || metrics?.mode || "real";
  const vramHot = Boolean(metrics?.alerts.vram_hot);
  const tempHot = Boolean(metrics?.alerts.temp_hot);
  const summary = busy ? h3?.label || "任务运行中" : env?.summary || "读取环境…";

  return (
    <div className="flex max-w-full justify-center">
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="flex max-w-full items-center justify-center gap-3 overflow-hidden rounded-md px-2 py-1.5 text-xs text-muted hover:bg-panel"
        title="查看环境与设备"
      >
        <span className="shrink-0 rounded border border-tungsten/50 bg-tungsten/10 px-2 py-0.5 text-tungsten">{modeLabel(mode)}</span>
        <span className="hidden min-w-0 items-center gap-3 sm:inline-flex">
          <StatusDot item={byId.get("ffmpeg")} label="ffmpeg" />
          <StatusDot item={byId.get("qwen3_asr_model_dir") || byId.get("asr")} label="ASR" />
          <StatusDot item={byId.get("vl")} label="VL" />
          <StatusDot item={byId.get("comfy")} label="Comfy" />
          {byId.get("smtp") ? <StatusDot item={byId.get("smtp")} label="SMTP" /> : null}
        </span>
        <span className="min-w-0 truncate text-text/80">{summary}</span>
      </button>
      {open ? (
        <div className="fixed inset-0 z-50 flex justify-end bg-bg" onClick={() => setOpen(false)}>
          <aside
            className="h-full w-full max-w-3xl overflow-y-auto border-l border-line bg-surface p-5 text-sm"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="mb-4 flex items-center justify-between">
              <h2 className="text-base font-semibold">环境与设备</h2>
              <button type="button" className="text-muted hover:text-text" onClick={() => setOpen(false)}>
                关闭
              </button>
            </div>
            <div className="grid gap-4 md:grid-cols-3">
              <section>
                <h3 className="mb-2 text-tungsten">安装</h3>
                {(env?.install || []).map((item) => (
                  <p key={item.id} className="mb-1">
                    <Dot item={item} /> <span className="text-text">{item.id}</span>
                    <span className="block pl-4 text-xs text-muted">{item.detail}</span>
                  </p>
                ))}
              </section>
              <section>
                <h3 className="mb-2 text-tungsten">进程</h3>
                {(env?.live || []).map((item) => (
                  <p key={item.id} className="mb-1">
                    <Dot item={item} /> <span className="text-text">{item.id}</span>
                    <span className="block pl-4 text-xs text-muted">{item.detail}</span>
                  </p>
                ))}
                <p className="mt-3 text-muted">
                  prompt_id {h3?.prompt_id || "—"}
                  <br />
                  队列 {h3?.queue_length ?? "—"}
                  <br />
                  {h3?.label || "未在生成"}
                  {h3?.quality ? ` · ${h3.quality}` : ""}
                </p>
              </section>
              <section>
                <h3 className="mb-2 text-tungsten">当前需要</h3>
                <p>{env?.summary}</p>
                <p className="mt-3 text-muted">
                  {gpu
                    ? `${gpu.name.replace("NVIDIA GeForce ", "")} · 显存 ${Math.round(gpu.memory_used_mb)}/${Math.round(gpu.memory_total_mb)} MB · 占用 ${gpu.utilization_pct}% · 温度 ${gpu.temperature_c}°C${tempHot || vramHot ? " · 偏高" : ""}`
                    : "读不到 nvidia-smi"}
                </p>
                {ram ? (
                  <p className="mt-2 text-muted">
                    内存 {fmtGb(ram.used_bytes)}/{fmtGb(ram.total_bytes)} GB
                  </p>
                ) : null}
                {vramHot ? <p className="mt-2 text-warn">显存 ≥90%（生成时发黄是预期）</p> : null}
              </section>
            </div>
          </aside>
        </div>
      ) : null}
    </div>
  );
}
