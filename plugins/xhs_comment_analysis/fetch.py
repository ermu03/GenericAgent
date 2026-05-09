"""小红书数据获取的流程编排。"""

from datetime import datetime, timezone
import threading
import time
from typing import Any
import uuid

from .browser import close_browser, open_browser, save_page_screenshot
from .extractor import extract_note_data
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

    if _FETCH_LOCK.locked():
        _emit(status_callback, "queued", message="已有小红书采集任务正在运行，本次任务排队等待")

    with _FETCH_LOCK:
        # 浏览器启动、页面提取、标准化、保存分别放在不同模块；
        # fetch.py 只负责把这些步骤串起来，避免后续变成大杂烩。
        try:
            browser = open_browser(headless=headless)
            tab = browser.latest_tab
            _emit(status_callback, "page_opening", message="正在打开小红书链接")
            tab.get(input_url)
            raw_data = _extract_with_intervention_wait(
                tab,
                comment_limit=comment_limit,
                status_callback=status_callback,
                intervention_timeout=intervention_timeout,
            )
            final_url = getattr(tab, "url", "") or input_url
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
    """在登录、二维码或验证码状态下保存截图，并通知调用方。"""
    quality = raw_data.get("quality") or {}
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
    """判断当前页面是否需要用户扫码、登录或处理验证码。"""
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
