import { useEffect, useMemo, useRef, useState } from 'react'
import {
  ArrowDownRight, ArrowRight, BarChart3, BookOpenCheck, Check, ChevronDown, CircleDot,
  CircleGauge, Code2, Database, Download, ExternalLink, FileCheck2, Fingerprint,
  GitBranch, Globe2, History, KeyRound, Languages, Layers3, LockKeyhole, Menu, Play,
  RotateCcw, Scale, ShieldCheck, Sparkles, Square, Terminal, X, Zap
} from 'lucide-react'
import type { LucideIcon } from 'lucide-react'
import { LANGUAGE_STORAGE_KEY, applyLanguageMetadata, resolveInitialLanguage, resolveSystemLanguage, storedLanguage } from './language'
import { mountStoryTimeline } from './motion/storyTimeline'

type Icon = LucideIcon

const copy = {
  en: {
    nav: { product: 'Product', workflow: 'Workflow', markets: 'Markets', privacy: 'Privacy', ot: '.ot Ecosystem', github: 'GitHub', download: 'Download' },
    switchLanguage: '中文',
    followSystem: 'Use system language',
    eyebrow: 'OPEN SOURCE · LOCAL FIRST',
    heroTitle: <><span className="headline-line">Research companies.</span><br /><span className="headline-line"><em>Not prices.</em></span></>,
    heroBody: 'OpenThesis turns official disclosures, deterministic finance and focused AI agents into an investment thesis you can trace, challenge and keep.',
    download: 'Download for Windows',
    github: 'Explore on GitHub',
    scroll: 'Scroll to enter the workbench',
    sideNote: 'An open-source company research workbench',
    marketKicker: '01 / THE WORKBENCH',
    marketTitle: <><span className="headline-line">Three markets.</span><br /><span className="headline-line"><em>One line of thought.</em></span></>,
    marketBody: 'US, A-share and Hong Kong companies share a deliberate research path — while their exchange, currency, reporting standard and source remain visible.',
    marketFoot: 'A single place for different disclosures.',
    source: 'Official source',
    evidenceKicker: '02 / THE QUALITY GATE',
    evidenceTitle: <><span className="headline-line">Evidence</span><br /><span className="headline-line"><em>before opinion.</em></span></>,
    evidenceBody: 'Every number has a home: source URL, page, period, unit, scope and currency. If a fact fails the quality gate, OpenThesis keeps it missing instead of filling the gap.',
    evidenceFoot: 'A gap is information too.',
    agentsKicker: '03 / THE DIVISION OF LABOUR',
    agentsTitle: <><span className="headline-line">AI researches.</span><br /><span className="headline-line"><em>Code calculates.</em></span></>,
    agentsBody: 'Specialist agents read the same controlled evidence. The program owns growth rates, cross-period checks and reverse DCF — so a persuasive paragraph cannot quietly change a number.',
    agentsFoot: 'Opinion and arithmetic, in their proper lanes.',
    workflowKicker: '04 / THE RUN',
    workflowTitle: <><span className="headline-line">A long research run,</span><br /><span className="headline-line"><em>in plain sight.</em></span></>,
    workflowBody: 'From disclosure discovery to synthesis, every phase has a real status, elapsed time and an honest outcome. Cancel safely. Resume from what is already done.',
    workflowFoot: 'No fake countdowns. No black box.',
    recoveryKicker: '05 / THE RECOVERY',
    recoveryTitle: <><span className="headline-line">Failure is visible.</span><br /><span className="headline-line"><em>So is the way back.</em></span></>,
    recoveryBody: 'A partial report stays readable. Retry the failed growth stage or synthesis stage without downloading the filings or rerunning work that already passed.',
    retry: 'Retry only what failed',
    reportReady: 'Partial report remains available',
    localKicker: '06 / MODEL CENTER',
    localTitle: <><span className="headline-line">Configure once.</span><br /><span className="headline-line"><em>Keep the boundary local.</em></span></>,
    localBody: 'Connect providers and local models in one Model Center. API keys live in the operating system credential vault; research runs and .ot files keep only non-secret references.',
    localFoot: 'Credentials never travel with a research object.',
    otKicker: '07 / THE .ot ECOSYSTEM',
    otTitle: <><span className="headline-line">Research can travel.</span><br /><span className="headline-line"><em>Proof stays with it.</em></span></>,
    otBody: '.ot carries workflows, evidence, calculations and reports in one typed, versioned and verifiable research object—without credentials or arbitrary code.',
    otFoot: 'One object keeps the structure and the proof.',
    otCta: 'Explore the .ot ecosystem',
    otObjectLabel: 'Open the .ot ecosystem page',
    otSignals: ['SCHEMA', 'EVIDENCE', 'HASH', 'PERMISSIONS'],
    finalKicker: 'OPEN SOURCE COMPANY RESEARCH',
    finalTitle: <>Take the long view<br /><em>seriously.</em></>,
    finalBody: 'Download the Windows portable build, inspect the source, and carry verifiable research through the open .ot ecosystem.',
    finalDownload: 'Download for Windows',
    finalGithub: 'Read the source',
    apache: 'Apache License 2.0 · No bundled models · No broker connection',
    disclaimer: 'OpenThesis is a research tool, not investment advice. It does not execute trades or promise returns.',
    menu: 'Open menu', close: 'Close menu',
    proof: ['Official filings', 'Deterministic finance', 'Focused agents'],
    capabilities: 'The full workbench',
    capabilitiesBody: 'A compact view of the workbench beyond the story above.',
    capabilityGroups: [
      { title: 'Markets & sources', items: ['SEC EDGAR + XBRL', 'A-share official disclosures', 'HKEX official disclosures', 'Issuer / security identity'] },
      { title: 'Research system', items: ['Seven specialist agents', 'Reusable model profiles', 'Unified Model Center', 'OT Studio + .ot ecosystem'] },
      { title: 'Report & history', items: ['Technical evidence detail', 'HTML / Markdown / text export', '90–130% report zoom', 'Append-only thesis versions'] },
      { title: 'Safety boundaries', items: ['OS credential vault', '.ot carries no secrets', 'Explicit connector permissions', 'No silent paid fallback'] }
    ]
  },
  zh: {
    nav: { product: '产品', workflow: '研究流程', markets: '市场', privacy: '隐私', ot: '.ot 生态', github: 'GitHub', download: '下载' },
    switchLanguage: 'English',
    followSystem: '跟随系统语言',
    eyebrow: '开源 · 本地优先',
    heroTitle: <><span className="headline-line">研究公司，</span><br /><span className="headline-line"><em>而不是追逐价格。</em></span></>,
    heroBody: 'OpenThesis 把官方披露、确定性财务计算和专注的 AI Agent，组织成一份可以追溯、质疑并长期保存的投资论点。',
    download: '下载 Windows 便携版',
    github: '在 GitHub 查看',
    scroll: '向下进入研究工作台',
    sideNote: '开源公司研究工作台',
    marketKicker: '01 / 研究工作台',
    marketTitle: <><span className="headline-line">三大市场。</span><br /><span className="headline-line"><em>一套研究思路。</em></span></>,
    marketBody: '美股、A 股和港股共享一条有纪律的研究路径，同时保留交易所、币种、会计准则和来源差异。',
    marketFoot: '不同披露，汇聚于同一个地方。',
    source: '官方来源',
    evidenceKicker: '02 / 财务质量门',
    evidenceTitle: <><span className="headline-line">先有证据，</span><br /><span className="headline-line"><em>再有观点。</em></span></>,
    evidenceBody: '每个数字都有出处：来源 URL、页码、期间、单位、合并范围和币种。如果事实没能通过质量门，OpenThesis 会保留缺失，而不是填补空白。',
    evidenceFoot: '缺口本身也是信息。',
    agentsKicker: '03 / 分工',
    agentsTitle: <><span className="headline-line">AI 负责研究。</span><br /><span className="headline-line"><em>程序负责计算。</em></span></>,
    agentsBody: '专业 Agent 围绕同一组受控证据工作。增长率、跨期校验和反向 DCF 由程序负责，因此有说服力的段落不会悄悄改动数字。',
    agentsFoot: '观点与算术，各司其职。',
    workflowKicker: '04 / 研究运行',
    workflowTitle: <><span className="headline-line">漫长的研究过程，</span><br /><span className="headline-line"><em>每一步都可见。</em></span></>,
    workflowBody: '从发现披露到综合报告，每个阶段都有真实状态、已用时间和诚实的结果。可以安全取消，也可以从已经完成的部分恢复。',
    workflowFoot: '没有虚假的倒计时，也没有黑箱。',
    recoveryKicker: '05 / 恢复',
    recoveryTitle: <><span className="headline-line">失败不伪装。</span><br /><span className="headline-line"><em>恢复路径也清楚。</em></span></>,
    recoveryBody: '部分报告仍然可读。只重试失败的增长或综合阶段，不重复下载财报，也不重跑已经通过的工作。',
    retry: '只重试失败部分',
    reportReady: '部分报告仍可阅读',
    localKicker: '06 / 模型中心',
    localTitle: <><span className="headline-line">配置一次。</span><br /><span className="headline-line"><em>边界留在本机。</em></span></>,
    localBody: '在统一模型中心连接云端服务商与本地模型。API Key 保存在操作系统凭据库中；研究运行与 .ot 文件只保留非秘密引用。',
    localFoot: '凭据不会跟随研究对象流转。',
    otKicker: '07 / .ot 生态',
    otTitle: <><span className="headline-line">让研究流转，</span><br /><span className="headline-line"><em>证据始终随行。</em></span></>,
    otBody: '.ot 把工作流、证据、计算与报告组织成一个类型化、版本化、可验证的研究对象；它可以跨工具流转，但不会携带凭据或任意代码。',
    otFoot: '一个对象，保留研究的结构与证明。',
    otCta: '探索 .ot 生态',
    otObjectLabel: '进入 .ot 生态页面',
    otSignals: ['SCHEMA', '证据', '哈希', '权限'],
    finalKicker: '开源公司研究',
    finalTitle: <>认真地，<br /><em>看得更远。</em></>,
    finalBody: '下载 Windows 便携版，查看源代码，让可验证研究在开放的 .ot 生态中持续流转。',
    finalDownload: '下载 Windows 便携版',
    finalGithub: '阅读源代码',
    apache: 'Apache License 2.0 · 不内置模型 · 不连接券商',
    disclaimer: 'OpenThesis 是研究工具，不构成投资建议。不执行交易，也不承诺收益。',
    menu: '打开菜单', close: '关闭菜单',
    proof: ['官方披露', '确定性财务', '专注 Agent'],
    capabilities: '完整工作台',
    capabilitiesBody: '除了上面的滚动故事，完整工作台还提供这些能力。',
    capabilityGroups: [
      { title: '市场与来源', items: ['SEC EDGAR + XBRL', 'A 股官方披露', '港交所披露易', '发行人 / 证券身份'] },
      { title: '研究系统', items: ['七个专业 Agent', '可复用模型组合', '统一模型中心', 'OT 创作工作室与 .ot 生态'] },
      { title: '报告与历史', items: ['技术证据详情', 'HTML / Markdown / 文本导出', '90–130% 报告缩放', '追加式论点版本'] },
      { title: '安全边界', items: ['系统凭据库', '.ot 不携带秘密', 'Connector 明确授权', '不静默切换付费模型'] }
    ]
  }
} as const

type Copy = (typeof copy)[keyof typeof copy]

function useScrollMeter() {
  const meterRef = useRef<HTMLSpanElement>(null)
  const frameRef = useRef(0)

  useEffect(() => {
    const update = () => {
      frameRef.current = 0
      const max = document.documentElement.scrollHeight - window.innerHeight
      const progress = max > 0 ? window.scrollY / max : 0
      meterRef.current?.style.setProperty('transform', `scaleX(${Math.max(progress, .02).toFixed(4)})`)
    }
    const onScroll = () => { if (!frameRef.current) frameRef.current = requestAnimationFrame(update) }
    update()
    window.addEventListener('scroll', onScroll, { passive: true })
    window.addEventListener('resize', onScroll, { passive: true })
    return () => { window.removeEventListener('scroll', onScroll); window.removeEventListener('resize', onScroll); if (frameRef.current) cancelAnimationFrame(frameRef.current) }
  }, [])

  return meterRef
}


function ProductCapture({ src, label, compact = false }: { src: string; label: string; compact?: boolean }) {
  const [failed, setFailed] = useState(false)
  return <div className={`capture ${compact ? 'capture-compact' : ''}`}>
    {!failed && <img src={src} alt={label} onError={() => setFailed(true)} />}
    {failed && <div className="capture-fallback" aria-label="Real product capture slot">
      <div className="fallback-top"><span className="window-dots"><i /><i /><i /></span><span>OpenThesis · research workspace</span><span className="fallback-dim">{label}</span></div>
      <div className="fallback-body"><div className="fallback-sidebar"><span /><span /><span /><span /></div><div className="fallback-content"><div className="fallback-line wide" /><div className="fallback-line" /><div className="fallback-grid"><span /><span /><span /></div><div className="fallback-line medium" /></div></div>
      <p>Real product capture slot · add approved WebP in <code>public/product/</code></p>
    </div>}
  </div>
}

function Stat({ value, label }: { value: string; label: string }) {
  return <div className="stat"><strong>{value}</strong><span>{label}</span></div>
}

function IconBadge({ icon: IconComponent, tone = 'coral' }: { icon: Icon; tone?: 'coral' | 'muted' }) {
  return <span className={`icon-badge ${tone}`}><IconComponent size={18} strokeWidth={1.7} /></span>
}

function App() {
  const [language, setLanguage] = useState(resolveInitialLanguage)
  const [usesSystemLanguage, setUsesSystemLanguage] = useState(() => storedLanguage() === null)
  const [menuOpen, setMenuOpen] = useState(false)
  const meterRef = useScrollMeter()
  const storyRef = useRef<HTMLDivElement>(null)
  const t: Copy = copy[language]

  useEffect(() => {
    applyLanguageMetadata(language)
    if (!usesSystemLanguage) {
      try { window.localStorage.setItem(LANGUAGE_STORAGE_KEY, language) } catch { /* optional */ }
    }
  }, [language, usesSystemLanguage])

  useEffect(() => storyRef.current ? mountStoryTimeline(storyRef.current) : undefined, [])

  const navItems = useMemo(() => [
    ['product', t.nav.product], ['workflow', t.nav.workflow], ['markets', t.nav.markets], ['privacy', t.nav.privacy]
  ] as const, [t])

  const changeLanguage = () => {
    setUsesSystemLanguage(false)
    setLanguage((current) => current === 'en' ? 'zh' : 'en')
  }
  const useSystemLanguage = () => {
    try { window.localStorage.removeItem(LANGUAGE_STORAGE_KEY) } catch { /* optional */ }
    setUsesSystemLanguage(true)
    setLanguage(resolveSystemLanguage())
  }
  const closeMenu = () => setMenuOpen(false)

  return <div className={`site ${language === 'zh' ? 'is-zh' : 'is-en'}`}>
    <div className="scroll-meter" aria-hidden="true"><span ref={meterRef} /></div>
    <header className={`site-header ${menuOpen ? 'menu-open' : ''}`}>
      <a className="brand" href="#top" aria-label="OpenThesis home"><span className="brand-mark"><span /></span><span>OpenThesis</span></a>
      <nav className="desktop-nav" aria-label="Primary navigation">
        {navItems.map(([id, label]) => <a href={`#${id}`} key={id}>{label}</a>)}
        <a href="/ot/">{t.nav.ot}</a>
        <a href="https://github.com/zjy1346/OpenThesis" target="_blank" rel="noreferrer">{t.nav.github}<ExternalLink size={13} /></a>
      </nav>
      <div className="header-actions">
        <div className="language-actions"><button className="language-toggle" onClick={changeLanguage} aria-label={`Switch language to ${t.switchLanguage}`}><Languages size={15} /> {t.switchLanguage}</button><button className="system-language" onClick={useSystemLanguage}>{t.followSystem}</button></div>
        <a className="header-download" href="https://github.com/zjy1346/OpenThesis/releases/latest" target="_blank" rel="noreferrer">{t.nav.download}<ArrowDownRight size={15} /></a>
        <button className="menu-toggle" onClick={() => setMenuOpen((current) => !current)} aria-expanded={menuOpen} aria-label={menuOpen ? t.close : t.menu}>{menuOpen ? <X size={20} /> : <Menu size={20} />}</button>
      </div>
      {menuOpen && <nav className="mobile-nav" aria-label="Mobile navigation">{navItems.map(([id, label]) => <a href={`#${id}`} onClick={closeMenu} key={id}>{label}<ArrowRight size={16} /></a>)}<a href="/ot/" onClick={closeMenu}>{t.nav.ot}<ArrowRight size={16} /></a><a href="https://github.com/zjy1346/OpenThesis" target="_blank" rel="noreferrer">{t.nav.github}<ExternalLink size={16} /></a></nav>}
    </header>

    <main id="top">
      <section className="hero section-shell" aria-labelledby="hero-title">
        <div className="hero-noise" aria-hidden="true"><span className="noise-row">SEC EDGAR · XBRL · HKEX · CNINFO · 10-K · 20-F · 002594.SZ · 00700.HK</span><span className="noise-row offset">REVENUE / EVIDENCE / ASSUMPTION / RESEARCH / THESIS / RISK</span><span className="noise-row">PERIOD · SCOPE · CURRENCY · PAGE · SOURCE · CHECKED</span></div>
        <div className="hero-portal" aria-hidden="true"><div className="hero-portal-image"><img src={language === 'zh' ? '/product/byd-evidence-zh.webp' : '/product/byd-report-en.webp'} alt="" /></div></div>
        <div className="hero-content">
          <p className="eyebrow">{t.eyebrow}</p>
          <h1 id="hero-title">{t.heroTitle}</h1>
          <p className="hero-body">{t.heroBody}</p>
          <div className="hero-actions"><a className="button button-primary" href="https://github.com/zjy1346/OpenThesis/releases/latest" target="_blank" rel="noreferrer">{t.download}<ArrowDownRight size={17} /></a><a className="button button-quiet" href="https://github.com/zjy1346/OpenThesis" target="_blank" rel="noreferrer"><GitBranch size={17} />{t.github}</a></div>
          <div className="hero-proof">{t.proof.map((item, index) => <span key={item}><Check size={14} /> {item}{index < t.proof.length - 1 && <i />}</span>)}</div>
        </div>
        <div className="hero-side"><span className="side-vertical">{t.sideNote}</span><span className="side-index">01 <i /> 06</span></div>
        <a className="scroll-cue" href="#markets"><span>{t.scroll}</span><ChevronDown size={17} /></a>
      </section>

      <div className="story-film" ref={storyRef} id="story-film"><div className="story-pin">
      <section id="markets" className="chapter story-scene chapter-markets section-shell" aria-labelledby="markets-title">
        <div className="chapter-stage market-stage"><svg className="market-paths" viewBox="0 0 560 560" aria-hidden="true"><path data-story-path d="M20 104 C130 72 174 198 280 280" /><path data-story-path d="M30 430 C132 448 186 330 280 280" /><path data-story-path d="M540 116 C430 94 385 188 280 280" /></svg><div className="stage-orbit" aria-hidden="true"><span className="orbit-line orbit-line-a" /><span className="orbit-line orbit-line-b" /><span className="orbit-core">OT</span><span className="ticker ticker-us">AAPL · SEC</span><span className="ticker ticker-cn">002594.SZ · CNINFO</span><span className="ticker ticker-hk">00700.HK · HKEX</span></div><div className="stage-caption"><Globe2 size={16} /> {t.marketFoot}</div></div>
        <div className="chapter-copy"><p className="eyebrow">{t.marketKicker}</p><h2 id="markets-title">{t.marketTitle}</h2><p>{t.marketBody}</p><div className="market-list"><div><span className="market-code">US</span><span><strong>NYSE · NASDAQ</strong><small>SEC EDGAR / XBRL</small></span><Check size={16} /></div><div><span className="market-code">A</span><span><strong>SH · SZ · BJ</strong><small>巨潮资讯 / 官方披露</small></span><Check size={16} /></div><div><span className="market-code">HK</span><span><strong>Main Board · GEM</strong><small>HKEX / 披露易</small></span><Check size={16} /></div></div></div>
      </section>

      <section id="product" className="chapter story-scene chapter-evidence section-shell" aria-labelledby="evidence-title">
        <div className="chapter-copy"><p className="eyebrow">{t.evidenceKicker}</p><h2 id="evidence-title">{t.evidenceTitle}</h2><p>{t.evidenceBody}</p><div className="evidence-chain"><span className="chain-node active"><FileCheck2 size={17} /><b>10-K / 2025</b><small>{t.source}</small></span><ArrowRight size={18} /><span className="chain-node"><Fingerprint size={17} /><b>Page 42</b><small>Evidence ID · E-014</small></span><ArrowRight size={18} /><span className="chain-node"><ShieldCheck size={17} /><b>Quality gate</b><small>Period · scope · unit</small></span></div><p className="section-foot">{t.evidenceFoot}</p></div>
        <div className="chapter-stage evidence-stage"><div data-story-reveal><ProductCapture src={language === 'zh' ? '/product/byd-evidence-zh.webp' : '/product/byd-report-en.webp'} label={language === 'zh' ? 'BYD 01211.HK evidence capture' : 'BYD 01211.HK report capture'} /></div><div className="source-focus" aria-label={language === 'zh' ? '放大的证据来源区域' : 'Magnified evidence source region'}><span className="source-focus-label"><Fingerprint size={13} />{language === 'zh' ? '证据来源 · 原始披露' : 'Evidence source · original filing'}</span><div className="source-focus-crop" aria-hidden="true"><img src="/product/byd-evidence-zh.webp" alt="" /></div></div><div className="capture-note"><CircleDot size={13} /> {language === 'zh' ? '真实公司截图 · 比亚迪 01211.HK · VERIFIED' : 'Real company capture · BYD 01211.HK · VERIFIED'}</div></div>
      </section>

      <section className="chapter story-scene chapter-agents section-shell" aria-labelledby="agents-title">
        <div className="chapter-stage agent-stage"><svg className="agent-paths" viewBox="0 0 500 420" aria-hidden="true"><path data-story-path d="M40 104 C142 104 152 180 224 210" /><path data-story-path d="M40 210 C140 210 154 210 224 210" /><path data-story-path d="M40 316 C142 316 152 240 224 210" /></svg><div className="agent-window"><div className="agent-window-head"><span><Sparkles size={15} /> Controlled research set</span><span className="live-pill"><i /> LIVE</span></div><div className="agent-row" data-story-focus><IconBadge icon={BarChart3} /><span><b>Financial analysis</b><small>Numbers stay deterministic</small></span><em>done</em></div><div className="agent-row" data-story-focus><IconBadge icon={Scale} tone="muted" /><span><b>Business & competition</b><small>Reads the same evidence</small></span><em>done</em></div><div className="agent-row" data-story-focus><IconBadge icon={ShieldCheck} tone="muted" /><span><b>Accounting risk</b><small>Challenges the quality gate</small></span><em>done</em></div><div className="agent-merge"><span className="merge-line" /><div><Code2 size={16} /><b>Reverse DCF · calculated by program</b></div></div></div><div className="stage-caption"><CircleGauge size={16} /> {t.agentsFoot}</div></div>
        <div className="chapter-copy"><p className="eyebrow">{t.agentsKicker}</p><h2 id="agents-title">{t.agentsTitle}</h2><p>{t.agentsBody}</p><div className="metric-pair"><Stat value="7" label={language === 'zh' ? '个专业 Agent' : 'specialist agents'} /><Stat value="0" label={language === 'zh' ? '模型自由计算' : 'model-owned calculations'} /></div></div>
      </section>

      <section id="workflow" className="chapter story-scene chapter-workflow section-shell" aria-labelledby="workflow-title">
        <div className="chapter-copy"><p className="eyebrow">{t.workflowKicker}</p><h2 id="workflow-title">{t.workflowTitle}</h2><p>{t.workflowBody}</p><p className="section-foot">{t.workflowFoot}</p><div className="workflow-actions"><span><Play size={14} fill="currentColor" /> 12 / 13 phases</span><span><Square size={11} fill="currentColor" /> Cancel safely</span></div></div>
        <div className="chapter-stage workflow-stage"><div className="workflow-report"><ProductCapture src={language === 'zh' ? '/product/byd-report-zh.webp' : '/product/byd-report-en.webp'} label={language === 'zh' ? '完整比亚迪 01211.HK 报告界面' : 'Complete BYD 01211.HK report workspace'} /></div><div className="workflow-progress"><div className="workflow-progress-head"><span><CircleDot size={12} />{language === 'zh' ? '实时研究状态' : 'Live research status'}</span><strong>12 / 13</strong></div><div className="progress-float"><ProductCapture src={language === 'zh' ? '/product/stages-zh.webp' : '/product/stages-en.webp'} label={language === 'zh' ? '完整真实研究进度界面' : 'Complete real research progress view'} compact /></div></div></div>
      </section>

      <section className="chapter story-scene chapter-recovery section-shell" aria-labelledby="recovery-title">
        <div className="chapter-stage recovery-stage"><svg className="recovery-path" viewBox="0 0 560 330" aria-hidden="true"><path data-story-path d="M62 88 C194 88 218 160 300 165 C382 170 402 244 500 244" /></svg><div className="report-window"><div className="report-head"><span><BookOpenCheck size={15} /> {language === 'zh' ? '研究报告 · 部分报告' : 'Research report · partial'}</span><span className="status-warning">{language === 'zh' ? '综合失败' : 'Synthesis failed'}</span></div><div className="report-columns"><div className="report-nav"><span className="selected" /><span /><span /><span /><span /></div><div className="report-main"><div className="report-heading" /><div className="report-text" /><div className="report-text short" /><div className="report-gap"><RotateCcw size={15} /><span><b>{t.reportReady}</b><small>{language === 'zh' ? '增长机会阶段可以单独重试' : 'Growth stage can be retried independently'}</small></span><span className="report-retry">{t.retry}<ArrowRight size={13} /></span></div></div></div></div></div>
        <div className="chapter-copy"><p className="eyebrow">{t.recoveryKicker}</p><h2 id="recovery-title">{t.recoveryTitle}</h2><p>{t.recoveryBody}</p><div className="recovery-checks"><span><Check size={15} /> {language === 'zh' ? '保留已完成阶段' : 'Completed work stays'}</span><span><Check size={15} /> {language === 'zh' ? '说明真实失败原因' : 'Failure reason is explicit'}</span><span><Check size={15} /> {language === 'zh' ? '定向重试' : 'Targeted retry only'}</span></div></div>
      </section>

      <section id="privacy" className="chapter story-scene chapter-local section-shell" aria-labelledby="local-title">
        <div className="chapter-copy"><p className="eyebrow">{t.localKicker}</p><h2 id="local-title">{t.localTitle}</h2><p>{t.localBody}</p><p className="section-foot">{t.localFoot}</p><div className="privacy-list"><span><KeyRound size={16} /> <b>{language === 'zh' ? '系统凭据库' : 'OS credential vault'}</b><small>{language === 'zh' ? '不写入 SQLite、日志或 .ot' : 'Never written to SQLite, logs or .ot'}</small></span><span><History size={16} /> <b>{language === 'zh' ? '.ot 不携带秘密' : '.ot carries no secrets'}</b><small>{language === 'zh' ? '只引用本机模型配置与能力' : 'References local model profiles and capabilities only'}</small></span></div></div>
        <div className="chapter-stage local-stage"><div className="local-stack"><div className="local-card local-model"><div className="local-card-head"><span><Zap size={15} /> {language === 'zh' ? '模型中心' : 'Model Center'}</span><span className="online-dot">ready</span></div><div className="model-pill"><span className="model-avatar">DS</span><b>DeepSeek</b><small>{language === 'zh' ? '已配置连接' : 'Configured connection'}</small><Check size={15} /></div><div className="model-pill muted"><span className="model-avatar">OL</span><b>Ollama</b><small>{language === 'zh' ? '连接已有本地服务' : 'Connect an existing local service'}</small><Check size={15} /></div></div><div className="local-card local-history"><div className="local-card-head"><span><Database size={15} /> {language === 'zh' ? '本地历史' : 'Local history'}</span><History size={15} /></div><div className="history-row"><span className="history-date">Apr 28</span><span><b>BYD · 01211.HK</b><small>Research run · official filings</small></span><ArrowRight size={14} /></div><div className="history-row faded"><span className="history-date">Mar 14</span><span><b>AAPL · NASDAQ</b><small>Research run · official filings</small></span><ArrowRight size={14} /></div></div></div><div className="stage-caption"><LockKeyhole size={16} /> {t.localFoot}</div></div>
      </section>

      <section id="ot-ecosystem" className="chapter story-scene chapter-ot section-shell" aria-labelledby="ot-summary-title">
        <div className="chapter-stage ot-summary-stage">
          <svg className="ot-summary-paths" viewBox="0 0 560 430" aria-hidden="true"><path data-story-path d="M34 86 C148 86 168 166 278 216" /><path data-story-path d="M34 344 C148 344 170 266 278 216" /><path data-story-path d="M526 92 C424 92 388 166 278 216" /><path data-story-path d="M526 338 C424 338 386 264 278 216" /></svg>
          <a className="ot-summary-object" href="/ot/" aria-label={t.otObjectLabel}>
            <div className="ot-summary-head"><span className="ot-summary-mark">.ot</span><span>OPEN RESEARCH OBJECT</span><FileCheck2 size={17} /></div>
            <div className="ot-summary-layers">{['workflow', 'evidence', 'calculations', 'claims', 'report'].map((item, index) => <span key={item} data-ot-summary-layer><i>0{index + 1}</i><b>{item}</b><Check size={13} /></span>)}</div>
            <div className="ot-summary-verified"><Fingerprint size={15} /><span>SHA256 · VERIFIED</span><ArrowRight size={15} /></div>
          </a>
          <div className="ot-summary-signals">{t.otSignals.map((signal) => <span key={signal} data-ot-summary-signal>{signal}</span>)}</div>
          <div className="stage-caption"><Layers3 size={16} /> {t.otFoot}</div>
        </div>
        <div className="chapter-copy"><p className="eyebrow">{t.otKicker}</p><h2 id="ot-summary-title">{t.otTitle}</h2><p>{t.otBody}</p><p className="section-foot">{t.otFoot}</p><a className="button button-quiet ot-summary-cta" href="/ot/">{t.otCta}<ArrowRight size={16} /></a></div>
      </section>
      </div></div>

      <section className="capabilities section-shell" aria-labelledby="capabilities-title"><div className="capabilities-intro"><p className="eyebrow">{language === 'zh' ? '全能力盘点' : 'THE COMPLETE INVENTORY'}</p><h2 id="capabilities-title">{t.capabilities}</h2><p>{t.capabilitiesBody}</p></div><div className="capability-grid">{t.capabilityGroups.map((group, index) => <div className="capability-group" key={group.title}><span className="group-number">0{index + 1}</span><h3>{group.title}</h3>{group.items.map((item) => <span key={item}><Check size={14} />{item}</span>)}</div>)}</div></section>

      <section className="final-cta section-shell" aria-labelledby="final-title"><div className="cta-glow" aria-hidden="true" /><p className="eyebrow">{t.finalKicker}</p><h2 id="final-title">{t.finalTitle}</h2><p>{t.finalBody}</p><div className="hero-actions"><a className="button button-primary" href="https://github.com/zjy1346/OpenThesis/releases/latest" target="_blank" rel="noreferrer">{t.finalDownload}<Download size={17} /></a><a className="button button-quiet" href="https://github.com/zjy1346/OpenThesis" target="_blank" rel="noreferrer"><GitBranch size={17} />{t.finalGithub}</a></div><p className="license-note">{t.apache}</p></section>
    </main>
    <footer className="site-footer"><div className="footer-brand"><a className="brand" href="#top"><span className="brand-mark"><span /></span><span>OpenThesis</span></a><p>{t.sideNote}</p></div><div className="footer-links"><a href="https://github.com/zjy1346/OpenThesis" target="_blank" rel="noreferrer">GitHub <ExternalLink size={13} /></a><a href="https://github.com/zjy1346/OpenThesis/releases/latest" target="_blank" rel="noreferrer">{t.nav.download} <ArrowDownRight size={13} /></a><button onClick={changeLanguage}><Languages size={14} /> {t.switchLanguage}</button><button onClick={useSystemLanguage}>{t.followSystem}</button></div><div className="footer-bottom"><span>© {new Date().getFullYear()} OpenThesis</span><span>{t.disclaimer}</span></div></footer>
  </div>
}

export default App
