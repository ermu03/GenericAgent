"""小红书数据获取的流程编排。"""

from datetime import datetime, timezone
import threading
import time
from typing import Any
import uuid

from .browser import close_browser, open_browser, save_page_screenshot
from .extractor import extract_note_data
from .login import BLOCKING_AUTH_STATUSES, ensure_logged_in
from .schema import build_standard_payload
from .storage import save_payload
from .url_utils import extract_xhs_url

_FETCH_LOCK = threading.Lock()


def fetch_xhs_note(
    text_or_url: str,
    *,
    headless: bool = True,
    comment_limit: int = 800,
    status_callback=None,
    intervention_timeout: int = 120,
) -> dict[str, Any]:
    """获取一个小红书帖子的标准 JSON，并保存到 data/xhs_data/raw/。"""
    input_url = extract_xhs_url(text_or_url)
    comment_limit = max(1, min(int(comment_limit), 800))
    started_at = datetime.now(timezone.utc)
    browser = None
    target_opened = False

    if _FETCH_LOCK.locked():
        _emit(status_callback, "queued", message="已有小红书采集任务正在运行，本次任务排队等待")

    with _FETCH_LOCK:
        # 浏览器启动、页面提取、标准化、保存分别放在不同模块；
        # fetch.py 只负责把这些步骤串起来，避免后续变成大杂烩。
        try:
            browser = open_browser(headless=headless)
            tab = browser.latest_tab

            auth_quality = ensure_logged_in(
                tab,
                status_callback=status_callback,
            )
            if auth_quality.get("auth_status") in BLOCKING_AUTH_STATUSES:
                raw_data = _build_auth_failure_raw_data(auth_quality)
            else:
                _emit(status_callback, "page_opening", message="正在打开小红书链接")
                tab.get(input_url)
                target_opened = True
                raw_data = _extract_with_intervention_wait(
                    tab,
                    comment_limit=comment_limit,
                    status_callback=status_callback,
                    intervention_timeout=intervention_timeout,
                )
                _emit_collection_finished(status_callback, raw_data)
                _merge_auth_quality(raw_data, auth_quality)

            final_url = (getattr(tab, "url", "") or input_url) if target_opened else input_url
            _attach_intervention_screenshot(tab, raw_data, status_callback=status_callback)

            payload = build_standard_payload(
                raw_data,
                input_url=input_url,
                final_url=final_url,
                fetched_at=started_at.isoformat(),
            )
            _emit(status_callback, "saving", message="正在保存小红书数据")
            result = save_payload(payload)
            _emit(
                status_callback,
                "saved",
                message="小红书数据已保存",
                file_path=result.get("file_path", ""),
                post_id=result.get("post_id", ""),
            )
            _emit(
                status_callback,
                "completed",
                message=_build_completion_message(result),
                file_path=result.get("file_path", ""),
                post_id=result.get("post_id", ""),
                quality=result.get("quality") or {},
            )
            return result
        finally:
            close_browser(browser)


def _build_auth_failure_raw_data(auth_quality: dict[str, Any]) -> dict[str, Any]:
    """登录/验证没有完成时，构造可保存的失败结果。"""
    quality = {
        "status": "failed",
        "page_state": auth_quality.get("page_state") or auth_quality.get("auth_status") or "login_required",
        "comment_count_collected": 0,
        "level1_comment_count_collected": 0,
        "level2_comment_count_collected": 0,
        "has_more_comments": None,
        "missing_fields": ["post.title", "post.content", "post_owner.nickname"],
        "warnings": auth_quality.get("warnings") or [],
        "errors": auth_quality.get("errors") or [],
        "login_success": bool(auth_quality.get("login_success")),
        "auth_status": auth_quality.get("auth_status", ""),
    }
    if auth_quality.get("screenshot_path"):
        quality["screenshot_path"] = auth_quality.get("screenshot_path")
    if auth_quality.get("missing_profile_session"):
        quality["missing_profile_session"] = True
    return {
        "post": {},
        "post_owner": {},
        "comments": [],
        "comment_owners": [],
        "quality": quality,
    }


def _merge_auth_quality(raw_data: dict[str, Any], auth_quality: dict[str, Any]) -> None:
    """把登录检测结果并入真实页面采集质量信息。"""
    quality = raw_data.get("quality") or {}
    merged = {**auth_quality, **quality}

    warnings = []
    for item in (auth_quality.get("warnings") or []) + (quality.get("warnings") or []):
        if item not in warnings:
            warnings.append(item)
    errors = []
    for item in (auth_quality.get("errors") or []) + (quality.get("errors") or []):
        if item not in errors:
            errors.append(item)

    merged["warnings"] = warnings
    merged["errors"] = errors
    merged["login_success"] = auth_quality.get("login_success", False)
    merged["auth_status"] = auth_quality.get("auth_status", "")
    if auth_quality.get("screenshot_path") and not merged.get("screenshot_path"):
        merged["screenshot_path"] = auth_quality.get("screenshot_path")
    raw_data["quality"] = merged


def _extract_with_intervention_wait(
    tab,
    *,
    comment_limit: int,
    status_callback,
    intervention_timeout: int,
) -> dict[str, Any]:
    """先采集一次；如遇登录/验证，截图提示用户并等待状态变化。"""
    raw_data = extract_note_data(tab, comment_limit=comment_limit, status_callback=status_callback)
    quality = raw_data.get("quality") or {}
    if not _needs_user_intervention(quality):
        return raw_data

    _attach_intervention_screenshot(tab, raw_data, status_callback=status_callback)
    page_state = quality.get("page_state", "unknown")
    collected_count = _collected_comment_count(quality)
    if collected_count > 0:
        quality.setdefault("warnings", []).append(
            f"采集中触发小红书验证或安全状态，已停止继续操作并保存已采集的 {collected_count} 条评论"
        )
        raw_data["quality"] = quality
        _emit(
            status_callback,
            "intervention_required",
            message=f"采集中触发小红书验证，已停止继续滚动并保存已采集的 {collected_count} 条评论",
            page_state=page_state,
            screenshot_path=quality.get("screenshot_path", ""),
            comment_count_collected=collected_count,
        )
        return raw_data

    _emit(
        status_callback,
        "intervention_required",
        message=f"页面需要用户处理: {page_state}，已发送截图，等待处理结果",
        page_state=page_state,
        screenshot_path=quality.get("screenshot_path", ""),
    )
    if page_state == "risk_control":
        quality.setdefault("warnings", []).append("页面触发安全限制，等待通常无法自动解除")
        raw_data["quality"] = quality
        return raw_data

    deadline = time.time() + max(intervention_timeout, 0)
    last_state = page_state
    while time.time() < deadline:
        time.sleep(5)
        current = extract_note_data(tab, comment_limit=comment_limit, status_callback=status_callback)
        current_quality = current.get("quality") or {}
        current_state = current_quality.get("page_state", "unknown")
        if current_state != last_state:
            last_state = current_state
            _emit(
                status_callback,
                "page_state_changed",
                message=f"页面状态变化: {current_state}",
                page_state=current_state,
            )
        if not _needs_user_intervention(current_quality):
            _emit(status_callback, "intervention_done", message="登录或验证状态已解除，继续采集")
            return current
        if current_quality.get("page_state") == "risk_control":
            _attach_intervention_screenshot(tab, current, status_callback=status_callback)
            current_quality.setdefault("warnings", []).append("页面触发安全限制，等待通常无法自动解除")
            current["quality"] = current_quality
            return current

    quality.setdefault("warnings", []).append(f"等待用户处理超时: {intervention_timeout} 秒")
    raw_data["quality"] = quality
    return raw_data


def _attach_intervention_screenshot(tab, raw_data: dict[str, Any], *, status_callback) -> None:
    """在登录或验证码状态下保存截图，并通知调用方。"""
    quality = raw_data.get("quality") or {}
    if quality.get("missing_profile_session"):
        return
    if not _needs_user_intervention(quality):
        return
    if quality.get("screenshot_path"):
        return
    try:
        screenshot_path = save_page_screenshot(tab, f"xhs_login_or_verify_{uuid.uuid4().hex[:8]}.png")
        quality.setdefault("warnings", []).append(f"页面截图已保存: {screenshot_path}")
        quality["screenshot_path"] = screenshot_path
        _emit(
            status_callback,
            "screenshot",
            message="已保存登录或验证截图",
            screenshot_path=screenshot_path,
            page_state=quality.get("page_state", ""),
        )
    except Exception as exc:
        quality.setdefault("warnings", []).append(f"页面截图保存失败: {exc}")
    raw_data["quality"] = quality


def _needs_user_intervention(quality: dict[str, Any]) -> bool:
    """判断当前页面是否需要登录或处理验证码。"""
    return bool(
        quality.get("page_state") in {"login_required", "verification_required", "login_or_verification", "risk_control"}
    )


def _collected_comment_count(quality: dict[str, Any]) -> int:
    """读取已采集评论数，异常值按 0 处理。"""
    return _quality_int(quality, "comment_count_collected")


def _quality_int(quality: dict[str, Any], key: str) -> int:
    """读取 quality 里的整数，异常值按 0 处理。"""
    try:
        return int(quality.get(key) or 0)
    except (TypeError, ValueError):
        return 0


def _emit_collection_finished(callback, raw_data: dict[str, Any]) -> None:
    """采集循环结束后先提示用户，避免保存和调试快照阶段显得无响应。"""
    quality = raw_data.get("quality") or {}
    comments = _collected_comment_count(quality)
    level1_count = _quality_int(quality, "level1_comment_count_collected")
    level2_count = _quality_int(quality, "level2_comment_count_collected")
    status = quality.get("status", "")
    page_state = quality.get("page_state", "")
    _emit(
        callback,
        "collection_finished",
        message=(
            "评论采集结束，正在整理和保存数据\n"
            f"评论数: {comments}，一级 {level1_count}，二级 {level2_count}\n"
            f"状态: {status} / {page_state}"
        ),
        comment_count_collected=comments,
        level1_comment_count_collected=level1_count,
        level2_comment_count_collected=level2_count,
        status=status,
        page_state=page_state,
    )


def _build_completion_message(result: dict[str, Any]) -> str:
    """构造可直接发给前端的最终完成消息。"""
    quality = result.get("quality") or {}
    post_id = result.get("post_id", "")
    file_path = result.get("file_path", "")
    comments = quality.get("comment_count_collected", 0)
    status = quality.get("status", "")
    page_state = quality.get("page_state", "")
    auth_status = quality.get("auth_status", "")
    warnings = quality.get("warnings") or []
    message = (
        "小红书数据采集完成\n\n"
        f"post_id: {post_id}\n"
        f"状态: {status} / {page_state}\n"
        f"登录: {auth_status}\n"
        f"评论数: {comments}\n"
        f"文件: [FILE:{file_path}]"
    )
    if warnings:
        message += "\n\n提示:\n" + "\n".join(f"- {item}" for item in warnings[:5])
    return message


def _emit(callback, event: str, **payload: Any) -> None:
    """向前端报告采集进度；回调异常不影响主流程。"""
    if not callback:
        return
    try:
        callback(event, payload)
    except Exception:
        pass
