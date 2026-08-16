import { useEffect, useMemo, useRef, useState, type MouseEvent } from "react";
import {
  BookOpenText,
  CircleHelp,
  ChevronLeft,
  ExternalLink,
  FileText,
  History,
  Info,
  Languages,
  PanelLeft,
  Plus,
  Settings,
  Sparkles,
} from "lucide-react";

import { isActiveResearchJob, useWorkbenchSession } from "./app/useWorkbenchSession";
import { EmptyState, LoadingState, ResearchProgress } from "./components/States";
import { AboutView } from "./features/about/AboutView";
import { HelpView } from "./features/help/HelpView";
import { HistoryView } from "./features/history/HistoryView";
import { ReportWorkspace } from "./features/report/ReportWorkspace";
import { NewResearchView } from "./features/research/NewResearchView";
import { SettingsView } from "./features/settings/SettingsView";
import { ThesisView } from "./features/thesis/ThesisView";
import { COPY } from "./i18n";
import type {
  Language,
  ResearchRequest,
} from "./types";

type ViewId = "workspace" | "new-research" | "history" | "theses" | "settings" | "help" | "about";

export default function App() {
  const [activeView, setActiveView] = useState<ViewId>("workspace");
  const [drawerPinned, setDrawerPinned] = useState(false);
  const [drawerHovered, setDrawerHovered] = useState(false);
  const [skipMotion, setSkipMotion] = useState(false);
  const [language, setLanguage] = useState<Language>("zh-CN");
  const initialPreferencesApplied = useRef(false);
  const suppressHoverUntilLeave = useRef(false);
  const copy = COPY[language];
  const {
    bootstrap,
    report,
    job,
    error,
    canRetry,
    selectRun,
    removeRun,
    beginResearch,
    retryResearch,
    retrySynthesis,
    retryGrowth,
    openFailedDisclosure,
    stopResearch,
    decideVisionUpload,
    savePreferences,
    refreshBootstrap,
  } = useWorkbenchSession();

  useEffect(() => {
    if (!bootstrap || initialPreferencesApplied.current) return;
    initialPreferencesApplied.current = true;
    setLanguage(bootstrap.preferences.ui_language === "en" ? "en" : "zh-CN");
    setDrawerPinned(bootstrap.preferences.sidebar_collapsed !== "true");
  }, [bootstrap]);

  const handleDrawerToggle = (event: MouseEvent<HTMLButtonElement>) => {
    if (event.detail === 0) {
      setSkipMotion(true);
      window.requestAnimationFrame(() => setSkipMotion(false));
    }
    const nextPinned = !drawerPinned;
    setDrawerPinned(nextPinned);
    if (!nextPinned) {
      suppressHoverUntilLeave.current = true;
      setDrawerHovered(false);
    }
    if (bootstrap) {
      void savePreferences({ sidebar_collapsed: nextPinned ? "false" : "true" }).catch(() => undefined);
    }
  };

  const handleNavigationEnter = () => {
    if (!suppressHoverUntilLeave.current) setDrawerHovered(true);
  };

  const handleNavigationLeave = () => {
    suppressHoverUntilLeave.current = false;
    setDrawerHovered(false);
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
    { id: "help" as const, label: copy.help, icon: CircleHelp },
    { id: "about" as const, label: copy.about, icon: Info },
  ], [copy]);

  const drawerOpen = drawerPinned || drawerHovered;
  const pageTitle: Record<ViewId, string> = {
    workspace: copy.report,
    "new-research": copy.newResearch,
    history: copy.history,
    theses: copy.theses,
    settings: copy.settings,
    help: copy.help,
    about: copy.about,
  };

  const navigateTo = (id: ViewId) => {
    setActiveView(id);
    if (!drawerPinned) setDrawerHovered(false);
  };

  return (
    <div className="app-shell" data-skip-motion={skipMotion || undefined}>
      <aside
        className="navigation-shell"
        data-open={drawerOpen || undefined}
        aria-label={copy.navigation}
        onMouseEnter={handleNavigationEnter}
        onMouseLeave={handleNavigationLeave}
      >
        <div className="nav-rail">
          <div className="brand-mark" aria-hidden="true">OT</div>
          <button className="icon-button drawer-toggle" type="button"
            aria-label={drawerPinned ? copy.toggleClose : copy.toggleOpen}
            aria-expanded={drawerOpen} onClick={handleDrawerToggle}>
            <PanelLeft size={20} strokeWidth={1.8} />
          </button>
          <nav className="rail-actions" aria-label={copy.navigation}>
            {navItems.map(({ id, label, icon: Icon }) => (
              <button key={id} className="icon-button"
                data-active={activeView === id || undefined} type="button"
                aria-label={label} title={drawerOpen ? undefined : label}
                onClick={() => navigateTo(id)}>
                <Icon size={20} strokeWidth={1.8} />
              </button>
            ))}
          </nav>
        </div>

        <div className="nav-drawer" data-open={drawerOpen || undefined} aria-hidden={!drawerOpen}>
          <div className="drawer-brand"><strong>OpenThesis</strong></div>
          <button className="drawer-pin" type="button" onClick={handleDrawerToggle}>
            <span>{drawerPinned ? copy.toggleClose : copy.toggleOpen}</span><ChevronLeft size={16} />
          </button>
          <nav className="drawer-actions" aria-label={copy.navigationLabels}>
            {navItems.map(({ id, label }) => (
              <button key={id} type="button" data-active={activeView === id || undefined}
                onClick={() => navigateTo(id)}>{label}</button>
            ))}
          </nav>
          <footer className="drawer-footer"><Languages size={16} /><span>{copy.currentLanguage}</span><small>v{bootstrap?.app_version ?? "1.0"}</small></footer>
        </div>
      </aside>

      <main className="workspace">
        <header className="workspace-header">
          <div><span className="eyebrow"><Sparkles size={14} /> OpenThesis</span><h1>{pageTitle[activeView]}</h1></div>
          <div className="status-cluster"><span className="status-dot" /><span>{bootstrap ? `Core ${bootstrap.contract_version}` : copy.loading}</span></div>
        </header>
        {error && <div className="error-banner" role="alert" data-tone={error.kind === "research-failed" && error.code === "NO_FILINGS_AVAILABLE" ? "notice" : undefined}>
          <span>{error.kind === "report-unavailable" ? copy.reportUnavailable : (error.detail ?? copy.coreUnavailable)}</span>
          <div className="error-actions">
            {canRetry && <button type="button" onClick={() => void retryResearch()}>{error.kind === "research-failed" ? copy.retryFetch : copy.retry}</button>}
            {error.kind === "research-failed" && error.disclosureUrl && <button type="button" onClick={() => void openFailedDisclosure()}><ExternalLink size={14} />{copy.officialDisclosure}</button>}
          </div>
        </div>}
        {bootstrap && bootstrap.interrupted_runs > 0 && (
          <div className="recovery-banner" role="status">
            {copy.interruptedRecovery.replace("{count}", String(bootstrap.interrupted_runs))}
          </div>
        )}
        {isActiveResearchJob(job) && (
          <ResearchProgress
            job={job}
            language={bootstrap?.preferences.ui_language ?? language}
            cancelLabel={copy.cancel}
            labels={{
              cancel: copy.cancel,
              cancelling: copy.cancelling,
              agents: copy.agents,
              running: copy.running,
              retrying: copy.retrying,
              queued: copy.queued,
              completed: copy.completed,
              cancelled: copy.cancelled,
              failed: copy.failed,
              unknown: copy.waiting,
              visionApprovalTitle: copy.visionApprovalTitle,
              visionApprovalProvider: copy.visionApprovalProvider,
              visionApprovalDocument: copy.visionApprovalDocument,
              visionApprovalPages: copy.visionApprovalPages,
              visionApprovalSize: copy.visionApprovalSize,
              visionApprovalFingerprint: copy.visionApprovalFingerprint,
              visionApprovalApprove: copy.visionApprovalApprove,
              visionApprovalDecline: copy.visionApprovalDecline,
            }}
            onCancel={() => void stopResearch()}
            onVisionDecision={(approved) => void decideVisionUpload(approved)}
          />
        )}
        <section className="report-stage" aria-label={copy.report}>
          {activeView === "settings" && bootstrap ? (
            <SettingsView language={language} preferences={bootstrap.preferences} copy={copy} onSave={savePreferences} />
          ) : activeView === "new-research" && bootstrap ? (
            <NewResearchView bootstrap={bootstrap} copy={copy} onSavePreferences={savePreferences} onStart={startNewResearch} />
          ) : activeView === "history" && bootstrap ? (
            <HistoryView runs={bootstrap.recent_runs} language={language} copy={copy}
              onRefresh={refreshBootstrap} onSelect={openRun} onDelete={removeRun} />
          ) : activeView === "theses" && bootstrap ? (
            <ThesisView copy={copy} />
          ) : activeView === "help" && bootstrap ? (
            <HelpView language={language} copy={copy} />
          ) : activeView === "about" && bootstrap ? (
            <AboutView bootstrap={bootstrap} copy={copy} />
          ) : !bootstrap ? (
            <LoadingState label={copy.loading} />
          ) : report ? (
            <ReportWorkspace report={report} copy={copy} onRetrySynthesis={retrySynthesis} onRetryGrowth={retryGrowth} />
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
