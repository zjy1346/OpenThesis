from __future__ import annotations

import json
import os
import queue
import re
import threading
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

from . import __version__
from .comparison import compare_research_runs
from .demo import DEMO_COMPANY, demo_facts
from .domain import Company, FinancialFact
from .filing_parser import build_filing_evidence
from .onboarding import (
    COMMON_COMPANY_LABELS,
    SEC_DEFAULT_PROFILE,
    SEC_PROFILE_LABELS,
    build_sec_user_agent,
    extract_sec_contact_email,
    get_common_company,
)
from .packs import (
    PackValidationError,
    ResearchPack,
    builtin_pack,
    install_pack,
    list_installed_packs,
)
from .providers import ModelConfig, ProviderError, create_provider
from .research import ResearchWorkflow
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


class OpenThesisApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(f"OpenThesis {__version__}")
        self.root.geometry("1260x820")
        self.root.minsize(980, 680)
        self.storage = Storage(default_data_dir())
        self.event_queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self.selected_company: Company | None = None
        self.current_run_id = ""
        self.company_results: list[Company] = []
        self.pack_by_label: dict[str, ResearchPack] = {}
        self.current_report_text = ""
        self.research_running = False
        self._report_link_tags: list[str] = []
        self._configure_style()
        self._build_ui()
        self._load_settings()
        self._refresh_packs()
        self._refresh_history()
        self._refresh_theses()
        self.root.after(100, self._drain_events)

    def _configure_style(self) -> None:
        style = ttk.Style()
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        style.configure("Title.TLabel", font=("Segoe UI", 20, "bold"))
        style.configure("Subtitle.TLabel", foreground="#4b5563")
        style.configure(
            "Accent.TButton",
            font=("Segoe UI", 12, "bold"),
            padding=(18, 10),
        )
        style.configure(
            "Workflow.TLabel",
            font=("Segoe UI", 10, "bold"),
            foreground="#075985",
        )

    def _build_ui(self) -> None:
        header = ttk.Frame(self.root, padding=(18, 14))
        header.pack(fill=X)
        ttk.Label(header, text="OpenThesis", style="Title.TLabel").pack(side=LEFT)
        ttk.Label(
            header,
            text="研究公司，而不是预测短期价格",
            style="Subtitle.TLabel",
        ).pack(side=LEFT, padx=(14, 0), pady=(8, 0))

        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill=BOTH, expand=True, padx=14, pady=(0, 8))

        self.research_tab = ttk.Frame(self.notebook, padding=12)
        self.history_tab = ttk.Frame(self.notebook, padding=12)
        self.model_tab = ttk.Frame(self.notebook, padding=12)
        self.packs_tab = ttk.Frame(self.notebook, padding=12)
        self.thesis_tab = ttk.Frame(self.notebook, padding=12)
        self.about_tab = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(self.research_tab, text="公司研究")
        self.notebook.add(self.history_tab, text="研究历史")
        self.notebook.add(self.model_tab, text="模型设置")
        self.notebook.add(self.packs_tab, text="研究模块")
        self.notebook.add(self.thesis_tab, text="投资逻辑")
        self.notebook.add(self.about_tab, text="关于")

        self._build_research_tab()
        self._build_history_tab()
        self._build_model_tab()
        self._build_packs_tab()
        self._build_thesis_tab()
        self._build_about_tab()

        status_frame = ttk.Frame(self.root, padding=(14, 3, 14, 9))
        status_frame.pack(fill=X)
        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(status_frame, textvariable=self.status_var).pack(side=LEFT)
        self.progress = ttk.Progressbar(
            status_frame, mode="determinate", maximum=100, length=280
        )
        self.progress.pack(side=RIGHT)

    def _build_research_tab(self) -> None:
        self.selected_company_var = tk.StringVar(value="尚未选择公司")
        self.start_hint_var = tk.StringVar(value="请先在下方选择一家公司。")

        workflow = ttk.LabelFrame(
            self.research_tab, text="研究流程", padding=(12, 8)
        )
        workflow.pack(fill=X, pady=(0, 10))
        workflow.columnconfigure(1, weight=1)
        ttk.Label(
            workflow,
            text="① 选择公司   →   ② 确认配置   →   ③ 开始研究",
            style="Workflow.TLabel",
        ).grid(row=0, column=0, columnspan=3, sticky=W, pady=(0, 6))
        ttk.Label(workflow, textvariable=self.selected_company_var).grid(
            row=1, column=0, columnspan=2, sticky=W
        )
        self.run_button = ttk.Button(
            workflow,
            text="开始研究",
            style="Accent.TButton",
            command=self._start_research,
        )
        self.run_button.grid(row=0, column=3, rowspan=2, sticky="e", padx=(16, 0))
        ttk.Label(
            workflow,
            textvariable=self.start_hint_var,
            style="Subtitle.TLabel",
        ).grid(row=2, column=0, columnspan=2, sticky=W, pady=(4, 0))
        ttk.Button(
            workflow,
            text="模型与 SEC 设置",
            command=lambda: self.notebook.select(self.model_tab),
        ).grid(row=2, column=3, sticky="e", padx=(16, 0), pady=(4, 0))

        outer = ttk.Panedwindow(self.research_tab, orient=HORIZONTAL)
        outer.pack(fill=BOTH, expand=True)
        controls = ttk.Frame(outer, padding=(0, 0, 12, 0), width=310)
        report = ttk.Frame(outer)
        outer.add(controls, weight=0)
        outer.add(report, weight=1)

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
        valuation_frame = ttk.LabelFrame(controls, text="可选：反向 DCF", padding=8)
        valuation_frame.pack(fill=X, pady=(10, 0))
        self.market_cap_var = tk.StringVar()
        self.discount_rate_var = tk.StringVar(value="10")
        self.terminal_growth_var = tk.StringVar(value="3")
        ttk.Label(valuation_frame, text="当前市值（十亿美元）").grid(
            row=0, column=0, sticky=W
        )
        ttk.Entry(valuation_frame, textvariable=self.market_cap_var, width=11).grid(
            row=0, column=1, sticky=W, padx=(6, 0)
        )
        ttk.Label(valuation_frame, text="折现率 %").grid(row=1, column=0, sticky=W)
        ttk.Entry(valuation_frame, textvariable=self.discount_rate_var, width=11).grid(
            row=1, column=1, sticky=W, padx=(6, 0)
        )
        ttk.Label(valuation_frame, text="永续增长率 %").grid(row=2, column=0, sticky=W)
        ttk.Entry(
            valuation_frame, textvariable=self.terminal_growth_var, width=11
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

        report_toolbar = ttk.Frame(report)
        report_toolbar.pack(fill=X)
        ttk.Label(
            report_toolbar, text="研究报告", font=("Segoe UI", 12, "bold")
        ).pack(side=LEFT)
        ttk.Button(
            report_toolbar, text="清空显示", command=lambda: self._set_report("")
        ).pack(side=RIGHT)
        text_frame = ttk.Frame(report)
        text_frame.pack(fill=BOTH, expand=True, pady=(8, 0))
        scrollbar = ttk.Scrollbar(text_frame, orient=VERTICAL)
        scrollbar.pack(side=RIGHT, fill=Y)
        self.report_text = tk.Text(
            text_frame,
            wrap="word",
            font=("Consolas", 10),
            padx=12,
            pady=12,
            yscrollcommand=scrollbar.set,
            background="#fbfbfb",
        )
        self.report_text.pack(side=LEFT, fill=BOTH, expand=True)
        scrollbar.configure(command=self.report_text.yview)
        self._set_report(
            "欢迎使用 OpenThesis。\n\n"
            "第一步：搜索或快捷选择公司；第二步：确认研究模块和模型设置；"
            "第三步：点击页面顶部始终可见的“开始研究”。\n\n"
            "可以选择“合成演示公司”离线验证完整流程。研究真实公司时，"
            "请在“模型与 SEC 设置”中填写你自己的 SEC 联系邮箱。"
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
        container = ttk.Frame(self.model_tab, width=760)
        container.pack(anchor=W, fill=BOTH, expand=True)
        ttk.Label(
            container, text="模型与数据源设置", font=("Segoe UI", 13, "bold")
        ).pack(anchor=W, pady=(0, 10))

        self.provider_var = tk.StringVar(value="none")
        self.model_var = tk.StringVar()
        self.base_url_var = tk.StringVar()
        self.api_key_var = tk.StringVar()
        self.sec_profile_var = tk.StringVar(value=SEC_DEFAULT_PROFILE)
        self.sec_email_var = tk.StringVar()
        self.sec_user_agent_var = tk.StringVar()

        model_frame = ttk.LabelFrame(
            container, text="AI 模型（可选）", padding=12
        )
        model_frame.pack(fill=X)
        fields = (
            ("模型提供方", "provider"),
            ("模型名称", "model"),
            ("接口地址", "base_url"),
            ("API Key（仅本次会话）", "api_key"),
        )
        for row, (label, field) in enumerate(fields):
            ttk.Label(model_frame, text=label).grid(
                row=row, column=0, sticky=W, padx=(0, 12), pady=6
            )
            if field == "provider":
                widget = ttk.Combobox(
                    model_frame,
                    textvariable=self.provider_var,
                    state="readonly",
                    values=("none", "ollama", "openai-compatible"),
                    width=48,
                )
                widget.bind("<<ComboboxSelected>>", self._provider_changed)
            else:
                variable = {
                    "model": self.model_var,
                    "base_url": self.base_url_var,
                    "api_key": self.api_key_var,
                }[field]
                widget = ttk.Entry(
                    model_frame,
                    textvariable=variable,
                    width=52,
                    show="*" if field == "api_key" else "",
                )
            widget.grid(row=row, column=1, sticky="ew", pady=6)
        model_frame.columnconfigure(1, weight=1)
        ttk.Label(
            model_frame,
            text=(
                "选择 none 时只运行本地确定性财务分析。API Key 仅保存在本次会话，"
                "不会写入数据库。"
            ),
            style="Subtitle.TLabel",
            wraplength=760,
        ).grid(row=4, column=0, columnspan=2, sticky=W, pady=(8, 0))

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
            values=SEC_PROFILE_LABELS,
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

        comparison = ttk.LabelFrame(container, text="可选：第二个对比模型", padding=10)
        comparison.pack(fill=X, pady=(12, 0))
        self.compare_provider_var = tk.StringVar(value="none")
        self.compare_model_var = tk.StringVar()
        self.compare_base_url_var = tk.StringVar()
        self.compare_api_key_var = tk.StringVar()
        compare_fields = (
            ("提供方", self.compare_provider_var, True, False),
            ("模型名称", self.compare_model_var, False, False),
            ("接口地址", self.compare_base_url_var, False, False),
            ("API Key（仅本次会话）", self.compare_api_key_var, False, True),
        )
        for row, (label, variable, is_combo, secret) in enumerate(compare_fields):
            ttk.Label(comparison, text=label).grid(
                row=row, column=0, sticky=W, padx=(0, 12), pady=4
            )
            if is_combo:
                widget = ttk.Combobox(
                    comparison,
                    textvariable=variable,
                    state="readonly",
                    values=("none", "ollama", "openai-compatible"),
                    width=43,
                )
            else:
                widget = ttk.Entry(
                    comparison,
                    textvariable=variable,
                    width=47,
                    show="*" if secret else "",
                )
            widget.grid(row=row, column=1, sticky="ew", pady=4)
        comparison.columnconfigure(1, weight=1)
        self.sec_email_var.trace_add("write", self._refresh_sec_preview)

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

    def _build_about_tab(self) -> None:
        text = (
            f"OpenThesis {__version__}\n\n"
            "面向个人长期投资者的开源、模型无关公司研究系统。\n\n"
            "原则：每个事实都需要证据；财务计算由确定性程序完成；"
            "预测使用情景、区间和失效条件；AI 不执行任何交易。\n\n"
            f"本地数据目录：{self.storage.data_dir}"
        )
        ttk.Label(
            self.about_tab,
            text=text,
            justify=LEFT,
            wraplength=780,
            font=("Segoe UI", 11),
        ).pack(anchor=W)

    def _load_settings(self) -> None:
        provider = self.storage.get_setting("provider", "none")
        self.provider_var.set(provider)
        self.model_var.set(self.storage.get_setting("model", ""))
        default_base = (
            "http://localhost:11434"
            if provider == "ollama"
            else "https://api.openai.com/v1"
        )
        self.base_url_var.set(self.storage.get_setting("base_url", default_base))
        self.sec_profile_var.set(
            self.storage.get_setting("sec_contact_profile", SEC_DEFAULT_PROFILE)
        )
        saved_email = self.storage.get_setting("sec_contact_email", "")
        if not saved_email:
            saved_email = extract_sec_contact_email(
                self.storage.get_setting("sec_user_agent", "")
            )
        self.sec_email_var.set(saved_email)
        self._refresh_sec_preview()
        self.compare_provider_var.set(
            self.storage.get_setting("compare_provider", "none")
        )
        self.compare_model_var.set(self.storage.get_setting("compare_model", ""))
        self.compare_base_url_var.set(
            self.storage.get_setting("compare_base_url", "")
        )

    def _save_settings(self) -> bool:
        email = self.sec_email_var.get().strip()
        user_agent = ""
        if email:
            try:
                user_agent = self._sec_user_agent_value()
            except ValueError as exc:
                messagebox.showerror("SEC 联系邮箱无效", str(exc))
                self.notebook.select(self.model_tab)
                return False
        self.storage.set_setting("provider", self.provider_var.get())
        self.storage.set_setting("model", self.model_var.get().strip())
        self.storage.set_setting("base_url", self.base_url_var.get().strip())
        self.storage.set_setting("sec_contact_profile", self.sec_profile_var.get())
        self.storage.set_setting("sec_contact_email", email)
        self.storage.set_setting("sec_user_agent", user_agent)
        self.storage.set_setting("compare_provider", self.compare_provider_var.get())
        self.storage.set_setting(
            "compare_model", self.compare_model_var.get().strip()
        )
        self.storage.set_setting(
            "compare_base_url", self.compare_base_url_var.get().strip()
        )
        self.status_var.set("设置已保存；API Key 未持久化")
        self._refresh_sec_preview()
        return True

    def _refresh_sec_preview(self, *_args: object) -> None:
        email = self.sec_email_var.get().strip()
        if not email:
            self.sec_user_agent_var.set("填写邮箱后自动生成，无需申请 SEC API Key")
            return
        try:
            self.sec_user_agent_var.set(self._sec_user_agent_value())
        except ValueError:
            self.sec_user_agent_var.set("邮箱格式尚未完成")

    def _sec_user_agent_value(self) -> str:
        return build_sec_user_agent(
            self.sec_profile_var.get(),
            self.sec_email_var.get(),
        )

    def _show_sec_help(self) -> None:
        help_window = tk.Toplevel(self.root)
        help_window.title("SEC EDGAR 使用帮助")
        help_window.transient(self.root)
        help_window.geometry("680x480")
        help_window.minsize(600, 420)

        ttk.Label(
            help_window,
            text="SEC 是什么，OpenThesis 如何获取财报？",
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
        help_text.insert(
            "1.0",
            (
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
                "示例：OpenThesis/0.2.0 (Personal Investor; "
                "contact: your-name@example.com)"
            ),
        )
        help_text.configure(state="disabled")
        help_buttons = ttk.Frame(help_window, padding=18)
        help_buttons.pack(fill=X)
        ttk.Button(
            help_buttons,
            text="打开 SEC 官方开发者说明",
            command=lambda: webbrowser.open_new_tab(
                "https://www.sec.gov/search-filings/edgar-application-programming-interfaces"
            ),
        ).pack(side=LEFT)
        ttk.Button(
            help_buttons, text="关闭", command=help_window.destroy
        ).pack(side=RIGHT)

    def _provider_changed(self, _event: object = None) -> None:
        if self.provider_var.get() == "ollama" and not self.base_url_var.get().strip():
            self.base_url_var.set("http://localhost:11434")
        elif (
            self.provider_var.get() == "openai-compatible"
            and not self.base_url_var.get().strip()
        ):
            self.base_url_var.set("https://api.openai.com/v1")

    def _model_config(self) -> ModelConfig:
        return ModelConfig(
            provider=self.provider_var.get(),
            model=self.model_var.get().strip(),
            base_url=self.base_url_var.get().strip(),
            api_key=self.api_key_var.get(),
        )

    def _comparison_model_config(self) -> ModelConfig:
        return ModelConfig(
            provider=self.compare_provider_var.get(),
            model=self.compare_model_var.get().strip(),
            base_url=self.compare_base_url_var.get().strip(),
            api_key=self.compare_api_key_var.get(),
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
            raise ValueError("反向 DCF 输入必须是数字") from exc
        if market_cap <= 0 or discount <= terminal:
            raise ValueError("市值必须为正数，且折现率必须高于永续增长率")
        return {
            "market_cap": market_cap,
            "discount_rate": discount,
            "terminal_growth": terminal,
        }

    def _search_company(self) -> None:
        query_text = self.company_query_var.get().strip()
        if not query_text:
            messagebox.showinfo("搜索公司", "请输入股票代码或公司名称。")
            return
        try:
            user_agent_text = self._sec_user_agent_value()
        except ValueError as exc:
            messagebox.showinfo(
                "需要 SEC 联系邮箱",
                f"{exc}\n\n请在“模型与数据源设置”中填写后保存。",
            )
            self.notebook.select(self.model_tab)
            return
        self._run_background(
            lambda: SecClient(
                user_agent_text, self.storage.data_dir / "sec-cache"
            ).search_companies(query_text),
            "company_results",
            "正在查询 SEC 公司列表…",
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
            messagebox.showinfo("选择常用公司", str(exc))
            return
        self.selected_company = company
        self.selected_company_var.set(f"{company.ticker} · {company.name}")
        self._set_report(
            f"已选择常用公司：{company.ticker} · {company.name}。\n\n"
            "请确认研究配置，然后点击页面顶部的“开始研究”。"
        )
        self._update_start_state()

    def _select_demo_company(self) -> None:
        self.selected_company = DEMO_COMPANY
        self.selected_company_var.set(
            f"{DEMO_COMPANY.ticker} · {DEMO_COMPANY.name}"
        )
        self._set_report(
            "已选择合成演示公司。所有数据均为虚构，只用于验证软件功能。"
        )
        self._update_start_state()

    def _update_start_state(self) -> None:
        if self.research_running:
            self.run_button.configure(state="disabled", text="研究进行中…")
            self.start_hint_var.set("正在运行多 Agent 研究流程，请查看底部进度。")
        elif self.selected_company is None:
            self.run_button.configure(state="disabled", text="开始研究")
            self.start_hint_var.set("请先在下方选择一家公司。")
        else:
            self.run_button.configure(state="normal", text="开始研究")
            self.start_hint_var.set("公司已选择；确认配置后即可开始。")

    def _start_research(self) -> None:
        company = self.selected_company
        if company is None:
            messagebox.showinfo("开始研究", "请先选择公司。")
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
                    "需要 SEC 联系邮箱",
                    f"{exc}\n\n真实公司研究需要访问 SEC，请先完成 SEC 设置。",
                )
                self.notebook.select(self.model_tab)
                return
        download_filings = self.download_filings_var.get()
        config = self._model_config()
        compare_enabled = self.compare_models_var.get()
        compare_config = self._comparison_model_config()
        selected_pack = self._selected_pack()
        try:
            valuation_inputs = self._valuation_inputs()
        except ValueError as exc:
            messagebox.showerror("反向 DCF 输入错误", str(exc))
            return
        if compare_enabled and (not config.enabled or not compare_config.enabled):
            messagebox.showerror(
                "双模型配置不完整",
                "启用模型比较时，主模型和第二模型都必须配置提供方、模型名称和接口地址。",
            )
            self.notebook.select(self.model_tab)
            return
        self.research_running = True
        self._update_start_state()
        self.progress["value"] = 2
        self._set_report(f"正在准备 {company.ticker} 的研究数据…")

        def task() -> object:
            filing_evidence: list[dict[str, object]] = []
            if company.cik == DEMO_COMPANY.cik:
                self.storage.save_company(company)
                facts = demo_facts()
                self.storage.save_facts([FinancialFact(**item) for item in facts])
            else:
                client = SecClient(user_agent, self.storage.data_dir / "sec-cache")
                self.storage.save_company(company)
                filings = client.list_annual_filings(company, limit=5)
                if download_filings:
                    target = self.storage.filings_dir / company.cik
                    filings = [client.download_filing(item, target) for item in filings]
                    filing_evidence = build_filing_evidence(filings)
                self.storage.save_filings(filings)
                normalized = client.get_company_facts(company)
                self.storage.save_facts(normalized)
                facts = [item.to_dict() for item in normalized]

            provider = create_provider(config)
            workflow = ResearchWorkflow(
                self.storage, selected_pack, provider, config
            )
            primary = workflow.run(
                company,
                facts,
                filing_evidence=filing_evidence,  # type: ignore[arg-type]
                valuation_inputs=valuation_inputs,
                progress=lambda message, percent: self.event_queue.put(
                    (
                        "progress",
                        (
                            f"主模型：{message}" if compare_enabled else message,
                            percent // 2 if compare_enabled else percent,
                        ),
                    )
                ),
            )
            if not compare_enabled:
                return primary

            secondary_provider = create_provider(compare_config)
            secondary_workflow = ResearchWorkflow(
                self.storage, selected_pack, secondary_provider, compare_config
            )
            secondary = secondary_workflow.run(
                company,
                facts,
                filing_evidence=filing_evidence,  # type: ignore[arg-type]
                valuation_inputs=valuation_inputs,
                progress=lambda message, percent: self.event_queue.put(
                    (
                        "progress",
                        (f"对比模型：{message}", 50 + percent // 2),
                    )
                ),
            )
            compare_research_runs(self.storage, primary, secondary)
            self.event_queue.put(("progress", ("双模型分歧比较完成", 100)))
            return primary

        self._run_background(task, "research_complete", "研究任务正在运行…")

    def _test_model(self) -> None:
        config = self._model_config()
        if not config.enabled:
            messagebox.showinfo("测试模型", "当前选择 none，不会调用语言模型。")
            return

        def task() -> str:
            provider = create_provider(config)
            if provider is None:
                return "未配置模型"
            return provider.test_connection()

        self._run_background(task, "model_test", "正在测试模型连接…")

    def _refresh_packs(self) -> None:
        self.pack_by_label.clear()
        self.packs_list.delete(0, END)
        for pack in list_installed_packs(self.storage.packs_dir):
            label = f"{pack.name} · {pack.version}"
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
            title="导入 OpenThesis 研究模块",
            filetypes=[("OpenThesis Research Pack", "*.othesis")],
        )
        if not path:
            return
        try:
            pack = install_pack(Path(path), self.storage.packs_dir)
        except (PackValidationError, OSError, ValueError) as exc:
            messagebox.showerror("研究模块验证失败", str(exc))
            return
        self._refresh_packs()
        messagebox.showinfo(
            "研究模块已安装",
            f"{pack.name}\n版本：{pack.version}\n哈希：{pack.content_hash[:16]}",
        )

    def _refresh_history(self) -> None:
        for item in self.history_tree.get_children():
            self.history_tree.delete(item)
        for row in self.storage.list_runs():
            self.history_tree.insert(
                "",
                END,
                iid=row["run_id"],
                values=(row["ticker"], row["name"], row["status"], row["started_at"]),
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
            messagebox.showinfo("投资逻辑", "请先选择一个已有版本。")
            return
        try:
            content = json.loads(self.thesis_editor.get("1.0", END))
        except json.JSONDecodeError as exc:
            messagebox.showerror(
                "JSON 格式错误", f"第 {exc.lineno} 行，第 {exc.colno} 列：{exc.msg}"
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
        messagebox.showinfo("投资逻辑", f"已保存为 v{saved['version']}")

    def _open_history(self, _event: object = None) -> None:
        selection = self.history_tree.selection()
        if not selection:
            return
        run_id = selection[0]
        self._display_run(run_id)
        self.notebook.select(self.research_tab)

    def _display_run(self, run_id: str) -> None:
        artifacts = self.storage.get_artifacts(run_id)
        self.current_run_id = run_id
        self._set_report(render_research_run(run_id, artifacts))

    def _export_report(self) -> None:
        if not self.current_report_text.strip():
            messagebox.showinfo("导出报告", "当前没有可导出的内容。")
            return
        path = filedialog.asksaveasfilename(
            title="导出 OpenThesis 报告",
            defaultextension=".txt",
            filetypes=[("Text", "*.txt"), ("Markdown", "*.md"), ("JSON", "*.json")],
        )
        if path:
            Path(path).write_text(self.current_report_text, encoding="utf-8")
            self.status_var.set(f"报告已导出：{path}")

    def _set_report(self, content: str) -> None:
        self.current_report_text = content
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
            except Exception as exc:
                self.event_queue.put(
                    (
                        "error",
                        {
                            "message": str(exc),
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
                self.status_var.set(f"找到 {len(self.company_results)} 家公司")
            elif event == "progress":
                message, percent = payload  # type: ignore[misc]
                self.status_var.set(str(message))
                self.progress["value"] = int(percent)
            elif event == "research_complete":
                run = payload
                self.research_running = False
                self._update_start_state()
                self.progress["value"] = 100
                self._refresh_history()
                self._refresh_theses()
                self._display_run(run.run_id)  # type: ignore[attr-defined]
                self.status_var.set(f"研究完成：{run.status.value}")  # type: ignore[attr-defined]
            elif event == "model_test":
                self.status_var.set(str(payload))
                messagebox.showinfo("模型连接测试", str(payload))
            elif event == "error":
                self.research_running = False
                self._update_start_state()
                self.status_var.set("任务失败")
                data = payload  # type: ignore[assignment]
                self._set_report(
                    f"任务失败\n\n{data['message']}\n\n开发诊断信息：\n{data['traceback']}"
                )
                messagebox.showerror("OpenThesis 任务失败", data["message"])
        self.root.after(100, self._drain_events)


def main() -> None:
    diagnostic_path = Path(tempfile.gettempdir()) / "OpenThesis-startup.log"

    def diagnostic(message: str) -> None:
        if os.environ.get("OPENTHESIS_DIAGNOSTIC") == "1":
            with diagnostic_path.open("a", encoding="utf-8") as stream:
                stream.write(f"{message}\n")

    diagnostic("creating-tk-root")
    root = tk.Tk()
    diagnostic("tk-root-created")
    app = OpenThesisApp(root)
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
    gui_smoke = os.environ.get("OPENTHESIS_GUI_SMOKE_TEST") == "1"
    smoke_result = {"viewable": False}
    if gui_smoke:
        def finish_gui_smoke() -> None:
            smoke_result["viewable"] = bool(
                root.winfo_exists()
                and root.winfo_ismapped()
                and root.winfo_viewable()
                and root.winfo_width() >= 980
                and root.winfo_height() >= 680
                and app.run_button.winfo_viewable()
                and app.run_button.cget("text") == "开始研究"
            )
            root.destroy()

        root.after(750, finish_gui_smoke)
    root.mainloop()
    diagnostic("mainloop-ended")
    if gui_smoke and not smoke_result["viewable"]:
        raise RuntimeError(
            "Packaged GUI or its primary start action did not become viewable"
        )
