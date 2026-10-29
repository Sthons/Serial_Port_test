"""串口通讯线路板测试工具 - 多列式 Tkinter 桌面界面。

布局：顶部为全局参数与总体判定，下方为可横向滚动的通道列，
每列包含该通道的串口配置、状态卡片与独立日志卡片，支持新增/删除通道。
"""

from __future__ import annotations

import queue
import sys
import threading
import tkinter as tk
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, font as tkfont
from tkinter import messagebox, ttk

import serial
from serial.tools import list_ports

from .config_store import AppSettings, ChannelSettings, load_settings, save_settings
from .protocol import (
    COM_PLACEHOLDER,
    DEFAULT_RESPONSE_TEMPLATE,
    REQUEST_FRAME,
    format_hex,
    format_response_pattern,
    parse_hex_bytes,
    parse_response_pattern,
)
from .tester import (
    STATUS_FAILED,
    STATUS_IDLE,
    STATUS_NOT_TESTED,
    STATUS_OPENING,
    STATUS_PASSED,
    STATUS_STOPPED,
    STATUS_WAITING,
    PortConfig,
    SerialTester,
    TestReport,
)

FONT_FAMILY = "Microsoft YaHei UI"
MONO_FAMILY = "Consolas"
BAUD_RATES = ("9600", "19200", "38400", "57600", "115200")

DATA_BITS_OPTIONS = ("5", "6", "7", "8")
PARITY_OPTIONS = {
    "无 (N)": serial.PARITY_NONE,
    "奇 (O)": serial.PARITY_ODD,
    "偶 (E)": serial.PARITY_EVEN,
    "Mark (M)": serial.PARITY_MARK,
    "Space (S)": serial.PARITY_SPACE,
}
STOP_BITS_OPTIONS = {
    "1": serial.STOPBITS_ONE,
    "1.5": serial.STOPBITS_ONE_POINT_FIVE,
    "2": serial.STOPBITS_TWO,
}
PARITY_LABELS = {value: label for label, value in PARITY_OPTIONS.items()}
STOP_BITS_LABELS = {
    float(value): label for label, value in STOP_BITS_OPTIONS.items()
}

PAGE_BG = "#edf1f7"
CARD_BG = "#ffffff"
CARD_BORDER = "#d6dee8"
STATUS_PANEL_BG = "#f8fafc"
TEXT_COLOR = "#111827"
MUTED_COLOR = "#6b7280"
LOG_BG = "#0f172a"
CHANNEL_WIDTH = 388
CHANNEL_AREA_HEIGHT = 600

STATUS_COLORS = {
    STATUS_IDLE: "#6b7280",
    STATUS_OPENING: "#2563eb",
    STATUS_WAITING: "#2563eb",
    STATUS_PASSED: "#15803d",
    STATUS_FAILED: "#b91c1c",
    STATUS_STOPPED: "#6b7280",
    STATUS_NOT_TESTED: "#b45309",
}

LOG_COLORS = {
    "info": "#d1d5db",
    "success": "#4ade80",
    "warning": "#fbbf24",
    "error": "#f87171",
}

# 总判断只有四种状态
VERDICT_PENDING = "待启动测试"
VERDICT_RUNNING = "测试中"
VERDICT_PASSED = "合格"
VERDICT_FAILED = "不合格"
VERDICT_COLORS = {
    VERDICT_PENDING: "#6b7280",
    VERDICT_RUNNING: "#2563eb",
    VERDICT_PASSED: "#15803d",
    VERDICT_FAILED: "#b91c1c",
}


@dataclass
class PortRow:
    """界面上单个通道列的控件集合。"""

    index: int
    card: tk.Frame
    title_label: tk.Label
    delete_btn: tk.Button
    port_var: tk.StringVar
    com_var: tk.IntVar
    baud_var: tk.StringVar
    data_bits_var: tk.StringVar
    parity_var: tk.StringVar
    stop_bits_var: tk.StringVar
    xonxoff_var: tk.BooleanVar
    rtscts_var: tk.BooleanVar
    dsrdtr_var: tk.BooleanVar
    combo: ttk.Combobox
    dot: tk.Label
    status_label: tk.Label
    response_label: tk.Label
    detail_label: tk.Label
    count_label: tk.Label
    log_text: tk.Text


class SerialTestApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.rows: list[PortRow] = []
        self.events: queue.Queue = queue.Queue()
        self.stop_event = threading.Event()
        self.worker: threading.Thread | None = None
        self._running = False
        self._port_choices: list[str] = []
        self._port_devices: list[str] = []
        self._port_labels: list[str] = []
        self._device_to_label: dict[str, str] = {}
        self._label_to_device: dict[str, str] = {}
        self._canvas_window_id: int | None = None
        self._page_window_id: int | None = None
        self._max_columns = 0

        self._configure_fonts()
        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._load_config_on_startup()
        self.refresh_ports()
        self.root.after(60, self._poll_events)

    # ------------------------------------------------------------------ 界面搭建
    def _configure_fonts(self) -> None:
        for name in (
            "TkDefaultFont",
            "TkTextFont",
            "TkHeadingFont",
            "TkMenuFont",
            "TkCaptionFont",
            "TkSmallCaptionFont",
        ):
            try:
                tkfont.nametofont(name).configure(family=FONT_FAMILY, size=10)
            except tk.TclError:
                pass

    def _build_ui(self) -> None:
        self.root.title("多通道串口通讯测试工具")
        # 按屏幕尺寸自适应初始窗口，1366x768 等小屏也不会超出可用区域
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        init_w = min(1240, max(980, screen_w - 80))
        init_h = min(900, max(560, screen_h - 120))
        self.root.geometry(f"{init_w}x{init_h}")
        self.root.minsize(980, 520)
        self.root.configure(bg=PAGE_BG)

        style = ttk.Style(self.root)
        if "vista" in style.theme_names():
            style.theme_use("vista")

        # 整页竖向滚动，适配 1366x768 及更小分辨率
        scroll_host = tk.Frame(self.root, bg=PAGE_BG)
        scroll_host.pack(fill="both", expand=True)
        self.page_canvas = tk.Canvas(
            scroll_host, bg=PAGE_BG, highlightthickness=0, bd=0
        )
        self.page_scroll = ttk.Scrollbar(
            scroll_host, orient="vertical", command=self.page_canvas.yview
        )
        self.page_canvas.configure(yscrollcommand=self.page_scroll.set)
        self.page_scroll.pack(side="right", fill="y")
        self.page_canvas.pack(side="left", fill="both", expand=True)

        page = tk.Frame(self.page_canvas, bg=PAGE_BG, padx=16, pady=14)
        self.page = page
        self._page_window_id = self.page_canvas.create_window(
            (0, 0), window=page, anchor="nw"
        )
        page.bind("<Configure>", self._on_page_configure)
        self.page_canvas.bind("<Configure>", self._on_page_canvas_configure)
        self.root.bind_all("<MouseWheel>", self._on_page_mousewheel, add="+")

        # 标题与总体判定
        header = tk.Frame(page, bg=PAGE_BG)
        header.pack(fill="x")
        title_block = tk.Frame(header, bg=PAGE_BG)
        title_block.pack(side="left")
        tk.Label(
            title_block,
            text="多通道串口通讯线路板测试",
            font=(FONT_FAMILY, 18, "bold"),
            fg=TEXT_COLOR,
            bg=PAGE_BG,
        ).pack(anchor="w")
        tk.Label(
            title_block,
            text="主机指令与期望响应可在下方全局配置；期望响应用 {COM} 表示该通道 COM 号",
            font=(FONT_FAMILY, 9),
            fg=MUTED_COLOR,
            bg=PAGE_BG,
        ).pack(anchor="w", pady=(4, 0))

        # 全局参数栏
        params = tk.Frame(
            page,
            bg=CARD_BG,
            highlightbackground=CARD_BORDER,
            highlightthickness=1,
        )
        params.pack(fill="x", pady=(12, 12))
        inner = tk.Frame(params, bg=CARD_BG, padx=14, pady=10)
        inner.pack(fill="x")

        tk.Label(inner, text="请求间隔（秒）", fg="#374151", bg=CARD_BG).pack(side="left")
        self.interval_var = tk.StringVar(value="2.0")
        ttk.Spinbox(
            inner,
            textvariable=self.interval_var,
            from_=0.1,
            to=600.0,
            increment=0.1,
            width=7,
        ).pack(side="left", padx=(6, 16))

        tk.Label(inner, text="响应等待（秒）", fg="#374151", bg=CARD_BG).pack(side="left")
        self.timeout_var = tk.StringVar(value="2.0")
        ttk.Spinbox(
            inner,
            textvariable=self.timeout_var,
            from_=0.1,
            to=120.0,
            increment=0.1,
            width=7,
        ).pack(side="left", padx=(6, 16))

        tk.Label(inner, text="目标轮次", fg="#374151", bg=CARD_BG).pack(side="left")
        self.target_rounds_var = tk.StringVar(value="0")
        ttk.Spinbox(
            inner,
            textvariable=self.target_rounds_var,
            from_=0,
            to=999999,
            increment=1,
            width=7,
        ).pack(side="left", padx=(6, 16))

        ttk.Button(inner, text="刷新串口列表", command=self.refresh_ports).pack(
            side="left"
        )
        self.add_channel_btn = ttk.Button(
            inner, text="新增通道", command=self.add_channel
        )
        self.add_channel_btn.pack(side="left", padx=(8, 0))
        ttk.Button(inner, text="导出日志", command=self.export_logs).pack(
            side="left", padx=(8, 0)
        )
        self.import_btn = ttk.Button(
            inner, text="导入配置", command=self.import_config
        )
        self.import_btn.pack(side="left", padx=(8, 0))

        self.stop_btn = tk.Button(
            inner,
            text="停止测试",
            command=self.stop_test,
            state="disabled",
            bg="#e5e7eb",
            fg="#9ca3af",
            activebackground="#e5e7eb",
            activeforeground="#9ca3af",
            disabledforeground="#9ca3af",
            relief="flat",
            padx=18,
            pady=5,
            font=(FONT_FAMILY, 10, "bold"),
        )
        self.stop_btn.pack(side="right", padx=(8, 0))
        self.start_btn = tk.Button(
            inner,
            text="开始测试",
            command=self.start_test,
            bg="#2563eb",
            fg="white",
            activebackground="#1d4ed8",
            activeforeground="white",
            disabledforeground="#e5e7eb",
            relief="flat",
            padx=20,
            pady=5,
            font=(FONT_FAMILY, 10, "bold"),
            cursor="hand2",
        )
        self.start_btn.pack(side="right")

        # 全局通讯帧配置
        frame_settings = tk.Frame(params, bg=CARD_BG)
        frame_settings.pack(fill="x", padx=14, pady=(0, 10))
        frame_settings.columnconfigure(1, weight=1)
        frame_settings.columnconfigure(3, weight=1)

        tk.Label(
            frame_settings, text="主机指令", fg="#374151", bg=CARD_BG
        ).grid(row=0, column=0, sticky="w")
        self.command_var = tk.StringVar(value=format_hex(REQUEST_FRAME))
        ttk.Entry(
            frame_settings, textvariable=self.command_var, width=30
        ).grid(row=0, column=1, sticky="ew", padx=(6, 16))
        tk.Label(
            frame_settings, text="期望响应", fg="#374151", bg=CARD_BG
        ).grid(row=0, column=2, sticky="w")
        self.response_pattern_var = tk.StringVar(
            value=format_response_pattern(DEFAULT_RESPONSE_TEMPLATE)
        )
        ttk.Entry(
            frame_settings, textvariable=self.response_pattern_var, width=34
        ).grid(row=0, column=3, sticky="ew", padx=(6, 0))
        tk.Label(
            frame_settings,
            text=(
                f"插槽格式：{COM_PLACEHOLDER} = 该通道期望 COM 号（1 字节）；"
                "例如 FA FB FC FD 00 {COM} FE FF；目标轮次 0 表示不限轮次"
            ),
            font=(FONT_FAMILY, 8),
            fg=MUTED_COLOR,
            bg=CARD_BG,
        ).grid(row=1, column=0, columnspan=4, sticky="w", pady=(4, 0))

        # 通道上方的总判断（仅四种状态）
        verdict_panel = tk.Frame(
            page,
            bg=CARD_BG,
            highlightbackground=CARD_BORDER,
            highlightthickness=1,
        )
        verdict_panel.pack(fill="x", pady=(0, 12))
        verdict_inner = tk.Frame(verdict_panel, bg=CARD_BG, padx=16, pady=10)
        verdict_inner.pack(fill="x")
        tk.Label(
            verdict_inner,
            text="总判断",
            font=(FONT_FAMILY, 10, "bold"),
            fg="#374151",
            bg=CARD_BG,
        ).pack(side="left")
        self.verdict_dot = tk.Label(
            verdict_inner,
            text="●",
            font=(FONT_FAMILY, 15),
            fg=VERDICT_COLORS[VERDICT_PENDING],
            bg=CARD_BG,
        )
        self.verdict_dot.pack(side="left", padx=(14, 0))
        self.verdict_label = tk.Label(
            verdict_inner,
            text=VERDICT_PENDING,
            font=(FONT_FAMILY, 16, "bold"),
            fg=VERDICT_COLORS[VERDICT_PENDING],
            bg=CARD_BG,
        )
        self.verdict_label.pack(side="left", padx=(8, 0))
        self.verdict_detail_label = tk.Label(
            verdict_inner,
            text="尚未开始测试",
            font=(FONT_FAMILY, 9),
            fg=MUTED_COLOR,
            bg=CARD_BG,
        )
        self.verdict_detail_label.pack(side="right")

        # 可横向滚动的通道列
        columns_wrap = tk.Frame(page, bg=PAGE_BG)
        columns_wrap.pack(fill="both", expand=True)
        self.columns_canvas = tk.Canvas(
            columns_wrap,
            bg=PAGE_BG,
            highlightthickness=0,
            bd=0,
            height=CHANNEL_AREA_HEIGHT,
        )
        self.columns_scroll = ttk.Scrollbar(
            columns_wrap, orient="horizontal", command=self.columns_canvas.xview
        )
        self.columns_canvas.configure(xscrollcommand=self.columns_scroll.set)
        self.columns_canvas.pack(side="top", fill="both", expand=True)
        self.columns_scroll.pack(side="bottom", fill="x")

        self.columns_frame = tk.Frame(self.columns_canvas, bg=PAGE_BG)
        self.columns_frame.rowconfigure(0, weight=1)
        self._canvas_window_id = self.columns_canvas.create_window(
            (0, 0), window=self.columns_frame, anchor="nw"
        )
        self.columns_frame.bind("<Configure>", self._on_columns_frame_configure)
        self.columns_canvas.bind("<Configure>", self._on_canvas_configure)

        for index in range(3):
            self._create_channel(port=f"COM{index + 1}", expected_com=index + 1)
        self._update_channel_buttons()

    def _build_column(
        self, card: tk.Frame, index: int, port: str, expected_com: int
    ) -> None:
        body = tk.Frame(card, bg=CARD_BG, padx=14, pady=12)
        body.pack(fill="both", expand=True)

        # 列标题 + 状态指示灯 + 删除按钮
        head = tk.Frame(body, bg=CARD_BG)
        head.pack(fill="x")
        dot = tk.Label(
            head,
            text="●",
            font=(FONT_FAMILY, 11),
            fg=STATUS_COLORS[STATUS_IDLE],
            bg=CARD_BG,
        )
        dot.pack(side="left")
        title_label = tk.Label(
            head,
            text=f"通道 {index + 1}",
            font=(FONT_FAMILY, 12, "bold"),
            fg=TEXT_COLOR,
            bg=CARD_BG,
        )
        title_label.pack(side="left", padx=(6, 0))
        delete_btn = tk.Button(
            head,
            text="删除",
            bg=CARD_BG,
            fg="#b91c1c",
            activebackground="#fee2e2",
            activeforeground="#b91c1c",
            disabledforeground="#d1d5db",
            relief="flat",
            font=(FONT_FAMILY, 9),
            cursor="hand2",
            padx=4,
            pady=0,
        )
        delete_btn.pack(side="right")

        tk.Frame(body, bg="#e8edf4", height=1).pack(fill="x", pady=(8, 10))

        # 串口配置
        config_frame = tk.Frame(body, bg=CARD_BG)
        config_frame.pack(fill="x")
        config_frame.columnconfigure(0, weight=1, uniform="cfg")
        config_frame.columnconfigure(1, weight=1, uniform="cfg")

        def add_label(text: str, row: int, column: int) -> None:
            tk.Label(
                config_frame,
                text=text,
                font=(FONT_FAMILY, 9),
                fg=MUTED_COLOR,
                bg=CARD_BG,
            ).grid(
                row=row,
                column=column,
                sticky="w",
                padx=(0 if column == 0 else 10, 0),
                pady=(8 if row > 1 else 0, 0),
            )

        add_label("串口", 0, 0)
        add_label("波特率", 0, 1)
        display_port = self._device_to_label.get(port.strip().upper(), port)
        port_var = tk.StringVar(value=display_port)
        combo = ttk.Combobox(
            config_frame,
            textvariable=port_var,
            values=self._port_choices,
            width=18,
            state="normal",
        )
        combo.grid(row=1, column=0, sticky="ew", pady=(2, 0))
        combo.configure(
            postcommand=lambda: self._widen_combo_popdown(combo)
        )
        # Post 会先按输入框宽度重置列表宽度，需在弹窗出现后再加宽
        combo.bind(
            "<Button-1>",
            lambda _event, c=combo: self.root.after(
                20, lambda: self._widen_combo_popdown(c)
            ),
            add="+",
        )
        baud_var = tk.StringVar(value="115200")
        ttk.Combobox(
            config_frame,
            textvariable=baud_var,
            values=BAUD_RATES,
            width=9,
            state="normal",
        ).grid(row=1, column=1, sticky="ew", padx=(10, 0), pady=(2, 0))

        add_label("期望 COM 号", 2, 0)
        add_label("数据位", 2, 1)
        com_var = tk.IntVar(value=expected_com)
        ttk.Spinbox(
            config_frame,
            from_=0,
            to=255,
            increment=1,
            width=5,
            textvariable=com_var,
        ).grid(row=3, column=0, sticky="w", pady=(2, 0))
        data_bits_var = tk.StringVar(value="8")
        ttk.Combobox(
            config_frame,
            textvariable=data_bits_var,
            values=DATA_BITS_OPTIONS,
            width=9,
            state="readonly",
        ).grid(row=3, column=1, sticky="ew", padx=(10, 0), pady=(2, 0))

        add_label("校验位", 4, 0)
        add_label("停止位", 4, 1)
        parity_var = tk.StringVar(value="无 (N)")
        ttk.Combobox(
            config_frame,
            textvariable=parity_var,
            values=list(PARITY_OPTIONS),
            width=9,
            state="readonly",
        ).grid(row=5, column=0, sticky="ew", pady=(2, 0))
        stop_bits_var = tk.StringVar(value="1")
        ttk.Combobox(
            config_frame,
            textvariable=stop_bits_var,
            values=list(STOP_BITS_OPTIONS),
            width=9,
            state="readonly",
        ).grid(row=5, column=1, sticky="ew", padx=(10, 0), pady=(2, 0))

        add_label("流控（可多选）", 6, 0)
        flow_frame = tk.Frame(config_frame, bg=CARD_BG)
        flow_frame.grid(row=7, column=0, columnspan=2, sticky="w", pady=(2, 0))
        xonxoff_var = tk.BooleanVar(value=False)
        rtscts_var = tk.BooleanVar(value=False)
        dsrdtr_var = tk.BooleanVar(value=False)
        for variable, text in (
            (xonxoff_var, "XON/XOFF"),
            (rtscts_var, "RTS/CTS"),
            (dsrdtr_var, "DSR/DTR"),
        ):
            tk.Checkbutton(
                flow_frame,
                text=text,
                variable=variable,
                bg=CARD_BG,
                activebackground=CARD_BG,
                activeforeground=TEXT_COLOR,
                selectcolor="#eef2f7",
                font=(FONT_FAMILY, 9),
                padx=0,
                pady=0,
                anchor="w",
            ).pack(side="left", padx=(0, 8))

        sync_btn = ttk.Button(config_frame, text="同步至其他")
        sync_btn.grid(row=8, column=0, columnspan=2, sticky="ew", pady=(10, 0))

        # 状态卡片
        status_card = tk.Frame(
            body,
            bg=STATUS_PANEL_BG,
            highlightbackground="#e3e9f1",
            highlightthickness=1,
        )
        status_card.pack(fill="x", pady=(12, 0))
        status_inner = tk.Frame(status_card, bg=STATUS_PANEL_BG, padx=10, pady=8)
        status_inner.pack(fill="x")
        status_inner.columnconfigure(0, weight=1)

        tk.Label(
            status_inner,
            text="状态",
            font=(FONT_FAMILY, 8),
            fg=MUTED_COLOR,
            bg=STATUS_PANEL_BG,
        ).grid(row=0, column=0, sticky="w")
        status_label = tk.Label(
            status_inner,
            text=STATUS_IDLE,
            font=(FONT_FAMILY, 14, "bold"),
            fg=STATUS_COLORS[STATUS_IDLE],
            bg=STATUS_PANEL_BG,
            anchor="w",
        )
        status_label.grid(row=1, column=0, sticky="w")
        count_label = tk.Label(
            status_inner,
            text="帧数 0",
            font=(MONO_FAMILY, 9),
            fg=MUTED_COLOR,
            bg=STATUS_PANEL_BG,
            anchor="e",
        )
        count_label.grid(row=1, column=1, sticky="e")

        response_label = tk.Label(
            status_inner,
            text="响应帧：—",
            font=(MONO_FAMILY, 9),
            fg=MUTED_COLOR,
            bg=STATUS_PANEL_BG,
            anchor="w",
            justify="left",
        )
        response_label.grid(row=2, column=0, columnspan=2, sticky="w", pady=(6, 0))
        detail_label = tk.Label(
            status_inner,
            text="说明：等待开始",
            font=(FONT_FAMILY, 9),
            fg=MUTED_COLOR,
            bg=STATUS_PANEL_BG,
            anchor="w",
            justify="left",
        )
        detail_label.grid(row=3, column=0, columnspan=2, sticky="w", pady=(2, 0))
        self._bind_wraplength(status_card, response_label, detail_label)

        # 通道独立日志卡片
        log_card = tk.Frame(body, bg=CARD_BG)
        log_card.pack(fill="both", expand=True, pady=(12, 0))
        tk.Label(
            log_card,
            text="运行日志",
            font=(FONT_FAMILY, 9, "bold"),
            fg="#374151",
            bg=CARD_BG,
        ).pack(anchor="w", pady=(0, 4))
        log_wrap = tk.Frame(log_card, bg=LOG_BG)
        log_wrap.pack(fill="both", expand=True)
        log_text = tk.Text(
            log_wrap,
            width=30,
            height=8,
            state="disabled",
            wrap="word",
            relief="flat",
            bg=LOG_BG,
            fg=LOG_COLORS["info"],
            insertbackground=LOG_COLORS["info"],
            font=(MONO_FAMILY, 9),
            padx=8,
            pady=6,
        )
        scrollbar = ttk.Scrollbar(log_wrap, orient="vertical", command=log_text.yview)
        log_text.configure(yscrollcommand=scrollbar.set)
        log_text.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        for tag, color in LOG_COLORS.items():
            log_text.tag_configure(tag, foreground=color)

        row = PortRow(
            index=index,
            card=card,
            title_label=title_label,
            delete_btn=delete_btn,
            port_var=port_var,
            com_var=com_var,
            baud_var=baud_var,
            data_bits_var=data_bits_var,
            parity_var=parity_var,
            stop_bits_var=stop_bits_var,
            xonxoff_var=xonxoff_var,
            rtscts_var=rtscts_var,
            dsrdtr_var=dsrdtr_var,
            combo=combo,
            dot=dot,
            status_label=status_label,
            response_label=response_label,
            detail_label=detail_label,
            count_label=count_label,
            log_text=log_text,
        )
        self.rows.append(row)
        combo.bind("<<ComboboxSelected>>", lambda _event, r=row: self._sync_expected_com(r))
        delete_btn.configure(command=lambda r=row: self.remove_channel(r))
        sync_btn.configure(command=lambda r=row: self._sync_channel_settings(r))

    @staticmethod
    def _bind_wraplength(container: tk.Widget, *labels: tk.Label) -> None:
        state = {"width": 0}

        def on_configure(event: tk.Event) -> None:
            width = max(event.width - 28, 150)
            if abs(width - state["width"]) < 4:
                return
            state["width"] = width
            for label in labels:
                label.configure(wraplength=width)

        container.bind("<Configure>", on_configure)

    # ------------------------------------------------------------------ 通道增删
    def _create_channel(self, port: str, expected_com: int) -> PortRow:
        card = tk.Frame(
            self.columns_frame,
            bg=CARD_BG,
            highlightbackground=CARD_BORDER,
            highlightthickness=1,
        )
        self._build_column(card, len(self.rows), port, expected_com)
        self._relayout_columns()
        return self.rows[-1]

    def add_channel(self) -> None:
        if self._running:
            return
        # 新增前自动重新扫描串口，避免依赖手动刷新
        self.refresh_ports(silent=True)
        used = {self._port_device(row.port_var.get()) for row in self.rows}
        port = next(
            (item for item in self._port_devices if item.upper() not in used), ""
        )
        if not port:
            counter = len(self.rows) + 1
            while f"COM{counter}" in used:
                counter += 1
            port = f"COM{counter}"
        digits = "".join(ch for ch in self._port_device(port) if ch.isdigit())
        expected = (
            int(digits)
            if digits and 1 <= int(digits) <= 0xFF
            else len(self.rows) + 1
        )
        row = self._create_channel(port, expected)
        display_port = self._device_to_label.get(port.upper(), port)
        self._append_log(f"已新增通道 {row.index + 1}（{display_port}）", "info")
        self.root.update_idletasks()
        self.columns_canvas.xview_moveto(1.0)

    def remove_channel(self, row: PortRow) -> None:
        if self._running or len(self.rows) <= 1 or row not in self.rows:
            return
        row.card.destroy()
        self.rows.remove(row)
        self._relayout_columns()

    def _relayout_columns(self) -> None:
        for index, row in enumerate(self.rows):
            row.index = index
            row.title_label.config(text=f"通道 {index + 1}")
            row.card.grid(
                row=0, column=index, sticky="nsew", padx=(0, 8), pady=(0, 0)
            )
            self.columns_frame.columnconfigure(index, minsize=CHANNEL_WIDTH, weight=0)
        for index in range(len(self.rows), self._max_columns):
            self.columns_frame.columnconfigure(index, minsize=0, weight=0)
        self._max_columns = max(self._max_columns, len(self.rows))
        self._update_canvas_window()
        self._update_channel_buttons()

    def _update_channel_buttons(self) -> None:
        self.add_channel_btn.config(state="disabled" if self._running else "normal")
        self.import_btn.config(state="disabled" if self._running else "normal")
        for row in self.rows:
            state = "disabled" if self._running or len(self.rows) <= 1 else "normal"
            row.delete_btn.config(state=state)

    def _update_canvas_window(self) -> None:
        if self._canvas_window_id is None:
            return
        width = max(
            self.columns_canvas.winfo_width(), len(self.rows) * CHANNEL_WIDTH, 1
        )
        height = max(self.columns_canvas.winfo_height(), CHANNEL_AREA_HEIGHT)
        self.columns_canvas.itemconfigure(
            self._canvas_window_id, width=width, height=height
        )

    def _on_canvas_configure(self, event: tk.Event) -> None:
        if self._canvas_window_id is None:
            return
        width = max(event.width, len(self.rows) * CHANNEL_WIDTH, 1)
        self.columns_canvas.itemconfigure(
            self._canvas_window_id, width=width, height=max(event.height, CHANNEL_AREA_HEIGHT)
        )

    def _on_columns_frame_configure(self, _event: tk.Event) -> None:
        self.columns_canvas.configure(scrollregion=self.columns_canvas.bbox("all"))

    def _on_page_configure(self, _event: tk.Event | None = None) -> None:
        self.page_canvas.configure(scrollregion=self.page_canvas.bbox("all"))

    def _on_page_canvas_configure(self, event: tk.Event) -> None:
        if self._page_window_id is None:
            return
        # 内容比可视区矮时铺满，比可视区高时保持内容高度以触发竖向滚动
        height = max(event.height, self.page.winfo_reqheight())
        self.page_canvas.itemconfigure(
            self._page_window_id, width=event.width, height=height
        )

    def _on_page_mousewheel(self, event: tk.Event) -> None:
        if self._wheel_belongs_to_child(event):
            return
        self.page_canvas.yview_scroll(int(-event.delta / 120), "units")

    def _wheel_belongs_to_child(self, event: tk.Event) -> bool:
        """日志文本框与下拉弹窗自己处理滚轮，避免整页跟着滚动。"""
        widget = self.root.winfo_containing(event.x_root, event.y_root)
        while widget is not None:
            if isinstance(widget, tk.Text):
                return True
            if widget.winfo_toplevel() is not self.root:
                return True
            widget = widget.master
        return False

    # ------------------------------------------------------------------ 状态渲染
    def _render_row(
        self,
        row: PortRow,
        status: str,
        detail: str,
        count: int,
        response: bytes | None = None,
        round_number: int = 0,
    ) -> None:
        color = STATUS_COLORS.get(status, MUTED_COLOR)
        row.dot.config(fg=color)
        row.status_label.config(text=status, fg=color)
        round_text = f"第 {round_number} 轮 · " if round_number else ""
        row.count_label.config(text=f"{round_text}帧数 {count}")
        if response:
            row.response_label.config(text=f"响应帧：{format_hex(response)}", fg=TEXT_COLOR)
        else:
            row.response_label.config(text="响应帧：—", fg=MUTED_COLOR)
        row.detail_label.config(
            text=f"说明：{detail}" if detail else "说明：—",
            fg="#b91c1c" if status == STATUS_FAILED else MUTED_COLOR,
        )

    def _sync_expected_com(self, row: PortRow) -> None:
        digits = "".join(
            ch for ch in self._port_device(row.port_var.get()) if ch.isdigit()
        )
        if not digits:
            return
        value = int(digits)
        if 0 <= value <= 0xFF:
            row.com_var.set(value)

    @staticmethod
    def _flow_text(row: PortRow) -> str:
        flows = [
            name
            for name, enabled in (
                ("XON/XOFF", row.xonxoff_var.get()),
                ("RTS/CTS", row.rtscts_var.get()),
                ("DSR/DTR", row.dsrdtr_var.get()),
            )
            if enabled
        ]
        return "+".join(flows) if flows else "无流控"

    @staticmethod
    def _program_dir() -> Path:
        if getattr(sys, "frozen", False):
            return Path(sys.executable).resolve().parent
        return Path(__file__).resolve().parent.parent

    def _export_log_dir(self) -> Path:
        """日志固定导出到程序目录下的 logs 文件夹。"""
        return self._program_dir() / "logs"

    def _config_path(self) -> Path:
        return self._program_dir() / "config.json"

    def export_logs(self) -> None:
        """按通道拆分导出当前日志到程序目录 logs 下。"""
        directory = self._export_log_dir()
        exported_at = datetime.now()
        timestamp = exported_at.strftime("%Y%m%d_%H%M%S")
        written: list[Path] = []
        try:
            directory.mkdir(parents=True, exist_ok=True)
            for row in self.rows:
                port = row.port_var.get().strip().upper() or "未指定"
                safe_port = "".join(
                    ch if ch.isalnum() or ch in "-_" else "_" for ch in port
                )
                filename = f"通道{row.index + 1}_{safe_port}_{timestamp}.log"
                path = directory / filename
                content = row.log_text.get("1.0", "end-1c").strip()
                header = (
                    f"通道 {row.index + 1}｜串口 {port}\n"
                    f"波特率 {row.baud_var.get()}｜数据位 {row.data_bits_var.get()}｜"
                    f"校验 {row.parity_var.get()}｜停止位 {row.stop_bits_var.get()}｜"
                    f"流控 {self._flow_text(row)}\n"
                    f"当前状态 {row.status_label.cget('text')}｜"
                    f"{row.count_label.cget('text')}\n"
                    f"导出时间 {exported_at.strftime('%Y-%m-%d %H:%M:%S')}\n"
                    f"{'-' * 60}\n"
                )
                path.write_text(
                    header + (content if content else "（无日志）") + "\n",
                    encoding="utf-8-sig",
                )
                written.append(path)
        except OSError as exc:
            messagebox.showerror(
                "导出失败",
                f"无法写入程序目录：\n{directory}\n\n{exc}\n\n"
                "请将程序移动到可写目录（如 D 盘）后重试。",
                parent=self.root,
            )
            return
        self._append_log(
            f"日志已导出：{len(written)} 个通道文件保存至 {directory}", "info"
        )
        messagebox.showinfo(
            "导出完成",
            f"已导出 {len(written)} 个通道日志到：\n{directory}",
            parent=self.root,
        )

    def _collect_settings(self) -> AppSettings:
        """从当前界面收集配置，供保存或导入后回写。"""
        self._parse_frame_settings()  # 校验主机指令与期望响应格式
        request_interval = float(self.interval_var.get())
        response_timeout = float(self.timeout_var.get())
        try:
            target_rounds = int(self.target_rounds_var.get())
        except (tk.TclError, ValueError):
            raise ValueError("目标轮次必须是 0 或正整数") from None
        if target_rounds < 0:
            raise ValueError("目标轮次必须是 0 或正整数")
        channels: list[ChannelSettings] = []
        seen: set[str] = set()
        for row in self.rows:
            port = self._port_device(row.port_var.get())
            if not port:
                raise ValueError(f"通道 {row.index + 1} 未填写串口名称")
            if port in seen:
                raise ValueError(f"串口 {port} 重复")
            seen.add(port)
            try:
                expected_com = int(row.com_var.get())
                baudrate = int(row.baud_var.get())
                bytesize = int(row.data_bits_var.get())
            except (tk.TclError, ValueError):
                raise ValueError(f"通道 {row.index + 1} 的串口参数无效") from None
            parity = PARITY_OPTIONS.get(row.parity_var.get())
            stopbits = STOP_BITS_OPTIONS.get(row.stop_bits_var.get())
            if parity is None or stopbits is None:
                raise ValueError(f"通道 {row.index + 1} 的串口参数无效")
            channels.append(
                ChannelSettings(
                    port=port,
                    expected_com=expected_com,
                    baudrate=baudrate,
                    bytesize=bytesize,
                    parity=parity,
                    stopbits=float(stopbits),
                    xonxoff=row.xonxoff_var.get(),
                    rtscts=row.rtscts_var.get(),
                    dsrdtr=row.dsrdtr_var.get(),
                )
            )
        return AppSettings(
            request_interval=request_interval,
            response_timeout=response_timeout,
            command=self.command_var.get().strip(),
            response_pattern=self.response_pattern_var.get().strip(),
            channels=channels,
            target_rounds=target_rounds,
        )

    def _save_current_config(self) -> Path:
        settings = self._collect_settings()
        path = self._config_path()
        save_settings(path, settings)
        return path

    def _clear_channels(self) -> None:
        for row in self.rows:
            row.card.destroy()
        self.rows.clear()
        for index in range(self._max_columns):
            self.columns_frame.columnconfigure(index, minsize=0, weight=0)
        self._max_columns = 0
        self._update_canvas_window()

    def _apply_settings(self, settings: AppSettings, source: str) -> None:
        """把配置应用到界面（会重建全部通道列）。"""
        self.interval_var.set(f"{settings.request_interval:g}")
        self.timeout_var.set(f"{settings.response_timeout:g}")
        self.target_rounds_var.set(str(settings.target_rounds))
        self.command_var.set(settings.command)
        self.response_pattern_var.set(settings.response_pattern)

        self._clear_channels()
        for channel in settings.channels:
            row = self._create_channel(channel.port, channel.expected_com)
            row.baud_var.set(str(channel.baudrate))
            row.data_bits_var.set(str(channel.bytesize))
            row.parity_var.set(PARITY_LABELS[channel.parity])
            row.stop_bits_var.set(STOP_BITS_LABELS[float(channel.stopbits)])
            row.xonxoff_var.set(channel.xonxoff)
            row.rtscts_var.set(channel.rtscts)
            row.dsrdtr_var.set(channel.dsrdtr)
        self._relayout_columns()
        self._set_verdict(VERDICT_PENDING, "配置已更新，尚未开始测试")
        self._append_log(f"{source}（{len(settings.channels)} 个通道）", "info")

    def _load_config_on_startup(self) -> None:
        path = self._config_path()
        if not path.exists():
            return
        try:
            settings = load_settings(path)
        except Exception as exc:  # noqa: BLE001 - 配置损坏时退回默认值
            self._append_log(f"配置读取失败，使用默认配置：{exc}", "error")
            return
        self._apply_settings(settings, f"已加载配置 {path}")

    def import_config(self) -> None:
        """选择 JSON 配置导入，应用后覆盖本地 config.json。"""
        if self._running:
            return
        selected = filedialog.askopenfilename(
            title="选择配置文件",
            parent=self.root,
            filetypes=[("JSON 配置文件", "*.json"), ("所有文件", "*.*")],
        )
        if not selected:
            return
        try:
            settings = load_settings(Path(selected))
        except Exception as exc:  # noqa: BLE001 - 展示校验失败原因
            messagebox.showerror("导入失败", f"配置文件无效：{exc}", parent=self.root)
            return

        self._apply_settings(settings, f"已导入配置 {selected}")
        self.refresh_ports(silent=True)
        try:
            save_settings(self._config_path(), settings)
        except OSError as exc:
            messagebox.showerror(
                "保存失败",
                f"配置已应用，但无法覆盖本地配置文件：\n{exc}",
                parent=self.root,
            )
            return
        self._append_log(f"配置文件已覆盖：{self._config_path()}", "info")
        messagebox.showinfo(
            "导入完成",
            f"已导入 {len(settings.channels)} 个通道配置，并覆盖本地配置文件。",
            parent=self.root,
        )

    def _sync_channel_settings(self, row: PortRow) -> None:
        """把当前通道的波特率与串口格式同步到其他通道。"""
        values = (
            row.baud_var.get(),
            row.data_bits_var.get(),
            row.parity_var.get(),
            row.stop_bits_var.get(),
            row.xonxoff_var.get(),
            row.rtscts_var.get(),
            row.dsrdtr_var.get(),
        )
        for other in self.rows:
            if other is row:
                continue
            other.baud_var.set(values[0])
            other.data_bits_var.set(values[1])
            other.parity_var.set(values[2])
            other.stop_bits_var.set(values[3])
            other.xonxoff_var.set(values[4])
            other.rtscts_var.set(values[5])
            other.dsrdtr_var.set(values[6])
        flows = self._flow_text(row)
        self._append_log(
            f"通道 {row.index + 1} 的串口参数已同步至其他通道："
            f"{values[0]}｜数据位 {values[1]}｜{values[2]}｜停止位 {values[3]}｜{flows}",
            "info",
        )

    # ------------------------------------------------------------------ 测试流程
    def _parse_frame_settings(self) -> tuple[bytes, tuple[int | None, ...]]:
        """解析全局主机指令与带插槽的期望响应模板。"""
        request_frame = parse_hex_bytes(self.command_var.get())
        response_template = parse_response_pattern(self.response_pattern_var.get())
        return request_frame, response_template

    def start_test(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        try:
            request_interval = float(self.interval_var.get())
            if request_interval <= 0:
                raise ValueError("请求间隔必须大于 0 秒")
            response_timeout = float(self.timeout_var.get())
            if response_timeout <= 0:
                raise ValueError("响应等待时间必须大于 0 秒")
            if response_timeout > request_interval:
                raise ValueError("响应等待时间不能大于请求间隔，否则无法按间隔发送")
            try:
                target_rounds = int(self.target_rounds_var.get())
            except (tk.TclError, ValueError):
                raise ValueError("目标轮次必须是 0 或正整数") from None
            if target_rounds < 0:
                raise ValueError("目标轮次必须是 0 或正整数")
            max_cycles = target_rounds if target_rounds > 0 else None
            request_frame, response_template = self._parse_frame_settings()
            configs: list[PortConfig] = []
            seen: set[str] = set()
            for row in self.rows:
                port = self._port_device(row.port_var.get())
                if not port:
                    raise ValueError(f"通道 {row.index + 1} 未填写串口名称")
                if port in seen:
                    raise ValueError(f"串口 {port} 重复，各通道必须选择不同串口")
                seen.add(port)
                try:
                    baudrate = int(row.baud_var.get())
                except (tk.TclError, ValueError):
                    raise ValueError(
                        f"通道 {row.index + 1} 的波特率必须是正整数"
                    ) from None
                if baudrate <= 0:
                    raise ValueError(f"通道 {row.index + 1} 的波特率必须为正整数")
                try:
                    expected = int(row.com_var.get())
                except (tk.TclError, ValueError):
                    raise ValueError(
                        f"通道 {row.index + 1} 的期望 COM 号必须是 0~255 的整数"
                    ) from None
                if not 0 <= expected <= 0xFF:
                    raise ValueError(f"通道 {row.index + 1} 的期望 COM 号必须在 0~255 之间")
                try:
                    bytesize = int(row.data_bits_var.get())
                    if bytesize not in (5, 6, 7, 8):
                        raise ValueError
                    parity = PARITY_OPTIONS[row.parity_var.get()]
                    stopbits = STOP_BITS_OPTIONS[row.stop_bits_var.get()]
                except (KeyError, ValueError):
                    raise ValueError(
                        f"通道 {row.index + 1} 的串口参数无效，请用下拉框重新选择"
                    ) from None
                configs.append(
                    PortConfig(
                        port=port,
                        baudrate=baudrate,
                        expected_com=expected,
                        bytesize=bytesize,
                        parity=parity,
                        stopbits=stopbits,
                        xonxoff=row.xonxoff_var.get(),
                        rtscts=row.rtscts_var.get(),
                        dsrdtr=row.dsrdtr_var.get(),
                    )
                )
        except ValueError as exc:
            messagebox.showerror("参数有误", str(exc), parent=self.root)
            return

        self.stop_event.clear()
        self._clear_logs()
        for row in self.rows:
            self._render_row(row, STATUS_IDLE, "等待开始", 0, None)
        try:
            config_path = self._save_current_config()
            self._append_log(f"配置已保存：{config_path}", "info")
        except Exception as exc:  # noqa: BLE001 - 保存失败不阻止测试
            self._append_log(f"配置保存失败：{exc}", "warning")
        self._append_log(
            f"开始测试：指令 {format_hex(request_frame)}｜"
            f"期望响应 {format_response_pattern(response_template)}｜"
            f"{len(configs)} 个通道｜请求间隔 {request_interval:.1f} 秒｜"
            f"响应等待 {response_timeout:.1f} 秒｜"
            f"目标轮次 {target_rounds if target_rounds else '不限'}",
            "info",
        )
        self._set_verdict(VERDICT_RUNNING, "正在打开串口并发送第 1 轮")
        self._set_running(True)
        self.worker = threading.Thread(
            target=self._run_test,
            args=(
                configs,
                request_interval,
                response_timeout,
                request_frame,
                response_template,
                max_cycles,
            ),
            daemon=True,
        )
        self.worker.start()

    def _run_test(
        self,
        configs: list[PortConfig],
        request_interval: float,
        response_timeout: float,
        request_frame: bytes,
        response_template: tuple[int | None, ...],
        max_cycles: int | None,
    ) -> None:
        try:
            SerialTester().run(
                configs,
                request_interval=request_interval,
                response_timeout=response_timeout,
                request_frame=request_frame,
                response_template=response_template,
                max_cycles=max_cycles,
                on_event=self.events.put,
                stop_event=self.stop_event,
            )
        except Exception as exc:  # noqa: BLE001 - 防止后台线程静默崩溃
            self.events.put(
                {"type": "log", "level": "error", "message": f"测试线程异常：{exc}"}
            )
            self.events.put({"type": "worker_error"})

    def stop_test(self) -> None:
        if not (self.worker and self.worker.is_alive()):
            return
        self.stop_event.set()
        self.stop_btn.config(state="disabled", bg="#e5e7eb", fg="#9ca3af")
        self._append_log("已请求停止测试，等待监听线程退出…", "warning")

    # ------------------------------------------------------------------ 事件处理
    def _poll_events(self) -> None:
        try:
            while True:
                self._handle_event(self.events.get_nowait())
        except queue.Empty:
            pass
        try:
            self.root.after(60, self._poll_events)
        except tk.TclError:
            pass

    def _handle_event(self, event: dict) -> None:
        kind = event.get("type")
        if kind == "log":
            self._append_log(
                event.get("message", ""),
                event.get("level", "info"),
                event.get("index"),
            )
        elif kind == "port":
            index = event.get("index")
            if isinstance(index, int) and 0 <= index < len(self.rows):
                status = event.get("status", STATUS_IDLE)
                self._render_row(
                    self.rows[index],
                    status,
                    event.get("detail", ""),
                    event.get("count", 0),
                    event.get("response"),
                    event.get("round", 0),
                )
                if status == STATUS_FAILED:
                    # 任意通道失败即总判断不合格，不必等本轮收尾
                    port = self._port_device(self.rows[index].port_var.get())
                    round_no = event.get("round", 0)
                    detail = (
                        f"第 {round_no} 轮 · {port} 失败"
                        if round_no
                        else f"{port} 失败"
                    )
                    self._set_verdict(VERDICT_FAILED, detail)
        elif kind == "cycle":
            round_no = event.get("round", 0)
            self._set_verdict(VERDICT_RUNNING, f"第 {round_no} 轮进行中")
        elif kind == "report":
            self._handle_report(event["report"])
        elif kind == "worker_error":
            self._set_verdict(VERDICT_FAILED, "测试线程异常")
            self._set_running(False)

    def _handle_report(self, report: TestReport) -> None:
        for result in report.results:
            if 0 <= result.index < len(self.rows):
                self._render_row(
                    self.rows[result.index],
                    result.status,
                    result.detail,
                    result.response_count,
                    result.response,
                    result.cycle_number,
                )
        total = len(report.results)
        if report.aborted_reason:
            self._set_verdict(VERDICT_FAILED, report.aborted_reason)
        elif report.passed:
            detail = f"{total} 通道 · {report.cycles_completed} 轮"
            if report.stopped:
                detail += "（手动停止）"
            self._set_verdict(VERDICT_PASSED, detail)
        elif report.stopped:
            # 没有失败，但一轮都未完成：仍未形成合格结论
            self._set_verdict(
                VERDICT_PENDING,
                f"已手动停止，完成 {report.cycles_completed} 轮",
            )
        else:
            failed_ports = [
                result.config.port
                for result in report.results
                if result.status == STATUS_FAILED
            ]
            if failed_ports:
                detail = f"第 {report.failed_round} 轮 · {'、'.join(failed_ports)} 失败"
            else:
                detail = report.failure_reason or f"第 {report.failed_round} 轮失败"
            self._set_verdict(VERDICT_FAILED, detail)
        self._set_running(False)

    # ------------------------------------------------------------------ 辅助方法
    def _port_device(self, value: str) -> str:
        """从下拉展示文本中取出真实串口名，如 COM3。"""
        text = value.strip()
        if not text:
            return ""
        mapped = self._label_to_device.get(text)
        if mapped:
            return mapped.upper()
        if " - " in text:
            text = text.split(" - ", 1)[0].strip()
        return text.upper()

    def _widen_combo_popdown(self, combo: ttk.Combobox) -> None:
        """让串口下拉列表比输入框更宽，完整展示设备名称。"""
        try:
            values = combo.cget("values")
            if isinstance(values, str):
                values = self.root.tk.splitlist(values)
            longest = max((len(str(value)) for value in values), default=10)
            popdown = self.root.tk.call("ttk::combobox::PopdownWindow", combo)
            listbox = f"{popdown}.f.l"
            self.root.tk.call(
                listbox, "configure", "-width", max(12, min(longest + 2, 80))
            )
        except tk.TclError:
            pass

    def _short_port_name(self, description: str) -> str:
        """把设备描述压缩成适合卡片宽度显示的名称。"""
        text = description.strip()
        if not text:
            return ""
        tokens = text.split()
        for token in reversed(tokens):
            cleaned = token.strip("()[],")
            if any(ch.isdigit() for ch in cleaned) and any(
                ch.isalpha() for ch in cleaned
            ):
                return cleaned
        joined = ""
        for token in tokens:
            candidate = f"{joined} {token}".strip()
            if len(candidate) > 12:
                break
            joined = candidate
        return joined or text[:12]

    def refresh_ports(self, silent: bool = False) -> None:
        ports = list(list_ports.comports())
        self._port_devices = [port.device for port in ports]
        self._port_labels = []
        self._device_to_label = {}
        self._label_to_device = {}
        full_labels: list[str] = []
        for port in ports:
            device = port.device
            description = (getattr(port, "description", "") or "").strip()
            suffix = f" ({device})"
            if description.endswith(suffix):
                description = description[: -len(suffix)].strip()
            if description.upper() == device.upper() or description.lower() == "n/a":
                description = ""
            full_label = f"{device} - {description}" if description else device
            full_labels.append(full_label)
            short_name = self._short_port_name(description)
            label = f"{device} - {short_name}" if short_name else device
            self._port_labels.append(label)
            self._device_to_label[device.upper()] = label
            self._label_to_device[label] = device
        self._port_choices = list(self._port_labels)

        for row in self.rows:
            row.combo.configure(values=self._port_choices)
            current = self._port_device(row.port_var.get())
            label = self._device_to_label.get(current)
            if label and row.port_var.get() != label:
                row.port_var.set(label)
        if silent:
            return
        if self._port_labels:
            self._append_log(
                f"检测到 {len(self._port_labels)} 个串口："
                f"{'，'.join(full_labels)}",
                "info",
            )
        else:
            self._append_log(
                "未检测到可用串口，可在串口下拉框中手动输入名称（如 COM3）", "warning"
            )

    def _set_verdict(self, state: str, detail: str = "") -> None:
        color = VERDICT_COLORS.get(state, MUTED_COLOR)
        self.verdict_label.config(text=state, fg=color)
        self.verdict_dot.config(fg=color)
        self.verdict_detail_label.config(text=detail)

    def _set_running(self, running: bool) -> None:
        self._running = running
        if running:
            self.start_btn.config(state="disabled", bg="#93c5fd", activebackground="#93c5fd")
            self.stop_btn.config(
                state="normal",
                bg="#ef4444",
                fg="white",
                activebackground="#dc2626",
                activeforeground="white",
            )
        else:
            self.start_btn.config(
                state="normal", bg="#2563eb", activebackground="#1d4ed8"
            )
            self.stop_btn.config(
                state="disabled",
                bg="#e5e7eb",
                fg="#9ca3af",
                activebackground="#e5e7eb",
                activeforeground="#9ca3af",
            )
        self._update_channel_buttons()

    def _append_log(self, message: str, level: str = "info", index: int | None = None) -> None:
        """写入日志；index 为通道号时只写该通道，否则广播到所有通道。"""
        if isinstance(index, int) and 0 <= index < len(self.rows):
            targets = [self.rows[index]]
        else:
            targets = self.rows
        tag = level if level in LOG_COLORS else "info"
        timestamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        for row in targets:
            widget = row.log_text
            widget.configure(state="normal")
            widget.insert("end", f"[{timestamp}] {message}\n", tag)
            # 长时间轮询会持续产生日志，限制每个通道的日志行数
            line_count = int(widget.index("end-1c").split(".")[0])
            if line_count > 900:
                widget.delete("1.0", "201.0")
            widget.see("end")
            widget.configure(state="disabled")

    def _clear_logs(self) -> None:
        for row in self.rows:
            row.log_text.configure(state="normal")
            row.log_text.delete("1.0", "end")
            row.log_text.configure(state="disabled")

    def _on_close(self) -> None:
        if self.worker and self.worker.is_alive():
            self.stop_event.set()
            self.worker.join(timeout=1.0)
        self.root.destroy()
