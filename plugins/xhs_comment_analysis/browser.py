"""小红书数据采集使用的 DrissionPage 浏览器配置。"""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
XHS_DATA_DIR = PROJECT_ROOT / "data" / "xhs_data"
WORK_DIR = XHS_DATA_DIR / "work"
BROWSER_PROFILE_DIR = WORK_DIR / "browser_profile"


class BrowserSetupError(RuntimeError):
    """浏览器初始化失败时抛出。"""


def ensure_browser_dirs() -> None:
    """创建 DrissionPage 浏览器 profile 目录。

    profile 目录放在项目数据目录下，用于保存小红书登录态。
    """
    BROWSER_PROFILE_DIR.mkdir(parents=True, exist_ok=True)


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
