"""小红书帖子标准 JSON 结构的构造工具。"""

import re
from typing import Any

from .url_utils import extract_note_id_from_url


def empty_post() -> dict[str, Any]:
    """返回帖子字段的默认结构。"""
    return {
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
        "page_state": "normal",
        "comment_count_collected": 0,
        "level1_comment_count_collected": 0,
        "level2_comment_count_collected": 0,
        "has_more_comments": None,
        "missing_fields": [],
        "warnings": [],
        "errors": [],
        "login_success": False,
        "auth_status": "",
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
    post = _clean_post({**empty_post(), **(raw_data.get("post") or {})})
    post_owner = _clean_owner({**empty_owner(), **(raw_data.get("post_owner") or {})})
    comments = _clean_comments(raw_data.get("comments") or [])
    comment_owners = [_clean_owner({**empty_owner(), **owner}) for owner in (raw_data.get("comment_owners") or [])]
    quality = _clean_quality(raw_data.get("quality") or {})
    # 如果采集阶段没有显式写 post.url，就使用浏览器最终 URL 兜底。
    post["url"] = post.get("url") or final_url
    if not post.get("post_id"):
        post["post_id"] = extract_note_id_from_url(final_url) or extract_note_id_from_url(input_url)
    return {
        "post": post,
        "post_owner": post_owner,
        "comments": comments,
        "comment_owners": comment_owners,
        "quality": quality,
        "source": {
            "input_url": input_url,
            "final_url": final_url,
            "fetched_at": fetched_at,
            "fetch_method": "drissionpage",
        },
    }


def _clean_post(post: dict[str, Any]) -> dict[str, Any]:
    """清理帖子字段，去掉内部调试信息并把正文中的话题标签拆出去。"""
    tags = _clean_tags(post.get("tags") or [])
    post["tags"] = tags
    post["content"] = _clean_post_content(post.get("content", ""), tags)
    post["metadata"] = _clean_metadata(post.get("metadata") or {})
    post.pop("platform", None)
    return post


def _clean_owner(owner: dict[str, Any]) -> dict[str, Any]:
    """清理用户字段，避免把头像等暂不使用的扩展字段写入正式 JSON。"""
    metadata = _clean_metadata(owner.get("metadata") or {})
    metadata.pop("avatar", None)
    owner["metadata"] = metadata
    return owner


def _clean_comments(comments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """清理评论字段，去掉采集内部使用的去重 key。"""
    cleaned = []
    for comment in comments:
        item = dict(comment)
        metadata = _clean_metadata(item.get("metadata") or {})
        metadata.pop("key", None)
        metadata.pop("dedupe_key", None)
        if not metadata.get("reply_to_user_id"):
            metadata.pop("reply_to_user_id", None)
        if not metadata.get("reply_to_nickname"):
            metadata.pop("reply_to_nickname", None)
        item.pop("parent_comment_id", None)
        item.pop("owner_profile_url", None)
        item["metadata"] = metadata
        cleaned.append(item)
    return cleaned


def _clean_quality(raw_quality: dict[str, Any]) -> dict[str, Any]:
    """把内部诊断信息压缩成用户需要看的采集质量摘要。"""
    quality = {**default_quality()}
    for key in quality:
        if key in raw_quality:
            quality[key] = raw_quality.get(key)

    # 失败时截图路径有排查价值；成功时不写空字段。
    screenshot_path = raw_quality.get("screenshot_path")
    if screenshot_path:
        quality["screenshot_path"] = screenshot_path
    return quality


def _clean_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    """删除开发阶段观察页面结构用的字段。"""
    metadata = dict(metadata)
    metadata.pop("raw_keys", None)
    return metadata


def _clean_tags(tags: list[Any]) -> list[str]:
    """去重并清理标签文本。"""
    seen = set()
    cleaned = []
    for tag in tags:
        text = _clean_text(str(tag).replace("[话题]", "").strip(" #"))
        if text and text not in seen:
            seen.add(text)
            cleaned.append(text)
    return cleaned


def _clean_post_content(content: Any, tags: list[str]) -> str:
    """正文只保留文本内容，话题标签统一放入 tags 字段。"""
    text = _clean_text(content)
    for tag in sorted(tags, key=len, reverse=True):
        escaped = re.escape(tag)
        text = re.sub(rf"#\s*{escaped}\s*(?:\[话题\])?\s*#", "", text)
    text = re.sub(r"#[^#\s]+?\[话题\]#", "", text)
    return _clean_text(text)


def _clean_text(value: Any) -> str:
    """压缩多余空白，保持 JSON 文本稳定。"""
    return re.sub(r"\s+", " ", str(value or "")).strip()
