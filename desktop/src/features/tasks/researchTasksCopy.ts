import type { Language } from "../../types";

export type ResearchTaskCopy = {
  title: string;
  close: string;
  openPage: string;
  continueQueue: string;
  queuePaused: string;
  loadMore: string;
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
    loadMore: "加载更多", loading: "正在读取任务…", empty: "暂无研究任务。", selectHint: "选择任务查看进度与可用操作。",
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
    loadMore: "載入更多", loading: "正在讀取任務…", empty: "目前沒有研究任務。", selectHint: "選擇任務以檢視進度與可用操作。",
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
    loadMore: "Load more", loading: "Loading tasks…", empty: "No research tasks yet.", selectHint: "Select a task to inspect progress and available actions.",
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

export function getResearchTaskCopy(language: Language): ResearchTaskCopy {
  return copy[language] ?? copy["zh-CN"];
}
