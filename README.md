# 视频下载器

基于 [yt-dlp](https://github.com/yt-dlp/yt-dlp) 的简单 GUI 视频下载工具（Python tkinter，无第三方 GUI 依赖）。

## 功能

- 粘贴 URL 后自动分析并下载**最高画质**视频（自动合并音视频流，输出 MP4）
- 实时下载进度条 + 进度百分比 / 已下载大小（分析/合并阶段滚动动画）
- **下载队列**：下载中时新 URL 自动入队；点「下载队列」打开队列窗口，
  每行一个 URL，右键或快捷键操作：复制 URL (`Ctrl+C`)、上移 (`↑`)、
  下移 (`↓`)、取消 (`Delete`，需确认)
- **多线程加速**：DASH/HLS 分片 8 线程并行下载（YouTube/B 站等）；
  若系统装有 aria2c（`aria2c -x16`），单文件流也会并行下载
- 断点续传 + 记忆保存目录 + **cookie 自动获取**（三级自动，无需手动换文件）：
  1. 自动从浏览器提取 cookie（Firefox 可直读；新版 Chrome/Edge 的 App-Bound 加密无法直读）
  2. **Chrome CDP**：让 Chrome 自己交出 cookie（浏览器自身解密，绕过加密限制）。
     首次使用时弹出**普通 Chrome 窗口**（无任何调试参数，避免自动化检测），
     在其中登录 YouTube 一次并关闭窗口，之后 cookie 过期全自动刷新
  3. 全部失败才弹窗手动选择 cookie 文件

## 环境要求

- Python 3.8+
- yt-dlp：`pip install -r requirements.txt`（或 `pip install -U yt-dlp`）
- ffmpeg（合并音视频流必需）：`winget install -e --id Gyan.FFmpeg`，装完重启终端
- 可选：aria2c（多线程下载单文件流）。本机已安装于 `E:\Programs\aria2`（已加入用户 PATH），程序自动检测启用 16 连接并行

## 运行

```bash
python video_downloader.py
```

## 使用

1. 输入视频 URL（支持 YouTube、B 站等 yt-dlp 支持的站点），回车或点「开始下载」
2. 下载中继续输入 URL 会进入队列，点「下载队列」可查看/排序/取消
3. 出错时点「继续下载」自动断点续传；cookie 失效时会弹出文件选择框并覆盖更新

> 提示：部分站点（如 YouTube 1080p+）视频流与音频流分离，依赖 ffmpeg 合并；
> 若遇需要登录/地区限制的视频，可在 `_download_worker` 的 `opts` 中追加
> `"cookiesfrombrowser": ("chrome",)` 使用浏览器 cookie。
