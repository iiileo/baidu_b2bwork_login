class BrowserUnavailableError(Exception):
    """浏览器会话不可用：空白页、断连、建会话失败等，走任务级重试。"""


class LoginFinished(Exception):
    """登录流程已得出终态（成功/业务失败/取消），worker 应 finish session。"""

    def __init__(
        self,
        *,
        ok: bool = False,
        cookies: list | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        cancelled: bool = False,
    ) -> None:
        self.ok = ok
        self.cookies = cookies
        self.error_code = error_code
        self.error_message = error_message
        self.cancelled = cancelled
        super().__init__(error_message or ("ok" if ok else "failed"))
