# 可选：在相邻目录创建 vrs-runtime 布局
# 默认 auto_deploy: false。本脚本不下载权重，只建目录。
# 用法（在仓库根目录）:
#   powershell -NoProfile -File scripts/deploy_runtime.ps1

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Runtime = Join-Path (Split-Path -Parent $RepoRoot) "vrs-runtime"

Write-Host "Creating $Runtime (no model downloads)"
$dirs = @(
    "models\whisper",
    "models\ocr",
    "models\qwen3-vl",
    "models\emotion2vec-plus-large",
    "models\realesrgan",
    "vl-server",
    "MiniMax-H3"
)
foreach ($rel in $dirs) {
    $path = Join-Path $Runtime $rel
    New-Item -ItemType Directory -Force -Path $path | Out-Null
}

$Readme = @"
vrs-runtime 不进 video-remake-studio 的 git。

把 Whisper / OCR 权重放到 models\ 下。
H3 请继续用已有的 minmaxH3（本机默认 ../minmaxH3），不要把 Comfy 拷进本目录，除非检测不到现成安装。
官方 h3-prompt-writing：在此浅克隆 MiniMax-H3 后，把 config/paths.yaml 的 h3_skills_root 指到 MiniMax-H3\skills。
"@
Set-Content -Path (Join-Path $Runtime "README.txt") -Value $Readme -Encoding UTF8
Write-Host "Done. Edit config/local.yaml if you need different paths."
