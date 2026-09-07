export type EnvItem = {
  id: string;
  ok: boolean;
  detail: string;
  layer: string;
  needed_now: boolean;
  status: string;
};

export type EnvPayload = {
  mode?: "mock" | "real";
  stage: string;
  summary: string;
  install: EnvItem[];
  live: EnvItem[];
  gate_new_job: { ok: boolean; reasons: string[] };
};

export type MetricsPayload = {
  gpu: {
    name: string;
    memory_used_mb: number;
    memory_total_mb: number;
    utilization_pct: number;
    temperature_c: number;
    power_w: number;
  } | null;
  ram: { used_bytes: number; total_bytes: number };
  h3: {
    workflow_file: string | null;
    label: string;
    quality: string | null;
    prompt_id: string | null;
    queue_length: number;
    foreign: boolean;
    comfy_reachable: boolean;
  };
  alerts: { vram_hot: boolean; temp_hot: boolean };
  mode?: "mock" | "real";
};

export type JobSummary = {
  id: string;
  mode?: "mock" | "real";
  state: string;
  stage: string;
  note?: string | null;
  created_at?: string;
  updated_at?: string;
  elapsed_sec?: number | null;
  source?: { kind?: string; url?: string | null };
  options?: Record<string, unknown>;
  stages_done?: number;
  stages_total?: number;
  need_aspect_confirm?: boolean;
  next_action?: string;
};

async function parse(res: Response) {
  const text = await res.text();
  let data: unknown = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = { detail: text };
  }
  if (!res.ok) {
    const detail =
      typeof data === "object" && data && "detail" in data
        ? String((data as { detail: unknown }).detail)
        : text || res.statusText;
    throw new Error(detail);
  }
  return data;
}

export const api = {
  env: (stage = "idle") => fetch(`/api/env?stage=${encodeURIComponent(stage)}`).then(parse) as Promise<EnvPayload>,
  metrics: () => fetch("/api/env/metrics").then(parse) as Promise<MetricsPayload>,
  settings: () => fetch("/api/settings").then(parse) as Promise<{
    mode: "mock" | "real";
    mock_speed: "0.25x" | "1x" | "4x";
    mock_faults: Record<string, unknown>;
    [key: string]: unknown;
  }>,
  patchSettings: (body: Record<string, unknown>) =>
    fetch("/api/settings", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then(parse),
  probe: (file_path: string) =>
    fetch("/api/probe", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ file_path }),
    }).then(parse) as Promise<{
      width: number;
      height: number;
      duration: number;
      source_aspect: string;
      default_aspect: string;
      mismatch: boolean;
    }>,
  jobs: () => fetch("/api/jobs").then(parse) as Promise<{ jobs: JobSummary[]; running_job_id: string | null }>,
  job: (id: string) => fetch(`/api/jobs/${id}`).then(parse) as Promise<Record<string, unknown>>,
  clip: (id: string, clipId: string) => fetch(`/api/jobs/${id}/clips/${clipId}`).then(parse),
  create: (body: Record<string, unknown>) =>
    fetch("/api/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then(parse) as Promise<{ id: string }>,
  resume: (id: string) => fetch(`/api/jobs/${id}/resume`, { method: "POST" }).then(parse),
  draft: (id: string, clip_ids: string[] = []) =>
    fetch(`/api/jobs/${id}/draft`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ clip_ids }),
    }).then(parse),
  finals: (id: string) => fetch(`/api/jobs/${id}/final`, { method: "POST" }).then(parse),
  cancel: (id: string) => fetch(`/api/jobs/${id}/cancel`, { method: "POST" }).then(parse),
  remove: (id: string) => fetch(`/api/jobs/${id}`, { method: "DELETE" }).then(parse),
  resetMock: () => fetch("/api/mock/reset", { method: "POST" }).then(parse),
  aspect: (id: string, follow_source: boolean) =>
    fetch(`/api/jobs/${id}/aspect`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ follow_source }),
    }).then(parse),
  saveClip: (id: string, clipId: string, body: Record<string, unknown>) =>
    fetch(`/api/jobs/${id}/clips/${clipId}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then(parse),
  saveScript: (id: string, body: Record<string, unknown>) =>
    fetch(`/api/jobs/${id}/script`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then(parse),
};
