# Video Remake Studio

本机流水线编排器：给 URL 或本地视频，理解画面和对白，写成 MiniMax H3 脚本，经本机 ComfyUI 出草稿/成片。

本仓库**不内置**视频生成模型，只把复刻脚本交给你已经部署好的服务。

## 视频生成模型怎么用

当前默认对接 **MiniMax H3 量化版**，I2VA 路径叠加 **Turbo LoRA**（`video_minimax_h3_i2v_turbo.json`）。H3 一次生成带原生声音的 24fps 短片，时长必须落在合法帧网格上（约 4.5–14.4 秒，不是口头的 4–15 秒）。更长的原片由流水线拆成多段再硬切拼接。需要锁角色时改走 Ref2VA（另一套权重，不能和 I2VA 混在同一次生成里）。

显存怎么选参数：

- **RTX 5060 Ti 8GB**：亲测可以本地跑 H3 量化版 + Turbo LoRA，把 `config/h3_constraints.yaml` 里的 `gpu_memory_gb` 设为 `8`。
- **RTX 5070 Ti 16GB**：仓库默认按这档（I2VA 草稿 0.4MP/6 步、成片 0.9MP/8 步；Ref2VA 成片 0.9MP/20 步）。
- **更高显存**：加大 megapixels 和步数即可；也可以把 Comfy 工作流换成别的视频生成模型，只要仍走「脚本 → 预检 → 逐段生成 → 拼接」。

权重和 ComfyUI 装在本项目目录之外。生成若卡死或 CUDA OOM：中断队列、按配置重启 Comfy、从失败的那一段接着跑。

口播 ASR（Qwen3-ASR-1.7B）/ RapidOCR / **Qwen3-VL-8B** / 口播情绪 emotion2vec+ large 是 Python 依赖（`uv sync --extra asr` 和 `--extra vl`），权重必须在仓库外。ASR 用 Transformers 原生加载 `Qwen/Qwen3-ASR-1.7B-hf` + `Qwen3-ForcedAligner-0.6B-hf`（**不要装 `qwen-asr` 包**，它会把 transformers 锁到 4.57，和 VL 冲突）。口播情绪用本机 PyTorch 加载 `emotion2vec/emotion2vec_plus_large`（**不要装 FunASR / ModelScope**）。VL 与脚本 LLM 用 HuggingFace Transformers 加载 `Qwen/Qwen3-VL-8B-Instruct`（能吃视频和图片），**不要用 Ollama，也不要用 2B**。H3 仍走外部 ComfyUI HTTP。

拆段用的文本 LLM 有两条路，`config/providers.yaml` 的 `llm.kind` 切换：`cursor_sdk`（默认，`uv sync --extra sdk`）和 `transformers`（本机 Qwen3.5-9B 4bit，权重在仓库外）。读图始终是本地 8B，不走 SDK。

SDK 这条路是本地 agent（`llm.sdk_mode`，默认 `agent`）+ `cwd` 指到空临时目录 + `tools=[]`，输入全部内联，密钥优先读环境变量 `CURSOR_API_KEY`。**模型必须用带推理档的强模型**：`grok-4.6 effort=high`，兜底 `claude-opus-5 thinking=true,effort=high`。拆段是在一堆相邻候选里做取舍，`composer-2.5` 这一档每跑一次结果都不一样（同一片会在 `61.93/62.77/67.53` 之间摇摆），强模型跨模型跨次数都给同一个答案。

## 阶段 0–8（当前）

骨架、环境检查、进料、页面元信息、理解（镜头 / Qwen3-ASR / OCR / 对白终审 / emotion2vec+ 口播情绪 / VL）、脚本打包、预检。缺 8B 或 emotion2vec+ 权重时，任务暂停等待。

脚本阶段先做「事件 → H3 合法片段」，产出 `clips.json`。**事件只拆不并**：两个事件合进同一次生成等于把故事边界埋进一条片子里，硬切拼接就再也切不开。超过 H3 上限（`frame_max/fps`，默认 14.375s）的事件按规则候选切点二次拆分——这里用的是**全量候选**而不是 Pass A 的短名单，因为短名单是为「哪里是故事边界」筛的，段内拆分要的是「哪里画面/对白允许断开」。每段再吸附到合法帧网格，漂移记在 `drift`。

然后是 Pass B：一段一次调用，给每段写 H3 英文提示词，产出 `prompts.json` 加 `prompts/h3_NN.txt`（喂 Comfy 的正文）和 `prompts/h3_NN.md`（中文对照，给人审），停在预检。

**模型只回 JSON，官方格式的正文由代码拼**（`passb.assemble_txt`）。首行的 `S.SS`、`[Shot N] At MM:SS.mmm` 都由构造保证，不指望模型把格式写准。

**发单帧，不发横条。** 横条是 7680×360，发给视觉模型会被内部缩放到每格百来像素，外观细节全丢还白付上传时间；改成每个画面不同的片刻各取一张 1920×1080 真帧（每段至多 6 张）。同一段的图片量从约 2.5 MB 降到约 0.6 MB。

**说话人外观锁向后传递。** 按顺序写，先写的段把 `(S1)` 的一句英文外观锁定下来，后面的段必须逐字复用，否则同一个人会在第三段换脸。落盘前 `promptcheck.check_locks` 再跨段对一遍。

**原片画面字一律不进提示词。** `h3_constraints.yaml` 的 `negative_prompt` 本来就在排斥烧字幕，字幕留到成片阶段再叠。

机检口径在 `promptcheck.py`，Pass B 落盘前自检和预检阶段共用：`<d>` 里的每句必须逐字命中 `dialogue.json`（拦编对白）、本段对白必须全部进 `<d>`（拦漏句）、标签只能是 `[Chinese]`、汉字只允许出现在 `<d>` 里、`[Shot N]` 切点严格递增且每镜至少 0.6s、首行 `S.SS` 等于本条时长、跨段外观锁逐字一致、同一句台词不能出现在两条里（拦跟读）。任一项不过，Pass B 带着错误列表重写（至多三次）；预检再对落盘后的 `.txt` 走同一套，并核对 JSON 与 txt 没有改岔、I2VA 首帧文件在。

预检通过后停在 `generate`。对照在 `prompts/h3_NN.md`，审完 `vrs resume <job_id>` 开始按段调用本机 ComfyUI 出草稿（默认 `pause_draft`：0.4MP/6 步）。草稿在 `generate/{path}/draft/`，整片预览 `output/{path}/draft.mp4`（已按源片时长裁掉 H3 补帧）。再 `resume` 按同一脚本出成片（0.9MP/8 步）。**finish** 再硬切拼接、可选 RealESRGAN、按 `dialogue.json` 烧 ASS（底栏对白 + 像戏剧核的标题，水印不烧）、封面（文生图或成片首帧），写出 `output/{path}/final.mp4` 与 `cover.jpg`。SMTP 默认关；启用后在开始、草稿待审、看门狗重启、完成/失败时发信（大文件超限则只写本机路径）。失败的段会留下 `generate.json`，resume 从缺的那一段接着跑。报告在 `precheck.json` / `precheck.md`。

WebUI：`uv run vrs serve` 后打开 `http://127.0.0.1:8787`（需先 `pnpm --dir web build`）。开发可另开 `pnpm --dir web dev`（5173 反代 API）。任务列表与三栏详情是整页；**新建**和**设置**为弹窗。详情里 **保存并预检**、**再出草稿**、**出成片**、**放弃**；进度走 SSE。一次只跑一个任务。Skill 与 WebUI 共用 FastAPI。

### 生成路线

`config/h3_constraints.yaml` 里按路线分块，`--path` / `options.generate_path` 选：

| 路线 | 工作流 | 关键帧 | 提示词首行 |
|-|-|-|-|
| `i2va_turbo`（默认） | `video_minimax_h3_i2v_turbo.json` | 抽源片区间起点到 `keyframes/h3_NN_a.jpg` | `<Picture 1>` 对齐指令 |
| `t2va_turbo` | `video_minimax_h3_t2v_turbo.json` | 不用 | 无，直接从三字段开始 |
| `ref2va` | `video_minimax_h3_r2v.json` | 另说 | 另说 |

早期把 I2VA 的工作流错标成了 `fl2va_turbo`（那个工作流只有一个 `LoadImage` 加 Turbo LoRA，是只锚首帧的 I2VA），已改名，旧 job.json 里的老名字由 `h3grid.PATH_ALIASES` 接住。真正的首末帧工作流是 `video_minimax_h3_flf.json`，没有 Turbo 变体；而且本片十段里有四段内部还有硬切，让 H3 从首帧插值到末帧是逆着内容走，所以暂时不接。

```powershell
uv sync --extra asr --extra vl --extra sdk
uv run playwright install chromium
uv run vrs env --stage understand
uv run vrs run --file D:\path\to\video.mp4
uv run vrs run --url "https://example.com/watch?v=..."
uv run vrs resume <job_id>
uv run vrs cancel <job_id>
uv run vrs approve-script <job_id>
uv run vrs serve
pnpm --dir web install
pnpm --dir web build
```

进料：

- **抖音**：走 [f2](https://github.com/Johnserf-Seed/f2) Python API，不弹浏览器、也不回退 yt-dlp。需要登录 Cookie：从已登录 Chrome 复制请求头里的 Cookie 整段（必须 ASCII），放到环境变量 `VRS_DOUYIN_COOKIE` 或 `config/local.yaml` 的 `douyin.cookie`（gitignore，不要提交）。过期就失败，改 Cookie 后 `vrs resume`。图集 / 动图 / 直播会以「不是单条视频」失败。落盘 `source/video.mp4`、封面、原声、`desc.txt`、`page_meta.json`、`f2_aweme.json`；评论尽力拉约 100 条按赞留 30（`comments.json`），关评或失败只警告，不进理解和脚本。
- **其余 URL**：yt-dlp，默认最高 1080p，合并为 mp4。TikTok 仍可能弹出 Chrome 过 WAF。也可在 `ytdlp.cookies_file` / `cookies_from_browser` 里手配。
- **本地文件**：必须是绝对路径，拷贝进 `data/jobs/<id>/source/video.mp4`，不抓页面。

浏览器或 curl：`http://127.0.0.1:8787/`（界面）、`/api/env` 、 `/api/env/metrics`、`POST /api/jobs`。开发时 Vite 在 `http://127.0.0.1:5173`。

本机覆盖：复制 `config/local.yaml.example` 为 `config/local.yaml`。SMTP 账号用 `config/smtp.local.yaml`（gitignore）或环境变量 `VRS_SMTP_*`。

可选相邻目录（不下载权重）：

```powershell
powershell -NoProfile -File scripts/deploy_runtime.ps1
```

## 开发约定

- Python 3.12 + uv；前端 `web/` 用 pnpm（Vite + React）。
- 默认绑定 `127.0.0.1:8787`，不做登录。
- 一次只推进一个交付阶段。
