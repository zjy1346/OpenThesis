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
        style.configure("Accent.TButton", font=("Segoe UI", 10, "bold"))

    def _build_ui(self) -> None:
        header = ttk.Frame(self.root, padding=(18, 14))
        header.pack(fill=X)
        ttk.Label(header, text="OpenThesis", style="Title.TLabel").pack(side=LEFT)
        ttk.Label(
            header,
            text="ç ”ç©¶å…¬å¸ï¼Œè€Œä¸æ˜¯é¢„æµ‹çŸ­æœŸä»·æ ¼",
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
        self.notebook.add(self.research_tab, text="å…¬å¸ç ”ç©¶")
        self.notebook.add(self.history_tab, text="ç ”ç©¶å†å²")
        self.notebook.add(self.model_tab, text="æ¨¡å‹è®¾ç½®")
        self.notebook.add(self.packs_tab, text="ç ”ç©¶æ¨¡å—")
        self.notebook.add(self.thesis_tab, text="æŠ•èµ„é€»è¾‘")
        self.notebook.add(self.about_tab, text="å…³äº")

        self._build_research_tab()
        self._build_history_tab()
        self._build_model_tab()
        self._build_packs_tab()
        self._build_thesis_tab()
        self._build_about_tab()

        status_frame = ttk.Frame(self.root, padding=(14, 3, 14, 9))
        status_frame.pack(fill=X)
        self.status_var = tk.StringVar(value="å°±ç»ª")
        ttk.Label(status_frame, textvariable=self.status_var).pack(side=LEFT)
        self.progress = ttk.Progressbar(
            status_frame, mode="determinate", maximum=100, length=280
        )
        self.progress.pack(side=RIGHT)

    def _build_research_tab(self) -> None:
        outer = ttk.Panedwindow(self.research_tab, orient=HORIZONTAL)
        outer.pack(fill=BOTH, expand=True)
        controls = ttk.Frame(outer, padding=(0, 0, 12, 0), width=310)
        report = ttk.Frame(outer)
        outer.add(controls, weight=0)
        outer.add(report, weight=1)

        ttk.Label(controls, text="1. é€‰æ‹©å…¬å¸", font=("Segoe UI", 12, "bold")).pack(
            anchor=W, pady=(0, 8)
        )
        search_row = ttk.Frame(controls)
        search_row.pack(fill=X)
        self.company_query_var = tk.StringVar()
        ttk.Entry(search_row, textvariable=self.company_query_var).pack(
            side=LEFT, fill=X, expand=True
        )
        ttk.Button(search_row, text="æœç´¢", command=self._search_company).pack(
            side=RIGHT, padx=(6, 0)
        )
        self.company_list = tk.Listbox(controls, height=8, exportselection=False)
        self.company_list.pack(fill=X, pady=(7, 5))
        self.company_list.bind("<<ListboxSelect>>", self._select_company)
        ttk.Button(
            controls, text="ä½¿ç”¨åˆæˆæ¼”ç¤ºå…¬å¸", command=self._select_demo_company
        ).pack(fill=X)

        ttk.Separator(controls).pack(fill=X, pady=14)
        ttk.Label(controls, text="2. ç ”ç©¶é…ç½®", font=("Segoe UI", 12, "bold")).pack(
            anchor=W, pady=(0, 8)
        )
        ttk.Label(controls, text="ç ”ç©¶æ¨¡å—").pack(anchor=W)
        self.pack_var = tk.StringVar()
        self.pack_combo = ttk.Combobox(
            controls, textvariable=self.pack_var, state="readonly"
        )
        self.pack_combo.pack(fill=X, pady=(3, 9))

        self.download_filings_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            controls,
            text="ä¸‹è½½æœ€è¿‘äº”ä»½ 10-K åŸæ–‡",
            variable=self.download_filings_var,
        ).pack(anchor=W)
        ttk.Label(
            controls,
            text="æœªé…ç½®æ¨¡å‹æ—¶ä»ä¼šç”Ÿæˆç¡®å®šæ€§è´¢åŠ¡æŠ¥å‘Šã€‚",
            style="Subtitle.TLabel",
            wraplength=280,
        ).pack(anchor=W, pady=(8, 0))
        valuation_frame = ttk.LabelFrame(controls, text="å¯é€‰ï¼šåå‘ DCF", padding=8)
        valuation_frame.pack(fill=X, pady=(10, 0))
        self.market_cap_var = tk.StringVar()
        self.discount_rate_var = tk.StringVar(value="10")
        self.terminal_growth_var = tk.StringVar(value="3")
        ttk.Label(valuation_frame, text="å½“å‰å¸‚å€¼ï¼ˆåäº¿ç¾å…ƒï¼‰").grid(
            row=0, column=0, sticky=W
        )
        ttk.Entry(valuation_frame, textvariable=self.market_cap_var, width=11).grid(
            row=0, column=1, sticky=W, padx=(6, 0)
        )
        ttk.Label(valuation_frame, text="æŠ˜ç°ç‡ %").grid(row=1, column=0, sticky=W)
        ttk.Entry(valuation_frame, textvariable=self.discount_rate_var, width=11).grid(
            row=1, column=1, sticky=W, padx=(6, 0)
        )
        ttk.Label(valuation_frame, text="æ°¸ç»­å¢é•¿ç‡ %").grid(row=2, column=0, sticky=W)
        ttk.Entry(
            valuation_frame, textvariable=self.terminal_growth_var, width=11
        ).grid(row=2, column=1, sticky=W, padx=(6, 0))
        self.compare_models_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            controls,
            text="è¿è¡Œç¬¬äºŒæ¨¡å‹å¹¶æ¯”è¾ƒåˆ†æ­§",
            variable=self.compare_models_var,
        ).pack(anchor=W, pady=(9, 0))

        ttk.Separator(controls).pack(fill=X, pady=14)
        ttk.Label(controls, text="3. å¼€å§‹ç ”ç©¶", font=("Segoe UI", 12, "bold")).pack(
            anchor=W, pady=(0, 8)
        )
        self.selected_company_var = tk.StringVar(value="å°šæœªé€‰æ‹©å…¬å¸")
        ttk.Label(
            controls, textvariable=self.selected_company_var, wraplength=280
        ).pack(anchor=W, pady=(0, 8))
        self.run_button = ttk.Button(
            controls,
            text="è¿è¡Œå®Œæ•´é•¿æœŸç ”ç©¶",
            style="Accent.TButton",
            command=self._start_research,
        )
        self.run_button.pack(fill=X)
        ttk.Button(controls, text="å¯¼å‡ºå½“å‰æŠ¥å‘Š", command=self._export_report).pack(
            fill=X, pady=(7, 0)
        )

        report_toolbar = ttk.Frame(report)
        report_toolbar.pack(fill=X)
        ttk.Label(
            report_toolbar, text="ç ”ç©¶æŠ¥å‘Š", font=("Segoe UI", 12, "bold")
        ).pack(side=LEFT)
        ttk.Button(
            report_toolbar, text="æ¸…ç©ºæ˜¾ç¤º", command=lambda: self._set_report("")
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
            "æ¬¢è¿ä½¿ç”¨ OpenThesisã€‚\n\n"
            "å¯ä»¥ç›´æ¥é€‰æ‹©â€œåˆæˆæ¼”ç¤ºå…¬å¸â€éªŒè¯æœ¬åœ°å·¥ä½œæµï¼Œ"
            "ä¹Ÿå¯ä»¥åœ¨æ¨¡å‹è®¾ç½®ä¸­å¡«å†™ SEC è”ç³»é‚®ç®±åæœç´¢çœŸå®ç¾è‚¡å…¬å¸ã€yë{h‘éì¶»§q«^t\˜Ù[ËÈˆYˆÛÛ\\™WÙ[˜X›Y[ÙH\˜Ù[ˆ
Kˆ
Bˆ
Kˆ
BˆYˆ›İÛÛ\\™WÙ[˜X›Y‚ˆ™]\›ˆš[X\B‚ˆÙXÛÛ™\WÜ›İšY\ˆHÜ™X]WÜ›İšY\ŠÛÛ\\™WØÛÛ™šYÊBˆÙXÛÛ™\WİÛÜšÙ›İÈH™\ÙX\˜ÚÛÜšÙ›İÊˆÙ[‹œİÜ˜YÙKÙ[XİYÜXÚËÙXÛÛ™\WÜ›İšY\‹ÛÛ\\™WØÛÛ™šYÂˆ
BˆÙXÛÛ™\HHÙXÛÛ™\WİÛÜšÙ›İËœ[ŠˆÛÛ\[Kˆ˜XİËˆš[[™×Ù]šY[˜ÙOYš[[™×Ù]šY[˜ÙKÈ\NˆYÛ›Ü™VØ\™Ë]\WBˆ˜[X][Û—Ú[œ]Ï]˜[X][Û—Ú[œ]Ëˆ›ÙÜ™\ÜÏ[[X™HY\ÜØYÙK\˜Ù[ˆÙ[‹™]™[Ü]Y]YKœ]
ˆ
ˆœ›ÙÜ™\ÜÈ‹ˆ
ˆ¹kîy«å9ª(yg¢ûï&ÛY\ÜØYÙ_H‹L
È\˜Ù[ËÈŠKˆ
Bˆ
Kˆ
BˆÛÛ\\™WÜ™\ÙX\˜ÚÜ[œÊÙ[‹œİÜ˜YÙKš[X\KÙXÛÛ™\JBˆÙ[‹™]™[Ü]Y]YKœ]

œ›ÙÜ™\ÜÈ‹
¹cã9ª(yg¢ùb!¹«iù«å:/ ùk£9¢$‹L
JJBˆ™]\›ˆš[X\B‚ˆÙ[‹—Ü[—Ø˜XÚÙÜ›İ[™
\ÚËœ™\ÙX\˜ÚØÛÛ\]H‹¹è%9êm¹.îùb¨y«hùg*:/ä:(c8 )ˆŠB‚ˆYˆİ\İÛ[Ù[
Ù[ŠHOˆ›Û™N‚ˆÛÛ™šYÈHÙ[‹—Û[Ù[ØÛÛ™šYÊ
BˆYˆ›İÛÛ™šYË™[˜X›Y‚ˆY\ÜØYÙX›ŞœÚİÚ[™›Ê¹­bú+åyª(yg¢È‹¹odùbcz`"y¢êH›Û™{ï#9.#y/&º, ùå*:+ëz* 9ª(yg¢øà ˆŠBˆ™]\›‚‚ˆYˆ\ÚÊ
HOˆİ‚ˆ›İšY\ˆHÜ™X]WÜ›İšY\ŠÛÛ™šYÊBˆYˆ›İšY\ˆ\È›Û™N‚ˆ™]\›ˆ¹§*ºacyïk¹ª(yg¢È‚ˆ™]\›ˆ›İšY\‹\İØÛÛ›™Xİ[ÛŠ
B‚ˆÙ[‹—Ü[—Ø˜XÚÙÜ›İ[™
\ÚË›[Ù[İ\İ‹¹«hùg*9­bú+åyª(yg¢ú/ç¹£©x )ˆŠB‚ˆYˆÜ™Yœ™\ÚÜXÚÜÊÙ[ŠHOˆ›Û™N‚ˆÙ[‹œXÚ×ØWÛX™[˜ÛX\Š
BˆÙ[‹œXÚÜ×Û\İ™[]JS‘
Bˆ›ÜˆXÚÈ[ˆ\İÚ[œİ[YÜXÚÜÊÙ[‹œİÜ˜YÙKœXÚÜ×Ù\ŠN‚ˆX™[HˆÜXÚË›˜[Y_H0­ÈÜXÚË™\œÚ[ÛŸH‚ˆÙ[‹œXÚ×ØWÛX™[ÛX™[HHXÚÂˆÙ[‹œXÚÜ×Û\İš[œÙ\
ˆS‘ˆÛX™[HŞÜXÚËœXÚ×ÚYWHÜXÚË˜ÛÛ[Ú\ÚÎŒL_H‚ˆ
BˆX™[ÈH\İ
Ù[‹œXÚ×ØWÛX™[
BˆÙ[‹œXÚ×ØÛÛX›Ë˜ÛÛ™šYİ\™J˜[Y\Ï[X™[ÊBˆYˆX™[È[™Ù[‹œXÚ×İ˜\‹™Ù]

H›İ[ˆX™[Î‚ˆÙ[‹œXÚ×İ˜\‹œÙ]
X™[ÖÌJB‚ˆYˆÚ[\ÜÜXÚÊÙ[ŠHOˆ›Û™N‚ˆ]Hš[YX[ÙË˜\ÚÛÜ[™š[[˜[YJˆ]OH¹kï9aiHÜ[•\Ú\È9è%9êm¹ª(ygeÈ‹ˆš[]\\ÏVÊ“Ü[•\Ú\È™\ÙX\˜ÚXÚÈ‹Š‹›İ\Ú\ÈŠWKˆ
BˆYˆ›İ]‚ˆ™]\›‚ˆN‚ˆXÚÈH[œİ[ÜXÚÊ]
]
KÙ[‹œİÜ˜YÙKœXÚÜ×Ù\ŠBˆ^Ù\
XÚÕ˜[Y][Û‘\œ›Ü‹ÔÑ\œ›Ü‹˜[YQ\œ›ÜŠH\È^Î‚ˆY\ÜØYÙX›ŞœÚİÙ\œ›ÜŠ¹è%9êm¹ª(ygeúj£:+àyi,z-)H‹İŠ^ÊJBˆ™]\›‚ˆÙ[‹—Ü™Yœ™\ÚÜXÚÜÊ
BˆY\ÜØYÙX›ŞœÚİÚ[™›Êˆ¹è%9êm¹ª(ygeùmì¹k¢z(áH‹ˆˆÜXÚË›˜[Y_W¹âb9§+;ï&ÜXÚË™\œÚ[ÛŸW¹dâ9n#;ï&ÜXÚË˜ÛÛ[Ú\ÚÎŒM—_H‹ˆ
B‚ˆYˆÜ™Yœ™\ÚÚ\İÜJÙ[ŠHOˆ›Û™N‚ˆ›Üˆ][H[ˆÙ[‹š\İÜWİ™YK™Ù]ØÚ[™[Š
N‚ˆÙ[‹š\İÜWİ™YK™[]J][JBˆ›Üˆ›İÈ[ˆÙ[‹œİÜ˜YÙK›\İÜ[œÊ
N‚ˆÙ[‹š\İÜWİ™YKš[œÙ\
ˆˆ‹ˆS‘ˆZY\›İÖÈœ[—ÚY—Kˆ˜[Y\ÏJ›İÖÈXÚÙ\ˆ—K›İÖÈ›˜[YH—K›İÖÈœİ]\È—K›İÖÈœİ\YØ]—JKˆ
B‚ˆYˆÜ™Yœ™\Úİ\Ù\ÊÙ[ŠHOˆ›Û™N‚ˆYˆ›İ\Ø]ŠÙ[‹\Ú\×İ™YHŠN‚ˆ™]\›‚ˆ›Üˆ][H[ˆÙ[‹\Ú\×İ™YK™Ù]ØÚ[™[Š
N‚ˆÙ[‹\Ú\×İ™YK™[]J][JBˆ›Üˆ›İÈ[ˆÙ[‹œİÜ˜YÙK›\İİ\Ú\×İ™\œÚ[ÛœÊ
N‚ˆÙ[‹\Ú\×İ™YKš[œÙ\
ˆˆ‹ˆS‘ˆZY\›İÖÈ\Ú\×İ™\œÚ[Û—ÚY—Kˆ˜[Y\ÏJˆ›İÖÈXÚÙ\ˆ—Kˆ›İÖÈ™\œÚ[Ûˆ—Kˆ›İÖÈ˜Ü™X]YØH—Kˆ›İÖÈ˜Ü™X]YØ]—Kˆ
Kˆ
B‚ˆYˆÛÜ[—İ\Ú\ÊÙ[‹Ù]™[ˆØš™XİH›Û™JHOˆ›Û™N‚ˆÙ[Xİ[ÛˆHÙ[‹\Ú\×İ™YKœÙ[Xİ[ÛŠ
BˆYˆ›İÙ[Xİ[Û‚ˆ™]\›‚ˆ\Ú\ÈHÙ[‹œİÜ˜YÙK™Ù]İ\Ú\×İ™\œÚ[ÛŠÙ[Xİ[Û–ÌJBˆYˆ\Ú\È\È›Û™N‚ˆ™]\›‚ˆÙ[‹™Y][™×İ\Ú\×ØÚZÈH\Ú\ÖÈ˜ÛÛ\[WØÚZÈ—BˆÙ[‹\Ú\×ÙY]Ü‹™[]JŒKŒ‹S‘
BˆÙ[‹\Ú\×ÙY]Ü‹š[œÙ\
ŒKŒ‹œÛÛ—Ü™]J\Ú\ÖÈ˜ÛÛ[—JJB‚ˆYˆÜØ]™Wİ\Ú\×ÙY]
Ù[ŠHOˆ›Û™N‚ˆYˆ›İÙ[‹™Y][™×İ\Ú\×ØÚZÎ‚ˆY\ÜØYÙX›ŞœÚİÚ[™›Ê¹¢¥z-a:`.ú/¤H‹º+íùab:`"y¢êy. 9.*¹mì¹§"yâb9§+8à ˆŠBˆ™]\›‚ˆN‚ˆÛÛ[HœÛÛ‹›ØYÊÙ[‹\Ú\×ÙY]Ü‹™Ù]
ŒKŒ‹S‘
JBˆ^Ù\œÛÛ‹’”ÓÓ‘XÛÙQ\œ›Üˆ\È^Î‚ˆY\ÜØYÙX›ŞœÚİÙ\œ›ÜŠˆ’”ÓÓˆ9¨/9o#úe&z+ëÈ‹ˆ¹ë+Ù^Ë›[™[›ßH:(c;ï#9ë+Ù^Ë˜ÛÛ›ßH9b%ûï&Ù^Ë›\ÙßH‚ˆ
Bˆ™]\›‚ˆœ›ÛH™ÛXZ[ˆ[\Ü]×Û›İ×Ú\ÛÂ‚ˆØ]™YHÙ[‹œİÜ˜YÙKœØ]™Wİ\Ú\×İ™\œÚ[ÛŠˆÙ[‹™Y][™×İ\Ú\×ØÚZËˆÛÛ[ˆÜ™X]YØOH\Ù\ˆ‹ˆÜ™X]YØ]]]×Û›İ×Ú\ÛÊ
Kˆ
BˆÙ[‹—Ü™Yœ™\Úİ\Ù\Ê
BˆY\ÜØYÙX›ŞœÚİÚ[™›Ê¹¢¥z-a:`.ú/¤H‹ˆ¹mì¹/çykf9..ˆÜØ]™YÉİ™\œÚ[Û‰×_HŠB‚ˆYˆÛÜ[—Ú\İÜJÙ[‹Ù]™[ˆØš™XİH›Û™JHOˆ›Û™N‚ˆÙ[Xİ[ÛˆHÙ[‹š\İÜWİ™YKœÙ[Xİ[ÛŠ
BˆYˆ›İÙ[Xİ[Û‚ˆ™]\›‚ˆ[—ÚYHÙ[Xİ[Û–ÌBˆÙ[‹—Ù\Ü^WÜ[Š[—ÚY
BˆÙ[‹››İX›ÛÚËœÙ[Xİ
Ù[‹œ™\ÙX\˜ÚİXŠB‚ˆYˆÙ\Ü^WÜ[ŠÙ[‹[—ÚYˆİŠHOˆ›Û™N‚ˆ\Y˜XİÈHÙ[‹œİÜ˜YÙK™Ù]Ø\Y˜XİÊ[—ÚY
BˆÙ[‹˜İ\œ™[Ü[—ÚYH[—ÚYˆÙ[‹—ÜÙ]Ü™\Ü
™[™\—Ü™\ÙX\˜ÚÜ[Š[—ÚY\Y˜XİÊJB‚ˆYˆÙ^ÜÜ™\Ü
Ù[ŠHOˆ›Û™N‚ˆYˆ›İÙ[‹˜İ\œ™[Ü™\Üİ^œİš\

N‚ˆY\ÜØYÙX›ŞœÚİÚ[™›Ê¹kï9aî¹¢©ydbˆ‹¹odùbcy¬¨y§"ycëùkï9aî¹æ¡9a¡yk®xà ˆŠBˆ™]\›‚ˆ]Hš[YX[ÙË˜\ÚÜØ]™X\Ùš[[˜[YJˆ]OH¹kï9aîˆÜ[•\Ú\È9¢©ydbˆ‹ˆY˜][^[œÚ[ÛH‹‹ˆš[]\\ÏVÊ•^‹Š‹ŠK
“X\šÙİÛˆ‹Š‹›YŠK
’”ÓÓˆ‹Š‹šœÛÛˆŠWKˆ
BˆYˆ]‚ˆ]
]
KÜš]Wİ^
Ù[‹˜İ\œ™[Ü™\Üİ^[˜ÛÙ[™ÏH]‹NŠBˆÙ[‹œİ]\×İ˜\‹œÙ]
ˆ¹¢©ydb¹mì¹kï9aî»ï&Ü]HŠB‚ˆYˆÜÙ]Ü™\Ü
Ù[‹ÛÛ[ˆİŠHOˆ›Û™N‚ˆÙ[‹˜İ\œ™[Ü™\Üİ^HÛÛ[ˆÙ[‹œ™\Üİ^™[]JŒKŒ‹S‘
BˆÙ[‹œ™\Üİ^š[œÙ\
ŒKŒ‹ÛÛ[
Bˆ›ÜˆYÈ[ˆÙ[‹—Ü™\ÜÛ[š×İYÜÎ‚ˆÙ[‹œ™\Üİ^Y×Ù[]JYÊBˆÙ[‹—Ü™\ÜÛ[š×İYÜË˜ÛX\Š
Bˆ›Üˆ[™^X]Ú[ˆ[[Y\˜]J™K™š[™]\ŠˆšÏÎ‹ËÖ×—Ï—J_WJÈ‹ÛÛ[
JN‚ˆ\›HX]Ú™Ü›İ\

Kœœİš\
‹‹Î»ï#8à »ï&ûï&ˆŠBˆYˆ›İ\›‚ˆÛÛ[YBˆYÈHˆœ™\Üİ\›ŞÚ[™^H‚ˆİ\HˆŒKŒ
ÈÛX]Úœİ\

_HÚ\œÈ‚ˆ[™HˆÜİ\H
ÈÛ[Š\›
_HÚ\œÈ‚ˆÙ[‹œ™\Üİ^Y×ØY
YËİ\[™
BˆÙ[‹œ™\Üİ^Y×ØÛÛ™šYİ\™JYË›Ü™YÜ›İ[™HˆÌÍNNH‹[™\›[™OUYJBˆÙ[‹œ™\Üİ^Y×Øš[™
ˆYËˆ]Û‹LOˆ‹ˆ[X™HÙ]™[\™Ù]]\›ˆÙX˜œ›İÜÙ\‹›Ü[—Û™]×İXŠ\™Ù]
Kˆ
BˆÙ[‹œ™\Üİ^Y×Øš[™
ˆYË[\ˆ‹[X™HÙ]™[ˆÙ[‹œ™\Üİ^˜ÛÛ™šYİ\™Jİ\œÛÜHš[™ˆŠBˆ
BˆÙ[‹œ™\Üİ^Y×Øš[™
ˆYËX]™Oˆ‹[X™HÙ]™[ˆÙ[‹œ™\Üİ^˜ÛÛ™šYİ\™Jİ\œÛÜHˆŠBˆ
BˆÙ[‹—Ü™\ÜÛ[š×İYÜË˜\[™
YÊB‚ˆYˆÜ[—Ø˜XÚÙÜ›İ[™
ˆÙ[‹ˆ\ÚÎˆØš™XİˆİXØÙ\Ü×Ù]™[ˆİ‹ˆİ]\×ÛY\ÜØYÙNˆİ‹ˆ
HOˆ›Û™N‚ˆÙ[‹œİ]\×İ˜\‹œÙ]
İ]\×ÛY\ÜØYÙJB‚ˆYˆ[›™\Š
HOˆ›Û™N‚ˆN‚ˆ™\İ[H\ÚÊ
HÈ\NˆYÛ›Ü™VÛÜ\˜]Ü—BˆÙ[‹™]™[Ü]Y]YKœ]

İXØÙ\Ü×Ù]™[™\İ[
JBˆ^Ù\^Ù\[Ûˆ\È^Î‚ˆÙ[‹™]™[Ü]Y]YKœ]
ˆ
ˆ™\œ›Üˆ‹ˆÂˆ›Y\ÜØYÙHˆİŠ^ÊKˆ˜XÙX˜XÚÈˆ˜XÙX˜XÚË™›Ü›X]Ù^Ê
KˆKˆ
Bˆ
B‚ˆ™XY[™Ë•™XY
\™Ù]\[›™\‹Y[[ÛUYJKœİ\

B‚ˆYˆÙ˜Z[—Ù]™[ÊÙ[ŠHOˆ›Û™N‚ˆÚ[HYN‚ˆN‚ˆ]™[^[ØYHÙ[‹™]™[Ü]Y]YK™Ù]Û›İØZ]

Bˆ^Ù\]Y]YK‘[\N‚ˆœ™XZÂˆYˆ]™[OH˜ÛÛ\[WÜ™\İ[È‚ˆÙ[‹˜ÛÛ\[WÜ™\İ[ÈH\İ
^[ØY
HÈ\NˆYÛ›Ü™VØ\™Ë]\WBˆÙ[‹˜ÛÛ\[WÛ\İ™[]JS‘
Bˆ›ÜˆÛÛ\[H[ˆÙ[‹˜ÛÛ\[WÜ™\İ[Î‚ˆÙ[‹˜ÛÛ\[WÛ\İš[œÙ\
S‘ˆØÛÛ\[KXÚÙ\ŸH0­ÈØÛÛ\[K›˜[Y_HŠBˆÙ[‹œİ]\×İ˜\‹œÙ]
ˆ¹¢o¹b,Û[ŠÙ[‹˜ÛÛ\[WÜ™\İ[Ê_H9k­¹ak9cîŠBˆ[Yˆ]™[OHœ›ÙÜ™\ÜÈ‚ˆY\ÜØYÙK\˜Ù[H^[ØYÈ\NˆYÛ›Ü™VÛZ\Ø×BˆÙ[‹œİ]\×İ˜\‹œÙ]
İŠY\ÜØYÙJJBˆÙ[‹œ›ÙÜ™\ÜÖÈ˜[YH—HH[
\˜Ù[
Bˆ[Yˆ]™[OHœ™\ÙX\˜ÚØÛÛ\]H‚ˆ[ˆH^[ØYˆÙ[‹œ[—Ø]Û‹˜ÛÛ™šYİ\™Jİ]OH››Ü›X[ŠBˆÙ[‹œ›ÙÜ™\ÜÖÈ˜[YH—HHLˆÙ[‹—Ü™Yœ™\ÚÚ\İÜJ
BˆÙ[‹—Ü™Yœ™\Úİ\Ù\Ê
BˆÙ[‹—Ù\Ü^WÜ[Š[‹œ[—ÚY
HÈ\NˆYÛ›Ü™VØ]‹YYš[™YBˆÙ[‹œİ]\×İ˜\‹œÙ]
ˆ¹è%9êm¹k£9¢$;ï&Ü[‹œİ]\Ë˜[Y_HŠHÈ\NˆYÛ›Ü™VØ]‹YYš[™YBˆ[Yˆ]™[OH›[Ù[İ\İ‚ˆÙ[‹œİ]\×İ˜\‹œÙ]
İŠ^[ØY
JBˆY\ÜØYÙX›ŞœÚİÚ[™›Ê¹ª(yg¢ú/ç¹£©y­bú+åH‹İŠ^[ØY
JBˆ[Yˆ]™[OH™\œ›Üˆ‚ˆÙ[‹œ[—Ø]Û‹˜ÛÛ™šYİ\™Jİ]OH››Ü›X[ŠBˆÙ[‹œİ]\×İ˜\‹œÙ]
¹.îùb¨yi,z-)HŠBˆ]HH^[ØYÈ\NˆYÛ›Ü™VØ\ÜÚYÛ›Y[BˆÙ[‹—ÜÙ]Ü™\Ü
ˆˆ¹.îùb¨yi,z-)W—Ù]VÉÛY\ÜØYÙI×_W—¹o 9cäz+â¹¥«y/èy kûï&—Ù]VÉİ˜XÙX˜XÚÉ×_H‚ˆ
BˆY\ÜØYÙX›ŞœÚİÙ\œ›ÜŠ“Ü[•\Ú\È9.îùb¨yi,z-)H‹]VÈ›Y\ÜØYÙH—JBˆÙ[‹œ›Ûİ˜Y\ŠLÙ[‹—Ù˜Z[—Ù]™[ÊB‚‚™YˆXZ[Š
HOˆ›Û™N‚ˆXYÛ›ÜİX×Ü]H]
[\š[K™Ù][\\Š
JHÈ“Ü[•\Ú\Ë\İ\\›ÙÈ‚‚ˆYˆXYÛ›ÜİXÊY\ÜØYÙNˆİŠHOˆ›Û™N‚ˆYˆÜË™[š\›Û‹™Ù]
“ÔS•TÒT×ÑPQÓ“ÔÕPÈŠHOHŒH‚ˆÚ]XYÛ›ÜİX×Ü]›Ü[Š˜H‹[˜ÛÙ[™ÏH]‹NŠH\Èİ™X[N‚ˆİ™X[KÜš]JˆÛY\ÜØYÙ_WˆŠB‚ˆXYÛ›ÜİXÊ˜Ü™X][™Ë]Ë\›ÛİŠBˆ›ÛİHË•Ê
BˆXYÛ›ÜİXÊË\›ÛİXÜ™X]YŠBˆÜ[•\Ú\Ğ\
›Ûİ
BˆXYÛ›ÜİXÊ˜\Z[š]X[^™YŠBˆ›Ûİ\]WÚY]\ÚÜÊ
BˆXYÛ›ÜİXÊˆÚ[™İË]]O^İ]_NÜİ]O^Üİ]_NÛX\Y^ÛX\YNİšY]ØX›O^İšY]ØX›_NÈ‚ˆœÚ^™O^İÚY^ÚZYÚH‹™›Ü›X]
ˆ]O\›Ûİ]J
Kˆİ]O\›Ûİœİ]J
KˆX\Y\›ÛİÚ[™›×Ú\ÛX\Y

KˆšY]ØX›O\›ÛİÚ[™›×İšY]ØX›J
KˆÚY\›ÛİÚ[™›×İÚY

KˆZYÚ\›ÛİÚ[™›×ÚZYÚ

Kˆ
Bˆ
BˆİZWÜÛ[ÚÙHHÜË™[š\›Û‹™Ù]
“ÔS•TÒT×ÑÕRWÔÓSÒÑWÕTÕŠHOHŒH‚ˆÛ[ÚÙWÜ™\İ[HÈšY]ØX›Hˆ˜[Ù_BˆYˆİZWÜÛ[ÚÙN‚ˆYˆš[š\ÚÙİZWÜÛ[ÚÙJ
HOˆ›Û™N‚ˆÛ[ÚÙWÜ™\İ[ÈšY]ØX›H—HH›ÛÛ
ˆ›ÛİÚ[™›×Ù^\İÊ
Bˆ[™›ÛİÚ[™›×Ú\ÛX\Y

Bˆ[™›ÛİÚ[™›×İšY]ØX›J
Bˆ[™›ÛİÚ[™›×İÚY

HHNˆ[™›ÛİÚ[™›×ÚZYÚ

HHˆ
Bˆ›Ûİ™\İ›ŞJ
B‚ˆ›Ûİ˜Y\ŠÍLš[š\ÚÙİZWÜÛ[ÚÙJBˆ›Ûİ›XZ[›ÛÜ

BˆXYÛ›ÜİXÊ›XZ[›ÛÜY[™YŠBˆYˆİZWÜÛ[ÚÙH[™›İÛ[ÚÙWÜ™\İ[ÈšY]ØX›H—N‚ˆ˜Z\ÙH[[YQ\œ›ÜŠ”XÚØYÙYÕRHY›İ™XÛÛYHX\Y[™šY]ØX›HŠB