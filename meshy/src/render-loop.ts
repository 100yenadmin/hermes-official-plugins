/** One demand-driven, capped render loop. Hidden/idle views schedule no frames. */
export function createRenderLoop(step: (time: number) => boolean, request = requestAnimationFrame, cancel = cancelAnimationFrame) {
  let frame: number | null = null
  let active = true
  let disposed = false
  let last = -Infinity
  let fps = 30
  const invalidate = () => {
    if (!disposed && active && frame === null) frame = request(tick)
  }
  const tick = (time: number) => {
    frame = null
    if (disposed || !active) return
    if (time - last < 1000 / fps) { invalidate(); return }
    last = time
    if (step(time)) invalidate()
  }
  return {
    invalidate,
    setMaxFps(value: number) { fps = Math.max(1, Math.min(30, value)) },
    setActive(value: boolean) {
      active = value
      if (!active && frame !== null) { cancel(frame); frame = null }
      if (active) { last = -Infinity; invalidate() }
    },
    dispose() {
      disposed = true
      if (frame !== null) cancel(frame)
      frame = null
    }
  }
}
