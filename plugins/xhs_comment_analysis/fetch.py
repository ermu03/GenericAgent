"""小红书数据获取的流程编排。"""

from datetime import datetime, timezone
import threading
import time
from typing import Any
import uuid

from .browser import close_browser, open_browser, save_page_screenshot
from .debug_snapshot import save_debug_snapshot
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
    comment_limit: int = 600,
    status_callback=None,
    intervention_timeout: int = 120,
) -> dict[str, Any]:
    """获取一个小红书帖子的标准 JSON，并保存到 data/xhs_data/raw/。"""
    input_url = extract_xhs_url(text_or_url)
    comment_limit = max(1, min(int(comment_limit), 600))
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
                raw_data = _build_auth_failure_raw_data(auth_quality, comment_limit)
                if auth_quality.get("auth_page_state") != "missing_profile_session":
                    _append_debug_snapshot_path(
                        raw_data,
                        _save_debug_snapshot_safely(
                            tab,
                            input_url=input_url,
                            phase="auth_blocked",
                            status_callback=status_callback,
                        ),
                    )
            else:
                _emit(status_callback, "page_opening", message="正在打开小红书链接")
                tab.get(input_url)
                target_opened = True
                debug_paths = [
                    _save_debug_snapshot_safely(
                        tab,
                        input_url=input_url,
                        phase="after_open",
                        status_callback=status_callback,
                    )
                ]
                raw_data = _extract_with_intervention_wait(
                    tab,
                    comment_limit=comment_limit,
                    status_callback=status_callback,
                    intervention_timeout=intervention_timeout,
                )
                debug_paths.append(
                    _save_debug_snapshot_safely(
                        tab,
                        input_url=input_url,
                        phase="after_extract",
                        status_callback=status_callback,
                    )
                )
                for debug_path in debug_paths:
                    _append_debug_snapshot_path(raw_data, debug_path)
                _merge_auth_quality(raw_data, auth_quality)

            final_url = (getattr(tab, "url", "") or input_url) if target_opened else input_url
            _attach_intervention_screenshot(tab, raw_data, status_callback=status_callback)

            payload = build_standard_payload(
                raw_data,
                input_url=input_url,
                final_url=final_url,
                fetched_at=started_at.isoformat(),
            )
            result = save_payload(payload)
            _emit(
                status_callback,
                "saved",
                message="小红书数据已保存",
                file_path=result.get("file_path", ""),
                record_id=result.get("record_id", ""),
            )
            return result
        finally:
            close_browser(browser)


def _save_debug_snapshot_safely(tab, *, input_url: str, phase: str, status_callback) -> str:
    """保存调试快照；失败只返回空路径，不阻断真实采集。"""
    try:
        return save_debug_snapshot(
            tab,
            input_url=input_url,
            phase=phase,
            status_callback=status_callback,
        )
    except Exception as exc:
        _emit(status_callback, "debug_snapshot_failed", message=f"保存小红书页面调试快照失败: {exc}")
        return ""


def _append_debug_snapshot_path(raw_data: dict[str, Any], path: str) -> None:
    """把调试快照路径写入 quality，便于后续根据真实页面结构修正提取逻辑。"""
    if not path:
        return
    quality = raw_data.setdefault("quality", {})
    paths = quality.setdefault("debug_snapshot_paths", [])
    if path not in paths:
        paths.append(path)


def _build_auth_failure_raw_data(auth_quality: dict[str, Any], comment_limit: int) -> dict[str, Any]:
    """登录/验证没有完成时，构造可保存的失败结果。"""
    quality = {
        **auth_quality,
        "status": "failed",
        "comment_count_collected": 0,
        "level1_comment_count_collected": 0,
        "level2_comment_count_collected": 0,
        "comment_limit": comment_limit,
        "comment_owner_count_collected": 0,
        "has_more_comments": None,
        "missing_fields": ["post.title", "post.content", "post_owner.nickname"],
        "page_state": auth_quality.get("auth_page_state") or auth_quality.get("auth_status") or "login_required",
    }
    if quality.get("auth_status") in {"verification_required", "risk_control"}:
        quality["verification_required"] = True
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
    merged["login_checked"] = auth_quality.get("login_checked", False)
    merged["login_success"] = auth_quality.get("login_success", False)
    merged["auth_status"] = auth_quality.get("auth_status", "")
    merged["auth_page_state"] = auth_quality.get("auth_page_state", "")
    merged["auth_detection_method"] = auth_quality.get("auth_detection_method", "")
    merged["auth_screenshot_path"] = auth_quality.get("auth_screenshot_path", "")
    merged["auth_elapsed_seconds"] = auth_quality.get("auth_elapsed_seconds", 0)
    merged["xhs_cookie_summary"] = auth_quality.get("xhs_cookie_summary") or {}
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
    raw_data = extract_note_data(tab, comment_limit=comment_limit)
    quality = raw_data.get("quality") or {}
    if not _needs_user_intervention(quality):
        return raw_data

    _attach_intervention_screenshot(tab, raw_data, status_callback=status_callback)
    page_state = quality.get("page_state", "unknown")
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
        current = extract_note_data(tab, comment_limit=comment_limit)
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
    if quality.get("auth_page_state") in {"missing_profile_session"}:
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
        quality.get("login_required")
        or quality.get("verification_required")
        or quality.get("page_state") in {"login_or_verification", "risk_control"}
    )


def _emit(callback, event: str, **payload: Any) -> None:
    """向前端报告采集进度；回调异常不影响主流程。"""
    if not callback:
        return
    try:
        callback(event, payload)
    except Exception:
        pass
