"""小红书帖子标准 JSON 结构的构造工具。"""

from typing import Any

from .url_utils import extract_note_id_from_url


def empty_post() -> dict[str, Any]:
    """返回帖子字段的默认结构。"""
    return {
        "platform": "xiaohongshu",
        "post_id": "",
        "url": "",
        "title": "",
        "content": "",
        "tags": [],
        "created_at": "",
        "liked_count": 0,
        "collected_count": 0,
        "comment_count": 0,
        "shared_count": 0,
        "metadata": {},
    }


def empty_owner() -> dict[str, Any]:
    """返回用户字段的默认结构。"""
    return {
        "user_id": "",
        "nickname": "",
        "profile_url": "",
        "bio": "",
        "location": "",
        "followers_count": None,
        "following_count": None,
        "liked_count": None,
        "verified": None,
        "profile_tags": [],
        "metadata": {},
    }


def default_quality() -> dict[str, Any]:
    """返回采集质量字段的默认结构。"""
    return {
        "status": "success",
        "comment_count_collected": 0,
        "level1_comment_count_collected": 0,
        "level2_comment_count_collected": 0,
        "comment_limit": 600,
        "comment_owner_count_collected": 0,
        "has_more_comments": None,
        "missing_fields": [],
        "warnings": [],
        "errors": [],
        "login_required": False,
        "verification_required": False,
        "page_state": "normal",
        "elapsed_seconds": 0,
        "screenshot_path": "",
    }


def build_standard_payload(
    raw_data: dict[str, Any],
    *,
    input_url: str,
    final_url: str,
    fetched_at: str,
) -> dict[str, Any]:
    """把页面采集结果合并进标准 JSON 结构。

    页面上取不到的字段保留默认值，缺失情况由 `quality.missing_fields`
    记录，避免 schema 因部分字段缺失而变化。
    """
    post = {**empty_post(), **(raw_data.get("post") or {})}
    post_owner = {**empty_owner(), **(raw_data.get("post_owner") or {})}
    quality = {**default_quality(), **(raw_data.get("quality") or {})}
    # 如果采集阶段没有显式写 post.url，就使用浏览器最终 URL 兜底。
    post["url"] = post.get("url") or final_url
    if not post.get("post_id"):
        post["post_id"] = extract_note_id_from_url(final_url) or extract_note_id_from_url(input_url)
    return {
        "record_id": "",
        "post": post,
        "post_owner": post_owner,
        "comments": raw_data.get("comments") or [],
        "comment_owners": raw_data.get("comment_owners") or [],
        "quality": quality,
        "source": {
            "input_url": input_url,
            "final_url": final_url,
            "fetched_at": fetched_at,
            "fetch_method": "drissionpage",
        },
    }
