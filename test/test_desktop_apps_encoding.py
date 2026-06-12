from core import desktop_apps


def test_decode_command_output_falls_back_to_windows_code_page(monkeypatch):
    """即使 Python 处于 UTF-8 模式，也能解析 Windows 本地代码页输出。"""

    monkeypatch.setattr(desktop_apps.platform, "system", lambda: "Windows")
    monkeypatch.setattr(desktop_apps.locale, "getencoding", lambda: "utf-8")
    monkeypatch.setattr(desktop_apps.locale, "getpreferredencoding", lambda _do_setlocale=False: "utf-8")

    assert desktop_apps._decode_command_output("中文".encode("gbk")) == "中文"


def test_decode_command_output_replaces_unknown_bytes(monkeypatch):
    """未知编码不再抛异常，最多以替换字符保留可读输出。"""

    monkeypatch.setattr(desktop_apps.platform, "system", lambda: "Linux")
    monkeypatch.setattr(desktop_apps.locale, "getencoding", lambda: "utf-8")
    monkeypatch.setattr(desktop_apps.locale, "getpreferredencoding", lambda _do_setlocale=False: "utf-8")

    assert desktop_apps._decode_command_output(b"\xffstatus") == "\ufffdstatus"
