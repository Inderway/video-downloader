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
- 断点续传 + 记忆保存目录 + cookie 管理：
  - 下载自动使用程序目录下的 `cookies.txt`（Netscape 格式）
  - 主页「更换 Cookie」按钮可手动选择新的 cookie 文件并覆盖
  - cookie 失效时弹窗提示手动选择有效 cookie 文件后重试
  - 可选：集成 [bgutil-ytdlp-pot-provider](https://github.com/Brainicism/bgutil-ytdlp-pot-provider)
    （需 Node/Deno），自动为 YouTube 生成 PO Token，解锁年龄限制视频高画质

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
> 若遇需要登录/地区限制的视频，请通过「更换 Cookie」提供有效的登录 cookie。

## 更新日志

- **v1.1.5**：修复小功能窗口（旋转/合并/提取）操作时主页跳到屏幕顶层——所有文件/目录选择对话框改为以工具窗口为父窗口
- **v1.1.4**：视频旋转支持多文件选择，队列式逐条执行（单文件失败不中断）
- **v1.1.3**：新增「取消任务」按钮，可中断当前下载（保留分片，支持续传）；URL 输入框内容不再自动清除，仅关闭软件后清空
- **v1.1.2**：音视频合并支持 `.weba` 音频格式
- **v1.1.1**：修复无代理环境变量时下载卡死在"分析视频信息中"——自动检测系统代理并启用；超时/重试缩短，减少假死等待
- **v1.1.0**：集成 bgutil PO Token provider（app 启动时自动拉起本地服务），配合有效会话 cookie 可解锁年龄限制视频高画质
- **v1.0.2**：修复年龄限制视频只能下 360P——自动改用 web_safari 客户端（HLS 免 PO Token）重试获取高画质
- **v1.0.1**：新增「更换 Cookie」按钮；cookie 失效改为纯手动选择文件（移除浏览器自动提取与 Chrome CDP 自动获取）
- **v1.0.0**：标题栏显示版本号 vX.Y.Z
