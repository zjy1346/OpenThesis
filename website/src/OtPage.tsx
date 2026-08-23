import { useEffect, useRef, useState } from 'react'
import {
  ArrowDownRight, ArrowLeft, ArrowRight, Boxes, Braces, Check, Code2, ExternalLink,
  FileCheck2, Fingerprint, GitBranch, KeyRound, Languages, LockKeyhole, Network,
  PackageCheck, ShieldCheck, Terminal, WifiOff, Workflow
} from 'lucide-react'
import {
  LANGUAGE_STORAGE_KEY, resolveInitialLanguage, resolveSystemLanguage, storedLanguage
} from './language'
import { mountOtTimeline } from './motion/otTimeline'

const copy = {
  en: {
    switchLanguage: '中文',
    systemLanguage: 'Use system language',
    home: 'OpenThesis',
    nav: ['Anatomy', 'Lifecycle', 'Packages', 'Boundaries'],
    heroKicker: 'THE OPEN RESEARCH OBJECT',
    heroTitle: <>Research that<br /><em>travels with its proof.</em></>,
    heroBody: '.ot is the typed, versioned and verifiable research object at the center of OpenThesis. It keeps data, rules, evidence and reports portable without carrying credentials or arbitrary code.',
    heroPrimary: 'Explore the object',
    heroSecondary: 'View source',
    heroProof: ['Typed schema', 'Deterministic hash', 'Explicit permissions'],
    scroll: 'Scroll through the object',
    anatomyKicker: '01 / OBJECT ANATOMY',
    anatomyTitle: <>Everything has<br /><em>a declared place.</em></>,
    anatomyBody: 'A stable manifest describes identity, compatibility, permissions and budgets. Content-addressed resources keep evidence, calculations and reports inspectable across tools.',
    anatomyNote: 'The same bytes produce the same identity.',
    lifecycleKicker: '02 / LIFECYCLE',
    lifecycleTitle: <>Create once.<br /><em>Verify everywhere.</em></>,
    lifecycleBody: 'OT Studio produces a deterministic structure, validation checks its schema and meaning, and the compiler emits one portable object for OpenThesis, the SDK, CLI and compatible tools.',
    lifecycleStages: ['OT Studio', 'Validate', 'Compile', '.ot object', 'OpenThesis · SDK · CLI'],
    lifecycleStatus: ['Structured authoring', 'Schema + semantics', 'Deterministic package', 'Hash + signature', 'One verified object'],
    packagesKicker: '03 / PACKAGE ECOSYSTEM',
    packagesTitle: <>One format.<br /><em>Six useful shapes.</em></>,
    packagesBody: 'The container adapts to the work while its trust model stays consistent.',
    packages: [
      ['Research pack', 'Reusable workflows, prompts and rules'],
      ['Research run', 'A complete, replayable research execution'],
      ['Evidence bundle', 'Sources, facts and traceable claims'],
      ['Financial dataset', 'Normalized facts and calculations'],
      ['Report bundle', 'Report, evidence and validation result'],
      ['Workspace snapshot', 'A portable, inspectable workspace state']
    ],
    boundaryKicker: '04 / TRUST BOUNDARIES',
    boundaryTitle: <>Data can move.<br /><em>Secrets do not.</em></>,
    boundaryBody: '.ot is data-only by default. It carries no API keys, arbitrary scripts or hidden network access. Connectors require explicit permission, and installation, opening, export and sharing all pass the same security checks.',
    boundaries: [
      ['No arbitrary code', 'No Python, JavaScript, shell or dynamic libraries'],
      ['No embedded secrets', 'Only local profile aliases and capability requirements'],
      ['Network off by default', 'Controlled connectors require explicit authorization'],
      ['Verifiable supply chain', 'Hashes, signatures, revocation and isolated validation']
    ],
    studioKicker: '05 / OT STUDIO',
    studioTitle: <>Structured for clarity.<br /><em>Open for experts.</em></>,
    studioBody: 'OT Studio combines guided authoring, professional controls, offline work and model-assisted editing. Every save is validated, and every change can be inspected before it becomes part of the object.',
    studioModes: ['Guided', 'Professional', 'Offline', 'Model-assisted'],
    ctaKicker: 'OPEN SOURCE · INTEROPERABLE · VERIFIABLE',
    ctaTitle: <>Keep the research.<br /><em>Keep the proof.</em></>,
    ctaBody: 'Use OpenThesis to create, inspect and carry verifiable research across the open .ot ecosystem.',
    ctaDownload: 'Download OpenThesis',
    ctaGithub: 'Read the source',
    footer: 'Apache License 2.0 · Data-only by default · Credentials stay local'
  },
  zh: {
    switchLanguage: 'English',
    systemLanguage: '跟随系统语言',
    home: 'OpenThesis',
    nav: ['对象结构', '生命周期', '包生态', '安全边界'],
    heroKicker: '开放研究对象',
    heroTitle: <>让研究流转，<br /><em>让证据始终随行。</em></>,
    heroBody: '.ot 是 OpenThesis 生态中的类型化、版本化、可验证研究对象。它让数据、规则、证据与报告跨工具流转，同时不携带凭据或任意代码。',
    heroPrimary: '查看对象结构',
    heroSecondary: '查看源代码',
    heroProof: ['类型化 Schema', '确定性哈希', '明确权限'],
    scroll: '向下查看研究对象',
    anatomyKicker: '01 / 对象结构',
    anatomyTitle: <>每一份内容，<br /><em>都有明确位置。</em></>,
    anatomyBody: '稳定的 Manifest 声明身份、兼容性、权限与资源预算；内容寻址资源让证据、计算与报告在不同工具中仍然可检查。',
    anatomyNote: '相同字节，始终得到相同身份。',
    lifecycleKicker: '02 / 生命周期',
    lifecycleTitle: <>一次创建，<br /><em>处处验证。</em></>,
    lifecycleBody: 'OT Studio 生成确定性的结构模型，验证器检查 Schema 与语义，编译器输出一个可供 OpenThesis、SDK、CLI 和兼容工具读取的研究对象。',
    lifecycleStages: ['OT Studio', '验证', '编译', '.ot 对象', 'OpenThesis · SDK · CLI'],
    lifecycleStatus: ['结构化创作', 'Schema + 语义', '确定性打包', '哈希 + 签名', '同一个可验证对象'],
    packagesKicker: '03 / 包生态',
    packagesTitle: <>一种格式，<br /><em>六种研究形态。</em></>,
    packagesBody: '容器随研究任务变化，但信任模型始终一致。',
    packages: [
      ['研究包', '可复用工作流、提示词与规则'],
      ['研究运行', '完整、可重放的研究执行'],
      ['证据束', '来源、事实与可追溯论点'],
      ['财务数据集', '规范化事实与确定性计算'],
      ['报告包', '报告、证据与验证结果'],
      ['工作区快照', '可携带、可检查的工作区状态']
    ],
    boundaryKicker: '04 / 信任边界',
    boundaryTitle: <>数据可以流转，<br /><em>秘密不会随行。</em></>,
    boundaryBody: '.ot 默认只承载数据，不携带 API Key、任意脚本或隐藏网络访问。Connector 必须获得明确权限，安装、打开、导出与分享都经过同一套安全检查。',
    boundaries: [
      ['不执行任意代码', '不包含 Python、JavaScript、Shell 或动态库'],
      ['不嵌入秘密', '只声明本机配置别名与模型能力要求'],
      ['网络默认关闭', '受控 Connector 必须得到明确授权'],
      ['可验证供应链', '哈希、签名、撤销与隔离验证']
    ],
    studioKicker: '05 / OT STUDIO',
    studioTitle: <>新手有引导，<br /><em>专家有控制。</em></>,
    studioBody: 'OT Studio 集成引导式创作、专业控制、离线工作与模型辅助编辑。每次保存都会验证，每项变化都能在写入对象前清楚检查。',
    studioModes: ['引导模式', '专业模式', '离线模式', '模型辅助'],
    ctaKicker: '开源 · 可互操作 · 可验证',
    ctaTitle: <>保留研究，<br /><em>也保留证明。</em></>,
    ctaBody: '使用 OpenThesis 创建、检查并携带可验证研究，让它在开放的 .ot 生态中持续流转。',
    ctaDownload: '下载 OpenThesis',
    ctaGithub: '阅读源代码',
    footer: 'Apache License 2.0 · 默认只承载数据 · 凭据留在本机'
  }
} as const

const tree = [
  ['manifest.json', 'identity · compatibility · permissions'],
  ['resources/', 'workflow · evidence · calculations · report'],
  ['blobs/sha256/', 'content-addressed assets'],
  ['schemas/', 'versioned validation contracts'],
  ['signatures/', 'integrity · publisher · revocation']
]

function OtPage() {
  const [language, setLanguage] = useState(resolveInitialLanguage)
  const [usesSystemLanguage, setUsesSystemLanguage] = useState(() => storedLanguage() === null)
  const rootRef = useRef<HTMLDivElement>(null)
  const t = copy[language]

  useEffect(() => {
    document.documentElement.lang = language === 'zh' ? 'zh-CN' : 'en'
    document.title = language === 'zh'
      ? 'OpenThesis .ot — 可验证研究对象'
      : 'OpenThesis .ot — The verifiable research object'
    const description = language === 'zh'
      ? '.ot 让研究数据、规则、证据与报告在 OpenThesis 生态中可携带、可验证地流转。'
      : '.ot carries typed, versioned and verifiable research across the OpenThesis ecosystem.'
    document.querySelector<HTMLMetaElement>('meta[name="description"]')?.setAttribute('content', description)
    if (!usesSystemLanguage) {
      try { window.localStorage.setItem(LANGUAGE_STORAGE_KEY, language) } catch { /* optional */ }
    }
  }, [language, usesSystemLanguage])

  useEffect(() => rootRef.current ? mountOtTimeline(rootRef.current) : undefined, [])

  const toggleLanguage = () => {
    setUsesSystemLanguage(false)
    setLanguage((current) => current === 'en' ? 'zh' : 'en')
  }
  const useSystemLanguage = () => {
    try { window.localStorage.removeItem(LANGUAGE_STORAGE_KEY) } catch { /* optional */ }
    setUsesSystemLanguage(true)
    setLanguage(resolveSystemLanguage())
  }

  return <div ref={rootRef} className={'ot-site ' + (language === 'zh' ? 'is-zh' : 'is-en')}>
    <header className="ot-header">
      <a className="brand" href="/"><span className="brand-mark"><span /></span><span>{t.home}</span></a>
      <nav className="ot-nav" aria-label="Page navigation">
        {['anatomy', 'lifecycle', 'packages', 'boundaries'].map((id, index) => <a key={id} href={'#' + id}>{t.nav[index]}</a>)}
      </nav>
      <div className="language-actions">
        <button className="language-toggle" onClick={toggleLanguage}><Languages size={14} />{t.switchLanguage}</button>
        {!usesSystemLanguage && <button className="system-language" onClick={useSystemLanguage}>{t.systemLanguage}</button>}
      </div>
    </header>

    <main>
      <section className="ot-hero ot-shell">
        <div className="ot-hero-copy">
          <p className="eyebrow">{t.heroKicker}</p>
          <h1>{t.heroTitle}</h1>
          <p className="ot-lead">{t.heroBody}</p>
          <div className="hero-actions">
            <a className="button button-primary" href="#anatomy">{t.heroPrimary}<ArrowDownRight size={16} /></a>
            <a className="button button-quiet" href="https://github.com/zjy1346/OpenThesis" target="_blank" rel="noreferrer">{t.heroSecondary}<ExternalLink size={15} /></a>
          </div>
          <div className="hero-proof">{t.heroProof.map((item) => <span key={item}><Check size={12} />{item}</span>)}</div>
        </div>

        <div className="ot-object-stage" aria-label=".ot research object diagram">
          <svg className="ot-orbit-lines" viewBox="0 0 620 620" aria-hidden="true">
            <circle cx="310" cy="310" r="234" />
            <circle cx="310" cy="310" r="174" />
            <path data-ot-path d="M102 412 C188 352 196 180 314 142 C421 107 492 198 535 270" />
            <path data-ot-path d="M90 246 C205 286 319 420 522 393" />
          </svg>
          <article className="ot-object" data-ot-object>
            <div className="ot-object-head"><span>.ot</span><span>RESEARCH OBJECT</span><Fingerprint size={17} /></div>
            <div className="ot-object-body">
              <p><span>type</span><b>research-pack</b></p>
              <p><span>schema</span><b>versioned</b></p>
              <p><span>content</span><b>addressed</b></p>
              <p><span>network</span><b>off · default</b></p>
            </div>
            <div className="ot-hash"><ShieldCheck size={15} /> sha256 · verified</div>
          </article>
          <span className="ot-node node-schema">SCHEMA</span>
          <span className="ot-node node-hash">HASH</span>
          <span className="ot-node node-permission">PERMISSIONS</span>
          <span className="ot-node node-budget">BUDGET</span>
          <p className="ot-scroll-label"><ArrowDownRight size={14} />{t.scroll}</p>
        </div>
      </section>

      <section id="anatomy" className="ot-section ot-shell ot-anatomy">
        <div className="ot-section-copy" data-ot-reveal>
          <p className="eyebrow">{t.anatomyKicker}</p>
          <h2>{t.anatomyTitle}</h2>
          <p>{t.anatomyBody}</p>
          <p className="section-foot">{t.anatomyNote}</p>
        </div>
        <div className="ot-tree" data-ot-reveal>
          <div className="tree-root"><PackageCheck size={18} /><b>company-research.ot</b><span>VERIFIED</span></div>
          {tree.map(([name, detail], index) => <div className="tree-row" key={name} data-ot-layer>
            <span className="tree-line" /><span className="tree-index">0{index + 1}</span><Code2 size={15} /><b>{name}</b><small>{detail}</small>
          </div>)}
        </div>
      </section>

      <section id="lifecycle" className="ot-section ot-shell ot-lifecycle">
        <div className="ot-section-copy" data-ot-reveal>
          <p className="eyebrow">{t.lifecycleKicker}</p>
          <h2>{t.lifecycleTitle}</h2>
          <p>{t.lifecycleBody}</p>
        </div>
        <div className="lifecycle-rail" data-ot-reveal>
          <svg viewBox="0 0 900 110" preserveAspectRatio="none" aria-hidden="true"><path data-ot-path d="M35 55 C210 55 245 55 390 55 S670 55 865 55" /></svg>
          {t.lifecycleStages.map((stage, index) => <article key={stage} className="lifecycle-step" data-ot-layer>
            <span>{String(index + 1).padStart(2, '0')}</span>
            {index === 0 ? <Workflow size={18} /> : index === 1 ? <FileCheck2 size={18} /> : index === 2 ? <Terminal size={18} /> : index === 3 ? <Fingerprint size={18} /> : <Network size={18} />}
            <b>{stage}</b><small>{t.lifecycleStatus[index]}</small>
          </article>)}
        </div>
      </section>

      <section id="packages" className="ot-section ot-shell ot-packages">
        <div className="ot-section-copy" data-ot-reveal>
          <p className="eyebrow">{t.packagesKicker}</p>
          <h2>{t.packagesTitle}</h2>
          <p>{t.packagesBody}</p>
        </div>
        <div className="package-grid">
          {t.packages.map(([title, detail], index) => <article key={title} data-ot-card>
            <span>0{index + 1}</span><Boxes size={20} /><h3>{title}</h3><p>{detail}</p><ArrowRight size={15} />
          </article>)}
        </div>
      </section>

      <section id="boundaries" className="ot-section ot-shell ot-boundaries">
        <div className="ot-section-copy" data-ot-reveal>
          <p className="eyebrow">{t.boundaryKicker}</p>
          <h2>{t.boundaryTitle}</h2>
          <p>{t.boundaryBody}</p>
        </div>
        <div className="boundary-core" data-ot-reveal>
          <div className="boundary-center"><LockKeyhole size={27} /><b>.ot</b><span>DATA · NOT SECRETS</span></div>
          {t.boundaries.map(([title, detail], index) => <article key={title} className={'boundary-item boundary-' + index} data-ot-card>
            {index === 0 ? <Braces size={18} /> : index === 1 ? <KeyRound size={18} /> : index === 2 ? <WifiOff size={18} /> : <GitBranch size={18} />}
            <h3>{title}</h3><p>{detail}</p>
          </article>)}
        </div>
      </section>

      <section className="ot-section ot-shell ot-studio">
        <div className="ot-section-copy" data-ot-reveal>
          <p className="eyebrow">{t.studioKicker}</p>
          <h2>{t.studioTitle}</h2>
          <p>{t.studioBody}</p>
        </div>
        <div className="studio-dial" data-ot-reveal>
          <div className="studio-ring"><span>OT</span><b>STUDIO</b></div>
          <div className="studio-modes">{t.studioModes.map((mode, index) => <span key={mode} data-ot-layer><i>0{index + 1}</i>{mode}<Check size={13} /></span>)}</div>
        </div>
      </section>

      <section className="ot-cta ot-shell" data-ot-reveal>
        <p className="eyebrow">{t.ctaKicker}</p>
        <h2>{t.ctaTitle}</h2>
        <p>{t.ctaBody}</p>
        <div className="hero-actions">
          <a className="button button-primary" href="https://github.com/zjy1346/OpenThesis/releases/latest" target="_blank" rel="noreferrer">{t.ctaDownload}<ArrowDownRight size={16} /></a>
          <a className="button button-quiet" href="https://github.com/zjy1346/OpenThesis" target="_blank" rel="noreferrer">{t.ctaGithub}<ExternalLink size={15} /></a>
        </div>
        <small>{t.footer}</small>
      </section>
    </main>

    <footer className="ot-footer ot-shell">
      <a href="/"><ArrowLeft size={14} />{t.home}</a>
      <span>.ot · OPEN RESEARCH OBJECT</span>
    </footer>
  </div>
}

export default OtPage
