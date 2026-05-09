"""小红书数据采集插件命令行入口。

用于本地调试：

python -m plugins.xhs_comment_analysis "/xhs https://..."
"""

import argparse
import json

from .browser import close_browser, open_browser
from .debug_snapshot import save_debug_snapshot
from .fetch import fetch_xhs_note
from .login import LOGIN_CHECK_URL, check_login_status


def main() -> int:
    parser = argparse.ArgumentParser(description="采集小红书帖子数据并保存 JSON")
    parser.add_argument("url", nargs="?", help="小红书帖子链接或包含链接的分享文本")
    parser.add_argument("--show", action="store_true", help="输出完整 payload")
    parser.add_argument("--headed", action="store_true", help="使用有界面模式调试")
    parser.add_argument("--limit", type=int, default=600, help="最多采集评论数，最大 600")
    parser.add_argument("--login-only", action="store_true", help="只检查当前小红书浏览器 profile 并保存调试快照")
    parser.add_argument("--login-profile", action="store_true", help="打开有界面浏览器，手动登录小红书 profile")
    args = parser.parse_args()

    if args.login_profile:
        _open_login_profile()
        return 0

    if args.login_only:
        output = _run_login_debug(headless=not args.headed)
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0

    if not args.url:
        parser.error("url is required unless --login-only is used")

    result = fetch_xhs_note(
        args.url,
        headless=not args.headed,
        comment_limit=args.limit,
    )
    output = result if args.show else {
        "record_id": result.get("record_id"),
        "file_path": result.get("file_path"),
        "index_path": result.get("index_path"),
        "quality": (result.get("payload") or {}).get("quality"),
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


def _run_login_debug(*, headless: bool) -> dict:
    """只检查当前 profile 登录态，用于排查 cookie 失效和 300012 风控。"""
    browser = None
    try:
        browser = open_browser(headless=headless)
        tab = browser.latest_tab
        tab.get(LOGIN_CHECK_URL)
        try:
            tab.wait.doc_loaded(timeout=20)
        except Exception:
            pass
        status = check_login_status(tab)
        debug_snapshot_path = save_debug_snapshot(
            tab,
            input_url=LOGIN_CHECK_URL,
            phase="login_debug",
        )
        return {
            "url": getattr(tab, "url", ""),
            "status": status,
            "debug_snapshot_path": debug_snapshot_path,
        }
    finally:
        close_browser(browser)


def _open_login_profile() -> None:
    """打开固定 profile 的有界面浏览器，让用户手动登录小红书。"""
    browser = open_browser(headless=False)
    tab = browser.latest_tab
    tab.get(LOGIN_CHECK_URL)
    print("已打开小红书登录 profile。请在浏览器中完成登录。")
    print("登录完成后关闭浏览器，或回到这里按 Ctrl+C 结束。")
    try:
        while True:
            input()
    except (KeyboardInterrupt, EOFError):
        close_browser(browser)


if __name__ == "__main__":
    raise SystemExit(main())
