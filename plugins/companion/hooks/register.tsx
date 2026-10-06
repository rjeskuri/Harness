// ROBO, a Game Boy style robot that lives in the band above the prompt and
// reacts to the session. Sprite data, encoders and hooks stay in this one file.

import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register, RenderElement, Timer } from 'claude-code'

import type { Mood } from '../types'

// ---------------------------------------------------------------- state

const mood = atom({ plugin: 'companion', key: 'mood' } as const, 'idle')
const note = atom({ plugin: 'companion', key: 'note' } as const, '')
const frame = atom({ plugin: 'companion', key: 'frame' } as const, 0)
const isHidden = atom({ plugin: 'companion', key: 'isHidden' } as const, false)

// ---------------------------------------------------------------- sprite

// 14 x 12 pixels. `.` is transparent; 1-4 are the Game Boy greens, light to dark.
type Frame = readonly string[]

const WIDTH = 14
const HEIGHT = 12

const BASE: Frame = [
  '......4.......',
  '......4.......',
  '...44444444...',
  '..4111111114..',
  '..4144114414..',
  '..4144114414..',
  '..4111111114..',
  '...44444444...',
  '.442232232244.',
  '44.22222222.44',
  '...42222224...',
  '...44....44...',
]

// idle B: base with row 4 = ..4111111114.. (a blink)
const IDLE_BLINK: Frame = [
  '......4.......',
  '......4.......',
  '...44444444...',
  '..4111111114..',
  '..4111111114..',
  '..4144114414..',
  '..4111111114..',
  '...44444444...',
  '.442232232244.',
  '44.22222222.44',
  '...42222224...',
  '...44....44...',
]

// thinking B: base with rows 0 and 1 = the antenna glowing
const THINKING_GLOW: Frame = [
  '......2.......',
  '......3.......',
  '...44444444...',
  '..4111111114..',
  '..4144114414..',
  '..4144114414..',
  '..4111111114..',
  '...44444444...',
  '.442232232244.',
  '44.22222222.44',
  '...42222224...',
  '...44....44...',
]

// working B: arms pumped up (row 8 = 44422322322444, row 9 = ...22222222...),
// then the chest lights swapped on row 8: its 3s (columns 5 and 8) become 2 and
// the 2s directly beside them (columns 4, 6, 7, 9) become 3.
const WORKING_PUMP: Frame = [
  '......4.......',
  '......4.......',
  '...44444444...',
  '..4111111114..',
  '..4144114414..',
  '..4144114414..',
  '..4111111114..',
  '...44444444...',
  '44423233232444',
  '...22222222...',
  '...42222224...',
  '...44....44...',
]

// waiting A: base with row 4 = ..4141114114.. (pupils left)
const WAITING_LEFT: Frame = [
  '......4.......',
  '......4.......',
  '...44444444...',
  '..4111111114..',
  '..4141114114..',
  '..4144114414..',
  '..4111111114..',
  '...44444444...',
  '.442232232244.',
  '44.22222222.44',
  '...42222224...',
  '...44....44...',
]

// waiting B: base with row 4 = ..4114111414.. (pupils right)
const WAITING_RIGHT: Frame = [
  '......4.......',
  '......4.......',
  '...44444444...',
  '..4111111114..',
  '..4114111414..',
  '..4144114414..',
  '..4111111114..',
  '...44444444...',
  '.442232232244.',
  '44.22222222.44',
  '...42222224...',
  '...44....44...',
]

// happy A: base with rows 4 and 5 = smiling arcs
const HAPPY: Frame = [
  '......4.......',
  '......4.......',
  '...44444444...',
  '..4111111114..',
  '..4141111414..',
  '..4114114114..',
  '..4111111114..',
  '...44444444...',
  '.442232232244.',
  '44.22222222.44',
  '...42222224...',
  '...44....44...',
]

// happy B: happy A shifted up one row, a blank row appended (a bounce)
const HAPPY_BOUNCE: Frame = [
  '......4.......',
  '...44444444...',
  '..4111111114..',
  '..4141111414..',
  '..4114114114..',
  '..4111111114..',
  '...44444444...',
  '.442232232244.',
  '44.22222222.44',
  '...42222224...',
  '...44....44...',
  '..............',
]

// sad: base with the antenna drooping (rows 0, 1) and the eyes low (row 4)
const SAD: Frame = [
  '..............',
  '......44......',
  '...44444444...',
  '..4111111114..',
  '..4111111114..',
  '..4144114414..',
  '..4111111114..',
  '...44444444...',
  '.442232232244.',
  '44.22222222.44',
  '...42222224...',
  '...44....44...',
]

// One entry per tick, cycled by `frame`: idle blinks on one tick in eight,
// the others alternate every tick, sad holds its one frame.
const ANIMATIONS: Readonly<Record<Mood, readonly Frame[]>> = {
  idle: [BASE, BASE, BASE, BASE, BASE, BASE, BASE, IDLE_BLINK],
  thinking: [BASE, THINKING_GLOW],
  working: [BASE, WORKING_PUMP],
  waiting: [WAITING_LEFT, WAITING_RIGHT],
  happy: [HAPPY, HAPPY_BOUNCE],
  sad: [SAD],
}

const HEX: Readonly<Record<string, string>> = {
  '1': '#9bbc0f',
  '2': '#8bac0f',
  '3': '#306230',
  '4': '#0f380f',
}

// Checked once, at load: a malformed frame stops the module loading instead of
// drawing garbage.
for (const [name, cycle] of Object.entries(ANIMATIONS)) {
  for (const rows of cycle) {
    if (rows.length !== HEIGHT) {
      throw new Error(`companion: a ${name} frame has ${rows.length} rows, not ${HEIGHT}`)
    }
    for (const row of rows) {
      if (row.length !== WIDTH || !/^[.1-4]+$/.test(row)) {
        throw new Error(`companion: a ${name} frame row "${row}" is not ${WIDTH} of . 1 2 3 4`)
      }
    }
  }
}

function cycleOf(current: Mood): readonly Frame[] {
  const cycle: readonly Frame[] | undefined = ANIMATIONS[current]

  return cycle !== undefined && cycle.length > 0 ? cycle : [BASE]
}

function pick(cycle: readonly Frame[], ticks: number): Frame {
  const step = Number.isSafeInteger(ticks) && ticks > 0 ? ticks % cycle.length : 0

  return cycle[step] ?? BASE
}

// ---------------------------------------------------------------- encoders

const CELL_ROWS = HEIGHT / 2
const DEFAULT_COLOR = 0x01000000
const UPPER_HALF = 0x2580 // ▀
const LOWER_HALF = 0x2584 // ▄
const SPACE = 0x20

type Base64Bytes = Uint8Array & { toBase64(): string }

function colorAt(rows: Frame, x: number, y: number): number | undefined {
  const hex = HEX[rows[y]?.[x] ?? '.']

  return hex === undefined ? undefined : parseInt(hex.slice(1), 16)
}

// Raster cells: cell (c, r) covers pixels (c, 2r) on top and (c, 2r+1) below.
function rasterCells(rows: Frame): string {
  const words = new Uint32Array(WIDTH * CELL_ROWS * 3)
  for (let r = 0; r < CELL_ROWS; r++) {
    for (let c = 0; c < WIDTH; c++) {
      const top = colorAt(rows, c, 2 * r)
      const bottom = colorAt(rows, c, 2 * r + 1)
      const at = (r * WIDTH + c) * 3
      if (top !== undefined) {
        words[at] = UPPER_HALF
        words[at + 1] = top
        words[at + 2] = bottom ?? DEFAULT_COLOR
      } else if (bottom !== undefined) {
        words[at] = LOWER_HALF
        words[at + 1] = bottom
        words[at + 2] = DEFAULT_COLOR
      } else {
        words[at] = SPACE
        words[at + 1] = DEFAULT_COLOR
        words[at + 2] = DEFAULT_COLOR
      }
    }
  }

  return (new Uint8Array(words.buffer) as Base64Bytes).toBase64()
}

function svgSource(rows: Frame): string {
  const rects: string[] = []
  rows.forEach((row, y) => {
    for (let x = 0; x < row.length; x++) {
      const fill = HEX[row[x] ?? '.']
      if (fill !== undefined) {
        rects.push(`<rect x="${x}" y="${y}" width="1" height="1" fill="${fill}"/>`)
      }
    }
  })

  return (
    `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${WIDTH} ${HEIGHT}" ` +
    `width="56" height="48" shape-rendering="crispEdges">${rects.join('')}</svg>`
  )
}

// ---------------------------------------------------------------- moods

type Look = { mood: Mood; note: string }

const FAILED: Look = { mood: 'sad', note: 'that failed' }
const DONE: Look = { mood: 'happy', note: 'done' }
const IDLE: Look = { mood: 'idle', note: '' }

function fileName(path: unknown): string | undefined {
  if (typeof path !== 'string') {
    return undefined
  }

  return path.split(/[\\/]/).filter(part => part !== '').pop()
}

function onFile(verb: string, path: unknown): string {
  const name = fileName(path)

  return name === undefined ? verb : `${verb} ${name}`
}

function activityOf(tool: string, args: Readonly<Record<string, unknown>>): string {
  switch (tool) {
    case 'Bash':
      return 'running a command'
    case 'Edit':
    case 'Write':
      return onFile('editing', args.file_path)
    case 'NotebookEdit':
      return onFile('editing', args.notebook_path ?? args.file_path)
    case 'Read':
      return onFile('reading', args.file_path)
    case 'Grep':
    case 'Glob':
      return 'searching'
    case 'Agent':
      return 'delegating'
    case 'Workflow':
      return 'orchestrating'
    case 'WebFetch':
    case 'WebSearch':
      return 'browsing'
    default:
      return tool
  }
}

// Every read of one dispatch sees the moment it began (and its own writes), not
// what nested events wrote meanwhile. So `show` compares against that moment,
// which is safe for each caller below, and any check of the present after a
// `next(e)` runs through `later`, a timer and so a dispatch of its own.
//
// Writes only what changed, so a repeated mood does not redraw the band. Every
// writer fails open: a state call that throws never breaks the event.
async function show($: EngineInterface, look: Look): Promise<void> {
  try {
    if ((await read($, mood)) !== look.mood) {
      await update($, mood, () => look.mood)
    }
    if ((await read($, note)) !== look.note) {
      await update($, note, () => look.note)
    }
  } catch {
    // the companion is decoration; the session goes on
  }
}

async function revert($: EngineInterface, from: Look, to: Look): Promise<void> {
  try {
    if ((await read($, mood)) === from.mood && (await read($, note)) === from.note) {
      await show($, to)
    }
  } catch {
    // as above
  }
}

function later($: EngineInterface, ms: number, from: Look, to: Look): void {
  try {
    $.clock.after(ms, () => void revert($, from, to))
  } catch {
    // as above
  }
}

async function setHidden($: EngineInterface, value: boolean): Promise<void> {
  try {
    await update($, isHidden, () => value)
  } catch {
    // as above
  }
}

async function advanceFrame($: EngineInterface): Promise<void> {
  try {
    if (!(await read($, isHidden))) {
      await update($, frame, n => n + 1)
    }
  } catch {
    // as above
  }
}

// ---------------------------------------------------------------- hooks

export const register: Register = on => {
  let ticker: Timer | undefined

  on('session.start', async ($, e, next) => {
    try {
      await $.command.register({
        name: 'companion',
        description: 'Show or hide the companion sprite',
      })
    } catch {
      // without the command the Hide button still works
    }
    await show($, IDLE)
    try {
      ticker?.cancel()
      ticker = $.clock.every(500, () => void advanceFrame($))
    } catch {
      // a still sprite is still a sprite
    }

    return next(e)
  })

  on('prompt.submit', async ($, e, next) => {
    await show($, { mood: 'thinking', note: '' })

    return next(e)
  })

  on('tool.call', async ($, e, next) => {
    const tool = String(e.tool)
    const working: Look = {
      mood: 'working',
      note: activityOf(tool, e as unknown as Readonly<Record<string, unknown>>),
    }
    await show($, working)
    const r = await next(e)
    if (r.isError === true) {
      await show($, FAILED)
      later($, 2500, FAILED, working)
    } else {
      // A permission prompt this call raised has been answered by now.
      later($, 0, { mood: 'waiting', note: `needs you: ${tool}` }, working)
    }

    return r
  })

  on('tool.check', async ($, e, next) => {
    const r = await next(e)
    // Only a real call puts a prompt in front of the person; a query asks no one.
    if (r.decision === 'ask' && e.tool_use_id !== undefined) {
      await show($, { mood: 'waiting', note: `needs you: ${e.tool}` })
    }

    return r
  })

  on('turn.complete', async ($, e, next) => {
    // A subagent's turn ending is not the session's turn ending.
    if (e.agentId === undefined) {
      if (e.reason === 'answer') {
        await show($, DONE)
        later($, 6000, DONE, IDLE)
      } else if (e.reason === 'aborted') {
        await show($, IDLE)
      } else {
        await show($, { mood: 'sad', note: 'error' })
      }
    }

    return next(e)
  })

  on('command.run', { command: 'companion' }, async ($, e, next) => {
    try {
      const hidden = await update($, isHidden, current => !current)
      // A local command starts no turn, so nothing else would clear a stale mood.
      await show($, IDLE)

      return { text: hidden ? 'Companion hidden; /companion shows it again.' : 'Companion shown.' }
    } catch {
      return next(e)
    }
  })

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    if (e.props.hasSurvey || (await read($, isHidden))) {
      return next(e)
    }
    const currentMood = await read($, mood)
    const currentNote = await read($, note)
    const cycle = cycleOf(currentMood)
    // A one-frame mood never reads the ticker, so its ticks do not redraw it.
    const pixels = pick(cycle, cycle.length > 1 ? await read($, frame) : 0)

    let sprite: RenderElement
    if (e.surface === 'terminal') {
      const { Raster } = $.ui.resolve(e)
      sprite = <Raster key="sprite" columns={WIDTH} rows={CELL_ROWS} cells={rasterCells(pixels)} />
    } else {
      const { Svg } = $.ui.resolve(e)
      sprite = <Svg source={svgSource(pixels)} alt={`ROBO, ${currentMood}`} width={56} height={48} />
    }
    if (e.props.bodyColumns < 36) {
      return sprite
    }

    const { Box, Button, Text } = $.ui.resolve(e)

    return (
      <Box flexDirection="row" gap={1}>
        {sprite}
        <Box flexDirection="column">
          <Text bold>{`ROBO · ${currentMood}`}</Text>
          <Text dimColor>{currentNote}</Text>
        </Box>
        <Button key="hide" label="Hide" onPress={() => void setHidden($, true)} />
      </Box>
    )
  })
}
