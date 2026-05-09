"""小红书链接提取和校验工具。"""

import re
from urllib.parse import parse_qs, unquote, urlparse

SUPPORTED_HOSTS = ("xiaohongshu.com", "xhslink.com")
XHS_URL_RE = re.compile(
    r"https?://[^\s<>\"']*(?:xiaohongshu\.com|xhslink\.com)/[^\s<>\"']+"
)
NOTE_PATH_NAMES = {"explore", "search_result", "discovery", "item"}


class XhsUrlError(ValueError):
    """没有找到可支持的小红书链接时抛出。"""


def extract_xhs_url(text: str) -> str:
    """从用户文本中提取第一个小红书链接或小红书短链。

    需要适配两种常见输入：
    - 电脑网页版复制的 `xiaohongshu.com/...` 链接。
    - 手机 App 分享文案里的 `xhslink.com/...` 短链。

    微信分享内容可能包含额外文案，所以这里不要求整条消息都是 URL。
    """
    text = (text or "").strip()
    match = XHS_URL_RE.search(text)
    if not match:
        raise XhsUrlError("未找到小红书链接")
    # 去掉中文聊天里常见的句末标点，避免把标点误当成 URL 的一部分。
    url = match.group(0).rstrip("。），,)\n\r\t")
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if not parsed.scheme or not any(host == h or host.endswith(f".{h}") for h in SUPPORTED_HOSTS):
        raise XhsUrlError("不是有效的小红书链接")
    return url


def extract_note_id_from_url(url: str) -> str:
    """从小红书 URL 中提取可能的帖子 ID。

    只从明确的帖子路径中提取，避免把 `/website-login/error` 里的
    `error` 错当成帖子 ID。安全验证页里常带 `redirectPath`，也会尝试
    从这个参数还原原始帖子 ID。
    """
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    redirect_path = (query.get("redirectPath") or [""])[0]
    if redirect_path:
        note_id = extract_note_id_from_url(unquote(redirect_path))
        if note_id:
            return note_id

    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) < 2:
        return ""
    if parts[-2] not in NOTE_PATH_NAMES:
        return ""
    value = parts[-1]
    return value if re.fullmatch(r"[0-9A-Za-z_-]+", value) else ""
