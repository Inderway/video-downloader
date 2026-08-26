# -*- coding: utf-8 -*-
"""视频下载器 - 基于 yt-dlp 的简单 GUI 程序

功能：
- 输入 URL 后自动分析并下载最高画质视频（自动合并音视频，输出 MP4）
- 实时显示下载进度条（分析/合并阶段滚动动画）
- 下载队列：支持复制/上移/下移/取消任务（右键菜单 + 快捷键）
- 断点续传 + 记忆保存目录
- 多线程加速：DASH/HLS 分片并行下载；装有 aria2c 时单文件流也并行
"""

import datetime
import json
import os
import shutil
import subprocess
import threading
import time
import tkinter as tk
import urllib.request
from tkinter import ttk, filedialog, messagebox

import yt_dlp

try:
    import websocket
except ImportError:
    websocket = None

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
DEFAULT_DIR = os.path.join(os.path.expanduser("~"), "Downloads")
LOGS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
CONCURRENT_FRAGMENTS = 8


class FileLogger:
    """按小时滚动写入 logs/yyyy-mm-dd HH.txt 的日志器（线程安全）"""

    def __init__(self, log_dir):
        self.log_dir = log_dir
        self._lock = threading.Lock()

    def _path(self):
        return os.path.join(self.log_dir,
                            datetime.datetime.now().strftime("%Y-%m-%d %H") + ".txt")

    def write(self, level, msg):
        try:
            with self._lock:
                os.makedirs(self.log_dir, exist_ok=True)
                line = "[{}] [{}] {}".format(
                    datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), level, msg)
                with open(self._path(), "a", encoding="utf-8") as f:
                    f.write(line + "\n")
        except OSError:
            pass

    def info(self, msg):
        self.write("INFO", msg)

    def warning(self, msg):
        self.write("WARNING", msg)

    def error(self, msg):
        self.write("ERROR", msg)


file_logger = FileLogger(LOGS_DIR)


def _load_config():
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_config(cfg):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except OSError as e:
        file_logger.error(f"保存配置失败: {e}")


def _find_aria2c():
    exe = shutil.which("aria2c")
    if exe:
        return exe
    for cand in (r"E:\Programs\aria2\aria2c.exe",
                 r"C:\Program Files\aria2\aria2c.exe",
                 r"C:\aria2\aria2c.exe"):
        if os.path.isfile(cand):
            return cand
    return None


def _find_ffmpeg():
    return shutil.which("ffmpeg")


def _find_ffprobe():
    return shutil.which("ffprobe")


def _find_chrome():
    for cand in (r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                 r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"):
        if os.path.isfile(cand):
            return cand
    return shutil.which("chrome")


class ChromeCookieFetcher:
    """通过 Chrome DevTools Protocol 让 Chrome 自己交出 cookie（浏览器自身解密，
    绕过新版 App-Bound 加密）。使用独立 profile，用户首次需在其中登录一次。"""

    PORT = 9222
    PROFILE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "chrome-cookie-profile")
    SESSION_COOKIES = {"__Secure-3PSID", "__Secure-1PSID", "SID",
                       "SAPISID", "HSID", "SSID"}

    def __init__(self):
        self.proc = None
        self.launched_by_me = False
        self.target = None

    # ---------- 启动/连接 ----------

    def _alive(self):
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{self.PORT}/json/version", timeout=2):
                return True
        except Exception:
            return False

    def launch(self, headless=False):
        exe = _find_chrome()
        if not exe:
            return False
        args = [exe, f"--remote-debugging-port={self.PORT}",
                "--remote-allow-origins=*",
                f"--user-data-dir={self.PROFILE_DIR}",
                "--no-first-run", "--no-default-browser-check",
                "--disable-session-crashed-bubble", "--disable-features=Translate",
                "https://www.youtube.com"]
        if headless:
            args.insert(1, "--headless=new")
        try:
            self.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL)
        except OSError:
            return False
        for _ in range(40):
            if self._alive():
                return True
            time.sleep(0.5)
        return self._alive()

    def kill(self):
        if self.proc:
            try:
                subprocess.run(["taskkill", "/PID", str(self.proc.pid), "/T", "/F"],
                               capture_output=True, timeout=10)
            except Exception:
                pass
            self.proc = None

    # ---------- CDP 取 cookie ----------

    def get_cookies(self, timeout=15):
        if websocket is None:
            return None, "缺少 websocket-client 库"
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{self.PORT}/json", timeout=5) as resp:
                targets = json.load(resp)
        except Exception as e:
            return None, f"连接调试端口失败: {e}"
        page = next((t for t in targets if t.get("type") == "page"), None)
        if not page:
            return None, "Chrome 无可用页面"
        try:
            ws = websocket.create_connection(page["webSocketDebuggerUrl"],
                                             timeout=timeout)
        except Exception as e:
            return None, f"WebSocket 连接失败: {e}"

        def send(method, params=None):
            ws.send(json.dumps({"id": 1, "method": method, "params": params or {}}))
            while True:
                resp = json.loads(ws.recv())
                if resp.get("id") == 1:
                    return resp

        try:
            send("Network.enable")
            resp = send("Network.getAllCookies")
        except Exception as e:
            ws.close()
            return None, f"CDP 调用失败: {e}"
        ws.close()
        cookies = (resp.get("result") or {}).get("cookies") or []
        return cookies, None

    @staticmethod
    def _logged_in(cookies):
        names = {c.get("name") for c in cookies}
        return bool(names & ChromeCookieFetcher.SESSION_COOKIES)

    @staticmethod
    def _to_netscape(cookies):
        lines = ["# Netscape HTTP Cookie File",
                 "# Generated by video-downloader (Chrome CDP)"]
        for c in cookies:
            if c.get("partitionKey"):
                continue
            domain = c.get("domain") or ""
            path = c.get("path") or "/"
            secure = "TRUE" if c.get("secure") else "FALSE"
            expires = c.get("expires")
            if not expires or expires == -1:
                expires = 2147483647
            else:
                expires = int(expires)
            prefix = f"#HttpOnly_{domain}" if c.get("httpOnly") else domain
            lines.append(f"{prefix}\tTRUE\t{path}\t{secure}\t{expires}\t"
                         f"{c.get('name') or ''}\t{c.get('value') or ''}")
        return "\n".join(lines) + "\n"

    def fetch(self, on_need_login=None):
        """获取登录 cookie 并写入 cookies.txt。返回 (ok, msg)
        登录阶段使用无调试参数的普通 Chrome（避免自动化检测导致无法登录），
        登录完成后无头启动采集 cookie。"""
        cookies, err = self._harvest()
        if cookies and self._logged_in(cookies):
            return self._write(cookies)

        if on_need_login:
            on_need_login()
        if not self._open_login_window():
            return False, "无法打开 Chrome 登录窗口"
        cookies, err = self._harvest()
        if cookies and self._logged_in(cookies):
            return self._write(cookies)
        return False, "Chrome 登录窗口已关闭，但未检测到登录（请确认已登录 YouTube）"

    def _harvest(self):
        """无头启动(若端口未占用)并采集 cookie, 完毕后关闭自启实例"""
        launched = not self._alive()
        if launched:
            ok = False
            for _ in range(5):  # profile 可能被前一个实例短暂占用, 重试
                if self.launch(headless=True):
                    ok = True
                    break
                time.sleep(3)
            if not ok:
                return None, "无法启动 Chrome"
        cookies, err = self.get_cookies()
        if launched:
            self.kill()
        return cookies or [], err

    def _open_login_window(self):
        """普通 Chrome (无任何调试参数), 打开登录页并等待用户关闭窗口"""
        exe = _find_chrome()
        if not exe:
            return False
        args = [exe, f"--user-data-dir={self.PROFILE_DIR}",
                "--no-first-run", "--no-default-browser-check",
                "https://www.youtube.com"]
        try:
            self.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL)
        except OSError:
            return False
        try:
            self.proc.wait()  # 用户登录后关闭窗口即继续
        except Exception:
            pass
        self.proc = None
        time.sleep(2)  # 等待 profile 释放
        return True

    def _write(self, cookies):
        try:
            self.target = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                       "cookies.txt")
            with open(self.target, "w", encoding="utf-8") as f:
                f.write(self._to_netscape(cookies))
        except OSError as e:
            return False, f"写入 cookie 文件失败: {e}"
        return True, ""


_HW_ENCODERS = None


def _available_hw_encoders():
    """检测可用的硬件 H.264 编码器 (h264_nvenc / h264_qsv / h264_amf)"""
    global _HW_ENCODERS
    if _HW_ENCODERS is None:
        _HW_ENCODERS = []
        ffmpeg = _find_ffmpeg()
        if ffmpeg:
            try:
                out = subprocess.run([ffmpeg, "-hide_banner", "-encoders"],
                                     capture_output=True, text=True, timeout=30)
                text = out.stdout or out.stderr or ""
                _HW_ENCODERS = [ln.split()[1] for ln in text.splitlines()
                                if " V" in ln and ln.split()[1].startswith("h264_")]
            except (OSError, subprocess.TimeoutExpired):
                pass
    return _HW_ENCODERS


class ToolWindowBase:
    """小功能窗口基类：输入控件(子类) + 进度条 + 状态 + 日志 一体，
    进度条独立于此窗口，不影响主页下载进度条；无模态锁，可与下载并行"""

    def __init__(self, app, title):
        self.app = app
        self.closed = False
        self.running = False
        self.win = tk.Toplevel(app.root)
        self.win.title(title)
        self.win.resizable(False, False)
        self.win.update_idletasks()
        w, h = self.win.winfo_reqwidth(), self.win.winfo_reqheight()
        x = app.root.winfo_rootx() + (app.root.winfo_width() - w) // 2
        y = app.root.winfo_rooty() + (app.root.winfo_height() - h) // 2
        self.win.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        self.win.protocol("WM_DELETE_WINDOW", self._on_close)

        # 运行中始终置顶；用户点击其他窗口后允许被覆盖，点击回本窗口恢复置顶
        self.win.attributes("-topmost", True)
        self.win.bind("<FocusIn>", self._on_focus_in)
        self.win.bind("<FocusOut>", self._on_focus_out)

        self.input_frame = ttk.Frame(self.win)
        self.input_frame.pack(fill="x", padx=12, pady=(12, 4))
        self._build_inputs()

        self.progress = ttk.Progressbar(self.win, mode="determinate")
        self.progress.pack(fill="x", padx=12, pady=8)

        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(self.win, textvariable=self.status_var).pack(anchor="w", padx=12)

        self.log_text = tk.Text(self.win, height=6, state="disabled")
        self.log_text.pack(fill="both", expand=True, padx=12, pady=(6, 4))

        btns = ttk.Frame(self.win)
        btns.pack(pady=(0, 10))
        self.start_btn = ttk.Button(btns, text="开始", command=self._start)
        self.start_btn.pack(side="left", padx=8)
        ttk.Button(btns, text="关闭", command=self._on_close).pack(side="left", padx=8)

    def _build_inputs(self):
        raise NotImplementedError

    def _validate(self):
        """校验输入并保存参数，返回 (ok, 错误信息或 None)"""
        raise NotImplementedError

    def _run(self):
        """启动后台线程"""
        raise NotImplementedError

    def _start(self):
        if self.running:
            return
        ok, err = self._validate()
        if not ok:
            messagebox.showwarning("提示", err, parent=self.win)
            return
        self.running = True
        self.start_btn.config(state="disabled")
        self.status_var.set("准备中...")
        self._run()

    # ---- 供后台线程调用的接口 ----

    def _on_close(self):
        self.closed = True
        self.win.destroy()

    def _on_focus_in(self, _event):
        self.win.attributes("-topmost", True)
        self.win.lift()

    def _on_focus_out(self, _event):
        self.win.attributes("-topmost", False)

    def _schedule(self, func, *args):
        if not self.closed:
            self.win.after(0, lambda: func(*args))

    def log(self, msg, level="INFO"):
        file_logger.write(level, msg)
        if self.closed:
            return
        self.log_text.config(state="normal")
        self.log_text.insert("end", msg + "\n")
        self.log_text.see("end")
        self.log_text.config(state="disabled")

    def start_animate(self):
        if self.closed:
            return
        self.progress.stop()
        self.progress.config(mode="indeterminate")
        self.progress.start(12)

    def update_progress(self, pct):
        if self.closed:
            return
        if self.progress["mode"] != "determinate":
            self.progress.stop()
            self.progress.config(mode="determinate", value=0)
        self.progress["value"] = pct
        self.status_var.set(f"处理中 {pct:.0f}%")

    def set_status(self, text):
        if not self.closed:
            self.status_var.set(text)

    def finish(self, success, msg):
        self.running = False
        if not self.closed:
            self.progress.stop()
            self.progress.config(mode="determinate", value=100 if success else 0)
            self.status_var.set("完成" if success else "失败")
            self.start_btn.config(state="normal")
        self.log(msg, "INFO" if success else "ERROR")


class RotationTool(ToolWindowBase):
    """视频旋转：输入 + 进度 + 日志一体窗口"""

    def __init__(self, app, saved_dir):
        self.outdir_default = saved_dir or ""
        super().__init__(app, "视频旋转")
        self.angle = 90
        self.path = ""
        self.outdir = ""
        self.out = ""

    def _build_inputs(self):
        frame = self.input_frame
        ttk.Label(frame, text="视频文件:").grid(row=0, column=0, sticky="w")
        self.video_var = tk.StringVar()
        entry = ttk.Entry(frame, textvariable=self.video_var, width=40)
        entry.grid(row=0, column=1, padx=6, pady=3)
        ttk.Button(frame, text="浏览", command=self._pick_video).grid(row=0, column=2)

        ttk.Label(frame, text="输出文件夹:").grid(row=1, column=0, sticky="w")
        self.outdir_var = tk.StringVar(value=self.outdir_default)
        entry = ttk.Entry(frame, textvariable=self.outdir_var, width=40)
        entry.grid(row=1, column=1, padx=6, pady=3)
        ttk.Button(frame, text="浏览", command=self._pick_outdir).grid(row=1, column=2)

        ttk.Label(frame, text="输出文件名:").grid(row=2, column=0, sticky="w")
        self.filename_var = tk.StringVar(value="")
        ttk.Label(frame, textvariable=self.filename_var,
                  foreground="#666").grid(row=2, column=1, columnspan=2, sticky="w", padx=6)

        ttk.Label(frame, text="顺时针角度:").grid(row=3, column=0, sticky="w")
        self.angle_var = tk.IntVar(value=90)
        angle_frame = ttk.Frame(frame)
        angle_frame.grid(row=3, column=1, columnspan=2, sticky="w", padx=6)
        for a in (90, 180, 270):
            ttk.Radiobutton(angle_frame, text=f"{a}°", value=a,
                            variable=self.angle_var,
                            command=self._update_filename).pack(side="left", padx=6)

    def _update_filename(self):
        base = os.path.splitext(os.path.basename(self.video_var.get()))[0]
        if base:
            self.filename_var.set(f"{base}_rot{self.angle_var.get()}.mp4")

    def _pick_video(self):
        path = filedialog.askopenfilename(
            title="选择视频文件",
            filetypes=[("视频文件", "*.mp4 *.mkv *.avi *.mov *.webm *.flv *.ts *.m4v"),
                       ("所有文件", "*.*")])
        if path:
            self.video_var.set(path)
            self._update_filename()

    def _pick_outdir(self):
        initial = self.outdir_var.get().strip() or ""
        if not os.path.isdir(initial):
            initial = DEFAULT_DIR
        path = filedialog.askdirectory(title="选择输出文件夹", initialdir=initial)
        if path:
            self.outdir_var.set(path)

    def _validate(self):
        self.path = self.video_var.get().strip()
        self.outdir = self.outdir_var.get().strip()
        if not self.path:
            return False, "请选择视频文件"
        if not self.outdir:
            return False, "请选择输出文件夹"
        if not os.path.isfile(self.path):
            return False, "视频文件不存在"
        if not os.path.isdir(self.outdir):
            return False, f"输出文件夹不存在:\n{self.outdir}"
        self.angle = self.angle_var.get()
        self.out = os.path.join(self.outdir,
                                os.path.splitext(os.path.basename(self.path))[0]
                                + f"_rot{self.angle}.mp4")
        if os.path.exists(self.out):
            if not messagebox.askyesno("文件已存在",
                                       f"目标文件已存在，是否覆盖？\n{self.out}",
                                       parent=self.win):
                return False, "已取消（文件已存在）"
        self.app.config["rotate_dir"] = self.outdir
        _save_config(self.app.config)
        return True, None

    def _run(self):
        self.log(f"开始旋转: {self.path} ({self.angle}° 顺时针)")
        self.log(f"输出: {self.out}")
        self.start_animate()
        threading.Thread(target=self.app._rotate_worker,
                         args=(self, self.path, self.angle, self.out),
                         daemon=True).start()


class MergeTool(ToolWindowBase):
    """音视频合并：输入 + 进度 + 日志一体窗口"""

    def __init__(self, app, saved_dir):
        self.outdir_default = saved_dir or ""
        super().__init__(app, "音视频合并")
        self.video = ""
        self.audio = ""
        self.outdir = ""
        self.out = ""

    def _build_inputs(self):
        frame = self.input_frame
        ttk.Label(frame, text="视频文件:").grid(row=0, column=0, sticky="w")
        self.video_var = tk.StringVar()
        entry = ttk.Entry(frame, textvariable=self.video_var, width=40)
        entry.grid(row=0, column=1, padx=6, pady=3)
        ttk.Button(frame, text="浏览", command=self._pick_video).grid(row=0, column=2)

        ttk.Label(frame, text="音频文件:").grid(row=1, column=0, sticky="w")
        self.audio_var = tk.StringVar()
        entry = ttk.Entry(frame, textvariable=self.audio_var, width=40)
        entry.grid(row=1, column=1, padx=6, pady=3)
        ttk.Button(frame, text="浏览", command=self._pick_audio).grid(row=1, column=2)

        ttk.Label(frame, text="输出文件夹:").grid(row=2, column=0, sticky="w")
        self.outdir_var = tk.StringVar(value=self.outdir_default)
        entry = ttk.Entry(frame, textvariable=self.outdir_var, width=40)
        entry.grid(row=2, column=1, padx=6, pady=3)
        ttk.Button(frame, text="浏览", command=self._pick_outdir).grid(row=2, column=2)

        ttk.Label(frame, text="输出文件名:").grid(row=3, column=0, sticky="w")
        self.filename_var = tk.StringVar(value="")
        ttk.Label(frame, textvariable=self.filename_var,
                  foreground="#666").grid(row=3, column=1, columnspan=2, sticky="w", padx=6)

        ttk.Label(self.win, text="输出固定为 MP4 格式",
                  foreground="#888").pack(anchor="w", padx=12, pady=(0, 2))

    def _pick_video(self):
        path = filedialog.askopenfilename(
            title="选择视频文件",
            filetypes=[("视频文件", "*.mp4 *.mkv *.mov *.avi *.webm *.flv *.ts *.m4v *.mpg *.wmv"),
                       ("所有文件", "*.*")])
        if path:
            self.video_var.set(path)
            base = os.path.splitext(os.path.basename(path))[0]
            self.filename_var.set(f"{base}_merged.mp4")

    def _pick_audio(self):
        path = filedialog.askopenfilename(
            title="选择音频文件",
            filetypes=[("音频文件", "*.mp3 *.m4a *.aac *.wav *.flac *.ogg *.opus *.wma"),
                       ("所有文件", "*.*")])
        if path:
            self.audio_var.set(path)

    def _pick_outdir(self):
        initial = self.outdir_var.get().strip() or ""
        if not os.path.isdir(initial):
            initial = DEFAULT_DIR
        path = filedialog.askdirectory(title="选择输出文件夹", initialdir=initial)
        if path:
            self.outdir_var.set(path)

    def _validate(self):
        self.video = self.video_var.get().strip()
        self.audio = self.audio_var.get().strip()
        self.outdir = self.outdir_var.get().strip()
        if not self.video:
            return False, "请选择视频文件"
        if not self.audio:
            return False, "请选择音频文件"
        if not self.outdir:
            return False, "请选择输出文件夹"
        if not os.path.isfile(self.video):
            return False, "视频文件不存在"
        if not os.path.isfile(self.audio):
            return False, "音频文件不存在"
        if not os.path.isdir(self.outdir):
            return False, f"输出文件夹不存在:\n{self.outdir}"
        self.out = os.path.join(self.outdir,
                                os.path.splitext(os.path.basename(self.video))[0]
                                + "_merged.mp4")
        if os.path.exists(self.out):
            if not messagebox.askyesno("文件已存在",
                                       f"目标文件已存在，是否覆盖？\n{self.out}",
                                       parent=self.win):
                return False, "已取消（文件已存在）"
        self.app.config["merge_dir"] = self.outdir
        _save_config(self.app.config)
        return True, None

    def _run(self):
        self.log(f"开始合并:\n视频: {self.video}\n音频: {self.audio}\n输出: {self.out}")
        self.start_animate()
        threading.Thread(target=self.app._merge_worker,
                         args=(self, self.video, self.audio, self.out),
                         daemon=True).start()


class ExtractTool(ToolWindowBase):
    """音频提取：输入 + 进度 + 日志一体窗口"""

    def __init__(self, app, saved_dir):
        self.outdir_default = saved_dir or ""
        super().__init__(app, "音频提取")
        self.video = ""
        self.outdir = ""
        self.out = ""

    def _build_inputs(self):
        frame = self.input_frame
        ttk.Label(frame, text="视频文件:").grid(row=0, column=0, sticky="w")
        self.video_var = tk.StringVar()
        entry = ttk.Entry(frame, textvariable=self.video_var, width=40)
        entry.grid(row=0, column=1, padx=6, pady=3)
        ttk.Button(frame, text="浏览", command=self._pick_video).grid(row=0, column=2)

        ttk.Label(frame, text="输出文件夹:").grid(row=1, column=0, sticky="w")
        self.outdir_var = tk.StringVar(value=self.outdir_default)
        entry = ttk.Entry(frame, textvariable=self.outdir_var, width=40)
        entry.grid(row=1, column=1, padx=6, pady=3)
        ttk.Button(frame, text="浏览", command=self._pick_outdir).grid(row=1, column=2)

        ttk.Label(frame, text="输出文件名:").grid(row=2, column=0, sticky="w")
        self.filename_var = tk.StringVar(value="")
        ttk.Label(frame, textvariable=self.filename_var,
                  foreground="#666").grid(row=2, column=1, columnspan=2, sticky="w", padx=6)

        ttk.Label(self.win, text="输出为 MP3 320kbps",
                  foreground="#888").pack(anchor="w", padx=12, pady=(0, 2))

    def _pick_video(self):
        path = filedialog.askopenfilename(
            title="选择视频文件",
            filetypes=[("视频文件", "*.mp4 *.mkv *.mov *.avi *.webm *.flv *.ts *.m4v *.mpg *.wmv"),
                       ("所有文件", "*.*")])
        if path:
            self.video_var.set(path)
            base = os.path.splitext(os.path.basename(path))[0]
            self.filename_var.set(f"{base}.mp3")

    def _pick_outdir(self):
        initial = self.outdir_var.get().strip() or ""
        if not os.path.isdir(initial):
            initial = DEFAULT_DIR
        path = filedialog.askdirectory(title="选择输出文件夹", initialdir=initial)
        if path:
            self.outdir_var.set(path)

    def _validate(self):
        self.video = self.video_var.get().strip()
        self.outdir = self.outdir_var.get().strip()
        if not self.video:
            return False, "请选择视频文件"
        if not self.outdir:
            return False, "请选择输出文件夹"
        if not os.path.isfile(self.video):
            return False, "视频文件不存在"
        if not os.path.isdir(self.outdir):
            return False, f"输出文件夹不存在:\n{self.outdir}"
        self.out = os.path.join(self.outdir,
                                os.path.splitext(os.path.basename(self.video))[0] + ".mp3")
        if os.path.exists(self.out):
            if not messagebox.askyesno("文件已存在",
                                       f"目标文件已存在，是否覆盖？\n{self.out}",
                                       parent=self.win):
                return False, "已取消（文件已存在）"
        self.app.config["audio_dir"] = self.outdir
        _save_config(self.app.config)
        return True, None

    def _run(self):
        self.log(f"开始提取音频 (MP3 320kbps): {self.video}")
        self.log(f"输出: {self.out}")
        self.start_animate()
        threading.Thread(target=self.app._extract_worker,
                         args=(self, self.video, self.out),
                         daemon=True).start()


class QueueWindow:
    """下载队列窗口：每行一个待下载 URL"""

    def __init__(self, app):
        self.app = app
        self.win = tk.Toplevel(app.root)
        self.win.title("下载队列")
        self.win.geometry("580x360")
        self.win.update_idletasks()
        x = (self.win.winfo_screenwidth() - self.win.winfo_width()) // 2
        y = (self.win.winfo_screenheight() - self.win.winfo_height()) // 2
        self.win.geometry(f"+{max(x, 0)}+{max(y, 0)}")

        top = ttk.Frame(self.win)
        top.pack(fill="x", padx=8, pady=6)
        self.current_var = tk.StringVar(value="")
        ttk.Label(top, textvariable=self.current_var).pack(anchor="w")

        body = ttk.Frame(self.win)
        body.pack(fill="both", expand=True, padx=8, pady=(0, 4))
        self.listbox = tk.Listbox(body, selectmode="single", activestyle="dotbox")
        sb = ttk.Scrollbar(body, orient="vertical", command=self.listbox.yview)
        self.listbox.config(yscrollcommand=sb.set)
        self.listbox.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        ttk.Label(self.win, text="右键: 复制URL / 上移 / 下移 / 取消   快捷键: Ctrl+C / ↑ / ↓ / Delete"
                  ).pack(fill="x", padx=8, pady=(0, 6))

        self.menu = tk.Menu(self.win, tearoff=0)
        self.menu.add_command(label="复制 URL", command=self._copy_url)
        self.menu.add_command(label="上移任务", command=self._move_up)
        self.menu.add_command(label="下移任务", command=self._move_down)
        self.menu.add_separator()
        self.menu.add_command(label="取消任务", command=self._cancel)

        self.listbox.bind("<Button-3>", self._on_right_click)
        self.listbox.bind("<Control-c>", lambda e: self._copy_url())
        self.listbox.bind("<Up>", lambda e: self._move_up())
        self.listbox.bind("<Down>", lambda e: self._move_down())
        self.listbox.bind("<Delete>", lambda e: self._cancel())
        self.refresh()

    def refresh(self):
        self.listbox.delete(0, "end")
        for url in self.app.queue:
            self.listbox.insert("end", url)
        self.current_var.set(f"正在下载: {self.app.current_url}" if self.app.current_url else "空闲")

    def _index(self):
        sel = self.listbox.curselection()
        return sel[0] if sel else None

    def _on_right_click(self, event):
        idx = self.listbox.nearest(event.y)
        if 0 <= idx < self.listbox.size():
            self.listbox.selection_clear(0, "end")
            self.listbox.selection_set(idx)
            self.listbox.activate(idx)
            self.menu.tk_popup(event.x_root, event.y_root)

    def _copy_url(self):
        idx = self._index()
        if idx is None:
            return
        self.win.clipboard_clear()
        self.win.clipboard_append(self.app.queue[idx])

    def _move(self, delta):
        idx = self._index()
        if idx is None:
            return
        q = self.app.queue
        new_idx = idx + delta
        if not (0 <= new_idx < len(q)):
            return
        q[idx], q[new_idx] = q[new_idx], q[idx]
        file_logger.info(f"队列任务{'上移' if delta < 0 else '下移'}: {q[new_idx]}")
        self.refresh()
        self.listbox.selection_set(new_idx)
        self.listbox.activate(new_idx)
        self.app._update_queue_ui()

    def _move_up(self):
        self._move(-1)

    def _move_down(self):
        self._move(1)

    def _cancel(self):
        idx = self._index()
        if idx is None:
            return
        url = self.app.queue[idx]
        if not messagebox.askyesno("取消任务", f"确定取消该任务？\n{url}", parent=self.win):
            return
        del self.app.queue[idx]
        file_logger.info(f"队列任务已取消: {url}")
        self.app._log(f"已取消任务: {url}")
        self.app._update_queue_ui()
        self.refresh()


class VideoDownloaderApp:
    def __init__(self, root):
        self.root = root
        self.root.title("视频下载器")
        self.root.geometry("640x360")
        self.root.resizable(False, False)

        self.config = _load_config()
        saved_dir = self.config.get("download_dir") or ""
        if not os.path.isdir(saved_dir):
            saved_dir = DEFAULT_DIR

        self.downloading = False
        self.total_bytes = 0
        self.downloaded_bytes = 0
        self.speed = 0.0
        self.bar_animated = False
        self.last_browser_ok = None
        self.queue = []
        self.current_url = None
        self.queue_win = None
        self.dir_var = tk.StringVar(value=saved_dir)

        self._build_ui()
        self._update_queue_ui()
        self._center_window()

    def _center_window(self):
        self.root.update_idletasks()
        x = (self.root.winfo_screenwidth() - self.root.winfo_width()) // 2
        y = (self.root.winfo_screenheight() - self.root.winfo_height()) // 2
        self.root.geometry(f"+{max(x, 0)}+{max(y, 0)}")

    def _build_ui(self):
        menubar = tk.Menu(self.root)
        video_menu = tk.Menu(menubar, tearoff=0)
        video_menu.add_command(label="视频旋转...", command=self._rotate_video)
        video_menu.add_command(label="音视频合并...", command=self._merge_video_audio)
        menubar.add_cascade(label="视频", menu=video_menu)
        audio_menu = tk.Menu(menubar, tearoff=0)
        audio_menu.add_command(label="音频提取...", command=self._extract_audio)
        menubar.add_cascade(label="音频", menu=audio_menu)
        self.root.config(menu=menubar)

        pad = {"padx": 10, "pady": 6}

        row = ttk.Frame(self.root)
        row.pack(fill="x", **pad)

        ttk.Label(row, text="视频 URL:").pack(side="left")
        self.url_var = tk.StringVar()
        url_entry = ttk.Entry(row, textvariable=self.url_var)
        url_entry.pack(side="left", fill="x", expand=True, padx=(6, 0))
        url_entry.bind("<Return>", lambda e: self._add_to_queue())

        row2 = ttk.Frame(self.root)
        row2.pack(fill="x", **pad)

        ttk.Label(row2, text="保存目录:").pack(side="left")
        dir_entry = ttk.Entry(row2, textvariable=self.dir_var)
        dir_entry.pack(side="left", fill="x", expand=True, padx=(6, 0))
        ttk.Button(row2, text="浏览", command=self._choose_dir).pack(side="left", padx=(6, 0))

        btn_row = ttk.Frame(self.root)
        btn_row.pack(pady=(4, 0))
        self.download_btn = ttk.Button(btn_row, text="开始下载", command=self._add_to_queue)
        self.download_btn.pack(side="left", padx=4)
        self.resume_btn = ttk.Button(btn_row, text="继续下载", command=self._add_to_queue,
                                     state="disabled")
        self.resume_btn.pack(side="left", padx=4)
        self.queue_btn = ttk.Button(btn_row, text="下载队列 (0)", command=self._open_queue)
        self.queue_btn.pack(side="left", padx=4)

        self.progress = ttk.Progressbar(self.root, mode="determinate")
        self.progress.pack(fill="x", padx=10, pady=12)

        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(self.root, textvariable=self.status_var).pack(anchor="w", padx=10)

        self.log_text = tk.Text(self.root, height=8, state="disabled")
        self.log_text.pack(fill="both", expand=True, padx=10, pady=(6, 10))

    # ---------- 队列 ----------

    def _update_queue_ui(self):
        self.queue_btn.config(text=f"下载队列 ({len(self.queue)})")
        if self.queue_win is not None and self.queue_win.winfo_exists():
            self.queue_win.refresh()

    def _open_queue(self):
        if self.queue_win is not None and self.queue_win.winfo_exists():
            self.queue_win.win.lift()
            self.queue_win.refresh()
            return
        self.queue_win = QueueWindow(self)

    def _add_to_queue(self):
        url = self.url_var.get().strip()
        if not url:
            messagebox.showwarning("提示", "请输入视频 URL")
            return
        if not self.dir_var.get().strip():
            messagebox.showwarning("提示", "请选择保存目录")
            return
        self.url_var.set("")
        self.queue.append(url)
        if self.downloading:
            self._log(f"已加入队列({len(self.queue)}): {url}")
        else:
            self._log(f"加入队列，即将开始: {url}")
        self._update_queue_ui()
        self._pump_queue()

    def _pump_queue(self):
        if self.downloading:
            return
        if not self.queue:
            self.status_var.set("就绪")
            self._update_queue_ui()
            return
        url = self.queue.pop(0)
        self._update_queue_ui()
        self._start_download(url)

    # ---------- 下载 ----------

    def _choose_dir(self):
        path = filedialog.askdirectory(initialdir=self.dir_var.get() or DEFAULT_DIR)
        if path:
            self.dir_var.set(path)
            self.config["download_dir"] = path
            _save_config(self.config)

    def _remember_dir(self):
        outdir = self.dir_var.get().strip()
        if outdir and os.path.isdir(outdir):
            self.config["download_dir"] = outdir
            _save_config(self.config)

    def _log(self, msg, level="INFO"):
        file_logger.write(level, msg)
        self.log_text.config(state="normal")
        self.log_text.insert("end", msg + "\n")
        self.log_text.see("end")
        self.log_text.config(state="disabled")

    def _progress_hook(self, d):
        self._schedule_ui(self._on_progress_ui, d)

    def _on_progress_ui(self, d):
        if d["status"] == "downloading":
            if self.bar_animated:
                self.bar_animated = False
                self.progress.stop()
                self.progress.config(mode="determinate", value=0)
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            downloaded = d.get("downloaded_bytes", 0)
            self.total_bytes = total
            self.downloaded_bytes = downloaded
            self.speed = d.get("speed") or 0.0
            self._update_progress()
        elif d["status"] == "finished":
            if not self.bar_animated:
                self.bar_animated = True
                self.progress.config(mode="indeterminate")
                self.progress.start(12)
            self.status_var.set("合并音视频中...")
        elif d["status"] == "error":
            self.status_var.set("下载出错")

    @staticmethod
    def _fmt_speed(speed):
        if speed >= 1024 * 1024:
            return f"{speed / 1024 / 1024:.1f} MB/s"
        if speed >= 1024:
            return f"{speed / 1024:.0f} KB/s"
        return f"{speed:.0f} B/s"

    def _update_progress(self):
        speed_txt = self._fmt_speed(self.speed) if self.speed > 0 else "计算中..."
        if self.total_bytes > 0:
            pct = min(self.downloaded_bytes / self.total_bytes, 1.0) * 100
            self.progress["value"] = pct
            mb = self.downloaded_bytes / 1024 / 1024
            total_mb = self.total_bytes / 1024 / 1024
            self.status_var.set(
                f"下载中 {pct:.1f}%  ({mb:.1f} MB / {total_mb:.1f} MB)  速度: {speed_txt}")
        else:
            self.status_var.set(
                f"下载中 {self.downloaded_bytes / 1024 / 1024:.1f} MB ...  速度: {speed_txt}")

    def _on_finish(self, success, msg):
        self.downloading = False
        self.current_url = None
        self.download_btn.config(state="normal")
        if self.bar_animated:
            self.bar_animated = False
            self.progress.stop()
            self.progress.config(mode="determinate")
        if success:
            self.url_var.set("")
            self.resume_btn.config(state="disabled")
            self.progress["value"] = 100
            self.status_var.set("下载完成")
            self._log(msg)
            self._remember_dir()
            if self.last_browser_ok:
                self.config["cookies_browser"] = self.last_browser_ok
                _save_config(self.config)
                self._log(f"已记住 cookie 来源浏览器: {self.last_browser_ok}")
        else:
            self.resume_btn.config(state="normal")
            self.progress["value"] = 0
            self.status_var.set("下载失败")
            self._log(msg, "ERROR")
            self._log("可点击「继续下载」重试，将自动断点续传")
        self._update_queue_ui()
        self._pump_queue()

    # ---------- cookie ----------

    AUTH_HINT_KEYWORDS = (
        "sign in to confirm", "not a bot", "cookies-from-browser",
        "login required", "login", "authentication", "cookies",
        "no cookies", "unable to extract cookies",
        "failed to decrypt", "dpapi", "could not copy",
        "cookie database", "is locked", "locked by the browser",
    )
    BROWSER_CANDIDATES = (
        ("chrome", (r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe")),
        ("edge", (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                  r"C:\Program Files\Microsoft\Edge\Application\msedge.exe")),
        ("firefox", (r"C:\Program Files\Mozilla Firefox\firefox.exe",
                     r"C:\Program Files (x86)\Mozilla Firefox\firefox.exe")),
        ("brave", (r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",)),
        ("opera", (r"C:\Program Files\Opera\launcher.exe",)),
    )

    @staticmethod
    def _detect_browser():
        for name, paths in VideoDownloaderApp.BROWSER_CANDIDATES:
            if any(os.path.isfile(p) for p in paths) or shutil.which(name):
                return name
        return None

    @staticmethod
    def _is_browser_running(name):
        exe = {"chrome": "chrome.exe", "edge": "msedge.exe", "firefox": "firefox.exe",
               "brave": "brave.exe", "opera": "opera.exe"}.get(name)
        if not exe:
            return False
        try:
            out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {exe}"],
                                 capture_output=True, text=True, timeout=10)
            return exe.lower() in out.stdout.lower()
        except (OSError, subprocess.TimeoutExpired):
            return False

    def _default_cookies_path(self):
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "cookies.txt")

    def _update_cookies(self, src):
        target = self._default_cookies_path()
        try:
            shutil.copyfile(src, target)
        except OSError as e:
            return None, str(e)
        return target, None

    @staticmethod
    def _is_auth_error(err):
        msg = str(err).lower()
        return any(k in msg for k in VideoDownloaderApp.AUTH_HINT_KEYWORDS)

    def _download_worker(self, url, outdir, cookies=None, browser=None,
                         tried_browsers=None, auth_stage=0, cdp_tried=False):
        output_template = os.path.join(outdir, "%(title)s [%(height)sp].%(ext)s")
        opts = {
            "outtmpl": output_template,
            "format": "bestvideo+bestaudio/best",
            "merge_output_format": "mp4",
            "noplaylist": True,
            "continuedl": True,
            "concurrent_fragment_downloads": CONCURRENT_FRAGMENTS,
            "progress_hooks": [self._progress_hook],
            "socket_timeout": 30,
            "retries": 5,
            "quiet": True,
            "no_warnings": True,
        }
        aria2c = _find_aria2c()
        if aria2c:
            opts["external_downloader"] = "aria2c"
            opts["external_downloader_args"] = {"aria2c": ["-x", "16", "-s", "16", "-k", "1M"]}
        if browser:
            opts["cookiesfrombrowser"] = (browser,)
        elif cookies:
            opts["cookiefile"] = cookies
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
            title = info.get("title", url)
            height = info.get("height")
            if browser:
                self.last_browser_ok = browser
            self._schedule_ui(self._on_finish, True,
                              f"已下载: {title}"
                              + (f" ({height}p)" if height else "")
                              + f"\n保存至: {outdir}")
        except Exception as e:
            if self._is_auth_error(e):
                tried = list(tried_browsers or [])
                if browser:
                    tried.append(browser)
                if auth_stage in (0, 1):
                    nxt = self._next_browser(tried)
                    if nxt:
                        self._schedule_ui(self._auto_retry_browser,
                                          url, outdir, nxt, str(e), tried + [nxt], 1)
                    else:
                        self._schedule_ui(self._after_browsers,
                                          url, outdir, str(e), cdp_tried)
                else:
                    self._schedule_ui(self._give_up_auth, url, str(e))
            else:
                self._schedule_ui(self._on_finish, False, f"错误: {e}")

    @staticmethod
    def _next_browser(tried):
        """返回下一个已安装、未尝试过、且当前未运行的浏览器"""
        for name, paths in VideoDownloaderApp.BROWSER_CANDIDATES:
            if name in tried:
                continue
            if not (any(os.path.isfile(p) for p in paths) or shutil.which(name)):
                continue
            if VideoDownloaderApp._is_browser_running(name):
                continue
            return name
        return None

    def _auto_retry_browser(self, url, outdir, browser, err_msg, tried, stage):
        lower = err_msg.lower()
        if "failed to decrypt" in lower or "dpapi" in lower:
            self._log(f"浏览器 {browser} 使用新版 App-Bound 加密，yt-dlp 无法自动解密其 cookie",
                      "WARNING")
            self._log("提示: 安装 Firefox 并登录该网站，即可实现自动获取 cookie", "WARNING")
        elif self._is_browser_running(browser):
            self._log(f"浏览器 {browser} 正在运行，cookie 数据库被锁定，跳过", "WARNING")
        else:
            self._log(f"尝试从浏览器 {browser} 自动获取 cookie", "WARNING")
        self.status_var.set(f"从 {browser} 自动获取 cookie...")
        threading.Thread(target=self._download_worker,
                         args=(url, outdir, None, browser, tried, stage),
                         daemon=True).start()

    def _after_browsers(self, url, outdir, err_msg, cdp_tried):
        """浏览器全部失败后: 优先 Chrome CDP 自动获取, 否则手动选择"""
        if (not cdp_tried and _find_chrome() and websocket is not None
                and self.config.get("cookies_cdp", True)):
            self._auto_retry_cdp(url, outdir, err_msg)
        else:
            self._ask_cookie_retry(url, outdir, err_msg, 2)

    def _auto_retry_cdp(self, url, outdir, err_msg):
        self.status_var.set("通过 Chrome 自动获取 cookie...")
        self._log("尝试通过 Chrome (CDP) 自动获取 cookie", "WARNING")
        if not self.config.get("cdp_logged_in"):
            self._log("首次使用: 将弹出 Chrome 窗口，请在其中登录 YouTube 一次，"
                      "之后即可全自动更新 cookie", "WARNING")
        threading.Thread(target=self._cdp_worker,
                         args=(url, outdir, err_msg), daemon=True).start()

    def _cdp_worker(self, url, outdir, err_msg):
        try:
            fetcher = ChromeCookieFetcher()
            ok, msg = fetcher.fetch(
                on_need_login=lambda: self._schedule_ui(
                    self._log,
                    "请在弹出的 Chrome 窗口中登录 YouTube，"
                    "登录完成后关闭该窗口，程序将自动继续...",
                    "WARNING"))
            if not ok:
                self._schedule_ui(self._ask_cookie_retry,
                                  url, outdir,
                                  f"{err_msg}\n(Chrome 自动获取失败: {msg})", 2)
                return
            self.config["cdp_logged_in"] = True
            _save_config(self.config)
            self._schedule_ui(self._log, f"已通过 Chrome 自动获取 cookie: {fetcher.target}")
            self._schedule_ui(self._download_worker,
                              url, outdir, fetcher.target, None, [], 2, True)
        except Exception as e:
            file_logger.error(f"CDP cookie 获取异常: {e}")
            self._schedule_ui(self._ask_cookie_retry,
                              url, outdir,
                              f"{err_msg}\n(Chrome 自动获取异常: {e})", 2)

    def _give_up_auth(self, url, err_msg):
        self._log("浏览器 cookie 与手动 cookie 均尝试失败", "ERROR")
        messagebox.showwarning(
            "自动获取失败",
            f"已自动尝试浏览器 cookie，仍无法通过验证:\n\n{err_msg}\n\n"
            "原因可能:\n"
            "1. Chrome CDP 自动获取时浏览器未登录\n"
            "2. 新版 Chrome/Edge 使用 App-Bound 加密，yt-dlp 无法直接读取\n\n"
            "建议: 用 Chrome 打开 YouTube 并登录后重试（程序会自动获取 cookie）；\n"
            "或手动选择 cookie 文件。")
        self._on_finish(False, f"错误: {err_msg}")

    def _ask_cookie_retry(self, url, outdir, err_msg, auth_stage=2):
        self.status_var.set("需要登录验证 (cookie)")
        self._log(f"需要登录验证，请求选择 cookie: {url}", "WARNING")
        choice = messagebox.askyesno(
            "需要登录验证",
            f"视频需要登录/验证，当前 cookie 无效或缺失:\n\n{err_msg}\n\n"
            "是否选择 cookie 文件后重试？"
            "\n(可从浏览器导出 Netscape 格式 cookies.txt)")
        if not choice:
            self._on_finish(False, f"错误: {err_msg}")
            return
        saved = self.config.get("cookie_dir") or os.path.dirname(os.path.abspath(__file__))
        if not os.path.isdir(saved):
            saved = os.path.dirname(os.path.abspath(__file__))
        path = filedialog.askopenfilename(
            title="选择 cookie 文件 (Netscape 格式)",
            filetypes=[("Cookie 文件", "*.txt"), ("所有文件", "*.*")],
            initialdir=saved)
        if not path:
            self._on_finish(False, f"错误: {err_msg}")
            return
        self.config["cookie_dir"] = os.path.dirname(path)
        _save_config(self.config)
        target, copy_err = self._update_cookies(path)
        if copy_err:
            self._log(f"复制 cookie 失败: {copy_err}", "ERROR")
            self._on_finish(False, f"错误: 复制 cookie 失败: {copy_err}")
            return
        self._log(f"已更新 cookie 文件: {target}")
        threading.Thread(target=self._download_worker,
                         args=(url, outdir, target, None, [], auth_stage), daemon=True).start()

    def _schedule_ui(self, func, *args):
        self.root.after(0, lambda: func(*args))

    # ---------- 视频旋转 ----------

    def _rotate_video(self):
        if not _find_ffmpeg():
            messagebox.showerror("错误", "未找到 ffmpeg，请先安装:\nwinget install -e --id Gyan.FFmpeg")
            return
        saved = self.config.get("rotate_dir") or ""
        RotationTool(self, saved)

    def _probe_duration(self, path):
        ffprobe = _find_ffprobe()
        if not ffprobe:
            return 0.0
        try:
            out = subprocess.run(
                [ffprobe, "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", path],
                capture_output=True, text=True, timeout=30)
            return float(out.stdout.strip())
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return 0.0

    def _run_rotate(self, cmd, duration, out, tw):
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL,
                                    text=True, encoding="utf-8", errors="replace")
            for line in proc.stdout:
                if line.startswith("out_time_ms="):
                    try:
                        t = int(line.strip().split("=")[1]) / 1e6
                    except ValueError:
                        continue
                    if duration > 0:
                        pct = min(t / duration, 1.0) * 100
                        tw._schedule(tw.update_progress, pct)
            proc.wait()
        except OSError as e:
            return False, f"无法执行 ffmpeg: {e}"
        if proc.returncode != 0 or not os.path.isfile(out):
            return False, "ffmpeg 返回错误"
        return True, ""

    def _rotate_worker(self, tw, path, angle, out):
        ffmpeg = _find_ffmpeg()
        vf = {90: "transpose=1", 180: "vflip,hflip", 270: "transpose=2"}[angle]
        duration = self._probe_duration(path)

        hws = _available_hw_encoders()
        hw = next((e for e in ("h264_nvenc", "h264_qsv", "h264_amf") if e in hws), None)
        presets = {"h264_nvenc": ["-preset", "p4"],
                   "h264_qsv": ["-preset", "veryfast"],
                   "h264_amf": ["-preset", "balanced"]}
        if hw:
            vc = [hw] + presets[hw] + ["-cq", "20"]
            tw.log(f"使用硬件编码: {hw}")
        else:
            vc = ["libx264", "-preset", "veryfast", "-crf", "20"]
            tw.log("使用软件编码: libx264 (veryfast)")

        cmd = ([ffmpeg, "-y", "-hwaccel", "auto", "-i", path, "-vf", vf,
                "-c:v"] + vc + ["-pix_fmt", "yuv420p", "-c:a", "copy",
                                "-progress", "pipe:1", "-nostats", out])
        ok, err = self._run_rotate(cmd, duration, out, tw)
        if not ok and hw:
            tw.log("硬件编码失败，改用软件编码重试...", "WARNING")
            vc = ["libx264", "-preset", "veryfast", "-crf", "20"]
            cmd = ([ffmpeg, "-y", "-hwaccel", "auto", "-i", path, "-vf", vf,
                    "-c:v"] + vc + ["-pix_fmt", "yuv420p", "-c:a", "copy",
                                    "-progress", "pipe:1", "-nostats", out])
            ok, err = self._run_rotate(cmd, duration, out, tw)
        if ok:
            tw._schedule(tw.finish, True, f"旋转完成: {out}")
        else:
            tw._schedule(tw.finish, False, f"旋转失败: {err}\n{path}")

    # ---------- 音视频合并 ----------

    def _merge_video_audio(self):
        if not _find_ffmpeg():
            messagebox.showerror("错误", "未找到 ffmpeg，请先安装:\nwinget install -e --id Gyan.FFmpeg")
            return
        saved = self.config.get("merge_dir") or ""
        MergeTool(self, saved)

    def _merge_worker(self, tw, video, audio, out):
        ffmpeg = _find_ffmpeg()
        duration = self._probe_duration(video)

        def run(cmd):
            if os.path.exists(out):
                try:
                    os.remove(out)
                except OSError:
                    pass
            return self._run_rotate(cmd, duration, out, tw)

        # 1) 双流 stream copy（零转码，最快）
        ok, err = run([ffmpeg, "-y", "-i", video, "-i", audio,
                       "-c:v", "copy", "-c:a", "copy",
                       "-progress", "pipe:1", "-nostats", out])
        if not ok:
            # 2) 视频流复制 + 音频转码 AAC
            tw.log("音频格式与 MP4 容器不兼容，转码音频为 AAC (视频仍为流复制)...")
            ok, err = run([ffmpeg, "-y", "-i", video, "-i", audio,
                           "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                           "-progress", "pipe:1", "-nostats", out])
        if not ok:
            # 3) 视频流也不兼容 MP4，整段转码（硬件编码优先）
            tw.log("视频流与 MP4 容器不兼容，转码视频 (硬件编码优先)...")
            hws = _available_hw_encoders()
            hw = next((e for e in ("h264_nvenc", "h264_qsv", "h264_amf") if e in hws), None)
            presets = {"h264_nvenc": ["-preset", "p4"],
                       "h264_qsv": ["-preset", "veryfast"],
                       "h264_amf": ["-preset", "balanced"]}
            if hw:
                vc = [hw] + presets[hw] + ["-cq", "20"]
                tw.log(f"使用硬件编码: {hw}")
            else:
                vc = ["libx264", "-preset", "veryfast", "-crf", "20"]
                tw.log("使用软件编码: libx264 (veryfast)")
            ok, err = run([ffmpeg, "-y", "-i", video, "-i", audio,
                           "-c:v"] + vc + ["-pix_fmt", "yuv420p",
                           "-c:a", "aac", "-b:a", "192k",
                           "-progress", "pipe:1", "-nostats", out])
        if ok:
            tw._schedule(tw.finish, True, f"合并完成: {out}")
        else:
            tw._schedule(tw.finish, False,
                         f"合并失败: {err}\n视频: {video}\n音频: {audio}")

    # ---------- 音频提取 ----------

    def _extract_audio(self):
        if not _find_ffmpeg():
            messagebox.showerror("错误", "未找到 ffmpeg，请先安装:\nwinget install -e --id Gyan.FFmpeg")
            return
        saved = self.config.get("audio_dir") or ""
        ExtractTool(self, saved)

    def _extract_worker(self, tw, video, out):
        ffmpeg = _find_ffmpeg()
        duration = self._probe_duration(video)
        cmd = [ffmpeg, "-y", "-i", video, "-vn",
               "-c:a", "libmp3lame", "-b:a", "320k",
               "-progress", "pipe:1", "-nostats", out]
        ok, err = self._run_rotate(cmd, duration, out, tw)
        if ok:
            tw._schedule(tw.finish, True, f"音频提取完成: {out}")
        else:
            tw._schedule(tw.finish, False,
                         f"音频提取失败: {err}\n视频: {video}")

    def _start_download(self, url):
        outdir = self.dir_var.get().strip()
        try:
            os.makedirs(outdir, exist_ok=True)
        except OSError as e:
            self._log(f"无法创建目录: {e}", "ERROR")
            self._on_finish(False, f"错误: 无法创建目录: {e}")
            return

        self.downloading = True
        self.current_url = url
        self.resume_btn.config(state="disabled")
        self.progress.stop()
        self.progress.config(mode="indeterminate")
        self.progress.start(12)
        self.bar_animated = True
        self.total_bytes = 0
        self.downloaded_bytes = 0
        self.status_var.set("分析视频信息中...")
        self._log(f"开始下载: {url}")
        self._log(f"保存目录: {outdir}")
        self.last_browser_ok = None
        browser = self.config.get("cookies_browser") or ""
        cookies = self._default_cookies_path()
        if browser and self._detect_browser() is not None:
            self._log(f"使用浏览器 cookie: {browser}")
            cookies = None
        elif os.path.isfile(cookies):
            self._log(f"已加载 cookie: {cookies}")
        else:
            cookies = None

        try:
            parts = [f for f in os.listdir(outdir) if f.endswith(".part")]
        except OSError:
            parts = []
        if parts:
            self._log(f"发现 {len(parts)} 个未完成的分片文件，将自动断点续传")

        threading.Thread(target=self._download_worker,
                         args=(url, outdir, cookies, browser or None), daemon=True).start()


def main():
    file_logger.info(f"程序启动 (yt-dlp {yt_dlp.version.__version__})")
    root = tk.Tk()
    VideoDownloaderApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
