import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import type { CSSProperties } from "react";
import { Bot, Braces, Check, CircleHelp, Code2, Download, Redo2, ShieldCheck, SlidersHorizontal, Sparkles, Undo2, X } from "lucide-react";

import { compileOtDraft, exportOtPackage, listConfiguredModels, suggestOtPatch, validateOtDraft } from "../../backend";
import type { ConfiguredModelSummary, Language, OtDiagnostic, OtDraft, OtSuggestion } from "../../types";

type Mode = "guided" | "professional";

const EN = {
  eyebrow: "OT Studio", title: "Build a reusable research package",
  intro: "Describe the research you want, tune the guardrails, and export a deterministic .ot package. Model suggestions never change the draft until you accept the diff.",
  guided: "Guided", professional: "Professional", undo: "Undo", redo: "Redo",
  intent: "Research intent", goal: "What should this package investigate?",
  goalHint: "For example: evaluate durable growth, accounting quality, and downside risk over five years.",
  applyGoal: "Use as description", name: "Package name", packageId: "Package ID", version: "Version",
  horizon: "Horizon", years: "years", depth: "Analysis depth", risk: "Risk emphasis",
  helpLabel: "About", horizonHelp: "Sets the time span emphasized by research and scenario analysis. It does not decide how many years of filings are downloaded; available evidence and the .ot package still bound the data range.",
  depthHelp: "Sets the breadth, detail, and instruction intensity of the analysis. Higher values may use more time and model tokens, but they never create missing evidence.",
  riskHelp: "Sets how strongly risk, counter-arguments, and uncertainty are emphasized during synthesis. It does not change source facts or guarantee a more conservative or accurate conclusion.",
  workflow: "Workflow", role: "Role", schema: "Output schema", prompt: "Bounded instruction",
  outputs: "Outputs", evidence: "Include traceable evidence", raw: "Raw draft JSON", applyJson: "Apply JSON",
  assistant: "Field assistant",
  assistantBody: "Choose one field. The model receives that field, your instruction, and only the minimum draft context needed for a bounded suggestion.",
  noModel: "No tested assistant model is configured. Manual editing and export remain available.",
  modelCenter: "Open Model Center", model: "Model", field: "Field to improve", instruction: "Instruction",
  fieldPackageName: "Package name", fieldPackageDescription: "Package description",
  fieldFinancialPrompt: "Financial analysis prompt", fieldRiskPrompt: "Risk review prompt",
  instructionHint: "Make this instruction explicit about evidence and missing values.",
  suggest: "Generate suggestion", suggesting: "Generating…", before: "Before", after: "Suggested",
  accept: "Accept change", reject: "Reject", validate: "Validate", validating: "Validating…",
  export: "Check and export", exporting: "Compiling…", valid: "Draft is valid",
  invalid: "Resolve the diagnostics before export", free: "Free", identity: "Content identity",
};
type StudioText = { [Key in keyof typeof EN]: string };
const ZH: StudioText = {
  eyebrow: ".ot 创作工作室", title: "构建可复用的研究包",
  intro: "描述研究目标、调整约束，并导出确定性的 .ot 包。模型建议只有在你查看差异并明确接受后才会修改草稿。",
  guided: "引导模式", professional: "专业模式", undo: "撤销", redo: "重做",
  intent: "研究意图", goal: "这个研究包应该研究什么？",
  goalHint: "例如：从五年视角评估增长质量、会计质量和下行风险。",
  applyGoal: "用作项目说明", name: "研究包名称", packageId: "研究包 ID", version: "版本",
  horizon: "研究周期", years: "年", depth: "分析深度", risk: "风险权重",
  helpLabel: "了解", horizonHelp: "控制研究与情景分析重点关注的时间跨度。它不等同于自动下载多少年财报；最终数据范围仍受可用证据和 .ot 研究包约束。",
  depthHelp: "控制分析覆盖面、细节程度和指令约束强度。更高的值可能增加耗时和模型用量，但不会自动创造缺失证据。",
  riskHelp: "控制风险、反方观点和不确定性在综合过程中的强调程度。它不会修改原始事实，也不保证结论一定更保守或更准确。",
  workflow: "研究流程", role: "角色", schema: "输出 Schema", prompt: "有边界的任务指令",
  outputs: "输出格式", evidence: "包含可追溯证据", raw: "原始草稿 JSON", applyJson: "应用 JSON",
  assistant: "字段助手",
  assistantBody: "一次只选择一个字段。模型只会收到该字段、修改要求和完成建议所需的最小草稿上下文。",
  noModel: "没有已测试的辅助模型。你仍可手动编辑和导出。",
  modelCenter: "打开模型中心", model: "模型", field: "要改进的字段", instruction: "修改要求",
  fieldPackageName: "研究包名称", fieldPackageDescription: "研究包说明",
  fieldFinancialPrompt: "财务分析指令", fieldRiskPrompt: "风险审查指令",
  instructionHint: "让这段指令更明确地要求证据，并说明如何处理缺失值。",
  suggest: "生成建议", suggesting: "正在生成…", before: "修改前", after: "建议内容",
  accept: "接受修改", reject: "拒绝", validate: "检查草稿", validating: "正在检查…",
  export: "检查并导出", exporting: "正在编译…", valid: "草稿有效",
  invalid: "请先解决诊断问题", free: "Free", identity: "内容身份",
};

const ZH_HANT: StudioText = {
  eyebrow: ".ot 創作工作室", title: "建立可重複使用的研究套件",
  intro: "描述研究目標、調整約束，並匯出具確定性的 .ot 套件。模型建議只有在你查看差異並明確接受後，才會修改草稿。",
  guided: "引導模式", professional: "專業模式", undo: "復原", redo: "重做",
  intent: "研究意圖", goal: "這個研究套件應該研究什麼？",
  goalHint: "例如：從五年視角評估成長品質、會計品質和下行風險。",
  applyGoal: "用作套件說明", name: "研究套件名稱", packageId: "研究套件 ID", version: "版本",
  horizon: "研究週期", years: "年", depth: "分析深度", risk: "風險權重",
  helpLabel: "瞭解", horizonHelp: "控制研究與情境分析重點關注的時間跨度。它不等同於自動下載多少年財報；最終資料範圍仍受可用證據和 .ot 研究套件約束。",
  depthHelp: "控制分析覆蓋面、細節程度和指令約束強度。更高的值可能增加耗時和模型用量，但不會自動創造缺失證據。",
  riskHelp: "控制風險、反方觀點和不確定性在綜合過程中的強調程度。它不會修改原始事實，也不保證結論一定更保守或更準確。",
  workflow: "研究流程", role: "角色", schema: "輸出 Schema", prompt: "有邊界的任務指令",
  outputs: "輸出格式", evidence: "包含可追溯證據", raw: "原始草稿 JSON", applyJson: "套用 JSON",
  assistant: "欄位助手",
  assistantBody: "一次只選擇一個欄位。模型只會收到該欄位、修改要求，以及完成建議所需的最小草稿上下文。",
  noModel: "沒有已測試的輔助模型。你仍可手動編輯和匯出。",
  modelCenter: "開啟模型中心", model: "模型", field: "要改進的欄位", instruction: "修改要求",
  fieldPackageName: "研究套件名稱", fieldPackageDescription: "研究套件說明",
  fieldFinancialPrompt: "財務分析指令", fieldRiskPrompt: "風險審查指令",
  instructionHint: "讓這段指令更明確地要求證據，並說明如何處理缺失值。",
  suggest: "產生建議", suggesting: "正在產生…", before: "修改前", after: "建議內容",
  accept: "接受修改", reject: "拒絕", validate: "檢查草稿", validating: "正在檢查…",
  export: "檢查並匯出", exporting: "正在編譯…", valid: "草稿有效",
  invalid: "請先解決診斷問題", free: "Free", identity: "內容身分",
};

const FIELDS = [
  { path: "/package/name", labelKey: "fieldPackageName" },
  { path: "/package/description", labelKey: "fieldPackageDescription" },
  { path: "/workflow/steps/0/prompt", labelKey: "fieldFinancialPrompt" },
  { path: "/workflow/steps/1/prompt", labelKey: "fieldRiskPrompt" },
] as const;

function initialDraft(): OtDraft {
  return {
    package: {
      id: "custom.company-research", name: "Custom company research", version: "1.0.0",
      kind: "openthesis.research-pack", description: "Evidence-first company research with deterministic financial analysis.",
      license: "Apache-2.0",
    },
    settings: { horizon_years: 5, depth: 3, risk_emphasis: 3, report_language: "en" },
    workflow: { steps: [
      { id: "financial-analysis", role: "financial_analysis", depends_on: [], prompt: "Analyze the validated financial facts. Cite evidence IDs and distinguish reported facts from interpretation.", output_schema: "analysis.financial.v1" },
      { id: "risk-review", role: "skeptic", depends_on: ["financial-analysis"], prompt: "Challenge the thesis using the same evidence set. Do not invent missing facts or values.", output_schema: "analysis.risk.v1" },
    ] },
    outputs: { formats: ["markdown", "json"], include_evidence: true, deterministic_transforms: ["financial_summary", "valuation_inputs"] },
    ui: {},
    model_requirements: { capabilities: ["text_chat", "structured_json"], preferred_profile_alias: null },
    dependencies: [], relationships: [], optional_extensions: {},
  };
}

function cloneDraft(value: OtDraft): OtDraft {
  return JSON.parse(JSON.stringify(value)) as OtDraft;
}
function errorMessage(error: unknown): string {
  if (typeof error === "string") return error;
  if (error && typeof error === "object" && "message" in error) return String(error.message);
  return "Unexpected OT Studio error.";
}
function setPointer(draft: OtDraft, path: string, value: unknown): OtDraft {
  const next = cloneDraft(draft) as unknown as Record<string, unknown>;
  const parts = path.split("/").slice(1).map((part) => part.replace(/~1/g, "/").replace(/~0/g, "~"));
  let cursor: unknown = next;
  for (let index = 0; index < parts.length - 1; index += 1) {
    const key = parts[index];
    cursor = Array.isArray(cursor) ? cursor[Number(key)] : (cursor as Record<string, unknown>)[key];
  }
  const key = parts[parts.length - 1];
  if (Array.isArray(cursor)) cursor[Number(key)] = value;
  else (cursor as Record<string, unknown>)[key] = value;
  return next as unknown as OtDraft;
}
function displayValue(value: unknown): string {
  return typeof value === "string" ? value : JSON.stringify(value, null, 2);
}


type TooltipPosition = { top: number; left: number; originX: number; side: "top" | "bottom" };
let tooltipWarmUntil = 0;

function InlineHelp({ id, label, children }: { id: string; label: string; children: string }) {
  const triggerRef = useRef<HTMLButtonElement>(null);
  const tooltipRef = useRef<HTMLSpanElement>(null);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const pointerTypeRef = useRef("");
  const [open, setOpen] = useState(false);
  const [instant, setInstant] = useState(false);
  const [position, setPosition] = useState<TooltipPosition>({ top: 0, left: 12, originX: 18, side: "bottom" });

  const clearTimer = () => {
    if (timerRef.current) clearTimeout(timerRef.current);
    timerRef.current = null;
  };

  const updatePosition = () => {
    const trigger = triggerRef.current;
    if (!trigger) return;
    const rect = trigger.getBoundingClientRect();
    const width = Math.min(280, Math.max(180, window.innerWidth - 24));
    const height = tooltipRef.current?.offsetHeight || 108;
    const side: TooltipPosition["side"] = window.innerHeight - rect.bottom >= height + 14 ? "bottom" : "top";
    const idealLeft = rect.left + rect.width / 2 - width / 2;
    const left = Math.min(Math.max(12, idealLeft), Math.max(12, window.innerWidth - width - 12));
    const top = side === "bottom" ? rect.bottom + 9 : Math.max(8, rect.top - height - 9);
    const originX = Math.min(width - 14, Math.max(14, rect.left + rect.width / 2 - left));
    setPosition({ top, left, originX, side });
  };

  const show = (allowDelay: boolean) => {
    clearTimer();
    const shouldBeInstant = !allowDelay || Date.now() < tooltipWarmUntil;
    setInstant(shouldBeInstant);
    const commitOpen = () => {
      tooltipWarmUntil = Date.now() + 1200;
      setOpen(true);
    };
    if (shouldBeInstant) commitOpen();
    else timerRef.current = setTimeout(commitOpen, 160);
  };

  const hide = (delay = 80) => {
    clearTimer();
    if (delay <= 0) setOpen(false);
    else timerRef.current = setTimeout(() => setOpen(false), delay);
  };

  useEffect(() => {
    if (!open) return;
    updatePosition();
    const reposition = () => updatePosition();
    const closeWithEscape = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      hide(0);
    };
    window.addEventListener("resize", reposition);
    window.addEventListener("scroll", reposition, true);
    window.addEventListener("keydown", closeWithEscape);
    return () => {
      window.removeEventListener("resize", reposition);
      window.removeEventListener("scroll", reposition, true);
      window.removeEventListener("keydown", closeWithEscape);
    };
  }, [open]);

  useEffect(() => () => clearTimer(), []);

  const style = {
    top: `${position.top}px`,
    left: `${position.left}px`,
    "--tooltip-origin-x": `${position.originX}px`,
  } as CSSProperties;

  return <span className="inline-help">
    <button
      ref={triggerRef}
      className="inline-help-trigger"
      type="button"
      aria-label={label}
      aria-describedby={open ? id : undefined}
      onPointerDown={(event) => { pointerTypeRef.current = event.pointerType; }}
      onPointerEnter={(event) => { if (event.pointerType === "mouse") show(true); }}
      onPointerLeave={() => hide()}
      onFocus={() => { if (pointerTypeRef.current !== "touch") show(false); }}
      onBlur={() => { hide(); pointerTypeRef.current = ""; }}
      onClick={() => { if (pointerTypeRef.current === "touch") open ? hide(0) : show(false); }}
    ><CircleHelp size={14} aria-hidden="true" /></button>
    {open && createPortal(<span
      ref={tooltipRef}
      id={id}
      className="parameter-tooltip"
      role="tooltip"
      data-side={position.side}
      data-instant={instant || undefined}
      style={style}
      onPointerEnter={clearTimer}
      onPointerLeave={() => hide()}
    >{children}</span>, document.body)}
  </span>;
}
export function OtStudioView({ language, onOpenModelCenter }: { language: Language; onOpenModelCenter: () => void }) {
  const text = language === "en" ? EN : language === "zh-Hant" ? ZH_HANT : ZH;
  const [draft, setDraft] = useState<OtDraft>(() => initialDraft());
  const [past, setPast] = useState<OtDraft[]>([]);
  const [future, setFuture] = useState<OtDraft[]>([]);
  const [mode, setMode] = useState<Mode>("guided");
  const [goal, setGoal] = useState("");
  const [stepIndex, setStepIndex] = useState(0);
  const [raw, setRaw] = useState(() => JSON.stringify(initialDraft(), null, 2));
  const [models, setModels] = useState<ConfiguredModelSummary[]>([]);
  const [modelId, setModelId] = useState("");
  const [selectedPath, setSelectedPath] = useState<string>(FIELDS[1].path);
  const [instruction, setInstruction] = useState("");
  const [suggestion, setSuggestion] = useState<OtSuggestion | null>(null);
  const [diagnostics, setDiagnostics] = useState<OtDiagnostic[]>([]);
  const [validationAttempted, setValidationAttempted] = useState(false);
  const [identity, setIdentity] = useState("");
  const [busy, setBusy] = useState<"" | "validate" | "export" | "suggest">("");
  const [error, setError] = useState("");

  useEffect(() => {
    void listConfiguredModels().then((items) => {
      const ready = items.filter((item) => item.enabled && item.health_status === "ready");
      setModels(ready);
      setModelId((current) => current || ready[0]?.configured_model_id || "");
    }).catch((reason) => setError(errorMessage(reason)));
  }, []);

  const model = models.find((item) => item.configured_model_id === modelId);
  const step = draft.workflow.steps[stepIndex] ?? draft.workflow.steps[0];
  const valid = validationAttempted && !diagnostics.some((item) => item.severity === "error");
  const clearValidation = () => { setDiagnostics([]); setValidationAttempted(false); setIdentity(""); };

  const commit = (next: OtDraft) => {
    setPast((items) => [...items.slice(-39), cloneDraft(draft)]);
    setFuture([]);
    setDraft(next);
    setRaw(JSON.stringify(next, null, 2));
    clearValidation();
    setSuggestion(null);
    setError("");
  };
  const update = (mutate: (value: OtDraft) => void) => {
    const next = cloneDraft(draft);
    mutate(next);
    commit(next);
  };
  const undo = () => {
    const previous = past[past.length - 1];
    if (!previous) return;
    setPast((items) => items.slice(0, -1));
    setFuture((items) => [cloneDraft(draft), ...items.slice(0, 39)]);
    setDraft(previous);
    setRaw(JSON.stringify(previous, null, 2));
    setSuggestion(null);
    clearValidation();
  };
  const redo = () => {
    const next = future[0];
    if (!next) return;
    setFuture((items) => items.slice(1));
    setPast((items) => [...items.slice(-39), cloneDraft(draft)]);
    setDraft(next);
    setRaw(JSON.stringify(next, null, 2));
    setSuggestion(null);
    clearValidation();
  };
  const applyRaw = () => {
    try { commit(JSON.parse(raw) as OtDraft); }
    catch (reason) { setError(errorMessage(reason)); }
  };
  const runValidation = async () => {
    setBusy("validate"); setError("");
    try { const result = await validateOtDraft(draft); setDiagnostics(result.diagnostics); setValidationAttempted(true); }
    catch (reason) { setError(errorMessage(reason)); }
    finally { setBusy(""); }
  };
  const compileAndExport = async () => {
    setBusy("export"); setError("");
    try {
      const result = await compileOtDraft(draft);
      setDiagnostics(result.diagnostics);
      setValidationAttempted(true);
      setIdentity(result.content_identity || "");
      if (result.valid && result.filename && result.data_base64) await exportOtPackage(result.filename, result.data_base64);
    } catch (reason) { setError(errorMessage(reason)); }
    finally { setBusy(""); }
  };
  const askAssistant = async () => {
    if (!model || !instruction.trim()) return;
    setBusy("suggest"); setError(""); setSuggestion(null);
    try {
      setSuggestion(await suggestOtPatch(draft, selectedPath, instruction.trim(), {
        configured_model_id: model.configured_model_id,
        configuration_version: model.configuration_version ?? 1,
        role: "ot_assistant",
      }));
    } catch (reason) { setError(errorMessage(reason)); }
    finally { setBusy(""); }
  };
  const toggleFormat = (format: string) => update((value) => {
    value.outputs.formats = value.outputs.formats.includes(format)
      ? value.outputs.formats.filter((item) => item !== format)
      : [...value.outputs.formats, format];
  });

  return <div className="ot-studio">
    <header className="ot-studio-hero">
      <div>
        <span className="eyebrow"><Code2 size={14} />{text.eyebrow}</span>
        <h2>{text.title}</h2>
        <p>{text.intro}</p>
      </div>
      <div className="ot-studio-toolbar">
        <div className="segmented-control">
          <button type="button" data-active={mode === "guided" || undefined} onClick={() => setMode("guided")}><SlidersHorizontal size={15} />{text.guided}</button>
          <button type="button" data-active={mode === "professional" || undefined} onClick={() => setMode("professional")}><Braces size={15} />{text.professional}</button>
        </div>
        <button className="icon-button" type="button" disabled={!past.length} title={text.undo} aria-label={text.undo} onClick={undo}><Undo2 size={17} /></button>
        <button className="icon-button" type="button" disabled={!future.length} title={text.redo} aria-label={text.redo} onClick={redo}><Redo2 size={17} /></button>
      </div>
    </header>
    {error && <div className="error-banner" role="alert">{error}</div>}
    <div className="ot-studio-grid">
      <div className="ot-editor-column">
        <section className="studio-card">
          <div className="studio-section-heading"><div><small>01</small><h3>{text.intent}</h3></div><ShieldCheck size={18} /></div>
          <label>{text.goal}<textarea value={goal} placeholder={text.goalHint} onChange={(event) => setGoal(event.target.value)} /></label>
          <button className="secondary-button" type="button" disabled={!goal.trim()} onClick={() => update((value) => { value.package.description = goal.trim(); })}>{text.applyGoal}</button>
          <div className="studio-form-grid">
            <label>{text.name}<input value={draft.package.name} onChange={(event) => update((value) => { value.package.name = event.target.value; })} /></label>
            <label>{text.packageId}<input value={draft.package.id} onChange={(event) => update((value) => { value.package.id = event.target.value; })} /></label>
            <label>{text.version}<input value={draft.package.version} onChange={(event) => update((value) => { value.package.version = event.target.value; })} /></label>
          </div>
          <div className="studio-slider-grid">
            <div className="studio-slider-field">
              <div className="studio-slider-heading"><span>{text.horizon}<InlineHelp id="horizon-help" label={`${text.helpLabel}: ${text.horizon}`}>{text.horizonHelp}</InlineHelp></span><output>{draft.settings.horizon_years} {text.years}</output></div>
              <input aria-label={text.horizon} type="range" min="1" max="20" value={draft.settings.horizon_years} onChange={(event) => update((value) => { value.settings.horizon_years = Number(event.target.value); })} />
            </div>
            <div className="studio-slider-field">
              <div className="studio-slider-heading"><span>{text.depth}<InlineHelp id="depth-help" label={`${text.helpLabel}: ${text.depth}`}>{text.depthHelp}</InlineHelp></span><output>{draft.settings.depth}/5</output></div>
              <input aria-label={text.depth} type="range" min="1" max="5" value={draft.settings.depth} onChange={(event) => update((value) => { value.settings.depth = Number(event.target.value); })} />
            </div>
            <div className="studio-slider-field">
              <div className="studio-slider-heading"><span>{text.risk}<InlineHelp id="risk-help" label={`${text.helpLabel}: ${text.risk}`}>{text.riskHelp}</InlineHelp></span><output>{draft.settings.risk_emphasis}/5</output></div>
              <input aria-label={text.risk} type="range" min="1" max="5" value={draft.settings.risk_emphasis} onChange={(event) => update((value) => { value.settings.risk_emphasis = Number(event.target.value); })} />
            </div>
          </div>
        </section>
        <section className="studio-card">
          <div className="studio-section-heading"><div><small>02</small><h3>{text.workflow}</h3></div><Code2 size={18} /></div>
          <div className="step-tabs" role="tablist">
            {draft.workflow.steps.map((item, index) => <button key={item.id} type="button" role="tab" aria-selected={stepIndex === index} data-active={stepIndex === index || undefined} onClick={() => setStepIndex(index)}>{item.role}</button>)}
          </div>
          {step && <div className="studio-form-grid">
            <label>{text.role}<input value={step.role} onChange={(event) => update((value) => { value.workflow.steps[stepIndex].role = event.target.value; })} /></label>
            <label>{text.schema}<input value={step.output_schema} onChange={(event) => update((value) => { value.workflow.steps[stepIndex].output_schema = event.target.value; })} /></label>
            <label className="full-span">{text.prompt}<textarea value={step.prompt} onChange={(event) => update((value) => { value.workflow.steps[stepIndex].prompt = event.target.value; })} /></label>
          </div>}
        </section>
        <section className="studio-card">
          <div className="studio-section-heading"><div><small>03</small><h3>{text.outputs}</h3></div><Download size={18} /></div>
          <div className="choice-row">
            <label><input type="checkbox" checked={draft.outputs.formats.includes("markdown")} onChange={() => toggleFormat("markdown")} />Markdown</label>
            <label><input type="checkbox" checked={draft.outputs.formats.includes("json")} onChange={() => toggleFormat("json")} />JSON</label>
            <label><input type="checkbox" checked={draft.outputs.include_evidence} onChange={(event) => update((value) => { value.outputs.include_evidence = event.target.checked; })} />{text.evidence}</label>
          </div>
        </section>
        {mode === "professional" && <section className="studio-card raw-editor-card">
          <div className="studio-section-heading"><div><small>04</small><h3>{text.raw}</h3></div><Braces size={18} /></div>
          <textarea className="code-editor" aria-label={text.raw} spellCheck={false} value={raw} onChange={(event) => setRaw(event.target.value)} />
          <button className="secondary-button" type="button" onClick={applyRaw}>{text.applyJson}</button>
        </section>}
      </div>
      <aside className="ot-assistant-column">
        <section className="studio-card assistant-card">
          <div className="studio-section-heading"><div><small><Sparkles size={12} /> AI</small><h3>{text.assistant}</h3></div><Bot size={18} /></div>
          <p>{text.assistantBody}</p>
          {!models.length ? <div className="assistant-empty"><p>{text.noModel}</p><button className="secondary-button" type="button" onClick={onOpenModelCenter}>{text.modelCenter}</button></div> : <>
            <label>{text.model}<select value={modelId} onChange={(event) => setModelId(event.target.value)}>{models.map((item) => <option key={item.configured_model_id} value={item.configured_model_id}>{item.alias}{item.free_tier ? " · " + text.free : ""}</option>)}</select></label>
            <label>{text.field}<select value={selectedPath} onChange={(event) => { setSelectedPath(event.target.value); setSuggestion(null); }}>{FIELDS.map((field) => <option key={field.path} value={field.path}>{text[field.labelKey]}</option>)}</select></label>
            <label>{text.instruction}<textarea value={instruction} placeholder={text.instructionHint} onChange={(event) => setInstruction(event.target.value)} /></label>
            <button className="primary-button" type="button" disabled={!instruction.trim() || busy === "suggest"} onClick={() => void askAssistant()}>{busy === "suggest" ? text.suggesting : text.suggest}</button>
          </>}
          {suggestion && <div className="suggestion-diff" aria-live="polite">
            <div><small>{text.before}</small><pre>{displayValue(suggestion.before)}</pre></div>
            <div data-tone="suggestion"><small>{text.after}</small><pre>{displayValue(suggestion.after)}</pre></div>
            <div className="diff-actions">
              <button className="primary-button" type="button" onClick={() => { commit(setPointer(draft, suggestion.path, suggestion.after)); setSuggestion(null); }}><Check size={15} />{text.accept}</button>
              <button className="secondary-button" type="button" onClick={() => setSuggestion(null)}><X size={15} />{text.reject}</button>
            </div>
          </div>}
        </section>
        <section className="studio-card validation-card">
          <div className="studio-actions">
            <button className="secondary-button" type="button" disabled={Boolean(busy)} onClick={() => void runValidation()}>{busy === "validate" ? text.validating : text.validate}</button>
            <button className="primary-button" type="button" disabled={Boolean(busy)} onClick={() => void compileAndExport()}><Download size={15} />{busy === "export" ? text.exporting : text.export}</button>
          </div>
          {validationAttempted && <div className="diagnostics" role="status" aria-live="polite" data-valid={valid || undefined}>
            <strong>{valid ? text.valid : text.invalid}</strong>
            {diagnostics.map((item, index) => <div key={item.code + "-" + index} data-severity={item.severity}><code>{item.path || "/"}</code><span>{item.message}</span></div>)}
          </div>}
          {identity && <p className="content-identity"><span>{text.identity}</span><code>{identity}</code></p>}
        </section>
      </aside>
    </div>
  </div>;
}

