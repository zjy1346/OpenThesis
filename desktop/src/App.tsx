import { useEffect, useMemo, useRef, useState, type MouseEvent } from "react";
import {
  BookOpenText,
  Boxes,
  CircleHelp,
  Code2,
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
  ListTodo,
} from "lucide-react";

import { useWorkbenchSession } from "./app/useWorkbenchSession";
import { EmptyState, LoadingState } from "./components/States";
import { finishWindowClose, listenForWindowClose, listConfiguredModels, listResearchTasks, extendResearchBudget } from "./backend";
import { ResearchTaskCenter, type TaskModelChoice } from "./features/tasks/ResearchTaskCenter";
import { getResearchTaskCopy } from "./features/tasks/researchTasksCopy";
import { CoreStartup } from "./components/CoreStartup";
import { AboutView } from "./features/about/AboutView";
import { HelpView } from "./features/help/HelpView";
import { HistoryView } from "./features/history/HistoryView";
import { ModelCenterView } from "./features/models/ModelCenterView";
import { OtStudioView } from "./features/ot-studio/OtStudioView";
import { ReportWorkspace } from "./features/report/ReportWorkspace";
import { NewResearchView } from "./features/research/NewResearchView";
import { ResearchDiagnosticsAction } from "./features/research/ModelCallProgress";
import { BudgetGapApproval } from "./features/research/BudgetGapApproval";
import { SettingsView } from "./features/settings/SettingsView";
import { ThesisView } from "./features/thesis/ThesisView";
import { COPY } from "./i18n";
import { resolveUiLanguage, setDocumentLanguage } from "./languageRegistry";
import type {
  Language,
  ResearchRequest,
} from "./types";

type ViewId = "workspace" | "new-research" | "history" | "theses" | "models" | "ot-studio" | "settings" | "help" | "about" | "tasks";

export default function App() {
  const [activeView, setActiveView] = useState<ViewId>("workspace");
  const [taskDrawerOpen, setTaskDrawerOpen] = useState(false);
  const [taskModels, setTaskModels] = useState<TaskModelChoice[]>([]);
  const [submissionMessage, setSubmissionMessage] = useState("");
  const [closePrompt, setClosePrompt] = useState(false);
  const [closing, setClosing] = useState(false);
  const [closeError, setCloseError] = useState("");
  const taskTrigger = useRef<HTMLButtonElement | null>(null);
  const taskDrawer = useRef<HTMLDivElement | null>(null);
  const closeDialog = useRef<HTMLElement | null>(null);
  const [drawerPinned, setDrawerPinned] = useState(false);
  const [drawerHovered, setDrawerHovered] = useState(false);
  const [skipMotion, setSkipMotion] = useState(false);
  const [language, setLanguage] = useState<Language>(() => resolveUiLanguage("system", undefined, typeof navigator === "undefined" ? [] : navigator.languages));
  const initialPreferencesApplied = useRef(false);
  const suppressHoverUntilLeave = useRef(false);
  const copy = COPY[language];
  const {
    bootstrap,
    report,
    reportLoadState,
    tasks,
    error,
    canRetry,
    selectRun,
    retryReportRead,
    removeRun,
    beginResearch,
    retryResearch,
    retryModelStages,
    approveBudgetGaps,
    loadResearchCallHistory,
    retrySynthesis,
    retryGrowth,
    retryRecoveryStage,
    revalidateSaved,
    retryFinancials,
    rebuildFinancials,
    refreshFinancialReport,
    openFailedDisclosure,
    savePreferences,
    refreshBootstrap,
  } = useWorkbenchSession();
  const storageRecoveryRequired = error?.kind === "storage-recovery-required";
  const taskCopy = getResearchTaskCopy(language);
  const text = (en: string, zh: string, hant: string = zh) => language === "en" ? en : language === "zh-Hant" ? hant : zh;
  const taskCount = Object.values(tasks.counts).reduce((sum, value) => sum + value, 0);

  useEffect(() => {
    let active = true;
    const registered = listenForWindowClose(() => {
      void listResearchTasks({ limit: 1 }).then((page) => {
        if (!active) return;
        if (Object.values(page.counts).some((count) => count > 0)) setClosePrompt(true);
        else void finishWindowClose("exit").catch(() => setClosePrompt(true));
      }).catch(() => { if (active) setClosePrompt(true); });
    }).catch(() => undefined);
    return () => { active = false; void registered.then((unlisten) => unlisten?.()); };
  }, []);

  useEffect(() => {
    if (!taskDrawerOpen && activeView !== "tasks") return;
    let active = true;
    void listConfiguredModels().then((models) => {
      if (active) setTaskModels(models.filter((model) => model.enabled && model.connection_enabled !== false
        && model.credential_available !== false && model.capabilities.includes("text_chat")));
    }).catch(() => { if (active) setTaskModels([]); });
    return () => { active = false; };
  }, [taskDrawerOpen, activeView]);

  useEffect(() => { if (taskDrawerOpen) taskDrawer.current?.focus({ preventScroll: true }); }, [taskDrawerOpen]);
  useEffect(() => { if (closePrompt) closeDialog.current?.querySelector<HTMLButtonElement>("button")?.focus(); }, [closePrompt]);

  useEffect(() => {
    if (!bootstrap || initialPreferencesApplied.current) return;
    initialPreferencesApplied.current = true;
    const resolved = resolveUiLanguage(bootstrap.preferences.ui_language_mode, bootstrap.preferences.ui_language, typeof navigator === "undefined" ? [] : navigator.languages);
    setLanguage(resolved);
    setDocumentLanguage(resolved);
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
    const accepted = await beginResearch(request);
    if (!accepted) return;
    setSubmissionMessage(accepted.state === "queued" && (accepted.ahead_count ?? 0) > 0
      ? text(`Added to queue · ${accepted.ahead_count} ahead`, `已加入队列，前面有 ${accepted.ahead_count} 个任务`, `已加入佇列，前面有 ${accepted.ahead_count} 個任務`)
      : text("Research submitted", "研究已提交", "研究已提交"));
  };

  const navItems = useMemo(() => [
    { id: "new-research" as const, label: copy.newResearch, icon: Plus },
    { id: "workspace" as const, label: copy.workspace, icon: BookOpenText },
    { id: "tasks" as const, label: taskCopy.title, icon: ListTodo },
    { id: "history" as const, label: copy.history, icon: History },
    { id: "theses" as const, label: copy.theses, icon: FileText },
    { id: "models" as const, label: copy.modelCenter, icon: Boxes },
    { id: "ot-studio" as const, label: copy.otStudio, icon: Code2 },
    { id: "settings" as const, label: copy.settings, icon: Settings },
    { id: "help" as const, label: copy.help, icon: CircleHelp },
    { id: "about" as const, label: copy.about, icon: Info },
  ], [copy, taskCopy.title]);

  const drawerOpen = drawerPinned || drawerHovered;
  const pageTitle: Record<ViewId, string> = {
    workspace: copy.report,
    "new-research": copy.newResearch,
    history: copy.history,
    theses: copy.theses,
    models: copy.modelCenter,
    "ot-studio": copy.otStudio,
    settings: copy.settings,
    help: copy.help,
    about: copy.about,
    tasks: taskCopy.title,
  };
  const storageRecoveryPanel = <section className="report-no-content" role="status">
    <h3>{copy.storageRecoveryTitle}</h3>
    <p>{copy.storageRecoverySummary}</p>
  </section>;
  const reportActionProps = storageRecoveryRequired
    ? { suppressResearchActions: true }
    : {
      onRetryModelStages: retryModelStages,
      onRetrySynthesis: retrySynthesis,
      onRetryGrowth: retryGrowth,
      onRetryRecoveryStage: retryRecoveryStage,
      onRevalidateSaved: revalidateSaved,
      onRetryFinancials: retryFinancials,
      onRebuildFinancials: rebuildFinancials,
      onRefreshFinancialReport: refreshFinancialReport,
      onApproveBudgetGaps: approveBudgetGaps,
      onConfigureCloud: () => setActiveView("models"),
    };

  const navigateTo = (id: ViewId) => {
    if (id === "tasks") setTaskDrawerOpen(true);
    else setActiveView(id);
    if (!drawerPinned) setDrawerHovered(false);
  };

  const closeTaskDrawer = () => {
    setTaskDrawerOpen(false);
    if (activeView !== "tasks") tasks.selectTask(null);
    taskTrigger.current?.focus({ preventScroll: true });
  };
  const taskCenterProps = {
    language, items: tasks.items, counts: tasks.counts, queuePaused: tasks.queuePaused,
    loading: tasks.loading, error: tasks.error, hasMore: tasks.hasMore,
    selectedTaskId: tasks.selectedTaskId, detail: tasks.detail, models: taskModels,
    onSelect: tasks.selectTask,
    onCancel: (id: string) => { void tasks.cancelTask(id); },
    onResume: (id: string, revision: number, model?: ResearchRequest["model"]) => { void tasks.resumeTask(id, revision, model); },
    onArchive: (id: string, revision: number) => { void tasks.archiveTask(id, revision); },
    onViewReport: (runId: string) => {
      const task = tasks.items.find((item) => item.run_id === runId);
      closeTaskDrawer();
      void openRun({ run_id: runId, company_name: task?.company.name ?? "", ticker: task?.company.ticker ?? "" } as Parameters<typeof selectRun>[0]);
    },
    onClose: () => { if (activeView === "tasks") setActiveView("workspace"); closeTaskDrawer(); },
    onFullPage: () => { setTaskDrawerOpen(false); setActiveView("tasks"); },
    onContinueQueue: () => { void tasks.continueQueue(); },
    onLoadMore: () => { void tasks.loadMore(); },
    onCancelModel: (id: string, requestId: string, attemptId: string) => { void tasks.cancelTaskModel(id, requestId, attemptId); },
    loadCallHistory: loadResearchCallHistory,
    onVisionDecision: (id: string, revision: number, planId: string, approved: boolean) => { void tasks.decideTaskVision(id, revision, planId, approved); },
    children: tasks.detail?.run_id && <>
      <ResearchDiagnosticsAction runId={tasks.detail.run_id} language={language} />
      <BudgetGapApproval runId={tasks.detail.run_id} callPlan={tasks.detail.call_plan} language={language}
        onApprove={async (digest, gaps, choice) => {
          const selected = tasks.detail!;
          await extendResearchBudget(selected.run_id!, digest, gaps, choice);
          const resumed = await tasks.resumeTask(selected.job_id, selected.revision ?? 0);
          if (!resumed) throw new Error("RESEARCH_TASK_RESUME_FAILED");
        }} />
      {tasks.detail.blocked_reason === "TASK_VISION_POLICY_CHANGED" && <button type="button"
        onClick={() => void tasks.resumeTask(tasks.detail!.job_id, tasks.detail!.revision ?? 0, undefined, true)}>
        {text("Use current vision settings and continue", "使用当前视觉设置并继续", "使用目前視覺設定並繼續")}</button>}
    </>,
  };

  // Do not expose localized navigation before bootstrap has resolved the
  // persisted manual preference.  The neutral shell is intentionally free of
  // language-specific labels; bootstrap failures fall through to the normal
  // error-visible application shell below.
  if (!bootstrap && !error) {
    return <CoreStartup language={language} />;
  }

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
                ref={id === "tasks" ? taskTrigger : undefined}
                data-active={activeView === id || undefined} type="button"
                aria-label={id === "tasks" ? `${label} · ${taskCopy.states.running} ${tasks.counts.running + tasks.counts.cancelling} · ${taskCopy.states.queued} ${tasks.counts.queued} · ${taskCopy.states.waiting_input} ${tasks.counts.waiting_input + tasks.counts.interrupted}` : label} title={drawerOpen ? undefined : label}
                onClick={(event) => { if (id === "tasks") setSkipMotion(event.detail === 0); navigateTo(id); }}>
                <Icon size={20} strokeWidth={1.8} />
                {id === "tasks" && taskCount > 0 && <span className="task-rail-count" aria-hidden="true">{taskCount}</span>}
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
                onClick={() => navigateTo(id)}>{label}{id === "tasks" && taskCount > 0 && <small className="task-nav-counts">
                  {taskCopy.states.running} {tasks.counts.running + tasks.counts.cancelling} · {taskCopy.states.queued} {tasks.counts.queued} · {taskCopy.states.waiting_input} {tasks.counts.waiting_input + tasks.counts.interrupted}
                </small>}</button>
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
        {error && error.kind !== "report-unavailable" && <div className="error-banner" role="alert" data-tone={error.kind === "research-failed" && error.code === "NO_FILINGS_AVAILABLE" ? "notice" : undefined}>
          <span>{storageRecoveryRequired ? copy.storageRecoveryRequired : error.detail ?? copy.coreUnavailable}</span>
          {!storageRecoveryRequired && <div className="error-actions">
              {error.kind === "research-failed" && error.code?.startsWith("VISION_") && (
                <button type="button" onClick={() => setActiveView("settings")}>{copy.openVisionSettings}</button>
              )}
              {canRetry && <button type="button" onClick={() => void retryResearch()}>{error.kind === "research-failed" ? copy.retryFetch : copy.retry}</button>}
              {canRetry && error.kind === "research-failed" && error.code === "FILING_RESEARCH_SCOPE_CONFIRMATION_REQUIRED" &&
                <button type="button" onClick={() => void retryResearch(true)}>{language === "en" ? "Research available material only" : language === "zh-Hant" ? "僅用現有資料研究" : "仅用现有资料研究"}</button>}
              {error.kind === "research-failed" && error.disclosureUrl && <button type="button" onClick={() => void openFailedDisclosure()}><ExternalLink size={14} />{copy.officialDisclosure}</button>}
          </div>}
        </div>}
        {bootstrap && bootstrap.interrupted_runs > 0 && (
          <div className="recovery-banner" role="status">
            {copy.interruptedRecovery.replace("{count}", String(bootstrap.interrupted_runs))}
          </div>
        )}
        {submissionMessage && <div className="task-submission-notice" role="status">
          <span>{submissionMessage}</span><button type="button" onClick={() => setTaskDrawerOpen(true)}>{taskCopy.title}</button>
          <button type="button" aria-label={taskCopy.close} onClick={() => setSubmissionMessage("")}>×</button>
        </div>}
        {tasks.notifications.length > 0 && <section className="task-notifications" aria-live="polite" aria-label={taskCopy.title}>
          {tasks.notifications.slice(-3).map((notice) => <div key={notice.id} role="status">
            <span>{notice.company.name || notice.company.ticker} · {taskCopy.states[notice.state]}</span>
            <button type="button" onClick={() => { tasks.selectTask(notice.job_id); setTaskDrawerOpen(true); }}>{taskCopy.details}</button>
            {notice.run_id && <button type="button" onClick={() => taskCenterProps.onViewReport(notice.run_id)}>{taskCopy.viewReport}</button>}
            <button type="button" aria-label={taskCopy.close} onClick={() => tasks.dismissNotification(notice.id)}>×</button>
          </div>)}
        </section>}
        <section className={`report-stage${activeView === "models" ? " model-center-stage" : ""}`} aria-label={copy.report}>
          {activeView === "tasks" ? (
            <div className="task-page"><ResearchTaskCenter variant="page" {...taskCenterProps} /></div>
          ) : activeView === "settings" && bootstrap ? (
            <SettingsView language={language} preferences={bootstrap.preferences} copy={copy} onSave={savePreferences} />
          ) : activeView === "models" ? (
            <ModelCenterView language={language} />
          ) : activeView === "ot-studio" ? (
            <OtStudioView language={language} onOpenModelCenter={() => setActiveView("models")} />
          ) : activeView === "new-research" && storageRecoveryRequired ? (
            storageRecoveryPanel
          ) : activeView === "new-research" && bootstrap ? (
            <NewResearchView bootstrap={bootstrap} copy={copy} activeResearchTasks={tasks.items}
              onOpenTask={(id) => { tasks.selectTask(id); setTaskDrawerOpen(true); }}
              onOpenModelCenter={() => setActiveView("models")} onOpenVisionSettings={() => setActiveView("settings")} onSavePreferences={savePreferences} onStart={startNewResearch} />
          ) : activeView === "history" && bootstrap ? (
            <HistoryView runs={bootstrap.recent_runs} language={language} copy={copy}
              onRefresh={refreshBootstrap} onSelect={openRun} onDelete={removeRun} />
          ) : activeView === "theses" && bootstrap ? (
            <ThesisView copy={copy} language={language} />
          ) : activeView === "help" && bootstrap ? (
            <HelpView language={language} copy={copy} />
          ) : activeView === "about" && bootstrap ? (
            <AboutView bootstrap={bootstrap} copy={copy} />
          ) : storageRecoveryRequired && !report ? (
            storageRecoveryPanel
          ) : !bootstrap ? (
            <LoadingState label={copy.loading} />
          ) : report && reportLoadState.status === "failed" && reportLoadState.run_id === report.run_id && reportLoadState.preserving_previous && !storageRecoveryRequired ? (
            <>
              <div className="report-status report-status-partial" role="alert">
                <span>{copy.reportPreviousVersion}</span>
                <button type="button" onClick={() => void retryReportRead()}>{copy.retryReportRead}</button>
              </div>
              <ReportWorkspace report={report} copy={copy} {...reportActionProps} />
            </>
          ) : report ? (
            <ReportWorkspace report={report} copy={copy} {...reportActionProps} />
          ) : storageRecoveryRequired ? (
            storageRecoveryPanel
          ) : reportLoadState.status === "loading" ? (
            <LoadingState label={copy.reportReading} />
          ) : reportLoadState.status === "failed" ? (
            <section className="report-no-content" role="alert">
              <h3>{copy.reportUnavailable}</h3>
              <p>{copy.reportReadFailure
                .replace("{company}", reportLoadState.company_name || reportLoadState.ticker || reportLoadState.run_id)
                .replace("{ticker}", reportLoadState.ticker || "—")
                .replace("{code}", reportLoadState.code)}</p>
              <div className="error-actions">
                <button type="button" onClick={() => void retryReportRead()}>{copy.retryReportRead}</button>
                <button type="button" onClick={() => setActiveView("history")}>{copy.returnResearchHistory}</button>
              </div>
            </section>
          ) : (
            <EmptyState title={copy.emptyTitle} body={copy.emptyBody} demoAction={copy.startDemo}
              realAction={copy.startReal} hint={copy.demoHint}
              onDemo={() => void startNewResearch()} onReal={() => setActiveView("new-research")} />
          )}
        </section>
      </main>
      {taskDrawerOpen && <div className="task-drawer-host" ref={taskDrawer} tabIndex={-1} role="dialog" aria-modal="false" aria-label={taskCopy.title}>
        <ResearchTaskCenter variant="drawer" {...taskCenterProps} />
      </div>}
      {closePrompt && <div className="task-close-backdrop"><section className="task-close-dialog" ref={closeDialog} role="dialog" aria-modal="true" aria-labelledby="task-close-title"
        onKeyDown={(event) => {
          if (event.key === "Escape" && !closing) { event.preventDefault(); setClosePrompt(false); }
          if (event.key === "Tab") {
            const buttons = [...event.currentTarget.querySelectorAll<HTMLButtonElement>("button:not(:disabled)")];
            const target = event.shiftKey ? buttons.at(-1) : buttons[0];
            const edge = event.shiftKey ? buttons[0] : buttons.at(-1);
            if (document.activeElement === edge || !buttons.includes(document.activeElement as HTMLButtonElement)) {
              event.preventDefault(); target?.focus();
            }
          }
        }}>
        <h2 id="task-close-title">{text("Keep research running?", "是否继续后台研究？", "是否繼續背景研究？")}</h2>
        <p>{text("Minimize keeps tasks running while the app is open. Exit stops local work and preserves progress and the queue; restart requires confirmation. A provider may still bill a request already sent.",
          "最小化后应用继续运行任务。退出会停止本地工作并保留进度和队列，重启后需确认才继续。已发送的远端请求仍可能计费。",
          "最小化後應用繼續執行任務。結束會停止本地工作並保留進度與佇列，重新啟動後需確認才繼續。已傳送的遠端請求仍可能計費。")}</p>
        {closeError && <p role="alert">{closeError}</p>}
        <div className="error-actions">
          <button type="button" disabled={closing} onClick={() => { void finishWindowClose("minimize").then(() => setClosePrompt(false)).catch(() => setCloseError(text("Could not minimize this window.", "未能最小化窗口。", "未能最小化視窗。"))); }}>{text("Minimize and continue", "最小化并继续", "最小化並繼續")}</button>
          <button type="button" disabled={closing} onClick={() => {
            setClosing(true); setCloseError("");
            void finishWindowClose("exit").catch(() => setCloseError(text("Work is still stopping. Please retry or keep the app open.", "任务仍在安全收尾，请稍后重试或保留应用。", "任務仍在安全收尾，請稍後重試或保留應用。"))).finally(() => setClosing(false));
          }}>{closing ? text("Saving progress…", "正在保存进度……", "正在儲存進度……") : text("Exit and preserve progress", "退出并保留进度", "結束並保留進度")}</button>
          <button type="button" disabled={closing} onClick={() => setClosePrompt(false)}>{text("Back to app", "返回应用", "返回應用")}</button>
        </div>
      </section></div>}
    </div>
  );
}
