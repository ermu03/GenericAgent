"""打开小红书登录 profile，方便手动切换账号。"""

import sys
from pathlib import Path

if __package__ in {None, ""}:
    project_root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(project_root))
    from plugins.xhs_comment_analysis.browser import close_browser, open_browser
    from plugins.xhs_comment_analysis.login import LOGIN_CHECK_URL
else:
    from .browser import close_browser, open_browser
    from .login import LOGIN_CHECK_URL


def main() -> int:
    """打开有界面浏览器，让用户在固定 profile 中登录小红书。"""
    browser = open_browser(headless=False)
    tab = browser.latest_tab
    tab.get(LOGIN_CHECK_URL)
    print("已打开小红书登录浏览器。")
    print("如果要换账号，请在浏览器里退出旧账号，再登录新账号。")
    print("登录完成后关闭浏览器，或回到这个终端按 Ctrl+C 结束。")
    try:
        while True:
            input()
    except (KeyboardInterrupt, EOFError):
        close_browser(browser)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
