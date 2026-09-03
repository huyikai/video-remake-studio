# Video Remake Studio — 通用 Skill

用本仓库的 `vrs` CLI 或 WebUI（`vrs serve` → `http://127.0.0.1:8787`）编排「下载 → 理解 → H3 脚本 → 草稿/成片」。不要在本仓库目录里下载模型权重。

## 必读

H3 提示词必须遵守 `h3_skills_root` 里的官方 [h3-prompt-writing](https://github.com/MiniMax-AI/MiniMax-H3/tree/main/skills/h3-prompt-writing)。缺这个目录时先跑 `vrs env`，按提示把 MiniMax-H3 的 `skills/` 指到仓库外。

## 审片循环

1. `vrs run --url <URL>` 或 WebUI 新建任务（默认 `pause_draft`）。
2. 脚本/预检停住后，对照 `prompts/*.md` 或详情页右栏改词，点 **保存并预检**（`PUT /api/jobs/{id}/clips/{clip_id}`）。
3. `vrs approve-script <job_id>` / `vrs resume` / 详情 **继续**：出 0.4 草稿。
4. 改过的段用 **再出草稿**（`POST /api/jobs/{id}/draft`）。草稿齐了再 **出成片**（`POST /api/jobs/{id}/final`）。CLI 在草稿已齐时再 `resume` 也会出成片。
5. **放弃** 会 interrupt Comfy、标 `cancelled`、保留产物。`vrs cancel <job_id>` 相同。

## 常用命令

```text
vrs env
vrs run --url <URL>
vrs run --file <绝对路径>
vrs jobs
vrs resume <job_id>
vrs approve-script <job_id>
vrs cancel <job_id>
vrs serve
```

## 约束（不要违反）

- 本仓库只编排、不存放权重。Qwen3-VL-8B 与脚本 LLM 用 Transformers 本进程加载（`vrs-runtime/models/qwen3-vl`）；口播用 Qwen3-ASR-1.7B + ForcedAligner（`vrs-runtime/models/qwen3-asr`）；口播情绪用 emotion2vec+ large（`vrs-runtime/models/emotion2vec-plus-large`，本机 PyTorch，不要装 FunASR）。ComfyUI MiniMax H3 走 HTTP。**不要用 Ollama / 2B 当 VL。不要装 `qwen-asr` 包。** RapidOCR 同样是 Python 依赖，权重在仓库外。
- 理解阶段：镜头是原子时间轴；Qwen3-ASR 管口播，OCR 管烧字，emotion2vec+ 管口播情绪，VL 不负责读字幕正文，也不吃 vocal_emotion。
- H3 时长走合法帧网格（24fps，`n % 17 == 5`，约 4.46–14.375 秒），不要按口头 4–15 秒。合剪时按源片这一拍裁掉 pad。字幕用 finish 阶段的 ASS 烧回，不要写进 H3 提示词。
- FL2VA 与 Ref2VA 不能混在同一次生成。
- 默认一次只跑一个 Job。
