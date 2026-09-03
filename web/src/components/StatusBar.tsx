import { useEffect, useMemo, useState } from "react";
import { api, type EnvItem, type EnvPayload, type MetricsPayload } from "../lib/api";
import { cn } from "../lib/utils";

const DOT = {
  ok: "bg-ok",
  red: "bg-bad",
  yellow: "bg-warn",
};

function Dot({ item }: { item?: EnvItem }) {
  const cls = item ? DOT[item.status as keyof typeof DOT] || "bg-muted" : "bg-muted";
  return <span className={cn("inline-block h-2 w-2 rounded-full", cls)} title={item?.detail} />;
}

function fmtGb(n: number) {
  return (n / 1024 ** 3).toFixed(1);
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
  const vramHot = Boolean(metrics?.alerts.vram_hot);
  const tempHot = Boolean(metrics?.alerts.temp_hot);

  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="w-full border-b border-line bg-panel px-5 py-2 text-left text-xs"
      >
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-muted">
          <span className="text-text">就绪</span>
          <span className="inline-flex items-center gap-1">
            <Dot item={byId.get("ffmpeg")} /> ffmpeg
          </span>
          <span className="inline-flex items-center gap-1">
            <Dot item={byId.get("qwen3_asr_model_dir") || byId.get("asr")} /> Whisper/ASR
          </span>
          <span className="inline-flex items-center gap-1">
            <Dot item={byId.get("vl")} /> VL
          </span>
          <span className="inline-flex items-center gap-1">
            <Dot item={byId.get("comfy")} /> Comfy
          </span>
          <span className="text-text/80">{env?.summary || "读取环境…"}</span>
        </div>
        <div className="mt-1 flex flex-wrap items-center gap-x-4 gap-y-1 text-muted">
          {gpu ? (
            <>
              <span>{gpu.name.replace("NVIDIA GeForce ", "")}</span>
              <span className={vramHot ? "text-warn" : ""}>
                显存 {Math.round(gpu.memory_used_mb)}/{Math.round(gpu.memory_total_mb)} MB
              </span>
              <span>GPU {Math.round(gpu.utilization_pct)}%</span>
              <span className={tempHot ? "text-warn" : ""}>温度 {Math.round(gpu.temperature_c)}°C</span>
            </>
          ) : (
            <span>GPU —</span>
          )}
          {ram ? (
            <span>
              内存 {fmtGb(ram.used_bytes)}/{fmtGb(ram.total_bytes)} GB
            </span>
          ) : null}
          <span className="text-tungsten">{h3?.label || "未在生成"}</span>
          {h3?.quality ? <span>{h3.quality}</span> : null}
        </div>
      </button>
      {open ? (
        <div className="fixed inset-0 z-50 flex justify-end bg-black/50" onClick={() => setOpen(false)}>
          <aside
            className="h-full w-full max-w-3xl overflow-y-auto border-l border-line bg-ink p-5 text-sm"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="mb-4 flex items-center justify-between">
              <h2 className="text-base font-semibold">环境与设备</h2>
              <button className="text-muted hover:text-text" onClick={() => setOpen(false)}>
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
                </p>
              </section>
              <section>
                <h3 className="mb-2 text-tungsten">当前需要</h3>
                <p>{env?.summary}</p>
                <p className="mt-3 text-muted">
                  {gpu
                    ? `功耗 ${gpu.power_w ?? "—"} W · 占用 ${gpu.utilization_pct}% · 温度 ${gpu.temperature_c}°C`
                    : "读不到 nvidia-smi"}
                </p>
                {vramHot ? <p className="mt-2 text-warn">显存 ≥90%（生成时发黄是预期）</p> : null}
              </section>
            </div>
          </aside>
        </div>
      ) : null}
    </>
  );
}
