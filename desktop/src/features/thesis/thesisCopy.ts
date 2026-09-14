import type { ThesisCopy } from "./ThesisView";
import type { Language } from '../../types';

export type ThesisWorkbenchCopy = {
  coreThesis: string;
  keyAssumptions: string;
  supportingEvidence: string;
  counterEvidence: string;
  invalidationConditions: string;
  leadingIndicators: string;
  sourceIdsHint: string;
  changeReason: string;
  changeReasonHint: string;
  versionDiff: string;
  noChanges: string;
  added: string;
  removed: string;
  changed: string;
  previousVersion: string;
  currentVersion: string;
  reasonPrefix: string;
  loadFailed: string;
  fetchVersionFailed: string;
  baselineFailed: string;
  validationFailed: string;
  saveFailed: string;
  dirtyWarning: string;
  discardChanges: string;
  keepEditing: string;
  evidenceSource: string;
  legacyDataNote: string;
};

const EN: ThesisWorkbenchCopy = {
  coreThesis: "Core thesis",
  keyAssumptions: "Key assumptions",
  supportingEvidence: "Supporting evidence",
  counterEvidence: "Counter evidence",
  invalidationConditions: "Invalidation conditions",
  leadingIndicators: "Leading indicators",
  sourceIdsHint: "One source ID per line. Existing evidence values remain intact until you edit this field.",
  changeReason: "Reason for change",
  changeReasonHint: "Required for every new version. The reason is stored exactly as entered.",
  versionDiff: "Version diff",
  noChanges: "No field changes between these versions.",
  added: "Added",
  removed: "Removed",
  changed: "Changed",
  previousVersion: "Previous version",
  currentVersion: "Current version",
  reasonPrefix: "Reason",
  loadFailed: "Could not load thesis versions.",
  fetchVersionFailed: "Could not load this thesis version.",
  baselineFailed: "Previous version sources could not be loaded; evidence comparison is incomplete.",
  validationFailed: "Please provide a reason for the changes before saving.",
  saveFailed: "Could not save the thesis. Your edits are still here.",
  dirtyWarning: "You have unsaved changes. Choose whether to discard them before opening another version.",
  discardChanges: "Discard changes",
  keepEditing: "Keep editing",
  evidenceSource: "Source IDs",
  legacyDataNote: "Legacy fields and mixed array entries are retained when you edit this version.",
};

const ZH_CN: ThesisWorkbenchCopy = {
  coreThesis: "核心判断",
  keyAssumptions: "关键假设",
  supportingEvidence: "支持证据",
  counterEvidence: "反方证据",
  invalidationConditions: "逻辑失效条件",
  leadingIndicators: "领先指标",
  sourceIdsHint: "每行一个来源 ID。未编辑此字段时，历史证据值会原样保留。",
  changeReason: "本次修改原因",
  changeReasonHint: "每个新版本都必须填写；系统会按原文保存。",
  versionDiff: "版本差异",
  noChanges: "两个版本之间没有字段变化。",
  added: "新增",
  removed: "移除",
  changed: "变更",
  previousVersion: "上一版本",
  currentVersion: "当前版本",
  reasonPrefix: "原因",
  loadFailed: "投资逻辑版本加载失败。",
  fetchVersionFailed: "此投资逻辑版本加载失败。",
  baselineFailed: "上一版本的来源加载失败，证据对比尚不完整。",
  validationFailed: "保存前请填写本次修改原因。",
  saveFailed: "投资逻辑保存失败；你的编辑仍然保留。",
  dirtyWarning: "当前有未保存修改。打开其他版本前，请选择是否放弃这些修改。",
  discardChanges: "放弃修改",
  keepEditing: "继续编辑",
  evidenceSource: "来源 ID",
  legacyDataNote: "编辑此版本时会保留历史字段和异构数组项。",
};

const ZH_HANT: ThesisWorkbenchCopy = {
  coreThesis: "核心判斷",
  keyAssumptions: "關鍵假設",
  supportingEvidence: "支持證據",
  counterEvidence: "反方證據",
  invalidationConditions: "邏輯失效條件",
  leadingIndicators: "領先指標",
  sourceIdsHint: "每行一個來源 ID。未編輯此欄位時，歷史證據值會原樣保留。",
  changeReason: "本次修改原因",
  changeReasonHint: "每個新版本都必須填寫；系統會按原文儲存。",
  versionDiff: "版本差異",
  noChanges: "兩個版本之間沒有欄位變化。",
  added: "新增",
  removed: "移除",
  changed: "變更",
  previousVersion: "上一版本",
  currentVersion: "目前版本",
  reasonPrefix: "原因",
  loadFailed: "投資邏輯版本載入失敗。",
  fetchVersionFailed: "此投資邏輯版本載入失敗。",
  baselineFailed: "上一版本的來源載入失敗，證據對比尚不完整。",
  validationFailed: "儲存前請填寫本次修改原因。",
  saveFailed: "投資邏輯儲存失敗；你的編輯仍然保留。",
  dirtyWarning: "目前有未儲存修改。開啟其他版本前，請選擇是否放棄這些修改。",
  discardChanges: "放棄修改",
  keepEditing: "繼續編輯",
  evidenceSource: "來源 ID",
  legacyDataNote: "編輯此版本時會保留歷史欄位和異構陣列項。",
};

export function getThesisWorkbenchCopy(copy: ThesisCopy, language?: Language): ThesisWorkbenchCopy {
  if (language) return language === 'en' ? EN : language === 'zh-Hant' ? ZH_HANT : ZH_CN;
  if (/Investment|English|thesis/i.test(copy.thesesTitle)) return EN;
  if (/邏輯/.test(copy.thesesTitle)) return ZH_HANT;
  return ZH_CN;
}
