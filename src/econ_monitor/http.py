"""API アクセス用の共通 HTTP セッション。"""

from __future__ import annotations

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from econ_monitor import __version__

DEFAULT_TIMEOUT = 30
USER_AGENT = f"econ-monitor/{__version__} (+https://github.com/features/actions)"


def make_session(total_retries: int = 4, backoff: float = 2.0) -> requests.Session:
    """接続エラーや 429/5xx を指数バックオフで自動リトライするセッションを返す。

    世界銀行 API は接続が不安定なことがあるため、接続エラーもリトライ対象にする。
    """
    retry = Retry(
        total=total_retries,
        connect=total_retries,
        read=total_retries,
        status=total_retries,
        backoff_factor=backoff,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        raise_on_status=False,
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session = requests.Session()
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers["User-Agent"] = USER_AGENT
    return session
