export type ProgressLanguage = "zh-CN" | "en";

type StageCopy = { title: string; note: string };

const STAGES: Record<ProgressLanguage, Record<string, StageCopy>> = {
  "zh-CN": {
    preparing: { title: "正在准备研究任务……", note: "先确认研究对象与配置，再开始形成判断。" },
    "company-profile": { title: "正在读取公司基本信息……", note: "了解一家企业，从知道它究竟靠什么赚钱开始。" },
    "filing-discovery": { title: "正在查找官方披露文件……", note: "可靠的研究，应该从可靠的一手资料开始。" },
    "filing-download": { title: "正在下载官方财报……", note: "文件正在抵达，下一步是核对其中真正重要的数据。" },
    "filing-parse": { title: "正在识别财务报表……", note: "表格被逐页读取，口径、单位与期间也会一并核对。" },
    "filing-validation": { title: "正在校验财务数据……", note: "数字只有通过口径与勾稽校验，才会进入后续研究。" },
    "vision-approval": { title: "等待确认云端识图页面……", note: "只有本地识别失败的财务表页会在您同意后上传。" },
    "vision-processing": { title: "正在补充识别失败的财务表页……", note: "本地识别没有放行的数据，正在接受第二次核对。" },
    "financial-analysis": { title: "正在分析近年财务表现……", note: "营收告诉我们规模，现金流往往告诉我们质量。" },
    "base-analysis": { title: "正在协同分析财务、商业与会计风险……", note: "先把不同视角分别研究，再把它们放回同一套证据中核对。" },
    "business-analysis": { title: "正在理解商业模式与竞争位置……", note: "一家公司如何赚钱，与它能否持续赚钱同样重要。" },
    "risk-analysis": { title: "正在梳理竞争优势与风险……", note: "好的投资研究，不应该只寻找买入的理由。" },
    "growth-analysis": { title: "正在研究未来增长机会……", note: "增长不只需要方向，也需要证据、时间与兑现路径。" },
    "counter-analysis": { title: "正在进行反方审查……", note: "如果结论经不起反对意见，它也很难经得起市场。" },
    "scenario-analysis": { title: "正在推演长期经营情景……", note: "长期价值来自多种可能，而不是唯一预测。" },
    synthesis: { title: "正在整合投资结论……", note: "数据已经找到，现在需要把它们变成判断。" },
    comparison: { title: "正在运行第二模型对比……", note: "不同模型的分歧，也是一种值得检查的信息。" },
    cancelling: { title: "正在安全停止研究……", note: "正在结束未完成请求，并保留已经完成的阶段。" },
    waiting: { title: "研究仍在进行……", note: "正在等待当前步骤完成，并持续记录已用时间。" },
  },
  en: {
    preparing: { title: "Preparing the research…", note: "First we confirm the company and research setup, then the analysis can begin." },
    "company-profile": { title: "Reading company basics…", note: "Understanding a business starts with knowing how it actually makes money." },
    "filing-discovery": { title: "Finding official disclosures…", note: "Reliable research should begin with reliable primary sources." },
    "filing-download": { title: "Downloading official filings…", note: "The filings are arriving; next we verify the numbers that truly matter." },
    "filing-parse": { title: "Reading financial statements…", note: "Each table is being read page by page, with periods, units, and scope checked along the way." },
    "filing-validation": { title: "Validating financial data…", note: "Only figures that pass scope and consistency checks move into the research." },
    "vision-approval": { title: "Waiting for cloud-vision approval…", note: "Only failed financial-table pages are uploaded, and only with your approval." },
    "vision-processing": { title: "Rechecking failed financial-table pages…", note: "Figures that failed local validation are receiving a second review." },
    "financial-analysis": { title: "Analyzing recent financial performance…", note: "Revenue shows scale; cash flow often reveals quality." },
    "base-analysis": { title: "Analyzing financials, the business, and accounting risks…", note: "Each perspective is studied separately, then checked against the same body of evidence." },
    "business-analysis": { title: "Studying the business model and competitive position…", note: "How a company makes money matters as much as whether it can keep doing so." },
    "risk-analysis": { title: "Reviewing advantages and risks…", note: "Good investment research should not look only for reasons to buy." },
    "growth-analysis": { title: "Evaluating future growth opportunities…", note: "Growth needs more than a direction; it needs evidence, timing, and a path to execution." },
    "counter-analysis": { title: "Stress-testing the thesis…", note: "If a conclusion cannot withstand objections, it is unlikely to withstand the market." },
    "scenario-analysis": { title: "Building long-term operating scenarios…", note: "Long-term value comes from a range of possibilities, not a single prediction." },
    synthesis: { title: "Integrating the investment conclusion…", note: "The data is in; now it must be turned into judgment." },
    comparison: { title: "Running the second-model comparison…", note: "Disagreement between models is itself information worth examining." },
    cancelling: { title: "Stopping the research safely…", note: "Finishing active requests and preserving completed stages." },
    waiting: { title: "Research is still in progress…", note: "Waiting for the current step to finish while keeping track of elapsed time." },
  },
};

const FIXED_MESSAGES: Record<ProgressLanguage, readonly string[]> = {
  "zh-CN": [
    "巴菲特用一生寻找好公司，而 OpenThesis 正在用几分钟，为您认真研究眼前这一家。",
    "真正值得投入真金白银的公司，也值得多花几分钟看清楚。",
  ],
  en: [
    "Buffett has spent a lifetime looking for great companies. OpenThesis is taking a few minutes to study this one carefully for you.",
    "A company worth committing real money to is also worth a few extra minutes to understand clearly.",
  ],
};

const RANDOM_MESSAGES: Record<ProgressLanguage, readonly string[]> = {
  "zh-CN": [
    "投资决策可能影响数年，多几分钟研究，往往比少几分钟等待更重要。",
    "我们正在逐项核对财务数据、公司信息与关键风险，请给研究一点时间。",
    "好的研究不是生成得最快的答案，而是尽可能少遗漏重要信息。",
    "几分钟之后，您看到的不只是一份结论，而是一条尽可能完整的投资逻辑。",
    "市场每天都有噪音，我们正在努力把真正重要的信息筛出来。",
    "财报可以在几秒钟内下载，但理解一家企业，需要更多一点时间。",
    "市场奖励耐心，研究同样如此。",
    "股价每秒都在变化，企业价值却值得慢几分钟看清。",
    "与其快速得到一个答案，不如稍等片刻，得到一个更值得参考的答案。",
    "投资最昂贵的成本，往往不是等待几分钟，而是在信息不足时做出决定。",
    "在点击“买入”之前，多了解一家企业几分钟，通常不是坏事。",
    "巴菲特花几十年训练自己的判断力，我们只希望占用您几分钟。",
    "巴菲特说机会要等，OpenThesis 至少不会让您等那么久。",
    "寻找一家好公司可能需要很多年，好在这份研究只需要几分钟。",
    "如果一家企业值得持有十年，那么它大概也值得您多研究几分钟。",
    "咖啡还没凉，研究已经在路上了。",
    "别急着看股价，我们正在先看它究竟值不值得看。",
    "正在翻财报。好消息是，您不用亲自翻。",
    "数字很多，我们正在替您把重要的留下。",
    "请稍候，我们正在和几十页财报进行一场严肃的谈判。",
    "研究仍在进行——至少这几分钟里，市场不会因为我们少看一页财报而变得更简单。",
  ],
  en: [
    "An investment decision can matter for years. A few more minutes of research often matter more than a few fewer minutes of waiting.",
    "We are checking the financials, company information, and key risks one by one. Please give the research a little time.",
    "Good research is not the fastest answer; it is the one that overlooks as little important information as possible.",
    "In a few minutes, you will see more than a conclusion—you will see as complete an investment rationale as we can build.",
    "Markets produce noise every day. We are working to filter out what truly matters.",
    "A filing can be downloaded in seconds, but understanding a business takes a little longer.",
    "Markets reward patience. Research does too.",
    "Share prices change every second, but business value deserves a few slower minutes of attention.",
    "Rather than getting an answer quickly, it is often better to wait a moment for one that is more useful.",
    "The most expensive cost in investing is often not waiting a few minutes, but deciding with too little information.",
    "Before clicking “Buy,” spending a few more minutes understanding the business is rarely a bad idea.",
    "Buffett spent decades training his judgment. We are only asking for a few minutes of your time.",
    "Buffett says opportunities require patience. OpenThesis, at least, will not keep you waiting that long.",
    "Finding a great company can take years. Fortunately, this research only takes a few minutes.",
    "If a business is worth holding for ten years, it is probably worth a few more minutes of research.",
    "Your coffee is still warm, and the research is already underway.",
    "Do not rush to the share price—we are first checking whether the business itself deserves attention.",
    "We are going through the filings. The good news is that you do not have to.",
    "There are plenty of numbers. We are keeping the ones that matter.",
    "Please wait—we are having a serious negotiation with dozens of pages of financial statements.",
    "The research is still running—and the market will not become any simpler because we skipped a page of the filing.",
  ],
};

export const WAITING_RANDOM_COUNT = RANDOM_MESSAGES.en.length;

function normalizeLanguage(language: string | undefined): ProgressLanguage {
  return language === "en" ? "en" : "zh-CN";
}

function hashSeed(value: string): number {
  let hash = 2166136261;
  for (let index = 0; index < value.length; index += 1) {
    hash ^= value.charCodeAt(index);
    hash = Math.imul(hash, 16777619);
  }
  return hash >>> 0;
}

function randomFrom(seed: number): () => number {
  let value = seed || 1;
  return () => {
    value += 0x6D2B79F5;
    let result = value;
    result = Math.imul(result ^ (result >>> 15), result | 1);
    result ^= result + Math.imul(result ^ (result >>> 7), result | 61);
    return ((result ^ (result >>> 14)) >>> 0) / 4294967296;
  };
}

function shuffledCycle(language: ProgressLanguage, jobId: string, targetCycle: number): string[] {
  let previousLast = "";
  let selected: string[] = [];
  for (let cycle = 0; cycle <= targetCycle; cycle += 1) {
    selected = [...RANDOM_MESSAGES[language]];
    const random = randomFrom(hashSeed(`${jobId}:${cycle}`));
    for (let index = selected.length - 1; index > 0; index -= 1) {
      const swap = Math.floor(random() * (index + 1));
      [selected[index], selected[swap]] = [selected[swap], selected[index]];
    }
    if (selected.length > 1 && selected[0] === previousLast) {
      [selected[0], selected[1]] = [selected[1], selected[0]];
    }
    previousLast = selected[selected.length - 1];
  }
  return selected;
}

export function waitingMessageAt(language: string | undefined, elapsedSeconds: number, jobId: string): string {
  const normalized = normalizeLanguage(language);
  const elapsed = Math.max(0, Math.floor(elapsedSeconds));
  if (elapsed < 10) return FIXED_MESSAGES[normalized][0];
  if (elapsed < 20) return FIXED_MESSAGES[normalized][1];
  const position = Math.floor((elapsed - 20) / 5);
  const cycle = Math.floor(position / WAITING_RANDOM_COUNT);
  const index = position % WAITING_RANDOM_COUNT;
  return shuffledCycle(normalized, jobId, cycle)[index];
}

export function formatElapsedTime(elapsedSeconds: number): string {
  const elapsed = Math.max(0, Math.floor(elapsedSeconds));
  const hours = Math.floor(elapsed / 3600);
  const minutes = Math.floor((elapsed % 3600) / 60);
  const seconds = elapsed % 60;
  const mmss = `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
  return hours > 0 ? `${String(hours).padStart(2, "0")}:${mmss}` : mmss;
}

export function progressStageCopy(language: string | undefined, stage: string | undefined): StageCopy {
  const normalized = normalizeLanguage(language);
  const aliases: Record<string, string> = {
    "parallel-agents": "base-analysis",
    research: "waiting",
    "vision-fallback": "vision-processing",
  };
  const key = aliases[stage ?? ""] ?? stage ?? "preparing";
  return STAGES[normalized][key] ?? STAGES[normalized].waiting;
}

export function progressStageDetail(
  language: string | undefined,
  stage: string | undefined,
  current: number | undefined,
  total: number | undefined,
): string {
  if (!total || current === undefined || current < 0) return "";
  const normalized = normalizeLanguage(language);
  const safeCurrent = Math.min(current, total);
  if (normalized === "en") {
    if (stage === "filing-download") return `Downloading filing ${safeCurrent}/${total}`;
    if (stage === "filing-parse") return `Reading filing ${safeCurrent}/${total}`;
    if (stage === "filing-validation") return `Validated filing ${safeCurrent}/${total}`;
    return `Completed ${safeCurrent}/${total}`;
  }
  if (stage === "filing-download") return `正在下载第 ${safeCurrent}/${total} 份官方财报`;
  if (stage === "filing-parse") return `正在识别第 ${safeCurrent}/${total} 份财报`;
  if (stage === "filing-validation") return `已校验第 ${safeCurrent}/${total} 份财报`;
  return `已完成 ${safeCurrent}/${total}`;
}

export function agentDisplayName(language: string | undefined, agentId: string): string {
  const normalized = normalizeLanguage(language);
  const labels: Record<string, readonly [string, string]> = {
    "financial-analyst": ["财务分析", "Financial analysis"],
    "business-analyst": ["商业模式", "Business analysis"],
    "accounting-risk-analyst": ["会计与风险", "Accounting and risk"],
  };
  const label = labels[agentId];
  return label ? label[normalized === "en" ? 1 : 0] : (normalized === "en" ? "Research stage" : "研究阶段");
}
