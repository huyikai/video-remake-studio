"""LLM 客户端：Anthropic Messages API（默认） 或 Cursor SDK（兼容旧配置）。

两个 backend 都实现同样的 generate_text(settings, prompt, *, images=...) 接口，
vlclient / llmclient 不用关心后端是哪个。Anthropic 走 base_url + x-api-key，
通常用于 MiniMax 这类 Anthropic 兼容代理；Cursor SDK 仍保留以便回退到 grok-4.6。
"""

from __future__ import annotations

import asyncio
import base64
import mimetypes
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from vrs.settings import Settings
from vrs.textjson import strip_think

DEFAULT_SDK_MODEL = "grok-4.6"
DEFAULT_ANTHROPIC_MODEL = "MiniMax-M3"
DEFAULT_ANTHROPIC_BASE_URL = "https://api.minimax.cn/anthropic"
DEFAULT_RETRIES = 3
DEFAULT_TIMEOUT = 900.0
# max_new_tokens 缺省值：Pass B 实测单段输出 17k+ tokens，8192 以下会截断 JSON
DEFAULT_ANTHROPIC_MAX_TOKENS = 32768
# 图片按 base64 内联，过大会爆单次请求；与 cursor SDK 行为对齐。
MAX_IMAGES = 8

# Anthropic 与 Cursor SDK 共用：禁止工具/读盘/联网，所有输入已内联。
GUARD = (
    "你是一个图文推理器。禁止调用任何工具，禁止读写文件，"
    "禁止执行命令，禁止联网。所有需要的输入都已内联在下面。"
    "只输出答案本身，不要输出解释、不要输出代码块围栏。\n\n"
)

_NO_WINDOW = 0x08000000
_NEW_CONSOLE = 0x00000010
_hidden = False


class SDKError(RuntimeError):
    pass


# ── 共用：配置读取 ─────────────────────────────────────────────────────────────


def _cfg(settings: Settings, section: str = "llm") -> dict[str, Any]:
    return dict(settings.providers.get(section) or settings.providers.get("llm") or {})


def _kind(settings: Settings, section: str = "llm") -> str:
    return (
        str(_cfg(settings, section).get("kind") or _cfg(settings).get("kind") or "anthropic_sdk")
        .strip()
        .lower()
    )


def resolve_api_key(settings: Settings, section: str = "llm") -> str:
    """优先用 providers.yaml / local.yaml 里的 api_key，否则 ANTHROPIC_API_KEY / CURSOR_API_KEY。"""
    key = (
        str(_cfg(settings, section).get("api_key") or _cfg(settings).get("api_key") or "").strip()
    )
    if key:
        return key
    kind = _kind(settings, section)
    env_name = "ANTHROPIC_API_KEY" if kind == "anthropic_sdk" else "CURSOR_API_KEY"
    return (os.environ.get(env_name) or "").strip()


def sdk_model(settings: Settings, section: str = "llm") -> str:
    kind = _kind(settings, section)
    cfg = _cfg(settings, section)
    if kind == "anthropic_sdk":
        return (
            str(cfg.get("model") or _cfg(settings).get("model") or "").strip()
            or DEFAULT_ANTHROPIC_MODEL
        )
    return (
        str(cfg.get("sdk_model") or _cfg(settings).get("sdk_model") or "").strip()
        or DEFAULT_SDK_MODEL
    )


def _model_plan(settings: Settings, section: str = "llm") -> list[tuple[str, list[str]]]:
    """主模型 + 可选兜底；视觉和文本调用都走同一套配置。"""
    cfg = _cfg(settings, section)
    kind = _kind(settings, section)
    model = sdk_model(settings, section)
    params: list[str]
    fallback: str
    if kind == "anthropic_sdk":
        params = []
        fallback = ""
    else:
        params = [str(p) for p in (cfg.get("sdk_params") or _cfg(settings).get("sdk_params") or [])]
        fallback = str(cfg.get("sdk_fallback") or _cfg(settings).get("sdk_fallback") or "").strip()
    plan = [(model, params)]
    if fallback and fallback != model:
        plan.append((fallback, [str(p) for p in (cfg.get("sdk_fallback_params") or [])]))
    return plan


def _anthropic_base_url(settings: Settings, section: str = "llm") -> str:
    cfg = _cfg(settings, section)
    url = (
        str(cfg.get("base_url") or _cfg(settings).get("base_url") or "").strip()
        or (os.environ.get("ANTHROPIC_BASE_URL") or "").strip()
        or DEFAULT_ANTHROPIC_BASE_URL
    )
    return url.rstrip("/")


# ── Cursor SDK 兼容：隐藏 Windows 控制台 + bridge 子进程改直接起 node ────────


def _rewrite_bridge_cmd(args: Any) -> Any:
    if not args or isinstance(args, (str, bytes)):
        return args
    seq = list(args)
    if not str(seq[0]).replace("/", "\\").lower().endswith("cursor-sdk-bridge.cmd"):
        return args
    cmd_path = Path(seq[0])
    node = cmd_path.parent / "node.exe"
    js = cmd_path.parent.parent / "dist" / "bin" / "cursor-sdk-bridge.js"
    if node.is_file() and js.is_file():
        return [str(node), str(js), *seq[1:]]
    return args


def hide_child_windows() -> None:
    global _hidden
    if _hidden or sys.platform != "win32":
        return
    _hidden = True
    orig_init = subprocess.Popen.__init__

    def hidden_init(self: Any, args: Any, *rest: Any, **kwargs: Any) -> None:
        args = _rewrite_bridge_cmd(args)
        kwargs["creationflags"] = (int(kwargs.get("creationflags") or 0) | _NO_WINDOW) & ~_NEW_CONSOLE
        info = kwargs.get("startupinfo") or subprocess.STARTUPINFO()
        info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        info.wShowWindow = 0
        kwargs["startupinfo"] = info
        orig_init(self, args, *rest, **kwargs)

    subprocess.Popen.__init__ = hidden_init  # type: ignore[method-assign]


# ── 公共：健康检查 ────────────────────────────────────────────────────────────


def sdk_health(settings: Settings, section: str = "llm") -> tuple[bool, str]:
    kind = _kind(settings, section)
    plan = _model_plan(settings, section)
    head = " ".join([plan[0][0], *plan[0][1]])
    tail = f"，兜底 {plan[1][0]}" if len(plan) > 1 else ""
    if kind == "anthropic_sdk":
        if not resolve_api_key(settings, section):
            return False, "缺少 API key（providers.yaml 的 api_key，或环境变量 ANTHROPIC_API_KEY）"
        url = _anthropic_base_url(settings, section)
        return True, f"anthropic_sdk {head}{tail}（{url}）"
    # cursor_sdk
    try:
        import cursor_sdk  # noqa: F401
    except ImportError:
        return False, "未安装 cursor-sdk，请执行 uv sync --extra sdk"
    if not resolve_api_key(settings, section):
        return False, "缺少 CURSOR_API_KEY（环境变量，或 providers.yaml 的 api_key）"
    mode = str(_cfg(settings, section).get("sdk_mode") or "agent").strip() or "agent"
    return True, f"cursor_sdk {head}{tail}（local，mode={mode}）"


# ── Cursor SDK 实现（旧分支，保留） ──────────────────────────────────────────


def _cursor_selection(model: str, params: list[str]) -> Any:
    if not params:
        return model
    from cursor_sdk import ModelParameterValue, ModelSelection

    values = []
    for item in params:
        k, _, v = str(item).partition("=")
        if not v:
            raise SDKError(f"sdk_params 要写成 id=value，收到 {item!r}")
        values.append(ModelParameterValue(id=k.strip(), value=v.strip()))
    return ModelSelection(id=model, params=tuple(values))


def _cursor_message(text: str, images: Sequence[Path] | None) -> Any:
    files = [p for p in (images or []) if Path(p).is_file()][:MAX_IMAGES]
    if not files:
        return text
    from cursor_sdk import SDKImage, UserMessage

    return UserMessage(text=text, images=[SDKImage.from_file(str(p)) for p in files])


def _cursor_options(settings: Settings, model: str, params: list[str], sandbox: str, section: str) -> Any:
    from cursor_sdk import AgentOptions, LocalAgentOptions

    cfg = _cfg(settings, section)
    mode = str(cfg.get("sdk_mode") or "agent").strip() or "agent"
    return AgentOptions(
        model=_cursor_selection(model, params),
        api_key=resolve_api_key(settings, section),
        mode=mode,
        local=LocalAgentOptions(cwd=sandbox, setting_sources=[]),
        tools=[],
    )


async def _cursor_prompt_with_retry(
    settings: Settings,
    message: Any,
    sandbox: str,
    *,
    retries: int,
    timeout: float,
    trace: list[str],
    section: str,
) -> Any:
    from cursor_sdk import AsyncAgent, AsyncClient

    plan = _model_plan(settings, section)
    async with await AsyncClient.launch_bridge(workspace=sandbox) as client:
        for model, params in plan:
            for i in range(retries):
                if i:
                    await asyncio.sleep(2 * i)
                try:
                    result = await asyncio.wait_for(
                        AsyncAgent.prompt(
                            message,
                            _cursor_options(settings, model, params, sandbox, section),
                            client=client,
                        ),
                        timeout=timeout,
                    )
                except TimeoutError:
                    trace.append(f"{model} 超时 {timeout:.0f}s，换模型")
                    break
                except Exception as exc:  # noqa: BLE001
                    trace.append(f"{model} {type(exc).__name__}: {exc}")
                    continue
                status = str(getattr(result, "status", "") or "unknown")
                if status == "finished":
                    return result
                detail = getattr(result, "error", None) or getattr(result, "result", "") or ""
                trace.append(f"{model} run 状态 {status}：{str(detail)[:200]}")
    raise SDKError("Cursor SDK 全部失败：" + "；".join(trace[-4:] or ["未知错误"]))


# ── Anthropic 实现 ────────────────────────────────────────────────────────────


def _media_type(path: Path) -> str:
    mt, _ = mimetypes.guess_type(str(path))
    return mt or "image/png"


def _anthropic_image_block(path: Path) -> dict[str, Any]:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise SDKError(f"读图片失败 {path}: {exc}") from exc
    if len(data) > 10 * 1024 * 1024:
        raise SDKError(f"图片过大 {path}：{len(data)} bytes（MiniMax M3 单图上限 10MB）")
    b64 = base64.standard_b64encode(data).decode("ascii")
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": _media_type(path),
            "data": b64,
        },
    }


def _anthropic_user_content(prompt: str, images: Sequence[Path] | None) -> list[dict[str, Any]]:
    """组装 messages[0].content：有图用 content 数组（text + image 块），无图直接返回纯文本。"""
    files = [p for p in (images or []) if Path(p).is_file()][:MAX_IMAGES]
    if not files:
        return [{"type": "text", "text": prompt}]
    blocks: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    blocks.extend(_anthropic_image_block(p) for p in files)
    return blocks


async def _anthropic_prompt_with_retry(
    settings: Settings,
    prompt: str,
    images: Sequence[Path] | None,
    *,
    retries: int,
    timeout: float,
    max_new_tokens: int | None,
    thinking: bool | None,
    trace: list[str],
    section: str,
) -> Any:
    """返回的对象要兼容下游：要有 .result（text）、.usage（input_tokens/output_tokens）、
    .model、.id、.duration_ms。最简实现：自己包一个 dict。"""
    try:
        from anthropic import AsyncAnthropic
    except ImportError as exc:
        raise SDKError("未安装 anthropic，请执行 uv sync --extra sdk") from exc

    api_key = resolve_api_key(settings, section)
    if not api_key:
        raise SDKError("缺少 ANTHROPIC_API_KEY（providers.yaml 的 api_key，或环境变量）")
    base_url = _anthropic_base_url(settings, section)
    # timeout/max_retries 交给 SDK：内部重试关掉（外层循环管重试），超时与外层 asyncio.wait_for 对齐
    client = AsyncAnthropic(api_key=api_key, base_url=base_url, timeout=timeout, max_retries=0)

    plan = _model_plan(settings, section)
    # GUARD 走独立 system 参数，符合 Anthropic 协议；user 只放真正的任务 prompt。
    user_content = _anthropic_user_content(prompt, images)
    # thinking: True → 显式开（MiniMax-M3 = adaptive），False/None → 不传（用模型默认）
    thinking_kwargs: dict[str, Any] = (
        {"thinking": {"type": "adaptive"}} if thinking is True else {}
    )
    last_exc: Exception | None = None
    started = asyncio.get_event_loop().time()
    for model, _params in plan:
        for i in range(retries):
            if i:
                await asyncio.sleep(2 * i)
            try:
                resp = await asyncio.wait_for(
                    client.messages.create(
                        model=model,
                        max_tokens=max_new_tokens or DEFAULT_ANTHROPIC_MAX_TOKENS,
                        system=GUARD.rstrip(),
                        messages=[{"role": "user", "content": user_content}],
                        **thinking_kwargs,
                    ),
                    timeout=timeout,
                )
            except TimeoutError:
                trace.append(f"{model} 超时 {timeout:.0f}s，换模型")
                break
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                trace.append(f"{model} {type(exc).__name__}: {exc}")
                # 鉴权/参数错误不重试，直接换模型或抛
                status = getattr(exc, "status_code", None)
                if status is not None and 400 <= int(status) < 500 and status != 429:
                    break
                continue
            # 正常响应
            text_parts = [
                block.text
                for block in getattr(resp, "content", []) or []
                if getattr(block, "type", "") == "text"
            ]
            text = "\n".join(text_parts).strip()
            if not text:
                # MiniMax 偶发返回 200 但正文为空（thinking 吃满或服务端抽风）。
                # 当场重试比抛回上层重新走一遍机检便宜得多。
                trace.append(
                    f"{model} 返回空内容（stop={getattr(resp, 'stop_reason', '?')}），重试"
                )
                continue
            usage = getattr(resp, "usage", None)
            return {
                "result": text,
                "usage": {
                    "input_tokens": int(getattr(usage, "input_tokens", 0) or 0),
                    "output_tokens": int(getattr(usage, "output_tokens", 0) or 0),
                },
                "model": getattr(resp, "model", model),
                "id": getattr(resp, "id", ""),
                "duration_ms": int((asyncio.get_event_loop().time() - started) * 1000),
                "status": "finished",
            }
    if last_exc is not None:
        raise SDKError("Anthropic 全部失败：" + "；".join(trace[-4:] or ["未知错误"])) from last_exc
    raise SDKError("Anthropic 全部失败：" + "；".join(trace[-4:] or ["未知错误"]))


# ── 公共：generate_text 入口 ──────────────────────────────────────────────────


def _result_text(result: Any) -> str:
    if isinstance(result, dict):
        return str(result.get("result") or "")
    return str(getattr(result, "result", "") or "")


def _result_usage(result: Any) -> tuple[int, int]:
    usage = result.get("usage") if isinstance(result, dict) else getattr(result, "usage", None)
    if usage is None:
        return 0, 0
    if isinstance(usage, dict):
        return int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0)
    return int(getattr(usage, "input_tokens", 0) or 0), int(getattr(usage, "output_tokens", 0) or 0)


def _result_meta(result: Any) -> tuple[str, str, int | None]:
    if isinstance(result, dict):
        return (
            str(result.get("model") or ""),
            str(result.get("id") or ""),
            result.get("duration_ms"),
        )
    used = getattr(result, "model", "") or ""
    return (
        str(getattr(used, "id", None) or used),
        str(getattr(result, "id", "") or ""),
        getattr(result, "duration_ms", None),
    )


def generate_text(
    settings: Settings,
    prompt: str,
    *,
    thinking: bool | None = None,
    max_new_tokens: int | None = None,
    images: Sequence[Path] | None = None,
    stats: dict[str, Any] | None = None,
    section: str = "llm",
) -> str:
    kind = _kind(settings, section)
    cfg = _cfg(settings, section)
    retries = max(1, int(cfg.get("sdk_retries") or DEFAULT_RETRIES))
    timeout = float(cfg.get("sdk_timeout") or DEFAULT_TIMEOUT)
    # 调用方显式传的优先；否则读配置（providers.llm.thinking: true → adaptive 推理档）
    eff_thinking = bool(cfg.get("thinking")) if thinking is None else thinking
    trace: list[str] = []

    try:
        if kind == "anthropic_sdk":
            try:
                result = asyncio.run(
                    _anthropic_prompt_with_retry(
                        settings,
                        prompt,
                        images,
                        retries=retries,
                        timeout=timeout,
                        max_new_tokens=max_new_tokens,
                        thinking=eff_thinking,
                        trace=trace,
                        section=section,
                    )
                )
            except SDKError:
                if stats is not None:
                    stats["trace"] = trace
                raise
        else:
            hide_child_windows()
            sandbox = tempfile.mkdtemp(prefix="vrs-sdk-")
            try:
                message = _cursor_message(GUARD + prompt, images)
                result = asyncio.run(
                    _cursor_prompt_with_retry(
                        settings,
                        message,
                        sandbox,
                        retries=retries,
                        timeout=timeout,
                        trace=trace,
                        section=section,
                    )
                )
            except SDKError:
                if stats is not None:
                    stats["trace"] = trace
                raise
            finally:
                shutil.rmtree(sandbox, ignore_errors=True)
    except SDKError:
        raise

    if stats is not None:
        tok_in, tok_out = _result_usage(result)
        model, run_id, duration_ms = _result_meta(result)
        stats.update(
            model=model,
            run=run_id,
            ms=duration_ms,
            tok_in=tok_in,
            tok_out=tok_out,
            trace=trace,
        )
    text = strip_think(_result_text(result)).strip()
    if not text:
        model, run_id, _ = _result_meta(result)
        backend = "Anthropic" if kind == "anthropic_sdk" else "Cursor SDK"
        raise SDKError(f"{backend} 返回空内容（run={run_id or '?'}）")
    return text
