"""小红书数据采集使用的 DrissionPage 浏览器配置。"""

import os
import socket
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
XHS_DATA_DIR = PROJECT_ROOT / "data" / "xhs_data"
WORK_DIR = XHS_DATA_DIR / "work"
BROWSER_PROFILE_DIR = Path(os.environ.get("XHS_BROWSER_PROFILE_DIR", WORK_DIR / "local_login_profile")).expanduser()
SCREENSHOT_DIR = WORK_DIR / "screenshots"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/146.0.0.0 Safari/537.36"
)
STEALTH_INIT_JS = r"""
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN', 'zh', 'en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
window.chrome = window.chrome || {runtime: {}};
"""


class BrowserSetupError(RuntimeError):
    """浏览器初始化失败时抛出。"""


def ensure_browser_dirs() -> None:
    """创建 DrissionPage 浏览器 profile 目录。

    profile 目录放在项目数据目录下，用于复用本地脚本登录后的浏览器状态。
    """
    BROWSER_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)


def create_browser_options(headless: bool = True):
    """创建 DrissionPage 的 ChromiumOptions。

    这里集中设置浏览器用户数据目录，后续如果要加 Chrome 路径、
    代理、headless 参数或服务器专用启动参数，也统一放在这里。
    """
    try:
        from DrissionPage import ChromiumOptions
    except ImportError as exc:
        raise BrowserSetupError(
            "DrissionPage is not installed. Install project dependencies first."
        ) from exc

    ensure_browser_dirs()
    options = ChromiumOptions()
    # 使用项目内固定 profile，让本地登录态可以跨任务复用。
    options.set_user_data_path(str(BROWSER_PROFILE_DIR))
    # DrissionPage 的 auto_port 会改写 user-data-dir，导致登录态不能复用。
    # 这里自己选一个空闲端口，既避开端口冲突，也保留固定 profile。
    options.set_local_port(_find_free_port())
    chrome_path = os.environ.get("XHS_CHROME_PATH", "").strip()
    if chrome_path:
        options.set_browser_path(chrome_path)
    user_agent = os.environ.get("XHS_USER_AGENT", DEFAULT_USER_AGENT).strip()
    if user_agent:
        options.set_user_agent(user_agent)
    options.set_timeouts(base=15, page_load=45, script=15)
    # 云服务器容器/低共享内存环境常见参数；本地 WSL 也兼容。
    options.set_argument("--no-sandbox")
    options.set_argument("--disable-dev-shm-usage")
    options.set_argument("--disable-gpu")
    options.set_argument("--no-first-run")
    options.set_argument("--no-default-browser-check")
    options.set_argument("--disable-popup-blocking")
    options.set_argument("--lang", "zh-CN")
    options.set_argument("--window-size", "1280,1800")
    options.set_argument("--disable-blink-features", "AutomationControlled")
    options.set_argument("--disable-features", "AutomationControlled")
    options.set_argument("--accept-lang", "zh-CN,zh,en")
    if headless:
        options.headless(True)
    return options


def open_browser(headless: bool = True):
    """启动或连接 DrissionPage Chromium 实例。"""
    try:
        from DrissionPage import Chromium
    except ImportError as exc:
        raise BrowserSetupError(
            "DrissionPage is not installed. Install project dependencies first."
        ) from exc

    browser = Chromium(create_browser_options(headless=headless))
    prepare_tab(browser.latest_tab)
    return browser


def prepare_tab(tab) -> None:
    """给页面注入基础反自动化暴露修正脚本。"""
    try:
        tab.add_init_js(STEALTH_INIT_JS)
        tab.run_cdp(
            "Emulation.setUserAgentOverride",
            userAgent=os.environ.get("XHS_USER_AGENT", DEFAULT_USER_AGENT).strip() or DEFAULT_USER_AGENT,
            acceptLanguage="zh-CN,zh;q=0.9,en;q=0.8",
            platform="Linux x86_64",
            userAgentMetadata={
                "brands": [
                    {"brand": "Not:A-Brand", "version": "99"},
                    {"brand": "Google Chrome", "version": "146"},
                    {"brand": "Chromium", "version": "146"},
                ],
                "fullVersionList": [
                    {"brand": "Not:A-Brand", "version": "99.0.0.0"},
                    {"brand": "Google Chrome", "version": "146.0.7680.153"},
                    {"brand": "Chromium", "version": "146.0.7680.153"},
                ],
                "fullVersion": "146.0.7680.153",
                "platform": "Linux",
                "platformVersion": "6.8.0",
                "architecture": "x86",
                "model": "",
                "mobile": False,
                "bitness": "64",
                "wow64": False,
            },
        )
        tab.run_cdp(
            "Emulation.setDeviceMetricsOverride",
            width=1280,
            height=1800,
            deviceScaleFactor=1,
            mobile=False,
            screenWidth=1280,
            screenHeight=1800,
        )
        tab.run_cdp(
            "Emulation.setTimezoneOverride",
            timezoneId=os.environ.get("XHS_TIMEZONE", "Asia/Shanghai"),
        )
    except Exception:
        # 注入失败不应阻断采集，后续页面状态会给出真实失败原因。
        pass


def close_browser(browser) -> None:
    """关闭本次采集启动的浏览器进程，但保留用户数据目录。"""
    if browser is None:
        return
    try:
        browser.quit(timeout=5, force=True, del_data=False)
    except Exception:
        # 浏览器进程已经退出或 CDP 断开时无需阻断数据保存。
        pass


def save_page_screenshot(tab, name: str = "xhs_page.png") -> str:
    """保存当前页面截图，用于验证码、安全验证或页面状态排查。"""
    ensure_browser_dirs()
    path = SCREENSHOT_DIR / name
    tab.get_screenshot(path=str(path), full_page=False)
    return str(path)


def _find_free_port() -> int:
    """从系统申请一个当前空闲的本地端口。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
