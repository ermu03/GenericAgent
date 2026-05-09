"""小红书数据采集使用的 DrissionPage 浏览器配置。"""

import os
import socket
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
XHS_DATA_DIR = PROJECT_ROOT / "data" / "xhs_data"
WORK_DIR = XHS_DATA_DIR / "work"
BROWSER_PROFILE_DIR = WORK_DIR / "browser_profile"
SCREENSHOT_DIR = WORK_DIR / "screenshots"


class BrowserSetupError(RuntimeError):
    """浏览器初始化失败时抛出。"""


def ensure_browser_dirs() -> None:
    """创建 DrissionPage 浏览器 profile 目录。

    profile 目录放在项目数据目录下，用于保存小红书登录态。
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
    # 使用项目内固定 profile，让二维码登录后的 cookie 可以跨任务复用。
    options.set_user_data_path(str(BROWSER_PROFILE_DIR))
    # DrissionPage 的 auto_port 会改写 user-data-dir，导致登录态不能复用。
    # 这里自己选一个空闲端口，既避开端口冲突，也保留固定 profile。
    options.set_local_port(_find_free_port())
    chrome_path = os.environ.get("XHS_CHROME_PATH", "").strip()
    if chrome_path:
        options.set_browser_path(chrome_path)
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

    return Chromium(create_browser_options(headless=headless))


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
    """保存当前页面截图，用于登录二维码或验证码提示。"""
    ensure_browser_dirs()
    path = SCREENSHOT_DIR / name
    tab.get_screenshot(path=str(path), full_page=False)
    return str(path)


def _find_free_port() -> int:
    """从系统申请一个当前空闲的本地端口。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
