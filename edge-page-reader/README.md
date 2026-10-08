# 网页取用器 Page Reader（Edge 扩展）

把 Edge 当前标签页的正文和元信息抽出来，一键复制或推送出去。纯本地运行，页面内容不会发往任何第三方。

## 安装

1. 打开 `edge://extensions`，打开左下角「开发人员模式」。
2. 点「加载解压缩的扩展」，选择本文件夹（`edge-page-reader`）。
3. 点工具栏的拼图图标，把「网页取用器」固定到工具栏。

更新代码后，回到 `edge://extensions` 点该扩展的「重新加载」即可。

## 用法

点工具栏图标打开弹窗，会自动读取当前标签页：

- **格式**：Markdown 正文 / 纯文本 / JSON（含元信息）/ 选中内容。
- **复制**：按当前格式复制到剪贴板，也可以直接在预览框里改完再复制。
- **存为 .md**：把 Markdown 正文下载成文件。
- **推送到接口**：POST 到设置里配置的地址。

浏览器内置页（`edge://`、扩展商店、PDF 阅读器）无法读取，会提示切换页面。

## 推送到本地接口

在仓库根目录双击 `run-ingest-server.cmd`（或运行 `python tools/page_ingest_server.py`），
它会在 `http://localhost:8787/ingest` 接收 POST，并把每条记录追加到
`scheduler/state/inbox.jsonl`。

推送的 JSON 结构：

```json
{
  "source": "edge-page-reader",
  "title": "页面标题",
  "url": "https://example.com/post",
  "canonicalUrl": "https://example.com/post",
  "siteName": "example.com",
  "author": "作者",
  "publishedTime": "2026-10-07T12:00:00Z",
  "description": "摘要",
  "charCount": 1234,
  "readAt": "2026-10-07T12:34:56.000Z",
  "format": "markdown",
  "content": "# 正文 Markdown"
}
```

设置页可以改成任何 `http://` / `https://` 地址，并附加自定义请求头；首次保存非本机地址时
Edge 会弹权限确认。

## 文件

| 文件 | 作用 |
| --- | --- |
| `manifest.json` | MV3 清单，权限尽量收窄 |
| `extract.js` | 正文抽取（自成一体，注入页面执行） |
| `popup.html` / `popup.js` | 弹窗界面与复制、下载、推送逻辑 |
| `options.html` / `options.js` | 接口地址、请求头、默认格式设置 |
| `style.css` | 弹窗与设置页样式 |
| `icons/` | 16 / 32 / 48 / 128 图标 |
