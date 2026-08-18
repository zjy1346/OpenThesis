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
    nav: { product: 'Product', workflow: 'Workflow', markets: 'Markets', privacy: 'Privacy', github: 'GitHub', download: 'Download' },
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
    localKicker: '06 / YOUR WORKBENCH',
    localTitle: <><span className="headline-line">Your models.</span><br /><span className="headline-line"><em>Your history.</em></span></>,
    localBody: 'Bring the model you trust, or use deterministic mode. API keys stay in session memory. Research history and append-only thesis versions stay on your machine.',
    localFoot: 'A private trail for public evidence.',
    finalKicker: 'OPEN SOURCE COMPANY RESEARCH',
    finalTitle: <>Take the long view<br /><em>seriously.</em></>,
    finalBody: 'Download the Windows portable build, inspect the source, and build a research practice that compounds.',
    finalDownload: 'Download for Windows',
    finalGithub: 'Read the source',
    apache: 'Apache License 2.0 · No account · No broker connection',
    disclaimer: 'OpenThesis is a research tool, not investment advice. It does not execute trades or promise returns.',
    menu: 'Open menu', close: 'Close menu',
    proof: ['Official filings', 'Deterministic finance', 'Focused agents'],
    capabilities: 'The full workbench',
    capabilitiesBody: 'A compact view of the workbench beyond the six stories above.',
    capabilityGroups: [
      { title: 'Markets & sources', items: ['SEC EDGAR + XBRL', 'A-share official disclosures', 'HKEX official disclosures', 'Issuer / security identity'] },
      { title: 'Research system', items: ['Seven specialist agents', 'Second-model comparison', 'Up to two parallel agents', 'Importable .othesis modules'] },
      { title: 'Report & history', items: ['Technical evidence detail', 'HTML / Markdown / text export', '90–130% report zoom', 'Append-only thesis versions'] },
      { title: 'Safety boundaries', items: ['Local-first history', 'Session-only API keys', 'User-approved vision fallback', 'Financial-institution Beta path'] }
    ]
  },
  zh: {
    nav: { product: '产品', workflow: '研究流程', markets: '市场', privacy: '隐私', github: 'GitHub', download: '下载' },
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
    localKicker: '06 / 你的工作台',
    localTitle: <><span className="headline-line">你的模型。</span><br /><span className="headline-line"><em>你的研究历史。</em></span></>,
    localBody: '使用你信任的模型，或直接使用确定性模式。API Key 只留在当前会话内存，研究历史和追加式论点版本留在你的电脑上。',
    localFoot: '为公开证据保留一条私密路径。',
    finalKicker: '开源公司研究',
    finalTitle: <>认真地，<br /><em>看得更远。</em></>,
    finalBody: '下载 Windows 便携版，查看源代码，建立一套会复利的研究习惯。',
    finalDownload: '下载 Windows 便携版',
    finalGithub: '阅读源代码',
    apache: 'Apache License 2.0 · 无账户 · 不连接券商',
    disclaimer: 'OpenThesis 是研究工具，不构成投资建议。不执行交易，也不承诺收益。',
    menu: '打开菜单', close: '关闭菜单',
    proof: ['官方披露', '确定性财务', '专注 Agent'],
    capabilities: '完整工作台',
    capabilitiesBody: '除了上面六段故事，完整工作台还提供这些能力。',
    capabilityGroups: [
      { title: '市场与来源', items: ['SEC EDGAR + XBRL', 'A 股官方披露', '港交所披露易', '发行人 / 证券身份'] },
      { title: '研究系统', items: ['七个专业 Agent', '第二模型对比', '最多两个 Agent 并行', '可导入 .othesis 模块'] },
      { title: '报告与历史', items: ['技术证据详情', 'HTML / Markdown / 文本导出', '90–130% 报告缩放', '追加式论点版本'] },
      { title: '安全边界', items: ['本地优先历史', '会话级 API Key', '用户批准的识图兜底', '金融机构 Beta 路径'] }
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
        <a href="https://github.com/zjy1346/OpenThesis" target="_blank" rel="noreferrer">{t.nav.github}<ExternalLink size={13} /></a>
      </nav>
      <div className="header-actions">
        <div className="language-actions"><button className="language-toggle" onClick={changeLanguage} aria-label={`Switch language to ${t.switchLanguage}`}><Languages size={15} /> {t.switchLanguage}</button><button className="system-language" onClick={useSystemLanguage}>{t.followSystem}</button></div>
        <a className="header-download" href="https://github.com/zjy1346/OpenThesis/releases/latest" target="_blank" rel="noreferrer">{t.nav.download}<ArrowDownRight size={15} /></a>
        <button className="menu-toggle" onClick={() => setMenuOpen((current) => !current)} aria-expanded={menuOpen} aria-label={menuOpen ? t.close : t.menu}>{menuOpen ? <X size={20} /> : <Menu size={20} />}</button>
      </div>
      {menuOpen && <nav className="mobile-nav" aria-label="Mobile navigation">{navItems.map(([id, label]) => <a href={`#${id}`} onClick={closeMenu} key={id}>{label}<ArrowRight size={16} /></a>)}<a href="https://github.com/zjy1346/OpenThesis" target="_blank" rel="noreferrer">{t.nav.github}<ExternalLink size={16} /></a></nav>}
    </header>

    <main id="top">
      <section className="hero section-shell" aria-labelledby="hero-title">
        <div className="hero-noise" aria-hidden="true"><span className="noise-row">SEC EDGAR · XBRL · HKEX · CNINFO · 10-K · 20-F · 002594.SZ · 00700.HK</span><span className="noise-row offset">REVENUE / EVIDENCE / ASSUMPTION / RESEARCH / THESIS / RISK</span><span className="noise-row">PERIOD · SCOPE · CURRENCY · PAGE · SOURCE · CHECKED</span></div>
        <div className="hero-portal" aria-hidden="true"><div className="hero-portal-image"><img src={language === 'zh' ? '/product/byd-evidence-zh.webp' : '/product/byd-report-en.webp'} alt="" /></div><span className="hero-portal-line" /></div>
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
        <div className="chapter-stage evidence-stage"><div data-story-reveal><ProductCapture src={language === 'zh' ? '/product/byd-evidence-zh.webp' : '/product/byd-report-en.webp'} label={language === 'zh' ? 'BYD 01211.HK evidence capture' : 'BYD 01211.HK report capture'} /></div><div className="evidence-magnifier" aria-hidden="true"><img src={language === 'zh' ? '/product/byd-evidence-zh.webp' : '/product/byd-report-en.webp'} alt="" /></div><div className="capture-note"><CircleDot size={13} /> {language === 'zh' ? '真实公司截图 · 比亚迪 01211.HK · VERIFIED' : 'Real company capture · BYD 01211.HK · VERIFIED'}</div></div>
      </section>

      <section className="chapter story-scene chapter-agents section-shell" aria-labelledby="agents-title">
        <div className="chapter-stage agent-stage"><svg className="agent-paths" viewBox="0 0 500 420" aria-hidden="true"><path data-story-path d="M40 104 C142 104 152 180 224 210" /><path data-story-path d="M40 210 C140 210 154 210 224 210" /><path data-story-path d="M40 316 C142 316 152 240 224 210" /></svg><div className="agent-window"><div className="agent-window-head"><span><Sparkles size={15} /> Controlled research set</span><span className="live-pill"><i /> LIVE</span></div><div className="agent-row" data-story-focus><IconBadge icon={BarChart3} /><span><b>Financial analysis</b><small>Numbers stay deterministic</small></span><em>done</em></div><div className="agent-row" data-story-focus><IconBadge icon={Scale} tone="muted" /><span><b>Business & competition</b><small>Reads the same evidence</small></span><em>done</em></div><div className="agent-row" data-story-focus><IconBadge icon={ShieldCheck} tone="muted" /><span><b>Accounting risk</b><small>Challenges the quality gate</small></span><em>done</em></div><div className="agent-merge"><span className="merge-line" /><div><Code2 size={16} /><b>Reverse DCF · calculated by program</b></div></div></div><div className="stage-caption"><CircleGauge size={16} /> {t.agentsFoot}</div></div>
        <div className="chapter-copy"><p className="eyebrow">{t.agentsKicker}</p><h2 id="agents-title">{t.agentsTitle}</h2><p>{t.agentsBody}</p><div className="metric-pair"><Stat value="7" label={language === 'zh' ? '个专业 Agent' : 'specialist agents'} /><Stat value="0" label={language === 'zh' ? '模型自由计算' : 'model-owned calculations'} /></div></div>
      </section>

      <section id="workflow" className="chapter story-scene chapter-workflow section-shell" aria-labelledby="workflow-title">
        <div className="chapter-copy"><p className="eyebrow">{t.workflowKicker}</p><h2 id="workflow-title">{t.workflowTitle}</h2><p>{t.workflowBody}</p><p className="section-foot">{t.workflowFoot}</p><div className="workflow-actions"><span><Play size={14} fill="currentColor" /> 12 / 13 phases</span><span><Square size={11} fill="currentColor" /> Cancel safely</span></div></div>
        <div className="chapter-stage workflow-stage"><div className="workflow-report"><ProductCapture src={language === 'zh' ? '/product/byd-report-zh.webp' : '/product/byd-report-en.webp'} label={language === 'zh' ? '完整比亚迪 01211.HK 报告界面' : 'Complete BYD 01211.HK report workspace'} /></div><div className="progress-float"><ProductCapture src={language === 'zh' ? '/product/stages-zh.webp' : '/product/stages-en.webp'} label={language === 'zh' ? '完整真实研究进度界面' : 'Complete real research progress view'} compact /></div><div className="timeline"><div className="timeline-line"><span /></div>{['Prepare', 'Discover filings', 'Parse & validate', 'Financial agents', 'Synthesis'].map((stage, index) => <div className={`timeline-item ${index < 4 ? 'complete' : 'current'}`} key={stage}><i>{index < 4 ? <Check size={11} /> : <CircleDot size={11} />}</i><span>{language === 'zh' ? ['准备', '发现披露', '解析与校验', '财务 Agent', '综合报告'][index] : stage}</span><small>{index < 4 ? `${index + 1}:0${index + 2}` : 'running'}</small></div>)}</div></div>
      </section>

      <section className="chapter story-scene chapter-recovery section-shell" aria-labelledby="recovery-title">
        <div className="chapter-stage recovery-stage"><svg className="recovery-path" viewBox="0 0 560 330" aria-hidden="true"><path data-story-path d="M62 88 C194 88 218 160 300 165 C382 170 402 244 500 244" /></svg><div className="report-window"><div className="report-head"><span><BookOpenCheck size={15} /> {language === 'zh' ? '研究报告 · 部分报告' : 'Research report · partial'}</span><span className="status-warning">{language === 'zh' ? '综合失败' : 'Synthesis failed'}</span></div><div className="report-columns"><div className="report-nav"><span className="selected" /><span /><span /><span /><span /></div><div className="report-main"><div className="report-heading" /><div className="report-text" /><div className="report-text short" /><div className="report-gap"><RotateCcw size={15} /><span><b>{t.reportReady}</b><small>{language === 'zh' ? '增长机会阶段可以单独重试' : 'Growth stage can be retried independently'}</small></span><span className="report-retry">{t.retry}<ArrowRight size={13} /></span></div></div></div></div></div>
        <div className="chapter-copy"><p className="eyebrow">{t.recoveryKicker}</p><h2 id="recovery-title">{t.recoveryTitle}</h2><p>{t.recoveryBody}</p><div className="recovery-checks"><span><Check size={15} /> {language === 'zh' ? '保留已完成阶段' : 'Completed work stays'}</span><span><Check size={15} /> {language === 'zh' ? '说明真实失败原因' : 'Failure reason is explicit'}</span><span><Check size={15} /> {language === 'zh' ? '定向重试' : 'Targeted retry only'}</span></div></div>
      </section>

      <section id="privacy" className="chapter story-scene chapter-local section-shell" aria-labelledby="local-title">
        <div className="chapter-copy"><p className="eyebrow">{t.localKicker}</p><h2 id="local-title">{t.localTitle}</h2><p>{t.localBody}</p><p className="section-foot">{t.localFoot}</p><div className="privacy-list"><span><KeyRound size={16} /> <b>{language === 'zh' ? '会话级密钥' : 'Session-only keys'}</b><small>{language === 'zh' ? '不写入数据库或日志' : 'Never written to database or logs'}</small></span><span><History size={16} /> <b>{language === 'zh' ? '追加式论点' : 'Append-only thesis'}</b><small>{language === 'zh' ? '每个判断都保留版本' : 'Every judgement keeps a version'}</small></span></div></div>
        <div className="chapter-stage local-stage"><div className="local-stack"><div className="local-card local-model"><div className="local-card-head"><span><Zap size={15} /> {language === 'zh' ? '模型目录' : 'Model catalog'}</span><span className="online-dot">online</span></div><div className="model-pill"><span className="model-avatar">DS</span><b>DeepSeek</b><small>OpenAI-compatible</small><Check size={15} /></div><div className="model-pill muted"><span className="model-avatar">OT</span><b>Deterministic mode</b><small>{language === 'zh' ? '不调用 AI' : 'No AI calls'}</small><Check size={15} /></div></div><div className="local-card local-history"><div className="local-card-head"><span><Database size={15} /> {language === 'zh' ? '本地历史' : 'Local history'}</span><History size={15} /></div><div className="history-row"><span className="history-date">Apr 28</span><span><b>BYD · 01211.HK</b><small>Research run · official filings</small></span><ArrowRight size={14} /></div><div className="history-row faded"><span className="history-date">Mar 14</span><span><b>AAPL · NASDAQ</b><small>Research run · official filings</small></span><ArrowRight size={14} /></div></div></div><div className="stage-caption"><LockKeyhole size={16} /> {t.localFoot}</div></div>
      </section>
      </div></div>

      <section className="capabilities section-shell" aria-labelledby="capabilities-title"><div className="capabilities-intro"><p className="eyebrow">{language === 'zh' ? '全能力盘点' : 'THE COMPLETE INVENTORY'}</p><h2 id="capabilities-title">{t.capabilities}</h2><p>{t.capabilitiesBody}</p></div><div className="capability-grid">{t.capabilityGroups.map((group, index) => <div className="capability-group" key={group.title}><span className="group-number">0{index + 1}</span><h3>{group.title}</h3>{group.items.map((item) => <span key={item}><Check size={14} />{item}</span>)}</div>)}</div></section>

      <section className="final-cta section-shell" aria-labelledby="final-title"><div className="cta-glow" aria-hidden="true" /><p className="eyebrow">{t.finalKicker}</p><h2 id="final-title">{t.finalTitle}</h2><p>{t.finalBody}</p><div className="hero-actions"><a className="button button-primary" href="https://github.com/zjy1346/OpenThesis/releases/latest" target="_blank" rel="noreferrer">{t.finalDownload}<Download size={17} /></a><a className="button button-quiet" href="https://github.com/zjy1346/OpenThesis" target="_blank" rel="noreferrer"><GitBranch size={17} />{t.finalGithub}</a></div><p className="license-note">{t.apache}</p></section>
    </main>
    <footer className="site-footer"><div className="footer-brand"><a className="brand" href="#top"><span className="brand-mark"><span /></span><span>OpenThesis</span></a><p>{t.sideNote}</p></div><div className="footer-links"><a href="https://github.com/zjy1346/OpenThesis" target="_blank" rel="noreferrer">GitHub <ExternalLink size={13} /></a><a href="https://github.com/zjy1346/OpenThesis/releases/latest" target="_blank" rel="noreferrer">{t.nav.download} <ArrowDownRight size={13} /></a><button onClick={changeLanguage}><Languages size={14} /> {t.switchLanguage}</button><button onClick={useSystemLanguage}>{t.followSystem}</button></div><div className="footer-bottom"><span>© {new Date().getFullYear()} OpenThesis</span><span>{t.disclaimer}</span></div></footer>
  </div>
}

export default App
