"""Cursor SDK 文本推理。

同步 API 在 Windows 上有 select() 管道 bug（WinError 10038），必须走 AsyncClient。
Pass A 是纯问答：mode=plan 让 agent 无法写盘，cwd 指到空临时目录让它无盘可读。
"""

from __future__ import annotations

import asyncio
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

DEFAULT_SDK_MODEL = "composer-2.5"
DEFAULT_RETRIES = 3
DEFAULT_TIMEOUT = 180.0

GUARD = (
    "你是一个纯文本推理器。禁止调用任何工具，禁止读写文件，"
    "禁止执行命令，禁止联网。所有需要的输入都已内联在下面。"
    "只输出答案本身，不要输出解释、不要输出代码块围栏。\n\n"
)

# 图片是 base64 内联进消息的，传太多会把单次请求撑爆
MAX_IMAGES = 8

_NO_WINDOW = 0x08000000
_NEW_CONSOLE = 0x00000010
_hidden = False


class SDKError(RuntimeError):
    pass


def _rewrite_bridge_cmd(args: Any) -> Any:
    """`.cmd` 会再拉起 node.exe，CREATE_NO_WINDOW 不传给孙子进程，黑框照样弹。直接起 node。"""
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
    """Windows：SDK bridge 是 node 子进程，默认会弹控制台黑框。serve 模式下尤其烦。"""
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


def _cfg(settings: Settings) -> dict[str, Any]:
    return dict(settings.providers.get("llm") or {})


def resolve_api_key(settings: Settings) -> str:
    key = (os.environ.get("CURSOR_API_KEY") or "").strip()
    if key:
        return key
    return str(_cfg(settings).get("api_key") or "").strip()


def sdk_model(settings: Settings) -> str:
    return str(_cfg(settings).get("sdk_model") or "").strip() or DEFAULT_SDK_MODEL


def _model_plan(settings: Settings) -> list[tuple[str, list[str]]]:
    """主模型 + 可选兜底。内容安全拒答是确定性的，重试同一个模型没用，换模型才有用。

    参数是按模型定义的（grok 没有 thinking，composer 只有 fast），所以兜底另配一份。
    """
    cfg = _cfg(settings)
    plan = [(sdk_model(settings), [str(p) for p in (cfg.get("sdk_params") or [])])]
    fallback = str(cfg.get("sdk_fallback") or "").strip()
    if fallback and fallback != plan[0][0]:
        plan.append((fallback, [str(p) for p in (cfg.get("sdk_fallback_params") or [])]))
    return plan


def _selection(model: str, params: list[str]) -> Any:
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


def _options(settings: Settings, model: str, params: list[str], sandbox: str) -> Any:
    from cursor_sdk import AgentOptions, LocalAgentOptions

    mode = str(_cfg(settings).get("sdk_mode") or "agent").strip() or "agent"
    return AgentOptions(
        model=_selection(model, params),
        api_key=resolve_api_key(settings),
        mode=mode,
        # 输入全部内联，所以 cwd 给一个空临时目录：读不到金标，写也只写进随后被删的目录。
        local=LocalAgentOptions(cwd=sandbox, setting_sources=[]),
        tools=[],
    )


def sdk_health(settings: Settings) -> tuple[bool, str]:
    try:
        import cursor_sdk  # noqa: F401
    except ImportError:
        return False, "未安装 cursor-sdk，请执行 uv sync --extra sdk"
    if not resolve_api_key(settings):
        return False, "缺少 CURSOR_API_KEY（环境变量，或 providers.yaml 的 llm.api_key）"
    plan = _model_plan(settings)
    head = " ".join([plan[0][0], *plan[0][1]])
    tail = f"，兜底 {plan[1][0]}" if len(plan) > 1 else ""
    mode = str(_cfg(settings).get("sdk_mode") or "agent").strip() or "agent"
    return True, f"cursor_sdk {head}{tail}（local，mode={mode}）"


def _usage(result: Any) -> tuple[int, int]:
    usage = getattr(result, "usage", None)
    if usage is None:
        return 0, 0
    return int(getattr(usage, "input_tokens", 0) or 0), int(getattr(usage, "output_tokens", 0) or 0)


def _message(text: str, images: Sequence[Path] | None) -> Any:
    """有图就发 UserMessage。图片走消息内联，不落进 sandbox，agent 仍然无盘可读。"""
    files = [p for p in (images or []) if Path(p).is_file()][:MAX_IMAGES]
    if not files:
        return text
    from cursor_sdk import SDKImage, UserMessage

    return UserMessage(text=text, images=[SDKImage.from_file(str(p)) for p in files])


async def _prompt_with_retry(
    settings: Settings,
    message: Any,
    sandbox: str,
    *,
    retries: int,
    timeout: float,
    trace: list[str],
) -> Any:
    from cursor_sdk import AsyncAgent, AsyncClient

    plan = _model_plan(settings)
    async with await AsyncClient.launch_bridge(workspace=sandbox) as client:
        for model, params in plan:
            for i in range(retries):
                if i:
                    await asyncio.sleep(2 * i)
                try:
                    result = await asyncio.wait_for(
                        AsyncAgent.prompt(
                            message, _options(settings, model, params, sandbox), client=client
                        ),
                        timeout=timeout,
                    )
                except TimeoutError:
                    # 超时说明这个模型在这道题上就是慢，同参数再等一遍只是白烧时间
                    trace.append(f"{model} 超时 {timeout:.0f}s，换模型")
                    break
                except Exception as exc:  # noqa: BLE001 - 网络/鉴权/内容安全都在这里，逐个重试
                    trace.append(f"{model} {type(exc).__name__}: {exc}")
                    continue
                status = str(getattr(result, "status", "") or "unknown")
                if status == "finished":
                    return result
                detail = getattr(result, "error", None) or getattr(result, "result", "") or ""
                trace.append(f"{model} run 状态 {status}：{str(detail)[:200]}")
    raise SDKError("Cursor SDK 全部失败：" + "；".join(trace[-4:] or ["未知错误"]))


def generate_text(
    settings: Settings,
    prompt: str,
    *,
    thinking: bool | None = None,  # noqa: ARG001 - agent 侧自己决定，保持签名一致
    max_new_tokens: int | None = None,  # noqa: ARG001
    images: Sequence[Path] | None = None,
    stats: dict[str, Any] | None = None,
) -> str:
    try:
        import cursor_sdk  # noqa: F401
    except ImportError as exc:
        raise SDKError("未安装 cursor-sdk，请执行 uv sync --extra sdk") from exc
    if not resolve_api_key(settings):
        raise SDKError("缺少 CURSOR_API_KEY（环境变量，或 providers.yaml 的 llm.api_key）")

    hide_child_windows()
    cfg = _cfg(settings)
    retries = max(1, int(cfg.get("sdk_retries") or DEFAULT_RETRIES))
    timeout = float(cfg.get("sdk_timeout") or DEFAULT_TIMEOUT)

    sandbox = tempfile.mkdtemp(prefix="vrs-sdk-")
    trace: list[str] = []
    try:
        message = _message(GUARD + prompt, images)
        result = asyncio.run(
            _prompt_with_retry(
                settings, message, sandbox, retries=retries, timeout=timeout, trace=trace
            )
        )
    except SDKError:
        if stats is not None:
            stats["trace"] = trace
        raise
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)

    if stats is not None:
        tok_in, tok_out = _usage(result)
        used = getattr(result, "model", "") or ""
        stats.update(
            model=str(getattr(used, "id", None) or used),
            run=str(getattr(result, "id", "") or ""),
            ms=getattr(result, "duration_ms", None),
            tok_in=tok_in,
            tok_out=tok_out,
            trace=trace,
        )
    text = strip_think(str(getattr(result, "result", "") or "")).strip()
    if not text:
        raise SDKError(f"Cursor SDK 返回空内容（run={getattr(result, 'id', '?')}）")
    return text
