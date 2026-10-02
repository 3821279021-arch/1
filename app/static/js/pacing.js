import {store, runtime} from './store.js';
import {SpeechBuffer, readingModes} from './speech-buffer.js';

const buffer = new SpeechBuffer(localStorage.getItem('werewolf-reading-mode') || 'standard');
let gameId = null, painted = null;
const keyFor = (turn, pid) => `${gameId}:${turn}:${pid}`;
function resetPresentation() { buffer.reset(); gameId = null; painted = null; }
function hasPresentation() { return Boolean(buffer.current || buffer.queue.length); }
function presentationPlayer() { return buffer.current?.pid; }
function deferSpeech(event) { const key=keyFor(event.turn_id,event.player_id);return buffer.current?.key===key || buffer.queue.some(item=>item.key===key); }
function paceStart(turn, pid, content = '') { if (store.state?.lifecycle === 'SUSPENDED') return; gameId = store.state?.game_id; buffer.put(keyFor(turn, pid), pid, content); }
function paceDelta(turn, pid, delta) { gameId = store.state?.game_id; buffer.append(keyFor(turn, pid), pid, delta); }
function paceFinish(turn, pid, content) { gameId = store.state?.game_id; buffer.put(keyFor(turn, pid), pid, content, true); }
function presentSnapshot(s) {
 if (gameId && gameId !== s.game_id) resetPresentation();
 gameId = s.game_id;
 if (s.lifecycle === 'SUSPENDED') { buffer.reset(); runtime.clearAudio(); return; }
 const liveKey = s.current_turn_player_id ? keyFor(s.turn_id,s.current_turn_player_id) : null;
 for (const item of [buffer.current,...buffer.queue].filter(Boolean)) {
  const completed = (s.events || []).find(e=>e.kind==='speech' && keyFor(e.turn_id,e.player_id)===item.key);
  if (completed) buffer.put(item.key,item.pid,completed.speech || item.text.join(''),true);
  else if (item.key !== liveKey && !item.finished) buffer.put(item.key,item.pid,item.text.join(''),true);
 }
 if (['day_speech', 'last_words'].includes(s.phase) && s.current_turn_player_id) {
  buffer.put(keyFor(s.turn_id, s.current_turn_player_id), s.current_turn_player_id, s.current_speech || '');
 }
}
function paintPresentation() {
 if (!store.state || document.hidden || store.state.lifecycle === 'SUSPENDED') return;
 const pid = buffer.current?.pid;
 const waitForAudio = store.ttsEnabled && (store.audioSpeaking && store.audioReadingPid === pid || store.audioQueue.some(x => x.pid === pid));
 const {item, delta, switched} = buffer.tick(performance.now(), waitForAudio);
 const status = runtime.$('presentationStatus');
 if (status) status.textContent = buffer.queue.length ? `发言缓冲 ${buffer.queue.length} 段 · 可切换瞬时追上现场` : buffer.skipped ? '长时间积压，已跳过早期展示' : '';
 if (!item) { if (painted) { painted = null; runtime.$('liveSpeech').textContent = store.state.current_speech || '等待下一段发言…'; runtime.renderPlayers(store.state); runtime.renderHistory(store.state); } return; }
 painted = item.key;
 runtime.$('speechWho').textContent = `${item.pid} 号 · ${item.finishedAt === null ? '公开发言' : '发言结束'}`;
 runtime.$('liveSpeech').textContent = item.text.slice(0, item.shown).join('') || '正在思考…';
 if (switched) { runtime.prepareSpeech?.(item.pid, item.key); runtime.renderPlayers(store.state); runtime.renderHistory(store.state); }
 if (delta) runtime.queueSpeechDelta(delta, item.pid, false);
 if (item.finishedAt !== null && !item.flushed) { runtime.flushAudio(item.pid); item.flushed = true; }
 if (delta || switched) runtime.followStage();
}
 function initPacing() {
 const select = runtime.$('readingMode'); select.value = buffer.mode;
 select.onchange = () => { buffer.mode = select.value in readingModes ? select.value : 'standard'; localStorage.setItem('werewolf-reading-mode', buffer.mode); paintPresentation(); };
 setInterval(paintPresentation, 30);
}
Object.assign(runtime, {resetPresentation,hasPresentation,presentationPlayer,deferSpeech,paceStart,paceDelta,paceFinish,presentSnapshot,initPacing});
