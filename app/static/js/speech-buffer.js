// Pure presentation clock. It never changes game state or request telemetry.
export const readingModes = {
 comfort: {rate: 12, hold: 1300}, standard: {rate: 16, hold: 1000},
 fast: {rate: 30, hold: 800}, instant: {rate: Infinity, hold: 0}
};
export class SpeechBuffer {
 constructor(mode = 'standard') { this.mode = mode; this.reset(); }
 reset() { this.queue = []; this.current = null; this.retired = new Set(); this.skipped = 0; }
 put(key, pid, content = '', finished = false) {
  if (this.retired.has(key)) return;
  let item = this.current?.key === key ? this.current : this.queue.find(x => x.key === key);
  if (!item) { item = {key, pid, text: [], shown: 0, finished: false, nextAt: null, finishedAt: null}; this.queue.push(item); }
  item.text = Array.from(content); item.shown = Math.min(item.shown, item.text.length); item.finished ||= finished;
  if (this.queue.length > 80) { const removed = this.queue.shift(); this.retired.add(removed.key); this.skipped++; }
 }
 append(key, pid, delta) {
  const item = this.current?.key === key ? this.current : this.queue.find(x => x.key === key);
  this.put(key, pid, (item?.text.join('') || '') + delta);
 }
 tick(now, waitForAudio = false) {
  const config = readingModes[this.mode] || readingModes.standard;
  const prior = this.current?.key;
  if (this.current?.finishedAt !== null && this.current?.finishedAt !== undefined &&
      now >= this.current.finishedAt + config.hold && (!waitForAudio || this.mode === 'instant')) {
   this.retired.add(this.current.key); this.current = null;
   if (this.retired.size > 500) this.retired.delete(this.retired.values().next().value);
  }
  if (!this.current) this.current = this.queue.shift() || null;
  const item = this.current;
  if (!item) return {item: null, delta: '', switched: Boolean(prior)};
  if (item.nextAt === null) item.nextAt = now;
  const before = item.shown;
  while (item.shown < item.text.length && now >= item.nextAt) {
   const ch = item.text[item.shown++];
   item.nextAt += config.rate === Infinity ? 0 : 1000 / config.rate + (/[。！？!?]/.test(ch) ? 300 : /[，,；;]/.test(ch) ? 120 : 0);
  }
  // A slow upstream stream is displayed as it arrives, without accumulating a
  // second delay when there was no buffered text to read.
  if (item.shown === item.text.length) item.nextAt = Math.max(item.nextAt, now);
  if (item.finished && item.shown === item.text.length && item.finishedAt === null) item.finishedAt = now;
  return {item, delta: item.text.slice(before, item.shown).join(''), switched: prior !== item.key};
 }
}
