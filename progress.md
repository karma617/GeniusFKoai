## 2026-07-01 - Task: 自动注册 ChatGPT 增加"强入K12空间"开关

### What was done
- 在自动注册 ChatGPT 弹窗中新增复选框"强入K12空间"，位置在"注册成功后自动获取支付链接"之上，仅 chatgpt 平台展示；下方带一个可编辑的"母号 Workspace ID"输入框（支持逗号/换行分隔）。
- 勾选后注册流程改为：账号注册成功、拿到 /api/auth/session 返回的 JSON 后，跳过接码/支付链接分支，直接：
  1. 使用注册得到的 accessToken 与 cookies，按脚本 v4 契约向配置的每个 workspace ID POST /backend-api/accounts/{ws}/invites/request；
  2. 按 HTML 转换脚本 convertSession.sub2apiAccount 结构，把 session JSON 转成 sub2api 账号格式；
  3. 复用 Settings 页已有 sub2api 配置（sub2api_url / email / password / group_name / account_priority / default_proxy_name），直接 POST /api/v1/admin/accounts 上传到 sub2api 云端。
- 复用现有 sub2api 登录/分组/代理辅助函数，避免与 sub2api_upload.py 逻辑重复实现。workspace 加入申请与 sub2api 上传失败均以 warning 记录，不阻断注册结果，符合原有"注册成功即成功"的语义。

### Testing
- .venv\Scripts\python.exe -m pytest tests/test_k12_join.py -x -q -> 3 passed（K12 转换 & workspace 参数解析）。
- .venv\Scripts\python.exe -m pytest tests/test_platform_action_task.py tests/test_chatgpt_get_rt_har.py -x -q -> 66 passed（注册/上传主链路回归）。
- cd frontend && node_modules\.bin\tsc.cmd -b --noEmit -> 无错误。

### Notes
- 修改文件清单
  - platforms/chatgpt/k12_join.py: 新增。K12 流程后端，含 convert_session_to_sub2api_account、upload_session_to_sub2api、send_workspace_join_requests 与 parse_workspace_ids。
  - application/tasks.py: 新增 _run_k12_join_followup；在 _do_one 的 _auto_followup_windsurf_payment 之后、_shortlink_reuse 分支之前接入 K12 分支，勾选后跳过默认支付/接码链路。
  - frontend/src/pages/Accounts.tsx: 新增 k12Join / k12WorkspaceIds 两个 state，弹窗中新增 K12 复选框与 workspace 输入框；start() 中把 k12_join 与 k12_workspace_ids 塞进 extra。
  - tests/test_k12_join.py: 新增回归测试。
  - progress.md、docs/k12-space-join.md: 新增。
- 回滚点：以下改动均围绕新增文件与 tasks.py 的两块 K12 代码；如需回滚，删除新文件并撤销 tasks.py 中 _run_k12_join_followup 定义与 _do_one 里 K12 分支、Accounts.tsx 里 k12Join/k12WorkspaceIds 相关新增代码即可。git diff 可全部定位到本次改动。

## 2026-07-01 - Task: 改写 K12 流程为 join→sleep→exchange→upload 完整串行链路

### What was done
将 _run_k12_join_followup 从旧"join 全部 → 直接 upload 注册 session"改写为完整 K12 串行流程：按顺序向配置的 workspace 发 join 请求，命中首个 2xx 即停；join 成功后延时 3s 调用 exchange_workspace_session 切换到 K12 workspace 获取新 session；用 K12 session 转换为 sub2api 格式上传到云端。exchange 失败时回退用注册 session 上传并记 warning；未配置 workspace 或全部 join 失败时直接用注册 session 上传并记 warning。同时更新 k12_join.py 新增 exchange_workspace_session 和 send_workspace_join_and_pick_first_success 两个函数的正式接入。docs/k12-space-join.md 同步更新后端接入点和流程描述。

### Testing
- pytest tests/test_k12_join.py -x -q -> 3 passed
- pytest tests/test_platform_action_task.py tests/test_chatgpt_get_rt_har.py -x -q -> 66 passed
- python -c "import ast; ast.parse(open('application/tasks.py'...)" -> syntax OK
- cd frontend && tsc -b --noEmit -> 无错误
- cd frontend && npm run build -> 成功

### Notes
- 修改文件清单
  - application/tasks.py: 改写 _run_k12_join_followup，替换 send_workspace_join_requests 为 send_workspace_join_and_pick_first_success + exchange_workspace_session，实现 join→sleep 3s→exchange→upload K12 session 完整流程。
  - platforms/chatgpt/k12_join.py: 新增 exchange_workspace_session（GET /api/auth/session?exchange_workspace_token=true，3 次重试）和 send_workspace_join_and_pick_first_success（顺序 join，首个 2xx 即返回 chosen workspace_id）。本轮正式被 tasks.py 调用接入。
  - docs/k12-space-join.md: 更新后端接入点列表与流程描述，补充 exchange 步骤。
- 回滚点：git diff application/tasks.py 可定位 _run_k12_join_followup 改写；如需回退，撤销该函数到上一版本（旧版使用 send_workspace_join_requests + 直接 upload 注册 session）。

## 2026-07-01 - Task: 修复 K12 模式在 platform_reference 注册路径下未跳过 token exchange 的问题

### What was done
根因：默认注册路径走 _run_platform_reference_register，该函数在 _platform_reference_create_account 之后直接调 _complete_platform_oauth 做 platform token exchange，而 K12 跳过逻辑在第 15 步、根本不在这条路径上。修复：在 _platform_reference_create_account 之后、_complete_platform_oauth 之前插入 K12 短路判断，勾选 skip_post_register_oauth 时直接调新方法 _k12_shortcut_result，该方法跟随 callback URL 设置 chatgpt.com session cookie、访问 chatgpt.com 首页触发 NextAuth、再调 /api/auth/session 拿到完整 session JSON，把 session/cookies/access_token 写入 result.metadata 后返回，完全跳过 platform OAuth token exchange。同时修复了 @dataclass 被误加到 _K12SkipOAuth 上导致 RegistrationResult() takes no arguments 的回归。

### Testing
- python -c "import ast; ast.parse(...)" -> register.py syntax OK
- python -c "from platforms.chatgpt.register import RegistrationEngine; inspect.getsource(...)" -> _k12_shortcut_result 方法存在
- pytest tests/test_k12_join.py tests/test_platform_action_task.py -x -q -> 64 passed

### Notes
- 修改文件清单
  - platforms/chatgpt/register.py: (1) 移除 _K12SkipOAuth 上误加的 @dataclass，恢复 RegistrationResult 上应有的 @dataclass。(2) 在 _run_platform_reference_register 中 _platform_reference_create_account 之后插入 K12 短路判断。(3) 新增 _k12_shortcut_result 方法：跟随 callback→访问 chatgpt.com→调 /api/auth/session→写入 result.metadata 并返回。
- 回滚点：git diff platforms/chatgpt/register.py 可定位 _k12_shortcut_result 新增方法和 _run_platform_reference_register 中 3 行短路判断；撤销即可恢复原来的 platform OAuth token exchange 流程。

## 2026-07-01 - Task: K12 模式改走 chatgpt.com NextAuth 授权链拿 session

### What was done
定位真实根因：Platform 注册返回的 continue_url 是 platform.openai.com/auth/callback（Platform client 的 code），跟 chatgpt.com 是不同 client / redirect，直接跟随它 chatgpt.com 那边不会建立 NextAuth 会话，所以 /api/auth/session 返回空 JSON、无 accessToken。K12 分支改成注册完成后用当前 auth.openai.com 登录态重新发起一次 chatgpt.com/api/auth/signin/openai NextAuth 授权链，让 auth.openai.com 直接 302 回 chatgpt.com/api/auth/callback/openai?code=... 完成 NextAuth 登录，再拉 /api/auth/session；若 NextAuth 通道失败，再兜底跟随 platform callback + 访问 chatgpt.com 首页重试一次。同时把 _k12_fetch_chatgpt_session/_k12_kick_chatgpt_nextauth_login 拆成独立方法，metadata 里加 k12_session_flow 标记当前用的通道。日志开头两行 “ChatGPT 代理预检失败” 是 _chatgpt_proxy_preflight 用 curl_cffi 直连 chatgpt.com 时代理链路上 curl-cffi 的 boringssl 报错，属于预检 warning，不阻塞注册，兜底逻辑保留原样、未改动。

### Testing
- pytest tests/test_k12_join.py tests/test_chatgpt_proxy_preflight.py tests/test_platform_action_task.py -x -q -> 67 passed

### Notes
- 修改文件清单
  - platforms/chatgpt/register.py: 重写 _k12_shortcut_result；新增 _k12_fetch_chatgpt_session、_k12_kick_chatgpt_nextauth_login。K12 分支不再依赖 platform callback 建立 chatgpt.com session，改走 chatgpt.com NextAuth 授权链，兜底路径保留。
- 回滚点：git diff platforms/chatgpt/register.py 可看到本轮改动集中在 _k12_shortcut_result 及新增的两个 K12 辅助方法；如需回退，把这三段函数替换回上一版单纯跟随 platform callback 的实现即可。

## 2026-07-02 - Task: 修复 K12 exchange 缺少 ChatGPT Web 登录态导致无 accessToken

### What was done
- 根据 HAR 确认 K12 切换 workspace 的成功请求依赖 chatgpt.com 同源 NextAuth cookie，不依赖 Bearer Authorization；当前失败日志中 exchange 只返回 WARNING_BANNER，根因是 platform_reference 注册结果没有保存可用于 chatgpt.com /api/auth/session 的 Web session/cookies。
- K12 模式下，platform_reference 注册成功后额外补建一次 chatgpt.com NextAuth Web session，并把 session JSON、cookies、session_token 写入 result.metadata；非 K12 注册不触发这一步，避免普通注册多做 OAuth 请求。
- K12 后续流程优先使用 metadata.session.accessToken；如果 Cookie header 缺少 __Secure-next-auth.session-token，则用 session_token 补齐；exchange 请求按 HAR 形态改为 Cookie 登录态请求，不再主动附加 Authorization。
- sub2api 上传仍沿用当前 upload_session_to_sub2api 路径，最终上传目标保持为 sub2api，不改成参考项目里的 CPA 本地导出。

### Testing
- .venv\Scripts\python.exe -m pytest tests\test_k12_join.py -q -> 5 passed，1 个 StarletteDeprecationWarning。
- .venv\Scripts\python.exe -m py_compile platforms\chatgpt\register.py platforms\chatgpt\protocol_mailbox.py platforms\chatgpt\k12_join.py -> 无输出，编译通过。

### Notes
- 修改文件清单
  - platforms/chatgpt/register.py: K12 模式下 platform_reference 注册后补建 ChatGPT Web session，并保存 K12 后续需要的 session/cookies/session_token。
  - platforms/chatgpt/protocol_mailbox.py: K12 follow-up 优先使用 ChatGPT Web session token，并补齐 session cookie 后再 join/exchange/upload。
  - platforms/chatgpt/k12_join.py: 新增 ensure_chatgpt_session_cookie；exchange 请求改为 Cookie 登录态请求，并对 WARNING_BANNER 输出缺少 NextAuth 登录态的诊断。
  - tests/test_k12_join.py: 增加 session cookie 补齐行为的回归测试。
  - docs/k12-space-join.md: 更新 K12 完整流程，明确 Platform token 与 ChatGPT Web session 的区别。
  - progress.md: 追加本轮进度、验证和回滚说明。
- 回滚点：撤销本轮对上述文件的 diff 即可恢复；重点回滚 register.py 中 _establish_chatgpt_web_session_for_platform_reference 及其 K12 条件调用、protocol_mailbox.py 中 K12 session/cookie 选择逻辑、k12_join.py 中 ensure_chatgpt_session_cookie 与 exchange Authorization 调整。

## 2026-07-02 - Task: 修复 NextAuth 停在 choose-an-account 导致 K12 无 session cookie

### What was done
- 根据 14:16 注册日志确认新失败点：NextAuth signin/openai 成功返回 authorize URL，但跟随后停在 `https://auth.openai.com/choose-an-account`，代码没有继续选择账号，因此 `chatgpt.com/api/auth/session` 仍只返回 WARNING_BANNER。
- NextAuth authorize URL 现在会补 `login_hint=<当前邮箱>` 与 `screen_hint=login`，优先避免进入账号选择页。
- 如果仍进入 choose-an-account / workspace select 页面，后端会读取 `client_auth_session_dump` 或 auth cookie 中的 workspace，调用 `/api/accounts/workspace/select`，取得 `chatgpt.com/api/auth/callback/openai?...` 后再跟随 callback 建立 chatgpt.com session cookie。
- 增加 HTML form 提交兜底：当 workspace dump 无法给出可用 workspace 时，尝试提交 choose-an-account 页面中的第一个 form，再跟随返回的 callback。

### Testing
- .venv\Scripts\python.exe -m pytest tests\test_k12_join.py -q -> 6 passed，1 个 StarletteDeprecationWarning。
- .venv\Scripts\python.exe -m pytest tests\test_chatgpt_protocol_sms_oauth.py -q -> 3 passed，1 个 StarletteDeprecationWarning。
- .venv\Scripts\python.exe -m py_compile platforms\chatgpt\register.py platforms\chatgpt\protocol_mailbox.py platforms\chatgpt\k12_join.py -> 无输出，编译通过。

### Notes
- 修改文件清单
  - platforms/chatgpt/register.py: NextAuth 补建流程新增 login_hint、choose-an-account 检测、workspace/select callback 补全和 form 兜底。
  - tests/test_k12_join.py: 新增 choose-an-account 经 workspace/select 成功建立 ChatGPT Web session 的回归测试。
  - docs/k12-space-join.md: 补充 NextAuth choose-an-account 处理流程。
  - progress.md: 追加本轮进度、验证和回滚说明。
- 回滚点：撤销 register.py 中 _add_login_hint_to_auth_url、_resolve_chatgpt_nextauth_callback、_resolve_chatgpt_nextauth_callback_via_workspace_select、_select_first_organization_for_nextauth、_workspace_id_from_auth_payload、_submit_first_auth_form_for_callback 及 _establish_chatgpt_web_session_for_platform_reference 中对应调用；同时撤销 tests/test_k12_join.py、docs/k12-space-join.md、progress.md 的本轮新增内容。

## 2026-07-02 - Task: K12 上传 SUB2API 时固定 plan_type 为 k12

### What was done
- 修正 K12 session 转 sub2api payload 的计划类型：即使 ChatGPT Web accessToken claims 里仍显示 `free`，上传到 SUB2API 的 `accounts.credentials.plan_type` 固定写为 `k12`。
- 更新 K12 回归测试，用 accessToken claims 为 `free` 的输入确认导出 credentials 仍为 `k12`。

### Testing
- .venv\Scripts\python.exe -m pytest tests\test_k12_join.py -q -> 6 passed，1 个 StarletteDeprecationWarning。
- .venv\Scripts\python.exe -m py_compile platforms\chatgpt\k12_join.py -> 无输出，编译通过。

### Notes
- 修改文件清单
  - platforms/chatgpt/k12_join.py: 新增 K12_SUB2API_PLAN_TYPE，并在 convert_session_to_sub2api_account 中固定 sub2api credentials.plan_type 为 k12。
  - tests/test_k12_join.py: 将 session token claims 的 plan_type 设为 free，并断言导出的 sub2api credentials.plan_type 为 k12。
  - docs/k12-space-join.md: 补充 K12 上传 payload 的 plan_type 固定规则。
  - progress.md: 追加本轮进度、验证和回滚说明。
- 回滚点：撤销本轮对 k12_join.py 中 K12_SUB2API_PLAN_TYPE 与 plan_type 固定赋值、test_k12_join.py 对 plan_type 断言的调整，以及 docs/progress 的对应追加即可恢复旧行为。

## 2026-07-02 - Task: 修复邮箱别名额度耗尽后本机占用锁残留

### What was done
- 修复 EmailAliasMailbox 在 parent alias quota exhausted 时的释放逻辑：保留“临时占住已满 parent 以便尝试下一个邮箱”的策略，但在 get_email 成功返回或最终抛出 quota exhausted 前，统一释放这些临时占用的 parent。
- 增加去重释放，避免同一个 parent 在重试判断中被重复释放。
- 增加回归测试覆盖真实失败形态：第一个 parent 已满后，底层 outlookEmail 下一次直接报“当前可用邮箱都已被本机其他任务占用”，wrapper 仍必须释放第一次占用的 parent。

### Testing
- .venv\Scripts\python.exe -m pytest tests\test_email_alias_mailbox.py -q -> 7 passed，1 个 StarletteDeprecationWarning。
- .venv\Scripts\python.exe -m py_compile core\email_alias_mailbox.py -> 无输出，编译通过。

### Notes
- 修改文件清单
  - core/email_alias_mailbox.py: 新增 _release_held_full_parents，并在 get_email 的成功/失败出口释放临时占住的已满 parent。
  - tests/test_email_alias_mailbox.py: 新增 quota exhausted 后底层邮箱池报本机占用时仍释放 parent 的回归测试。
  - progress.md: 追加本轮进度、验证和回滚说明。
- 回滚点：撤销 core/email_alias_mailbox.py 中 held_full_parents/_release_held_full_parents 相关改动，以及 tests/test_email_alias_mailbox.py 本轮新增测试即可恢复旧行为。

## 2026-07-02 - Task: 别名额度耗尽后切换新主邮箱继续注册

### What was done
- 调整 EmailAliasMailbox 的主邮箱选择逻辑：当当前主邮箱已达到 alias_limit 或总成功上限时，先把该主邮箱标记为已使用并释放本机占用，再继续向底层邮箱池请求下一个主邮箱。
- 修复重复返回同一已满主邮箱时的二次释放问题：首次标记已满后如果底层仍返回同一主邮箱，包装层直接结束本轮选择，不再重复释放同一占用。
- 增加回归测试覆盖已满主邮箱后成功切换到新主邮箱生成别名，避免单个主邮箱额度耗尽直接终止整条注册任务。
- 补充 docs/email-alias-mailbox.md，说明别名额度耗尽后的切换与失败边界。

### Testing
- .venv\Scripts\python.exe -m pytest tests\test_email_alias_mailbox.py -q -> 8 passed，1 个 StarletteDeprecationWarning。
- .venv\Scripts\python.exe -m py_compile core\email_alias_mailbox.py -> 无输出，编译通过。

### Notes
- 修改文件清单
  - core/email_alias_mailbox.py: 已满主邮箱改为先标记已使用并释放，再继续切换下一个主邮箱；重复已满主邮箱不再二次释放。
  - tests/test_email_alias_mailbox.py: 增加已满主邮箱后切换新主邮箱的回归测试，并校验释放/标记行为。
  - docs/email-alias-mailbox.md: 新增邮箱别名额度耗尽后的运行规则说明。
  - progress.md: 追加本轮进度、验证和回滚说明。
- 回滚点：撤销 core/email_alias_mailbox.py 中 _mark_parent_alias_quota_exhausted 与 get_email 已满分支调整，撤销 tests/test_email_alias_mailbox.py 中切换新主邮箱相关测试，删除 docs/email-alias-mailbox.md，并移除 progress.md 本轮追加记录即可恢复旧行为。

## 2026-07-02 - Task: 别名主邮箱耗尽不消耗注册名额

### What was done
- 修复任务调度层对 `Email alias quota exhausted` 的处理：这类错误现在作为邮箱别名软重试信号返回，不再先写入注册失败和错误计数。
- 启用邮箱别名时，普通注册会按成功缺口继续补投同一个注册目标；`count=1` 时遇到多个已满主邮箱也会继续尝试切换新主邮箱，而不是直接结束任务。
- 保留兜底上限：每个目标最多额外尝试 20 次别名主邮箱切换；如果仍没有可用主邮箱，才把任务判定为失败。
- 增加任务级回归测试，覆盖第一次遇到 `Email alias quota exhausted`、第二次继续注册成功的场景。

### Testing
- .venv\Scripts\python.exe -m pytest tests\test_platform_action_task.py tests\test_email_alias_mailbox.py -q -> 70 passed，1 个 StarletteDeprecationWarning。
- .venv\Scripts\python.exe -m py_compile application\tasks.py core\email_alias_mailbox.py -> 无输出，编译通过。

### Notes
- 修改文件清单
  - application/tasks.py: 新增邮箱别名软重试结果，调度层对主邮箱耗尽不计失败并继续补投，且设置有限重试上限。
  - tests/test_platform_action_task.py: 增加主邮箱耗尽后继续注册成功的任务级回归测试。
  - docs/email-alias-mailbox.md: 补充任务层软重试行为说明。
  - progress.md: 追加本轮进度、验证和回滚说明。
- 回滚点：撤销 application/tasks.py 中 EMAIL_ALIAS_PARENT_RETRY_RESULT、EMAIL_ALIAS_PARENT_RETRY_LIMIT_PER_ACCOUNT、_is_email_alias_parent_exhausted_error 扩展、_do_one 软重试返回和调度层补投逻辑；撤销 tests/test_platform_action_task.py 本轮新增测试；撤销 docs/progress 本轮追加内容。

## 2026-07-02 - Task: 修复 outlookEmail 可用邮箱误判与别名补投范围

### What was done
- 修正上一轮邮箱别名补投条件：只有 `_do_one` 返回内部别名软重试标记时，才增加一次补投额度；普通 `outlookEmail 账号列表中没有可用邮箱` 不再被重复执行到第 21/1 次。
- 修复 outlookEmail 账号选择只看当前页的问题：当当前页账号都带 `skip_tags=已注册` 时，会按相同 group/tag/sort 条件继续请求下一页，直到找到未跳过邮箱或扫描范围内确实没有可用账号。
- 增加回归测试覆盖两种真实问题：无可用邮箱不能被别名补投重复执行；第一页全是已注册账号时应继续扫下一页找到 fresh 邮箱。

### Testing
- .venv\Scripts\python.exe -m pytest tests\test_outlook_email_mailbox.py tests\test_platform_action_task.py tests\test_email_alias_mailbox.py -q -> 94 passed，1 个 StarletteDeprecationWarning。
- .venv\Scripts\python.exe -m py_compile application\tasks.py core\email_alias_mailbox.py core\outlook_email_mailbox.py -> 无输出，编译通过。

### Notes
- 修改文件清单
  - application/tasks.py: 别名软重试改为按实际软重试次数增加补投额度，普通邮箱池错误不再重复补投。
  - core/outlook_email_mailbox.py: 账号选择支持分页扫描，避免当前页全是跳过标签时误判整个 group 无可用邮箱。
  - tests/test_platform_action_task.py: 增加 outlookEmail 无可用邮箱不重复补投的任务级回归测试。
  - tests/test_outlook_email_mailbox.py: 增加第一页全是 skip tag 时扫描下一页的回归测试。
  - docs/email-alias-mailbox.md: 补充 outlookEmail 分页选择规则。
  - progress.md: 追加本轮进度、验证和回滚说明。
- 回滚点：撤销 application/tasks.py 中 email_alias_retry_count 与 allowed_attempts 调整；撤销 core/outlook_email_mailbox.py 中分页参数、分页迭代器和 _select_account 跨页扫描；撤销 tests/test_platform_action_task.py、tests/test_outlook_email_mailbox.py 的本轮新增测试；撤销 docs/progress 本轮追加内容。

## 2026-07-02 - Task: 收紧 K12 强入成功判定

### What was done
- 修复 K12 join/exchange 成功误判：join 接口 HTTP 2xx 只代表 join request accepted，不再直接记录为“成功加入 workspace”。
- exchange session 成功判定新增严格校验：返回 JSON 必须包含 accessToken，且 `account.id` 必须等于目标 workspace ID；否则视为 exchange 校验失败，不上传 SUB2API。
- join 响应如果 HTTP 200 但 body 显式 `success:false`，不再视为成功。
- 更新 K12 日志文案，只有 exchange 校验通过后才记录“已确认切换到目标 K12 workspace”。

### Testing
- .venv\Scripts\python.exe -m pytest tests\test_k12_join.py -q -> 10 passed，1 个 StarletteDeprecationWarning。
- .venv\Scripts\python.exe -m py_compile platforms\chatgpt\k12_join.py platforms\chatgpt\protocol_mailbox.py -> 无输出，编译通过。

### Notes
- 修改文件清单
  - platforms/chatgpt/k12_join.py: 新增 join body success 校验和 exchange session account.id 校验，防止 HTTP 200 误判强入成功。
  - platforms/chatgpt/protocol_mailbox.py: K12 日志改为 join request accepted，exchange 校验通过后才提示已切换到目标 workspace。
  - tests/test_k12_join.py: 增加 account.id 不匹配拒绝、匹配通过、join success:false 不通过的回归测试。
  - docs/k12-space-join.md: 更新 K12 成功判定和失败策略说明。
  - progress.md: 追加本轮进度、验证和回滚说明。
- 回滚点：撤销 k12_join.py 中 _join_response_ok、_session_account_id、validate_workspace_exchange_session 及 exchange 校验调用；撤销 protocol_mailbox.py 的 K12 日志文案调整；撤销 tests/test_k12_join.py、docs/k12-space-join.md、progress.md 的本轮追加内容。

## 2026-07-02 - Task: K12 上传 SUB2API 增加重试并取消 rt 限制

### What was done
- 修复 K12 session 上传 SUB2API 遇到网络、TLS、限流或 5xx 请求异常时一次失败即结束的问题；现在首次上传失败后会再重试 3 次。
- K12 类型账号走单账号上传 SUB2API 时，不再因为缺少 `refresh_token` 被普通账号 rt 校验拦截；普通账号仍保持原有 rt 要求。
- K12 类型账号上传 SUB2API 直建接口时不再写入 `rate_multiplier`，避免按普通账号的倍率限制处理，并同步保留 `credentials.plan_type=k12`。
- 增加回归测试覆盖 SUB2API TLS 类请求异常重试成功、K12 payload 不包含 `rate_multiplier`、K12 单账号无 rt 可生成 SUB2API payload、普通账号无 rt 仍被拒绝。
- 补充 K12 文档，说明上传重试边界、K12 不设置 `rate_multiplier`，以及单账号 K12 无 rt 可上传的行为。

### Testing
- .venv\Scripts\python.exe -m pytest tests\test_k12_join.py tests\test_sub2api_upload.py -q -> 15 passed，1 个 StarletteDeprecationWarning。
- .venv\Scripts\python.exe -m py_compile platforms\chatgpt\k12_join.py platforms\chatgpt\sub2api_upload.py platforms\chatgpt\protocol_mailbox.py -> 无输出，编译通过。
- git diff --check -- platforms\chatgpt\k12_join.py platforms\chatgpt\sub2api_upload.py tests\test_k12_join.py tests\test_sub2api_upload.py docs\k12-space-join.md progress.md -> 无空白错误；仅提示 sub2api_upload.py 的 LF/CRLF 工作区换行警告。

### Notes
- 修改文件清单
  - platforms/chatgpt/k12_join.py: K12 SUB2API 上传增加可重试错误的 3 次重试，并从 K12 直建 payload 移除 `rate_multiplier`。
  - platforms/chatgpt/sub2api_upload.py: 单账号 SUB2API 上传识别 `plan_type=k12` 时允许无 `refresh_token`，并在直建 credentials 中携带 plan_type。
  - tests/test_k12_join.py: 增加 SUB2API 请求异常重试和 K12 payload 不限制 RT 的回归测试。
  - tests/test_sub2api_upload.py: 增加 K12 无 rt 上传与普通账号无 rt 拒绝的回归测试。
  - docs/k12-space-join.md: 补充 K12 SUB2API 上传重试策略、不设置 `rate_multiplier`、单账号 K12 无 rt 可上传的说明。
  - progress.md: 追加本轮进度、验证和回滚说明。
- 回滚点：撤销 k12_join.py 中 K12_SUB2API_UPLOAD_RETRIES、K12_SUB2API_UPLOAD_RETRY_DELAY_SECONDS、_is_retryable_sub2api_upload_error、upload_session_to_sub2api 重试循环和 `rate_multiplier` 移除；撤销 sub2api_upload.py 中 _account_plan_type、_is_k12_account、K12 无 rt 豁免与 plan_type 写入；撤销 tests/test_k12_join.py、tests/test_sub2api_upload.py 新增测试；撤销 docs/progress 本轮追加内容即可恢复旧行为。

## 2026-07-02 - Task: K12 exchange 失败继续尝试后续 workspace

### What was done
- 修复 K12 强入流程只使用第一个 join accepted workspace 的问题：现在会对配置列表里的所有 workspace 发 join 请求。
- exchange 校验失败时，不再直接结束 K12 流程；会继续尝试下一个 join accepted 的 workspace，直到某个 workspace exchange 校验通过。
- 只有所有 join accepted 的 workspace 都 exchange 校验失败，才停止 K12 上传。
- 增加回归测试覆盖第一个 workspace exchange 失败、第二个 workspace exchange 成功并继续上传的场景。

### Testing
- .venv\Scripts\python.exe -m pytest tests\test_k12_join.py -q -> 13 passed，1 个 StarletteDeprecationWarning。
- .venv\Scripts\python.exe -m py_compile platforms\chatgpt\k12_join.py platforms\chatgpt\protocol_mailbox.py -> 无输出，编译通过。
- git diff --check -- platforms\chatgpt\protocol_mailbox.py tests\test_k12_join.py docs\k12-space-join.md progress.md -> 无空白错误；仅提示 protocol_mailbox.py 的 LF/CRLF 工作区换行警告。

### Notes
- 修改文件清单
  - platforms/chatgpt/protocol_mailbox.py: K12 编排改为 join 所有 workspace，并按 join accepted 列表逐个 exchange，失败继续下一个。
  - tests/test_k12_join.py: 增加 K12 flow 在第一个 exchange 失败后继续第二个 workspace 的回归测试。
  - docs/k12-space-join.md: 更新 K12 join/exchange 顺序与全部失败才停止上传的说明。
  - progress.md: 追加本轮进度、验证和回滚说明。
- 回滚点：撤销 protocol_mailbox.py 中 _run_k12_flow 对 send_workspace_join_requests、accepted_workspaces 循环 exchange 的调整，恢复只处理单个 chosen workspace；撤销 tests/test_k12_join.py 新增测试；撤销 docs/progress 本轮追加内容即可恢复旧行为。

## 2026-07-02 - Task: K12 workspace 按 join + exchange 成对尝试

### What was done
- 修正上一版 K12 控制流：不再先批量 join 所有 workspace。
- K12 workspace 现在按配置顺序逐个执行 `join -> exchange`；同一个 workspace 的 join accepted 且 exchange 校验通过，才算成功加入。
- 当前 workspace 的 exchange 校验失败时，才进入下一个 workspace 重新执行 `join -> exchange`；任意一个 workspace 成功后立即停止，避免一个账号加入多个 workspace。
- 更新回归测试，确认第一组 exchange 失败后才 join 第二组，第二组成功后不会 join 第三组。

### Testing
- .venv\Scripts\python.exe -m pytest tests\test_k12_join.py -q -> 13 passed，1 个 StarletteDeprecationWarning。
- .venv\Scripts\python.exe -m py_compile platforms\chatgpt\k12_join.py platforms\chatgpt\protocol_mailbox.py -> 无输出，编译通过。
- git diff --check -- platforms\chatgpt\protocol_mailbox.py tests\test_k12_join.py docs\k12-space-join.md progress.md -> 无空白错误；仅提示 protocol_mailbox.py 的 LF/CRLF 工作区换行警告。

### Notes
- 修改文件清单
  - platforms/chatgpt/protocol_mailbox.py: K12 编排改为单个 workspace 内 join + exchange 成对执行，成功后立即停止。
  - tests/test_k12_join.py: 回归测试改为校验不会提前 join 后续 workspace，且第二个 workspace 成功后停止。
  - docs/k12-space-join.md: 更新 join/exchange 成功定义、失败继续策略和接入点说明。
  - progress.md: 追加本轮进度、验证和回滚说明。
- 回滚点：撤销 protocol_mailbox.py 中 workspace_list 循环内成对调用 send_workspace_join_requests 与 exchange_workspace_session 的调整；撤销 tests/test_k12_join.py 本轮测试断言调整；撤销 docs/progress 本轮追加内容即可恢复旧行为。

## 2026-07-02 - Task: 修正 K12 exchange 成功判定为 chatgpt_account_id

### What was done
- 追溯 14:33 成功日志确认：`ritarussell4987+r7ky6nqs@outlook.com` 成功上传时，exchange 返回的 `account.id=35a29304-...` 本来就不等于目标 workspace `631e1603-...`，旧流程仍判定成功并上传。
- 定位后续失败根因：`收紧 K12 强入成功判定` 一轮把 exchange 成功条件改成 `account.id == workspace_id`，导致后续同类成功响应被误判失败。
- 将 exchange 成功判定改为：响应必须包含 accessToken，并且能从响应 JSON 或 accessToken claims 中取得 `chatgpt_account_id`；不再比较 `account.id` 与 workspace ID。
- 更新 exchange 成功日志文案，改为“已获取 ChatGPT account 标识”，避免继续把 `account.id` 当 workspace ID。
- 更新回归测试，覆盖 `account.id` 与 workspace 不一致但存在 `chatgpt_account_id` 时通过，以及缺少 `chatgpt_account_id` 时拒绝。

### Testing
- .venv\Scripts\python.exe -m pytest tests\test_k12_join.py -q -> 14 passed，1 个 StarletteDeprecationWarning。
- .venv\Scripts\python.exe -m py_compile platforms\chatgpt\k12_join.py platforms\chatgpt\protocol_mailbox.py -> 无输出，编译通过。
- git diff --check -- platforms\chatgpt\k12_join.py tests\test_k12_join.py docs\k12-space-join.md progress.md -> 无输出，空白检查通过。

### Notes
- 修改文件清单
  - platforms/chatgpt/k12_join.py: exchange 校验改为提取 `chatgpt_account_id`，支持响应 JSON 和 accessToken claims 两种来源，移除 `account.id == workspace_id` 判定。
  - tests/test_k12_join.py: 调整 exchange 判定回归测试，新增缺少 `chatgpt_account_id` 拒绝场景。
  - docs/k12-space-join.md: 更新 K12 exchange 成功判定说明，不再要求 `account.id` 等于 workspace ID。
  - progress.md: 追加本轮追溯、验证和回滚说明。
- 回滚点：撤销 k12_join.py 中 _find_key_recursive、_session_chatgpt_account_id、validate_workspace_exchange_session 的本轮改动并恢复 `account.id == workspace_id` 校验；撤销 tests/test_k12_join.py、docs/k12-space-join.md、progress.md 本轮追加内容即可恢复旧行为。

## 2026-07-02 - Task: K12 Session 复制与 SUB2API 上传重试增强

### What was done
- 账号注册结果保存 K12 workspace session，并在账号列表 overview 中暴露 `k12_session` 与 `k12_workspace_id`，避免 K12 session 混入 legacy_extra。
- 账号列表更多菜单新增“复制K12 Session”，仅当账号存在 K12 session 时展示，复制内容为切换到 K12 workspace 后的 session JSON。
- SUB2API 通用请求层增加 TLS/connect、408/409/425/429、5xx 的可重试处理，手动上传路径覆盖登录、查分组、查代理、导入和直建。
- K12 专用上传重试次数从 3 次调整为 8 次，并避免与通用请求层形成双层叠加重试；通用 SUB2API 请求层默认也从 3 次调整为 8 次。
- 更新 K12 文档，说明“复制K12 Session”和 SUB2API 8 次重试边界。

### Testing
- .venv\Scripts\python.exe -m pytest tests\test_k12_join.py tests\test_sub2api_upload.py tests\test_api_accounts.py -q -> 50 passed，1 个 StarletteDeprecationWarning。
- .venv\Scripts\python.exe -m py_compile platforms\chatgpt\k12_join.py platforms\chatgpt\sub2api_upload.py platforms\chatgpt\plugin.py core\account_graph.py -> 无输出，编译通过。
- npm run build（frontend）-> tsc -b 与 vite build 通过；仅有 Vite chunk size warning。
- git diff --check -- platforms\chatgpt\k12_join.py platforms\chatgpt\sub2api_upload.py platforms\chatgpt\plugin.py core\account_graph.py frontend\src\pages\Accounts.tsx tests\test_api_accounts.py tests\test_sub2api_upload.py docs\k12-space-join.md progress.md -> 无空白错误；仅提示部分文件 LF/CRLF 工作区换行警告。

### Notes
- 修改文件清单
  - platforms/chatgpt/plugin.py: 注册结果 extra 持久化 `k12_session` 与 `k12_workspace_id`。
  - core/account_graph.py: 将 K12 session 归入 overview，避免进入 legacy_extra。
  - frontend/src/pages/Accounts.tsx: 增加 K12 session 读取和“复制K12 Session”菜单按钮。
  - platforms/chatgpt/sub2api_upload.py: 增加 SUB2API 请求层可重试处理，默认重试 8 次，并为 K12 外层重试提供禁用内层重试参数。
  - platforms/chatgpt/k12_join.py: K12 SUB2API 上传外层重试改为 8 次，K12 内部调用 SUB2API helper 时禁用内层重试以避免叠加。
  - tests/test_api_accounts.py: 增加 K12 session 在账号列表 overview 暴露的回归测试。
  - tests/test_sub2api_upload.py: 增加 SUB2API 请求异常重试与 400 业务错误不重试的回归测试。
  - docs/k12-space-join.md: 补充 K12 Session 复制和 SUB2API 8 次重试策略说明。
  - progress.md: 追加本轮进度、验证和回滚说明。
- 回滚点：撤销 plugin.py 中 `k12_session`/`k12_workspace_id` extra 写入；撤销 account_graph.py 的 K12 session overview 暴露；撤销 Accounts.tsx 的 K12 复制按钮与读取函数；撤销 sub2api_upload.py 的请求重试、helper retries 参数和默认 8 次配置；撤销 k12_join.py 的 8 次重试与 retries=0 调用；撤销 tests/docs/progress 本轮新增内容即可恢复旧行为。
