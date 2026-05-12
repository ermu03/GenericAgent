"""小红书原始 JSON 和索引文件的保存逻辑。"""

import hashlib
import json
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
XHS_DATA_DIR = PROJECT_ROOT / "data" / "xhs_data"
RAW_DIR = XHS_DATA_DIR / "raw"
INDEX_PATH = XHS_DATA_DIR / "index.json"


def ensure_data_dirs() -> None:
    """确保 raw 数据目录存在。"""
    RAW_DIR.mkdir(parents=True, exist_ok=True)


def build_post_key(payload: dict[str, Any]) -> str:
    """生成稳定的文件主键。

    优先使用 post_id。拿不到 post_id 时，用 URL hash 兜底，保证失败结果
    或异常页面也能落盘。
    """
    post_id = str((payload.get("post") or {}).get("post_id") or "").strip()
    if post_id:
        return post_id
    source = payload.get("source") or {}
    seed = source.get("final_url") or source.get("input_url") or json.dumps(payload, ensure_ascii=False)
    digest = hashlib.sha256(str(seed).encode("utf-8")).hexdigest()[:8]
    return f"url_{digest}"


def load_index() -> dict[str, Any]:
    """读取 data/xhs_data/index.json。

    索引文件损坏时返回空索引，避免影响本次原始数据保存。
    """
    if not INDEX_PATH.exists():
        return {"items": []}
    try:
        data = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"items": []}
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        return {"items": []}
    return data


def save_index(index: dict[str, Any]) -> None:
    """保存索引文件。"""
    XHS_DATA_DIR.mkdir(parents=True, exist_ok=True)
    INDEX_PATH.write_text(
        json.dumps(index, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _build_raw_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """构造写入 raw JSON 的数据本体，不包含采集管理信息。"""
    return {
        "post": payload.get("post") or {},
        "post_owner": payload.get("post_owner") or {},
        "comments": payload.get("comments") or [],
        "comment_owners": payload.get("comment_owners") or [],
    }


def update_index(payload: dict[str, Any], file_path: Path) -> dict[str, Any]:
    """用本次采集结果更新 index.json。

    同一个 post_id 只保留最新一条记录，并把最新记录放在列表最前面，
    方便 bot 和用户优先看到最近采集的数据。
    """
    index = load_index()
    post = payload.get("post") or {}
    owner = payload.get("post_owner") or {}
    source = payload.get("source") or {}
    quality = payload.get("quality") or {}
    record = {
        "post_id": post.get("post_id", ""),
        "title": post.get("title", ""),
        "owner_nickname": owner.get("nickname", ""),
        "file_path": str(file_path.relative_to(PROJECT_ROOT)),
        "input_url": source.get("input_url", ""),
        "final_url": source.get("final_url", ""),
        "fetched_at": source.get("fetched_at", ""),
        "fetch_method": source.get("fetch_method", ""),
        "status": quality.get("status", ""),
        "page_state": quality.get("page_state", ""),
        "comment_count_collected": quality.get("comment_count_collected", 0),
        "level1_comment_count_collected": quality.get("level1_comment_count_collected", 0),
        "level2_comment_count_collected": quality.get("level2_comment_count_collected", 0),
        "has_more_comments": quality.get("has_more_comments"),
        "missing_fields": quality.get("missing_fields", []),
        "warnings": quality.get("warnings", []),
        "errors": quality.get("errors", []),
        "login_success": quality.get("login_success", False),
        "auth_status": quality.get("auth_status", ""),
        "updated_at": source.get("fetched_at", ""),
    }
    if quality.get("screenshot_path"):
        record["screenshot_path"] = quality.get("screenshot_path")
    if quality.get("missing_profile_session"):
        record["missing_profile_session"] = True

    index_key = record["post_id"] or record["file_path"]
    items = [
        item
        for item in index.get("items", [])
        if (item.get("post_id") or item.get("file_path")) != index_key
    ]
    items.insert(0, record)
    index["items"] = items
    save_index(index)
    return record


def save_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """保存标准 JSON，并同步维护索引文件。"""
    ensure_data_dirs()
    post_key = build_post_key(payload)
    file_path = RAW_DIR / f"{post_key}.json"
    raw_payload = _build_raw_payload(payload)
    file_path.write_text(
        json.dumps(raw_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    index_record = update_index(payload, file_path)
    return {
        "post_id": (payload.get("post") or {}).get("post_id", ""),
        "post_key": post_key,
        "file_path": str(file_path),
        "index_path": str(INDEX_PATH),
        "payload": raw_payload,
        "quality": payload.get("quality") or {},
        "source": payload.get("source") or {},
        "index_record": index_record,
    }
