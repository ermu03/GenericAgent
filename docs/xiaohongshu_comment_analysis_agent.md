# 小红书评论分析 Agent 目标文档

## 总目标

开发一个小红书评论分析 Agent。用户在微信前端发送一个小红书帖子链接后，GenericAgent 自动获取帖子相关数据，并生成评论分析报告，或者根据用户的提问，根据帖子的数据进行回答（如这个帖子的主题是什么，有哪些高赞观点？）

微信入口先定为：

```text
/xhs <小红书帖子链接>
```

目标链路：

```text
微信发送 /xhs <url>
  -> 识别小红书链接
  -> 获取帖子、作者、评论和评论作者数据
  -> 整理成标准数据结构
  -> 分析评论观点和洞察
  -> 生成分析报告或者根据用户问题进行回答
  -> 通过微信返回分析
```

## 项目位置

后续功能作为 GenericAgent 插件开发。

插件目录命名为：

```text
plugins/xhs_comment_analysis/
```

微信前端只使用：

```text
frontends/wechatapp.py
```

爬取和整理后的小红书 JSON 数据不放在 `temp/`。在项目根目录新建数据目录：

```text
data/xhs_data/
```
保存结构：

```text
data/xhs_data/
  raw/
  reports/
  work/
```

目录含义：

- `data/xhs_data/raw/`：保存从小红书链接获取并标准化后的帖子 JSON。
- `data/xhs_data/reports/`：保存基于 JSON 生成的 Markdown 或 JSON 分析报告。
- `data/xhs_data/work/`：保存插件运行时的中间文件。

## 用户体验目标

用户只需要在微信里发送：

```text
/xhs https://www.xiaohongshu.com/explore/...
```

Agent 应该完成后续流程：

- 识别链接。
- 获取该帖子的相关数据。
- 告诉用户采集进度和结果。
- 评论采集耗时较长时，持续返回阶段性进度、采集结束提示和最终保存结果。
- 生成评论分析报告。
- 如果报告较长，通过文件返回。

## 数据获取目标

第一阶段重点是根据小红书帖子链接获取分析所需数据。

希望获取的数据包括：

- 帖子标题。
- 帖子正文。
- 帖子标签。
- 帖子发布时间。
- 帖子点赞、收藏、评论、分享等互动数据。
- 发帖人基础信息。
- 评论文本。
- 评论点赞数。
- 评论回复关系。
- 评论发布时间。
- 评论作者基础信息。
- 可用于后续分析的数据质量信息。

数据获取完成后，保存为标准 JSON 文件，供后续分析模块使用。

## 标准数据目标

标准数据结构围绕四类对象组织。

### 帖子

```json
{
  "post": {
    "post_id": "",
    "url": "",
    "title": "",
    "content": "",
    "tags": [],
    "created_at": "",
    "liked_count": 0,
    "collected_count": 0,
    "comment_count": 0,
    "shared_count": 0,
    "metadata": {}
  }
}
```

### 发帖人

```json
{
  "post_owner": {
    "user_id": "",
    "nickname": "",
    "profile_url": "",
    "bio": "",
    "location": "",
    "followers_count": null,
    "following_count": null,
    "liked_count": null,
    "verified": null,
    "profile_tags": [],
    "metadata": {}
  }
}
```

### 评论

```json
{
  "comments": [
    {
      "comment_id": "",
      "level": 1,
      "root_comment_id": null,
      "reply_comment_id": null,
      "owner_user_id": "",
      "owner_nickname": "",
      "text": "",
      "liked_count": 0,
      "reply_count": 0,
      "created_at": "",
      "metadata": {}
    }
  ]
}
```

### 评论作者

```json
{
  "comment_owners": [
    {
      "user_id": "",
      "nickname": "",
      "profile_url": "",
      "bio": "",
      "location": "",
      "followers_count": null,
      "following_count": null,
      "liked_count": null,
      "verified": null,
      "profile_tags": [],
      "metadata": {}
    }
  ]
}
```

## 数据获取方案

目前考虑四种数据获取方案。

### 1. DrissionPage

选择 DrissionPage 作为主要开发方案。

DrissionPage 可以在服务器上控制 Chromium 浏览器，支持 headless 运行，适合放在 `plugins/xhs_comment_analysis/` 里做独立采集模块。

当前开发环境：

- 本地开发环境：WSL2，Ubuntu 22.04。
- 本地 Python：Python 3.10。
- 本地 Chrome：已检测到 `/usr/bin/google-chrome`。
- 本地 DrissionPage：已加入项目依赖并安装到虚拟环境。
- 服务器环境：Ubuntu 24.04。
- 后续如需安装 Chrome、DrissionPage 或浏览器相关组件，需要先明确告知再执行。

大致思路：

- 使用 DrissionPage 打开用户提供的小红书链接。
- 使用 `.venv/bin/python plugins/xhs_comment_analysis/login_profile.py` 打开固定浏览器 profile，人工登录小红书。
- 服务器采集流程直接复用这个浏览器 profile 中的登录态和本地状态。
- 验证码、安全验证或风控状态只提示用户介入，不自动破解。
- 从页面可见内容、DOM 或页面状态中提取帖子和评论数据。
- 滚动评论区，逐步采集评论。
- 评论采集阶段每新增约 100 条评论返回一次进度，采集结束后返回整理保存提示，保存完成后返回最终结果。
- 将结果整理成标准 JSON，保存到 `data/xhs_data/raw/`。
- 后续基于 JSON 生成报告或回答问题。

登录态和浏览器本地状态保存在 `data/xhs_data/work/local_login_profile/`，并随项目 git 同步。raw JSON 只保存帖子、作者、评论和评论作者；采集质量、来源和截图路径写入 `data/xhs_data/index.json`。

### 2. TMWebDriver

GenericAgent 已有的浏览器控制方案。

优点是已经集成在 GA 里，可以控制真实 Chrome，并保留浏览器登录态。缺点是服务器上需要配置 Chrome、扩展和图形环境或虚拟显示，部署成本比 DrissionPage 高。

后续可以作为调试和辅助方案。

### 3. xiaohongshu-mcp

一个基于 Go 和浏览器自动化的小红书 MCP 项目。

它已经实现了部分帖子详情和评论采集能力，代码可以参考。但当前它对帖子详情参数有额外要求，和微信里直接复制的小红书链接不完全匹配，所以暂时不作为主方案。

### 4. xhs-cli-headless

一个面向无 GUI 服务器的小红书 CLI 项目。

它的结构化输出和错误诊断设计值得参考。但它主要通过 HTTP API 和签名逻辑获取数据，不作为当前默认实现路线。
