"""成片音频一期：逐条响度归一 + 声道塌单声 + 接缝 room-tone 填洞。

H3 分片各自独立生成，直拼的三个可闻问题（h3-replica 实测）：
1. 响度条间跳变 —— 逐条 loudnorm 拉到 -12 LUFS（linear 二遍法，不改动态）；
2. H3 输出左右声道约 4% 去相关，立体声像漂 —— pan 塌成双单声；
3. 每条头尾 ~30ms 数字静音，硬切处出空洞 —— 用接缝两侧的 room tone
   （安静的非静音片段）填洞，只填洞、不碰真实语音，功率不突变。

只挂在 finish（成片链路），试片/生成产物不动。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from vrs.media import run_ffmpeg

TARGET_I = -12.0
TARGET_TP = -1.5
TARGET_LRA = 11.0
SR = 32000
# 低于 ~20ms 的洞人耳听不出来，不值得动
MIN_FILL_SEC = 0.020
SILENCE_DB = -60.0
EDGE_FADE_SEC = 0.005
TAIL_FADE_SEC = 0.02
PEAK_CEILING = 0.84


def _loudnorm_measure(src: Path, seconds: float, log_path: Path | None) -> dict[str, float] | None:
    """loudnorm 第一遍：只测不写，拿 measured_* 给第二遍 linear 模式用。"""
    import subprocess

    proc = subprocess.run(
        [
            "ffmpeg", "-v", "info", "-i", str(src), "-t", f"{seconds:.3f}",
            "-af", _AUDIO_CHAIN(""), "-f", "null", "-",
        ],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", proc.stderr)
    if not m:
        return None
    try:
        vals = json.loads(m.group(0))
        return {k: float(vals[k]) for k in ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset")}
    except (ValueError, KeyError, json.JSONDecodeError):
        return None


def _AUDIO_CHAIN(measured: str) -> str:
    pan = "pan=stereo|c0=0.5*c0+0.5*c1|c1=0.5*c0+0.5*c1"
    loud = f"loudnorm=I={TARGET_I}:TP={TARGET_TP}:LRA={TARGET_LRA}:linear=true{measured}"
    return f"{pan},{loud}"


def normalize_piece_audio(src: Path, dest: Path, seconds: float, *, log_path: Path | None = None) -> None:
    """单条分片音频母带化：塌单声 + linear loudnorm，视频流直拷，时长钉死。"""
    measured = _loudnorm_measure(src, seconds, log_path)
    extra = ""
    if measured:
        extra = (
            f":measured_I={measured['input_i']}:measured_TP={measured['input_tp']}"
            f":measured_LRA={measured['input_lra']}:measured_thresh={measured['input_thresh']}"
            f":offset={measured['target_offset']}"
        )
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".norm.mp4")
    run_ffmpeg(
        [
            "-i", str(src), "-t", f"{seconds:.3f}",
            "-c:v", "copy",
            "-af", _AUDIO_CHAIN(extra),
            "-c:a", "aac", "-b:a", "192k", "-ar", str(SR),
            "-movflags", "+faststart",
            str(tmp),
        ],
        log_path=log_path,
    )
    tmp.replace(dest)


def _extract_wav_bytes(src: Path) -> bytes:
    """soundfile/libsndfile 不认 mp4 容器；ffmpeg 抽成 32k 立体声 WAV 读进内存。
    Windows 上临时文件会被 libsndfile 句柄锁住删不掉，干脆全程内存。"""
    import io
    import subprocess

    proc = subprocess.run(
        [
            "ffmpeg", "-v", "error", "-i", str(src), "-vn",
            "-ar", str(SR), "-ac", "2", "-c:a", "pcm_s16le", "-f", "wav", "pipe:1",
        ],
        capture_output=True,
    )
    if proc.returncode != 0 or not proc.stdout:
        raise RuntimeError(f"抽音轨失败 {src}: {proc.stderr[:200]!r}")
    return proc.stdout


def _load_stereo(path: Path):
    import io

    import soundfile as sf

    data = path.read_bytes() if path.suffix.lower() == ".wav" else _extract_wav_bytes(path)
    with sf.SoundFile(io.BytesIO(data)) as handle:
        sr = handle.samplerate
        track = handle.read(dtype="float32", always_2d=True)
    return track.T, sr  # (2, n)


def _edge_silence(mono: "np.ndarray", from_end: bool) -> int:
    """头/尾数字静音长度（样本数）。-60dB、10ms 帧、封顶 80ms。"""
    import numpy as np

    frame = int(SR * 0.01)
    cap = int(SR * 0.08)
    thresh = 10 ** (SILENCE_DB / 20)
    data = mono[::-1] if from_end else mono
    silent = 0
    for start in range(0, min(len(data), cap), frame):
        chunk = data[start : start + frame]
        if chunk.size and float(np.sqrt(np.mean(chunk * chunk))) <= thresh:
            silent = start + frame
        else:
            break
    return min(silent, cap)


def _room_tone(mono: "np.ndarray", hi: int) -> "np.ndarray | None":
    """在 [hi-0.8s, hi) 里找最安静的 60ms 非静音窗当 room tone。"""
    import numpy as np

    win = int(SR * 0.06)
    span = int(SR * 0.8)
    lo = max(0, hi - span)
    best: np.ndarray | None = None
    best_energy = None
    for start in range(lo, max(lo + 1, hi - win), win):
        chunk = mono[start : start + win]
        if chunk.size < win:
            break
        energy = float(np.mean(chunk * chunk))
        if energy <= 0:
            continue
        if best_energy is None or energy < best_energy:
            best_energy = energy
            best = chunk.copy()
    return best


def _fade(signal: "np.ndarray", fade_in: int, fade_out: int) -> "np.ndarray":
    import numpy as np

    out = signal.copy()
    n = len(out)
    for i in range(min(fade_in, n)):
        out[i] *= i / max(1, fade_in)
    for i in range(min(fade_out, n)):
        out[-1 - i] *= i / max(1, fade_out)
    return out


def _fill_hole(track: "np.ndarray", hole_start: int, hole_len: int, tone: "np.ndarray") -> None:
    """把 room tone 平铺进静音洞，5ms 边缘淡入淡出，不碰洞外样本。"""
    import numpy as np

    if hole_len <= 0 or tone is None or tone.size == 0:
        return
    fill = np.tile(tone, hole_len // tone.size + 1)[:hole_len]
    fade = int(SR * EDGE_FADE_SEC)
    track[:, hole_start : hole_start + hole_len] = _fade(fill, fade, fade)[None, :]


def speech_islands(path: Path, *, sr: int = 16000) -> list[tuple[float, float]]:
    """检测音轨里的人声语音岛（180–3800Hz 带能量，自适应阈值）。

    H3 实际开口时间常偏离脚本节拍；字幕贴真实语音比贴源片推算准。
    返回 [(start, end), ...] 秒。检测不可靠（岛太少）时返回空，调用方保留脚本时间。
    """
    import librosa
    import numpy as np

    track, native_sr = _load_stereo(path)
    y = track.mean(axis=0)
    if native_sr != sr:
        y = librosa.resample(y, orig_sr=native_sr, target_sr=sr)
    if y.size < sr // 4:
        return []
    n_fft, hop = 512, 160  # 10ms 帧
    S = np.abs(librosa.stft(y, n_fft=n_fft, hop_length=hop))
    freqs = librosa.fft_frequencies(sr=sr, n_fft=n_fft)
    band = (freqs >= 180) & (freqs <= 3800)
    energy = S[band].mean(axis=0)
    if energy.size < 10:
        return []
    peak = float(energy.max())
    p70 = float(np.percentile(energy, 70))
    p25 = float(np.percentile(energy, 25))
    thresh = max(peak * 0.04, p70 * 1.15, p25 * 8.0)
    active = energy >= thresh

    frame_sec = hop / sr
    hang = int(0.16 / frame_sec)
    gap = int(0.18 / frame_sec)
    # 先补 ≤180ms 的洞，再展 160ms 保持沿
    padded = active.copy()
    run = 0
    for i, v in enumerate(active):
        if not v:
            run += 1
        else:
            if 0 < run <= gap:
                padded[i - run : i] = True
            run = 0
    extended = padded.copy()
    last = -hang * 2
    for i, v in enumerate(padded):
        if v:
            if i - last <= hang:
                extended[last : i + 1] = True
            last = i
    islands: list[tuple[float, float]] = []
    start = None
    for i, v in enumerate(extended):
        if v and start is None:
            start = i
        elif not v and start is not None:
            islands.append((start * frame_sec, i * frame_sec))
            start = None
    if start is not None:
        islands.append((start * frame_sec, len(extended) * frame_sec))
    # ≥150ms 且 ≥4 个高帧才算岛（h3-replica 口径）
    return [(a, b) for a, b in islands if b - a >= 0.15 and b - a >= 4 * frame_sec]


def align_events_to_speech(
    events: list[tuple],
    islands: list[tuple[float, float]],
) -> list[tuple]:
    """把对白事件贴到最近的语音岛上（中心距最近且未被他句占用，1.5s 内有效）。

    H3 漂移常达 1 秒、整句挪出脚本窗口，按重叠找会漏贴；
    检测不可靠或没贴上时保留脚本时间，最差等于现状。
    """
    if not islands:
        return events
    used: set[int] = set()
    out: list[tuple] = []
    for ev in events:
        style, t0, t1, text = ev[0], ev[1], ev[2], ev[3]
        if style != "bottom" or t1 - t0 <= 0.05:
            out.append(ev)
            continue
        center = (t0 + t1) / 2
        best = None
        best_d = 1.5  # 漂移超 1.5s 视为检测不可信，不贴
        for i, (a, b) in enumerate(islands):
            if i in used:
                continue
            d = abs((a + b) / 2 - center)
            if d < best_d:
                best_d = d
                best = (i, a, b)
        if best is None:
            out.append(ev)
            continue
        i, a, b = best
        used.add(i)
        new_t0 = max(0.0, a - 0.08)
        new_t1 = b + 0.12
        prev = out[-1] if out else None
        if prev is not None and new_t0 < prev[2] - 0.01:
            # 和上一条撞了（两句落进同一岛）：这一条保留脚本时间
            out.append(ev)
            continue
        out.append((style, round(new_t0, 3), round(new_t1, 3), text, *ev[4:]))
    return out


def master_audio(raw: Path, dest: Path, spans: list[dict], *, log_path: Path | None = None) -> int:
    """对合剪后的整轨做接缝填洞 + 峰值封顶 + 结尾淡出，重混进 dest。

    spans 用 concat_spans 的输出（cat0/cat1 是各段在合剪轴上的起止），
    边界即 cat1 处。返回填了几个洞。"""
    import numpy as np
    import soundfile as sf

    track, sr = _load_stereo(raw)
    if sr != SR:
        # librosa 已按 SR 重采样，这里 sr 就是 SR；防御性保留
        pass
    mono = track.mean(axis=0)
    filled = 0
    for span in spans[:-1]:
        boundary = int(round(float(span["cat1"]) * SR))
        if boundary <= 0 or boundary >= mono.size:
            continue
        tail_len = _edge_silence(mono[:boundary], from_end=True)
        head_len = _edge_silence(mono[boundary:], from_end=False)
        hole = tail_len + head_len
        if hole < int(SR * MIN_FILL_SEC):
            continue
        tone = _room_tone(mono[: boundary - tail_len], boundary - tail_len)
        if tone is None:
            tone = _room_tone(mono[boundary + head_len :], int(SR * 0.8))
        if tone is None:
            continue
        _fill_hole(track, boundary - tail_len, hole, tone)
        filled += 1

    peak = float(np.max(np.abs(track))) if track.size else 0.0
    if peak > PEAK_CEILING:
        track *= PEAK_CEILING / peak
    fade = int(SR * TAIL_FADE_SEC)
    if track.size and fade > 0:
        track[:, -fade:] *= np.linspace(1.0, 0.0, fade)[None, :]

    wav = dest.with_suffix(".master.wav")
    sf.write(str(wav), track.T, SR, subtype="PCM_16")
    tmp = dest.with_suffix(dest.suffix + ".master.mp4")
    run_ffmpeg(
        [
            "-i", str(raw), "-i", str(wav),
            "-map", "0:v", "-map", "1:a",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart",
            str(tmp),
        ],
        log_path=log_path,
    )
    tmp.replace(dest)
    wav.unlink(missing_ok=True)
    return filled
