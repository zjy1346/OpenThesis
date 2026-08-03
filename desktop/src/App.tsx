import { useEffect, useMemo, useState, type MouseEvent } from "react";
import {
  BookOpenText,
  ChevronLeft,
  ChevronRight,
  Clock3,
  FileText,
  History,
  Languages,
  PanelLeft,
  Search,
  Settings,
  Sparkles,
} from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import {
  bootstrapBackend,
  cancelResearch,
  getResearchReport,
  getResearchStatus,
  startResearch,
  updatePreferences,
} from "./backend";
import type {
  BootstrapResult,
  Preferences,
  ResearchJob,
  ResearchReport,
  ResearchRunSummary,
} from "./types";

type Language = "zh-CN" | "en";
type ViewId = "workspace" | "history" | "settings";

const COPY = {
  "zh-CN": {
    workspace: "研究工作台",
    history: "研究历史",
    settings: "设置",
    toggleOpen: "展开导航",
    toggleClose: "收起导航",
    closePanel: "关闭导航面板",
    dismissPanel: "关闭导航遮罩",
    emptyTitle: "还没有研究报告",
    emptyBody: "先运行安全的合成演示研究，结构化报告会在这里占据整个工作区。",
    loading: "正在连接研究核心…",
    report: "研究报告",
    noSelection: "从研究历史中选择一份报告",
    currentLanguage: "简体中文",
    coreUnavailable: "OpenThesis 研究核心暂时不可用。",
    reportUnavailable: "无法加载所选研究报告。",
    startDemo: "运行合成演示研究",
    demoHint: "不访问网络、不调用模型，也不使用真实公司数据。",
    cancel: "取消研究",
    settingsTitle: "语言设置",
    settingsBody: "界面语言在下次启动时生效；报告语言会立即用于下一次研究。",
    interfaceLanguage: "界面语言",
    reportLanguage: "研究报告语言",
    chinese: "简体中文",
    english: "English",
    saveSettings: "保存设置",
    saving: "正在保存…",
    saved: "设置已保存。界面语言将在下次启动时生效。",
    settingsFailed: "设置保存失败。",
  },
  en: {
    workspace: "Research workspace",
    history: "Research history",
    settings: "Settings",
    toggleOpen: "Open navigation",
    toggleClose: "Collapse navigation",
    closePanel: "Close navigation panel",
    dismissPanel: "Dismiss navigation overlay",
    emptyTitle: "No research reports yet",
    emptyBody: "Run the safe synthetic demo first. Its structured report will fill this workspace.",
    loading: "Connecting to the research core…",
    report: "Research report",
    noSelection: "Choose a report from research history",
    currentLanguage: "English",
    coreUnavailable: "The OpenThesis research core is unavailable.",
    reportUnavailable: "The selected research report could not be loaded.",
    startDemo: "Run synthetic demo research",
    demoHint: "No network, model, or real company data is used.",
    cancel: "Cancel research",
    settingsTitle: "Language settings",
    settingsBody: "Interface language applies after restart. Report language applies to the next research run immediately.",
    interfaceLanguage: "Interface language",
    reportLanguage: "Report language",
    chinese: "简体中文",
    english: "English",
    saveSettings: "Save settings",
    saving: "Saving…",
    saved: "Settings saved. Interface language will apply after restart.",
    settingsFailed: "Settings could not be saved.",
  },
} as const;

const TERMINAL_JOB_STATES = new Set(["completed", "failed", "cancelled"]);

export default function App() {
  const [bootstrap, setBootstrap] = useState<BootstrapResult | null>(null);
  const [report, setReport] = useState<ResearchReport | null>(null);
  const [job, setJob] = useState<ResearchJob | null>(null);
  const [activeView, setActiveView] = useState<ViewId>("workspace");
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [skipMotion, setSkipMotion] = useState(false);
  const [error, setError] = useState("");
  const [language, setLanguage] = useState<Language>("zh-CN");
  const copy = COPY[language];

  useEffect(() => {
    let active = true;
    void bootstrapBackend()
      .then(async (value) => {
        if (!active) return;
        setBootstrap(value);
        setLanguage(value.preferences.ui_language === "en" ? "en" : "zh-CN");
        setDrawerOpen(value.preferences.sidebar_collapsed !== "true");
        if (value.recent_runs[0]) {
          const initialReport = await getResearchReport(
            value.recent_runs[0].run_id,
            value.preferences.report_language,
          );
          if (active) setReport(initialReport);
        }
      })
      .catch(() => {
        if (active) setError(COPY[language].coreUnavailable);
      });
    return () => {
      active = false;
    };
  }, []);

  useEffect(() => {
    if (!job || TERMINAL_JOB_STATES.has(job.state)) return;
    let active = true;
    const poll = window.setInterval(() => {
      void getResearchStatus(job.job_id)
        .then(async (next) => {
          if (!active) return;
          setJob(next);
          if (next.state === "completed" && next.run_id) {
            window.clearInterval(poll);
            const [nextBootstrap, nextReport] = await Promise.all([
              bootstrapBackend(),
              getResearchReport(next.run_id, bootstrap?.preferences.report_language),
            ]);
            if (!active) return;
            setBootstrap(nextBootstrap);
            setReport(nextReport);
            setActiveView("workspace");
          } else if (next.state === "failed") {
            window.clearInterval(poll);
            setError(next.message || copy.coreUnavailable);
          } else if (next.state === "cancelled") {
            window.clearInterval(poll);
          }
        })
        .catch(() => {
          if (active) {
            setError(copy.coreUnavailable);
            setJob((current) => current ? {
              ...current,
              state: "failed",
              message: copy.coreUnavailable,
            } : current);
          }
        });
    }, 350);
    return () => {
      active = false;
      window.clearInterval(poll);
    };
  }, [job?.job_id]);

  const handleDrawerToggle = (event: MouseEvent<HTMLButtonElement>) => {
    if (event.detail === 0) {
      setSkipMotion(true);
      window.requestAnimationFrame(() => setSkipMotion(false));
    }
    setDrawerOpen((value) => !value);
  };

  const selectRun = async (run: ResearchRunSummary) => {
    setActiveView("workspace");
    setError("");
    try {
      setReport(
        await getResearchReport(run.run_id, bootstrap?.preferences.report_language),
      );
    } catch {
      setError(copy.reportUnavailable);
    }
  };

  const beginDemoResearch = async () => {
    setError("");
    try {
      setJob(await startResearch());
    } catch {
      setError(copy.coreUnavailable);
    }
  };

  const stopResearch = async () => {
    if (!job) return;
    try {
      setJob(await cancelResearch(job.job_id));
    } catch {
      setError(copy.coreUnavailable);
    }
  };

  const saveSettings = async (
    preferences: Pick<Preferences, "ui_language" | "report_language">,
  ) => {
    const saved = await updatePreferences(preferences);
    setBootstrap((current) => current ? { ...current, preferences: saved } : current);
  };

  const navItems = useMemo(
    () => [
      { id: "workspace" as const, label: copy.workspace, icon: BookOpenText },
      { id: "history" as const, label: copy.history, icon: History },
      { id: "settings" as const, label: copy.settings, icon: Settings },
    ],
    [copy],
  );

  return (
    <div className="app-shell" data-skip-motion={skipMotion || undefined}>
      <aside className="nav-rail" aria-label="OpenThesis">
        <div className="brand-mark" aria-hidden="true">OT</div>
        <button className="icon-button drawer-toggle" type="button"
          aria-label={drawerOpen ? copy.toggleClose : copy.toggleOpen}
          aria-expanded={drawerOpen} onClick={handleDrawerToggle}>
          <PanelLeft size={20} strokeWidth={1.8} />
        </button>
        <nav className="rail-actions">
          {navItems.map(({ id, label, icon: Icon }) => (
            <button key={id} className="icon-button"
              data-active={activeView === id || undefined} type="button"
              aria-label={label} title={label}
              onClick={() => { setActiveView(id); setDrawerOpen(true); }}>
              <Icon size={20} strokeWidth={1.8} />
            </button>
          ))}
        </nav>
      </aside>

      <aside className="nav-drawer" data-open={drawerOpen || undefined} aria-hidden={!drawerOpen}>
        <header className="drawer-header">
          <div><span className="eyebrow">OpenThesis 1.0</span><strong>{copy.workspace}</strong></div>
          <button className="icon-button compact" type="button" aria-label={copy.closePanel}
            onClick={() => setDrawerOpen(false)}><ChevronLeft size={18} /></button>
        </header>
        <div className="drawer-search"><Search size={16} /><span>{copy.history}</span></div>
        <div className="history-list">
          {bootstrap?.recent_runs.map((run) => (
            <button key={run.run_id} type="button" onClick={() => void selectRun(run)}>
              <span className="history-symbol">{run.ticker || "—"}</span>
              <span><strong>{run.company_name}</strong><small>{new Date(run.started_at).toLocaleDateString(language)}</small></span>
              <ChevronRight size={15} />
            </button>
          ))}
          {bootstrap && bootstrap.recent_runs.length === 0 && <p className="drawer-empty">{copy.noSelection}</p>}
        </div>
        <footer className="drawer-footer"><Languages size={16} /><span>{copy.currentLanguage}</span><small>v{bootstrap?.app_version ?? "1.0"}</small></footer>
      </aside>

      {drawerOpen && <button className="drawer-scrim" type="button" aria-label={copy.dismissPanel} onClick={() => setDrawerOpen(false)} />}

      <main className="workspace">
        <header className="workspace-header">
          <div><span className="eyebrow"><Sparkles size={14} /> OpenThesis</span><h1>{copy.workspace}</h1></div>
          <div className="status-cluster"><span className="status-dot" /><span>{bootstrap ? `Core ${bootstrap.contract_version}` : copy.loading}</span></div>
        </header>
        {error && <div className="error-banner" role="alert">{error}</div>}
        {job && !TERMINAL_JOB_STATES.has(job.state) && (
          <ResearchProgress job={job} cancelLabel={copy.cancel} onCancel={() => void stopResearch()} />
        )}
        <section className="report-stage" aria-label={copy.report}>
          {activeView === "settings" && bootstrap ? (
            <SettingsView language={language} preferences={bootstrap.preferences} copy={copy} onSave={saveSettings} />
          ) : !bootstrap ? (
            <LoadingState label={copy.loading} />
          ) : report ? (
            <ReportView report={report} label={copy.report} />
          ) : (
            <EmptyState title={copy.emptyTitle} body={copy.emptyBody} action={copy.startDemo}
              hint={copy.demoHint} onStart={() => void beginDemoResearch()} />
          )}
        </section>
      </main>
    </div>
  );
}

function LoadingState({ label }: { label: string }) {
  return <div className="center-state" aria-live="polite"><span className="loading-ring" /><p>{label}</p></div>;
}

function EmptyState({ title, body, action, hint, onStart }: {
  title: string; body: string; action: string; hint: string; onStart: () => void;
}) {
  return (
    <div className="center-state empty-state">
      <span className="empty-icon"><FileText size={28} strokeWidth={1.5} /></span>
      <h2>{title}</h2><p>{body}</p>
      <button className="primary-button" type="button" onClick={onStart}>{action}</button>
      <small>{hint}</small>
    </div>
  );
}

function ResearchProgress({ job, cancelLabel, onCancel }: { job: ResearchJob; cancelLabel: string; onCancel: () => void }) {
  return (
    <section className="research-progress" aria-live="polite">
      <div><strong>{job.message}</strong><span>{job.percent}%</span></div>
      <div className="progress-track"><span style={{ transform: `scaleX(${job.percent / 100})` }} /></div>
      <button type="button" onClick={onCancel} disabled={job.state === "cancelling"}>{cancelLabel}</button>
    </section>
  );
}

function ReportView({ report, label }: { report: ResearchReport; label: string }) {
  return (
    <article className="report-document">
      <header className="report-meta">
        <div><span className="eyebrow">{report.ticker}</span><h2>{report.company_name}</h2></div>
        <span><Clock3 size={15} /> {label}</span>
      </header>
      <div className="report-markdown"><ReactMarkdown remarkPlugins={[remarkGfm]}>{report.markdown}</ReactMarkdown></div>
    </article>
  );
}

function SettingsView({ language, preferences, copy, onSave }: {
  language: Language;
  preferences: Preferences;
  copy: typeof COPY[Language];
  onSave: (value: Pick<Preferences, "ui_language" | "report_language">) => Promise<void>;
}) {
  const [uiLanguage, setUiLanguage] = useState<Language>(preferences.ui_language);
  const [reportLanguage, setReportLanguage] = useState<Language>(preferences.report_language);
  const [state, setState] = useState<"idle" | "saving" | "saved" | "failed">("idle");

  const submit = async () => {
    setState("saving");
    try {
      await onSave({ ui_language: uiLanguage, report_language: reportLanguage });
      setState("saved");
    } catch {
      setState("failed");
    }
  };

  return (
    <div className="settings-view">
      <span className="eyebrow">OpenThesis</span>
      <h2>{copy.settingsTitle}</h2><p>{copy.settingsBody}</p>
      <div className="settings-card">
        <label htmlFor="ui-language">{copy.interfaceLanguage}</label>
        <select id="ui-language" value={uiLanguage} onChange={(event) => setUiLanguage(event.target.value as Language)}>
          <option value="zh-CN">{copy.chinese}</option><option value="en">{copy.english}</option>
        </select>
        <label htmlFor="report-language">{copy.reportLanguage}</label>
        <select id="report-language" value={reportLanguage} onChange={(event) => setReportLanguage(event.target.value as Language)}>
          <option value="zh-CN">{copy.chinese}</option><option value="en">{copy.english}</option>
        </select>
        <button className="primary-button" type="button" onClick={() => void submit()} disabled={state === "saving"}>
          {state === "saving" ? copy.saving : copy.saveSettings}
        </button>
        {state === "saved" && <p className="settings-message" role="status">{copy.saved}</p>}
        {state === "failed" && <p className="settings-message error" role="alert">{copy.settingsFailed}</p>}
      </div>
      <small className="settings-locale">{language}</small>
    </div>
  );
}
