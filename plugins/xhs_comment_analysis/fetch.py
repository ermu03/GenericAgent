"""小红书数据获取的流程编排。"""

from datetime import datetime, timezone
from typing import Any

from .browser import open_browser
from .extractor import extract_note_data
from .schema import build_standard_payload
from .storage import save_payload
from .url_utils import extract_xhs_url


def fetch_xhs_note(text_or_url: str, *, headless: bool = True, comment_limit: int = 600) -> dict[str, Any]:
    """获取一个小红书帖子的标准 JSON，并保存到 data/xhs_data/raw/。"""
    input_url = extract_xhs_url(text_or_url)
    started_at = datetime.now(timezone.utc)

    # 浏览器启动、页面提取、标准化、保存分别放在不同模块；
    # fetch.py 只负责把这些步骤串起来，避免后续变成大杂烩。
    browser = open_browser(headless=headless)
    tab = browser.latest_tab
    tab.get(input_url)

    raw_data = extract_note_data(tab, comment_limit=comment_limit)
    final_url = getattr(tab, "url", "") or input_url
    payload = build_standard_payload(
        raw_data,
        input_url=input_url,
        final_url=final_url,
        fetched_at=started_at.isoformat(),
    )
    return save_payload(payload)
