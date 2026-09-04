import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

export function fmtDur(sec: number | null | undefined): string {
  if (sec == null || Number.isNaN(sec)) return "—";
  const n = Math.max(0, Math.round(sec));
  const h = Math.floor(n / 3600);
  const m = Math.floor((n % 3600) / 60);
  const s = n % 60;
  if (h) return `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  return `${m}:${String(s).padStart(2, "0")}`;
}

export function fmtTime(t: number | null | undefined): string {
  if (t == null) return "—";
  return `${Number(t).toFixed(2)}s`;
}

export function fileUrl(jobId: string, rel: string | null | undefined): string | undefined {
  if (!rel) return undefined;
  return `/api/jobs/${jobId}/files/${rel.split("/").map(encodeURIComponent).join("/")}`;
}

const JOB_STATE_LABEL: Record<string, string> = {
  running: "运行中",
  paused: "已暂停",
  pause: "已暂停",
  failed: "失败",
  error: "失败",
  cancelled: "已取消",
  canceled: "已取消",
  done: "完成",
  pending: "等待",
  waiting: "等待",
};

const STAGE_STATUS_LABEL: Record<string, string> = {
  pending: "等待",
  running: "运行中",
  done: "完成",
  skipped: "跳过",
  waiting: "等待环境",
  failed: "失败",
  error: "失败",
  dirty: "待更新",
};

const MODE_LABEL: Record<string, string> = {
  mock: "模拟",
  real: "真实",
};

function lookupLabel(value: string | null | undefined, map: Record<string, string>) {
  if (!value) return "";
  return map[value.trim().toLowerCase()] || "";
}

export function jobStateLabel(state: string | null | undefined) {
  return lookupLabel(state, JOB_STATE_LABEL) || "未知";
}

export function stageStatusLabel(status: string | null | undefined) {
  return lookupLabel(status, STAGE_STATUS_LABEL) || "未知";
}

export function modeLabel(mode: string | null | undefined) {
  return lookupLabel(mode, MODE_LABEL) || "未知";
}
