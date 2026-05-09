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


def build_record_id(payload: dict[str, Any]) -> str:
    """生成稳定的 record_id。

    优先使用 post_id。拿不到 post_id 时，用 URL hash 兜底，保证同一链接
    重复采集时能覆盖同一个文件。
    """
    post_id = str((payload.get("post") or {}).get("post_id") or "").strip()
    if post_id:
        return f"xhs_note_{post_id}"
    source = payload.get("source") or {}
    seed = source.get("final_url") or source.get("input_url") or json.dumps(payload, ensure_ascii=False)
    digest = hashlib.sha256(str(seed).encode("utf-8")).hexdigest()[:8]
    return f"xhs_note_url_{digest}"


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


def update_index(payload: dict[str, Any], file_path: Path) -> None:
    """用本次采集结果更新 index.json。

    同一个 record_id 只保留最新一条记录，并把最新记录放在列表最前面，
    方便 bot 和用户优先看到最近采集的数据。
    """
    index = load_index()
    post = payload.get("post") or {}
    owner = payload.get("post_owner") or {}
    source = payload.get("source") or {}
    quality = payload.get("quality") or {}
    record = {
        "record_id": payload["record_id"],
        "post_id": post.get("post_id", ""),
        "title": post.get("title", ""),
        "owner_nickname": owner.get("nickname", ""),
        "file_path": str(file_path.relative_to(PROJECT_ROOT)),
        "input_url": source.get("input_url", ""),
        "final_url": source.get("final_url", ""),
        "fetched_at": source.get("fetched_at", ""),
        "comment_count_collected": quality.get("comment_count_collected", 0),
    }
    items = [item for item in index.get("items", []) if item.get("record_id") != record["record_id"]]
    items.insert(0, record)
    index["items"] = items
    save_index(index)


def save_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """保存标准 JSON，并同步维护索引文件。"""
    ensure_data_dirs()
    record_id = build_record_id(payload)
    payload["record_id"] = record_id
    file_path = RAW_DIR / f"{record_id}.json"
    file_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    update_index(payload, file_path)
    return {
        "record_id": record_id,
        "file_path": str(file_path),
        "index_path": str(INDEX_PATH),
        "payload": payload,
    }
