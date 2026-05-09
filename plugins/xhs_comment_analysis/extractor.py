"""从已经打开的小红书页面中提取帖子和评论数据。"""

from typing import Any


def extract_note_data(tab: Any, *, comment_limit: int = 600) -> dict[str, Any]:
    """从 DrissionPage tab 中提取原始帖子数据。

    当前只是骨架占位。下一步会在这里实现页面状态判断、
    帖子信息提取、评论滚动采集和去重。
    """
    return {
        "post": {},
        "post_owner": {},
        "comments": [],
        "comment_owners": [],
        "quality": {
            "status": "failed",
            "comment_count_collected": 0,
            "comment_limit": comment_limit,
            "comment_owner_count_collected": 0,
            "has_more_comments": None,
            "missing_fields": [],
            # 明确标记未实现，避免调用方误以为采集成功。
            "warnings": ["extract_note_data is not implemented yet"],
            "errors": [],
            "login_required": False,
            "verification_required": False,
            "page_state": "not_implemented",
            "elapsed_seconds": 0,
        },
    }
