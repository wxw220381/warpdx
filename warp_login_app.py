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
APP_VERSION = "2.0.0"
APP_NAME    = "Warp 账号切换工具"

# PyInstaller 单文件兼容
if getattr(sys, "frozen", False):
    SCRIPT_DIR = os.path.dirname(sys.executable)
else:
    SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

CONFIG_FILE  = os.path.join(SCRIPT_DIR, "update_config.json")
DEFAULT_POOL = os.path.join(SCRIPT_DIR, "output", "warp_accounts_standard.json")

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
    url  = f"https://securetoken.googleapis.com/v1/token?key={FIREBASE_API_KEY}"
    body = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }).encode()
    req = Request(url, data=body, method="POST",
                  headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        if proxy:
            import urllib.request as _ur
            opener = _ur.build_opener(
                _ur.ProxyHandler({
                    "http":  f"http://{proxy}",
                    "https": f"http://{proxy}",
                })
            )
            with opener.open(req, timeout=20) as r:
                resp = json.loads(r.read().decode())
        else:
            with urlopen(req, timeout=20) as r:
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

        self.root.after(400, self._auto_load_pool)
        self.root.after(800, self._refresh_status)

    # ══════════════════════════════════════════
    # 配置文件
    # ══════════════════════════════════════════
    def _load_config(self):
        defaults = {
            "version":      APP_VERSION,
            "manifest_url": "",
            "pool_url":     "",
            "proxy":        "",
        }
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    defaults.update(json.load(f))
            except Exception:
                pass
        self._config = defaults

    def _save_config(self):
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(self._config, f, indent=2, ensure_ascii=False)
        except Exception:
            pass

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
            list_wrap, columns=("email", "st", "uid"),
            show="headings", height=12)
        self._tree.heading("email", text="邮箱")
        self._tree.heading("st",    text="状态")
        self._tree.heading("uid",   text="Firebase UID (前12位)")
        self._tree.column("email", width=200, minwidth=140)
        self._tree.column("st",    width=60,  minwidth=44)
        self._tree.column("uid",   width=120, minwidth=80)

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

        path_row = tk.Frame(frame, bg=C["surface"])
        path_row.grid(row=4, column=0, sticky="ew", padx=12, pady=(0, 10))
        tk.Label(path_row, text="本地路径:",
                 bg=C["surface"], fg=C["muted"],
                 font=("Segoe UI", 8)).pack(side=tk.LEFT)
        self._pool_path_var = tk.StringVar(value=DEFAULT_POOL)
        tk.Entry(path_row, textvariable=self._pool_path_var,
                 bg=C["overlay"], fg=C["text"],
                 insertbackground=C["text"],
                 font=("Consolas", 8), relief="flat",
                 width=38).pack(side=tk.LEFT, padx=(4, 0),
                                fill=tk.X, expand=True)

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

        sc_btns = tk.Frame(sc, bg=C["surface"])
        sc_btns.pack(fill=tk.X, padx=12, pady=(2, 10))
        tk.Button(sc_btns, text="🔄 刷新状态",
                  bg=C["overlay"], fg=C["blue"],
                  font=("Segoe UI", 8), relief="flat",
                  padx=8, pady=3, cursor="hand2",
                  command=self._refresh_status).pack(side=tk.LEFT)
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
            con = sqlite3.connect(WARP_SQLITE)
            con.execute("UPDATE current_user_information SET email = ?", (email,))
            con.execute("UPDATE users SET firebase_uid = ?", (firebase_uid,))
            con.execute(
                "INSERT OR REPLACE INTO user_profiles "
                "(firebase_uid, email, display_name, photo_url) VALUES (?, ?, NULL, NULL)",
                (firebase_uid, email),
            )
            con.commit()
            con.close()
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
            # 只保留有 refresh_token + local_id 的 Warp 终端账号
            self._pool = [
                a for a in data
                if a.get("refresh_token") and a.get("local_id")
            ]
            self._update_tree()
            self._update_stats()
            self._log(f"✅  已加载 {len(self._pool)} 个账号  ←  {path}", "ok")
            if not self._pool:
                self._log(
                    "⚠️  账号池为空或均缺少 refresh_token/local_id。\n"
                    "    请确认账号来自 Warp 终端 IDE 注册（warp_accounts_standard.json），\n"
                    "    而非协议直连注册（warp_protocol_accounts.json）。",
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
            req = Request(url, headers={"User-Agent": "WarpLoginTool/2.0"})
            with urlopen(req, timeout=20) as r:
                data = json.loads(r.read().decode("utf-8"))
            if not isinstance(data, list):
                data = [data]
            accounts = [a for a in data if a.get("refresh_token") and a.get("local_id")]
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

    def _update_tree(self):
        self._tree.delete(*self._tree.get_children())
        icon_map = {"active": "✅", "banned": "⛔", "deleted": "🗑", "error": "❓"}
        for acc in self._pool:
            email = acc.get("email", "")
            st    = acc.get("status", "")
            uid   = acc.get("local_id", "")[:12]
            icon  = icon_map.get(st, "○")
            self._tree.insert("", tk.END,
                              values=(email, f"{icon} {st}" if st else "○ 未知", uid))

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
                  and a.get("refresh_token")), None),
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
        if not acc.get("refresh_token"):
            self._log("⚠️  该账号缺少 refresh_token（非 Warp 终端账号）", "warn")
            return
        self._busy = True
        self._btn_apply.configure(state=tk.DISABLED)
        email = acc.get("email", "")
        self._log(f"🚀  正在切换账号: {email}…", "info")
        threading.Thread(target=self._worker_apply, args=(acc,), daemon=True).start()

    def _worker_apply(self, acc: dict):
        """后台线程：完整的账号切换流程"""
        refresh_token = acc.get("refresh_token", "")
        local_id      = acc.get("local_id", "")
        email         = acc.get("email", "")
        proxy         = self._config.get("proxy", "").strip() or None

        # ① 刷新 Firebase Token
        self._log_q.put("🔄  正在刷新 Firebase Token…")
        new_id_token, new_refresh, expires_in = _firebase_refresh(refresh_token, proxy)
        if new_id_token is None:
            err_str = str(new_refresh or "")
            if "USER_DISABLED" in err_str:
                self._log_q.put("❌  账号已被封禁（USER_DISABLED）")
            elif "USER_NOT_FOUND" in err_str:
                self._log_q.put("❌  账号已删除（USER_NOT_FOUND）")
            else:
                self._log_q.put(f"❌  Token 刷新失败: {err_str[:80]}")
            self._result_q.put({"_apply_done": True, "ok": False})
            return

        # ② 构建认证 JSON（与 Warp 终端 dev.warp.Warp-User 格式一致）
        expires_in  = int(expires_in or 3600)
        expiration  = (
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

    def _worker_guard(self, interval: int):
        """守护线程：定期用 Firebase 刷新检测账号状态，异常则自动切换"""
        self._log_q.put(f"🤖  守护线程就绪（间隔 {interval}s）")
        while not self._guard_stop.wait(interval):
            try:
                auth = self._read_current_auth()
                if not auth:
                    continue
                id_tkn_obj    = auth.get("id_token") or {}
                refresh_token = id_tkn_obj.get("refresh_token", "")
                if not refresh_token:
                    continue
                proxy = self._config.get("proxy", "").strip() or None
                new_id, err, _ = _firebase_refresh(refresh_token, proxy)
                if new_id is None:
                    err_str = str(err or "")
                    # 只在账号被封/删除时才触发切换；网络错误不切换
                    if "USER_DISABLED" in err_str or "USER_NOT_FOUND" in err_str:
                        self._log_q.put(
                            f"🔄  [守护] 账号异常 ({err_str[:40]})，触发自动切换…")
                        self._auto_q.put("switch")
                        self._guard_stop.wait(interval)  # 等一个周期防重复触发
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
            if st not in ("banned", "deleted") and rt:
                self._pool_idx = idx
                self._switch_count += 1
                n     = self._switch_count
                email = acc.get("email", "")
                self._lbl_switch_count.configure(text=str(n))
                disp = email[:20] + "…" if len(email) > 20 else email
                self._lbl_guard_lic.configure(text=disp, fg=C["blue"])
                self._log(
                    f"🔄  [续杯第{n}次]  账号 {idx+1}/{total}  {email}", "info")
                threading.Thread(
                    target=self._worker_apply_guard, args=(acc,),
                    daemon=True).start()
                return
        self._log("❌  [守护] 号池无可用账号，请重新加载", "err")

    def _worker_apply_guard(self, acc: dict):
        """守护模式静默切换（不影响按钮状态）"""
        refresh_token = acc.get("refresh_token", "")
        local_id      = acc.get("local_id", "")
        email         = acc.get("email", "")
        proxy         = self._config.get("proxy", "").strip() or None

        new_id_token, new_refresh, expires_in = _firebase_refresh(refresh_token, proxy)
        if new_id_token is None:
            self._log_q.put(f"❌  [守护] Token 刷新失败: {new_refresh[:60]}")
            return

        expires_in  = int(expires_in or 3600)
        expiration  = (
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

    def _bg_check_update(self):
        try:
            info = self._fetch_manifest()
            if info and (self._ver_tuple(info.get("version", "0"))
                         > self._ver_tuple(APP_VERSION)):
                self._result_q.put({
                    "_update_available": True,
                    "info": info, "_silent": True,
                })
        except Exception:
            pass

    def _fetch_manifest(self) -> Optional[dict]:
        url = self._config.get("manifest_url", "").strip()
        if not url:
            return None
        req = Request(url, headers={"User-Agent": "WarpLoginTool/2.0"})
        with urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode("utf-8"))

    def _manual_check_update(self):
        if not self._config.get("manifest_url", "").strip():
            self._open_config_dialog()
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
        tmp_dir  = tempfile.mkdtemp(prefix="warp_login_upd_")
        zip_path = os.path.join(tmp_dir, "update.zip")
        try:
            req = Request(url, headers={"User-Agent": "WarpLoginTool/2.0"})
            with urlopen(req, timeout=60) as r:
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
                            self._lbl_ip.configure(text=email, fg=C["blue"])
                            self._lbl_cur_lic.configure(
                                text=(local_id[:12] + "…") if local_id else "——",
                                fg=C["blue"])
                            self._lbl_conn.configure(text="● 已登录", fg=C["green"])
                            disp = email[:20] + "…" if len(email) > 20 else email
                            self._lbl_guard_lic.configure(text=disp, fg=C["blue"])
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
                        self._lbl_ip.configure(text=email, fg=C["text"])
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

                elif "_guard_refreshed" in res:
                    email = res.get("email", "")
                    if email:
                        self._cur_email = email
                        self._lbl_ip.configure(text=email, fg=C["blue"])
                        self._lbl_conn.configure(text="● 已切换", fg=C["green"])

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
                    self._log(f"✅  v{ver} 下载完成，准备重启更新…", "ok")
                    if messagebox.askyesno(
                        "更新就绪",
                        f"v{ver} 已下载完成。\n需要重启应用以完成更新，立即重启？",
                        parent=self.root
                    ):
                        subprocess.Popen(
                            ["cmd", "/c", bat],
                            creationflags=subprocess.CREATE_NEW_CONSOLE)
                        self.root.after(500, sys.exit)
                    else:
                        self._btn_update.configure(
                            state=tk.NORMAL, text="🔄 检查更新",
                            bg=C["overlay"], fg=C["blue"])

                elif "_update_failed" in res:
                    self._btn_update.configure(
                        state=tk.NORMAL, text="🔄 检查更新",
                        bg=C["overlay"], fg=C["blue"])
                    self._log(f"❌  下载更新包失败: {res['msg']}", "err")

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
