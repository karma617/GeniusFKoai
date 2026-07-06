#!/usr/bin/env python3
"""将 annimail_orders 目录下已有的卡密文件转换为新格式。

旧格式：包含订单头信息 + 重复的卡密列表
新格式：每行一个邮箱凭证（email----password----uuid----token），无多余内容
文件命名：{datetime}-{domain}.txt（如 2026-06-13_18-44-16-outlook.txt）

用法：
    python scripts/annimail_reformat_orders.py [orders_dir]
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# 卡密凭证行正则：email----password----uuid----token
CRED_LINE_RE = re.compile(
    r"[\w.-]+@(?:outlook|hotmail)\.com"
    r"----[\w-]+"
    r"----[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}"
    r"----.+"
)


def reformat_file(filepath: Path) -> tuple[Path | None, str]:
    """读取旧格式文件，提取凭证行，写入新格式文件。

    返回 (新文件路径, 状态信息)。无凭证行时返回 (None, 状态)。
    """
    content = filepath.read_text(encoding="utf-8")

    seen: set[str] = set()
    cred_lines: list[str] = []
    for line in content.split("\n"):
        line = line.strip()
        m = CRED_LINE_RE.match(line)
        if m and line not in seen:
            seen.add(line)
            cred_lines.append(line)

    if not cred_lines:
        return None, f"  跳过（无凭证行）: {filepath.name}"

    # 确定邮箱域名后缀
    first_email = cred_lines[0].split("----")[0].lower()
    if "@hotmail.com" in first_email:
        domain = "hotmail"
    else:
        domain = "outlook"

    # 构建新文件名（保留旧文件名的 stem 部分）
    stem = filepath.stem  # 去掉 .txt
    new_name = f"{stem}-{domain}.txt"
    new_path = filepath.parent / new_name

    # 写入新文件（仅凭证行，一行一个）
    new_path.write_text("\n".join(cred_lines) + "\n", encoding="utf-8")

    # 删除旧文件（如果新旧路径不同）
    if new_path != filepath:
        filepath.unlink()

    return new_path, f"  ✓ {filepath.name} → {new_name} ({len(cred_lines)} 条)"


def main(argv: list[str]) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    if len(argv) > 1:
        orders_dir = Path(argv[1])
    else:
        orders_dir = Path(__file__).resolve().parent / "annimail_orders"

    if not orders_dir.exists():
        print(f"目录不存在: {orders_dir}")
        return 1

    txt_files = sorted(orders_dir.glob("*.txt"))
    # 排除已经是新格式的文件（stem 以 -outlook 或 -hotmail 结尾）
    old_files = [
        f for f in txt_files
        if not f.stem.endswith("-outlook") and not f.stem.endswith("-hotmail")
    ]

    print(f"共 {len(txt_files)} 个 txt 文件，其中 {len(old_files)} 个需要处理")
    print()

    success = 0
    skipped = 0
    for f in old_files:
        new_path, msg = reformat_file(f)
        print(msg)
        if new_path:
            success += 1
        else:
            skipped += 1

    print(f"\n完成：处理 {success} 个，跳过 {skipped} 个")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
