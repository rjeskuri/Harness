import { expect, mock, test } from 'claude-code/testing'
import type { Engine } from 'claude-code/testing'
import type { On, RenderPropsOf, RenderSurface } from 'claude-code'

const PLUGIN = 'companion'
const ENGINE_BAND = 'the engine band'
const DEFAULT_COLOR = 0x01000000

const BAND: RenderPropsOf['AbovePrompt'] = {
  hasSurvey: false,
  isWorking: false,
  maxRows: 12,
  bodyColumns: 80,
  scroll: { offset: 0, bodyRows: 12 },
  view: {},
}

// ------------------------------------------------ the spec's frames, rebuilt here

const BASE = [
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

function withRows(rows: Record<number, string>): string[] {
  return BASE.map((row, i) => rows[i] ?? row)
}

// The 3s of the row become 2 and the 2s directly beside them become 3.
function swapLights(row: string): string {
  const chars = [...row]
  for (let i = 0; i < row.length; i++) {
    if (row[i] === '3') {
      chars[i] = '2'
      for (const j of [i - 1, i + 1]) {
        if (row[j] === '2') {
          chars[j] = '3'
        }
      }
    }
  }

  return chars.join('')
}

const HAPPY_A = withRows({ 4: '..4141111414..', 5: '..4114114114..' })

const SPEC = {
  idle: [BASE, withRows({ 4: '..4111111114..' })],
  thinking: [BASE, withRows({ 0: '......2.......', 1: '......3.......' })],
  working: [BASE, withRows({ 8: swapLights('44422322322444'), 9: '...22222222...' })],
  waiting: [withRows({ 4: '..4141114114..' }), withRows({ 4: '..4114111414..' })],
  happy: [HAPPY_A, [...HAPPY_A.slice(1), '..............']],
  sad: [withRows({ 0: '..............', 1: '......44......', 4: '..4111111114..' })],
}

const PALETTE: Record<string, number> = { '1': 0x9bbc0f, '2': 0x8bac0f, '3': 0x306230, '4': 0x0f380f }

function pixelOf(color: number | undefined): string {
  const found = Object.entries(PALETTE).find(([, value]) => value === color)

  return found === undefined ? '?' : found[0]
}

function blank(): string[][] {
  return Array.from({ length: 12 }, () => Array.from({ length: 14 }, () => '.'))
}

function decode(cells: string): Uint32Array {
  const bytes = Uint8Array.from(atob(cells), ch => ch.charCodeAt(0))

  return new Uint32Array(bytes.buffer)
}

// The Raster's cells read back as pixels: each cell is two pixels, top and bottom.
function rasterPixels(cells: string): string[] {
  const words = decode(cells)
  const grid = blank()
  for (let r = 0; r < 6; r++) {
    for (let c = 0; c < 14; c++) {
      const at = (r * 14 + c) * 3
      const [glyph, fg, bg] = [words[at], words[at + 1], words[at + 2]]
      const top = grid[2 * r]
      const bottom = grid[2 * r + 1]
      if (top === undefined || bottom === undefined) {
        continue
      }
      if (glyph === 0x2580) {
        top[c] = pixelOf(fg)
        if (bg !== DEFAULT_COLOR) {
          bottom[c] = pixelOf(bg)
        }
      } else if (glyph === 0x2584) {
        bottom[c] = pixelOf(fg)
        if (bg !== DEFAULT_COLOR) {
          top[c] = '?'
        }
      } else if (glyph !== 0x20 || fg !== DEFAULT_COLOR || bg !== DEFAULT_COLOR) {
        top[c] = '?'
      }
    }
  }

  return grid.map(row => row.join(''))
}

const HEX: Record<string, string> = { '#9bbc0f': '1', '#8bac0f': '2', '#306230': '3', '#0f380f': '4' }

// The Svg's rects read back as pixels, one rect per opaque pixel.
function svgPixels(source: string): string[] {
  const grid = blank()
  for (const [, x, y, fill] of source.matchAll(/<rect x="(\d+)" y="(\d+)" width="1" height="1" fill="(#[0-9a-f]{6})"\/>/g)) {
    const row = grid[Number(y)]
    if (row !== undefined) {
      row[Number(x)] = row[Number(x)] === '.' ? HEX[fill ?? ''] ?? '?' : '?'
    }
  }

  return grid.map(row => row.join(''))
}

// ------------------------------------------------ helpers

// Stands for the engine beneath the plugin: its own band, which the plugin yields to.
function engineBand(on: On): void {
  on('ui.render', { component: 'AbovePrompt' }, ($, e) => {
    const { Text } = $.ui.resolve(e)

    return Text({ children: ENGINE_BAND })
  })
}

// Stands for the engine beneath every event the companion hooks.
function engineEvents(on: On, toolFails: () => boolean): void {
  on('session.start', ($, e) => ({ cwd: e.cwd }))
  on('prompt.submit', ($, e) => ({ text: e.text }))
  on('tool.call', () => (toolFails() ? { isError: true, result: 'exit 1', text: 'exit 1' } : { result: 'ok', text: 'ok' }))
  on('tool.check', () => ({ decision: 'ask' }))
  on('turn.complete', ($, e) => ({ text: e.answer }))
}

type ToolCall = Parameters<Engine['tool']['call']>[0]

function mountBand($: Engine, surface: RenderSurface, props: RenderPropsOf['AbovePrompt'] = BAND) {
  return $.ui.mount({ plugin: PLUGIN, surface, component: 'AbovePrompt', props })
}

// What the band says: the bold title and the dim note.
async function says($: Engine): Promise<string[]> {
  const ui = await mountBand($, 'terminal')
  const texts = await ui.findAll({ type: 'Text' })
  await ui.unmount()

  return texts.map(text => text.text)
}

async function spritePixels($: Engine, surface: RenderSurface): Promise<string[]> {
  const ui = await mountBand($, surface)
  const raster = await ui.find({ type: 'Raster', key: 'sprite' })
  const svg = await ui.find({ type: 'Svg' })
  await ui.unmount()
  if (surface === 'terminal') {
    expect(svg).toBeUndefined()

    return rasterPixels(String(raster?.props.cells))
  }
  expect(raster).toBeUndefined()

  return svgPixels(String(svg?.props.source))
}

async function expectFrame($: Engine, rows: string[]): Promise<void> {
  for (const surface of ['terminal', 'vscode'] as const) {
    expect(await spritePixels($, surface), `${surface} pixels`).toEqual(rows)
  }
}

async function submit($: Engine): Promise<void> {
  await $.prompt.submit({ text: 'hello', wait: false, origin: { kind: 'composer' } })
}

async function finish($: Engine, reason: 'answer' | 'aborted' | 'error', agentId?: string): Promise<void> {
  await $.turn.complete({ answer: 'ok', durationMs: 1, isAborted: reason === 'aborted', turnId: 't', reason, agentId })
}

async function companion($: Engine): Promise<string | undefined> {
  const run = await $.command.run({
    command: 'companion',
    args: '',
    origin: { kind: 'composer' },
    presentation: { isFullscreen: false, columns: 80 },
  })

  return run.text
}

// ------------------------------------------------ the spec's four

test('the terminal draws the sprite as a Raster keyed sprite', async $ => {
  const ui = await mountBand($, 'terminal')
  const raster = await ui.find({ type: 'Raster', key: 'sprite' })
  expect(raster).toBeDefined()
  expect(raster?.props).toMatchObject({ key: 'sprite', columns: 14, rows: 6 })
  expect(await ui.find({ type: 'Svg' })).toBeUndefined()
  expect(await ui.find({ type: 'Text', text: 'ROBO · idle' })).toBeDefined()
  expect(await ui.find({ type: 'Button', key: 'hide' })).toBeDefined()

  const cells = String(raster?.props.cells)
  expect(atob(cells)).toHaveLength(14 * 6 * 3 * 4)
  const words = decode(cells)
  const cell = (c: number, r: number) => [...words.slice((r * 14 + c) * 3, (r * 14 + c) * 3 + 3)]
  expect(cell(0, 0), 'neither pixel').toEqual([0x20, DEFAULT_COLOR, DEFAULT_COLOR])
  expect(cell(6, 0), 'both pixels').toEqual([0x2580, 0x0f380f, 0x0f380f])
  expect(cell(3, 1), 'both pixels, two colors').toEqual([0x2580, 0x0f380f, 0x9bbc0f])
  expect(cell(2, 4), 'top pixel only').toEqual([0x2580, 0x0f380f, DEFAULT_COLOR])
  expect(cell(2, 1), 'bottom pixel only').toEqual([0x2584, 0x0f380f, DEFAULT_COLOR])
  await ui.unmount()
})

test('vscode draws the sprite as an Svg, and so do desktop and mobile', async $ => {
  for (const surface of ['vscode', 'desktop', 'mobile'] as const) {
    const ui = await mountBand($, surface)
    const svg = await ui.find({ type: 'Svg' })
    expect(svg, surface).toBeDefined()
    expect(svg?.props).toMatchObject({ alt: 'ROBO, idle', width: 56, height: 48 })
    expect(await ui.find({ type: 'Raster' })).toBeUndefined()
    const source = String(svg?.props.source)
    expect(source).toStartWith(
      '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 14 12" width="56" height="48" shape-rendering="crispEdges">',
    )
    expect(source).toEndWith('</svg>')
    expect(source).toContain('<rect x="6" y="0" width="1" height="1" fill="#0f380f"/>')
    const opaque = BASE.join('').replaceAll('.', '').length
    expect(source.match(/<rect /g)).toHaveLength(opaque)
    await ui.unmount()
  }
})

test('after Hide is pressed the next mount yields nothing of the plugin', async ($, on) => {
  engineBand(on)
  const shown = await mountBand($, 'terminal')
  await shown.press({ key: 'hide' })
  await shown.unmount()

  for (const surface of ['terminal', 'vscode'] as const) {
    const ui = await mountBand($, surface)
    expect(await ui.find({ text: ENGINE_BAND }), surface).toBeDefined()
    expect(await ui.find({ type: 'Raster' })).toBeUndefined()
    expect(await ui.find({ type: 'Svg' })).toBeUndefined()
    expect(await ui.find({ text: /ROBO/ })).toBeUndefined()
    expect(await ui.find({ key: 'hide' })).toBeUndefined()
    await ui.unmount()
  }
})

test('a survey in the band makes the plugin yield', async ($, on) => {
  engineBand(on)
  for (const surface of ['terminal', 'desktop'] as const) {
    const ui = await mountBand($, surface, { ...BAND, hasSurvey: true })
    expect(await ui.find({ text: ENGINE_BAND }), surface).toBeDefined()
    expect(await ui.find({ type: 'Raster' })).toBeUndefined()
    expect(await ui.find({ type: 'Svg' })).toBeUndefined()
    expect(await ui.find({ text: /ROBO/ })).toBeUndefined()
    await ui.unmount()
  }
})

// ------------------------------------------------ layout and the command

test('a band narrower than 36 columns draws the sprite alone', async $ => {
  for (const surface of ['terminal', 'desktop'] as const) {
    const ui = await mountBand($, surface, { ...BAND, bodyColumns: 35 })
    const drawn = await ui.drawn()
    expect(drawn.type, surface).toBe(surface === 'terminal' ? 'Raster' : 'Svg')
    expect(await ui.findAll({ type: 'Text' })).toHaveLength(0)
    expect(await ui.find({ key: 'hide' })).toBeUndefined()
    await ui.unmount()
  }
  const wide = await mountBand($, 'terminal', { ...BAND, bodyColumns: 36 })
  expect(await wide.find({ key: 'hide' })).toBeDefined()
  await wide.unmount()
})

test('/companion hides and shows the sprite', async ($, on) => {
  engineBand(on)
  expect(await companion($)).toBe('Companion hidden; /companion shows it again.')
  const hidden = await mountBand($, 'terminal')
  expect(await hidden.find({ type: 'Raster' })).toBeUndefined()
  expect(await hidden.find({ text: ENGINE_BAND })).toBeDefined()
  await hidden.unmount()

  expect(await companion($)).toBe('Companion shown.')
  const shown = await mountBand($, 'terminal')
  expect(await shown.find({ type: 'Raster', key: 'sprite' })).toBeDefined()
  await shown.unmount()
})

// ------------------------------------------------ moods

test('session.start registers /companion and settles on idle', async ($, on) => {
  const registered: string[] = []
  on('command.register', ($, e) => {
    registered.push(`${e.name}: ${e.description}`)

    return { value: { command: e.name } }
  })
  mock.clock(on)
  engineEvents(on, () => false)
  await submit($)
  await $.session.start({ cwd: '/tmp', surface: 'terminal', isInteractive: true })
  expect(registered).toEqual(['companion: Show or hide the companion sprite'])
  expect(await says($)).toEqual(['ROBO · idle', ''])
})

test('each tool names what the robot is doing', async ($, on) => {
  mock.clock(on)
  engineEvents(on, () => false)
  // Grep and Glob are not in every build's tool table, so they are named loosely.
  const loose = (call: Record<string, unknown>) => call as unknown as ToolCall
  const calls: [ToolCall, string][] = [
    [{ tool: 'Bash', command: 'ls' }, 'running a command'],
    [{ tool: 'Edit', file_path: '/repo/src/app.ts', old_string: 'a', new_string: 'b' }, 'editing app.ts'],
    [{ tool: 'Write', file_path: '/repo/README.md', content: '' }, 'editing README.md'],
    [{ tool: 'NotebookEdit', notebook_path: '/repo/nb/plot.ipynb', new_source: '' }, 'editing plot.ipynb'],
    [{ tool: 'Read', file_path: '/repo/docs/design.md' }, 'reading design.md'],
    [{ tool: 'Agent', description: 'd', prompt: 'p' }, 'delegating'],
    [{ tool: 'Workflow', name: 'review' }, 'orchestrating'],
    [{ tool: 'WebFetch', url: 'https://example.com', prompt: 'p' }, 'browsing'],
    [{ tool: 'WebSearch', query: 'q', mode: 'standard' }, 'browsing'],
    [loose({ tool: 'Grep', pattern: 'x' }), 'searching'],
    [loose({ tool: 'Glob', pattern: '*.ts' }), 'searching'],
    [{ tool: 'TodoWrite', todos: [] }, 'TodoWrite'],
    [{ tool: 'mcp__github__create_issue', title: 't' }, 'mcp__github__create_issue'],
  ]
  for (const [call, note] of calls) {
    await $.tool.call(call)
    expect(await says($), String(call.tool)).toEqual(['ROBO · working', note])
  }
})

test('a failed tool is sad for 2.5 s, then back to working', async ($, on) => {
  const clock = mock.clock(on)
  engineEvents(on, () => true)
  await $.tool.call({ tool: 'Bash', command: 'false' })
  expect(await says($)).toEqual(['ROBO · sad', 'that failed'])
  await clock.advance(2499)
  expect(await says($)).toEqual(['ROBO · sad', 'that failed'])
  await clock.advance(1)
  expect(await says($)).toEqual(['ROBO · working', 'running a command'])

  // A turn that ends in error within the 2.5 s stays sad.
  await $.tool.call({ tool: 'Bash', command: 'false' })
  await finish($, 'error')
  await clock.advance(2500)
  expect(await says($)).toEqual(['ROBO · sad', 'error'])
})

test('a permission prompt is waiting until the call it asked for settles', async ($, on) => {
  const clock = mock.clock(on)
  let during: string[] = []
  on('tool.check', () => ({ decision: 'ask' }))
  on('tool.call', async (_, e) => {
    await $.tool.check({ tool: String(e.tool), input: { command: 'make' }, tool_use_id: e.tool_use_id })
    during = await says($)

    return { result: 'ok', text: 'ok' }
  })
  await $.tool.call({ tool: 'Bash', command: 'make' })
  expect(during, 'while the prompt is up').toEqual(['ROBO · waiting', 'needs you: Bash'])
  await clock.settle()
  expect(await says($), 'once the call settled').toEqual(['ROBO · working', 'running a command'])

  // A query (no call behind it) asks no one.
  await $.tool.check({ tool: 'Bash', input: { command: 'make' } })
  expect(await says($), 'after a query').toEqual(['ROBO · working', 'running a command'])
})

test('the end of a turn: happy then idle, sad on error or refusal, idle on abort', async ($, on) => {
  const clock = mock.clock(on)
  engineEvents(on, () => false)
  await submit($)
  expect(await says($)).toEqual(['ROBO · thinking', ''])

  await finish($, 'answer')
  expect(await says($)).toEqual(['ROBO · happy', 'done'])
  await clock.advance(5999)
  expect(await says($)).toEqual(['ROBO · happy', 'done'])
  await clock.advance(1)
  expect(await says($)).toEqual(['ROBO · idle', ''])

  await finish($, 'error')
  expect(await says($)).toEqual(['ROBO · sad', 'error'])
  await $.turn.complete({
    answer: '',
    durationMs: 1,
    isAborted: false,
    turnId: 't',
    reason: 'refusal',
    refusal: { category: null, explanation: null },
  })
  expect(await says($)).toEqual(['ROBO · sad', 'error'])

  await submit($)
  await finish($, 'aborted')
  expect(await says($)).toEqual(['ROBO · idle', ''])

  // A subagent's turn ending leaves the main turn's mood alone.
  await submit($)
  await finish($, 'answer', 'agent-1')
  expect(await says($)).toEqual(['ROBO · thinking', ''])
})

// ------------------------------------------------ frames and the ticker

test('every mood draws the spec frames, the ticker animating them', async ($, on) => {
  const clock = mock.clock(on)
  engineEvents(on, () => false)
  await $.session.start({ cwd: '/tmp', surface: 'terminal', isInteractive: true })

  // idle: A, with the blink on one tick in eight (frame 7)
  await expectFrame($, SPEC.idle[0] ?? [])
  await clock.advance(3000)
  await expectFrame($, SPEC.idle[0] ?? [])
  await clock.advance(500)
  await expectFrame($, SPEC.idle[1] ?? [])
  await clock.advance(500) // frame 8

  await submit($)
  await expectFrame($, SPEC.thinking[0] ?? [])
  await clock.advance(500) // frame 9
  await expectFrame($, SPEC.thinking[1] ?? [])

  await $.tool.call({ tool: 'Bash', command: 'make' })
  await expectFrame($, SPEC.working[1] ?? [])
  await clock.advance(500) // frame 10
  await expectFrame($, SPEC.working[0] ?? [])

  await $.tool.check({ tool: 'Bash', input: { command: 'make' }, tool_use_id: 'toolu_frames' })
  await expectFrame($, SPEC.waiting[0] ?? [])
  await clock.advance(500) // frame 11
  await expectFrame($, SPEC.waiting[1] ?? [])

  await finish($, 'answer')
  await expectFrame($, SPEC.happy[1] ?? [])
  await clock.advance(500) // frame 12
  await expectFrame($, SPEC.happy[0] ?? [])

  await finish($, 'error')
  await expectFrame($, SPEC.sad[0] ?? [])
  await clock.advance(500)
  await expectFrame($, SPEC.sad[0] ?? [])

  const svg = await mountBand($, 'vscode')
  expect((await svg.find({ type: 'Svg' }))?.props.alt).toBe('ROBO, sad')
  await svg.unmount()
})

test('the ticker rests while the sprite is hidden', async ($, on) => {
  const clock = mock.clock(on)
  engineEvents(on, () => false)
  engineBand(on)
  await $.session.start({ cwd: '/tmp', surface: 'terminal', isInteractive: true })
  await submit($)
  await clock.advance(500) // frame 1: the antenna glows
  await expectFrame($, SPEC.thinking[1] ?? [])

  // /companion leaves the look idle, which blinks only on frame 7.
  await companion($)
  await clock.advance(3000) // six ticks that must not count: they would reach frame 7
  await companion($)
  await expectFrame($, SPEC.idle[0] ?? []) // still frame 1: the base
  await clock.advance(3000) // shown again, the six ticks count
  await expectFrame($, SPEC.idle[1] ?? []) // frame 7: the blink
})
