from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any

from vrs.media import extract_fps_frames
from vrs.settings import Settings

_WATERMARK = re.compile(
    r"(抖音|快手|西瓜视频|火山小视频|tiktok|douyin|watermark|关注我|来抖音|打开抖音|直播中)",
    re.I,
)


def ensure_ocr_models(settings: Settings) -> Path:
    dest = settings.path("ocr_model_dir")
    dest.mkdir(parents=True, exist_ok=True)
    names = [
        "ch_PP-OCRv4_det_infer.onnx",
        "ch_PP-OCRv4_rec_infer.onnx",
        "ch_ppocr_mobile_v2.0_cls_infer.onnx",
    ]
    bundled = Path(__import__("rapidocr_onnxruntime").__file__).resolve().parent / "models"
    for name in names:
        target = dest / name
        if not target.is_file():
            src = bundled / name
            if not src.is_file():
                raise RuntimeError(f"找不到 OCR 模型 {name}，请放到 {dest}")
            shutil.copy2(src, target)
    cfg = dest / "config.yaml"
    cfg.write_text(
            "\n".join(
                [
                    "Global:",
                    "  text_score: 0.5",
                    "  use_det: true",
                    "  use_cls: true",
                    "  use_rec: true",
                    "  print_verbose: false",
                    "  min_height: 30",
                    "  width_height_ratio: 8",
                    "  max_side_len: 2000",
                    "  min_side_len: 30",
                    "  return_word_box: false",
                    "  intra_op_num_threads: -1",
                    "  inter_op_num_threads: -1",
                    "Det:",
                    "  intra_op_num_threads: -1",
                    "  inter_op_num_threads: -1",
                    "  use_cuda: false",
                    "  use_dml: false",
                    f"  model_path: { (dest / names[0]).as_posix() }",
                    "  limit_side_len: 736",
                    "  limit_type: min",
                    "  std: [0.5, 0.5, 0.5]",
                    "  mean: [0.5, 0.5, 0.5]",
                    "  thresh: 0.3",
                    "  box_thresh: 0.5",
                    "  max_candidates: 1000",
                    "  unclip_ratio: 1.6",
                    "  use_dilation: true",
                    "  score_mode: fast",
                    "Cls:",
                    "  intra_op_num_threads: -1",
                    "  inter_op_num_threads: -1",
                    "  use_cuda: false",
                    "  use_dml: false",
                    f"  model_path: { (dest / names[2]).as_posix() }",
                    "  cls_image_shape: [3, 48, 192]",
                    "  cls_batch_num: 6",
                    "  cls_thresh: 0.9",
                    "  label_list: ['0', '180']",
                    "Rec:",
                    "  intra_op_num_threads: -1",
                    "  inter_op_num_threads: -1",
                    "  use_cuda: false",
                    "  use_dml: false",
                    f"  model_path: { (dest / names[1]).as_posix() }",
                    "  rec_img_shape: [3, 48, 320]",
                    "  rec_batch_num: 6",
                    "",
                ]
            ),
            encoding="utf-8",
        )
    return dest


def _region(cx: float, cy: float, width: float, height: float) -> str:
    x_ratio = cx / max(width, 1)
    y_ratio = cy / max(height, 1)
    if y_ratio >= 0.72:
        return "bottom"
    if y_ratio <= 0.22:
        return "title"
    return "other"


def _is_watermark(text: str, region: str, cx: float, cy: float, width: float, height: float) -> bool:
    x_ratio = cx / max(width, 1)
    y_ratio = cy / max(height, 1)
    if _WATERMARK.search(text) and (y_ratio < 0.22 or x_ratio > 0.72 or x_ratio < 0.14):
        return True
    if len(text.strip()) <= 2 and (y_ratio < 0.14 or x_ratio > 0.86 or x_ratio < 0.08):
        return True
    if region == "title" and "@" in text and len(text) < 24:
        return True
    return False


def run_ocr(
    video: Path,
    directory: Path,
    settings: Settings,
    *,
    probe: dict[str, Any],
    log_path: Path | None = None,
) -> dict[str, Any]:
    try:
        from rapidocr_onnxruntime import RapidOCR
    except ImportError as exc:
        raise RuntimeError("未安装 RapidOCR，请执行 uv sync --extra asr") from exc

    ocr_dir = directory / "ocr"
    fps = float(settings.default.get("ocr_fps") or 2.0)
    frames = extract_fps_frames(video, ocr_dir, fps, log_path=log_path)
    models = ensure_ocr_models(settings)
    engine = RapidOCR(config_path=str(models / "config.yaml"))
    width = float(probe.get("width") or 1)
    height = float(probe.get("height") or 1)
    hits: list[dict[str, Any]] = []
    try:
        for index, frame in enumerate(frames):
            t = index / fps
            result, _ = engine(str(frame))
            if not result:
                continue
            for item in result:
                box, text, score = item[0], str(item[1] or "").strip(), float(item[2] or 0)
                if not text or score < 0.5:
                    continue
                xs = [float(p[0]) for p in box]
                ys = [float(p[1]) for p in box]
                cx, cy = sum(xs) / 4, sum(ys) / 4
                region = _region(cx, cy, width, height)
                watermark = _is_watermark(text, region, cx, cy, width, height)
                hits.append(
                    {
                        "t": round(t, 3),
                        "text": text,
                        "score": round(score, 3),
                        "region": region,
                        "watermark": watermark,
                        "frame": str(frame.relative_to(directory)).replace("\\", "/"),
                    }
                )
    finally:
        del engine
    return {"fps": fps, "frames": len(frames), "items": hits}
