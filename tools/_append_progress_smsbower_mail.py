#!/usr/bin/env python3
from pathlib import Path

p = Path("progress.md")
text = p.read_text(encoding="utf-8") if p.exists() else ""
marker = "SMSBROWER谷歌邮箱"
if marker in text:
    print("progress already has entry")
    raise SystemExit(0)
entry = """

## 2026-07-25 - Task: 新增 SMSBROWER谷歌邮箱 provider

### 目标
- 在邮箱服务-第三方服务下新增 SMSBower 谷歌邮箱，注册任务可选后自动 getActivation 取号并 getCode 收码。

### 实现
- core/smsbower_mail_mailbox.py: SMSBower mail API 封装
- core/base_mailbox.py: 注册 factory
- infrastructure/provider_definitions_repository.py: 第三方服务定义（默认 domain=gmail.com, service=dr）
- providers/mailbox/smsbower_mail.py: 统一 registry 注册
- tests/test_smsbower_mail_mailbox.py: 工厂/取号/收码回归

### 验证
- pytest tests/test_smsbower_mail_mailbox.py
- 实网 probe: service=dr domain=gmail.com 可 getActivation，随后 setStatus=2 取消

### 回滚点
- 删除上述新增文件与定义/注册即可。
"""
p.write_text(text.rstrip() + entry, encoding="utf-8")
print("progress appended")
