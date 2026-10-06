export type Mood = 'idle' | 'thinking' | 'working' | 'waiting' | 'happy' | 'sad'
declare module 'claude-code' {
  interface PluginState {
    companion: { mood: Mood; note: string; frame: number; isHidden: boolean }
  }
}
