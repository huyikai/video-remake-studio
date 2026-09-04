import { api } from "./api";

export const PRIMARY_ACTIONS = [
  "download",
  "pagemeta",
  "understand",
  "script",
  "precheck",
  "draft",
  "final",
  "retry",
  "redraft",
  "done",
  "busy",
  "cancelled",
] as const;

export type PrimaryAction = (typeof PRIMARY_ACTIONS)[number];

const PRIMARY_LABEL: Record<PrimaryAction, string> = {
  download: "开始下载",
  pagemeta: "获取页面信息",
  understand: "开始理解",
  script: "生成脚本",
  precheck: "开始预检",
  draft: "出试片",
  final: "出成片",
  retry: "重试",
  redraft: "再出试片",
  done: "已完成",
  busy: "处理中...",
  cancelled: "已取消",
};

const PRIMARY_HINT: Record<PrimaryAction, string> = {
  download: "下载原片并做到预检停点",
  pagemeta: "读取页面信息后继续",
  understand: "分析画面和对白",
  script: "生成各片段脚本",
  precheck: "检查脚本是否可以生成",
  draft: "生成试片，用来快速验证",
  final: "生成成片并交付",
  retry: "从失败处接着跑",
  redraft: "按新脚本重新生成试片",
  done: "任务已经完成",
  busy: "任务正在处理",
  cancelled: "任务已放弃",
};

const EARLY_STAGES = ["download", "pagemeta", "understand", "script", "precheck"] as const;

type ActionJob = {
  next_action?: string | null;
  state?: string | null;
  stage?: string | null;
  running?: boolean;
  dirty_clip_ids?: string[];
  stages?: Record<string, { status?: string } | string | undefined>;
};

function isPrimaryAction(value: string | null | undefined): value is PrimaryAction {
  return Boolean(value && (PRIMARY_ACTIONS as readonly string[]).includes(value));
}

function stageStatus(job: ActionJob, name: string) {
  const rec = job.stages?.[name];
  if (typeof rec === "string") return rec;
  return rec?.status || "";
}

function inferPrimaryAction(job: ActionJob): PrimaryAction {
  const state = (job.state || "").toLowerCase();
  if (job.running || state === "running") return "busy";
  if (state === "done") return "done";
  if (state === "cancelled" || state === "canceled") return "cancelled";
  if (state === "failed" || state === "error") return "retry";
  if ((job.dirty_clip_ids || []).length) return "redraft";
  if (job.stages) {
    for (const name of EARLY_STAGES) {
      const status = stageStatus(job, name);
      if (status !== "done" && status !== "skipped") return name;
    }
    const generate = stageStatus(job, "generate");
    if (generate !== "done" && generate !== "skipped") return "draft";
    const finish = stageStatus(job, "finish");
    if (finish !== "done" && finish !== "skipped") return "final";
    return "done";
  }
  const stage = job.stage || "download";
  if ((EARLY_STAGES as readonly string[]).includes(stage)) return stage as (typeof EARLY_STAGES)[number];
  if (stage === "generate") return "draft";
  if (stage === "finish") return "final";
  return "download";
}

export function primaryActionFromJob(job: ActionJob): PrimaryAction {
  const action = isPrimaryAction(job.next_action) ? job.next_action : inferPrimaryAction(job);
  if ((job.dirty_clip_ids || []).length && !["busy", "done", "cancelled", "retry"].includes(action)) {
    return "redraft";
  }
  return action;
}

export function primaryActionLabel(job: ActionJob) {
  return PRIMARY_LABEL[primaryActionFromJob(job)];
}

export function primaryActionHint(job: ActionJob) {
  return PRIMARY_HINT[primaryActionFromJob(job)];
}

export function primaryActionDisabled(job: ActionJob) {
  const action = primaryActionFromJob(job);
  return action === "busy" || action === "done" || action === "cancelled";
}

export function dispatchPrimaryAction(id: string, job: ActionJob, clipIds: string[] = []) {
  const action = primaryActionFromJob(job);
  if (action === "draft" || action === "redraft") return api.draft(id, clipIds);
  if (action === "final") return api.finals(id);
  return api.resume(id);
}
