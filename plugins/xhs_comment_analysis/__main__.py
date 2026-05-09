"""小红书数据采集插件命令行入口。

用于本地调试：

python -m plugins.xhs_comment_analysis "/xhs https://..."
"""

import argparse
import json

from .fetch import fetch_xhs_note


def main() -> int:
    parser = argparse.ArgumentParser(description="采集小红书帖子数据并保存 JSON")
    parser.add_argument("url", help="小红书帖子链接或包含链接的分享文本")
    parser.add_argument("--show", action="store_true", help="输出完整 payload")
    parser.add_argument("--headed", action="store_true", help="使用有界面模式调试")
    parser.add_argument("--limit", type=int, default=600, help="最多采集评论数，最大 600")
    args = parser.parse_args()

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


if __name__ == "__main__":
    raise SystemExit(main())
