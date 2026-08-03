import { useEffect, useMemo, useRef, useState, type MouseEvent } from "react";
import {
  BookOpenText,
  CircleHelp,
  ChevronLeft,
  ChevronRight,
  FileText,
  History,
  Languages,
  PanelLeft,
  Plus,
  RefreshCw,
  Search,
  Settings,
  Sparkles,
} from "lucide-react";

import { isActiveResearchJob, useWorkbenchSession } from "./app/useWorkbenchSession";
import { EmptyState, LoadingState, ResearchProgress } from "./components/States";
import { AboutView } from "./features/about/AboutView";
import { ReportWorkspace } from "./features/report/ReportWorkspace";
import { NewResearchView } from "./features/research/NewResearchView";
import { SettingsView } from "./features/settings/SettingsView";
import { ThesisView } from "./features/thesis/ThesisView";
import { COPY } from "./i18n";
import type {
  Language,
  ResearchRequest,
} from "./types";

type ViewId = "workspace" | "new-research" | "history" | "theses" | "settings" | "about";

export default function App() {
  const [activeView, setActiveView] = useState<ViewId>("workspace");
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [skipMotion, setSkipMotion] = useState(false);
  const [language, setLanguage] = useState<Language>("zh-CN");
  const initialPreferencesApplied = useRef(false);
  const copy = COPY[language];
  const {
    bootstrap,
    report,
    job,
    error,
    canRetry,
    selectRun,
    beginResearch,
    retryResearch,
    stopResearch,
    savePreferences,
    refreshBootstrap,
  } = useWorkbenchSession();

  useEffect(() => {
    if (!bootstrap || initialPreferencesApplied.current) return;
    initialPreferencesApplied.current = true;
    setLanguage(bootstrap.preferences.ui_language === "en" ? "en" : "zh-CN");
    setDrawerOpen(bootstrap.preferences.sidebar_collapsed !== "true");
  }, [bootstrap]);

  const handleDrawerToggle = (event: MouseEvent<HTMLButtonElement>) => {
    if (event.detail === 0) {
      setSkipMotion(true);
      window.requestAnimationFrame(() => setSkipMotion(false));
    }
    setDrawerOpen((value) => !value);
  };

  const openRun = async (run: Parameters<typeof selectRun>[0]) => {
    setActiveView("workspace");
    await selectRun(run);
  };

  const startNewResearch = async (request: ResearchRequest = { mode: "demo" }) => {
    setActiveView("workspace");
    await beginResearch(request);
  };

  const navItems = useMemo(() => [
    { id: "new-research" as const, label: copy.newResearch, icon: Plus },
    { id: "workspace" as const, label: copy.workspace, icon: BookOpenText },
    { id: "history" as const, label: copy.history, icon: History },
    { id: "theses" as const, label: copy.theses, icon: FileText },
    { id: "settings" as const, label: copy.settings, icon: Settings },
    { id: "about" as const, label: copy.about, icon: CircleHelp },
  ], [copy]);

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
              onClick={() => { setActiveView(id); setDrawerOpen(id === "history"); }}>
              <Icon size={20} strokeWidth={1.8} />
            </button>
          ))}
        </nav>
      </aside>

      <aside className="nav-drawer" data-open={drawerOpen || undefined} aria-hidden={!drawerOpen}>
        <header className="drawer-header">
          <div><span className="eyebrow">OpenThesis 1.0</span><strong>{copy.history}</strong></div>
          <div className="drawer-header-actions">
            <button className="icon-button compact" type="button" aria-label={copy.refreshHistory} title={copy.refreshHistory}
              onClick={() => void refreshBootstrap()}><RefreshCw size={16} /></button>
            <button className="icon-button compact" type="button" aria-label={copy.closePanel}
              onClick={() => setDrawerOpen(false)}><ChevronLeft size={18} /></button>
          </div>
        </header>
        <div className="drawer-search"><Search size={16} /><span>{copy.history}</span></div>
        <div className="history-list">
          {bootstrap?.recent_runs.map((run) => (
            <button key={run.run_id} type="button" onClick={() => void openRun(run)}>
              <span className="history-symbol">{run.ticker || "—"}</span>
              <span><strong>{run.company_name}</strong><small>{new Date(run.started_at).toLocaleDateString(language)}</small></span>
              <ChevronRight size={15} />
            </button>
          ))}
          {bootstrap && bootstrap.recent_runs.length === 0 && <p className="drawer-empty">{copy.noSelection}</p>}
        </div>
        <footer className="drawer-footer"><Languages size={16} /><span>{copy.currentLanguage}</span><small>v{bootstrap?.app_version ?? "1.0"}</small></footer>
      </aside>

      <button className="drawer-scrim" type="button" data-open={drawerOpen || undefined}
        aria-label={copy.dismissPanel} aria-hidden={!drawerOpen} tabIndex={drawerOpen ? 0 : -1}
        onClick={() => setDrawerOpen(false)} />

      <main className="workspace">
        <header className="workspace-header">
          <div><span className="eyebrow"><Sparkles size={14} /> OpenThesis</span><h1>{activeView === "new-research" ? copy.newResearch : copy.workspace}</h1></div>
          <div className="status-cluster"><span className="status-dot" /><span>{bootstrap ? `Core ${bootstrap.contract_version}` : copy.loading}</span></div>
        </header>
        {error && <div className="error-banner" role="alert">
          <span>{error.kind === "report-unavailable" ? copy.reportUnavailable : (error.detail ?? copy.coreUnavailable)}</span>
          {canRetry && <button type="button" onClick={() => void retryResearch()}>{copy.retry}</button>}
        </div>}
        {bootstrap && bootstrap.interrupted_runs > 0 && (
          <div className="recovery-banner" role="status">
            {copy.interruptedRecovery.replace("{count}", String(bootstrap.interrupted_runs))}
          </div>
        )}
        {isActiveResearchJob(job) && (
          <ResearchProgress
            job={job}
            cancelLabel={copy.cancel}
            labels={{
              cancel: copy.cancel,
              cancelling: copy.cancelling,
              agents: copy.agents,
              running: copy.running,
              queued: copy.queued,
              completed: copy.completed,
              cancelled: copy.cancelled,
              failed: copy.failed,
              unknown: copy.waiting,
            }}
            onCancel={() => void stopResearch()}
          />
        )}
        <section className="report-stage" aria-label={copy.report}>
          {activeView === "settings" && bootstrap ? (
            <SettingsView language={language} preferences={bootstrap.preferences} copy={copy} onSave={savePreferences} />
          ) : activeView === "new-research" && bootstrap ? (
            <NewResearchView bootstrap={bootstrap} copy={copy} onSavePreferences={savePreferences} onStart={startNewResearch} />
          ) : activeView === "theses" && bootstrap ? (
            <ThesisView copy={copy} />
          ) : activeView === "about" && bootstrap ? (
            <AboutView bootstrap={bootstrap} copy={copy} />
          ) : !bootstrap ? (
            <LoadingState label={copy.loading} />
          ) : report ? (
            <ReportWorkspace report={report} copy={copy} />
          ) : (
            <EmptyState title={copy.emptyTitle} body={copy.emptyBody} demoAction={copy.startDemo}
              realAction={copy.startReal} hint={copy.demoHint}
              onDemo={() => void startNewResearch()} onReal={() => setActiveView("new-research")} />
          )}
        </section>
      </main>
    </div>
  );
}
