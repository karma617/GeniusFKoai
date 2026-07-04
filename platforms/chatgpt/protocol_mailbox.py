"""ChatGPT 协议邮箱注册 worker。"""

from __future__ import annotations



from typing import Any, Callable



from platforms.chatgpt.register import RegistrationEngine, RegistrationResult





_OTP_FAILURE_MARKERS = (

    "开始 OAuth 流程失败",

    "signin/openai 失败",

    "cloudflare",

    "__cf_chl",

    "enable javascript and cookies",

    "invalid_state",

    "no longer valid",

    "提交注册表单失败",

    "获取验证码失败",

    "发送验证码失败",

    "验证码超时",

    "邮箱验证码",

    "email otp",

    "verification code",

)





def _result_text(result: Any, key: str) -> str:

    if isinstance(result, dict):

        return str(result.get(key, "") or "")

    return str(getattr(result, key, "") or "")





def _result_dict(result: Any) -> dict:

    return result if isinstance(result, dict) else {}





class _MailboxEmailService:

    def __init__(self, *, mailbox, mailbox_account, provider: str, log_fn: Callable[[str], None] | None = None):

        self.service_type = type("ST", (), {"value": provider})()

        self._mailbox = mailbox

        self._mailbox_account = mailbox_account

        self._acct = None

        self._before_ids = None

        self._log_fn = log_fn or print



    def _log(self, message: str) -> None:

        try:

            self._log_fn(message)

        except Exception:

            print(message)



    def create_email(self, config=None):

        self._acct = self._mailbox_account

        try:

            self._before_ids = self._mailbox.get_current_ids(self._mailbox_account)

        except Exception:

            self._before_ids = set()

        return {

            "email": self._mailbox_account.email,

            "service_id": getattr(self._mailbox_account, "account_id", ""),

            "token": getattr(self._mailbox_account, "account_id", ""),

        }



    def get_verification_code(self, email=None, email_id=None, timeout=120, pattern=None, otp_sent_at=None):

        import time as _time

        acct = self._acct or self._mailbox_account

        mailbox_type = type(self._mailbox).__name__



        # 如果知道 OTP 发送时间，先等邮件投递完成再开始轮询

        effective_timeout = timeout

        if otp_sent_at is not None:

            elapsed = _time.time() - otp_sent_at

            delivery_delay = 8

            if elapsed < delivery_delay:

                wait_remaining = delivery_delay - elapsed

                self._log(f"[Mailbox:{mailbox_type}] OTP 发送 {elapsed:.0f}s 前，等待 {wait_remaining:.0f}s 后开始轮询（让邮件到达）")

                _time.sleep(wait_remaining)

                effective_timeout = max(30, timeout - int(wait_remaining))



        before_count = len(self._before_ids) if self._before_ids else 0

        self._log(f"[Mailbox:{mailbox_type}] 开始等待验证码 email={acct.email} timeout={effective_timeout}s before_ids={before_count}")



        try:

            import inspect as _inspect

            wait_kwargs = {
                "keyword": "",
                "timeout": effective_timeout,
                "code_pattern": pattern,
                "before_ids": self._before_ids or None,
            }
            try:
                if "otp_sent_at" in _inspect.signature(self._mailbox.wait_for_code).parameters:
                    wait_kwargs["otp_sent_at"] = otp_sent_at
            except Exception:
                pass

            code = self._mailbox.wait_for_code(acct, **wait_kwargs)

            self._log(f"[Mailbox:{mailbox_type}] 轮询成功，获取到验证码: {code}")

            return code

        except TimeoutError:

            self._log(f"[Mailbox:{mailbox_type}] 轮询超时 ({effective_timeout}s)，未收到验证码")

            raise

    def refresh_before_ids(self) -> set:
        """刷新已见邮件集合，用于 OTP 会话失效后忽略旧验证码邮件。"""
        try:
            self._before_ids = set(self._mailbox.get_current_ids(self._mailbox_account) or set())
        except Exception:
            self._before_ids = set()
        return set(self._before_ids or set())

    def delete_current_email(self, *, reason: str = "") -> bool:
        """删除当前领取的邮箱；由注册器在邮箱被 OpenAI 判为不可用时调用。"""
        delete = getattr(self._mailbox, "delete_account", None)
        if not callable(delete):
            return False
        return bool(delete(self._mailbox_account, reason=reason))

    def mark_invalid_email(self, *, reason: str = "") -> list[str]:
        """给当前领取的邮箱打无效标签；用于验证码三轮未送达。"""
        marker = getattr(self._mailbox, "mark_invalid_email", None)
        if not callable(marker):
            return []
        return list(marker(self._mailbox_account, reason=reason) or [])

    def mark_parent_exhausted(self, reason: str = "") -> list[str]:
        """Force-mark the parent email as exhausted (alias quota reached)."""
        marker = getattr(self._mailbox, "mark_parent_exhausted", None)
        if not callable(marker):
            return []
        return list(marker(self._mailbox_account) or [])



    def update_status(self, success, error=None):

        return None



    @property

    def status(self):

        return None





class ChatGPTProtocolMailboxWorker:

    def __init__(

        self,

        *,

        mailbox,

        mailbox_account,

        provider: str,

        proxy_url: str | None = None,

        log_fn: Callable[[str], None] = print,
        skip_post_register_oauth: bool = False,
        k12_workspace_ids: str = "",
        remote_upload_enabled: bool = False,
    ):

        if not mailbox or not mailbox_account:

            raise ValueError("ChatGPT 注册流程依赖 mailbox provider，当前未获取到邮箱账号")

        self.mailbox = mailbox

        self.mailbox_account = mailbox_account

        self.proxy_url = proxy_url

        self.log_fn = log_fn
        self.skip_post_register_oauth = skip_post_register_oauth
        self.k12_workspace_ids = k12_workspace_ids
        self.remote_upload_enabled = remote_upload_enabled

        email_service = _MailboxEmailService(

            mailbox=mailbox,

            mailbox_account=mailbox_account,

            provider=provider,

            log_fn=log_fn,

        )

        self.engine = RegistrationEngine(

            email_service=email_service,

            proxy_url=proxy_url,

            callback_logger=log_fn,

        )
        self.engine.k12_join_enabled = self.skip_post_register_oauth
        self.engine.k12_workspace_ids = self.k12_workspace_ids



    def _log(self, message: str) -> None:

        try:

            self.log_fn(message)

        except Exception:

            pass



    def run(self, *, email: str, password: str):

        self.engine.email = email

        self.engine.password = password

        result = self.engine.run()

        if not result or not result.success:
            raise RuntimeError(result.error_message if result else "??????")

        if self.skip_post_register_oauth and result and result.success:
            self._run_k12_flow(result)

        return result

    def _run_k12_flow(self, result):
        """K12 强入空间流程：注册成功后跳过接码/支付，直接向 workspace 发加入申请并上传 session。"""
        try:
            from platforms.chatgpt.k12_join import (
                send_workspace_join_requests,
                exchange_workspace_session,
                ensure_chatgpt_session_cookie,
                parse_workspace_ids,
                save_session_to_local_upload_jsons,
                upload_session_to_sub2api,
            )

            metadata = getattr(result, "metadata", None) or {}
            registration_session = metadata.get("session") if isinstance(metadata.get("session"), dict) else {}
            access_token = (
                str(registration_session.get("accessToken") or registration_session.get("access_token") or "").strip()
                or result.access_token
                or ""
            )
            session_token = (
                str(registration_session.get("sessionToken") or registration_session.get("session_token") or "").strip()
                or str(getattr(result, "session_token", "") or "").strip()
            )
            cookies = ensure_chatgpt_session_cookie(metadata.get("cookies", "") or "", session_token)
            workspace_ids = self.k12_workspace_ids or ""
            proxy = self.proxy_url

            self._log("=" * 60)
            self._log("[K12] 开始强入 K12 空间流程...")
            if not registration_session.get("accessToken") and not registration_session.get("access_token"):
                self._log("[K12] 当前结果缺少 ChatGPT Web session accessToken，跳过 workspace join/exchange")
                return
            if "__Secure-next-auth.session-token=" not in cookies:
                self._log("[K12] 当前结果缺少 chatgpt.com NextAuth session cookie，跳过 workspace join/exchange")
                return

            workspace_list = parse_workspace_ids(workspace_ids)
            if not workspace_list:
                self._log("[K12] 未配置 workspace ID，跳过后续步骤")
                return

            successful_sessions = []
            for ws_id in workspace_list:
                # join、exchange、upload 必须绑定同一个 workspace；每个成功的 workspace 都要单独上传。
                join_results = send_workspace_join_requests(
                    access_token=access_token,
                    cookies=cookies,
                    workspace_ids=ws_id,
                    proxy=proxy,
                    log=self._log,
                )
                join_ok = any(isinstance(item, dict) and item.get("ok") for item in join_results)
                if not join_ok:
                    self._log(f"[K12] workspace {ws_id[:8]}... join 未成功，继续尝试下一个 workspace")
                    continue

                self._log(f"[K12] workspace {ws_id[:8]}... join 请求已接受，开始校验 exchange session")
                new_session = exchange_workspace_session(
                    cookies=cookies,
                    workspace_id=ws_id,
                    access_token=access_token,
                    proxy=proxy,
                    log=self._log,
                )
                if not new_session:
                    self._log(f"[K12] workspace {ws_id[:8]}... exchange 校验失败，继续尝试下一个 workspace")
                    continue

                self._log(f"[K12] workspace {ws_id[:8]}... 已确认切换到目标 K12 workspace，开始上传 session...")
                upload_ok = False
                upload_msg = ""
                local_paths = {}
                if self.remote_upload_enabled:
                    upload_ok, upload_msg = upload_session_to_sub2api(
                        new_session,
                        workspace_id=ws_id,
                        log=self._log,
                        proxy=proxy,
                    )

                    if upload_ok:
                        self._log(f"[K12] workspace {ws_id[:8]}... session 上传成功: {upload_msg}")
                    else:
                        self._log(f"[K12] workspace {ws_id[:8]}... session 上传失败: {upload_msg}")
                else:
                    cpa_path, sub2api_path = save_session_to_local_upload_jsons(new_session, workspace_id=ws_id)
                    local_paths = {"cpa_path": cpa_path, "sub2api_path": sub2api_path}
                    if cpa_path:
                        self._log(f"[K12] workspace {ws_id[:8]}... CPA JSON 已保存: {cpa_path}")
                    if sub2api_path:
                        self._log(f"[K12] workspace {ws_id[:8]}... SUB2API JSON 已保存: {sub2api_path}")

                successful_sessions.append({
                    "workspace_id": ws_id,
                    "session": new_session,
                    "upload_ok": upload_ok,
                    "upload_message": upload_msg,
                    **local_paths,
                })

            if not successful_sessions:
                self._log("[K12] 所有 workspace 均未完成 join + exchange 校验，停止 K12 上传")
                return

            last_success = successful_sessions[-1]
            chosen_ws = last_success["workspace_id"]
            new_session = last_success["session"]

            # 4. 更新 result 的 access_token 为最后一个成功 K12 workspace session 的 token，兼容旧调用方。
            k12_access_token = str(new_session.get("accessToken") or new_session.get("access_token") or "")
            if k12_access_token:
                result.access_token = k12_access_token

            # 保存 K12 信息到 metadata；旧字段保留最后一个成功 workspace，新增列表保存全部成功 workspace。
            metadata["k12_workspace_id"] = chosen_ws
            metadata["k12_session"] = new_session
            metadata["k12_workspace_sessions"] = successful_sessions
            metadata["k12_workspace_ids"] = [item["workspace_id"] for item in successful_sessions]
            result.metadata = metadata

            self._log(f"[K12] 强入 K12 空间流程完成，成功 {len(successful_sessions)}/{len(workspace_list)} 个 workspace")
            self._log("=" * 60)

        except Exception as e:
            self._log(f"[K12] 流程异常: {e}")
