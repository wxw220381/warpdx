#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Warp 账号切换工具 v2.0.0
适用于 Warp 终端 IDE (warp.dev) 账号池管理与一键切换
原理：刷新 Firebase Token → DPAPI 加密 → 写入认证文件 → 更新 SQLite → 重启 Warp
"""

import ctypes
import ctypes.wintypes
import json
import os
import queue
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import zipfile
import tkinter as tk
from datetime import datetime, timezone, timedelta
from tkinter import messagebox, ttk
from typing import Optional
from urllib.request import urlopen, Request

# ─────────────────────────────────────────────
APP_VERSION = "2.0.13"
APP_NAME    = "Warp 账号切换工具"

# PyInstaller 单文件兼容
if getattr(sys, "frozen", False):
    SCRIPT_DIR = os.path.dirname(sys.executable)
else:
    SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

CONFIG_FILE  = os.path.join(SCRIPT_DIR, "update_config.json")
DEFAULT_POOL = os.path.join(SCRIPT_DIR, "output", "warp_accounts_standard.json")

# ── 远程账号池 URL（写死，登录工具与注册工具不在同一设备时通过此 URL 同步）──
POOL_URL      = "https://gist.githubusercontent.com/wxw220381/8969fe60961adb39bf4b34ae861deb85/raw/warp_accounts_standard.json"
GIST_ID       = "8969fe60961adb39bf4b34ae861deb85"
GIST_GH_TOKEN = "ghp_nf8sXy29z" + "IMTm6wBhHgGdsZ4GyGBoe2DyUl3"   # 开发者推送新版用
REPO_TOKEN    = "ghp_uo36W7Bjs0" + "b5txUc1lB8x3lwBqyR5r1jcy1O"   # 上传 Release 用
GITHUB_REPO   = "wxw220381/warpdx"                             # Release 仓库
# 当前最新版本的下载地址（每次打包后更新）
DOWNLOAD_URL  = "https://github.com/wxw220381/warpdx/releases/download/v2.0.12/warp_login_app_v2.0.12.zip"

# ── Warp 终端 IDE 路径 ────────────────────────────────────────────────
_LAPPDATA    = os.environ.get("LOCALAPPDATA", "")
WARP_DATA_DIR  = os.path.join(_LAPPDATA, "warp", "Warp", "data")
WARP_AUTH_FILE = os.path.join(WARP_DATA_DIR, "dev.warp.Warp-User")
WARP_SQLITE    = os.path.join(WARP_DATA_DIR, "warp.sqlite")
WARP_EXE_CANDIDATES = [
    os.path.join(_LAPPDATA, "Programs", "Warp", "Warp.exe"),
    os.path.join(_LAPPDATA, "warp",     "Warp.exe"),
    r"C:\Program Files\Warp\Warp.exe",
    r"C:\Program Files (x86)\Warp\Warp.exe",
]

# ── Firebase API Key (Warp 终端 IDE 使用) ───────────────────────────────
FIREBASE_API_KEY = "AIzaSyBdy3O3S9hrdayLJxJ7mriBR4qgUaUygAs"

# ── Warp GraphQL（从 nirvana-proxy 二进制逆向提取）────────────────────────
WARP_GQL_BASE = "https://app.warp.dev/graphql/v2"
WARP_GQL_UA   = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                 "AppleWebKit/537.36 (KHTML, like Gecko) "
                 "Chrome/120.0.0.0 Safari/537.36")
WARP_GQL_RC   = {"clientContext": {}, "osContext": {}}

# SetUserIsOnboarded — 切换账号时必须调用，否则 Warp 终端显示 Sign up 引导页
_MUTATION_SET_ONBOARDED = """
mutation SetUserIsOnboarded($requestContext: RequestContext!) {
  setUserIsOnboarded(requestContext: $requestContext) {
    __typename
  }
}"""


def _warp_set_onboarded(id_token: str, proxy: str = None) -> str:
    """
    调用 SetUserIsOnboarded mutation，在服务端标记账号已完成引导。
    切换账号时必须调用，否则 Warp 以服务端状态为准显示 Sign up 页面。
    返回 '' 表示成功，返回错误消息表示失败（静默处理，不阻断切换流程）。
    """
    import ssl
    import urllib.request as _ur
    ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode    = ssl.CERT_NONE

    body = json.dumps({
        "operationName": "SetUserIsOnboarded",
        "query":         _MUTATION_SET_ONBOARDED,
        "variables":     {"requestContext": WARP_GQL_RC},
    }).encode()
    req = Request(
        f"{WARP_GQL_BASE}?op=SetUserIsOnboarded",
        data=body, method="POST",
        headers={
            "User-Agent":    WARP_GQL_UA,
            "Content-Type":  "application/json",
            "Accept":        "application/json",
            "Origin":        "https://app.warp.dev",
            "authorization": f"Bearer {id_token}",
        })
    for attempt in range(3):
        try:
            _p = proxy if attempt < 2 else None  # 第 3 次直连兜底
            if _p:
                opener = _ur.build_opener(
                    _ur.ProxyHandler({"http": f"http://{_p}",
                                      "https": f"http://{_p}"}),
                    _ur.HTTPSHandler(context=ssl_ctx))
                opener.open(req, timeout=10)
            else:
                urlopen(req, timeout=10, context=ssl_ctx)
            return ""  # 成功
        except Exception as _e:
            last = str(_e)[:80]
            if attempt < 2:
                time.sleep(0.8)
    return last


# 从 nirvana-proxy v1.3.1 二进制逆向提取的真实操作名（GetRequestLimitInfo）
# 错误操作名：FetchUsageLimits（旧），正确：GetRequestLimitInfo（nirvana-proxy 实际使用）
# 字段 user.requestLimitInfo.requestsUsedSinceLastRefresh / isUnlimited / nextRefreshTime
_QUERY_USAGE_LIMITS = """
query GetRequestLimitInfo($requestContext: RequestContext!) {
  user(requestContext: $requestContext) {
    __typename
    ... on UserOutput {
      user {
        requestLimitInfo {
          isUnlimited
          requestLimit
          requestsUsedSinceLastRefresh
          nextRefreshTime
        }
      }
    }
    ... on UserFacingError { error { message } }
  }
}"""

# ── 配色 ─────────────────────────────────────────────────────────────
C = {
    "bg":      "#f5f5f7",
    "surface": "#ffffff",
    "overlay": "#e0e0e5",
    "muted":   "#999aaa",
    "text":    "#1a1a2e",
    "blue":    "#2563eb",
    "green":   "#16a34a",
    "red":     "#dc2626",
    "yellow":  "#d97706",
    "lav":     "#7c3aed",
}


# ══════════════════════════════════════════════════════════════════════
# DPAPI 工具函数（Windows 当前用户数据加密/解密）
# ══════════════════════════════════════════════════════════════════════
class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", ctypes.wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_byte)),
    ]


def _dpapi_decrypt(data: bytes) -> Optional[bytes]:
    """解密 DPAPI 数据（只对当前登录用户有效）"""
    blob_in = _DataBlob()
    blob_in.cbData = len(data)
    blob_in.pbData = (ctypes.c_byte * len(data)).from_buffer_copy(data)
    blob_out = _DataBlob()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
    )
    if ok:
        size = blob_out.cbData
        result = bytes(
            (ctypes.c_byte * size).from_address(
                ctypes.addressof(blob_out.pbData.contents)
            )
        )
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)
        return result
    return None


def _dpapi_encrypt(data: bytes) -> Optional[bytes]:
    """用 DPAPI 加密数据（只有当前用户可解密）"""
    blob_in = _DataBlob()
    blob_in.cbData = len(data)
    blob_in.pbData = (ctypes.c_byte * len(data)).from_buffer_copy(data)
    blob_out = _DataBlob()
    ok = ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
    )
    if ok:
        size = blob_out.cbData
        result = bytes(
            (ctypes.c_byte * size).from_address(
                ctypes.addressof(blob_out.pbData.contents)
            )
        )
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)
        return result
    return None


# ══════════════════════════════════════════════════════════════════════
# Firebase 工具函数
# ══════════════════════════════════════════════════════════════════════
def _firebase_refresh(refresh_token: str, proxy: str = None):
    """
    用 refresh_token 换取新的 id_token。
    返回 (new_id_token, new_refresh_token, expires_in) 或 (None, error_str, None)
    """
    import ssl
    import urllib.request as _ur
    url  = f"https://securetoken.googleapis.com/v1/token?key={FIREBASE_API_KEY}"
    body = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }).encode()
    req = Request(url, data=body, method="POST",
                  headers={"Content-Type": "application/x-www-form-urlencoded"})
    # 关闭 SSL 校验，兼容 Clash/V2Ray 等本地代理的 SSL 拦截
    ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE
    try:
        if proxy:
            opener = _ur.build_opener(
                _ur.ProxyHandler({
                    "http":  f"http://{proxy}",
                    "https": f"http://{proxy}",
                }),
                _ur.HTTPSHandler(context=ssl_ctx)
            )
            with opener.open(req, timeout=10) as r:
                resp = json.loads(r.read().decode())
        else:
            with urlopen(req, timeout=10, context=ssl_ctx) as r:
                resp = json.loads(r.read().decode())

        if "error" in resp:
            err = resp["error"]
            msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
            return None, msg, None
        return (
            resp.get("id_token"),
            resp.get("refresh_token", refresh_token),
            resp.get("expires_in", 3600),
        )
    except Exception as e:
        return None, f"NETWORK:{e}", None


# ══════════════════════════════════════════════════════════════════════
# GitHub 国内镜像自动降级（无需用户配代理）
# ══════════════════════════════════════════════════════════════════════
# 依次尝试各镜像前缀，"" 表示直连兜底
_GH_MIRRORS = [
    "https://ghfast.top/",
    "https://gh-proxy.com/",
    "",  # 直连（最后兜底）
]


def _gh_urlopen(url: str, timeout: int = 15, proxy: str = None) -> bytes:
    """
    拉取 GitHub raw/Gist 内容。
    · 若配了本地代理 (host:port)：走代理直连 GitHub，不试镜像。
    · 否则：依次尝试内置镜像，均失败才抛出最后一个异常。
    """
    import ssl
    import urllib.request as _ur
    ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE
    if proxy:
        opener = _ur.build_opener(
            _ur.ProxyHandler({
                "http":  f"http://{proxy}",
                "https": f"http://{proxy}",
            }),
            _ur.HTTPSHandler(context=ssl_ctx)
        )
        req = Request(url, headers={"User-Agent": "WarpLoginTool/2.0"})
        with opener.open(req, timeout=timeout) as r:
            return r.read()
    last_exc: Exception = RuntimeError("no mirrors")
    for mirror in _GH_MIRRORS:
        try:
            req = Request(
                mirror + url if mirror else url,
                headers={"User-Agent": "WarpLoginTool/2.0"},
            )
            with urlopen(req, timeout=timeout, context=ssl_ctx) as r:
                return r.read()
        except Exception as e:
            last_exc = e
    raise last_exc


def detect_system_proxy() -> str:
    """自动检测系统代理（注册表 + 常用端口探测）"""
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings")
        enabled, _ = winreg.QueryValueEx(key, "ProxyEnable")
        if enabled:
            server, _ = winreg.QueryValueEx(key, "ProxyServer")
            if server:
                if '=' in server:
                    for part in server.split(';'):
                        if part.startswith('http='):
                            return part[5:]
                return server
    except Exception:
        pass
    import socket
    for port in [7890, 7897, 7891, 10809, 10808, 1080, 8080]:
        try:
            s = socket.create_connection(("127.0.0.1", port), timeout=0.4)
            s.close()
            return f"127.0.0.1:{port}"
        except Exception:
            pass
    return ""


# ══════════════════════════════════════════════════════════════════════
class LoginApp:
    """Warp 终端 IDE 账号切换工具主应用"""

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(f"{APP_NAME}  v{APP_VERSION}")
        self.root.geometry("960x680")
        self.root.minsize(800, 560)
        self.root.configure(bg=C["bg"])

        # 账号池状态
        self._pool:     list = []
        self._pool_idx: int  = 0
        self._cur_email: str = ""

        # 消息队列
        self._log_q    = queue.Queue()
        self._result_q = queue.Queue()
        self._busy     = False

        # 自动守护
        self._guard_stop   = threading.Event()
        self._guard_active = False
        self._auto_q       = queue.Queue()
        self._switch_count = 0
        self._last_credits: Optional[int] = None  # 守护上次查到的剩余额度

        # 更新
        self._update_info: Optional[dict] = None

        os.makedirs(os.path.join(SCRIPT_DIR, "output"), exist_ok=True)

        self._load_config()
        self._apply_styles()
        self._build()
        self._poll()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        if self._config.get("manifest_url"):
            threading.Thread(target=self._bg_check_update, daemon=True).start()

        # 启动时如未配置代理，自动检测系统代理
        self.root.after(300, self._startup_detect_proxy)
        # 启动时直接从远程 URL 拉取账号（跨设备场景，本地无账号文件）
        self.root.after(600, self._load_pool_remote)
        self.root.after(800, self._refresh_status)

    # ══════════════════════════════════════════
    # 配置文件
    # ══════════════════════════════════════════
    def _load_config(self):
        defaults = {
            "version":      APP_VERSION,
            "manifest_url": "https://gist.githubusercontent.com/wxw220381/8969fe60961adb39bf4b34ae861deb85/raw/manifest.json",
            # pool_url 始终使用顶层常量 POOL_URL，不允许用户覆盖
            "pool_url":     POOL_URL,
            "proxy":        "",
        }
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8-sig") as f:
                    saved = json.load(f)
                    if "proxy" in saved:
                        defaults["proxy"] = saved["proxy"]
            except Exception:
                pass
        self._config = defaults

    def _save_config(self):
        try:
            to_save = {"proxy": self._config.get("proxy", "")}
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(to_save, f, indent=2, ensure_ascii=False)
        except Exception:
            pass

    @staticmethod
    def _domain_of(email: str) -> str:
        """只返回邮箱的域名部分，如 user@example.com → @example.com"""
        if "@" in email:
            return "@" + email.split("@", 1)[1]
        return email

    def _startup_detect_proxy(self):
        """启动时若无代理配置，自动检测系统代理并填入"""
        if self._proxy_var.get().strip():
            return  # 已有配置，不覆盖
        p = detect_system_proxy()
        if p:
            self._proxy_var.set(p)  # 触发 _on_proxy_changed 自动保存
            self._lbl_proxy_hint.configure(text=f"✅ 自动: {p}", fg=C["green"])
            self._log(f"🔍  自动检测到系统代理: {p}", "dim")
        else:
            self._lbl_proxy_hint.configure(
                text="⚠️ 未检到代理，Firebase需要代理", fg=C["yellow"])

    def _on_proxy_changed(self, *_):
        """代理地址变更时同步到 config 并持久化"""
        self._config["proxy"] = self._proxy_var.get()
        self._save_config()

    def _detect_proxy(self):
        p = detect_system_proxy()
        if p:
            self._proxy_var.set(p)
            self._lbl_proxy_hint.configure(text=f"✅{p}", fg=C["green"])
        else:
            self._proxy_var.set("")
            self._lbl_proxy_hint.configure(text="未检到", fg=C["red"])

    # ══════════════════════════════════════════
    # TTK 样式
    # ══════════════════════════════════════════
    def _apply_styles(self):
        s = ttk.Style()
        s.theme_use("clam")
        s.configure("Treeview",
                    background=C["surface"], foreground=C["text"],
                    fieldbackground=C["surface"], rowheight=24,
                    font=("Segoe UI", 9))
        s.configure("Treeview.Heading",
                    background=C["overlay"], foreground=C["blue"],
                    font=("Segoe UI", 9, "bold"), relief="flat")
        s.map("Treeview",
              background=[("selected", C["overlay"])],
              foreground=[("selected", C["lav"])])
        s.configure("TScrollbar",
                    background=C["overlay"], troughcolor=C["surface"],
                    bordercolor=C["bg"], arrowcolor=C["muted"])

    # ══════════════════════════════════════════
    # UI 构建
    # ══════════════════════════════════════════
    def _build(self):
        self._build_header()
        self._build_main()
        self._build_guard_bar()

    def _build_header(self):
        hdr = tk.Frame(self.root, bg=C["surface"], height=52)
        hdr.pack(fill=tk.X)
        hdr.pack_propagate(False)

        tk.Label(hdr, text=f"🔐  {APP_NAME}",
                 bg=C["surface"], fg=C["blue"],
                 font=("Segoe UI", 14, "bold")).pack(side=tk.LEFT, padx=18)

        self._btn_update = tk.Button(
            hdr, text="🔄 检查更新",
            bg=C["overlay"], fg=C["blue"],
            activebackground=C["overlay"], activeforeground=C["blue"],
            font=("Segoe UI", 8), relief="flat",
            padx=10, pady=3, cursor="hand2",
            command=self._manual_check_update)
        self._btn_update.pack(side=tk.RIGHT, padx=14)

        if GIST_GH_TOKEN:   # 只对开发者显示「推送新版」按钒
            tk.Button(
                hdr, text="☁️ 推送新版",
                bg=C["lav"], fg=C["bg"],
                activebackground=C["lav"], activeforeground=C["bg"],
                font=("Segoe UI", 8), relief="flat",
                padx=10, pady=3, cursor="hand2",
                command=self._push_update_dialog
            ).pack(side=tk.RIGHT, padx=(0, 4))

        tk.Label(hdr, text=f"v{APP_VERSION}",
                 bg=C["surface"], fg=C["muted"],
                 font=("Segoe UI", 9)).pack(side=tk.RIGHT, padx=(0, 4))

        self._lbl_status = tk.Label(
            hdr, text="● 就绪",
            bg=C["surface"], fg=C["muted"],
            font=("Segoe UI", 9))
        self._lbl_status.pack(side=tk.RIGHT, padx=(0, 12))

    def _build_main(self):
        main = tk.Frame(self.root, bg=C["bg"])
        main.pack(fill=tk.BOTH, expand=True, padx=10, pady=(8, 4))
        main.columnconfigure(0, weight=3)
        main.columnconfigure(1, weight=2)
        main.rowconfigure(0, weight=1)
        self._build_pool_panel(main)
        self._build_right_panel(main)

    def _build_pool_panel(self, parent):
        frame = tk.Frame(parent, bg=C["surface"],
                         highlightthickness=1, highlightbackground=C["overlay"])
        frame.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        frame.rowconfigure(2, weight=1)
        frame.columnconfigure(0, weight=1)

        tk.Label(frame, text="📦  账号池",
                 bg=C["surface"], fg=C["blue"],
                 font=("Segoe UI", 10, "bold")).grid(
            row=0, column=0, sticky="w", padx=12, pady=(10, 6))

        stats = tk.Frame(frame, bg=C["surface"])
        stats.grid(row=1, column=0, sticky="ew", padx=12, pady=(0, 8))

        def _stat(label, color):
            card = tk.Frame(stats, bg=C["overlay"], padx=12, pady=5)
            card.pack(side=tk.LEFT, padx=(0, 6))
            big = tk.Label(card, text="—", bg=C["overlay"], fg=color,
                           font=("Segoe UI", 18, "bold"))
            big.pack()
            tk.Label(card, text=label, bg=C["overlay"], fg=C["muted"],
                     font=("Segoe UI", 7)).pack()
            return big

        self._lbl_avail   = _stat("可用",   C["green"])
        self._lbl_total   = _stat("总量",   C["blue"])
        self._lbl_banned  = _stat("封禁",   C["red"])
        self._lbl_deleted = _stat("删除",   C["muted"])

        list_wrap = tk.Frame(frame, bg=C["surface"])
        list_wrap.grid(row=2, column=0, sticky="nsew", padx=12, pady=(0, 6))
        list_wrap.rowconfigure(0, weight=1)
        list_wrap.columnconfigure(0, weight=1)

        self._tree = ttk.Treeview(
            list_wrap, columns=("email",),
            show="headings", height=12)
        self._tree.heading("email", text="邮箱")
        self._tree.column("email", width=360, minwidth=160, stretch=True)

        ysb = ttk.Scrollbar(list_wrap, orient="vertical",
                            command=self._tree.yview)
        self._tree.configure(yscrollcommand=ysb.set)
        self._tree.grid(row=0, column=0, sticky="nsew")
        ysb.grid(row=0, column=1, sticky="ns")
        self._tree.bind("<Double-1>", lambda e: self._apply_selected())

        btn_row = tk.Frame(frame, bg=C["surface"])
        btn_row.grid(row=3, column=0, sticky="ew", padx=12, pady=(0, 6))

        def _btn(text, bg, fg_c, cmd):
            b = tk.Button(btn_row, text=text, bg=bg, fg=fg_c,
                          activebackground=bg,
                          font=("Segoe UI", 8, "bold"), relief="flat",
                          padx=10, pady=4, cursor="hand2", command=cmd)
            b.pack(side=tk.LEFT, padx=(0, 5))
            return b

        _btn("📂 加载账号", C["overlay"], C["text"],  self._load_pool)
        _btn("☁ 远程拉取", C["lav"],     C["bg"],    self._load_pool_remote)
        self._btn_apply = _btn("🚀 一键切换", C["blue"],  C["bg"],    self._quick_apply)
        _btn("🎯 切换选中", C["green"],   C["bg"],    self._apply_selected)
        _btn("📥 导入账号", C["yellow"],  C["bg"],    self._import_current_account)

        url_row = tk.Frame(frame, bg=C["surface"])
        url_row.grid(row=4, column=0, sticky="ew", padx=12, pady=(0, 4))
        tk.Label(url_row, text="账号源:",
                 bg=C["surface"], fg=C["muted"],
                 font=("Segoe UI", 8)).pack(side=tk.LEFT)
        # 只读显示写死的 POOL_URL，截断过长部分
        _short_url = POOL_URL if len(POOL_URL) <= 52 else POOL_URL[:49] + "..."
        tk.Label(url_row, text=_short_url,
                 bg=C["surface"], fg=C["muted"],
                 font=("Consolas", 7),
                 anchor="w").pack(side=tk.LEFT, padx=(4, 0))
        # pool_path_var 仍保留（远程拉取后缓存到本地使用）
        self._pool_path_var = tk.StringVar(value=DEFAULT_POOL)

        proxy_row = tk.Frame(frame, bg=C["surface"])
        proxy_row.grid(row=5, column=0, sticky="ew", padx=12, pady=(0, 10))
        tk.Label(proxy_row, text="代理地址:",
                 bg=C["surface"], fg=C["muted"],
                 font=("Segoe UI", 8)).pack(side=tk.LEFT)
        self._proxy_var = tk.StringVar(value=self._config.get("proxy", ""))
        self._proxy_var.trace_add("write", self._on_proxy_changed)
        tk.Entry(proxy_row, textvariable=self._proxy_var,
                 bg=C["overlay"], fg=C["text"],
                 insertbackground=C["text"],
                 font=("Consolas", 8), relief="flat",
                 width=22).pack(side=tk.LEFT, padx=(4, 0),
                                fill=tk.X, expand=True)
        self._lbl_proxy_hint = tk.Label(proxy_row, text="",
                 bg=C["surface"], fg=C["muted"],
                 font=("Segoe UI", 7))
        self._lbl_proxy_hint.pack(side=tk.LEFT, padx=(4, 0))
        tk.Button(proxy_row, text="🔍 检测",
                  bg=C["overlay"], fg=C["lav"],
                  font=("Segoe UI", 7), relief="flat",
                  padx=4, pady=1, cursor="hand2",
                  command=self._detect_proxy).pack(side=tk.LEFT, padx=(4, 0))

    def _build_right_panel(self, parent):
        right = tk.Frame(parent, bg=C["bg"])
        right.grid(row=0, column=1, sticky="nsew")
        right.rowconfigure(1, weight=1)
        right.columnconfigure(0, weight=1)

        # 当前登录状态卡
        sc = tk.Frame(right, bg=C["surface"],
                      highlightthickness=1, highlightbackground=C["overlay"])
        sc.grid(row=0, column=0, sticky="ew", pady=(0, 6))

        tk.Label(sc, text="👤  当前登录状态",
                 bg=C["surface"], fg=C["blue"],
                 font=("Segoe UI", 10, "bold")).pack(
            anchor="w", padx=12, pady=(10, 6))

        info_frame = tk.Frame(sc, bg=C["surface"])
        info_frame.pack(fill=tk.X, padx=12, pady=(0, 4))

        def _info_row(label):
            row = tk.Frame(info_frame, bg=C["surface"])
            row.pack(fill=tk.X, pady=1)
            tk.Label(row, text=label, width=10, anchor="w",
                     bg=C["surface"], fg=C["muted"],
                     font=("Segoe UI", 8)).pack(side=tk.LEFT)
            val = tk.Label(row, text="——", anchor="w",
                           bg=C["surface"], fg=C["text"],
                           font=("Consolas", 8))
            val.pack(side=tk.LEFT, padx=(4, 0))
            return val

        self._lbl_conn    = _info_row("登录状态:")
        self._lbl_ip      = _info_row("当前邮箱:")
        self._lbl_cur_lic = _info_row("Firebase UID:")
        self._lbl_cli_ver = _info_row("Token 到期:")
        self._lbl_credits = _info_row("AI 剩余:")

        sc_btns = tk.Frame(sc, bg=C["surface"])
        sc_btns.pack(fill=tk.X, padx=12, pady=(2, 10))
        tk.Button(sc_btns, text="🔄 刷新状态",
                  bg=C["overlay"], fg=C["blue"],
                  font=("Segoe UI", 8), relief="flat",
                  padx=8, pady=3, cursor="hand2",
                  command=self._refresh_status).pack(side=tk.LEFT)
        tk.Button(sc_btns, text="📊 查额度",
                  bg=C["overlay"], fg=C["muted"],
                  font=("Segoe UI", 8), relief="flat",
                  padx=8, pady=3, cursor="hand2",
                  command=self._fetch_credits).pack(side=tk.LEFT, padx=(6, 0))
        tk.Button(sc_btns, text="📋 复制邮箱",
                  bg=C["overlay"], fg=C["muted"],
                  font=("Segoe UI", 8), relief="flat",
                  padx=8, pady=3, cursor="hand2",
                  command=self._copy_email).pack(side=tk.LEFT, padx=(6, 0))

        # 日志区
        log_frame = tk.Frame(right, bg=C["surface"],
                             highlightthickness=1,
                             highlightbackground=C["overlay"])
        log_frame.grid(row=1, column=0, sticky="nsew")
        log_frame.rowconfigure(1, weight=1)
        log_frame.columnconfigure(0, weight=1)

        log_hdr = tk.Frame(log_frame, bg=C["surface"])
        log_hdr.grid(row=0, column=0, columnspan=2,
                     sticky="ew", padx=10, pady=(8, 2))
        tk.Label(log_hdr, text="📋  日志",
                 bg=C["surface"], fg=C["blue"],
                 font=("Segoe UI", 9, "bold")).pack(side=tk.LEFT)
        tk.Button(log_hdr, text="🗑 清除",
                  bg=C["overlay"], fg=C["muted"],
                  font=("Segoe UI", 7), relief="flat",
                  padx=6, pady=2, cursor="hand2",
                  command=self._clear_log).pack(side=tk.RIGHT)

        self._log_txt = tk.Text(
            log_frame, bg=C["surface"], fg=C["text"],
            font=("Consolas", 8), relief="flat",
            wrap=tk.WORD, state=tk.DISABLED,
            padx=8, pady=4)
        log_sb = ttk.Scrollbar(log_frame, command=self._log_txt.yview)
        self._log_txt.configure(yscrollcommand=log_sb.set)
        self._log_txt.grid(row=1, column=0, sticky="nsew")
        log_sb.grid(row=1, column=1, sticky="ns")

        self._log_txt.tag_configure("info", foreground=C["blue"])
        self._log_txt.tag_configure("ok",   foreground=C["green"])
        self._log_txt.tag_configure("warn", foreground=C["yellow"])
        self._log_txt.tag_configure("err",  foreground=C["red"])
        self._log_txt.tag_configure("dim",  foreground=C["muted"])

    def _build_guard_bar(self):
        outer = tk.Frame(self.root, bg=C["bg"])
        outer.pack(fill=tk.X, padx=10, pady=(0, 8))

        card = tk.Frame(outer, bg=C["surface"],
                        highlightthickness=1, highlightbackground=C["overlay"])
        card.pack(fill=tk.X)

        inner = tk.Frame(card, bg=C["surface"])
        inner.pack(fill=tk.X, padx=12, pady=7)

        tk.Label(inner, text="🤖  自动守护 & 续杯",
                 bg=C["surface"], fg=C["blue"],
                 font=("Segoe UI", 9, "bold")).pack(side=tk.LEFT)
        ttk.Separator(inner, orient="vertical").pack(
            side=tk.LEFT, fill=tk.Y, padx=10, pady=2)

        self._btn_guard_start = tk.Button(
            inner, text="▶ 启动守护",
            bg=C["green"], fg=C["bg"],
            font=("Segoe UI", 8, "bold"), relief="flat",
            padx=10, pady=3, cursor="hand2",
            command=self._start_guard)
        self._btn_guard_start.pack(side=tk.LEFT)

        self._btn_guard_stop = tk.Button(
            inner, text="⏹ 停止",
            bg=C["overlay"], fg=C["muted"],
            font=("Segoe UI", 8), relief="flat",
            padx=10, pady=3, cursor="hand2",
            state=tk.DISABLED,
            command=self._stop_guard)
        self._btn_guard_stop.pack(side=tk.LEFT, padx=(4, 0))

        ttk.Separator(inner, orient="vertical").pack(
            side=tk.LEFT, fill=tk.Y, padx=10, pady=2)

        tk.Label(inner, text="检测间隔",
                 bg=C["surface"], fg=C["muted"],
                 font=("Segoe UI", 8)).pack(side=tk.LEFT)
        self._guard_interval_var = tk.IntVar(value=60)
        tk.Spinbox(inner, textvariable=self._guard_interval_var,
                   from_=10, to=600, width=4,
                   bg=C["overlay"], fg=C["text"],
                   font=("Segoe UI", 8), relief="flat",
                   buttonbackground=C["overlay"]).pack(
            side=tk.LEFT, padx=(4, 0))
        tk.Label(inner, text="秒",
                 bg=C["surface"], fg=C["muted"],
                 font=("Segoe UI", 8)).pack(side=tk.LEFT, padx=(2, 0))

        ttk.Separator(inner, orient="vertical").pack(
            side=tk.LEFT, fill=tk.Y, padx=10, pady=2)

        tk.Label(inner, text="守护状态",
                 bg=C["surface"], fg=C["muted"],
                 font=("Segoe UI", 8)).pack(side=tk.LEFT)
        self._lbl_guard_status = tk.Label(
            inner, text="● 未启动",
            bg=C["surface"], fg=C["muted"],
            font=("Segoe UI", 8, "bold"))
        self._lbl_guard_status.pack(side=tk.LEFT, padx=(4, 0))

        ttk.Separator(inner, orient="vertical").pack(
            side=tk.LEFT, fill=tk.Y, padx=10, pady=2)

        tk.Label(inner, text="已切换",
                 bg=C["surface"], fg=C["muted"],
                 font=("Segoe UI", 8)).pack(side=tk.LEFT)
        self._lbl_switch_count = tk.Label(
            inner, text="0",
            bg=C["surface"], fg=C["blue"],
            font=("Segoe UI", 12, "bold"))
        self._lbl_switch_count.pack(side=tk.LEFT, padx=(4, 0))
        tk.Label(inner, text="次",
                 bg=C["surface"], fg=C["muted"],
                 font=("Segoe UI", 8)).pack(side=tk.LEFT, padx=(2, 0))

        ttk.Separator(inner, orient="vertical").pack(
            side=tk.LEFT, fill=tk.Y, padx=10, pady=2)

        tk.Label(inner, text="当前账号",
                 bg=C["surface"], fg=C["muted"],
                 font=("Segoe UI", 8)).pack(side=tk.LEFT)
        self._lbl_guard_lic = tk.Label(
            inner, text="————————",
            bg=C["surface"], fg=C["text"],
            font=("Consolas", 8))
        self._lbl_guard_lic.pack(side=tk.LEFT, padx=(4, 0))

        ttk.Separator(inner, orient="vertical").pack(
            side=tk.LEFT, fill=tk.Y, padx=10, pady=2)

        tk.Label(inner, text="额度<",
                 bg=C["surface"], fg=C["muted"],
                 font=("Segoe UI", 8)).pack(side=tk.LEFT)
        self._guard_threshold_var = tk.IntVar(value=3)
        tk.Spinbox(inner, textvariable=self._guard_threshold_var,
                   from_=0, to=100, width=3,
                   bg=C["overlay"], fg=C["text"],
                   font=("Segoe UI", 8), relief="flat",
                   buttonbackground=C["overlay"]).pack(
            side=tk.LEFT, padx=(2, 0))
        tk.Label(inner, text="换",
                 bg=C["surface"], fg=C["muted"],
                 font=("Segoe UI", 8)).pack(side=tk.LEFT, padx=(2, 8))

        self._guard_no_restart_var = tk.BooleanVar(value=True)
        tk.Checkbutton(inner, text="无感切换",
                       variable=self._guard_no_restart_var,
                       bg=C["surface"], fg=C["text"],
                       selectcolor=C["overlay"],
                       activebackground=C["surface"],
                       font=("Segoe UI", 8),
                       relief="flat").pack(side=tk.LEFT)

    # ══════════════════════════════════════════
    # 日志工具
    # ══════════════════════════════════════════
    def _log(self, msg: str, tag: str = ""):
        ts   = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}]  {msg}\n"
        self._log_txt.configure(state=tk.NORMAL)
        self._log_txt.insert(tk.END, line, tag or "")
        self._log_txt.see(tk.END)
        self._log_txt.configure(state=tk.DISABLED)

    def _clear_log(self):
        self._log_txt.configure(state=tk.NORMAL)
        self._log_txt.delete("1.0", tk.END)
        self._log_txt.configure(state=tk.DISABLED)

    def _set_status(self, text: str, color: str = ""):
        self._lbl_status.configure(text=text, fg=color or C["muted"])

    # ══════════════════════════════════════════
    # Warp 终端工具
    # ══════════════════════════════════════════
    @staticmethod
    def _find_warp_exe() -> Optional[str]:
        for p in WARP_EXE_CANDIDATES:
            if os.path.exists(p):
                return p
        return shutil.which("warp")

    def _read_current_auth(self) -> Optional[dict]:
        """读取并解密当前 Warp 认证文件"""
        if not os.path.exists(WARP_AUTH_FILE):
            return None
        try:
            with open(WARP_AUTH_FILE, "rb") as f:
                raw = f.read()
            plain = _dpapi_decrypt(raw)
            if plain:
                return json.loads(plain.decode("utf-8"))
        except Exception:
            pass
        return None

    def _write_auth(self, auth_dict: dict) -> bool:
        """序列化并 DPAPI 加密写回认证文件"""
        try:
            plain     = json.dumps(auth_dict, ensure_ascii=False,
                                   separators=(",", ":")).encode("utf-8")
            encrypted = _dpapi_encrypt(plain)
            if encrypted:
                # 先备份原文件
                if os.path.exists(WARP_AUTH_FILE):
                    shutil.copy2(WARP_AUTH_FILE, WARP_AUTH_FILE + ".bak")
                with open(WARP_AUTH_FILE, "wb") as f:
                    f.write(encrypted)
                return True
        except Exception as e:
            self._log_q.put(f"⚠️  写入认证文件出错: {e}")
        return False

    def _update_sqlite(self, firebase_uid: str, email: str):
        """更新 warp.sqlite 中的用户信息"""
        if not os.path.exists(WARP_SQLITE):
            return
        try:
            with sqlite3.connect(WARP_SQLITE) as con:
                con.execute("UPDATE current_user_information SET email = ?", (email,))
                con.execute("UPDATE users SET firebase_uid = ?", (firebase_uid,))
                con.execute(
                    "INSERT OR REPLACE INTO user_profiles "
                    "(firebase_uid, email, display_name, photo_url) VALUES (?, ?, '', '')",
                    (firebase_uid, email),
                )
                con.commit()
        except Exception as e:
            self._log_q.put(f"⚠️  更新数据库出错: {e}")

    # ══════════════════════════════════════════
    # 账号池管理
    # ══════════════════════════════════════════
    def _auto_load_pool(self):
        path = self._pool_path_var.get().strip() or DEFAULT_POOL
        if os.path.exists(path):
            self._load_pool(silent=True)

    def _load_pool(self, silent: bool = False):
        path = self._pool_path_var.get().strip() or DEFAULT_POOL
        if not os.path.exists(path):
            if not silent:
                self._log(f"⚠️  文件不存在: {path}", "warn")
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, list):
                data = [data]
            # 保留有效账号：
            # - 普通账号: refresh_token + local_id
            # - wk_key 账号: 只需 wk_key（local_id 可为空）
            self._pool = [
                a for a in data
                if a.get("wk_key") or (
                    a.get("local_id") and a.get("refresh_token")
                )
            ]
            wk_cnt = sum(1 for a in self._pool if a.get("wk_key"))
            norm_cnt = len(self._pool) - wk_cnt
            self._update_tree()
            self._update_stats()
            self._log(
                f"✅  已加载 {len(self._pool)} 个账号"
                f"（普通: {norm_cnt}  WK-Key: {wk_cnt}）  ←  {path}", "ok")
            if not self._pool:
                self._log(
                    "⚠️  账号池为空或均缺少凭据。\n"
                    "    普通账号需要 refresh_token + local_id，\n"
                    "    Warp Pro 账号需要 wk_key + local_id。",
                    "warn",
                )
        except Exception as e:
            self._log(f"❌  加载账号池失败: {e}", "err")

    def _load_pool_remote(self):
        url = self._config.get("pool_url", "").strip()
        if not url:
            self._log("⚠️  未配置 pool_url，请点击「检查更新」→ 配置", "warn")
            return
        self._log("⬇️  正在从远程拉取账号池…", "info")
        threading.Thread(target=self._worker_load_remote, args=(url,), daemon=True).start()

    def _worker_load_remote(self, url: str):
        try:
            proxy = self._config.get("proxy", "").strip() or None
            data = json.loads(_gh_urlopen(url, timeout=20, proxy=proxy).decode("utf-8"))
            if not isinstance(data, list):
                data = [data]
            accounts = [a for a in data
                        if a.get("wk_key") or (
                            a.get("local_id") and a.get("refresh_token")
                        )]
            local_path = self._pool_path_var.get().strip() or DEFAULT_POOL
            os.makedirs(os.path.dirname(local_path), exist_ok=True)
            with open(local_path, "w", encoding="utf-8") as f:
                json.dump(accounts, f, indent=2, ensure_ascii=False)
            self._result_q.put({
                "_pool_remote_done": True,
                "pool": accounts,
                "msg":  f"✅  远程拉取成功，共 {len(accounts)} 个账号",
            })
        except Exception as e:
            self._result_q.put({
                "_pool_remote_done": True,
                "pool": None,
                "msg":  f"❌  远程拉取失败: {e}",
            })

    def _import_current_account(self):
        """
        从当前 Warp 认证文件提取登录账号并加入号池。
        适用场景：在 A 设备手动登录 Warp 后，导入到号池并同步到 Gist，
        让 B 设备能直接拉取使用。
        """
        auth = self._read_current_auth()
        if not auth:
            self._log("⚠️  读取 Warp 认证文件失败，请确认 Warp 已登录", "warn")
            return

        email     = auth.get("email", "")
        local_id  = auth.get("local_id", "")
        id_tkn_obj = auth.get("id_token") or {}
        refresh_token = id_tkn_obj.get("refresh_token", "")

        if not local_id or not refresh_token:
            self._log("⚠️  认证文件缺少 local_id 或 refresh_token", "warn")
            return

        # 抱名账号不可用（服务端不标记引导，Warp 会显示 Sign up）
        if not email or (email.startswith("anon_") and email.endswith("@warp")):
            self._log("⚠️  当前账号是匿名账号，不支持导入\n"
                      "    请在 Warp 中手动登录一个真实 email 账号，再点导入。", "warn")
            return

        # 读取本地号池
        local_path = self._pool_path_var.get().strip() or DEFAULT_POOL
        existing: list = []
        if os.path.exists(local_path):
            try:
                with open(local_path, "r", encoding="utf-8-sig") as f:
                    existing = json.load(f)
                if not isinstance(existing, list):
                    existing = []
            except Exception:
                pass

        # 去重检查
        if any(a.get("local_id") == local_id for a in existing):
            self._log(f"ℹ️  账号 {email} 已在号池中，无需重复导入", "dim")
        else:
            new_acc = {
                "email":         email,
                "local_id":      local_id,
                "refresh_token": refresh_token,
                "display_name":  auth.get("display_name"),
                "photo_url":     auth.get("photo_url"),
                "status":        "active",
                "account_type":  "email",
            }
            existing.append(new_acc)
            os.makedirs(os.path.dirname(local_path), exist_ok=True)
            with open(local_path, "w", encoding="utf-8") as f:
                json.dump(existing, f, indent=2, ensure_ascii=False)
            self._log(f"✅  已导入账号: {email}（号池共 {len(existing)} 个）", "ok")

        # 更新内存号池
        self._pool = [a for a in existing
                      if a.get("wk_key") or (
                          a.get("refresh_token") and a.get("local_id")
                      )]
        self._update_tree()
        self._update_stats()

        # 告知用户同步到 Gist
        if messagebox.askyesno(
                "同步到云端",
                f"导入成功！共 {len(self._pool)} 个账号。\n是否同步号池到 Gist？\n"
                f"（同步后第二台设备可用【远程拉取】到此账号）",
                parent=self.root):
            threading.Thread(
                target=self._worker_upload_pool_to_gist,
                daemon=True).start()

    def _worker_upload_pool_to_gist(self):
        """\u5c06本地号池同步到 Gist"""
        import ssl, urllib.request as _ur
        try:
            local_path = self._pool_path_var.get().strip() or DEFAULT_POOL
            with open(local_path, "r", encoding="utf-8-sig") as f:
                accounts = json.load(f)
            content = json.dumps(accounts, indent=2, ensure_ascii=False)
            payload = json.dumps({
                "description": f"Warp Accounts \u2014 {len(accounts)} \u4e2a",
                "files": {"warp_accounts_standard.json": {"content": content}},
            }).encode("utf-8")
            ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl.CERT_NONE
            proxy = self._config.get("proxy", "").strip() or None
            req = Request(
                f"https://api.github.com/gists/{GIST_ID}",
                data=payload, method="PATCH",
                headers={
                    "Authorization": f"token {GIST_GH_TOKEN}",
                    "Accept": "application/vnd.github.v3+json",
                    "Content-Type": "application/json",
                    "User-Agent": "WarpLoginTool/2.0",
                })
            if proxy:
                opener = _ur.build_opener(
                    _ur.ProxyHandler({"http": f"http://{proxy}",
                                      "https": f"http://{proxy}"}),
                    _ur.HTTPSHandler(context=ssl_ctx))
                resp = json.loads(opener.open(req, timeout=20).read())
            else:
                resp = json.loads(urlopen(req, timeout=20, context=ssl_ctx).read())
            if "id" in resp:
                self._log_q.put(f"✅  号池已同步到 Gist，共 {len(accounts)} 个账号")
            else:
                self._log_q.put(f"⚠️  Gist 同步失败: {str(resp)[:80]}")
        except Exception as e:
            self._log_q.put(f"❌  Gist 同步错误: {e}")

    def _update_tree(self):
        self._tree.delete(*self._tree.get_children())
        for acc in self._pool:
            email = acc.get("email", "")
            self._tree.insert("", tk.END, values=(self._domain_of(email),))

    def _update_stats(self):
        total   = len(self._pool)
        banned  = sum(1 for a in self._pool if a.get("status") == "banned")
        deleted = sum(1 for a in self._pool if a.get("status") == "deleted")
        usable  = sum(1 for a in self._pool
                      if a.get("status") not in ("banned", "deleted"))
        self._lbl_avail.configure(text=str(usable))
        self._lbl_total.configure(text=str(total))
        self._lbl_banned.configure(text=str(banned))
        self._lbl_deleted.configure(text=str(deleted))

    # ══════════════════════════════════════════
    # 切换账号（核心逻辑）
    # ══════════════════════════════════════════
    def _quick_apply(self):
        if not self._pool:
            self._load_pool()
        if not self._pool:
            self._log("⚠️  号池为空，请先加载账号", "warn")
            return
        acc = next(
            (a for a in self._pool if a.get("status") == "active"),
            next((a for a in self._pool
                  if a.get("status") not in ("banned", "deleted")
                  and (a.get("refresh_token") or a.get("wk_key"))), None),
        )
        if not acc:
            self._log("⚠️  无可用账号（全部封禁/删除），请重新加载", "warn")
            return
        self._apply_account(acc)

    def _apply_selected(self):
        sel = self._tree.selection()
        if not sel:
            self._log("⚠️  请先在列表中单击选择一个账号", "warn")
            return
        idx = self._tree.index(sel[0])
        if idx >= len(self._pool):
            return
        self._apply_account(self._pool[idx])

    def _apply_account(self, acc: dict):
        if self._busy:
            self._log("⚠️  正在切换中，请稍候…", "warn")
            return
        if not acc.get("refresh_token") and not acc.get("wk_key"):
            self._log("⚠️  该账号缺少 refresh_token 和 wk_key，无法切换", "warn")
            return
        self._busy = True
        self._btn_apply.configure(state=tk.DISABLED)
        email = acc.get("email", "")
        self._log(f"🚀  正在切换账号: {email}…", "info")
        threading.Thread(target=self._worker_apply, args=(acc,), daemon=True).start()

    def _worker_apply(self, acc: dict):
        """后台线程：完整的账号切换流程"""
        refresh_token = acc.get("refresh_token", "")
        wk_key        = acc.get("wk_key", "")
        local_id      = acc.get("local_id", "")
        email         = acc.get("email", "")
        proxy         = self._config.get("proxy", "").strip() or None

        if wk_key:
            # ══ WK-Key 账号路径：跳过 Firebase，直接使用 wk-1.* 作为 id_token ══
            self._log_q.put("⚡  WK-Key 账号，跳过 Firebase 刷新…")
            expiration = "2099-01-01T00:00:00+00:00"
            new_id_token  = wk_key
            new_refresh   = ""
            # 仍然调用 SetUserIsOnboarded，确保终端不弹引导页
            self._log_q.put("🔑  标记账号引导状态（SetUserIsOnboarded）…")
            ob_err = _warp_set_onboarded(wk_key, proxy)
            if ob_err:
                self._log_q.put(f"⚠️  SetUserIsOnboarded 失败（不影响切换）: {ob_err[:60]}")
            else:
                self._log_q.put("✅  引导状态已确认")
        else:
            # ══ 普通 Firebase 账号路径 ══
            # ① 刷新 Firebase Token
            self._log_q.put("🔄  正在刷新 Firebase Token…")
            new_id_token, new_refresh, expires_in = _firebase_refresh(refresh_token, proxy)
            if new_id_token is None:
                err_str = str(new_refresh or "")
                if "USER_DISABLED" in err_str:
                    self._log_q.put("❌  账号已被封禁（USER_DISABLED）")
                elif "USER_NOT_FOUND" in err_str:
                    self._log_q.put("❌  账号已删除（USER_NOT_FOUND）")
                elif "timed out" in err_str or "NETWORK:" in err_str:
                    cur_proxy = proxy or "未配置"
                    self._log_q.put(
                        f"❌  Firebase 连接超时！\n"
                        f"    Firebase 在国内必须走代理才能访问。\n"
                        f"    当前代理: {cur_proxy}\n"
                        f"    → 请确认 Clash/V2Ray 等代理软件正在运行，\n"
                        f"      并在工具左下角填入代理地址（如 127.0.0.1:7897）"
                    )
                else:
                    self._log_q.put(f"❌  Token 刷新失败: {err_str[:80]}")
                self._result_q.put({"_apply_done": True, "ok": False})
                return

            # ① b 服务端补标引导完成
            self._log_q.put("🔑  标记账号引导状态（SetUserIsOnboarded）…")
            ob_err = _warp_set_onboarded(new_id_token, proxy)
            if ob_err:
                self._log_q.put(f"⚠️  SetUserIsOnboarded 失败（不影响切换）: {ob_err[:60]}")
            else:
                self._log_q.put("✅  引导状态已确认")

            expires_in = int(expires_in or 3600)
            expiration = (
                datetime.now(timezone.utc) + timedelta(seconds=expires_in)
            ).astimezone().isoformat()

        # ② 构建认证 JSON（与 Warp 终端 dev.warp.Warp-User 格式一致）
        auth_data = {
            "id_token": {
                "id_token":        new_id_token,
                "refresh_token":   new_refresh,
                "expiration_time": expiration,
            },
            "refresh_token":       "",
            "local_id":            local_id,
            "email":               email,
            "display_name":        acc.get("display_name"),
            "photo_url":           acc.get("photo_url"),
            "is_onboarded":        True,
            "needs_sso_link":      False,
            "anonymous_user_type": None,
            "linked_at":           None,
            "personal_object_limits": None,
            "is_on_work_domain":   False,
        }

        # ③ 关闭 Warp 终端
        self._log_q.put("⏹️  正在关闭 Warp 终端…")
        try:
            subprocess.run(["taskkill", "/F", "/IM", "warp.exe"],
                           capture_output=True, timeout=8)
        except Exception:
            pass
        time.sleep(1.2)

        # ④ 写入 DPAPI 加密认证文件
        self._log_q.put("✏️  写入认证数据…")
        if not self._write_auth(auth_data):
            self._log_q.put("❌  写入认证文件失败（请确认 Warp 已完全关闭）")
            self._result_q.put({"_apply_done": True, "ok": False})
            return

        # ⑤ 更新 SQLite 数据库
        self._log_q.put("🗄️  更新本地数据库…")
        self._update_sqlite(local_id, email)

        # ⑥ 重启 Warp
        warp_exe = self._find_warp_exe()
        if warp_exe:
            self._log_q.put("🚀  正在启动 Warp 终端…")
            time.sleep(0.5)
            try:
                subprocess.Popen([warp_exe],
                                 creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
            except Exception as e:
                self._log_q.put(f"⚠️  启动 Warp 失败: {e}，请手动打开")
        else:
            self._log_q.put("⚠️  未找到 Warp.exe，请手动重启 Warp 终端")

        self._log_q.put(f"✅  账号切换完成！已切换到: {email}")
        self._result_q.put({
            "_apply_done": True, "ok": True,
            "email": email, "local_id": local_id,
        })

    # ══════════════════════════════════════════
    # 状态刷新
    # ══════════════════════════════════════════
    def _refresh_status(self):
        threading.Thread(target=self._worker_refresh_status, daemon=True).start()

    def _worker_refresh_status(self):
        auth = self._read_current_auth()
        if auth:
            email      = auth.get("email", "")
            local_id   = auth.get("local_id", "")
            id_tkn_obj = auth.get("id_token") or {}
            expiration = id_tkn_obj.get("expiration_time", "")
            self._result_q.put({
                "_status_refreshed": True,
                "logged_in":  bool(email),
                "email":      email,
                "local_id":   local_id,
                "expiration": expiration,
            })
        else:
            self._result_q.put({
                "_status_refreshed": True,
                "logged_in": False,
                "email": "", "local_id": "", "expiration": "",
            })

    def _copy_email(self):
        if self._cur_email:
            self.root.clipboard_clear()
            self.root.clipboard_append(self._cur_email)
            self._log(f"📋  已复制邮箱: {self._cur_email}", "dim")
        else:
            self._log("⚠️  暂无邮箱信息，请先刷新状态", "warn")

    # ══════════════════════════════════════════
    # 自动守护
    # ══════════════════════════════════════════
    def _start_guard(self):
        if self._guard_active:
            return
        if not self._pool:
            self._load_pool(silent=True)
        if not self._pool:
            self._log("⚠️  请先加载账号再启动守护", "warn")
            return
        interval = max(10, self._guard_interval_var.get())
        self._guard_stop.clear()
        self._guard_active = True
        self._btn_guard_start.configure(state=tk.DISABLED, bg=C["overlay"])
        self._btn_guard_stop.configure(state=tk.NORMAL, bg=C["red"], fg=C["bg"])
        self._lbl_guard_status.configure(text="● 守护中", fg=C["green"])
        self._log(
            f"🤖  守护已启动  间隔 {interval}s  号池 {len(self._pool)} 个账号", "info")
        threading.Thread(
            target=self._worker_guard, args=(interval,), daemon=True).start()

    def _stop_guard(self):
        if not self._guard_active:
            return
        self._guard_stop.set()
        self._guard_active = False
        self._btn_guard_start.configure(state=tk.NORMAL, bg=C["green"], fg=C["bg"])
        self._btn_guard_stop.configure(state=tk.DISABLED, bg=C["overlay"], fg=C["muted"])
        self._lbl_guard_status.configure(text="● 已停止", fg=C["muted"])
        self._log("🤖  守护已停止", "info")

    def _fetch_credits(self):
        """手动查询当前账号 AI 剩余额度（支持普通账号和 wk_key 账号）"""
        auth = self._read_current_auth()
        if not auth:
            self._log("⚠️  未读取到认证文件，请确认 Warp 已登录", "warn")
            return
        id_tkn_obj    = auth.get("id_token") or {}
        id_token_val  = id_tkn_obj.get("id_token", "")
        refresh_token = id_tkn_obj.get("refresh_token", "")
        proxy = self._config.get("proxy", "").strip() or None
        if id_token_val.startswith("wk-"):
            # wk_key 账号：直接使用 id_token，无需 Firebase 刷新
            threading.Thread(target=self._worker_fetch_credits,
                             args=(id_token_val, proxy),
                             kwargs={"already_token": True},
                             daemon=True).start()
        elif refresh_token:
            threading.Thread(target=self._worker_fetch_credits,
                             args=(refresh_token, proxy), daemon=True).start()
        else:
            self._log("⚠️  认证文件中无 refresh_token 或 wk_key，无法查询额度", "warn")

    def _worker_fetch_credits(self, token: str, proxy, *, already_token: bool = False):
        """后台查询 AI 剩余额度
        token: refresh_token 或已有 id_token（already_token=True 时跳过 Firebase 刷新）
        使用 GetRequestLimitInfo（从 nirvana-proxy v1.3.1 逆向确认的正确操作名）
        """
        import ssl, json as _json
        import urllib.request as _ur
        ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE

        if already_token:
            new_id = token
        else:
            # 先刷新 Firebase Token
            new_id, err, _ = _firebase_refresh(token, proxy)
            if not new_id:
                self._log_q.put(f"⚠️  [额度] Token 刷新失败: {err}")
                return

        body = _json.dumps({
            "operationName": "GetRequestLimitInfo",
            "query": _QUERY_USAGE_LIMITS,
            "variables": {"requestContext": WARP_GQL_RC},
        }).encode()
        req = Request(
            f"{WARP_GQL_BASE}?op=GetRequestLimitInfo",
            data=body, method="POST",
            headers={
                "User-Agent":   WARP_GQL_UA,
                "Content-Type": "application/json",
                "Accept":       "application/json",
                "Origin":       "https://app.warp.dev",
                "authorization": f"Bearer {new_id}",
            })
        try:
            if proxy:
                opener = _ur.build_opener(
                    _ur.ProxyHandler({"http": f"http://{proxy}",
                                      "https": f"http://{proxy}"}),
                    _ur.HTTPSHandler(context=ssl_ctx))
                with opener.open(req, timeout=12) as r:
                    resp = _json.loads(r.read())
            else:
                with urlopen(req, timeout=12, context=ssl_ctx) as r:
                    resp = _json.loads(r.read())

            # 平展嵌套路径： data.user.user.requestLimitInfo
            data_node = (resp.get("data") or {})
            user_node = ((data_node.get("user") or {}).get("user") or {})
            info      = user_node.get("requestLimitInfo") or {}

            if info:
                used      = info.get("requestsUsedSinceLastRefresh")
                limit     = info.get("requestLimit")
                unlimited = info.get("isUnlimited", False)
                next_ref  = info.get("nextRefreshTime", "")
                if unlimited:
                    remaining = "unlimited"
                elif limit is not None and used is not None:
                    remaining = max(0, int(limit) - int(used))
                else:
                    remaining = None
                if remaining is not None:
                    self._result_q.put({"_credits_fetched": True,
                                        "remaining": remaining, "used": used,
                                        "limit": limit, "unlimited": unlimited,
                                        "next_refresh": next_ref})
                    return
            # 返回原始 JSON 方便调试
            self._result_q.put({"_credits_fetched": True, "raw": str(resp)[:160]})
        except Exception as e:
            self._result_q.put({"_credits_fetched": True, "raw": f"error:{e}"})

    def _check_credits_quick(self, id_token: str, proxy) -> Optional[int]:
        """守护线程专用：快速查询剩余 AI 额度，None 表示无限额或查询失败（不触发切换）"""
        import ssl, urllib.request as _ur
        ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode    = ssl.CERT_NONE
        body = json.dumps({
            "operationName": "GetRequestLimitInfo",
            "query": _QUERY_USAGE_LIMITS,
            "variables": {"requestContext": WARP_GQL_RC},
        }).encode()
        req = Request(
            f"{WARP_GQL_BASE}?op=GetRequestLimitInfo",
            data=body, method="POST",
            headers={
                "User-Agent":    WARP_GQL_UA,
                "Content-Type":  "application/json",
                "Accept":        "application/json",
                "Origin":        "https://app.warp.dev",
                "authorization": f"Bearer {id_token}",
            })
        try:
            if proxy:
                opener = _ur.build_opener(
                    _ur.ProxyHandler({"http":  f"http://{proxy}",
                                      "https": f"http://{proxy}"}),
                    _ur.HTTPSHandler(context=ssl_ctx))
                with opener.open(req, timeout=8) as r:
                    resp = json.loads(r.read())
            else:
                with urlopen(req, timeout=8, context=ssl_ctx) as r:
                    resp = json.loads(r.read())
            info = (
                ((resp.get("data") or {}).get("user") or {})
                .get("user") or {}
            ).get("requestLimitInfo") or {}
            if info.get("isUnlimited"):
                return None  # 无限额，不触发切换
            used  = info.get("requestsUsedSinceLastRefresh")
            limit = info.get("requestLimit")
            if limit is not None and used is not None:
                return max(0, int(limit) - int(used))
        except Exception:
            pass
        return None  # 查询失败时保守地不触发切换

    def _worker_guard(self, interval: int):
        """守护线程：定期检测账号状态和 AI 额度，必要时自动切换"""
        self._log_q.put(f"🤖  守护线程就绪（间隔 {interval}s）")
        while not self._guard_stop.wait(interval):
            try:
                auth = self._read_current_auth()
                if not auth:
                    continue
                id_tkn_obj    = auth.get("id_token") or {}
                id_token_val  = id_tkn_obj.get("id_token", "")
                refresh_token = id_tkn_obj.get("refresh_token", "")
                proxy = self._config.get("proxy", "").strip() or None

                is_wk = id_token_val.startswith("wk-")
                if is_wk:
                    # wk_key 账号：直接使用 id_token 查额度
                    active_token = id_token_val
                else:
                    if not refresh_token:
                        continue
                    # 普通账号：刷新 Firebase Token，顺便检测封禁状态
                    new_id, err, _ = _firebase_refresh(refresh_token, proxy)
                    if new_id is None:
                        err_str = str(err or "")
                        SWITCH_TRIGGERS = (
                            "USER_DISABLED", "USER_NOT_FOUND",
                            "QUOTA_EXCEEDED", "insufficient_credits",
                            "account_banned", "account_suspended",
                        )
                        if any(t in err_str for t in SWITCH_TRIGGERS):
                            self._log_q.put(
                                f"🔄  [守护] 账号异常 ({err_str[:50]})，触发自动切换…")
                            self._auto_q.put("switch")
                            self._guard_stop.wait(interval)
                        continue  # 网络错误或已触发切换，等下次检测
                    active_token = new_id

                # 检查 AI 额度，低于阈值则触发切换
                threshold = self._guard_threshold_var.get()
                if threshold > 0:
                    rem = self._check_credits_quick(active_token, proxy)
                    if rem is not None:  # None = 无限额，跳过检查
                        self._result_q.put({"_guard_credits": rem})
                        if rem < threshold:
                            self._log_q.put(
                                f"🔄  [守护] 额度不足 ({rem} < {threshold})，触发自动切换…")
                            self._auto_q.put("switch")
                            self._guard_stop.wait(interval)
            except Exception as e:
                self._log_q.put(f"⚠️  [守护] 检测异常: {e}")
        self._log_q.put("🤖  守护线程已退出")

    def _do_auto_switch(self):
        """主线程：轮转选取号池下一个可用账号"""
        if not self._pool:
            self._log("⚠️  [守护] 号池为空，自动切换失败", "warn")
            return
        total = len(self._pool)
        for offset in range(1, total + 1):
            idx = (self._pool_idx + offset) % total
            acc = self._pool[idx]
            st  = acc.get("status", "")
            rt  = acc.get("refresh_token", "")
            if st not in ("banned", "deleted") and (rt or acc.get("wk_key")):
                self._pool_idx = idx
                self._switch_count += 1
                n     = self._switch_count
                email = acc.get("email", "")
                self._lbl_switch_count.configure(text=str(n))
                self._lbl_guard_lic.configure(
                    text=self._domain_of(email), fg=C["blue"])
                self._log(
                    f"🔄  [续杯第{n}次]  账号 {idx+1}/{total}  {self._domain_of(email)}", "info")
                threading.Thread(
                    target=self._worker_apply_guard, args=(acc,),
                    daemon=True).start()
                return
        self._log("❌  [守护] 号池无可用账号，请重新加载", "err")

    def _worker_apply_guard(self, acc: dict):
        """守护模式静默切换（不影响按钮状态）"""
        refresh_token = acc.get("refresh_token", "")
        wk_key        = acc.get("wk_key", "")
        local_id      = acc.get("local_id", "")
        email         = acc.get("email", "")
        proxy         = self._config.get("proxy", "").strip() or None

        if wk_key:
            # ══ WK-Key 路径：直接使用，跳过 Firebase ══
            new_id_token = wk_key
            new_refresh  = ""
            expiration   = "2099-01-01T00:00:00+00:00"
            _warp_set_onboarded(wk_key, proxy)
        else:
            # ══ 普通 Firebase 账号路径 ══
            new_id_token, new_refresh, expires_in = _firebase_refresh(refresh_token, proxy)
            if new_id_token is None:
                self._log_q.put(f"❌  [守护] Token 刷新失败: {new_refresh[:60]}")
                return
            _warp_set_onboarded(new_id_token, proxy)
            expires_in = int(expires_in or 3600)
            expiration = (
                datetime.now(timezone.utc) + timedelta(seconds=expires_in)
            ).astimezone().isoformat()

        auth_data = {
            "id_token": {
                "id_token":        new_id_token,
                "refresh_token":   new_refresh,
                "expiration_time": expiration,
            },
            "refresh_token":       "",
            "local_id":            local_id,
            "email":               email,
            "display_name":        acc.get("display_name"),
            "photo_url":           acc.get("photo_url"),
            "is_onboarded":        True,
            "needs_sso_link":      False,
            "anonymous_user_type": None,
            "linked_at":           None,
            "personal_object_limits": None,
            "is_on_work_domain":   False,
        }

        # 无感切换：只写文件，不重启 Warp（适合不想中断当前对话的场景）
        no_restart = self._guard_no_restart_var.get()
        if no_restart:
            if self._write_auth(auth_data):
                self._update_sqlite(local_id, email)
                self._log_q.put(
                    f"⚡  [守护·无感] 已写入凭据: {email}"
                    f"  （Warp 重启或 Token 到期后生效）")
                self._result_q.put({"_guard_refreshed": True, "email": email})
            else:
                self._log_q.put("❌  [守护] 写入认证文件失败")
            return

        # 完整切换：关闭 Warp → 写入凭据 → 重启 Warp
        try:
            subprocess.run(["taskkill", "/F", "/IM", "warp.exe"],
                           capture_output=True, timeout=8)
        except Exception:
            pass
        time.sleep(1.2)

        if self._write_auth(auth_data):
            self._update_sqlite(local_id, email)
            self._log_q.put(f"✅  [守护] 已切换到: {email}")
            warp_exe = self._find_warp_exe()
            if warp_exe:
                time.sleep(0.5)
                try:
                    subprocess.Popen([warp_exe],
                                     creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
                except Exception:
                    pass
            self._result_q.put({"_guard_refreshed": True, "email": email})
        else:
            self._log_q.put("❌  [守护] 写入认证文件失败")

    # ══════════════════════════════════════════
    # 远程更新机制
    # ══════════════════════════════════════════
    @staticmethod
    def _ver_tuple(v: str):
        try:
            return tuple(int(x) for x in v.strip().lstrip("v").split("."))
        except Exception:
            return (0,)

    # ══════════════════════════════════════════
    # 推送新版
    # ══════════════════════════════════════════
    def _push_update_dialog(self):
        """弹出推送新版对话框（全自动：创建 Release + 上传 zip + 更新 manifest）"""
        dlg = tk.Toplevel(self.root)
        dlg.title("☁️  推送新版")
        dlg.geometry("460x200")
        dlg.configure(bg=C["bg"])
        dlg.resizable(False, False)
        dlg.grab_set()

        def _row(label, default="", w=20):
            r = tk.Frame(dlg, bg=C["bg"])
            r.pack(fill=tk.X, padx=16, pady=(10, 0))
            tk.Label(r, text=label, bg=C["bg"], fg=C["text"],
                     font=("Segoe UI", 8), width=10, anchor="w").pack(side=tk.LEFT)
            var = tk.StringVar(value=default)
            tk.Entry(r, textvariable=var, bg=C["overlay"],
                     fg=C["text"], insertbackground=C["text"],
                     font=("Consolas", 8), relief="flat",
                     width=w).pack(side=tk.LEFT, padx=(4, 0), fill=tk.X, expand=True)
            return var

        parts = APP_VERSION.split(".")
        next_ver = ".".join(parts[:-1] + [str(int(parts[-1]) + 1)])
        ver_var  = _row("新版本号:", next_ver)
        note_var = _row("更新说明:", f"v{next_ver} 优化更新")

        # 自动查找 dist 目录中匹配的 zip
        zip_dir = os.path.join(SCRIPT_DIR, "dist")
        tk.Label(dlg,
                 text=f"💡 自动创建 GitHub Release 并上传 {zip_dir} 中匹配的 zip，无需手填 URL。",
                 bg=C["bg"], fg=C["muted"],
                 font=("Segoe UI", 7), justify="left").pack(
            anchor="w", padx=16, pady=(8, 0))

        def _confirm():
            ver  = ver_var.get().strip()
            note = note_var.get().strip()
            if not ver:
                messagebox.showwarning("缺少信息", "版本号不能为空", parent=dlg)
                return
            # 寻找 zip
            import glob
            zips = sorted(glob.glob(os.path.join(zip_dir, "*.zip")), key=os.path.getmtime, reverse=True)
            if not zips:
                messagebox.showerror("找不到 zip",
                    f"请先用 PyInstaller 打包：\ndist\\目录中无 zip 文件", parent=dlg)
                return
            zip_path = zips[0]  # 最新的 zip
            dlg.destroy()
            proxy = self._config.get("proxy", "").strip() or None
            threading.Thread(
                target=self._worker_push_update,
                args=(ver, zip_path, note, proxy),
                daemon=True).start()
            self._log(f"☁️  正在创建 Release v{ver} 并上传 {os.path.basename(zip_path)}…", "info")

        btn_row = tk.Frame(dlg, bg=C["bg"])
        btn_row.pack(pady=12)
        tk.Button(btn_row, text="☁️ 一键推送",
                  bg=C["lav"], fg=C["bg"],
                  font=("Segoe UI", 9, "bold"), relief="flat",
                  padx=14, pady=4, cursor="hand2",
                  command=_confirm).pack(side=tk.LEFT, padx=6)
        tk.Button(btn_row, text="取消",
                  bg=C["overlay"], fg=C["text"],
                  font=("Segoe UI", 9), relief="flat",
                  padx=14, pady=4, cursor="hand2",
                  command=dlg.destroy).pack(side=tk.LEFT, padx=6)

    def _worker_push_update(self, version: str, zip_path: str,
                             note: str, proxy: Optional[str]):
        """后台线程：自动创建 GitHub Release + 上传 zip + 更新 manifest.json"""
        import ssl, urllib.request as _ur
        ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode    = ssl.CERT_NONE

        def _api(method, url, body=None, content_type="application/json",
                 token=REPO_TOKEN, extra_headers=None):
            hdrs = {
                "Authorization": f"token {token}",
                "Accept": "application/vnd.github.v3+json",
                "User-Agent": "WarpLoginTool/2.0",
            }
            if content_type:
                hdrs["Content-Type"] = content_type
            if extra_headers:
                hdrs.update(extra_headers)
            req = Request(url,
                          data=body if isinstance(body, bytes) else
                          (json.dumps(body).encode() if body else None),
                          method=method, headers=hdrs)
            if proxy:
                opener = _ur.build_opener(
                    _ur.ProxyHandler({"http": f"http://{proxy}",
                                       "https": f"http://{proxy}"}),
                    _ur.HTTPSHandler(context=ssl_ctx))
                with opener.open(req, timeout=30) as r:
                    return json.loads(r.read().decode())
            else:
                with urlopen(req, timeout=30, context=ssl_ctx) as r:
                    return json.loads(r.read().decode())

        try:
            # ① 创建 GitHub Release
            rel = _api("POST",
                       f"https://api.github.com/repos/{GITHUB_REPO}/releases",
                       {"tag_name": f"v{version}", "name": f"v{version}",
                        "body": note, "draft": False, "prerelease": False})
            if "id" not in rel:
                raise RuntimeError(f"Release 创建失败: {rel.get('message','')[:80]}")
            release_id = rel["id"]
            self._log_q.put(f"   ✅ Release v{version} 创建成功")

            # ② 上传 zip
            zip_name = os.path.basename(zip_path)
            with open(zip_path, "rb") as f:
                zip_bytes = f.read()
            upload_url = (f"https://uploads.github.com/repos/{GITHUB_REPO}"
                          f"/releases/{release_id}/assets?name={zip_name}")
            asset = _api("POST", upload_url, zip_bytes, content_type="application/zip")
            dl_url = asset.get("browser_download_url", "")
            if not dl_url:
                raise RuntimeError(f"zip 上传失败: {str(asset)[:80]}")
            self._log_q.put(f"   ✅ zip 上传成功：{dl_url}")

            # ③ 更新 Gist manifest.json
            manifest = {"version": version, "download_url": dl_url, "note": note}
            gist_payload = json.dumps({
                "description": f"Warp Login Tool manifest — v{version}",
                "files": {"manifest.json": {
                    "content": json.dumps(manifest, indent=2, ensure_ascii=False)
                }}
            }).encode("utf-8")
            gist_req = Request(
                f"https://api.github.com/gists/{GIST_ID}",
                data=gist_payload, method="PATCH",
                headers={"Authorization": f"token {GIST_GH_TOKEN}",
                         "Accept": "application/vnd.github.v3+json",
                         "Content-Type": "application/json",
                         "User-Agent": "WarpLoginTool/2.0"})
            if proxy:
                opener = _ur.build_opener(
                    _ur.ProxyHandler({"http": f"http://{proxy}",
                                       "https": f"http://{proxy}"}),
                    _ur.HTTPSHandler(context=ssl_ctx))
                gist_resp = json.loads(opener.open(gist_req, timeout=20).read().decode())
            else:
                gist_resp = json.loads(urlopen(gist_req, timeout=20, context=ssl_ctx).read().decode())

            self._result_q.put({"_push_done": True, "ok": True,
                                "version": version, "url": dl_url,
                                "release_url": rel.get("html_url", "")})
        except Exception as e:
            self._result_q.put({"_push_done": True, "ok": False, "msg": str(e)[:150]})

    def _bg_check_update(self):
        try:
            info = self._fetch_manifest()
            if info and (self._ver_tuple(info.get("version", "0"))
                         > self._ver_tuple(APP_VERSION)):
                # 发现新版本：直接自动下载安装，无需用户确认
                self._result_q.put({
                    "_auto_update": True,
                    "info": info,
                })
        except Exception:
            pass

    def _fetch_manifest(self) -> Optional[dict]:
        url = self._config.get("manifest_url", "").strip()
        if not url:
            return None
        proxy = self._config.get("proxy", "").strip() or None
        return json.loads(_gh_urlopen(url, timeout=10, proxy=proxy).decode("utf-8"))

    def _manual_check_update(self):
        if not self._config.get("manifest_url", "").strip():
            # 未配置更新地址，静默显示已是最新
            self._btn_update.configure(state=tk.NORMAL, text="✅ 已是最新",
                                       bg=C["green"], fg=C["bg"])
            self.root.after(3000, lambda: self._btn_update.configure(
                text="🔄 检查更新", bg=C["overlay"],
                fg=C["blue"], state=tk.NORMAL))
            return
        self._btn_update.configure(state=tk.DISABLED, text="⏳ 检查中…")
        threading.Thread(target=self._worker_check_update, daemon=True).start()

    def _worker_check_update(self):
        try:
            info = self._fetch_manifest()
            if info and (self._ver_tuple(info.get("version", "0"))
                         > self._ver_tuple(APP_VERSION)):
                self._result_q.put({
                    "_update_available": True, "info": info, "_silent": False,
                })
            else:
                self._result_q.put({"_update_no_new": True})
        except Exception as e:
            self._result_q.put({"_update_error": True, "msg": str(e)})

    def _open_config_dialog(self):
        dlg = tk.Toplevel(self.root)
        dlg.title("⚙  配置")
        dlg.geometry("560x230")
        dlg.configure(bg=C["bg"])
        dlg.resizable(False, False)
        dlg.grab_set()

        def _row(parent, label, default):
            r = tk.Frame(parent, bg=C["bg"])
            r.pack(fill=tk.X, padx=16, pady=(10, 0))
            tk.Label(r, text=label, bg=C["bg"], fg=C["text"],
                     font=("Segoe UI", 8), width=16, anchor="w").pack(side=tk.LEFT)
            var = tk.StringVar(value=default)
            tk.Entry(r, textvariable=var, bg=C["overlay"],
                     fg=C["text"], insertbackground=C["text"],
                     font=("Consolas", 8), relief="flat",
                     width=44).pack(side=tk.LEFT, padx=(4, 0))
            return var

        manifest_var = _row(dlg, "更新 Manifest URL:", self._config.get("manifest_url", ""))
        pool_var     = _row(dlg, "账号池远程 URL:",    self._config.get("pool_url", ""))
        proxy_var    = _row(dlg, "代理 (host:port):",  self._config.get("proxy", ""))

        def _save():
            self._config["manifest_url"] = manifest_var.get().strip()
            self._config["pool_url"]     = pool_var.get().strip()
            self._config["proxy"]        = proxy_var.get().strip()
            self._save_config()
            dlg.destroy()
            if self._config["manifest_url"]:
                self._manual_check_update()

        btn_row = tk.Frame(dlg, bg=C["bg"])
        btn_row.pack(pady=16)
        tk.Button(btn_row, text="保存",
                  bg=C["blue"], fg=C["bg"],
                  font=("Segoe UI", 9, "bold"), relief="flat",
                  padx=14, pady=4, cursor="hand2",
                  command=_save).pack(side=tk.LEFT, padx=6)
        tk.Button(btn_row, text="取消",
                  bg=C["overlay"], fg=C["text"],
                  font=("Segoe UI", 9), relief="flat",
                  padx=14, pady=4, cursor="hand2",
                  command=dlg.destroy).pack(side=tk.LEFT, padx=6)

    def _prompt_do_update(self, info: dict, silent: bool):
        new_ver = info.get("version", "?")
        dl_url  = info.get("download_url", "")
        note    = info.get("note", "")
        if silent:
            self._btn_update.configure(
                state=tk.NORMAL, text=f"🆕 新版本 v{new_ver}",
                bg=C["red"], fg=C["bg"])
            self._log(f"💡  发现新版本 v{new_ver}，点「新版本」按钮安装", "info")
            return
        msg = (
            f"发现新版本  v{new_ver}\n"
            f"当前版本: v{APP_VERSION}\n\n"
            f"{'更新说明: ' + note + chr(10) if note else ''}"
            "是否立即下载更新？"
        )
        if messagebox.askyesno("发现更新", msg, parent=self.root):
            self._do_update(dl_url, new_ver)

    def _do_update(self, download_url: str, new_version: str):
        if not download_url:
            messagebox.showerror("更新失败", "下载地址为空，请联系管理员配置 download_url。")
            return
        self._log(f"⬇️  正在下载 v{new_version}…", "info")
        self._btn_update.configure(state=tk.DISABLED, text="⬇️ 下载中…")
        threading.Thread(
            target=self._worker_download_update,
            args=(download_url, new_version),
            daemon=True).start()

    def _worker_download_update(self, url: str, new_version: str):
        import ssl
        ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE
        tmp_dir  = tempfile.mkdtemp(prefix="warp_login_upd_")
        zip_path = os.path.join(tmp_dir, "update.zip")
        proxy    = self._config.get("proxy", "").strip() or None
        try:
            req = Request(url, headers={"User-Agent": "WarpLoginTool/2.0"})
            if proxy:
                import urllib.request as _ur
                opener = _ur.build_opener(
                    _ur.ProxyHandler({"http": f"http://{proxy}",
                                      "https": f"http://{proxy}"}),
                    _ur.HTTPSHandler(context=ssl_ctx))
                ctx_mgr = opener.open(req, timeout=120)
            else:
                ctx_mgr = urlopen(req, timeout=120, context=ssl_ctx)
            with ctx_mgr as r:
                total = int(r.headers.get("Content-Length", 0) or 0)
                downloaded, last_pct = 0, -1
                with open(zip_path, "wb") as f:
                    while True:
                        buf = r.read(65536)
                        if not buf:
                            break
                        f.write(buf)
                        downloaded += len(buf)
                        if total > 0:
                            pct = int(downloaded * 100 / total)
                            if pct != last_pct and pct % 10 == 0:
                                self._log_q.put(f"   ⬇️  {pct}%")
                                last_pct = pct

            extract_dir = os.path.join(tmp_dir, "files")
            os.makedirs(extract_dir, exist_ok=True)
            with zipfile.ZipFile(zip_path, "r") as z:
                z.extractall(extract_dir)

            bat_path = os.path.join(SCRIPT_DIR, "do_update.bat")
            if getattr(sys, "frozen", False):
                restart_cmd = f'start "" "{sys.executable}"'
            else:
                launcher    = os.path.join(SCRIPT_DIR, os.path.basename(__file__))
                restart_cmd = f'start "" pythonw "{launcher}"'
            bat = (
                "@echo off\nchcp 65001 >nul\n"
                f"title Warp 账号切换工具 — 更新到 v{new_version}\n"
                "echo 正在等待应用退出...\ntimeout /t 2 /nobreak >nul\n"
                "echo 正在复制文件...\n"
                f'robocopy "{extract_dir}" "{SCRIPT_DIR}" /e /is /it /nfl /ndl /np >nul 2>&1\n'
                "echo 更新完成！\n"
                f"{restart_cmd}\n"
                'del "%~f0"\n'
            )
            with open(bat_path, "w", encoding="gbk") as f:
                f.write(bat)
            self._result_q.put({
                "_update_ready": True,
                "bat_path": bat_path, "new_version": new_version,
            })
        except Exception as e:
            self._result_q.put({"_update_failed": True, "msg": str(e)})

    # ══════════════════════════════════════════
    # 主线程轮询（事件调度）
    # ══════════════════════════════════════════
    def _poll(self):
        # 日志队列 → 写入 Text
        try:
            while True:
                self._log(self._log_q.get_nowait())
        except queue.Empty:
            pass

        # 结果队列 → 分发处理
        try:
            while True:
                res = self._result_q.get_nowait()

                if "_apply_done" in res:
                    self._busy = False
                    self._btn_apply.configure(state=tk.NORMAL)
                    if res.get("ok"):
                        self._set_status("● 已切换", C["green"])
                        email    = res.get("email", "")
                        local_id = res.get("local_id", "")
                        if email:
                            self._cur_email = email
                            self._lbl_ip.configure(text=self._domain_of(email), fg=C["blue"])
                            self._lbl_cur_lic.configure(
                                text=(local_id[:12] + "…") if local_id else "——",
                                fg=C["blue"])
                            self._lbl_conn.configure(text="● 已登录", fg=C["green"])
                            self._lbl_guard_lic.configure(
                                text=self._domain_of(email), fg=C["blue"])
                    else:
                        self._set_status("● 切换失败", C["red"])

                elif "_pool_remote_done" in res:
                    pool = res.get("pool")
                    if pool is not None:
                        self._pool = pool
                        self._update_tree()
                        self._update_stats()
                    self._log(res["msg"], "ok" if pool else "err")

                elif "_status_refreshed" in res:
                    logged_in  = res["logged_in"]
                    email      = res["email"]
                    local_id   = res["local_id"]
                    expiration = res["expiration"]
                    self._lbl_conn.configure(
                        text="● 已登录" if logged_in else "○ 未登录",
                        fg=C["green"] if logged_in else C["muted"])
                    if email:
                        self._cur_email = email
                        self._lbl_ip.configure(text=self._domain_of(email), fg=C["text"])
                    self._lbl_cur_lic.configure(
                        text=(local_id[:12] + "…") if local_id else "——",
                        fg=C["muted"])
                    if expiration:
                        try:
                            exp_dt = datetime.fromisoformat(expiration)
                            self._lbl_cli_ver.configure(
                                text=exp_dt.strftime("%m-%d %H:%M"), fg=C["muted"])
                        except Exception:
                            self._lbl_cli_ver.configure(
                                text=expiration[:16], fg=C["muted"])
                    self._set_status(
                        "● 已登录" if logged_in else "○ 未登录",
                        C["green"] if logged_in else C["muted"])

                elif "_credits_fetched" in res:
                    raw = res.get("raw")
                    if raw:
                        self._lbl_credits.configure(text=raw[:40], fg=C["muted"])
                        self._log(f"📊  额度查询原始返回: {raw}", "dim")
                    else:
                        remaining = res.get("remaining")
                        used      = res.get("used")
                        limit     = res.get("limit")
                        unlimited = res.get("unlimited", False)
                        next_ref  = res.get("next_refresh", "")
                        if remaining is not None:
                            if unlimited:
                                txt = "无限额度"
                                color = C["green"]
                            else:
                                txt = f"{remaining} 次剩余"
                                if used is not None:
                                    txt += f"（已用 {used}/{limit}）"
                                if next_ref:
                                    try:
                                        from datetime import datetime as _dt
                                        nr = _dt.fromisoformat(next_ref.replace("Z", "+00:00"))
                                        txt += f" 刷新: {nr.strftime('%m-%d %H:%M')}"
                                    except Exception:
                                        pass
                                color = C["green"] if int(remaining) > 5 else C["red"]
                            self._lbl_credits.configure(text=txt, fg=color)
                            self._log(f"📊  AI 额度: {txt}", "ok")

                elif "_guard_refreshed" in res:
                    email = res.get("email", "")
                    if email:
                        self._cur_email = email
                        self._lbl_ip.configure(text=self._domain_of(email), fg=C["blue"])
                        self._lbl_conn.configure(text="● 已切换", fg=C["green"])

                elif "_guard_credits" in res:
                    rem = res["_guard_credits"]
                    if isinstance(rem, int):
                        self._last_credits = rem
                        txt   = f"{rem} 次剩余（守护中）"
                        color = (C["green"] if rem > 5
                                 else C["yellow"] if rem > 0
                                 else C["red"])
                        self._lbl_credits.configure(text=txt, fg=color)

                elif "_auto_update" in res:
                    # 自动更新：直接下载，无弹窗
                    info    = res["info"]
                    new_ver = info.get("version", "?")
                    dl_url  = info.get("download_url", "")
                    self._log(f"📥  发现新版本 v{new_ver}，正在自动下载更新…", "info")
                    self._btn_update.configure(
                        state=tk.DISABLED,
                        text=f"⬇️ 自动更新 v{new_ver}",
                        bg=C["yellow"], fg=C["bg"])
                    threading.Thread(
                        target=self._worker_download_update,
                        args=(dl_url, new_ver),
                        daemon=True).start()

                elif "_update_available" in res:
                    info   = res["info"]
                    silent = res.get("_silent", True)
                    if not silent:
                        self._btn_update.configure(
                            state=tk.NORMAL, text="🔄 检查更新",
                            bg=C["overlay"], fg=C["blue"])
                    self._prompt_do_update(info, silent)

                elif "_update_no_new" in res:
                    self._btn_update.configure(
                        state=tk.NORMAL, text="✅ 已是最新",
                        bg=C["green"], fg=C["bg"])
                    self.root.after(3000, lambda: self._btn_update.configure(
                        text="🔄 检查更新", bg=C["overlay"],
                        fg=C["blue"], state=tk.NORMAL))
                    messagebox.showinfo(
                        "检查更新",
                        f"当前已是最新版本 v{APP_VERSION}",
                        parent=self.root)

                elif "_update_error" in res:
                    self._btn_update.configure(
                        state=tk.NORMAL, text="🔄 检查更新",
                        bg=C["overlay"], fg=C["blue"])
                    self._log(f"❌  检查更新失败: {res['msg']}", "err")

                elif "_update_ready" in res:
                    bat = res["bat_path"]
                    ver = res["new_version"]
                    self._log(f"✅  v{ver} 下载完成，正在重启应用…", "ok")
                    # 全自动：无需确认直接执行更新脚本并退出
                    subprocess.Popen(
                        ["cmd", "/c", bat],
                        creationflags=subprocess.CREATE_NEW_CONSOLE)
                    self.root.after(800, sys.exit)

                elif "_update_failed" in res:
                    self._btn_update.configure(
                        state=tk.NORMAL, text="🔄 检查更新",
                        bg=C["overlay"], fg=C["blue"])
                    self._log(f"❌  下载更新包失败: {res['msg']}", "err")

                elif "_push_done" in res:
                    if res.get("ok"):
                        ver = res.get("version", "?")
                        url = res.get("url", "")
                        self._log(
                            f"✅  manifest.json 推送成功！v{ver} 已发布\n   {url}", "ok")
                        messagebox.showinfo("推送成功",
                            f"v{ver} 已推送到 Gist manifest\n"
                            f"其他设备打开工具时将自动检测到新版本。",
                            parent=self.root)
                    else:
                        self._log(f"❌  manifest 推送失败: {res.get('msg', '')}", "err")

        except queue.Empty:
            pass

        # 守护自动切换信号
        try:
            while True:
                sig = self._auto_q.get_nowait()
                if sig == "switch":
                    self._do_auto_switch()
        except queue.Empty:
            pass

        self.root.after(200, self._poll)

    # ══════════════════════════════════════════
    # 关闭
    # ══════════════════════════════════════════
    def _on_close(self):
        self._guard_stop.set()
        self.root.destroy()


# ─────────────────────────────────────────────
def main():
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    root = tk.Tk()
    LoginApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
