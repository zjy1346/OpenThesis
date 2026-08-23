import gsap from 'gsap'
import { ScrollTrigger } from 'gsap/ScrollTrigger'

gsap.registerPlugin(ScrollTrigger)

export function mountOtTimeline(root: HTMLElement) {
  const motion = window.matchMedia('(prefers-reduced-motion: no-preference)')
  if (!motion.matches) return () => undefined

  const context = gsap.context(() => {
    const hero = root.querySelector<HTMLElement>('.ot-hero')
    const object = root.querySelector<HTMLElement>('[data-ot-object]')
    const nodes = root.querySelectorAll<HTMLElement>('.ot-node')
    const heroCopy = root.querySelectorAll<HTMLElement>('.ot-hero-copy > *')

    gsap.fromTo(heroCopy, {
      opacity: 0,
      transform: 'translate3d(0, 22px, 0)'
    }, {
      opacity: 1,
      transform: 'translate3d(0, 0, 0)',
      duration: .78,
      stagger: .06,
      ease: 'power3.out'
    })

    if (hero && object) {
      gsap.fromTo(object, {
        xPercent: -50,
        yPercent: -50,
        y: 20,
        rotation: -4,
        scale: .96,
        opacity: 0
      }, {
        xPercent: -50,
        yPercent: -50,
        y: 0,
        rotation: 0,
        scale: 1,
        opacity: 1,
        duration: .92,
        ease: 'power3.out'
      })
      gsap.to(object, {
        y: 58,
        rotation: 4,
        scale: .93,
        ease: 'none',
        scrollTrigger: { trigger: hero, start: 'top top', end: 'bottom top', scrub: .8 }
      })
    }

    gsap.fromTo(nodes, { opacity: 0, scale: .92 }, {
      opacity: 1,
      scale: 1,
      duration: .55,
      stagger: .07,
      delay: .25,
      ease: 'power3.out'
    })

    root.querySelectorAll<SVGPathElement>('[data-ot-path]').forEach((path) => {
      const length = path.getTotalLength()
      gsap.set(path, { strokeDasharray: length, strokeDashoffset: length })
      gsap.to(path, {
        strokeDashoffset: 0,
        ease: 'none',
        scrollTrigger: { trigger: path.closest('section') ?? path, start: 'top 78%', end: 'center 45%', scrub: .75 }
      })
    })

    root.querySelectorAll<HTMLElement>('[data-ot-reveal]').forEach((element) => {
      gsap.fromTo(element, {
        opacity: 0,
        transform: 'translate3d(0, 28px, 0) scale(.99)'
      }, {
        opacity: 1,
        transform: 'translate3d(0, 0, 0) scale(1)',
        ease: 'none',
        scrollTrigger: { trigger: element, start: 'top 88%', end: 'top 55%', scrub: .7 }
      })
    })

    root.querySelectorAll<HTMLElement>('[data-ot-layer], [data-ot-card]').forEach((element) => {
      gsap.fromTo(element, {
        opacity: 0,
        transform: 'translate3d(0, 18px, 0) scale(.985)'
      }, {
        opacity: 1,
        transform: 'translate3d(0, 0, 0) scale(1)',
        ease: 'none',
        scrollTrigger: {
          trigger: element.parentElement ?? element,
          start: 'top 82%',
          end: 'center 54%',
          scrub: .65
        }
      })
    })

    const orbit = root.querySelector<HTMLElement>('.ot-orbit-lines')
    if (hero && orbit) {
      gsap.to(orbit, {
        rotation: 18,
        transformOrigin: '50% 50%',
        ease: 'none',
        scrollTrigger: { trigger: hero, start: 'top top', end: 'bottom top', scrub: .9 }
      })
    }
  }, root)

  const refresh = () => ScrollTrigger.refresh()
  window.addEventListener('resize', refresh)
  return () => {
    window.removeEventListener('resize', refresh)
    context.revert()
  }
}
