import { Activity, useEffect, useLayoutEffect, useMemo, useRef, useState, type MouseEvent } from "react";
import {
  BookOpenText,
  Boxes,
  CircleHelp,
  Code2,
  ChevronLeft,
  CircleAlert,
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
import { finishWindowClose, listenForWindowClose, listConfiguredModels, listResearchTasks } from "./backend";
import { ResearchTaskCenter } from "./features/tasks/ResearchTaskCenter";
import type { TaskDetailActions, TaskModelChoice } from "./features/tasks/TaskDetail";
import { TaskQuickPanel } from "./features/tasks/TaskQuickPanel";
import { TaskNotices, type TaskNotice } from "./features/tasks/TaskNotices";
import { getResearchTaskCopy } from "./features/tasks/researchTasksCopy";
import { matchesTaskFilter, reportAction, taskFilterFor, taskStatus, type TaskFilter } from "./features/tasks/taskPresentation";
import { CoreStartup } from "./components/CoreStartup";
import { AboutView } from "./features/about/AboutView";
import { HelpView } from "./features/help/HelpView";
import { HistoryView } from "./features/history/HistoryView";
import { ModelCenterView } from "./features/models/ModelCenterView";
import { OtStudioView } from "./features/ot-studio/OtStudioView";
import { ReportWorkspace } from "./features/report/ReportWorkspace";
import { NewResearchView } from "./features/research/NewResearchView";
import { SettingsView } from "./features/settings/SettingsView";
import { ThesisView } from "./features/thesis/ThesisView";
import { COPY } from "./i18n";
import { resolveUiLanguage, setDocumentLanguage } from "./languageRegistry";
import { modelEligibility } from "./modelEligibility";
import type {
  Language,
  ResearchJob,
  ResearchRequest,
  ResearchTaskSummary,
} from "./types";

type ViewId = "workspace" | "new-research" | "history" | "theses" | "models" | "ot-studio" | "settings" | "help" | "about" | "tasks";

export default function App() {
  const [activeView, setActiveView] = useState<ViewId>("workspace");
  const [quickOpen, setQuickOpen] = useState(false);
  const [taskModels, setTaskModels] = useState<TaskModelChoice[]>([]);
  const [submission, setSubmission] = useState<ResearchJob | "failed" | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const submittingRef = useRef(false);
  const [draftOpened, setDraftOpened] = useState(false);
  const [openDetailToken, setOpenDetailToken] = useState(0);
  const stageElement = useRef<HTMLElement>(null);
  const scrollPositions = useRef(new Map<string, number>());
  const [closePrompt, setClosePrompt] = useState(false);
  const [closing, setClosing] = useState(false);
  const [closeError, setCloseError] = useState("");
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
  } = useWorkbenchSession({ catalogVisible: activeView === "tasks", detailVisible: activeView === "tasks", quickVisible: quickOpen });
  const storageRecoveryRequired = error?.kind === "storage-recovery-required";
  const taskCopy = getResearchTaskCopy(language);
  const text = (en: string, zh: string, hant: string = zh) => language === "en" ? en : language === "zh-Hant" ? hant : zh;
  const taskCount = Object.values(tasks.counts).reduce((sum, value) => sum + value, 0);
  const attentionCount = tasks.counts.waiting_input + tasks.counts.interrupted;
  const runningCount = tasks.counts.running + tasks.counts.cancelling;
  const viewScrollKey = activeView === "workspace" && report ? `report:${report.run_id}` : activeView;
  const knownTasks = useMemo(() => new Map(tasks.items.map((item) => [item.job_id, item])), [tasks.items]);
  const catalog = {
    ...tasks.catalog,
    items: tasks.catalog.items.map((item) => {
      const current = knownTasks.get(item.job_id);
      return current && current.revision >= item.revision ? { ...item, ...current } : item;
    }),
    error: tasks.catalog.error || tasks.error,
  };
  useLayoutEffect(() => {
    if (stageElement.current) stageElement.current.scrollTop = scrollPositions.current.get(viewScrollKey) ?? 0;
  }, [viewScrollKey, Boolean(bootstrap)]);

  useEffect(() => {
    let active = true;
    const registered = listenForWindowClose(() => {
      void listResearchTasks({ limit: 1 }).then((page) => {
        if (!active) return;
        if (Object.values(page.counts).some((count) => count > 0)) { setQuickOpen(false); setClosePrompt(true); }
        else void finishWindowClose("exit").catch(() => setClosePrompt(true));
      }).catch(() => { if (active) setClosePrompt(true); });
    }).catch(() => undefined);
    return () => { active = false; void registered.then((unlisten) => unlisten?.()); };
  }, []);

  useEffect(() => {
    if (activeView !== "tasks") return;
    let active = true;
    void listConfiguredModels().then((models) => {
      if (active) setTaskModels(models.filter((model) => modelEligibility(model, "text_chat").selectable));
    }).catch(() => { if (active) setTaskModels([]); });
    return () => { active = false; };
  }, [activeView]);

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
    setQuickOpen(false);
    setActiveView("workspace");
    await selectRun(run);
  };

  const startNewResearch = async (request: ResearchRequest = { mode: "demo" }) => {
    if (submittingRef.current) return;
    submittingRef.current = true;
    setSubmitting(true);
    try {
      const accepted = await beginResearch(request);
      if (accepted) setSubmission(accepted);
    } finally {
      submittingRef.current = false;
      setSubmitting(false);
    }
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
  const activeReportTask = report && tasks.items.find((task) => task.run_id === report.run_id && !matchesTaskFilter(task.state, "ended"));
  const reportActionProps = storageRecoveryRequired ? { suppressResearchActions: true }
    : activeReportTask ? {
      suppressResearchActions: !matchesTaskFilter(activeReportTask.state, "attention"),
      taskAction: { label: taskCopy.viewTask, open: () => openTask(activeReportTask.job_id, activeReportTask.state) },
      // The task owns its generic resume and approvals. An explicit
      // stage-level recovery with the report's remote-billing warning is
      // still available when the durable plan permits it.
      onRetryRecoveryStage: retryRecoveryStage,
      onRevalidateSaved: revalidateSaved,
      onConfigureCloud: () => navigateTo("models"),
    } : {
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
    setQuickOpen(false);
    if (id === "new-research") setDraftOpened(true);
    setActiveView(id);
    if (!drawerPinned) setDrawerHovered(false);
  };

  const openTask = (id: string, state?: ResearchJob["state"]) => {
    const targetState = knownTasks.get(id)?.state ?? state;
    if (targetState) tasks.catalog.selectFilter(taskFilterFor(targetState));
    tasks.selectTask(id);
    setOpenDetailToken((token) => token + 1);
    navigateTo("tasks");
  };
  const openTaskResult = (task: Pick<ResearchJob, "run_id" | "company">) => {
    if (!task.run_id) return;
    void openRun({ run_id: task.run_id, company_name: task.company?.name ?? "", ticker: task.company?.ticker ?? "" } as Parameters<typeof selectRun>[0]);
  };
  const taskActions: TaskDetailActions = {
    cancel: (id) => { if (!storageRecoveryRequired) void tasks.cancelTask(id); },
    resume: (id, revision, model, vision) => { if (!storageRecoveryRequired) void tasks.resumeTask(id, revision, model, vision); },
    archive: (id, revision) => { if (!storageRecoveryRequired) void tasks.archiveTask(id, revision); },
    openResult: openTaskResult,
    cancelModel: (id, request, attempt) => { if (!storageRecoveryRequired) void tasks.cancelTaskModel(id, request, attempt); },
    decideVision: (id, revision, plan, approved) => { if (!storageRecoveryRequired) void tasks.decideTaskVision(id, revision, plan, approved); },
    approveBudget: async (job, digest, gaps, choice) => { if (!storageRecoveryRequired) await tasks.approveBudget(job, digest, gaps, choice); },
    configureModels: () => navigateTo("models"),
    loadCallHistory: loadResearchCallHistory,
  };
  const activeCommand = tasks.selectedTaskId ? tasks.commands[tasks.selectedTaskId] : undefined;
  const notices: TaskNotice[] = tasks.notifications.map((notice) => {
    const resultLabel = reportAction(notice, language);
    return {
      id: notice.id, text: `${notice.company.name || notice.company.ticker} · ${taskStatus(notice, language)}`,
      attention: notice.state === "failed",
      actions: [
        { label: taskCopy.viewTask, run: () => { tasks.dismissNotification(notice.id); openTask(notice.job_id, notice.state); } },
        ...(resultLabel ? [{ label: resultLabel, run: () => { tasks.dismissNotification(notice.id); openTaskResult(notice); } }] : []),
      ],
      dismiss: () => tasks.dismissNotification(notice.id),
    };
  });
  if (submission === "failed") notices.push({ id: "submission-failed", text: taskCopy.submitFailed, attention: true, dismiss: () => setSubmission(null) });
  else if (submission) {
    const target = submission;
    notices.unshift({
      id: `submitted:${target.job_id}`,
      text: target.state === "queued" && (target.ahead_count ?? 0) > 0
        ? taskCopy.queuedWithAhead.replace("{count}", String(target.ahead_count)) : taskCopy.submitted,
      actions: [{ label: taskCopy.viewTask, run: () => { setSubmission(null); openTask(target.job_id, target.state); } }],
      dismiss: () => setSubmission(null),
    });
  }
  const showTasks = (filter?: TaskFilter) => { if (filter) tasks.catalog.selectFilter(filter); navigateTo("tasks"); };

  // Do not expose localized navigation before bootstrap has resolved the
  // persisted manual preference.  The neutral shell is intentionally free of
  // language-specific labels; bootstrap failures fall through to the normal
  // error-visible application shell below.
  if (!bootstrap && !error) {
    return <CoreStartup language={language} />;
  }

  return (
    <div className="app-shell" data-nav-pinned={drawerPinned || undefined} data-skip-motion={skipMotion || undefined}>
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
                aria-current={activeView === id ? "page" : undefined}
                aria-label={id === "tasks" ? `${label} · ${taskCopy.states.running} ${runningCount} · ${taskCopy.states.queued} ${tasks.counts.queued} · ${taskCopy.attention} ${attentionCount}` : label}
                title={drawerOpen ? undefined : label} onClick={() => navigateTo(id)}>
                <Icon size={20} strokeWidth={1.8} />
                {id === "tasks" && taskCount > 0 && <span className="task-rail-count" data-attention={attentionCount > 0 || undefined} aria-hidden="true">
                  {attentionCount > 0 && <CircleAlert size={12} />}{taskCount}
                </span>}
              </button>
            ))}
          </nav>
        </div>

        <div className="nav-drawer" data-open={drawerOpen || undefined} aria-hidden={!drawerOpen} inert={!drawerOpen}>
          <div className="drawer-brand"><strong>OpenThesis</strong></div>
          <button className="drawer-pin" type="button" onClick={handleDrawerToggle}>
            <span>{drawerPinned ? copy.toggleClose : copy.toggleOpen}</span><ChevronLeft size={16} />
          </button>
          <nav className="drawer-actions" aria-label={copy.navigationLabels}>
            {navItems.map(({ id, label }) => (
              <button key={id} type="button" data-active={activeView === id || undefined} aria-current={activeView === id ? "page" : undefined}
                onClick={() => navigateTo(id)}>{label}{id === "tasks" && taskCount > 0 && <small className="task-nav-counts">
                  {attentionCount > 0 ? `${taskCopy.attention} ${attentionCount} · ` : ""}{taskCopy.states.running} {runningCount} · {taskCopy.states.queued} {tasks.counts.queued}
                </small>}</button>
            ))}
          </nav>
          <footer className="drawer-footer"><Languages size={16} /><span>{copy.currentLanguage}</span><small>v{bootstrap?.app_version ?? "1.0"}</small></footer>
        </div>
      </aside>

      <main className="workspace" data-view={activeView}>
        <header className="workspace-header">
          <div><span className="eyebrow"><Sparkles size={14} /> OpenThesis</span><h1>{pageTitle[activeView]}</h1></div>
          <div className="workspace-header-actions">
            {activeView === "tasks" && report && <button type="button" className="task-button task-return-report" onClick={() => navigateTo("workspace")}>
              <BookOpenText size={15} />{taskCopy.backToReport}
            </button>}
            {bootstrap && <TaskQuickPanel language={language} counts={tasks.counts} items={tasks.items}
              open={quickOpen} onToggle={setQuickOpen} onOpenTask={(task: ResearchTaskSummary) => openTask(task.job_id, task.state)}
              onOpenAll={(filter) => showTasks(filter ?? "all")} />}
            {activeView === "tasks" && bootstrap && <button type="button" className="task-button task-button--primary" onClick={() => navigateTo("new-research")}>
              <Plus size={16} />{taskCopy.start}
            </button>}
            {activeView !== "tasks" && <div className="status-cluster"><span className="status-dot" /><span>{bootstrap ? `Core ${bootstrap.contract_version}` : copy.loading}</span></div>}
          </div>
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
        {bootstrap && bootstrap.interrupted_runs > 0 && tasks.counts.interrupted === 0 && activeView === "history" && (
          <div className="recovery-banner" role="status">
            {copy.interruptedRecovery.replace("{count}", String(bootstrap.interrupted_runs))}
          </div>
        )}
        <section ref={stageElement} className={`report-stage${activeView === "models" ? " model-center-stage" : activeView === "tasks" ? " task-stage" : ""}`}
          aria-label={activeView === "tasks" ? taskCopy.allTasks : copy.report}
          onScroll={(event) => { if (activeView !== "tasks") scrollPositions.current.set(viewScrollKey, event.currentTarget.scrollTop); }}>
          <Activity mode={activeView === "tasks" ? "visible" : "hidden"}>
            <ResearchTaskCenter language={language} catalog={catalog} counts={tasks.counts} queuePaused={tasks.queuePaused}
              queuePending={Boolean(tasks.commands.queue?.pending || storageRecoveryRequired)} queueError={tasks.commands.queue?.error ?? ""}
              onContinueQueue={() => { if (!storageRecoveryRequired) void tasks.continueQueue(); }}
              selectedTaskId={tasks.selectedTaskId} selectedSummary={tasks.selectedSummary} detail={tasks.detail}
              detailError={tasks.detailError} onReloadDetail={tasks.refreshDetail} onSelect={tasks.selectTask}
              actionPending={Boolean(activeCommand?.pending || storageRecoveryRequired)} actionError={activeCommand?.error ?? ""}
              openDetailToken={openDetailToken} actions={taskActions} models={taskModels} />
          </Activity>
          {draftOpened && bootstrap && !storageRecoveryRequired && <Activity mode={activeView === "new-research" ? "visible" : "hidden"}>
            <NewResearchView bootstrap={bootstrap} copy={copy} activeResearchTasks={tasks.items}
              onOpenTask={openTask} onOpenModelCenter={() => navigateTo("models")} onOpenVisionSettings={() => navigateTo("settings")}
              onSavePreferences={savePreferences} onStart={startNewResearch} />
          </Activity>}
          {activeView === "tasks" ? null : activeView === "settings" && bootstrap ? (
            <SettingsView language={language} preferences={bootstrap.preferences} copy={copy} onSave={savePreferences} />
          ) : activeView === "models" ? (
            <ModelCenterView language={language} />
          ) : activeView === "ot-studio" ? (
            <OtStudioView language={language} onOpenModelCenter={() => setActiveView("models")} />
          ) : activeView === "new-research" && storageRecoveryRequired ? (
            storageRecoveryPanel
          ) : activeView === "new-research" && bootstrap ? null : activeView === "history" && bootstrap ? (
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
            <EmptyState title={copy.emptyTitle} body={copy.emptyBody} demoAction={copy.startDemo} demoPending={submitting}
              realAction={copy.startReal} hint={copy.demoHint}
              onDemo={() => void startNewResearch().catch(() => setSubmission("failed"))} onReal={() => navigateTo("new-research")} />
          )}
        </section>
      </main>
      <TaskNotices notices={notices} language={language} />
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
        <div className="action-row task-close-actions">
          <button className="primary-button" type="button" disabled={closing} onClick={() => { void finishWindowClose("minimize").then(() => setClosePrompt(false)).catch(() => setCloseError(text("Could not minimize this window.", "未能最小化窗口。", "未能最小化視窗。"))); }}>{text("Minimize and continue", "最小化并继续", "最小化並繼續")}</button>
          <button className="secondary-button" type="button" disabled={closing} onClick={() => {
            setClosing(true); setCloseError("");
            void finishWindowClose("exit").catch(() => setCloseError(text("Work is still stopping. Please retry or keep the app open.", "任务仍在安全收尾，请稍后重试或保留应用。", "任務仍在安全收尾，請稍後重試或保留應用。"))).finally(() => setClosing(false));
          }}>{closing ? text("Saving progress…", "正在保存进度……", "正在儲存進度……") : text("Exit and preserve progress", "退出并保留进度", "結束並保留進度")}</button>
          <button className="secondary-button" type="button" disabled={closing} onClick={() => setClosePrompt(false)}>{text("Back to app", "返回应用", "返回應用")}</button>
        </div>
      </section></div>}
    </div>
  );
}
