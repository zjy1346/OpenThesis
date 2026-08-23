import gsap from 'gsap'
import { ScrollTrigger } from 'gsap/ScrollTrigger'
import { mountMobileStoryTimeline } from './mobileStoryTimeline'

gsap.registerPlugin(ScrollTrigger)

export function mountStoryTimeline(root: HTMLElement) {
  const desktopMotion = window.matchMedia('(min-width: 981px) and (prefers-reduced-motion: no-preference)')
  const pin = root.querySelector<HTMLElement>('.story-pin')
  const scenes = gsap.utils.toArray<HTMLElement>('.story-scene', root)
  if (!pin || scenes.length !== 7) return () => undefined
  const teardownMobile = mountMobileStoryTimeline(root, scenes)

  let teardown = () => undefined
  const setup = () => {
    teardown()
    if (!desktopMotion.matches) return
    const context = gsap.context(() => {
      const [market, evidence, agents, workflow, recovery, local, ot] = scenes
      const hero = document.querySelector<HTMLElement>('.hero')
      const heroPortal = document.querySelector<HTMLElement>('.hero-portal')
      if (hero && heroPortal) gsap.fromTo(heroPortal, { opacity: .88, clipPath: 'inset(0 0 0 0)', transform: 'scale(1.04)' }, { opacity: 0, clipPath: 'inset(0 0 100% 0)', transform: 'scale(.86)', ease: 'none', scrollTrigger: { trigger: hero, start: 'top top', end: 'bottom top', scrub: .8 } })
      const timeline = gsap.timeline({ defaults: { ease: 'none' }, scrollTrigger: { trigger: root, pin, start: 'top top', end: '+=700%', scrub: .8, anticipatePin: 1, invalidateOnRefresh: true } })
      gsap.set(scenes, { autoAlpha: 0, pointerEvents: 'none' })
      gsap.set(market, { autoAlpha: 1, pointerEvents: 'auto' })
      const handoff = (scene: HTMLElement, previous: HTMLElement | null, at: number) => {
        if (previous) timeline.to(previous, { autoAlpha: 0, pointerEvents: 'none', duration: .18 }, at)
        timeline.to(scene, { autoAlpha: 1, pointerEvents: 'auto', duration: .18 }, at)
      }

      const orbit = market.querySelector<HTMLElement>('.stage-orbit')
      const marketPaths = market.querySelectorAll<SVGPathElement>('[data-story-path]')
      const tickers = market.querySelectorAll<HTMLElement>('.ticker')
      if (orbit) timeline.fromTo(orbit, { transform: 'rotate(-24deg) scale(.86)' }, { transform: 'rotate(-12deg) scale(1)', duration: .9 }, 0)
      marketPaths.forEach((path) => { const length = path.getTotalLength(); gsap.set(path, { strokeDasharray: length, strokeDashoffset: length }); timeline.to(path, { strokeDashoffset: 0, duration: .58 }, .1) })
      timeline.fromTo(tickers, { opacity: 0, transform: 'translate3d(0, 16px, 0) rotate(12deg)' }, { opacity: 1, transform: 'translate3d(0, 0, 0) rotate(12deg)', stagger: .1, duration: .35 }, .28)
      timeline.fromTo(market.querySelector('.chapter-copy'), { clipPath: 'inset(0 0 0 100%)' }, { clipPath: 'inset(0 0 0 0)', duration: .45 }, .2)

      handoff(evidence, market, 1)
      const evidenceCapture = evidence.querySelector<HTMLElement>('[data-story-reveal]')
      const sourceFocus = evidence.querySelector<HTMLElement>('.source-focus')
      if (evidenceCapture) timeline.fromTo(evidenceCapture, { clipPath: 'inset(0 100% 0 0)', transform: 'scale(1.04)' }, { clipPath: 'inset(0 0 0 0)', transform: 'scale(1)', duration: .72 }, 1.08)
      if (sourceFocus) timeline.fromTo(sourceFocus, { opacity: 0, transform: 'translate3d(0, 16px, 0) scale(.97)' }, { opacity: 1, transform: 'translate3d(0, 0, 0) scale(1)', duration: .4 }, 1.52)
      timeline.fromTo(evidence.querySelectorAll('.chain-node'), { opacity: 0, transform: 'translate3d(0, 12px, 0)' }, { opacity: 1, transform: 'translate3d(0, 0, 0)', stagger: .06, duration: .25 }, 1.48)

      handoff(agents, evidence, 2)
      const rows = agents.querySelectorAll<HTMLElement>('.agent-row')
      const rowOffsets = [-74, 0, 74]
      rows.forEach((row, index) => timeline.fromTo(row, { opacity: 0, transform: `translate3d(${rowOffsets[index] ?? 0}px, 0, 0)` }, { opacity: 1, transform: 'translate3d(0, 0, 0)', duration: .36 }, 2.1 + index * .1))
      agents.querySelectorAll<SVGPathElement>('[data-story-path]').forEach((path) => { const length = path.getTotalLength(); gsap.set(path, { strokeDasharray: length, strokeDashoffset: length }); timeline.to(path, { strokeDashoffset: 0, duration: .38 }, 2.12) })
      timeline.fromTo(agents.querySelector('.agent-merge'), { opacity: 0, transform: 'translate3d(0, 18px, 0)' }, { opacity: 1, transform: 'translate3d(0, 0, 0)', duration: .4 }, 2.54)

      handoff(workflow, agents, 3)
      timeline.fromTo(workflow.querySelector('.workflow-report'), { opacity: 0, transform: 'scale(1.08)' }, { opacity: 1, transform: 'scale(1)', duration: .52 }, 3.04)
      timeline.fromTo(workflow.querySelector('.workflow-progress'), { opacity: 0, transform: 'translate3d(0, 18px, 0) scale(.98)' }, { opacity: 1, transform: 'translate3d(0, 0, 0) scale(1)', duration: .46 }, 3.25)

      handoff(recovery, workflow, 4)
      timeline.fromTo(recovery.querySelector('.report-window'), { opacity: .46, transform: 'translate3d(0, 14px, 0)' }, { opacity: 1, transform: 'translate3d(0, 0, 0)', duration: .42 }, 4.08)
      const recoveryPath = recovery.querySelector<SVGPathElement>('[data-story-path]')
      if (recoveryPath) { const length = recoveryPath.getTotalLength(); gsap.set(recoveryPath, { strokeDasharray: length, strokeDashoffset: length }); timeline.to(recoveryPath, { strokeDashoffset: 0, duration: .62 }, 4.2) }
      timeline.fromTo(recovery.querySelector('.report-gap'), { opacity: .38, transform: 'translate3d(0, 8px, 0)' }, { opacity: 1, transform: 'translate3d(0, 0, 0)', duration: .3 }, 4.52)

      handoff(local, recovery, 5)
      const cards = local.querySelectorAll<HTMLElement>('.local-card')
      timeline.fromTo(cards[0], { opacity: 0, transform: 'translate3d(52px, 0, 90px) rotateY(-10deg)' }, { opacity: 1, transform: 'translate3d(0, 0, 0) rotateY(0deg)', duration: .48 }, 5.08)
      timeline.fromTo(cards[1], { opacity: 0, transform: 'translate3d(-42px, 14px, -70px) rotateY(9deg)' }, { opacity: 1, transform: 'translate3d(0, 0, 0) rotateY(0deg)', duration: .48 }, 5.28)
      timeline.fromTo(local.querySelector('.privacy-list'), { opacity: 0, transform: 'translate3d(0, 18px, 0)' }, { opacity: 1, transform: 'translate3d(0, 0, 0)', duration: .35 }, 5.5)

      handoff(ot, local, 6)
      const otObject = ot.querySelector<HTMLElement>('.ot-summary-object')
      const otLayers = ot.querySelectorAll<HTMLElement>('[data-ot-summary-layer]')
      const otSignals = ot.querySelectorAll<HTMLElement>('[data-ot-summary-signal]')
      if (otObject) timeline.fromTo(otObject, { opacity: 0, transform: 'translate3d(0, 24px, 0) scale(.96)' }, { opacity: 1, transform: 'translate3d(0, 0, 0) scale(1)', duration: .48 }, 6.08)
      ot.querySelectorAll<SVGPathElement>('[data-story-path]').forEach((path) => { const length = path.getTotalLength(); gsap.set(path, { strokeDasharray: length, strokeDashoffset: length }); timeline.to(path, { strokeDashoffset: 0, duration: .56 }, 6.12) })
      timeline.fromTo(otLayers, { opacity: 0, transform: 'translate3d(-18px, 0, 0)' }, { opacity: 1, transform: 'translate3d(0, 0, 0)', stagger: .06, duration: .32 }, 6.22)
      timeline.fromTo(otSignals, { opacity: 0, transform: 'translate3d(0, 12px, 0) scale(.98)' }, { opacity: 1, transform: 'translate3d(0, 0, 0) scale(1)', stagger: .055, duration: .3 }, 6.34)
      timeline.fromTo(ot.querySelectorAll('.chapter-copy > *'), { opacity: 0, transform: 'translate3d(0, 20px, 0)' }, { opacity: 1, transform: 'translate3d(0, 0, 0)', stagger: .055, duration: .4 }, 6.16)
    }, root)
    const refresh = () => ScrollTrigger.refresh()
    window.addEventListener('resize', refresh)
    teardown = () => { window.removeEventListener('resize', refresh); context.revert(); teardown = () => undefined }
  }

  setup()
  const onMediaChange = () => setup()
  desktopMotion.addEventListener('change', onMediaChange)
  return () => { desktopMotion.removeEventListener('change', onMediaChange); teardownMobile(); teardown() }
}
