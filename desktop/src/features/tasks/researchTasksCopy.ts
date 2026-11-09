import type { Language } from "../../types";

export type ResearchTaskCopy = {
  title: string;
  close: string;
  openPage: string;
  continueQueue: string;
  queuePaused: string;
  loading: string;
  empty: string;
  selectHint: string;
  details: string;
  cancel: string;
  cancelling: string;
  resume: string;
  resumeWithModel: string;
  chooseModel: string;
  chooseModelHint: string;
  modelRequired: string;
  waitingReason: string;
  interruptedReason: string;
  outcomeTitle: string;
  attemptComplete: string;
  researchIncomplete: string;
  researchComplete: string;
  actionRequired: string;
  recoveryNextStep: string;
  diagnosticCode: string;
  openDisclosure: string;
  genericReason: string;
  remoteUnknown: string;
  reportRecovery: string;
  viewReport: string;
  archive: string;
  stage: string;
  elapsed: string;
  queuePosition: string;
  aheadCount: string;
  model: string;
  operation: string;
  status: string;
  error: string;
  more: string;
  visionPlanMissing: string;
  reasons: Record<string, string>;
  states: Record<string, string>;
  operations: Record<string, string>;
};

const copy: Record<Language, ResearchTaskCopy> = {
  "zh-CN": {
    title: "研究任务", close: "关闭任务面板", openPage: "全页查看", continueQueue: "继续队列", queuePaused: "研究队列已暂停。",
    loading: "正在读取任务…", empty: "暂无研究任务。", selectHint: "选择任务查看进度与可用操作。",
    details: "任务详情", cancel: "取消任务", cancelling: "正在取消…", resume: "继续任务",
    resumeWithModel: "更换模型并继续", chooseModel: "选择模型配置", chooseModelHint: "原模型配置已变化。请选择当前配置，再明确继续。",
    modelRequired: "先选择一个模型配置。", waitingReason: "等待处理原因", interruptedReason: "中断原因",
    outcomeTitle: "任务结果与下一步", attemptComplete: "本次处理已结束。", researchIncomplete: "研究尚未完成。请先核对报告中的缺失数据和证据，再决定后续操作。",
    researchComplete: "研究已完成。", actionRequired: "此任务仍需要您处理后续事项。",
    recoveryNextStep: "系统已保存恢复诊断。请先查看对应报告中的恢复信息；不会自动重启研究。", diagnosticCode: "诊断代码",
    openDisclosure: "打开官方披露页面", genericReason: "任务需要检查后才能继续。",
    remoteUnknown: "上次模型请求的远端结果未知，可能已经计费。不会自动重发；请到对应研究报告的恢复入口核对阶段后再决定。",
    reportRecovery: "打开报告恢复入口", viewReport: "查看研究报告", archive: "归档任务", stage: "阶段",
    elapsed: "已用时间", queuePosition: "队列位置", aheadCount: "前方任务", model: "模型", operation: "操作",
    status: "状态", error: "任务列表读取失败", more: "更多任务可加载。", visionPlanMissing: "授权计划信息缺失，暂不能提交此决定。",
    reasons: {
      MODEL_REMOTE_RESULT_UNKNOWN: "上一项模型请求的远端结果无法确认。请先核对报告阶段，不会自动重发。",
      REMOTE_OUTCOME_UNKNOWN: "上一项模型请求的远端结果无法确认。请先核对报告阶段，不会自动重发。",
      EXECUTION_INTERRUPTED: "应用停止时保留了已完成进度；核对后可继续。",
      LEGACY_REQUEST_UNAVAILABLE: "此旧任务缺少安全续作所需的请求快照，请通过报告恢复入口处理。",
      TASK_REQUEST_UNAVAILABLE: "此任务缺少可用的请求信息，请通过报告恢复入口处理。",
      MODEL_GATEWAY_CONFIGURATION_ERROR: "模型配置检查未能完成。请检查模型连接设置后再继续。",
      TASK_VISION_POLICY_CHANGED: "视觉识别设置已变化。请检查当前设置后再决定是否继续。",
      MODEL_CONFIGURATION_CHANGED: "模型配置已变化。请选择当前可用的模型配置后再继续。",
      NO_FILINGS_AVAILABLE: "目前没有可用的官方财务报告。请先核对官方披露来源。",
      FILING_DATA_QUALITY_FAILED: "财务数据未通过完整性校验。请查看报告中的缺失字段和证据。",
      FINANCIAL_DATA_QUALITY_FAILED: "财务数据尚未达到研究要求。请查看报告中的具体缺口。",
      FINANCIAL_CORE_COVERAGE_INSUFFICIENT: "核心财务数据覆盖不足。请查看报告中的未确认项目。",
    },
    states: { queued: "排队中", running: "运行中", waiting_input: "等待处理", interrupted: "已中断", cancelling: "正在取消", completed: "已完成", failed: "失败", cancelled: "已取消" },
    operations: { research: "研究", "financial-repair": "财报修复", financial_repair: "财报修复", financial_retry: "财报重试", financial_rebuild: "财报重建", model_recovery: "模型恢复", report_refresh: "报告刷新", revalidate_saved: "重新校验已保存数据", resume: "续作", continuation: "续作", "comprehensive-regeneration": "综合重生", comprehensive_regeneration: "综合重生", report_repair: "报告修复" },
  },
  "zh-Hant": {
    title: "研究任務", close: "關閉任務面板", openPage: "全頁檢視", continueQueue: "繼續佇列", queuePaused: "研究佇列已暫停。",
    loading: "正在讀取任務…", empty: "目前沒有研究任務。", selectHint: "選擇任務以檢視進度與可用操作。",
    details: "任務詳情", cancel: "取消任務", cancelling: "正在取消…", resume: "繼續任務",
    resumeWithModel: "更換模型並繼續", chooseModel: "選擇模型設定", chooseModelHint: "原模型設定已變更。請選擇目前設定，再明確繼續。",
    modelRequired: "請先選擇模型設定。", waitingReason: "等待處理原因", interruptedReason: "中斷原因",
    outcomeTitle: "任務結果與下一步", attemptComplete: "本次處理已結束。", researchIncomplete: "研究尚未完成。請先核對報告中的缺失資料與證據，再決定後續操作。",
    researchComplete: "研究已完成。", actionRequired: "此任務仍需要您處理後續事項。",
    recoveryNextStep: "系統已保存恢復診斷。請先查看對應報告中的恢復資訊；不會自動重新啟動研究。", diagnosticCode: "診斷代碼",
    openDisclosure: "開啟官方披露頁面", genericReason: "任務需要檢查後才能繼續。",
    remoteUnknown: "上次模型請求的遠端結果未知，可能已計費。不會自動重送；請前往對應研究報告的恢復入口核對階段，再決定是否繼續。",
    reportRecovery: "開啟報告恢復入口", viewReport: "檢視研究報告", archive: "封存任務", stage: "階段",
    elapsed: "已用時間", queuePosition: "佇列位置", aheadCount: "前方任務", model: "模型", operation: "操作",
    status: "狀態", error: "任務清單讀取失敗", more: "還有更多任務可載入。", visionPlanMissing: "缺少授權計畫資訊，暫時無法提交此決定。",
    reasons: {
      MODEL_REMOTE_RESULT_UNKNOWN: "上一項模型請求的遠端結果無法確認。請先核對報告階段，不會自動重送。",
      REMOTE_OUTCOME_UNKNOWN: "上一項模型請求的遠端結果無法確認。請先核對報告階段，不會自動重送。",
      EXECUTION_INTERRUPTED: "應用程式停止時保留了已完成進度；核對後可繼續。",
      LEGACY_REQUEST_UNAVAILABLE: "此舊任務缺少安全續作所需的請求快照，請透過報告恢復入口處理。",
      TASK_REQUEST_UNAVAILABLE: "此任務缺少可用的請求資訊，請透過報告恢復入口處理。",
      MODEL_GATEWAY_CONFIGURATION_ERROR: "模型設定檢查未能完成。請檢查模型連線設定後再繼續。",
      TASK_VISION_POLICY_CHANGED: "視覺辨識設定已變更。請檢查目前設定後再決定是否繼續。",
      MODEL_CONFIGURATION_CHANGED: "模型設定已變更。請選擇目前可用的模型設定後再繼續。",
      NO_FILINGS_AVAILABLE: "目前沒有可用的官方財務報告。請先核對官方披露來源。",
      FILING_DATA_QUALITY_FAILED: "財務資料未通過完整性檢查。請查看報告中的缺失欄位與證據。",
      FINANCIAL_DATA_QUALITY_FAILED: "財務資料尚未達到研究要求。請查看報告中的具體缺口。",
      FINANCIAL_CORE_COVERAGE_INSUFFICIENT: "核心財務資料涵蓋不足。請查看報告中的未確認項目。",
    },
    states: { queued: "排隊中", running: "執行中", waiting_input: "等待處理", interrupted: "已中斷", cancelling: "正在取消", completed: "已完成", failed: "失敗", cancelled: "已取消" },
    operations: { research: "研究", "financial-repair": "財報修復", financial_repair: "財報修復", financial_retry: "財報重試", financial_rebuild: "財報重建", model_recovery: "模型恢復", report_refresh: "報告重新整理", revalidate_saved: "重新驗證已儲存資料", resume: "續作", continuation: "續作", "comprehensive-regeneration": "綜合重生", comprehensive_regeneration: "綜合重生", report_repair: "報告修復" },
  },
  en: {
    title: "Research tasks", close: "Close task panel", openPage: "Open full page", continueQueue: "Resume queue", queuePaused: "The research queue is paused.",
    loading: "Loading tasks…", empty: "No research tasks yet.", selectHint: "Select a task to inspect progress and available actions.",
    details: "Task details", cancel: "Cancel task", cancelling: "Cancelling…", resume: "Continue task",
    resumeWithModel: "Change model and continue", chooseModel: "Choose model configuration", chooseModelHint: "The previous model configuration changed. Choose a current configuration before continuing.",
    modelRequired: "Choose a model configuration first.", waitingReason: "Waiting reason", interruptedReason: "Interruption reason",
    outcomeTitle: "Task outcome and next steps", attemptComplete: "This task attempt has ended.", researchIncomplete: "The research is not complete. Review the report's missing data and evidence before deciding what to do next.",
    researchComplete: "The research is complete.", actionRequired: "Further action is required for this task.",
    recoveryNextStep: "Recovery diagnostics were saved. Review the recovery details in the associated report first; research will not restart automatically.", diagnosticCode: "Diagnostic code",
    openDisclosure: "Open official disclosure page", genericReason: "This task needs review before it can continue.",
    remoteUnknown: "The previous model request has an unknown remote outcome and may have been billed. It will not be resent automatically; inspect the stage in the recovery entry for this report before deciding.",
    reportRecovery: "Open report recovery", viewReport: "View research report", archive: "Archive task", stage: "Stage",
    elapsed: "Elapsed", queuePosition: "Queue position", aheadCount: "Tasks ahead", model: "Model", operation: "Operation",
    status: "Status", error: "Could not load the task list", more: "More tasks are available.", visionPlanMissing: "The approval plan is missing, so this decision cannot be submitted.",
    reasons: {
      MODEL_REMOTE_RESULT_UNKNOWN: "The previous model request has an unconfirmed remote outcome. Check the report stage first; it will not be resent automatically.",
      REMOTE_OUTCOME_UNKNOWN: "The previous model request has an unconfirmed remote outcome. Check the report stage first; it will not be resent automatically.",
      EXECUTION_INTERRUPTED: "Completed progress was preserved when the app stopped; review it before continuing.",
      LEGACY_REQUEST_UNAVAILABLE: "This older task has no request snapshot for safe continuation. Use the report recovery entry.",
      TASK_REQUEST_UNAVAILABLE: "This task has no available request details. Use the report recovery entry.",
      MODEL_GATEWAY_CONFIGURATION_ERROR: "The model configuration could not be checked. Review the model connection before continuing.",
      TASK_VISION_POLICY_CHANGED: "Vision settings have changed. Review the current settings before deciding whether to continue.",
      MODEL_CONFIGURATION_CHANGED: "The model configuration has changed. Choose an available current configuration before continuing.",
      NO_FILINGS_AVAILABLE: "No usable official financial reports are currently available. Check the official disclosure source first.",
      FILING_DATA_QUALITY_FAILED: "Financial data did not pass completeness checks. Review the missing fields and evidence in the report.",
      FINANCIAL_DATA_QUALITY_FAILED: "Financial data does not yet meet the research requirements. Review the specific gaps in the report.",
      FINANCIAL_CORE_COVERAGE_INSUFFICIENT: "Core financial-data coverage is insufficient. Review the unverified items in the report.",
    },
    states: { queued: "Queued", running: "Running", waiting_input: "Waiting for input", interrupted: "Interrupted", cancelling: "Cancelling", completed: "Completed", failed: "Failed", cancelled: "Cancelled" },
    operations: { research: "Research", "financial-repair": "Financial repair", financial_repair: "Financial repair", financial_retry: "Financial retry", financial_rebuild: "Financial rebuild", model_recovery: "Model recovery", report_refresh: "Report refresh", revalidate_saved: "Revalidate saved data", resume: "Continuation", continuation: "Continuation", "comprehensive-regeneration": "Full regeneration", comprehensive_regeneration: "Full regeneration", report_repair: "Report repair" },
  },
};

const experience = {
  en: {
    quick: "Task summary", allTasks: "All tasks", attention: "Needs attention", notifications: "Task notifications", closeNotice: "Dismiss notification",
    filtersLabel: "Filter tasks", filters: { active: "In progress", attention: "Needs attention", ended: "Ended", all: "All" },
    tabs: { overview: "Overview", preparation: "Preparation", models: "Model research", diagnostics: "Diagnostics" },
    backToList: "Back to task list", start: "Start research", backToReport: "Return to report",
    viewTask: "View task", viewSaved: "View saved research", attemptEnded: "Attempt ended", partialResult: "Partial result",
    noMatching: "No tasks match this filter.", outsideFilter: "This task is no longer in the selected filter. Its details are still available here.",
    latestActivity: "Last recorded activity", noActivity: "No recent activity reported", awaitingResponse: "Waiting for a model response",
    materials: "Filings processed", modelStages: "Model stages completed", noProgress: "No progress recorded yet",
    queuedAt: "Submitted", startedAt: "Started", finishedAt: "Ended",
    nextStep: "Next step", queueHint: "This task will start when the queue reaches it.", activeHint: "You can leave this page while the task runs.",
    cancellingHint: "Local work is stopping. An already submitted request may still be billed.",
    noSavedResult: "No saved research result is available for this attempt.", actionBusy: "Applying task action…",
    actionFailed: "The task action did not complete. Review the refreshed task before trying again.",
    revisionConflict: "The task changed. Review the current details before trying again.",
    retryList: "Reload tasks", reloadDetail: "Reload task details",
    pagination: "Task pages", previousPage: "Previous page", nextPage: "Next page", pageNumber: "Page {n}",
    emptyPage: "No tasks remain on this page. Return to the previous page.",
    useVisionSettings: "Use current vision settings and continue", configureModels: "Open Model Center",
    submitted: "Research submitted", submitFailed: "Research could not be submitted. Your draft is unchanged.",
    queuedWithAhead: "Added to queue · {count} ahead", clearDraft: "Clear draft",
    vision: { title: "Vision upload approval", provider: "Provider", document: "Document", pages: "Pages", size: "Size", fingerprint: "Fingerprint",
      approve: "Approve upload", decline: "Do not upload", notice: "Only these pages will be sent to the listed provider if you approve." },
    detailsUnavailable: "Task details are unavailable.",
  },
  "zh-CN": {
    quick: "任务摘要", allTasks: "全部任务", attention: "需处理", notifications: "任务通知", closeNotice: "关闭通知",
    filtersLabel: "筛选任务", filters: { active: "进行中", attention: "需处理", ended: "已结束", all: "全部" },
    tabs: { overview: "概览", preparation: "资料准备", models: "模型研究", diagnostics: "诊断" },
    backToList: "返回任务列表", start: "发起研究", backToReport: "返回报告",
    viewTask: "查看任务", viewSaved: "查看已有研究", attemptEnded: "本次处理已结束", partialResult: "阶段性结果",
    noMatching: "当前筛选没有任务。", outsideFilter: "此任务已移出当前筛选，仍可在这里查看详情。",
    latestActivity: "最近实际活动", noActivity: "尚未记录最近活动", awaitingResponse: "等待模型响应",
    materials: "已处理财报", modelStages: "已完成模型阶段", noProgress: "暂未记录进度",
    queuedAt: "提交时间", startedAt: "开始时间", finishedAt: "结束时间",
    nextStep: "下一步", queueHint: "轮到此任务时将自动开始。", activeHint: "可离开本页，任务会在后台继续。",
    cancellingHint: "正在停止本地工作。已提交的远端请求仍可能计费。",
    noSavedResult: "本次处理尚无已保存的研究结果。", actionBusy: "正在处理任务操作……",
    actionFailed: "操作未完成。请核对刷新后的任务，再决定是否重试。",
    revisionConflict: "任务状态已变化。请查看当前详情后再操作。",
    retryList: "重新读取任务", reloadDetail: "重新读取详情",
    pagination: "任务列表分页", previousPage: "上一页", nextPage: "下一页", pageNumber: "第 {n} 页",
    emptyPage: "当前页已无任务，请返回上一页。",
    useVisionSettings: "使用当前视觉设置并继续", configureModels: "打开模型中心",
    submitted: "研究已提交", submitFailed: "提交未完成，草稿已保留。",
    queuedWithAhead: "已加入队列，前面有 {count} 个任务", clearDraft: "清空草稿",
    vision: { title: "视觉上传授权", provider: "供应商", document: "文件", pages: "页面", size: "大小", fingerprint: "指纹",
      approve: "同意上传", decline: "拒绝上传", notice: "只有您同意后，才会将所列页面发送给此供应商。" },
    detailsUnavailable: "暂时无法读取任务详情。",
  },
  "zh-Hant": {
    quick: "任務摘要", allTasks: "全部任務", attention: "需處理", notifications: "任務通知", closeNotice: "關閉通知",
    filtersLabel: "篩選任務", filters: { active: "進行中", attention: "需處理", ended: "已結束", all: "全部" },
    tabs: { overview: "概覽", preparation: "資料準備", models: "模型研究", diagnostics: "診斷" },
    backToList: "返回任務清單", start: "發起研究", backToReport: "返回報告",
    viewTask: "檢視任務", viewSaved: "檢視已有研究", attemptEnded: "本次處理已結束", partialResult: "階段性結果",
    noMatching: "目前篩選沒有任務。", outsideFilter: "此任務已移出目前篩選，仍可在這裡檢視詳情。",
    latestActivity: "最近實際活動", noActivity: "尚未記錄最近活動", awaitingResponse: "等待模型回應",
    materials: "已處理財報", modelStages: "已完成模型階段", noProgress: "暫未記錄進度",
    queuedAt: "提交時間", startedAt: "開始時間", finishedAt: "結束時間",
    nextStep: "下一步", queueHint: "輪到此任務時將自動開始。", activeHint: "可離開本頁，任務會在背景繼續。",
    cancellingHint: "正在停止本地工作。已提交的遠端請求仍可能計費。",
    noSavedResult: "本次處理尚無已儲存的研究結果。", actionBusy: "正在處理任務操作……",
    actionFailed: "操作未完成。請核對重新整理後的任務，再決定是否重試。",
    revisionConflict: "任務狀態已變更。請檢視目前詳情後再操作。",
    retryList: "重新讀取任務", reloadDetail: "重新讀取詳情",
    pagination: "任務清單分頁", previousPage: "上一頁", nextPage: "下一頁", pageNumber: "第 {n} 頁",
    emptyPage: "目前頁面已無任務，請返回上一頁。",
    useVisionSettings: "使用目前視覺設定並繼續", configureModels: "開啟模型中心",
    submitted: "研究已提交", submitFailed: "提交未完成，草稿已保留。",
    queuedWithAhead: "已加入佇列，前面有 {count} 個任務", clearDraft: "清空草稿",
    vision: { title: "視覺上傳授權", provider: "供應商", document: "文件", pages: "頁面", size: "大小", fingerprint: "指紋",
      approve: "同意上傳", decline: "拒絕上傳", notice: "只有您同意後，才會將所列頁面傳送給此供應商。" },
    detailsUnavailable: "暫時無法讀取任務詳情。",
  },
} as const;

export function getResearchTaskCopy(language: Language) {
  return { ...copy[language], ...experience[language] };
}
