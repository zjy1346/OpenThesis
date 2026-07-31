from __future__ import annotations

import json
import os
import queue
import re
import threading
import time
import traceback
import tempfile
import webbrowser
from pathlib import Path
from tkinter import (
    BOTH,
    END,
    HORIZONTAL,
    LEFT,
    RIGHT,
    VERTICAL,
    W,
    X,
    Y,
    filedialog,
    messagebox,
)
import tkinter as tk
from tkinter import ttk

try:
    from tkinterweb import HtmlFrame
except ImportError:  # Source fallback; packaged builds include TkinterWeb.
    HtmlFrame = None  # type: ignore[assignment]

from . import __version__
from .comparison import compare_research_runs
from .demo import DEMO_COMPANY, demo_facts
from .domain import Company, FilingDocument, FinancialFact
from .filing_parser import build_filing_evidence
from .onboarding import (
    COMMON_COMPANY_LABELS,
    SEC_DEFAULT_PROFILE,
    build_sec_user_agent,
    extract_sec_contact_email,
    get_common_company,
)
from .model_catalog import (
    ModelDiscoveryError,
    ModelPreset,
    discover_models,
    get_model_preset,
    infer_model_preset,
    merge_model_ids,
    preset_labels,
)
from .i18n import (
    EN,
    LANGUAGE_NAMES,
    SEC_PROFILE_IDS,
    language_name,
    model_preset_label,
    normalize_language,
    run_status_label,
    sec_profile_id_from_label,
    sec_profile_label,
    translate,
    translate_error,
)
from .packs import (
    PackValidationError,
    ResearchPack,
    builtin_pack,
    install_pack,
    list_installed_packs,
)
from .providers import ModelConfig, ProviderError, create_provider
from .research import ResearchCancelled, ResearchWorkflow
from .report_html import render_message_html, render_research_html
from .reporting import render_research_run
from .sec_client import SecClient, SecClientError
from .storage import Storage


def default_data_dir() -> Path:
    override = os.environ.get("OPENTHESIS_DATA_DIR")
    if override:
        return Path(override)
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "OpenThesis"
    return Path.home() / ".openthesis"


def json_pretty(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


def format_elapsed(seconds: float) -> str:
    total = max(0, int(seconds))
    minutes, remaining = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{remaining:02d}"
    return f"{minutes:02d}:{remaining:02d}"


def ease_out_cubic(progress: float) -> float:
    bounded = min(1.0, max(0.0, progress))
    return 1.0 - (1.0 - bounded) ** 3


def clamp_report_zoom(value: float) -> float:
    return min(1.6, max(0.8, round(value, 2)))


def friendly_research_error(
    message: str, language: str = "zh-CN"
) -> tuple[str, str]:
    normalized = message.lower()
    if "sec 请求失败" in normalized or "sec request failed" in normalized:
        result = (
            "SEC 数据获取失败",
            "请检查网络连接和 SEC 联系邮箱后重试；也可以先使用“合成演示公司”验证完整流程。",
        )
        return tuple(translate(item, language) for item in result)  # type: ignore[return-value]
    if "http 401" in normalized or "http 403" in normalized:
        result = (
            "模型认证失败",
            "请检查 API Key、提供方账号权限以及接口地址。Key 不会出现在诊断信息中。",
        )
        return tuple(translate(item, language) for item in result)  # type: ignore[return-value]
    if "http 429" in normalized or "rate limit" in normalized:
        result = (
            "模型请求受到限流",
            "服务商暂时限制了请求频率。请稍后重新运行，或检查账号额度。",
        )
        return tuple(translate(item, language) for item in result)  # type: ignore[return-value]
    if "timeout" in normalized or "timed out" in normalized or "超时" in message:
        result = (
            "模型或数据请求超时",
            "网络或模型响应时间超过限制。已完成的中间结果仍保存在研究历史中。",
        )
        return tuple(translate(item, language) for item in result)  # type: ignore[return-value]
    if "http 404" in normalized or "model not found" in normalized:
        result = (
            "模型或接口不存在",
            "请核对模型 ID 与接口地址，必要时在模型设置中刷新在线目录。",
        )
        return tuple(translate(item, language) for item in result)  # type: ignore[return-value]
    if any(
        marker in normalized
        for marker in ("connection refused", "urlopen error", "name resolution")
    ):
        result = (
            "网络连接失败",
            "无法连接模型或数据服务。请检查网络、代理、接口地址和本地服务状态。",
        )
        return tuple(translate(item, language) for item in result)  # type: ignore[return-value]
    result = (
        "研究任务失败",
        "任务未能完成；已完成的中间结果仍保存在研究历史中。可检查设置后重新运行。",
    )
    return tuple(translate(item, language) for item in result)  # type: ignore[return-value]


class VerticalScrolledFrame(ttk.Frame):
    """A vertical scroller whose mouse wheel is active only while hovered."""

    def __init__(self, master: tk.Misc, *, width: int | None = None):
        super().__init__(master)
        self.canvas = tk.Canvas(
            self, highlightthickness=0, borderwidth=0, width=width
        )
        self.scrollbar = ttk.Scrollbar(
            self, orient=VERTICAL, command=self.canvas.yview
        )
        self.content = ttk.Frame(self.canvas)
        self._window = self.canvas.create_window(
            (0, 0), window=self.content, anchor="nw"
        )
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.pack(side=LEFT, fill=BOTH, expand=True)
        self.scrollbar.pack(side=RIGHT, fill=Y)
        self.content.bind("<Configure>", self._sync_scroll_region)
        self.canvas.bind("<Configure>", self._sync_content_width)
        self.canvas.bind_all("<MouseWheel>", self._on_mousewheel, add="+")
        self.canvas.bind_all("<Button-4>", self._on_mousewheel, add="+")
        self.canvas.bind_all("<Button-5>", self._on_mousewheel, add="+")

    def _sync_scroll_region(self, _event: object = None) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _sync_content_width(self, event: tk.Event[tk.Misc]) -> None:
        self.canvas.itemconfigure(self._window, width=event.width)

    def _on_mousewheel(self, event: tk.Event[tk.Misc]) -> str:
        widget = self.winfo_containing(
            self.winfo_pointerx(), self.winfo_pointery()
        )
        while widget is not None and widget is not self:
            widget = widget.master
        if widget is not self:
            return ""
        if getattr(event, "num", None) == 4:
            units = -1
        elif getattr(event, "num", None) == 5:
            units = 1
        else:
            delta = int(getattr(event, "delta", 0))
            units = -int(delta / 120) if delta else 0
        if units:
            self.canvas.yview_scroll(units, "units")
        return "break"

    def scroll_to_bottom(self) -> None:
        self.canvas.yview_moveto(1.0)


class OpenThesisApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(f"OpenThesis {__version__}")
        self.root.geometry("1260x820")
        self.root.minsize(980, 680)
        self.storage = Storage(default_data_dir())
        self.ui_language = normalize_language(
            os.environ.get("OPENTHESIS_UI_LANGUAGE")
            or self.storage.get_setting("ui_language", "zh-CN")
        )
        self.report_language = normalize_language(
            os.environ.get("OPENTHESIS_REPORT_LANGUAGE")
            or self.storage.get_setting("report_language", "zh-CN")
        )
        self._preset_labels = preset_labels(self.ui_language)
        self._sec_profile_labels = tuple(
            sec_profile_label(profile_id, self.ui_language)
            for profile_id in SEC_PROFILE_IDS
        )
        self._interrupted_run_count = self.storage.interrupt_running_runs()
        self.event_queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self.selected_company: Company | None = None
        self.current_run_id = ""
        self.company_results: list[Company] = []
        self.pack_by_label: dict[str, ResearchPack] = {}
        self.current_report_text = ""
        self.current_report_html = ""
        self._report_context: tuple[
            str,
            list[dict[str, object]],
            str,
        ] | None = None
        self.report_technical_visible = False
        self.report_focus_mode = False
        self.report_zoom = 1.0
        self.report_focus_window: tk.Toplevel | None = None
        self.report_focus_view: object | None = None
        self.report_focus_text: tk.Text | None = None
        self.report_focus_technical_button: ttk.Button | None = None
        self._report_focus_fade_job: str | None = None
        self._report_focus_transitioning = False
        self._report_focus_alpha_supported = True
        self._main_report_dirty = False
        self.research_running = False
        self._research_cancel_event: threading.Event | None = None
        self._research_started_monotonic = 0.0
        self._research_last_progress_monotonic = 0.0
        self._research_progress_message = self._t("等待开始研究")
        self._research_progress_percent = 0
        self._research_activity_lines: list[str] = []
        self._last_research_error = ""
        self._report_link_tags: list[str] = []
        self._nav_buttons: dict[str, ttk.Button] = {}
        self._model_catalog_cache: dict[tuple[str, str], tuple[str, ...]] = {}
        self._model_refresh_generation = {"primary": 0, "compare": 0}
        self._configure_style()
        self._build_ui()
        self._translate_static_widgets(self.root)
        self._load_settings()
        self._refresh_packs()
        self._refresh_history()
        self._refresh_theses()
        if self._interrupted_run_count:
            self._show_interrupted_recovery(self._interrupted_run_count)
        self.root.after(100, self._drain_events)
        self.root.after(500, self._tick_research_feedback)

    def _t(self, source: str, **params: object) -> str:
        return translate(source, self.ui_language, **params)

    def _error_text(self, message: str) -> str:
        return translate_error(message, self.ui_language)

    def _company_display_name(self, company: Company) -> str:
        if company.cik == DEMO_COMPANY.cik and self.ui_language == EN:
            return "Example Cloud Systems (Synthetic Demo Company)"
        return company.name

    def _translate_static_widgets(self, widget: tk.Misc) -> None:
        if self.ui_language != EN:
            return
        # HtmlFrame owns a native Tkhtml widget tree that is not part of our
        # translatable application controls and may expose cyclic descendants.
        if HtmlFrame is not None and isinstance(widget, HtmlFrame):
            return
        try:
            keys = widget.keys()
        except (AttributeError, tk.TclError):
            keys = ()
        if "text" in keys:
            try:
                current = str(widget.cget("text"))
                translated = self._t(current)
                if translated != current:
                    widget.configure(text=translated)
            except tk.TclError:
                pass
        if isinstance(widget, ttk.Notebook):
            for tab_id in widget.tabs():
                current = str(widget.tab(tab_id, "text"))
                widget.tab(tab_id, text=self._t(current))
        if isinstance(widget, ttk.Treeview):
            for column in widget["columns"]:
                current = str(widget.heading(column, "text"))
                widget.heading(column, text=self._t(current))
        for child in widget.winfo_children():
            self._translate_static_widgets(child)

    def _configure_style(self) -> None:
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        self.root.configure(background="#f4f7fb")
        style.configure(
            ".",
            font=("Segoe UI", 10),
            background="#f4f7fb",
            foreground="#172033",
        )
        style.configure("TFrame", background="#f4f7fb")
        style.configure("App.TFrame", background="#f4f7fb")
        style.configure("Content.TFrame", background="#f4f7fb")
        style.configure("Sidebar.TFrame", background="#0f172a")
        style.configure("Card.TFrame", background="#ffffff", relief="solid", borderwidth=1)
        style.configure("CardContent.TFrame", background="#ffffff")
        style.configure(
            "TLabel",
            background="#f4f7fb",
            foreground="#172033",
        )
        style.configure(
            "Title.TLabel",
            font=("Segoe UI", 22, "bold"),
            background="#ffffff",
            foreground="#0f172a",
        )
        style.configure(
            "TitleSmall.TLabel",
            font=("Segoe UI", 13, "bold"),
            background="#ffffff",
            foreground="#0f172a",
        )
        style.configure(
            "Subtitle.TLabel",
            background="#f4f7fb",
            foreground="#64748b",
        )
        style.configure(
            "HeaderSubtitle.TLabel",
            background="#ffffff",
            foreground="#64748b",
        )
        style.configure(
            "Version.TLabel",
            background="#eff6ff",
            foreground="#1d4ed8",
            font=("Segoe UI", 9, "bold"),
            padding=(8, 4),
        )
        style.configure(
            "SidebarBrand.TLabel",
            background="#0f172a",
            foreground="#ffffff",
            font=("Segoe UI", 17, "bold"),
        )
        style.configure(
            "SidebarCaption.TLabel",
            background="#0f172a",
            foreground="#94a3b8",
            font=("Segoe UI", 9),
        )
        style.configure(
            "Nav.TButton",
            background="#0f172a",
            foreground="#cbd5e1",
            borderwidth=0,
            relief="flat",
            anchor="w",
            padding=(16, 11),
            font=("Segoe UI", 10),
        )
        style.map(
            "Nav.TButton",
            background=[("active", "#1e293b")],
            foreground=[("active", "#ffffff")],
        )
        style.configure(
            "NavActive.TButton",
            background="#1d4ed8",
            foreground="#ffffff",
            borderwidth=0,
            relief="flat",
            anchor="w",
            padding=(16, 11),
            font=("Segoe UI", 10, "bold"),
        )
        style.map(
            "NavActive.TButton",
            background=[("active", "#2563eb")],
            foreground=[("active", "#ffffff")],
        )
        style.configure(
            "TButton",
            background="#ffffff",
            foreground="#334155",
            bordercolor="#cbd5e1",
            lightcolor="#ffffff",
            darkcolor="#cbd5e1",
            padding=(11, 7),
            relief="flat",
        )
        style.map(
            "TButton",
            background=[("active", "#f1f5f9"), ("disabled", "#e2e8f0")],
            foreground=[("disabled", "#94a3b8")],
        )
        style.configure(
            "Accent.TButton",
            font=("Segoe UI", 11, "bold"),
            padding=(18, 10),
            background="#2563eb",
            foreground="#ffffff",
            bordercolor="#2563eb",
        )
        style.map(
            "Accent.TButton",
            background=[("active", "#1d4ed8"), ("disabled", "#94a3b8")],
            foreground=[("active", "#ffffff"), ("disabled", "#e2e8f0")],
        )
        style.configure(
            "Workflow.TLabel",
            font=("Segoe UI", 10, "bold"),
            foreground="#1d4ed8",
        )
        style.configure(
            "ResearchStatus.TLabel",
            font=("Segoe UI", 10, "bold"),
            foreground="#0f172a",
        )
        style.configure(
            "ResearchError.TLabel",
            foreground="#b91c1c",
        )
        style.configure(
            "TLabelframe",
            background="#ffffff",
            bordercolor="#dbe3ec",
            relief="solid",
            borderwidth=1,
        )
        style.configure(
            "TLabelframe.Label",
            background="#ffffff",
            foreground="#334155",
            font=("Segoe UI", 10, "bold"),
        )
        style.configure(
            "TEntry",
            fieldbackground="#ffffff",
            bordercolor="#cbd5e1",
            lightcolor="#cbd5e1",
            darkcolor="#cbd5e1",
            padding=7,
        )
        style.configure(
            "TCombobox",
            fieldbackground="#ffffff",
            background="#ffffff",
            bordercolor="#cbd5e1",
            padding=6,
        )
        style.configure(
            "Treeview",
            background="#ffffff",
            fieldbackground="#ffffff",
            foreground="#1e293b",
            rowheight=30,
            bordercolor="#dbe3ec",
            borderwidth=1,
        )
        style.configure(
            "Treeview.Heading",
            background="#eef2f7",
            foreground="#334155",
            font=("Segoe UI", 9, "bold"),
            padding=(8, 7),
        )
        style.map(
            "Treeview",
            background=[("selected", "#dbeafe")],
            foreground=[("selected", "#1e3a8a")],
        )
        style.configure(
            "Horizontal.TProgressbar",
            background="#2563eb",
            troughcolor="#e2e8f0",
            bordercolor="#e2e8f0",
            lightcolor="#2563eb",
            darkcolor="#2563eb",
        )
        style.configure("Sidebar.TNotebook", borderwidth=0, background="#f4f7fb")
        try:
            style.layout("Sidebar.TNotebook.Tab", [])
        except tk.TclError:
            pass

    def _build_ui(self) -> None:
        self.shell = ttk.Frame(self.root, style="App.TFrame")
        self.shell.pack(fill=BOTH, expand=True)

        self.sidebar = ttk.Frame(
            self.shell, style="Sidebar.TFrame", width=210
        )
        self.sidebar.pack(side=LEFT, fill=Y)
        self.sidebar.pack_propagate(False)
        brand = ttk.Frame(
            self.sidebar, style="Sidebar.TFrame", padding=(20, 24, 14, 20)
        )
        brand.pack(fill=X)
        ttk.Label(
            brand, text="OpenThesis", style="SidebarBrand.TLabel"
        ).pack(anchor=W)
        ttk.Label(
            brand,
            text="Evidence-first research",
            style="SidebarCaption.TLabel",
        ).pack(anchor=W, pady=(3, 0))

        self.main_panel = ttk.Frame(self.shell, style="Content.TFrame")
        self.main_panel.pack(side=LEFT, fill=BOTH, expand=True)
        self.header = ttk.Frame(
            self.main_panel, style="Card.TFrame", padding=(22, 14)
        )
        self.header.pack(fill=X, padx=14, pady=(14, 10))
        header_title = ttk.Frame(self.header, style="CardContent.TFrame")
        header_title.pack(side=LEFT, fill=X, expand=True)
        ttk.Label(header_title, text="长期公司研究工作台", style="Title.TLabel").pack(
            anchor=W
        )
        ttk.Label(
            header_title,
            text="研究公司，而不是预测短期价格",
            style="HeaderSubtitle.TLabel",
        ).pack(anchor=W, pady=(2, 0))
        ttk.Label(
            self.header,
            text=f"v{__version__}",
            style="Version.TLabel",
        ).pack(side=RIGHT, padx=(12, 0))

        self.notebook = ttk.Notebook(
            self.main_panel, style="Sidebar.TNotebook"
        )

        self.research_tab = ttk.Frame(self.notebook, padding=12)
        self.history_tab = ttk.Frame(self.notebook, padding=12)
        self.model_tab = ttk.Frame(self.notebook, padding=12)
        self.packs_tab = ttk.Frame(self.notebook, padding=12)
        self.thesis_tab = ttk.Frame(self.notebook, padding=12)
        self.settings_tab = ttk.Frame(self.notebook, padding=12)
        self.about_tab = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(self.research_tab, text="公司研究")
        self.notebook.add(self.history_tab, text="研究历史")
        self.notebook.add(self.model_tab, text="模型设置")
        self.notebook.add(self.packs_tab, text="研究模块")
        self.notebook.add(self.thesis_tab, text="投资逻辑")
        self.notebook.add(self.settings_tab, text="设置")
        self.notebook.add(self.about_tab, text="关于")

        navigation = (
            ("公司研究", self.research_tab),
            ("研究历史", self.history_tab),
            ("模型与数据源", self.model_tab),
            ("研究模块", self.packs_tab),
            ("投资逻辑", self.thesis_tab),
            ("设置", self.settings_tab),
            ("关于", self.about_tab),
        )
        nav_frame = ttk.Frame(
            self.sidebar, style="Sidebar.TFrame", padding=(10, 4)
        )
        nav_frame.pack(fill=X)
        for label, tab in navigation:
            tab_id = str(tab)
            button = ttk.Button(
                nav_frame,
                text=label,
                style="Nav.TButton",
                command=lambda target=tab: self.notebook.select(target),
            )
            button.pack(fill=X, pady=2)
            self._nav_buttons[tab_id] = button
        ttk.Label(
            self.sidebar,
            text="本地优先 · 不执行交易",
            style="SidebarCaption.TLabel",
        ).pack(side="bottom", anchor=W, padx=22, pady=18)

        self._build_research_tab()
        self._build_history_tab()
        self._build_model_tab()
        self._build_packs_tab()
        self._build_thesis_tab()
        self._build_settings_tab()
        self._build_about_tab()

        self.status_frame = ttk.Frame(
            self.main_panel, padding=(14, 4, 14, 10)
        )
        self.status_frame.pack(side="bottom", fill=X)
        self.status_var = tk.StringVar(value=self._t("就绪"))
        ttk.Label(self.status_frame, textvariable=self.status_var).pack(side=LEFT)
        self.progress = ttk.Progressbar(
            self.status_frame, mode="determinate", maximum=100, length=280
        )
        self.progress.pack(side=RIGHT)
        self.notebook.pack(fill=BOTH, expand=True, padx=14, pady=(0, 8))
        self.notebook.bind("<<NotebookTabChanged>>", self._sync_navigation)
        self.root.bind("<F11>", self._handle_report_focus_shortcut, add="+")
        self.root.bind("<Escape>", self._handle_report_escape, add="+")
        self.root.bind("<Control-plus>", self._handle_report_zoom_in, add="+")
        self.root.bind("<Control-equal>", self._handle_report_zoom_in, add="+")
        self.root.bind("<Control-minus>", self._handle_report_zoom_out, add="+")
        self.root.bind("<Control-0>", self._handle_report_zoom_reset, add="+")
        self.root.bind(
            "<Control-MouseWheel>",
            self._handle_report_zoom_wheel,
            add="+",
        )
        self.root.bind(
            "<Configure>",
            self._sync_report_focus_geometry,
            add="+",
        )
        self._sync_navigation()

    def _sync_navigation(self, _event: object = None) -> None:
        selected = self.notebook.select()
        for tab_id, button in self._nav_buttons.items():
            button.configure(
                style="NavActive.TButton" if tab_id == selected else "Nav.TButton"
            )

    def _research_controls_are_visible(self) -> bool:
        return str(self.research_controls_scroll) in self.research_split.panes()

    @staticmethod
    def _report_scroll_fraction(view: object | None) -> float:
        try:
            internal = getattr(view, "_html")
            position = internal.yview()
            if isinstance(position, (tuple, list)) and position:
                return min(1.0, max(0.0, float(position[0])))
        except (AttributeError, TypeError, ValueError, tk.TclError):
            pass
        return 0.0

    def _toggle_report_focus(self) -> None:
        if self._report_focus_transitioning:
            return
        self._set_report_focus(not self.report_focus_mode)

    def _set_report_focus(self, expanded: bool, *, animate: bool = True) -> None:
        if self._report_focus_transitioning or expanded == self.report_focus_mode:
            return
        if expanded:
            self._open_report_focus(animate=animate)
        else:
            self._close_report_focus(animate=animate)

    def _open_report_focus(self, *, animate: bool) -> None:
        self.root.update_idletasks()
        scroll_fraction = self._report_scroll_fraction(self.report_view)
        window = tk.Toplevel(self.root)
        self.report_focus_window = window
        self.report_focus_mode = True
        self._report_focus_transitioning = True
        window.withdraw()
        window.overrideredirect(True)
        window.transient(self.root)
        window.configure(background="#f4f7fb")
        window.protocol("WM_DELETE_WINDOW", self._close_report_focus)

        shell = ttk.Frame(window, style="App.TFrame", padding=(14, 10))
        shell.pack(fill=BOTH, expand=True)
        toolbar = ttk.Frame(shell, style="Card.TFrame", padding=(14, 8))
        toolbar.pack(fill=X, pady=(0, 8))
        ttk.Label(
            toolbar,
            text=self._t("沉浸阅读"),
            font=("Segoe UI", 13, "bold"),
            style="TitleSmall.TLabel",
        ).pack(side=LEFT)
        ttk.Label(
            toolbar,
            textvariable=self.research_percent_var,
            style="HeaderSubtitle.TLabel",
        ).pack(side=LEFT, padx=(12, 0))
        ttk.Button(
            toolbar,
            text=self._t("⤢ 恢复布局"),
            command=self._close_report_focus,
        ).pack(side=RIGHT)
        self.report_focus_technical_button = ttk.Button(
            toolbar,
            text=self._t(
                "隐藏技术详情"
                if self.report_technical_visible
                else "显示技术详情"
            ),
            command=self._toggle_report_technical,
        )
        self.report_focus_technical_button.pack(side=RIGHT, padx=(0, 7))
        ttk.Button(
            toolbar,
            text=self._t("导出"),
            command=self._export_report,
        ).pack(side=RIGHT, padx=(0, 7))
        ttk.Button(
            toolbar,
            text="＋",
            width=3,
            command=lambda: self._change_report_zoom(0.1),
        ).pack(side=RIGHT, padx=(7, 0))
        ttk.Label(
            toolbar,
            textvariable=self.report_zoom_label_var,
            width=6,
            anchor="center",
            style="HeaderSubtitle.TLabel",
        ).pack(side=RIGHT)
        ttk.Button(
            toolbar,
            text="－",
            width=3,
            command=lambda: self._change_report_zoom(-0.1),
        ).pack(side=RIGHT)

        content = ttk.Frame(shell, style="Card.TFrame")
        content.pack(fill=BOTH, expand=True)
        self.report_focus_view = None
        self.report_focus_text = None
        if HtmlFrame is not None:
            focus_view = HtmlFrame(
                content,
                messages_enabled=False,
                vertical_scrollbar=True,
                horizontal_scrollbar="auto",
                selection_enabled=True,
                images_enabled=False,
                javascript_enabled=False,
                on_link_click=self._open_report_link,
                zoom=self.report_zoom,
            )
            focus_view.pack(fill=BOTH, expand=True)
            focus_view.load_html(self.current_report_html)
            self.report_focus_view = focus_view
            window.after_idle(
                lambda: focus_view.yview_moveto(scroll_fraction)
            )
        else:
            scrollbar = ttk.Scrollbar(content, orient=VERTICAL)
            scrollbar.pack(side=RIGHT, fill=Y)
            focus_text = tk.Text(
                content,
                wrap="word",
                font=("Segoe UI", max(8, round(10 * self.report_zoom))),
                padx=22,
                pady=18,
                yscrollcommand=scrollbar.set,
                background="#ffffff",
                foreground="#172033",
                relief="flat",
            )
            focus_text.insert("1.0", self.current_report_text)
            focus_text.pack(side=LEFT, fill=BOTH, expand=True)
            scrollbar.configure(command=focus_text.yview)
            focus_text.yview_moveto(scroll_fraction)
            self.report_focus_text = focus_text

        window.bind("<Escape>", self._handle_report_escape, add="+")
        window.bind("<F11>", self._handle_report_focus_shortcut, add="+")
        window.bind("<Control-plus>", self._handle_report_zoom_in, add="+")
        window.bind("<Control-equal>", self._handle_report_zoom_in, add="+")
        window.bind("<Control-minus>", self._handle_report_zoom_out, add="+")
        window.bind("<Control-0>", self._handle_report_zoom_reset, add="+")
        window.bind(
            "<Control-MouseWheel>",
            self._handle_report_zoom_wheel,
            add="+",
        )
        self._sync_report_focus_geometry()
        self._report_focus_alpha_supported = True
        try:
            window.attributes("-alpha", 0.0 if animate else 1.0)
        except tk.TclError:
            self._report_focus_alpha_supported = False
        window.deiconify()
        window.lift(self.root)
        window.focus_force()
        self.report_focus_button.configure(text=self._t("⤢ 恢复布局"))
        if animate and self._report_focus_alpha_supported:
            self._fade_report_focus(opening=True, frame=0, frames=10)
        else:
            self._report_focus_transitioning = False

    def _close_report_focus(
        self,
        _event: object = None,
        *,
        animate: bool = True,
    ) -> None:
        window = self.report_focus_window
        if window is None or not window.winfo_exists():
            self._destroy_report_focus()
            return
        if self._report_focus_fade_job:
            self.root.after_cancel(self._report_focus_fade_job)
            self._report_focus_fade_job = None
        self._sync_main_report_from_focus()
        self._report_focus_transitioning = True
        if animate and self._report_focus_alpha_supported:
            self._fade_report_focus(opening=False, frame=0, frames=9)
        else:
            self._destroy_report_focus()

    def _fade_report_focus(
        self,
        *,
        opening: bool,
        frame: int,
        frames: int,
    ) -> None:
        window = self.report_focus_window
        if window is None or not window.winfo_exists():
            self._destroy_report_focus()
            return
        eased = ease_out_cubic(frame / frames)
        alpha = eased if opening else 1.0 - eased
        try:
            window.attributes("-alpha", alpha)
        except tk.TclError:
            self._report_focus_alpha_supported = False
            if opening:
                self._report_focus_transitioning = False
            else:
                self._destroy_report_focus()
            return
        if frame < frames:
            self._report_focus_fade_job = self.root.after(
                16,
                lambda: self._fade_report_focus(
                    opening=opening,
                    frame=frame + 1,
                    frames=frames,
                ),
            )
            return
        self._report_focus_fade_job = None
        if opening:
            self._report_focus_transitioning = False
            window.attributes("-alpha", 1.0)
        else:
            self._destroy_report_focus()

    def _destroy_report_focus(self) -> None:
        scroll_fraction = self._report_scroll_fraction(self.report_focus_view)
        window = self.report_focus_window
        if window is not None:
            try:
                window.destroy()
            except tk.TclError:
                pass
        self.report_focus_window = None
        self.report_focus_view = None
        self.report_focus_text = None
        self.report_focus_technical_button = None
        self.report_focus_mode = False
        self._report_focus_transitioning = False
        self._report_focus_fade_job = None
        self.report_focus_button.configure(text=self._t("⛶ 沉浸阅读"))
        if self.report_view is not None:
            self.root.after_idle(
                lambda: self.report_view.yview_moveto(scroll_fraction)
            )
        elif self.report_text is not None:
            self.report_text.yview_moveto(scroll_fraction)
        self.root.focus_force()

    def _sync_main_report_from_focus(self) -> None:
        if not self._main_report_dirty:
            return
        if self.report_view is not None:
            self.report_view.load_html(self.current_report_html)
            self.report_view.configure(zoom=self.report_zoom)
        elif self.report_text is not None:
            self.report_text.delete("1.0", END)
            self.report_text.insert("1.0", self.current_report_text)
            self.report_text.configure(
                font=("Segoe UI", max(8, round(10 * self.report_zoom)))
            )
        self._main_report_dirty = False

    def _sync_report_focus_geometry(self, event: object = None) -> None:
        if event is not None and getattr(event, "widget", self.root) is not self.root:
            return
        window = self.report_focus_window
        if window is None or not window.winfo_exists():
            return
        if self.root.state() == "iconic":
            return
        self.root.update_idletasks()
        width = max(1, self.root.winfo_width())
        height = max(1, self.root.winfo_height())
        x = self.root.winfo_rootx()
        y = self.root.winfo_rooty()
        window.geometry(f"{width}x{height}+{x}+{y}")

    def _handle_report_focus_shortcut(self, _event: object = None) -> str | None:
        if self.notebook.select() != str(self.research_tab):
            return None
        self._toggle_report_focus()
        return "break"

    def _handle_report_escape(self, _event: object = None) -> str | None:
        if not self.report_focus_mode:
            return None
        self._set_report_focus(False)
        return "break"

    def _change_report_zoom(self, delta: float) -> None:
        self._set_report_zoom(self.report_zoom + delta)

    def _set_report_zoom(self, target: float, *, animate: bool = True) -> None:
        # Tkhtml must reflow the complete document when zoom changes. Apply the
        # target once; the focus-window fade remains compositor-animated.
        self._apply_report_zoom(clamp_report_zoom(target))

    def _apply_report_zoom(self, value: float) -> None:
        self.report_zoom = clamp_report_zoom(value)
        self.report_zoom_label_var.set(f"{self.report_zoom * 100:.0f}%")
        if self.report_focus_view is not None:
            self.report_focus_view.configure(zoom=self.report_zoom)  # type: ignore[attr-defined]
            self._main_report_dirty = True
        elif self.report_focus_text is not None:
            self.report_focus_text.configure(
                font=("Segoe UI", max(8, round(10 * self.report_zoom)))
            )
            self._main_report_dirty = True
        elif self.report_view is not None:
            self.report_view.configure(zoom=self.report_zoom)
        elif self.report_text is not None:
            self.report_text.configure(
                font=("Segoe UI", max(8, round(10 * self.report_zoom)))
            )

    def _handle_report_zoom_in(self, _event: object = None) -> str | None:
        if not self.report_focus_mode:
            return None
        self._change_report_zoom(0.1)
        return "break"

    def _handle_report_zoom_out(self, _event: object = None) -> str | None:
        if not self.report_focus_mode:
            return None
        self._change_report_zoom(-0.1)
        return "break"

    def _handle_report_zoom_reset(self, _event: object = None) -> str | None:
        if not self.report_focus_mode:
            return None
        self._set_report_zoom(1.0)
        return "break"

    def _handle_report_zoom_wheel(self, event: tk.Event) -> str | None:
        if not self.report_focus_mode:
            return None
        delta = 0.1 if int(getattr(event, "delta", 0)) > 0 else -0.1
        self._change_report_zoom(delta)
        return "break"

    def _build_research_tab(self) -> None:
        self.selected_company_var = tk.StringVar(value=self._t("尚未选择公司"))
        self.start_hint_var = tk.StringVar(
            value=self._t("请先在下方选择一家公司。")
        )

        self.workflow_frame = ttk.LabelFrame(
            self.research_tab, text="研究流程", padding=(12, 8)
        )
        self.workflow_frame.pack(fill=X, pady=(0, 10))
        self.workflow_frame.columnconfigure(0, weight=1)
        ttk.Label(
            self.workflow_frame,
            text="① 选择公司   →   ② 确认配置   →   ③ 开始研究",
            style="Workflow.TLabel",
        ).grid(row=0, column=0, sticky=W, pady=(0, 6))
        ttk.Label(
            self.workflow_frame, textvariable=self.selected_company_var
        ).grid(
            row=1, column=0, sticky=W
        )
        self.run_button = ttk.Button(
            self.workflow_frame,
            text="开始研究",
            style="Accent.TButton",
            command=self._start_research,
        )
        self.run_button.grid(row=0, column=1, rowspan=2, sticky="e", padx=(16, 0))
        ttk.Label(
            self.workflow_frame,
            textvariable=self.start_hint_var,
            style="Subtitle.TLabel",
        ).grid(row=2, column=0, sticky=W, pady=(4, 0))
        ttk.Button(
            self.workflow_frame,
            text="模型与 SEC 设置",
            command=lambda: self.notebook.select(self.model_tab),
        ).grid(row=2, column=1, sticky="e", padx=(16, 0), pady=(4, 0))

        self.research_feedback_frame = ttk.LabelFrame(
            self.workflow_frame, text="任务进度", padding=(10, 7)
        )
        self.research_feedback_frame.grid(
            row=3, column=0, columnspan=2, sticky="ew", pady=(9, 0)
        )
        self.research_feedback_frame.columnconfigure(0, weight=1)
        self.research_feedback_title_var = tk.StringVar(
            value=self._t("等待开始研究")
        )
        self.research_feedback_detail_var = tk.StringVar(
            value=self._t(
                "选择公司并确认配置后，任务阶段、等待时间和错误会显示在这里。"
            )
        )
        self.research_elapsed_var = tk.StringVar(value=self._t("已用时 00:00"))
        self.research_error_var = tk.StringVar()
        ttk.Label(
            self.research_feedback_frame,
            textvariable=self.research_feedback_title_var,
            style="ResearchStatus.TLabel",
        ).grid(row=0, column=0, sticky=W)
        ttk.Label(
            self.research_feedback_frame,
            textvariable=self.research_elapsed_var,
            style="Subtitle.TLabel",
        ).grid(row=0, column=1, sticky="e", padx=(12, 0))
        ttk.Label(
            self.research_feedback_frame,
            textvariable=self.research_feedback_detail_var,
            style="Subtitle.TLabel",
            wraplength=680,
        ).grid(row=1, column=0, columnspan=2, sticky=W, pady=(3, 5))
        progress_row = ttk.Frame(self.research_feedback_frame)
        progress_row.grid(row=2, column=0, columnspan=2, sticky="ew")
        progress_row.columnconfigure(0, weight=1)
        self.research_progress_bar = ttk.Progressbar(
            progress_row, mode="determinate", maximum=100
        )
        self.research_progress_bar.grid(row=0, column=0, sticky="ew")
        self.research_percent_var = tk.StringVar(value="0%")
        ttk.Label(progress_row, textvariable=self.research_percent_var, width=5).grid(
            row=0, column=1, padx=(8, 0)
        )
        self.cancel_research_button = ttk.Button(
            progress_row,
            text="取消研究",
            command=self._cancel_research,
            state="disabled",
        )
        self.cancel_research_button.grid(row=0, column=2, padx=(8, 0))
        self.research_error_label = ttk.Label(
            self.research_feedback_frame,
            textvariable=self.research_error_var,
            style="ResearchError.TLabel",
            wraplength=680,
        )
        self.research_error_label.grid(
            row=3, column=0, sticky=W, pady=(6, 0)
        )
        self.research_error_actions = ttk.Frame(self.research_feedback_frame)
        self.research_error_actions.grid(
            row=3, column=1, sticky="e", padx=(12, 0), pady=(6, 0)
        )
        self.retry_research_button = ttk.Button(
            self.research_error_actions,
            text="重新运行",
            command=self._retry_research,
        )
        self.retry_research_button.pack(side=LEFT)
        ttk.Button(
            self.research_error_actions,
            text="检查模型设置",
            command=lambda: self.notebook.select(self.model_tab),
        ).pack(side=LEFT, padx=(6, 0))
        self.research_error_actions.grid_remove()

        self.research_split = ttk.Panedwindow(
            self.research_tab, orient=HORIZONTAL
        )
        self.research_split.pack(fill=BOTH, expand=True)
        self.research_controls_scroll = VerticalScrolledFrame(
            self.research_split, width=310
        )
        controls = ttk.Frame(
            self.research_controls_scroll.content, padding=(0, 0, 12, 12)
        )
        controls.pack(fill=BOTH, expand=True)
        self.report_panel = ttk.Frame(self.research_split)
        self.research_split.add(self.research_controls_scroll, weight=0)
        self.research_split.add(self.report_panel, weight=1)

        ttk.Label(controls, text="1. 选择公司", font=("Segoe UI", 12, "bold")).pack(
            anchor=W, pady=(0, 8)
        )
        search_row = ttk.Frame(controls)
        search_row.pack(fill=X)
        self.company_query_var = tk.StringVar()
        company_search = ttk.Entry(search_row, textvariable=self.company_query_var)
        company_search.pack(
            side=LEFT, fill=X, expand=True
        )
        company_search.bind("<Return>", lambda _event: self._search_company())
        ttk.Button(search_row, text="搜索", command=self._search_company).pack(
            side=RIGHT, padx=(6, 0)
        )

        ttk.Label(controls, text="常用公司快捷选择").pack(
            anchor=W, pady=(9, 3)
        )
        common_row = ttk.Frame(controls)
        common_row.pack(fill=X)
        self.common_company_var = tk.StringVar(value=COMMON_COMPANY_LABELS[0])
        self.common_company_combo = ttk.Combobox(
            common_row,
            textvariable=self.common_company_var,
            state="readonly",
            values=COMMON_COMPANY_LABELS,
            width=28,
        )
        self.common_company_combo.pack(side=LEFT, fill=X, expand=True)
        ttk.Button(
            common_row, text="选择", command=self._select_common_company
        ).pack(side=RIGHT, padx=(6, 0))

        self.company_list = tk.Listbox(controls, height=5, exportselection=False)
        self.company_list.pack(fill=X, pady=(7, 5))
        self.company_list.bind("<<ListboxSelect>>", self._select_company)
        ttk.Button(
            controls, text="使用合成演示公司", command=self._select_demo_company
        ).pack(fill=X)

        ttk.Separator(controls).pack(fill=X, pady=14)
        ttk.Label(controls, text="2. 研究配置", font=("Segoe UI", 12, "bold")).pack(
            anchor=W, pady=(0, 8)
        )
        ttk.Label(controls, text="研究模块").pack(anchor=W)
        self.pack_var = tk.StringVar()
        self.pack_combo = ttk.Combobox(
            controls, textvariable=self.pack_var, state="readonly"
        )
        self.pack_combo.pack(fill=X, pady=(3, 9))

        self.download_filings_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            controls,
            text="下载最近五份 10-K 原文",
            variable=self.download_filings_var,
        ).pack(anchor=W)
        ttk.Label(
            controls,
            text="未配置模型时仍会生成确定性财务报告。",
            style="Subtitle.TLabel",
            wraplength=280,
        ).pack(anchor=W, pady=(8, 0))
        self.dcf_expanded = tk.BooleanVar(value=False)
        self.dcf_toggle_button = ttk.Button(
            controls,
            text="▶ 高级设置：反向 DCF",
            command=self._toggle_dcf,
        )
        self.dcf_toggle_button.pack(fill=X, pady=(10, 0))
        self.valuation_frame = ttk.LabelFrame(
            controls, text="反向 DCF 参数", padding=8
        )
        self.market_cap_var = tk.StringVar()
        self.discount_rate_var = tk.StringVar(value="10")
        self.terminal_growth_var = tk.StringVar(value="3")
        ttk.Label(self.valuation_frame, text="当前市值（十亿美元）").grid(
            row=0, column=0, sticky=W
        )
        ttk.Entry(
            self.valuation_frame, textvariable=self.market_cap_var, width=11
        ).grid(
            row=0, column=1, sticky=W, padx=(6, 0)
        )
        ttk.Label(self.valuation_frame, text="折现率 %").grid(
            row=1, column=0, sticky=W
        )
        ttk.Entry(
            self.valuation_frame, textvariable=self.discount_rate_var, width=11
        ).grid(
            row=1, column=1, sticky=W, padx=(6, 0)
        )
        ttk.Label(self.valuation_frame, text="永续增长率 %").grid(
            row=2, column=0, sticky=W
        )
        ttk.Entry(
            self.valuation_frame, textvariable=self.terminal_growth_var, width=11
        ).grid(row=2, column=1, sticky=W, padx=(6, 0))
        self.compare_models_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            controls,
            text="运行第二模型并比较分歧",
            variable=self.compare_models_var,
        ).pack(anchor=W, pady=(9, 0))

        ttk.Button(controls, text="导出当前报告", command=self._export_report).pack(
            fill=X, pady=(12, 0)
        )

        self.report_toolbar = ttk.Frame(
            self.report_panel, padding=(2, 0, 0, 0)
        )
        self.report_toolbar.pack(fill=X)
        ttk.Label(
            self.report_toolbar,
            text="研究报告",
            font=("Segoe UI", 13, "bold"),
        ).pack(side=LEFT)
        self.report_focus_button = ttk.Button(
            self.report_toolbar,
            text="⛶ 沉浸阅读",
            command=self._toggle_report_focus,
        )
        self.report_focus_button.pack(side=RIGHT)
        self.report_technical_button = ttk.Button(
            self.report_toolbar,
            text="显示技术详情",
            command=self._toggle_report_technical,
        )
        self.report_technical_button.pack(side=RIGHT, padx=(0, 7))
        ttk.Button(
            self.report_toolbar,
            text="清空显示",
            command=lambda: self._set_report(""),
        ).pack(side=RIGHT, padx=(0, 7))
        ttk.Button(
            self.report_toolbar,
            text="导出",
            command=self._export_report,
        ).pack(side=RIGHT, padx=(0, 7))
        self.report_zoom_label_var = tk.StringVar(value="100%")
        ttk.Button(
            self.report_toolbar,
            text="＋",
            width=3,
            command=lambda: self._change_report_zoom(0.1),
        ).pack(side=RIGHT, padx=(7, 0))
        ttk.Label(
            self.report_toolbar,
            textvariable=self.report_zoom_label_var,
            width=6,
            anchor="center",
        ).pack(side=RIGHT)
        ttk.Button(
            self.report_toolbar,
            text="－",
            width=3,
            command=lambda: self._change_report_zoom(-0.1),
        ).pack(side=RIGHT)
        text_frame = ttk.Frame(self.report_panel)
        text_frame.pack(fill=BOTH, expand=True, pady=(8, 0))
        self.report_view = None
        self.report_text: tk.Text | None = None
        if HtmlFrame is not None:
            self.report_view = HtmlFrame(
                text_frame,
                messages_enabled=False,
                vertical_scrollbar=True,
                horizontal_scrollbar="auto",
                selection_enabled=True,
                images_enabled=False,
                javascript_enabled=False,
                on_link_click=self._open_report_link,
            )
            self.report_view.pack(fill=BOTH, expand=True)
        else:
            scrollbar = ttk.Scrollbar(text_frame, orient=VERTICAL)
            scrollbar.pack(side=RIGHT, fill=Y)
            self.report_text = tk.Text(
                text_frame,
                wrap="word",
                font=("Segoe UI", 10),
                padx=18,
                pady=16,
                yscrollcommand=scrollbar.set,
                background="#ffffff",
                foreground="#172033",
                relief="flat",
            )
            self.report_text.pack(side=LEFT, fill=BOTH, expand=True)
            scrollbar.configure(command=self.report_text.yview)
        self._set_report(
            self._t(
                "欢迎使用 OpenThesis。\n\n"
                "第一步：搜索或快捷选择公司；第二步：确认研究模块和模型设置；"
                "第三步：点击页面顶部始终可见的“开始研究”。\n\n"
                "可以选择“合成演示公司”离线验证完整流程。研究真实公司时，"
                "请在“模型与 SEC 设置”中填写你自己的 SEC 联系邮箱。"
            )
        )
        self._update_start_state()

    def _build_history_tab(self) -> None:
        toolbar = ttk.Frame(self.history_tab)
        toolbar.pack(fill=X, pady=(0, 8))
        ttk.Label(toolbar, text="本地研究历史", font=("Segoe UI", 12, "bold")).pack(
            side=LEFT
        )
        ttk.Button(toolbar, text="刷新", command=self._refresh_history).pack(side=RIGHT)
        columns = ("ticker", "name", "status", "started_at")
        self.history_tree = ttk.Treeview(
            self.history_tab, columns=columns, show="headings", selectmode="browse"
        )
        for column, label, width in (
            ("ticker", "代码", 90),
            ("name", "公司", 320),
            ("status", "状态", 110),
            ("started_at", "开始时间", 220),
        ):
            self.history_tree.heading(column, text=label)
            self.history_tree.column(column, width=width, anchor=W)
        self.history_tree.pack(fill=BOTH, expand=True)
        self.history_tree.bind("<Double-1>", self._open_history)

    def _build_model_tab(self) -> None:
        self.model_settings_scroll = VerticalScrolledFrame(self.model_tab)
        self.model_settings_scroll.pack(fill=BOTH, expand=True)
        container = ttk.Frame(
            self.model_settings_scroll.content, padding=(0, 0, 12, 14), width=760
        )
        container.pack(anchor=W, fill=X, expand=True)
        ttk.Label(
            container, text="模型与数据源设置", font=("Segoe UI", 13, "bold")
        ).pack(anchor=W, pady=(0, 10))

        self.provider_var = tk.StringVar(value="none")
        self.model_preset_var = tk.StringVar(
            value=model_preset_label("none", self.ui_language)
        )
        self.model_var = tk.StringVar()
        self.base_url_var = tk.StringVar()
        self.api_key_var = tk.StringVar()
        self.model_catalog_status_var = tk.StringVar(
            value=self._t("请选择提供方预设。")
        )
        self.sec_profile_var = tk.StringVar(
            value=sec_profile_label(SEC_DEFAULT_PROFILE, self.ui_language)
        )
        self.sec_email_var = tk.StringVar()
        self.sec_user_agent_var = tk.StringVar()

        model_frame = ttk.LabelFrame(
            container, text="主 AI 模型（可选）", padding=12
        )
        model_frame.pack(fill=X)
        self._build_model_selector(model_frame, compare=False)
        ttk.Label(
            model_frame,
            text=(
                "首次启动不会调用 AI；只有主动选择模型并开始研究时才会发送研究上下文。"
                "API Key 只保存在内存中，不写入数据库或日志。"
            ),
            style="Subtitle.TLabel",
            wraplength=760,
        ).grid(row=6, column=0, columnspan=3, sticky=W, pady=(8, 0))

        sec_frame = ttk.LabelFrame(
            container, text="SEC EDGAR 财报访问", padding=12
        )
        sec_frame.pack(fill=X, pady=(12, 0))
        sec_frame.columnconfigure(1, weight=1)
        ttk.Label(
            sec_frame,
            text="SEC 不需要 API Key，但要求请求者提供真实、可联系的邮箱。",
            style="Subtitle.TLabel",
            wraplength=620,
        ).grid(row=0, column=0, columnspan=2, sticky=W, pady=(0, 8))
        self.sec_help_button = ttk.Button(
            sec_frame,
            text="帮助：SEC 是什么，如何获取财报？",
            command=self._show_sec_help,
        )
        self.sec_help_button.grid(row=0, column=2, sticky="e", padx=(12, 0))

        ttk.Label(sec_frame, text="常用请求身份模板").grid(
            row=1, column=0, sticky=W, padx=(0, 12), pady=6
        )
        sec_profile_combo = ttk.Combobox(
            sec_frame,
            textvariable=self.sec_profile_var,
            state="readonly",
            values=self._sec_profile_labels,
            width=28,
        )
        sec_profile_combo.grid(row=1, column=1, columnspan=2, sticky="ew", pady=6)
        sec_profile_combo.bind(
            "<<ComboboxSelected>>", lambda _event: self._refresh_sec_preview()
        )

        ttk.Label(sec_frame, text="联系邮箱（填写你自己的）").grid(
            row=2, column=0, sticky=W, padx=(0, 12), pady=6
        )
        ttk.Entry(
            sec_frame, textvariable=self.sec_email_var, width=52
        ).grid(row=2, column=1, columnspan=2, sticky="ew", pady=6)

        ttk.Label(sec_frame, text="发送给 SEC 的请求标识").grid(
            row=3, column=0, sticky=W, padx=(0, 12), pady=6
        )
        ttk.Label(
            sec_frame,
            textvariable=self.sec_user_agent_var,
            style="Subtitle.TLabel",
            wraplength=660,
        ).grid(row=3, column=1, columnspan=2, sticky=W, pady=6)
        ttk.Label(
            sec_frame,
            text=(
                "请勿填写目标公司的投资者关系邮箱。这里标识的是数据请求者。"
                "邮箱保存在本机设置，并随 SEC 请求发送。"
            ),
            foreground="#92400e",
            wraplength=760,
        ).grid(row=4, column=0, columnspan=3, sticky=W, pady=(6, 0))

        buttons = ttk.Frame(container)
        buttons.pack(fill=X, pady=(12, 0))
        ttk.Button(
            buttons,
            text="保存本机设置（不保存 API Key）",
            command=self._save_settings,
        ).pack(side=LEFT)
        ttk.Button(buttons, text="测试模型连接", command=self._test_model).pack(
            side=LEFT, padx=(8, 0)
        )

        self.comparison_expanded = tk.BooleanVar(value=False)
        self.comparison_toggle_button = ttk.Button(
            container,
            text="▶ 可选：第二个对比模型",
            command=self._toggle_comparison_model,
        )
        self.comparison_toggle_button.pack(fill=X, pady=(12, 0))
        self.comparison_frame = ttk.LabelFrame(
            container, text="第二个对比模型", padding=10
        )
        self.compare_provider_var = tk.StringVar(value="none")
        self.compare_model_preset_var = tk.StringVar(
            value=model_preset_label("none", self.ui_language)
        )
        self.compare_model_var = tk.StringVar()
        self.compare_base_url_var = tk.StringVar()
        self.compare_api_key_var = tk.StringVar()
        self.compare_model_catalog_status_var = tk.StringVar(
            value=self._t("请选择提供方预设。")
        )
        self._build_model_selector(self.comparison_frame, compare=True)
        self.sec_email_var.trace_add("write", self._refresh_sec_preview)

    def _build_model_selector(
        self, parent: ttk.LabelFrame, *, compare: bool
    ) -> None:
        preset_var = (
            self.compare_model_preset_var if compare else self.model_preset_var
        )
        model_var = self.compare_model_var if compare else self.model_var
        base_url_var = (
            self.compare_base_url_var if compare else self.base_url_var
        )
        api_key_var = self.compare_api_key_var if compare else self.api_key_var
        status_var = (
            self.compare_model_catalog_status_var
            if compare
            else self.model_catalog_status_var
        )
        ttk.Label(parent, text="提供方预设").grid(
            row=0, column=0, sticky=W, padx=(0, 12), pady=5
        )
        preset_combo = ttk.Combobox(
            parent,
            textvariable=preset_var,
            state="readonly",
            values=self._preset_labels,
            width=52,
        )
        preset_combo.grid(row=0, column=1, columnspan=2, sticky="ew", pady=5)
        preset_combo.bind(
            "<<ComboboxSelected>>",
            lambda _event: self._model_preset_changed(compare=compare),
        )

        ttk.Label(parent, text="模型名称").grid(
            row=1, column=0, sticky=W, padx=(0, 12), pady=5
        )
        model_combo = ttk.Combobox(
            parent, textvariable=model_var, state="normal", width=52
        )
        model_combo.grid(row=1, column=1, columnspan=2, sticky="ew", pady=5)
        if compare:
            self.compare_model_combo = model_combo
        else:
            self.model_combo = model_combo

        ttk.Label(parent, text="在线目录").grid(
            row=2, column=0, sticky=W, padx=(0, 12), pady=5
        )
        ttk.Button(
            parent,
            text="刷新在线模型",
            command=lambda: self._refresh_online_models(compare=compare),
        ).grid(row=2, column=1, sticky=W, pady=5)
        ttk.Label(
            parent,
            textvariable=status_var,
            style="Subtitle.TLabel",
            wraplength=480,
        ).grid(row=2, column=2, sticky=W, padx=(8, 0), pady=5)

        ttk.Label(parent, text="接口地址").grid(
            row=3, column=0, sticky=W, padx=(0, 12), pady=5
        )
        ttk.Entry(parent, textvariable=base_url_var, width=56).grid(
            row=3, column=1, columnspan=2, sticky="ew", pady=5
        )

        ttk.Label(parent, text="API Key（仅本次会话）").grid(
            row=4, column=0, sticky=W, padx=(0, 12), pady=5
        )
        ttk.Entry(parent, textvariable=api_key_var, width=56, show="*").grid(
            row=4, column=1, columnspan=2, sticky="ew", pady=5
        )

        ttk.Label(parent, text="帮助").grid(
            row=5, column=0, sticky=W, padx=(0, 12), pady=5
        )
        help_button = ttk.Button(
            parent,
            text="获取 API Key / 安装帮助",
            command=lambda: self._open_model_help(compare=compare),
        )
        help_button.grid(row=5, column=1, sticky=W, pady=5)
        if compare:
            self.compare_model_help_button = help_button
        else:
            self.model_help_button = help_button
        parent.columnconfigure(1, weight=1)
        parent.columnconfigure(2, weight=2)

    def _build_packs_tab(self) -> None:
        toolbar = ttk.Frame(self.packs_tab)
        toolbar.pack(fill=X, pady=(0, 8))
        ttk.Label(
            toolbar, text=".othesis 研究模块", font=("Segoe UI", 12, "bold")
        ).pack(side=LEFT)
        ttk.Button(toolbar, text="导入模块", command=self._import_pack).pack(side=RIGHT)
        self.packs_list = tk.Listbox(self.packs_tab, height=12, exportselection=False)
        self.packs_list.pack(fill=X)
        ttk.Label(
            self.packs_tab,
            text=(
                "v0.1 模块仅允许 Markdown、JSON 兼容 YAML、JSON Schema 和文本；"
                "不允许运行代码、访问文件系统、网络或密钥。"
            ),
            style="Subtitle.TLabel",
        ).pack(anchor=W, pady=(10, 0))

    def _build_thesis_tab(self) -> None:
        outer = ttk.Panedwindow(self.thesis_tab, orient=HORIZONTAL)
        outer.pack(fill=BOTH, expand=True)
        left = ttk.Frame(outer, padding=(0, 0, 10, 0))
        right = ttk.Frame(outer)
        outer.add(left, weight=1)
        outer.add(right, weight=2)

        ttk.Label(left, text="投资逻辑版本", font=("Segoe UI", 12, "bold")).pack(
            anchor=W, pady=(0, 8)
        )
        self.thesis_tree = ttk.Treeview(
            left,
            columns=("ticker", "version", "created_by", "created_at"),
            show="headings",
            height=18,
        )
        for column, label, width in (
            ("ticker", "代码", 70),
            ("version", "版本", 60),
            ("created_by", "创建者", 150),
            ("created_at", "时间", 180),
        ):
            self.thesis_tree.heading(column, text=label)
            self.thesis_tree.column(column, width=width, anchor=W)
        self.thesis_tree.pack(fill=BOTH, expand=True)
        self.thesis_tree.bind("<<TreeviewSelect>>", self._open_thesis)
        ttk.Button(left, text="刷新", command=self._refresh_theses).pack(
            fill=X, pady=(7, 0)
        )

        toolbar = ttk.Frame(right)
        toolbar.pack(fill=X, pady=(0, 8))
        ttk.Label(toolbar, text="可编辑 Thesis JSON", font=("Segoe UI", 12, "bold")).pack(
            side=LEFT
        )
        ttk.Button(
            toolbar, text="另存为新版本", command=self._save_thesis_edit
        ).pack(side=RIGHT)
        self.thesis_editor = tk.Text(
            right, wrap="word", font=("Consolas", 10), padx=10, pady=10
        )
        self.thesis_editor.pack(fill=BOTH, expand=True)
        self.editing_thesis_cik = ""

    def _build_settings_tab(self) -> None:
        container = ttk.Frame(self.settings_tab, padding=(4, 4, 4, 12))
        container.pack(fill=X)
        ttk.Label(
            container,
            text="界面与报告语言",
            font=("Segoe UI", 13, "bold"),
        ).grid(row=0, column=0, columnspan=2, sticky=W, pady=(0, 14))

        self.ui_language_var = tk.StringVar(value=language_name(self.ui_language))
        self.report_language_var = tk.StringVar(
            value=language_name(self.report_language)
        )
        language_values = tuple(LANGUAGE_NAMES.values())

        ttk.Label(container, text="界面语言").grid(
            row=1, column=0, sticky=W, pady=6
        )
        self.ui_language_combo = ttk.Combobox(
            container,
            textvariable=self.ui_language_var,
            values=language_values,
            state="readonly",
            width=28,
        )
        self.ui_language_combo.grid(
            row=1, column=1, sticky=W, padx=(18, 0), pady=6
        )
        ttk.Label(
            container,
            text="界面语言将在下次启动时生效。",
            style="Subtitle.TLabel",
        ).grid(row=2, column=1, sticky=W, padx=(18, 0), pady=(0, 10))

        ttk.Label(container, text="研究报告语言").grid(
            row=3, column=0, sticky=W, pady=6
        )
        self.report_language_combo = ttk.Combobox(
            container,
            textvariable=self.report_language_var,
            values=language_values,
            state="readonly",
            width=28,
        )
        self.report_language_combo.grid(
            row=3, column=1, sticky=W, padx=(18, 0), pady=6
        )
        ttk.Label(
            container,
            text=(
                "报告语言立即用于下一次研究；历史报告只翻译程序生成的标题，"
                "AI 正文保持原文。"
            ),
            style="Subtitle.TLabel",
            wraplength=720,
        ).grid(row=4, column=1, sticky=W, padx=(18, 0), pady=(0, 14))

        self.save_language_button = ttk.Button(
            container,
            text="保存语言设置",
            command=self._save_language_settings,
        )
        self.save_language_button.grid(
            row=5, column=1, sticky=W, padx=(18, 0)
        )
        self.language_settings_status_var = tk.StringVar()
        ttk.Label(
            container,
            textvariable=self.language_settings_status_var,
            style="Subtitle.TLabel",
            wraplength=720,
        ).grid(row=6, column=1, sticky=W, padx=(18, 0), pady=(10, 0))
        container.columnconfigure(1, weight=1)

    def _language_code_from_name(self, value: str) -> str:
        for code, name in LANGUAGE_NAMES.items():
            if value == name:
                return code
        return "zh-CN"

    def _save_language_settings(self) -> None:
        selected_ui = self._language_code_from_name(self.ui_language_var.get())
        selected_report = self._language_code_from_name(
            self.report_language_var.get()
        )
        self.storage.set_setting("ui_language", selected_ui)
        self.storage.set_setting("report_language", selected_report)
        self.report_language = selected_report
        notices = [self._t("语言设置已保存。")]
        if selected_ui != self.ui_language:
            notices.append(self._t("界面语言将在重启 OpenThesis 后生效。"))
        notices.append(self._t("报告语言已应用于下一次研究。"))
        message = " ".join(notices)
        self.language_settings_status_var.set(message)
        self.status_var.set(message)
        if self.current_run_id:
            self._display_run(self.current_run_id)

    def _build_about_tab(self) -> None:
        text = "\n\n".join(
            (
                f"OpenThesis {__version__}",
                self._t("面向个人长期投资者的开源、模型无关公司研究系统。"),
                self._t(
                    "原则：每个事实都需要证据；财务计算由确定性程序完成；"
                    "预测使用情景、区间和失效条件；AI 不执行任何交易。"
                ),
                self._t("本地数据目录：{path}", path=self.storage.data_dir),
            )
        )
        ttk.Label(
            self.about_tab,
            text=text,
            justify=LEFT,
            wraplength=780,
            font=("Segoe UI", 11),
        ).pack(anchor=W)

    def _toggle_dcf(self) -> None:
        expanded = not self.dcf_expanded.get()
        self.dcf_expanded.set(expanded)
        if expanded:
            self.dcf_toggle_button.configure(
                text=self._t("▼ 高级设置：反向 DCF")
            )
            self.valuation_frame.pack(fill=X, pady=(6, 0))
            self.research_controls_scroll.scroll_to_bottom()
        else:
            self.dcf_toggle_button.configure(
                text=self._t("▶ 高级设置：反向 DCF")
            )
            self.valuation_frame.pack_forget()

    def _toggle_comparison_model(self) -> None:
        expanded = not self.comparison_expanded.get()
        self.comparison_expanded.set(expanded)
        if expanded:
            self.comparison_toggle_button.configure(
                text=self._t("▼ 可选：第二个对比模型")
            )
            self.comparison_frame.pack(fill=X, pady=(6, 0))
            self.model_settings_scroll.scroll_to_bottom()
        else:
            self.comparison_toggle_button.configure(
                text=self._t("▶ 可选：第二个对比模型")
            )
            self.comparison_frame.pack_forget()

    def _load_settings(self) -> None:
        provider = self.storage.get_setting("provider", "none")
        model = self.storage.get_setting("model", "")
        base_url = self.storage.get_setting("base_url", "")
        saved_preset = self.storage.get_setting("model_preset", "")
        preset = get_model_preset(saved_preset) if saved_preset else infer_model_preset(
            provider, base_url
        )
        self.model_preset_var.set(
            model_preset_label(preset.preset_id, self.ui_language)
        )
        self.provider_var.set(provider if provider else preset.protocol)
        self.model_var.set(model)
        self.base_url_var.set(base_url or preset.base_url)
        self._configure_model_choices(compare=False, preset=preset)
        saved_profile = sec_profile_id_from_label(
            self.storage.get_setting("sec_contact_profile", SEC_DEFAULT_PROFILE)
        )
        self.sec_profile_var.set(
            sec_profile_label(saved_profile, self.ui_language)
        )
        saved_email = self.storage.get_setting("sec_contact_email", "")
        if not saved_email:
            saved_email = extract_sec_contact_email(
                self.storage.get_setting("sec_user_agent", "")
            )
        self.sec_email_var.set(saved_email)
        self._refresh_sec_preview()
        compare_provider = self.storage.get_setting("compare_provider", "none")
        compare_model = self.storage.get_setting("compare_model", "")
        compare_base_url = self.storage.get_setting("compare_base_url", "")
        saved_compare_preset = self.storage.get_setting("compare_model_preset", "")
        compare_preset = (
            get_model_preset(saved_compare_preset)
            if saved_compare_preset
            else infer_model_preset(compare_provider, compare_base_url)
        )
        self.compare_model_preset_var.set(
            model_preset_label(compare_preset.preset_id, self.ui_language)
        )
        self.compare_provider_var.set(
            compare_provider if compare_provider else compare_preset.protocol
        )
        self.compare_model_var.set(compare_model)
        self.compare_base_url_var.set(compare_base_url or compare_preset.base_url)
        self._configure_model_choices(compare=True, preset=compare_preset)

    def _save_settings(self) -> bool:
        email = self.sec_email_var.get().strip()
        user_agent = ""
        if email:
            try:
                user_agent = self._sec_user_agent_value()
            except ValueError as exc:
                messagebox.showerror(
                    self._t("SEC 联系邮箱无效"), self._error_text(str(exc))
                )
                self.notebook.select(self.model_tab)
                return False
        self.storage.set_setting("provider", self.provider_var.get())
        self.storage.set_setting(
            "model_preset",
            get_model_preset(self.model_preset_var.get()).preset_id,
        )
        self.storage.set_setting("model", self.model_var.get().strip())
        self.storage.set_setting("base_url", self.base_url_var.get().strip())
        self.storage.set_setting(
            "sec_contact_profile",
            sec_profile_id_from_label(self.sec_profile_var.get()),
        )
        self.storage.set_setting("sec_contact_email", email)
        self.storage.set_setting("sec_user_agent", user_agent)
        self.storage.set_setting("compare_provider", self.compare_provider_var.get())
        self.storage.set_setting(
            "compare_model_preset",
            get_model_preset(self.compare_model_preset_var.get()).preset_id,
        )
        self.storage.set_setting(
            "compare_model", self.compare_model_var.get().strip()
        )
        self.storage.set_setting(
            "compare_base_url", self.compare_base_url_var.get().strip()
        )
        self.status_var.set(self._t("设置已保存；API Key 未持久化"))
        self._refresh_sec_preview()
        return True

    def _refresh_sec_preview(self, *_args: object) -> None:
        email = self.sec_email_var.get().strip()
        if not email:
            self.sec_user_agent_var.set(
                self._t("填写邮箱后自动生成，无需申请 SEC API Key")
            )
            return
        try:
            self.sec_user_agent_var.set(self._sec_user_agent_value())
        except ValueError:
            self.sec_user_agent_var.set(self._t("邮箱格式尚未完成"))

    def _sec_user_agent_value(self) -> str:
        return build_sec_user_agent(
            self.sec_profile_var.get(),
            self.sec_email_var.get(),
        )

    def _show_sec_help(self) -> None:
        help_window = tk.Toplevel(self.root)
        help_window.title(self._t("SEC EDGAR 使用帮助"))
        help_window.transient(self.root)
        help_window.geometry("680x480")
        help_window.minsize(600, 420)

        ttk.Label(
            help_window,
            text=self._t("SEC 是什么，OpenThesis 如何获取财报？"),
            font=("Segoe UI", 14, "bold"),
        ).pack(anchor=W, padx=18, pady=(16, 8))
        help_text = tk.Text(
            help_window,
            wrap="word",
            font=("Segoe UI", 10),
            padx=12,
            pady=12,
            background="#fbfbfb",
            height=16,
        )
        help_text.pack(fill=BOTH, expand=True, padx=18)
        help_content = (
            (
                "SEC is the U.S. Securities and Exchange Commission. EDGAR is "
                "its public company filing database, providing 10-K, 10-Q, and "
                "structured Company Facts data.\n\n"
                "OpenThesis needs neither an API key nor an SEC account to "
                "retrieve this public data. SEC requires automated requests to "
                "carry a User-Agent so the requester can be contacted if "
                "traffic causes a problem.\n\n"
                "How to configure it correctly:\n"
                "1. Select the requester profile that describes you;\n"
                "2. Enter a working email belonging to you or your research team;\n"
                "3. Click “Save Local Settings”;\n"
                "4. Return to “Company Research”, select a company, and click "
                "“Start Research” at the top.\n\n"
                "Do not enter the researched company's investor-relations email "
                "or impersonate the company. Built-in common companies are only "
                "research targets and are unrelated to the requester email.\n\n"
                f"Example: OpenThesis/{__version__} (Personal Investor; "
                "contact: your-name@example.com)"
            )
            if self.ui_language == EN
            else (
                "SEC 是美国证券交易委员会。EDGAR 是其公开公司申报数据库，"
                "可获取 10-K、10-Q 和结构化 Company Facts 等资料。\n\n"
                "OpenThesis 获取这些公开数据不需要 API Key，也不需要注册 SEC 账号。"
                "SEC 要求自动化请求携带 User-Agent，以便出现异常流量时联系请求者。\n\n"
                "正确填写方法：\n"
                "1. 选择与你相符的常用请求身份模板；\n"
                "2. 填写你本人或所在研究团队能够正常收信的邮箱；\n"
                "3. 点击“保存本机设置”；\n"
                "4. 回到“公司研究”，选择公司并点击顶部“开始研究”。\n\n"
                "不要填写被研究公司的投资者关系邮箱，也不要冒充目标公司。"
                "内置常用公司只用于快速选择研究对象，与请求者联系邮箱无关。\n\n"
                f"示例：OpenThesis/{__version__} (Personal Investor; "
                "contact: your-name@example.com)"
            )
        )
        help_text.insert("1.0", help_content)
        help_text.configure(state="disabled")
        help_buttons = ttk.Frame(help_window, padding=18)
        help_buttons.pack(fill=X)
        ttk.Button(
            help_buttons,
            text=self._t("打开 SEC 官方开发者说明"),
            command=lambda: webbrowser.open_new_tab(
                "https://www.sec.gov/search-filings/edgar-application-programming-interfaces"
            ),
        ).pack(side=LEFT)
        ttk.Button(
            help_buttons, text=self._t("关闭"), command=help_window.destroy
        ).pack(side=RIGHT)

    def _model_preset_changed(self, *, compare: bool) -> None:
        preset_var = (
            self.compare_model_preset_var if compare else self.model_preset_var
        )
        provider_var = self.compare_provider_var if compare else self.provider_var
        model_var = self.compare_model_var if compare else self.model_var
        base_url_var = (
            self.compare_base_url_var if compare else self.base_url_var
        )
        preset = get_model_preset(preset_var.get())
        provider_var.set(preset.protocol)
        base_url_var.set(preset.base_url)
        model_var.set(preset.recommended_models[0] if preset.recommended_models else "")
        self._model_refresh_generation["compare" if compare else "primary"] += 1
        self._configure_model_choices(compare=compare, preset=preset)

    def _configure_model_choices(
        self, *, compare: bool, preset: ModelPreset | None = None
    ) -> None:
        selected = (
            get_model_preset(self.compare_model_preset_var.get())
            if compare
            else get_model_preset(self.model_preset_var.get())
        )
        if preset is not None:
            selected = preset
        combo = self.compare_model_combo if compare else self.model_combo
        status_var = (
            self.compare_model_catalog_status_var
            if compare
            else self.model_catalog_status_var
        )
        model_var = self.compare_model_var if compare else self.model_var
        base_url_var = (
            self.compare_base_url_var if compare else self.base_url_var
        )
        cache_key = (selected.preset_id, base_url_var.get().strip())
        online = self._model_catalog_cache.get(cache_key, ())
        choices = merge_model_ids(selected.recommended_models, online)
        current = model_var.get().strip()
        if current and current not in choices:
            choices = (*choices, current)
        combo.configure(values=choices)
        if selected.preset_id == "none":
            status_var.set(self._t("当前不会调用 AI。"))
        elif online:
            status_var.set(
                self._t(
                    "本次会话已缓存 {count} 个在线模型。",
                    count=len(online),
                )
            )
        elif selected.models_path is None:
            status_var.set(
                self._t("此提供方使用内置模型列表，也可手动填写模型 ID。")
            )
        elif selected.preset_id == "ollama":
            status_var.set(
                self._t("可刷新本机已安装模型；未安装时请先使用帮助链接。")
            )
        else:
            status_var.set(
                self._t("已加载内置推荐模型；可手动刷新在线目录。")
            )

    def _refresh_online_models(self, *, compare: bool) -> None:
        preset_var = (
            self.compare_model_preset_var if compare else self.model_preset_var
        )
        base_url_var = (
            self.compare_base_url_var if compare else self.base_url_var
        )
        api_key_var = self.compare_api_key_var if compare else self.api_key_var
        status_var = (
            self.compare_model_catalog_status_var
            if compare
            else self.model_catalog_status_var
        )
        preset = get_model_preset(preset_var.get())
        if preset.preset_id == "none":
            status_var.set(self._t("当前未启用 AI，无需刷新。"))
            return
        base_url = base_url_var.get().strip()
        api_key = api_key_var.get()
        slot = "compare" if compare else "primary"
        self._model_refresh_generation[slot] += 1
        generation = self._model_refresh_generation[slot]
        cache_key = (preset.preset_id, base_url)
        cached = self._model_catalog_cache.get(cache_key)
        if cached:
            self._configure_model_choices(compare=compare, preset=preset)
            return
        status_var.set(self._t("正在后台刷新在线模型…"))

        def runner() -> None:
            try:
                models = discover_models(preset, base_url, api_key)
                payload: dict[str, object] = {
                    "slot": slot,
                    "generation": generation,
                    "preset_id": preset.preset_id,
                    "base_url": base_url,
                    "models": models,
                    "error": "",
                }
            except ModelDiscoveryError as exc:
                payload = {
                    "slot": slot,
                    "generation": generation,
                    "preset_id": preset.preset_id,
                    "base_url": base_url,
                    "models": (),
                    "error": str(exc),
                }
            except Exception:
                payload = {
                    "slot": slot,
                    "generation": generation,
                    "preset_id": preset.preset_id,
                    "base_url": base_url,
                    "models": (),
                    "error": self._t(
                        "在线模型目录刷新失败，已保留内置列表。"
                    ),
                }
            self.event_queue.put(("model_catalog", payload))

        threading.Thread(target=runner, daemon=True).start()

    def _open_model_help(self, *, compare: bool) -> None:
        preset_var = (
            self.compare_model_preset_var if compare else self.model_preset_var
        )
        preset = get_model_preset(preset_var.get())
        if preset.help_url:
            webbrowser.open_new_tab(preset.help_url)
        else:
            messagebox.showinfo(
                self._t("模型帮助"),
                self._t(
                    "自定义接口请向服务提供方获取 API Key、模型 ID 和兼容地址。"
                ),
            )

    def _model_config(self) -> ModelConfig:
        preset = get_model_preset(self.model_preset_var.get())
        return ModelConfig(
            provider=self.provider_var.get(),
            model=self.model_var.get().strip(),
            base_url=self.base_url_var.get().strip(),
            api_key=self.api_key_var.get(),
            temperature=preset.temperature,
        )

    def _comparison_model_config(self) -> ModelConfig:
        preset = get_model_preset(self.compare_model_preset_var.get())
        return ModelConfig(
            provider=self.compare_provider_var.get(),
            model=self.compare_model_var.get().strip(),
            base_url=self.compare_base_url_var.get().strip(),
            api_key=self.compare_api_key_var.get(),
            temperature=preset.temperature,
        )

    def _selected_pack(self) -> ResearchPack:
        label = self.pack_var.get()
        return self.pack_by_label.get(label) or builtin_pack()

    def _valuation_inputs(self) -> dict[str, float] | None:
        raw_market_cap = self.market_cap_var.get().strip()
        if not raw_market_cap:
            return None
        try:
            market_cap = float(raw_market_cap) * 1_000_000_000
            discount = float(self.discount_rate_var.get()) / 100
            terminal = float(self.terminal_growth_var.get()) / 100
        except ValueError as exc:
            raise ValueError(self._t("反向 DCF 输入必须是数字")) from exc
        if market_cap <= 0 or discount <= terminal:
            raise ValueError(
                self._t("市值必须为正数，且折现率必须高于永续增长率")
            )
        return {
            "market_cap": market_cap,
            "discount_rate": discount,
            "terminal_growth": terminal,
        }

    def _search_company(self) -> None:
        query_text = self.company_query_var.get().strip()
        if not query_text:
            messagebox.showinfo(
                self._t("搜索公司"), self._t("请输入股票代码或公司名称。")
            )
            return
        try:
            user_agent_text = self._sec_user_agent_value()
        except ValueError as exc:
            messagebox.showinfo(
                self._t("需要 SEC 联系邮箱"),
                self._t(
                    "{error}\n\n请在“模型与数据源设置”中填写后保存。",
                    error=self._error_text(str(exc)),
                ),
            )
            self.notebook.select(self.model_tab)
            return
        self._run_background(
            lambda: SecClient(
                user_agent_text, self.storage.data_dir / "sec-cache"
            ).search_companies(query_text),
            "company_results",
            self._t("正在查询 SEC 公司列表…"),
        )

    def _select_company(self, _event: object = None) -> None:
        selection = self.company_list.curselection()
        if not selection:
            return
        self.selected_company = self.company_results[selection[0]]
        self.selected_company_var.set(
            f"{self.selected_company.ticker} · {self.selected_company.name}"
        )
        self._update_start_state()

    def _select_common_company(self) -> None:
        try:
            company = get_common_company(self.common_company_var.get())
        except ValueError as exc:
            messagebox.showinfo(
                self._t("选择常用公司"), self._error_text(str(exc))
            )
            return
        self.selected_company = company
        self.selected_company_var.set(f"{company.ticker} · {company.name}")
        self._set_report(
            self._t(
                "已选择常用公司：{ticker} · {name}。\n\n"
                "请确认研究配置，然后点击页面顶部的“开始研究”。",
                ticker=company.ticker,
                name=company.name,
            )
        )
        self._update_start_state()

    def _select_demo_company(self) -> None:
        self.selected_company = DEMO_COMPANY
        self.selected_company_var.set(
            f"{DEMO_COMPANY.ticker} · {self._company_display_name(DEMO_COMPANY)}"
        )
        self._set_report(
            self._t(
                "已选择合成演示公司。所有数据均为虚构，只用于验证软件功能。"
            )
        )
        self._update_start_state()

    def _show_interrupted_recovery(self, count: int) -> None:
        self.research_feedback_title_var.set(
            self._t("已恢复上次异常中断的任务记录（{count}）", count=count)
        )
        self.research_feedback_detail_var.set(
            self._t(
                "上次关闭应用时仍有研究在运行，现已安全标记为“已取消”；"
                "已完成的中间产物仍可在研究历史中查看。"
            )
        )
        self.research_error_var.set(
            self._t("如需继续，请重新选择公司并运行研究。")
        )
        self.research_error_actions.grid()

    def _begin_research_feedback(self, company: Company) -> None:
        self._research_started_monotonic = time.monotonic()
        self._research_last_progress_monotonic = self._research_started_monotonic
        self._research_progress_message = self._t(
            "正在准备 {ticker} 的研究数据", ticker=company.ticker
        )
        self._research_progress_percent = 2
        self._research_activity_lines = []
        self._last_research_error = ""
        self.research_error_var.set("")
        self.research_error_actions.grid_remove()
        self.research_elapsed_var.set(self._t("已用时 00:00"))
        self._set_research_progress(
            self._research_progress_message, 2, record=True
        )

    def _set_research_progress(
        self, message: str, percent: int, *, record: bool = True
    ) -> None:
        bounded = max(0, min(100, int(percent)))
        self._research_progress_message = message
        self._research_progress_percent = bounded
        self._research_last_progress_monotonic = time.monotonic()
        self.research_feedback_title_var.set(message)
        self.research_feedback_detail_var.set(
            self._t("任务正在后台运行；窗口保持响应，可以随时查看当前阶段。")
        )
        self.research_progress_bar["value"] = bounded
        self.progress["value"] = bounded
        self.research_percent_var.set(f"{bounded}%")
        self.status_var.set(message)
        if record:
            elapsed = (
                time.monotonic() - self._research_started_monotonic
                if self._research_started_monotonic
                else 0
            )
            line = f"[{format_elapsed(elapsed)}] {bounded:>3}%  {message}"
            if not self._research_activity_lines or self._research_activity_lines[-1] != line:
                self._research_activity_lines.append(line)
            self._set_report(
                self._t("研究正在进行中\n\n")
                + "\n".join(self._research_activity_lines)
                + "\n\n"
                + self._t("完成后将在这里显示完整报告。")
            )

    def _tick_research_feedback(self) -> None:
        if self.research_running:
            now = time.monotonic()
            elapsed = now - self._research_started_monotonic
            waiting = now - self._research_last_progress_monotonic
            self.research_elapsed_var.set(
                self._t("已用时 {elapsed}", elapsed=format_elapsed(elapsed))
            )
            cancel_requested = bool(
                self._research_cancel_event
                and self._research_cancel_event.is_set()
            )
            if cancel_requested:
                self.research_feedback_detail_var.set(
                    self._t(
                        "取消请求已收到；正在等待当前网络请求安全结束，不会再启动新的研究步骤。"
                    )
                )
            elif waiting >= 5:
                self.research_feedback_detail_var.set(
                    self._t(
                        "后台仍在工作 · 当前步骤已等待 {elapsed} · "
                        "模型研究通常需要数分钟，请勿关闭应用。",
                        elapsed=format_elapsed(waiting),
                    )
                )
        self.root.after(1000, self._tick_research_feedback)

    def _cancel_research(self) -> None:
        if not self.research_running or self._research_cancel_event is None:
            return
        self._research_cancel_event.set()
        self.cancel_research_button.configure(state="disabled")
        self.research_feedback_title_var.set(self._t("正在取消研究…"))
        self.research_feedback_detail_var.set(
            self._t(
                "已停止启动新步骤；当前网络请求结束后会安全保存中间结果。"
            )
        )
        self.status_var.set(self._t("正在安全取消研究…"))

    def _retry_research(self) -> None:
        if self.research_running:
            return
        self.research_error_var.set("")
        self.research_error_actions.grid_remove()
        self._start_research()

    def _sanitize_research_error(self, message: str) -> str:
        sanitized = message
        for variable in (
            self.api_key_var,
            self.compare_api_key_var,
        ):
            secret = variable.get().strip()
            if secret:
                sanitized = sanitized.replace(secret, "[REDACTED]")
        return sanitized[:800]

    def _show_research_error(self, message: str) -> None:
        safe_message = self._sanitize_research_error(message)
        display_message = self._error_text(safe_message)
        title, guidance = friendly_research_error(
            safe_message, self.ui_language
        )
        self._last_research_error = safe_message
        self.research_feedback_title_var.set(title)
        self.research_feedback_detail_var.set(guidance)
        self.research_error_var.set(
            self._t("技术信息：{message}", message=display_message)
        )
        self.research_error_actions.grid()
        self.research_elapsed_var.set(
            self._t(
                "已用时 {elapsed}",
                elapsed=format_elapsed(
                    time.monotonic() - self._research_started_monotonic
                ),
            )
        )
        self._set_report(
            self._t("研究任务未完成\n\n")
            + "\n".join(self._research_activity_lines)
            + f"\n\n{title}\n{guidance}\n\n"
            + self._t("技术信息：{message}", message=display_message)
        )

    def _update_start_state(self) -> None:
        if self.research_running:
            self.run_button.configure(
                state="disabled", text=self._t("研究进行中…")
            )
            self.start_hint_var.set(
                self._t("正在运行多 Agent 研究流程，请查看下方任务进度。")
            )
            cancel_state = (
                "disabled"
                if self._research_cancel_event
                and self._research_cancel_event.is_set()
                else "normal"
            )
            self.cancel_research_button.configure(state=cancel_state)
        elif self.selected_company is None:
            self.run_button.configure(
                state="disabled", text=self._t("开始研究")
            )
            self.start_hint_var.set(self._t("请先在下方选择一家公司。"))
            self.cancel_research_button.configure(state="disabled")
        else:
            self.run_button.configure(state="normal", text=self._t("开始研究"))
            self.start_hint_var.set(
                self._t("公司已选择；确认配置后即可开始。")
            )
            self.cancel_research_button.configure(state="disabled")

    def _start_research(self) -> None:
        company = self.selected_company
        if company is None:
            messagebox.showinfo(
                self._t("开始研究"), self._t("请先选择公司。")
            )
            return
        if not self._save_settings():
            return
        # Capture all Tk values on the UI thread. Tk variables must never be
        # read from the background worker.
        user_agent = ""
        if company.cik != DEMO_COMPANY.cik:
            try:
                user_agent = self._sec_user_agent_value()
            except ValueError as exc:
                messagebox.showerror(
                    self._t("需要 SEC 联系邮箱"),
                    self._t(
                        "{error}\n\n真实公司研究需要访问 SEC，请先完成 SEC 设置。",
                        error=self._error_text(str(exc)),
                    ),
                )
                self.notebook.select(self.model_tab)
                return
        download_filings = self.download_filings_var.get()
        config = self._model_config()
        compare_enabled = self.compare_models_var.get()
        compare_config = self._comparison_model_config()
        selected_pack = self._selected_pack()
        report_language = self.report_language
        try:
            valuation_inputs = self._valuation_inputs()
        except ValueError as exc:
            messagebox.showerror(
                self._t("反向 DCF 输入错误"), str(exc)
            )
            return
        if compare_enabled and (not config.enabled or not compare_config.enabled):
            messagebox.showerror(
                self._t("双模型配置不完整"),
                self._t(
                    "启用模型比较时，主模型和第二模型都必须配置提供方、模型名称和接口地址。"
                ),
            )
            self.notebook.select(self.model_tab)
            return
        cancel_event = threading.Event()
        self._research_cancel_event = cancel_event
        self.research_running = True
        self._update_start_state()
        self._begin_research_feedback(company)

        def task() -> object:
            def check_cancelled() -> None:
                if cancel_event.is_set():
                    raise ResearchCancelled()

            def emit(message: str, percent: int) -> None:
                self.event_queue.put(("progress", (message, percent)))

            def workflow_progress(
                message: str,
                percent: int,
                *,
                base: int,
                span: int,
                prefix: str = "",
            ) -> None:
                mapped = base + round(max(0, min(100, percent)) * span / 100)
                emit(f"{prefix}{message}", mapped)

            filing_evidence: list[dict[str, object]] = []
            check_cancelled()
            if company.cik == DEMO_COMPANY.cik:
                emit(self._t("正在加载离线演示数据"), 8)
                self.storage.save_company(company)
                facts = demo_facts()
                self.storage.save_facts([FinancialFact(**item) for item in facts])
                emit(self._t("演示数据准备完成"), 30)
            else:
                client = SecClient(user_agent, self.storage.data_dir / "sec-cache")
                self.storage.save_company(company)
                emit(self._t("正在获取 SEC 年报清单"), 5)
                filings = client.list_annual_filings(company, limit=5)
                check_cancelled()
                if download_filings:
                    target = self.storage.filings_dir / company.cik
                    downloaded: list[FilingDocument] = []
                    total_filings = max(1, len(filings))
                    for index, filing in enumerate(filings, start=1):
                        check_cancelled()
                        emit(
                            self._t(
                                "正在下载 SEC 10-K（{index}/{total}）",
                                index=index,
                                total=len(filings),
                            ),
                            6 + round(index * 10 / total_filings),
                        )
                        downloaded.append(client.download_filing(filing, target))
                    filings = downloaded
                    check_cancelled()
                    emit(self._t("正在解析财报证据与表格"), 18)
                    filing_evidence = build_filing_evidence(filings)
                self.storage.save_filings(filings)
                check_cancelled()
                emit(self._t("正在获取 SEC Company Facts"), 23)
                normalized = client.get_company_facts(company)
                check_cancelled()
                self.storage.save_facts(normalized)
                facts = [item.to_dict() for item in normalized]
                emit(self._t("研究数据准备完成，正在启动 Agent"), 30)

            check_cancelled()
            provider = create_provider(config)
            workflow = ResearchWorkflow(
                self.storage,
                selected_pack,
                provider,
                config,
                cancel_check=cancel_event.is_set,
                report_language=report_language,
                ui_language=self.ui_language,
            )
            primary = workflow.run(
                company,
                facts,
                filing_evidence=filing_evidence,  # type: ignore[arg-type]
                valuation_inputs=valuation_inputs,
                progress=lambda message, percent: workflow_progress(
                    message,
                    percent,
                    base=30,
                    span=35 if compare_enabled else 70,
                    prefix=self._t("主模型：") if compare_enabled else "",
                ),
            )
            if not compare_enabled:
                return primary

            check_cancelled()
            emit(self._t("主模型研究完成，正在启动对比模型"), 65)
            secondary_provider = create_provider(compare_config)
            secondary_workflow = ResearchWorkflow(
                self.storage,
                selected_pack,
                secondary_provider,
                compare_config,
                cancel_check=cancel_event.is_set,
                report_language=report_language,
                ui_language=self.ui_language,
            )
            secondary = secondary_workflow.run(
                company,
                facts,
                filing_evidence=filing_evidence,  # type: ignore[arg-type]
                valuation_inputs=valuation_inputs,
                progress=lambda message, percent: workflow_progress(
                    message,
                    percent,
                    base=65,
                    span=35,
                    prefix=self._t("对比模型："),
                ),
            )
            check_cancelled()
            compare_research_runs(
                self.storage, primary, secondary, report_language
            )
            emit(self._t("双模型分歧比较完成"), 100)
            return primary

        self._run_background(
            task,
            "research_complete",
            self._t("研究任务正在运行…"),
        )

    def _test_model(self) -> None:
        config = self._model_config()
        if not config.enabled:
            messagebox.showinfo(
                self._t("测试模型"),
                self._t("当前选择 none，不会调用语言模型。"),
            )
            return

        def task() -> str:
            provider = create_provider(config)
            if provider is None:
                return self._t("未配置模型")
            return provider.test_connection()

        self._run_background(
            task, "model_test", self._t("正在测试模型连接…")
        )

    def _refresh_packs(self) -> None:
        self.pack_by_label.clear()
        self.packs_list.delete(0, END)
        for pack in list_installed_packs(self.storage.packs_dir):
            display_name = (
                "OpenThesis Long-term Fundamentals"
                if self.ui_language == EN
                and pack.pack_id == "official.long-term-fundamentals"
                else pack.name
            )
            label = f"{display_name} · {pack.version}"
            self.pack_by_label[label] = pack
            self.packs_list.insert(
                END, f"{label}  [{pack.pack_id}]  {pack.content_hash[:10]}"
            )
        labels = list(self.pack_by_label)
        self.pack_combo.configure(values=labels)
        if labels and self.pack_var.get() not in labels:
            self.pack_var.set(labels[0])

    def _import_pack(self) -> None:
        path = filedialog.askopenfilename(
            title=self._t("导入 OpenThesis 研究模块"),
            filetypes=[("OpenThesis Research Pack", "*.othesis")],
        )
        if not path:
            return
        try:
            pack = install_pack(Path(path), self.storage.packs_dir)
        except (PackValidationError, OSError, ValueError) as exc:
            messagebox.showerror(
                self._t("研究模块验证失败"), self._error_text(str(exc))
            )
            return
        self._refresh_packs()
        messagebox.showinfo(
            self._t("研究模块已安装"),
            self._t(
                "{name}\n版本：{version}\n哈希：{hash}",
                name=pack.name,
                version=pack.version,
                hash=pack.content_hash[:16],
            ),
        )

    def _refresh_history(self) -> None:
        for item in self.history_tree.get_children():
            self.history_tree.delete(item)
        for row in self.storage.list_runs():
            self.history_tree.insert(
                "",
                END,
                iid=row["run_id"],
                values=(
                    row["ticker"],
                    row["name"],
                    run_status_label(str(row["status"]), self.ui_language),
                    row["started_at"],
                ),
            )

    def _refresh_theses(self) -> None:
        if not hasattr(self, "thesis_tree"):
            return
        for item in self.thesis_tree.get_children():
            self.thesis_tree.delete(item)
        for row in self.storage.list_thesis_versions():
            self.thesis_tree.insert(
                "",
                END,
                iid=row["thesis_version_id"],
                values=(
                    row["ticker"],
                    row["version"],
                    row["created_by"],
                    row["created_at"],
                ),
            )

    def _open_thesis(self, _event: object = None) -> None:
        selection = self.thesis_tree.selection()
        if not selection:
            return
        thesis = self.storage.get_thesis_version(selection[0])
        if thesis is None:
            return
        self.editing_thesis_cik = thesis["company_cik"]
        self.thesis_editor.delete("1.0", END)
        self.thesis_editor.insert("1.0", json_pretty(thesis["content"]))

    def _save_thesis_edit(self) -> None:
        if not self.editing_thesis_cik:
            messagebox.showinfo(
                self._t("投资逻辑"), self._t("请先选择一个已有版本。")
            )
            return
        try:
            content = json.loads(self.thesis_editor.get("1.0", END))
        except json.JSONDecodeError as exc:
            messagebox.showerror(
                self._t("JSON 格式错误"),
                self._t(
                    "第 {line} 行，第 {column} 列：{message}",
                    line=exc.lineno,
                    column=exc.colno,
                    message=exc.msg,
                ),
            )
            return
        from .domain import utc_now_iso

        saved = self.storage.save_thesis_version(
            self.editing_thesis_cik,
            content,
            created_by="user",
            created_at=utc_now_iso(),
        )
        self._refresh_theses()
        messagebox.showinfo(
            self._t("投资逻辑"),
            self._t("已保存为 v{version}", version=saved["version"]),
        )

    def _open_history(self, _event: object = None) -> None:
        selection = self.history_tree.selection()
        if not selection:
            return
        run_id = selection[0]
        self._display_run(run_id)
        self.notebook.select(self.research_tab)

    def _display_run(self, run_id: str) -> None:
        artifacts = self.storage.get_artifacts(run_id)
        run = self.storage.get_run(run_id)
        self.current_run_id = run_id
        company_name = str(run["name"]) if run else ""
        if (
            run
            and str(run.get("ticker", "")) == DEMO_COMPANY.ticker
            and self.report_language == EN
        ):
            company_name = "Example Cloud Systems (Synthetic Demo Company)"
        self._report_context = (run_id, artifacts, company_name)
        markdown = render_research_run(
            run_id,
            artifacts,
            self.report_language,
            company_name=company_name,
            include_technical=self.report_technical_visible,
        )
        html_content = render_research_html(
            run_id,
            artifacts,
            self.report_language,
            company_name=company_name,
            include_technical=self.report_technical_visible,
        )
        self._set_report(markdown, html_content=html_content, preserve_context=True)

    def _export_report(self) -> None:
        if not self.current_report_text.strip():
            messagebox.showinfo(
                self._t("导出报告"), self._t("当前没有可导出的内容。")
            )
            return
        path = filedialog.asksaveasfilename(
            parent=self.report_focus_window or self.root,
            title=self._t("导出 OpenThesis 报告"),
            defaultextension=".html",
            filetypes=[
                ("HTML", "*.html"),
                ("Markdown", "*.md"),
                ("Text", "*.txt"),
            ],
        )
        if path:
            content = (
                self.current_report_html
                if Path(path).suffix.lower() in {".html", ".htm"}
                else self.current_report_text
            )
            Path(path).write_text(content, encoding="utf-8")
            self.status_var.set(
                self._t("报告已导出：{path}", path=path)
            )

    def _open_report_link(self, url: str) -> None:
        if url.lower().startswith(("https://", "http://")):
            webbrowser.open_new_tab(url)

    def _toggle_report_technical(self) -> None:
        self.report_technical_visible = not self.report_technical_visible
        self.report_technical_button.configure(
            text=self._t(
                "隐藏技术详情"
                if self.report_technical_visible
                else "显示技术详情"
            )
        )
        if self.report_focus_technical_button is not None:
            self.report_focus_technical_button.configure(
                text=self._t(
                    "隐藏技术详情"
                    if self.report_technical_visible
                    else "显示技术详情"
                )
            )
        if not self._report_context:
            return
        run_id, artifacts, company_name = self._report_context
        markdown = render_research_run(
            run_id,
            artifacts,
            self.report_language,
            company_name=company_name,
            include_technical=self.report_technical_visible,
        )
        html_content = render_research_html(
            run_id,
            artifacts,
            self.report_language,
            company_name=company_name,
            include_technical=self.report_technical_visible,
        )
        self._set_report(markdown, html_content=html_content, preserve_context=True)

    def _set_report(
        self,
        content: str,
        *,
        html_content: str | None = None,
        preserve_context: bool = False,
    ) -> None:
        if not preserve_context:
            self._report_context = None
            self.report_technical_visible = False
            if hasattr(self, "report_technical_button"):
                self.report_technical_button.configure(text=self._t("显示技术详情"))
            if self.report_focus_technical_button is not None:
                self.report_focus_technical_button.configure(
                    text=self._t("显示技术详情")
                )
        self.current_report_text = content
        self.current_report_html = html_content or render_message_html(
            content,
            self.ui_language,
        )
        focus_active = (
            self.report_focus_view is not None
            or self.report_focus_text is not None
        )
        if self.report_view is not None and not focus_active:
            self.report_view.load_html(self.current_report_html)
        if self.report_focus_view is not None:
            self.report_focus_view.load_html(  # type: ignore[attr-defined]
                self.current_report_html
            )
            self.report_focus_view.configure(  # type: ignore[attr-defined]
                zoom=self.report_zoom
            )
        if self.report_focus_text is not None:
            self.report_focus_text.delete("1.0", END)
            self.report_focus_text.insert("1.0", content)
        if focus_active:
            self._main_report_dirty = True
        if self.report_text is None:
            return
        if focus_active:
            return
        self.report_text.delete("1.0", END)
        self.report_text.insert("1.0", content)
        for tag in self._report_link_tags:
            self.report_text.tag_delete(tag)
        self._report_link_tags.clear()
        for index, match in enumerate(re.finditer(r"https?://[^\s<>\])}]+", content)):
            url = match.group(0).rstrip(".,;:，。；：")
            if not url:
                continue
            tag = f"report_url_{index}"
            start = f"1.0 + {match.start()} chars"
            end = f"{start} + {len(url)} chars"
            self.report_text.tag_add(tag, start, end)
            self.report_text.tag_configure(tag, foreground="#075985", underline=True)
            self.report_text.tag_bind(
                tag,
                "<Button-1>",
                lambda _event, target=url: webbrowser.open_new_tab(target),
            )
            self.report_text.tag_bind(
                tag, "<Enter>", lambda _event: self.report_text.configure(cursor="hand2")
            )
            self.report_text.tag_bind(
                tag, "<Leave>", lambda _event: self.report_text.configure(cursor="")
            )
            self._report_link_tags.append(tag)

    def _run_background(
        self,
        task: object,
        success_event: str,
        status_message: str,
    ) -> None:
        self.status_var.set(status_message)

        def runner() -> None:
            try:
                result = task()  # type: ignore[operator]
                self.event_queue.put((success_event, result))
            except ResearchCancelled as exc:
                if success_event == "research_complete":
                    self.event_queue.put(
                        (
                            "research_cancelled",
                            {"run_id": exc.run_id, "message": str(exc)},
                        )
                    )
                else:
                    self.event_queue.put(
                        (
                            "error",
                            {
                                "message": str(exc),
                                "operation": success_event,
                                "traceback": traceback.format_exc(),
                            },
                        )
                    )
            except Exception as exc:
                self.event_queue.put(
                    (
                        "error",
                        {
                            "message": str(exc),
                            "operation": success_event,
                            "traceback": traceback.format_exc(),
                        },
                    )
                )

        threading.Thread(target=runner, daemon=True).start()

    def _drain_events(self) -> None:
        while True:
            try:
                event, payload = self.event_queue.get_nowait()
            except queue.Empty:
                break
            if event == "company_results":
                self.company_results = list(payload)  # type: ignore[arg-type]
                self.company_list.delete(0, END)
                for company in self.company_results:
                    self.company_list.insert(END, f"{company.ticker} · {company.name}")
                self.status_var.set(
                    self._t(
                        "找到 {count} 家公司", count=len(self.company_results)
                    )
                )
            elif event == "progress":
                message, percent = payload  # type: ignore[misc]
                self._set_research_progress(
                    str(message), int(percent), record=True
                )
            elif event == "research_complete":
                run = payload
                self.research_running = False
                self._research_cancel_event = None
                if self._research_started_monotonic is not None:
                    elapsed = time.monotonic() - self._research_started_monotonic
                    self.research_elapsed_var.set(
                        self._t(
                            "已用时 {elapsed}", elapsed=format_elapsed(elapsed)
                        )
                    )
                self._update_start_state()
                self._set_research_progress(
                    self._t("研究完成"), 100, record=False
                )
                self.research_feedback_detail_var.set(
                    self._t(
                        "全部研究阶段已完成，完整报告和中间产物已经保存。"
                    )
                )
                self.research_error_var.set("")
                self.research_error_actions.grid_remove()
                self._refresh_history()
                self._refresh_theses()
                self._display_run(run.run_id)  # type: ignore[attr-defined]
                self.status_var.set(
                    self._t(
                        "研究完成：{status}",
                        status=run_status_label(
                            run.status.value, self.ui_language  # type: ignore[attr-defined]
                        ),
                    )
                )
            elif event == "research_cancelled":
                self.research_running = False
                self._research_cancel_event = None
                if self._research_started_monotonic is not None:
                    elapsed = time.monotonic() - self._research_started_monotonic
                    self.research_elapsed_var.set(
                        self._t(
                            "已用时 {elapsed}", elapsed=format_elapsed(elapsed)
                        )
                    )
                self._update_start_state()
                data = payload  # type: ignore[assignment]
                self.research_feedback_title_var.set(self._t("研究已取消"))
                self.research_feedback_detail_var.set(
                    self._t(
                        "任务已停止；当前步骤之前完成的中间结果已经安全保存。"
                    )
                )
                self.research_error_var.set(
                    self._t(
                        "可以重新运行研究，或在研究历史中查看中间结果。"
                    )
                )
                self.research_error_actions.grid()
                self.status_var.set(self._t("研究已取消"))
                self._refresh_history()
                run_id = str(data.get("run_id", ""))
                if run_id and self.storage.get_artifacts(run_id):
                    self._display_run(run_id)
                else:
                    self._set_report(
                        self._t("研究已取消。\n\n")
                        + "\n".join(self._research_activity_lines)
                    )
            elif event == "model_test":
                self.status_var.set(self._error_text(str(payload)))
                messagebox.showinfo(
                    self._t("模型连接测试"),
                    self._error_text(str(payload)),
                )
            elif event == "model_catalog":
                data = payload  # type: ignore[assignment]
                slot = str(data["slot"])
                compare = slot == "compare"
                if int(data["generation"]) != self._model_refresh_generation[slot]:
                    continue
                preset_var = (
                    self.compare_model_preset_var
                    if compare
                    else self.model_preset_var
                )
                base_url_var = (
                    self.compare_base_url_var if compare else self.base_url_var
                )
                current_preset = get_model_preset(preset_var.get())
                if (
                    current_preset.preset_id != data["preset_id"]
                    or base_url_var.get().strip() != data["base_url"]
                ):
                    continue
                status_var = (
                    self.compare_model_catalog_status_var
                    if compare
                    else self.model_catalog_status_var
                )
                models = tuple(data["models"])
                if models:
                    self._model_catalog_cache[
                        (str(data["preset_id"]), str(data["base_url"]))
                    ] = models
                    self._configure_model_choices(
                        compare=compare, preset=current_preset
                    )
                    status_var.set(
                        self._t(
                            "已合并 {count} 个在线模型；内置推荐项保持置顶。",
                            count=len(models),
                        )
                    )
                else:
                    message = self._error_text(str(data["error"]))
                    if current_preset.preset_id == "ollama":
                        message += self._t(
                            " 若尚未安装或启动 Ollama，请点击帮助。"
                        )
                    status_var.set(message)
            elif event == "error":
                data = payload  # type: ignore[assignment]
                if data.get("operation") == "research_complete":
                    self.research_running = False
                    self._research_cancel_event = None
                    self._update_start_state()
                    self.status_var.set(self._t("研究任务失败"))
                    self._show_research_error(str(data["message"]))
                    self._refresh_history()
                    messagebox.showerror(
                        self.research_feedback_title_var.get(),
                        self.research_feedback_detail_var.get(),
                    )
                else:
                    self.status_var.set(self._t("后台任务失败"))
                    safe_message = self._sanitize_research_error(
                        str(data["message"])
                    )
                    messagebox.showerror(
                        self._t("OpenThesis 后台任务失败"),
                        self._error_text(safe_message),
                    )
        self.root.after(100, self._drain_events)


def main() -> None:
    gui_smoke = os.environ.get("OPENTHESIS_GUI_SMOKE_TEST") == "1"
    diagnostic_path = Path(tempfile.gettempdir()) / "OpenThesis-startup.log"

    def diagnostic(message: str) -> None:
        if os.environ.get("OPENTHESIS_DIAGNOSTIC") == "1":
            with diagnostic_path.open("a", encoding="utf-8") as stream:
                stream.write(f"{message}\n")

    diagnostic("creating-tk-root")
    root = tk.Tk()
    if gui_smoke:
        root.tk.call("tk", "scaling", 1.75)
    diagnostic("tk-root-created")
    app = OpenThesisApp(root)
    if gui_smoke:
        root.geometry("980x680")
    diagnostic("app-initialized")
    root.update_idletasks()
    diagnostic(
        "window-title={title};state={state};mapped={mapped};viewable={viewable};"
        "size={width}x{height}".format(
            title=root.title(),
            state=root.state(),
            mapped=root.winfo_ismapped(),
            viewable=root.winfo_viewable(),
            width=root.winfo_width(),
            height=root.winfo_height(),
        )
    )
    smoke_result = {"viewable": False}
    if gui_smoke:
        def begin_gui_smoke_research() -> None:
            offline_preset = get_model_preset("none")
            app.model_preset_var.set(
                model_preset_label(offline_preset.preset_id, app.ui_language)
            )
            app.provider_var.set(offline_preset.protocol)
            app.model_var.set("")
            app.base_url_var.set("")
            app.api_key_var.set("")
            app.compare_models_var.set(False)
            app._select_demo_company()
            app._start_research()

        def finish_gui_smoke() -> None:
            # Flush geometry changes posted by the worker completion event
            # before evaluating visibility. This avoids a false failure when
            # Tk has updated data bindings but has not completed layout yet.
            root.update_idletasks()

            def settle_animation(
                seconds: float,
                until: object | None = None,
            ) -> None:
                deadline = time.monotonic() + seconds
                while time.monotonic() < deadline:
                    root.update()
                    if until is not None and until():  # type: ignore[operator]
                        break
                    time.sleep(0.01)

            start_visible = bool(
                app.run_button.winfo_viewable()
                and app.run_button.cget("text") == app._t("开始研究")
            )
            feedback_visible = bool(
                app.research_feedback_frame.winfo_viewable()
                and app.research_progress_bar.winfo_viewable()
            )
            research_completed = bool(
                not app.research_running
                and app.research_percent_var.get() == "100%"
            )
            app._set_report_focus(True)
            settle_animation(
                2.0,
                lambda: not app._report_focus_transitioning,
            )
            focus_transition_at_check = app._report_focus_transitioning
            focus_window_size = (
                (
                    app.report_focus_window.winfo_width(),
                    app.report_focus_window.winfo_height(),
                    bool(app.report_focus_window.winfo_viewable()),
                )
                if app.report_focus_window is not None
                else (0, 0, False)
            )
            immersive_accessible = bool(
                app.report_focus_mode
                and not app._report_focus_transitioning
                and app.report_focus_window is not None
                and app.report_focus_window.winfo_viewable()
                and app.report_focus_window.winfo_width() >= 970
                and app.report_focus_view is not None
                and app.sidebar.winfo_viewable()
                and app.header.winfo_viewable()
                and app.workflow_frame.winfo_viewable()
                and app.status_frame.winfo_viewable()
                and app._research_controls_are_visible()
            )
            app._set_report_zoom(1.2)
            root.update_idletasks()
            zoom_accessible = bool(
                app.report_zoom_label_var.get() == "120%"
                and abs(app.report_zoom - 1.2) < 0.001
            )
            app._set_report_focus(False)
            settle_animation(
                2.0,
                lambda: not app._report_focus_transitioning,
            )
            layout_restored = bool(
                not app.report_focus_mode
                and app.report_focus_window is None
                and app.sidebar.winfo_viewable()
                and app.header.winfo_viewable()
                and app.workflow_frame.winfo_viewable()
                and app.status_frame.winfo_viewable()
                and app._research_controls_are_visible()
            )
            app._toggle_dcf()
            app.research_controls_scroll.scroll_to_bottom()
            root.update_idletasks()
            dcf_accessible = bool(app.valuation_frame.winfo_ismapped())
            app.notebook.select(app.model_tab)
            app._toggle_comparison_model()
            app.model_settings_scroll.scroll_to_bottom()
            root.update_idletasks()
            model_bottom_accessible = bool(app.comparison_frame.winfo_ismapped())
            app.notebook.select(app.settings_tab)
            root.update_idletasks()
            settings_accessible = bool(
                app.ui_language_combo.winfo_viewable()
                and app.report_language_combo.winfo_viewable()
                and app.save_language_button.winfo_viewable()
            )
            rich_report_ready = bool(
                app.report_view is not None
                and '<table class="data-table">' in app.current_report_html
                and "supporting_evidence_ids" not in app.current_report_html
            )
            diagnostic(
                "gui-smoke="
                f"language:{app.ui_language};"
                f"start_visible:{start_visible};"
                f"start_mapped:{bool(app.run_button.winfo_ismapped())};"
                f"start_text:{app.run_button.cget('text')};"
                f"start_geometry:{app.run_button.winfo_x()},"
                f"{app.run_button.winfo_y()},"
                f"{app.run_button.winfo_width()}x{app.run_button.winfo_height()};"
                f"feedback_visible:{feedback_visible};"
                f"feedback_mapped:{bool(app.research_feedback_frame.winfo_ismapped())};"
                f"feedback_geometry:{app.research_feedback_frame.winfo_x()},"
                f"{app.research_feedback_frame.winfo_y()},"
                f"{app.research_feedback_frame.winfo_width()}x"
                f"{app.research_feedback_frame.winfo_height()};"
                f"research_completed:{research_completed};"
                f"immersive_accessible:{immersive_accessible};"
                f"focus_transitioning:{app._report_focus_transitioning};"
                f"focus_transition_at_check:{focus_transition_at_check};"
                f"focus_window_size:{focus_window_size};"
                f"focus_window_exists:{bool(app.report_focus_window)};"
                f"focus_mode:{app.report_focus_mode};"
                f"zoom_accessible:{zoom_accessible};"
                f"zoom_label:{app.report_zoom_label_var.get()};"
                f"zoom_value:{app.report_zoom};"
                f"layout_restored:{layout_restored};"
                f"dcf_accessible:{dcf_accessible};"
                f"model_bottom_accessible:{model_bottom_accessible};"
                f"settings_accessible:{settings_accessible};"
                f"rich_report_ready:{rich_report_ready};"
                f"status_visible:{bool(app.status_frame.winfo_viewable())};"
                f"size:{root.winfo_width()}x{root.winfo_height()}"
            )
            smoke_result["viewable"] = bool(
                root.winfo_exists()
                and root.winfo_ismapped()
                and root.winfo_viewable()
                and root.winfo_width() >= 980
                and root.winfo_height() >= 680
                and start_visible
                and feedback_visible
                and research_completed
                and immersive_accessible
                and zoom_accessible
                and layout_restored
                and dcf_accessible
                and model_bottom_accessible
                and settings_accessible
                and rich_report_ready
                and app.status_frame.winfo_viewable()
            )
            root.destroy()

        root.after(150, begin_gui_smoke_research)
        root.after(1500, finish_gui_smoke)
    root.mainloop()
    diagnostic("mainloop-ended")
    if gui_smoke and not smoke_result["viewable"]:
        raise RuntimeError(
            "Packaged GUI or its primary start action did not become viewable"
        )
