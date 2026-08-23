import gsap from 'gsap'
import { ScrollTrigger } from 'gsap/ScrollTrigger'

gsap.registerPlugin(ScrollTrigger)

export function mountMobileStoryTimeline(root: HTMLElement, scenes: HTMLElement[]) {
  const mobileMotion = window.matchMedia('(max-width: 980px) and (prefers-reduced-motion: no-preference)')
  let teardown = () => undefined

  const setup = () => {
    teardown()
    if (!mobileMotion.matches) return

    const context = gsap.context(() => {
      const hero = document.querySelector<HTMLElement>('.hero')
      const heroPortal = document.querySelector<HTMLElement>('.hero-portal')
      const heroItems = document.querySelectorAll<HTMLElement>('.hero-content > *')

      if (heroItems.length) {
        gsap.fromTo(heroItems, {
          opacity: 0,
          transform: 'translate3d(0, 18px, 0)'
        }, {
          opacity: 1,
          transform: 'translate3d(0, 0, 0)',
          duration: .72,
          stagger: .055,
          ease: 'power3.out',
          clearProps: 'transform'
        })
      }

      if (hero && heroPortal) {
        gsap.fromTo(heroPortal, {
          transform: 'translate3d(0, 0, 0) rotate(-3deg) scale(1.02)',
          opacity: .18
        }, {
          transform: 'translate3d(0, 46px, 0) rotate(1deg) scale(.94)',
          opacity: .04,
          ease: 'none',
          scrollTrigger: { trigger: hero, start: 'top top', end: 'bottom top', scrub: .65 }
        })
      }

      scenes.forEach((scene, index) => {
        const copyItems = scene.querySelectorAll<HTMLElement>('.chapter-copy > *')
        const stage = scene.querySelector<HTMLElement>('.chapter-stage')
        const timeline = gsap.timeline({
          defaults: { ease: 'none' },
          scrollTrigger: {
            trigger: scene,
            start: 'top 88%',
            end: 'top 28%',
            scrub: .7,
            invalidateOnRefresh: true
          }
        })

        timeline.fromTo(copyItems, {
          opacity: 0,
          transform: 'translate3d(0, 22px, 0)'
        }, {
          opacity: 1,
          transform: 'translate3d(0, 0, 0)',
          stagger: .055,
          duration: .42
        }, 0)

        if (stage) {
          timeline.fromTo(stage, {
            opacity: .18,
            transform: `translate3d(0, ${index % 2 === 0 ? 28 : 22}px, 0) scale(.975)`
          }, {
            opacity: 1,
            transform: 'translate3d(0, 0, 0) scale(1)',
            duration: .68
          }, .08)
        }

        scene.querySelectorAll<SVGPathElement>('[data-story-path]').forEach((path) => {
          const length = path.getTotalLength()
          gsap.set(path, { strokeDasharray: length, strokeDashoffset: length })
          timeline.to(path, { strokeDashoffset: 0, duration: .58 }, .18)
        })
        if (index === 6) {
          timeline.fromTo(scene.querySelectorAll('[data-ot-summary-layer]'), { opacity: 0, transform: 'translate3d(-16px, 0, 0)' }, { opacity: 1, transform: 'translate3d(0, 0, 0)', stagger: .055, duration: .34 }, .22)
          timeline.fromTo(scene.querySelectorAll('[data-ot-summary-signal]'), { opacity: 0, transform: 'translate3d(0, 10px, 0) scale(.98)' }, { opacity: 1, transform: 'translate3d(0, 0, 0) scale(1)', stagger: .05, duration: .3 }, .32)
        }
      })

      const details: Array<[Element | null, gsap.TweenVars, gsap.TweenVars]> = [
        [scenes[0]?.querySelector('.stage-orbit') ?? null, { rotation: -18, scale: .94 }, { rotation: -12, scale: 1 }],
        [scenes[1]?.querySelector('.source-focus') ?? null, { opacity: 0, y: 18, scale: .98 }, { opacity: 1, y: 0, scale: 1 }],
        [scenes[2]?.querySelector('.agent-merge') ?? null, { opacity: 0, y: 16 }, { opacity: 1, y: 0 }],
        [scenes[3]?.querySelector('.workflow-progress') ?? null, { opacity: 0, y: 18, scale: .98 }, { opacity: 1, y: 0, scale: 1 }],
        [scenes[4]?.querySelector('.report-gap') ?? null, { opacity: .3, x: -12 }, { opacity: 1, x: 0 }],
        [scenes[5]?.querySelector('.local-history') ?? null, { opacity: 0, y: 18, scale: .98 }, { opacity: 1, y: 0, scale: 1 }],
        [scenes[6]?.querySelector('.ot-summary-object') ?? null, { opacity: 0, transform: 'translate3d(0, 18px, 0) scale(.98)' }, { opacity: 1, transform: 'translate3d(0, 0, 0) scale(1)' }]
      ]

      details.forEach(([target, from, to], index) => {
        if (!target) return
        gsap.fromTo(target, from, {
          ...to,
          ease: 'none',
          scrollTrigger: { trigger: scenes[index], start: 'top 70%', end: 'top 18%', scrub: .65 }
        })
      })
    }, root)

    const refresh = () => ScrollTrigger.refresh()
    window.addEventListener('resize', refresh)
    teardown = () => {
      window.removeEventListener('resize', refresh)
      context.revert()
      teardown = () => undefined
    }
  }

  setup()
  mobileMotion.addEventListener('change', setup)
  return () => {
    mobileMotion.removeEventListener('change', setup)
    teardown()
  }
}

