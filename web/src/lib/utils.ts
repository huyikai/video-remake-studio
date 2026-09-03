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
