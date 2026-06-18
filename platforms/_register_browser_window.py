from __future__ import annotations

from typing import Any


REGISTER_BROWSER_WINDOW_WIDTH = 1800
REGISTER_BROWSER_WINDOW_HEIGHT = 900
REGISTER_BROWSER_WINDOW_SIZE = (
    REGISTER_BROWSER_WINDOW_WIDTH,
    REGISTER_BROWSER_WINDOW_HEIGHT,
)
REGISTER_BROWSER_WINDOW_ARG = (
    f"--window-size={REGISTER_BROWSER_WINDOW_WIDTH},{REGISTER_BROWSER_WINDOW_HEIGHT}"
)


def register_browser_viewport() -> dict[str, int]:
    return {
        "width": REGISTER_BROWSER_WINDOW_WIDTH,
        "height": REGISTER_BROWSER_WINDOW_HEIGHT,
    }


def _replace_arg(args: list[Any], prefix: str, value: str) -> list[str]:
    normalized = [str(arg) for arg in args if str(arg or "").strip()]
    without_existing = [arg for arg in normalized if not arg.startswith(prefix)]
    without_existing.append(value)
    return without_existing


def apply_camoufox_register_window_size(launch_opts: dict[str, Any]) -> dict[str, Any]:
    launch_opts["window"] = REGISTER_BROWSER_WINDOW_SIZE
    return launch_opts


def apply_chromium_register_window_size(launch_opts: dict[str, Any]) -> dict[str, Any]:
    launch_opts["args"] = _replace_arg(
        list(launch_opts.get("args") or []),
        "--window-size=",
        REGISTER_BROWSER_WINDOW_ARG,
    )
    return launch_opts


def set_register_page_viewport(page: Any) -> None:
    setter = getattr(page, "set_viewport_size", None)
    if callable(setter):
        setter(register_browser_viewport())
