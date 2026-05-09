"""小红书登录态检测和阻断状态处理。"""

import time
import uuid
from typing import Any

from .browser import BROWSER_PROFILE_DIR, save_page_screenshot

LOGIN_CHECK_URL = "https://www.xiaohongshu.com/explore"

REQUIRED_COOKIE_NAMES = ("a1", "webId", "web_session")
RECOMMENDED_COOKIE_NAMES = (
    "web_session_sec",
    "gid",
    "websectiga",
    "sec_poison_id",
    "xsecappid",
    "id_token",
)

BLOCKING_AUTH_STATUSES = {"login_required", "verification_required", "risk_control"}


CHECK_LOGIN_STATUS_JS = r"""
function safeText(el) {
  return (el && el.innerText ? el.innerText : '').replace(/\s+/g, ' ').trim();
}

function visible(el) {
  if (!el) return false;
  const style = window.getComputedStyle(el);
  if (style.display === 'none' || style.visibility === 'hidden' || Number(style.opacity) === 0) return false;
  const rect = el.getBoundingClientRect();
  return rect.width > 0 && rect.height > 0;
}

function visibleMatches(selectors) {
  const out = [];
  for (const selector of selectors) {
    try {
      const nodes = Array.from(document.querySelectorAll(selector));
      if (nodes.some(visible)) out.push(selector);
    } catch (_) {}
  }
  return out;
}

const text = document.body ? document.body.innerText : '';
const title = document.title || '';
const url = location.href;
const combined = `${text}\n${title}\n${url}`;

const loggedSelectors = visibleMatches([
  '.main-container .user .link-wrapper .channel',
  '.main-container .user a.link-wrapper[href*="/user/profile"]',
  '.main-container [class*="user"] a[href*="/user/profile"][class*="link"]'
]);

const loginSelectors = visibleMatches([
  '.login-container',
  '[class*="login-container"]',
  '[class*="LoginContainer"]',
  '[class*="login-modal"]',
  '[class*="login-wrapper"]'
]);

const riskControl = /ip at risk|error_code=300012|安全限制|访问太频繁|风险|异常访问/i.test(combined);
const verificationRequired = /安全验证|拖动滑块|人机验证|请完成验证|verify|captcha|captcha/i.test(combined);
const loginRequired = /登录后|请登录|扫码登录|登录小红书|手机号登录|短信登录/.test(text + title) && loginSelectors.length > 0;

let pageState = 'normal';
if (/404|not.?found/i.test(title + text)) pageState = 'not_found';
else if (riskControl) pageState = 'risk_control';
else if (verificationRequired) pageState = 'verification_required';
else if (loginRequired) pageState = 'login_required';

return {
  url,
  title,
  page_state: pageState,
  logged_in_by_dom: loggedSelectors.length > 0,
  logged_selectors: loggedSelectors,
  login_required: loginRequired,
  login_selectors: loginSelectors,
  verification_required: verificationRequired,
  risk_control: riskControl,
  text_preview: text.slice(0, 300)
};
"""


def ensure_logged_in(
    tab,
    *,
    status_callback=None,
) -> dict[str, Any]:
    """检查当前浏览器 profile 是否已有可用登录态。

    登录状态由 Chromium profile 自己管理，采集流程只复用 profile。
    """
    started_at = time.time()
    quality: dict[str, Any] = {
        "login_checked": True,
        "login_success": False,
        "auth_status": "checking",
        "auth_page_state": "unknown",
        "auth_detection_method": "",
        "auth_screenshot_path": "",
        "xhs_cookie_summary": {},
        "auth_attempts": [],
        "warnings": [],
        "errors": [],
    }

    _emit(status_callback, "login_checking", message="正在检查小红书登录态")
    initial_cookie_summary = read_xhs_cookie_summary(tab)
    quality["xhs_cookie_summary"] = initial_cookie_summary

    if not _has_session_cookie(initial_cookie_summary):
        quality["auth_status"] = "login_required"
        quality["auth_page_state"] = "missing_profile_session"
        quality["login_required"] = True
        quality.setdefault("warnings", []).append(f"当前浏览器 profile 未登录小红书: {BROWSER_PROFILE_DIR}")
        _emit(status_callback, "login_required", message="当前小红书浏览器 profile 未登录，请先在有界面浏览器中登录")
        return _finish_quality(quality, started_at)

    status = _open_and_check(tab, LOGIN_CHECK_URL, quality)
    _apply_status_to_quality(quality, status)
    _append_auth_attempt(quality, LOGIN_CHECK_URL, status)

    if _is_logged_in_status(status):
        _mark_logged_in(quality, status)
        _emit(status_callback, "login_success", message="小红书登录态有效")
        return _finish_quality(quality, started_at)

    if _is_blocked_status(status):
        _mark_blocked(quality, status, tab, status_callback)
        return _finish_quality(quality, started_at)

    quality["auth_status"] = "login_required"
    quality["auth_page_state"] = status.get("page_state") or "login_required"
    quality["login_required"] = True
    quality.setdefault("warnings", []).append(f"当前浏览器 profile 登录态无效或已过期: {BROWSER_PROFILE_DIR}")
    _emit(status_callback, "login_required", message="当前小红书浏览器 profile 登录态无效，请重新在有界面浏览器中登录")
    return _finish_quality(quality, started_at)


def check_login_status(tab) -> dict[str, Any]:
    """读取当前页面 DOM 和 cookie，判断登录/验证码/风控状态。"""
    errors: list[str] = []
    try:
        result = tab.run_js(CHECK_LOGIN_STATUS_JS, timeout=8)
    except Exception as exc:
        result = {}
        errors.append(f"登录状态脚本执行失败: {exc}")

    if not isinstance(result, dict):
        result = {}
        errors.append("登录状态脚本没有返回字典结果")

    cookie_summary = read_xhs_cookie_summary(tab)
    if cookie_summary.get("errors"):
        errors.extend(cookie_summary.get("errors") or [])

    cookie_session_present = "web_session" in set(cookie_summary.get("required_present") or [])
    logged_in_by_dom = bool(result.get("logged_in_by_dom"))
    login_required = bool(result.get("login_required"))
    verification_required = bool(result.get("verification_required"))
    risk_control = bool(result.get("risk_control"))

    # DOM 标记优先；cookie 只在页面没有登录弹窗/验证提示时作为兜底。
    logged_in_by_cookie = bool(cookie_session_present and not login_required and not verification_required and not risk_control)
    logged_in = logged_in_by_dom or logged_in_by_cookie

    auth_status = "unknown"
    if risk_control:
        auth_status = "risk_control"
    elif verification_required:
        auth_status = "verification_required"
    elif logged_in:
        auth_status = "logged_in"
    elif login_required:
        auth_status = "login_required"

    detection_method = ""
    if logged_in_by_dom:
        detection_method = "dom"
    elif logged_in_by_cookie:
        detection_method = "cookie"

    return {
        "auth_status": auth_status,
        "page_state": result.get("page_state") or auth_status,
        "url": result.get("url") or getattr(tab, "url", ""),
        "title": result.get("title") or "",
        "logged_in": logged_in,
        "logged_in_by_dom": logged_in_by_dom,
        "logged_in_by_cookie": logged_in_by_cookie,
        "auth_detection_method": detection_method,
        "login_required": login_required,
        "verification_required": verification_required,
        "risk_control": risk_control,
        "logged_selectors": result.get("logged_selectors") or [],
        "login_selectors": result.get("login_selectors") or [],
        "text_preview": result.get("text_preview") or "",
        "xhs_cookie_summary": cookie_summary,
        "errors": errors,
    }


def read_xhs_cookie_summary(tab) -> dict[str, Any]:
    """读取小红书相关 cookie 的摘要，不保存敏感 cookie 值。"""
    errors: list[str] = []
    try:
        cookies = tab.cookies(all_domains=True, all_info=False)
    except TypeError:
        try:
            cookies = tab.cookies(all_info=False)
        except Exception as exc:
            cookies = []
            errors.append(f"读取小红书 cookie 失败: {exc}")
    except Exception as exc:
        cookies = []
        errors.append(f"读取小红书 cookie 失败: {exc}")

    names = set()
    domains = set()
    for cookie in list(cookies or []):
        name = _cookie_attr(cookie, "name")
        domain = _cookie_attr(cookie, "domain")
        if domain and "xiaohongshu" not in domain and "xhslink" not in domain:
            continue
        if name:
            names.add(name)
        if domain:
            domains.add(domain)

    required_present = [name for name in REQUIRED_COOKIE_NAMES if name in names]
    required_missing = [name for name in REQUIRED_COOKIE_NAMES if name not in names]
    recommended_present = [name for name in RECOMMENDED_COOKIE_NAMES if name in names]
    recommended_missing = [name for name in RECOMMENDED_COOKIE_NAMES if name not in names]
    return {
        "cookie_count": len(names),
        "required_present": required_present,
        "required_missing": required_missing,
        "recommended_present": recommended_present,
        "recommended_missing": recommended_missing,
        "domains": sorted(domains),
        "errors": errors,
    }


def _append_auth_attempt(quality: dict[str, Any], requested_url: str, status: dict[str, Any]) -> None:
    """记录登录态验证页面的结果，便于定位风控或会话过期。"""
    attempts = quality.setdefault("auth_attempts", [])
    attempts.append(
        {
            "requested_url": requested_url,
            "final_url": status.get("url", ""),
            "auth_status": status.get("auth_status", ""),
            "page_state": status.get("page_state", ""),
            "title": status.get("title", ""),
            "login_required": bool(status.get("login_required")),
            "verification_required": bool(status.get("verification_required")),
            "risk_control": bool(status.get("risk_control")),
            "text_preview": status.get("text_preview", ""),
        }
    )


def _open_and_check(tab, url: str, quality: dict[str, Any]) -> dict[str, Any]:
    """打开指定页面后读取登录状态。"""
    try:
        tab.get(url)
        try:
            tab.wait.doc_loaded(timeout=20)
        except Exception as exc:
            quality.setdefault("warnings", []).append(f"登录检查页面加载等待超时: {exc}")
        time.sleep(1.5)
    except Exception as exc:
        quality.setdefault("errors", []).append(f"打开登录检查页面失败: {exc}")
    return check_login_status(tab)


def _apply_status_to_quality(quality: dict[str, Any], status: dict[str, Any]) -> None:
    """把本轮登录检测结果写入 quality。"""
    quality["auth_status"] = status.get("auth_status") or "unknown"
    quality["auth_page_state"] = status.get("page_state") or "unknown"
    quality["auth_detection_method"] = status.get("auth_detection_method") or quality.get("auth_detection_method", "")
    quality["xhs_cookie_summary"] = status.get("xhs_cookie_summary") or {}
    if status.get("login_required"):
        quality["login_required"] = True
    if status.get("verification_required"):
        quality["verification_required"] = True
    for error in status.get("errors") or []:
        if error not in quality.setdefault("errors", []):
            quality["errors"].append(error)


def _is_logged_in_status(status: dict[str, Any]) -> bool:
    return bool(status.get("auth_status") == "logged_in" or status.get("logged_in"))


def _is_blocked_status(status: dict[str, Any]) -> bool:
    return bool(status.get("auth_status") in {"verification_required", "risk_control"})


def _has_session_cookie(cookie_summary: dict[str, Any]) -> bool:
    """判断本地 profile 是否已经有小红书登录会话 cookie。"""
    return "web_session" in set(cookie_summary.get("required_present") or [])


def _mark_logged_in(quality: dict[str, Any], status: dict[str, Any]) -> None:
    quality["auth_status"] = "logged_in"
    quality["login_success"] = True
    quality["login_required"] = False
    quality["verification_required"] = False
    quality["auth_page_state"] = status.get("page_state") or "normal"
    quality["auth_detection_method"] = status.get("auth_detection_method") or quality.get("auth_detection_method", "")


def _mark_blocked(quality: dict[str, Any], status: dict[str, Any], tab, status_callback) -> None:
    auth_status = status.get("auth_status") or "verification_required"
    quality["auth_status"] = auth_status
    quality["auth_page_state"] = status.get("page_state") or auth_status
    quality["verification_required"] = auth_status in {"verification_required", "risk_control"}
    message = "小红书页面需要验证码或安全验证，已保存截图"
    if auth_status == "risk_control":
        message = "小红书页面触发安全限制，已保存截图"
        quality.setdefault("warnings", []).append("页面触发小红书安全限制，通常需要更换网络、登录态或人工验证后重试")
    _save_auth_screenshot(
        tab,
        quality,
        status_callback,
        event="login_verification_required",
        message=message,
    )


def _save_auth_screenshot(tab, quality: dict[str, Any], status_callback, *, event: str, message: str) -> None:
    """保存登录/验证截图，并把截图路径写入 quality。"""
    try:
        path = save_page_screenshot(tab, f"xhs_auth_{uuid.uuid4().hex[:8]}.png")
        quality["auth_screenshot_path"] = path
        quality["screenshot_path"] = path
        if f"页面截图已保存: {path}" not in quality.setdefault("warnings", []):
            quality["warnings"].append(f"页面截图已保存: {path}")
        _emit(status_callback, event, message=message, screenshot_path=path)
    except Exception as exc:
        quality.setdefault("warnings", []).append(f"登录/验证截图保存失败: {exc}")
        _emit(status_callback, event, message=message)


def _finish_quality(quality: dict[str, Any], started_at: float) -> dict[str, Any]:
    quality["auth_elapsed_seconds"] = round(time.time() - started_at, 2)
    return quality


def _cookie_attr(cookie: Any, name: str) -> str:
    """兼容 dict、对象属性两种 cookie 表示。"""
    if isinstance(cookie, dict):
        return str(cookie.get(name) or "")
    return str(getattr(cookie, name, "") or "")


def _emit(callback, event: str, **payload: Any) -> None:
    """向前端报告登录进度；回调异常不影响主流程。"""
    if not callback:
        return
    try:
        callback(event, payload)
    except Exception:
        pass
