# 小红书数据获取阶段实现方案

## 阶段目标

第一阶段只完成小红书帖子数据获取。

目标链路：

```text
微信 /xhs <小红书帖子链接>
  -> 提取小红书链接
  -> 调用 plugins/xhs_comment_analysis/
  -> 使用 DrissionPage 打开链接并获取页面数据
  -> 标准化帖子、作者、评论、评论作者数据
  -> 保存到 data/xhs_data/raw/*.json
  -> 微信返回采集结果和 JSON 文件路径
```

本阶段不做评论分析、观点总结和报告生成。

## 第一阶段决策

- 评论采集尽可能多，但设置上限，最多采集 600 条评论。
- 帖子下的直接评论是一级评论；对一级评论的回复是二级评论。
- 第一版需要采集二级评论，并保留评论之间的回复关系。
- DrissionPage 从第一版开始使用 headless 模式。
- 第一版需要包含二维码登录和验证码提示流程。
- 同一个帖子重复采集时覆盖原 JSON，不保留历史版本。

## 目录规划

插件代码目录：

```text
plugins/xhs_comment_analysis/
```

数据目录：

```text
data/xhs_data/
  raw/
  reports/
  work/
```

本阶段只写入：

```text
data/xhs_data/raw/
data/xhs_data/work/
```

`reports/` 留给后续分析阶段。

## 模块规划

第一版插件建议包含：

```text
plugins/xhs_comment_analysis/
  __init__.py
  browser.py
  extractor.py
  fetch.py
  schema.py
  storage.py
  url_utils.py
```

职责：

- `browser.py`：封装 DrissionPage 浏览器启动、登录态复用、页面打开和基础页面状态判断。
- `extractor.py`：接收 DrissionPage 页面对象，提取帖子信息，滚动评论区，采集评论并去重。
- `fetch.py`：编排完整数据获取流程，从 URL 到保存 JSON。
- `schema.py`：把采集到的原始数据转换成标准 JSON 结构。
- `storage.py`：创建数据目录、生成文件名、保存 JSON。
- `url_utils.py`：从微信文本中提取和规范化小红书链接。
- `__init__.py`：暴露插件入口函数。

这个划分保持模块数量较少，同时让浏览器控制、页面采集、数据结构和文件保存各自独立，避免 `fetch.py` 变成过大的混合模块。

## 微信入口

微信前端新增命令：

```text
/xhs <小红书帖子链接>
```

处理目标：

- 校验命令格式。
- 提取小红书链接，支持电脑网页版链接和手机 App 分享短链。
- 调用插件采集数据。
- 将采集状态返回微信用户。
- 采集成功时返回 JSON 文件路径。

第一版可以先让 `/xhs` 只负责数据获取，不触发分析。

需要支持的链接形态：

```text
https://www.xiaohongshu.com/search_result/<note_id>?xsec_token=...&xsec_source=pc_search
http://xhslink.com/o/<short_code>
```

手机 App 分享内容通常会带额外文案，只需要从整段文本中提取 `xhslink.com` 短链。

## DrissionPage 采集思路

DrissionPage 负责控制 Chromium 打开页面并读取数据。

大致流程：

```text
启动或复用 Chromium
  -> 使用固定浏览器用户数据目录
  -> 使用 headless 模式
  -> 打开小红书链接
  -> 等待页面加载
  -> 检查是否需要登录或验证码
  -> 提取帖子基础数据
  -> 滚动评论区
  -> 分批提取评论数据
  -> 去重
  -> 生成标准 JSON
  -> 保存文件
```

浏览器用户数据目录建议放在：

```text
data/xhs_data/work/browser_profile/
```

这样可以复用登录态。

## 数据提取目标

第一版尽量提取：

- 帖子标题。
- 帖子正文。
- 帖子标签。
- 帖子发布时间。
- 帖子互动数据。
- 发帖人昵称、主页链接、可见 ID。
- 评论文本。
- 评论点赞数。
- 评论发布时间。
- 评论作者昵称、主页链接、可见 ID。
- 评论父子关系。
- 二级评论。

如果某些字段无法稳定获取，允许为空，但需要写入数据质量信息。

数据质量信息写入输出 JSON 顶层的 `quality` 字段。

建议记录：

- `status`：采集状态，例如 `success`、`partial_success`、`failed`。
- `missing_fields`：缺失字段列表，例如 `post.created_at`、`comments.created_at`。
- `warnings`：采集过程中的非致命问题，例如“评论区未滚动到底”。
- `errors`：导致采集失败或部分失败的错误信息。
- `comment_count_collected`：实际采集到的评论数。
- `comment_limit`：本次采集评论数上限，第一版为 600。
- `comment_owner_count_collected`：实际采集到的评论作者数。
- `has_more_comments`：是否判断还有更多评论未采集。
- `login_required`：是否检测到需要登录。
- `verification_required`：是否检测到验证码或安全验证。
- `page_state`：页面状态，例如 `normal`、`login_required`、`verification_required`、`not_found`。
- `elapsed_seconds`：本次采集耗时。

## 标准 JSON

输出文件包含：

```json
{
  "post": {},
  "post_owner": {},
  "comments": [],
  "comment_owners": [],
  "quality": {},
  "source": {}
}
```

`quality` 记录采集质量：

```json
{
  "status": "success",
  "comment_count_collected": 0,
  "comment_limit": 600,
  "comment_owner_count_collected": 0,
  "has_more_comments": null,
  "missing_fields": [],
  "warnings": [],
  "errors": [],
  "login_required": false,
  "verification_required": false,
  "page_state": "normal",
  "elapsed_seconds": 0
}
```

`source` 记录采集来源：

```json
{
  "input_url": "",
  "final_url": "",
  "fetched_at": "",
  "fetch_method": "drissionpage"
}
```

## 文件命名

一个小红书帖子的原始数据对应一个 JSON 文件。

文件名优先使用帖子 ID：

```text
data/xhs_data/raw/xhs_note_<post_id>.json
```

对应的记录 ID 为：

```text
xhs_note_<post_id>
```

后续用户可以把 `record_id` 发给 bot，让 bot 继续读取对应 JSON。

如果暂时无法提取帖子 ID，使用 URL hash 作为兜底：

```text
data/xhs_data/raw/xhs_note_url_<hash8>.json
```

对应的记录 ID 为：

```text
xhs_note_url_<hash8>
```

保存文件时使用 UTF-8，JSON 缩进为 2，保留中文。

## 索引文件

为方便用户和 bot 找到已经采集过的帖子数据，维护一个索引文件：

```text
data/xhs_data/index.json
```

每次成功保存帖子 JSON 后，同步更新 `index.json`。

索引文件建议结构：

```json
{
  "items": [
    {
      "record_id": "xhs_note_<post_id>",
      "post_id": "<post_id>",
      "title": "",
      "owner_nickname": "",
      "file_path": "data/xhs_data/raw/xhs_note_<post_id>.json",
      "input_url": "",
      "final_url": "",
      "fetched_at": "",
      "comment_count_collected": 0
    }
  ]
}
```

用户可以先问 bot 当前有哪些小红书帖子数据。bot 读取 `data/xhs_data/index.json` 后，把已有记录返回给用户。用户再把某个 `record_id` 发给 bot，bot 根据 `index.json` 找到对应 JSON 文件继续处理。

同一个帖子重复采集时，默认覆盖同一个 `data/xhs_data/raw/xhs_note_<post_id>.json` 文件，并刷新 `index.json` 中对应记录。不保留历史版本。

## 登录和验证码

第一版需要做二维码登录和验证码提示流程。

需要识别：

- 未登录。
- 登录二维码。
- 验证码或安全验证。
- 页面打开失败。
- 链接无效。

如果需要二维码登录，插件需要把登录提示返回给微信用户。验证码或安全验证暂不自动破解，需要明确提示用户介入。

后续可以参考 `xhs-cli-headless` 的二维码登录、cookie 导入和登录态诊断思路。

## 第一版完成标准

- 能从 `/xhs <url>` 提取小红书链接。
- 能以 headless 模式启动 DrissionPage 并打开链接。
- 能处理二维码登录提示和验证码提示。
- 能保存一个标准 JSON 文件到 `data/xhs_data/raw/`。
- JSON 至少包含帖子基础信息、已采集到的一级评论和二级评论。
- 失败时能返回明确错误原因。
- 不改其他前端。
- 不生成分析报告。

## 后续阶段

第二阶段再基于 `data/xhs_data/raw/*.json` 做评论分析、观点提取、置信度计算和报告生成。
