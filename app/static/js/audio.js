import {store, runtime} from './store.js';

// Short local WAV cues work without a speech engine or an external service.
const cueKinds = new Set(['turn', 'start', 'end', 'vote', 'night', 'day', 'gameover', 'test']);
const maxSpeechQueue = 80;
let cuePlayer, cuePlaying = false, cueGeneration = 0, cueWatchdog;
const cueQueue = [], cueSeen = new Map();
let observed = null, speechWatchdog, voicesTimer;
store.ttsEnabled ??= false;
store.ttsRate ??= 1;
store.audioError = '';
store.ttsError = '';
store.audioReadingPid = null;
store.audioBacklogSkipped = false;

const node = id => runtime.$(id);
function updateAudioStatus() {
 const status = node('audioStatus');
 const reading = store.audioSpeaking ? `正在朗读${store.audioReadingPid || ''}号` : store.audioQueue.length ? '朗读准备中' : '';
 if (status) status.textContent = [store.audioEnabled ? '提示音已开启' : '提示音已关闭', store.ttsEnabled ? `AI 朗读已开启 · ${store.ttsRate.toFixed(1)} 倍速` : 'AI 朗读已关闭', reading, store.ttsEnabled ? `待播${store.audioQueue.length}句` : '', store.audioBacklogSkipped ? '积压已跳过早期朗读' : '', store.audioError, store.ttsError].filter(Boolean).join(' · ');
 const stop = node('audioStop');
 if (stop) stop.disabled = false;
}
function audioFailure(error) {
 const name = error?.name || '';
 store.audioError = name === 'NotAllowedError' ? '提示音被浏览器阻止（autoplay / not-allowed），请再次点击启用声音' : name === 'NotSupportedError' ? '提示音格式不受此浏览器支持' : `提示音播放失败${name ? `（${name}）` : '，请检查设备声音并重试'}`;
 store.audioEnabled = false;
 const toggle = node('audioToggle');
 if (toggle) { toggle.textContent = '重试提示音'; toggle.setAttribute('aria-pressed', 'false'); }
 updateAudioStatus();
}
function stopCues() {
 cueGeneration++;
 cueQueue.length = 0;
 cuePlaying = false;
 clearTimeout(cueWatchdog);
 if (cuePlayer) { cuePlayer.pause(); try { cuePlayer.currentTime = 0; } catch (_) {} }
 updateAudioStatus();
}
function pumpCues() {
 if (!store.audioEnabled || cuePlaying || !cueQueue.length || !cuePlayer) return;
 const kind = cueQueue.shift(), generation = ++cueGeneration;
 let completed = false;
 cuePlaying = true;
 cuePlayer.src = `/static/assets/audio/${kind}.wav`;
 const done = () => {
  if (generation !== cueGeneration || completed) return;
  completed = true;
  clearTimeout(cueWatchdog);
  cuePlaying = false;
  updateAudioStatus();
  pumpCues();
 };
 cuePlayer.onended = done;
 cuePlayer.onerror = () => { if (generation !== cueGeneration) return; audioFailure(cuePlayer.error); done(); };
 // A stalled local request also receives a visible diagnosis.
 cueWatchdog = setTimeout(() => { if (generation !== cueGeneration) return; cuePlayer.pause(); audioFailure({name: 'TimeoutError'}); done(); }, 7000);
 try {
  const result = cuePlayer.play();
  if (result?.then) result.then(() => { if (generation === cueGeneration) { store.audioError = ''; updateAudioStatus(); } }).catch(error => { if (generation === cueGeneration) { audioFailure(error); done(); } });
 } catch (error) { audioFailure(error); done(); }
 updateAudioStatus();
}
function playCue(kind, identity) {
 if (!cueKinds.has(kind) || !store.audioEnabled || !cuePlayer) return false;
 // Both live packets and snapshots may report the same transition.
 const s = store.state || {}, turn = s.turn_id || s.turn_sequence || '', key = identity || (['start', 'end'].includes(kind) ? `${s.game_id || ''}:${turn}:${s.current_turn_player_id || ''}` : `${s.game_id || ''}:${turn}:${s.phase || ''}`);
 const seenKey = `${kind}:${key}`;
 if (kind !== 'test' && cueSeen.has(seenKey)) return false;
 if (kind !== 'test') { cueSeen.set(seenKey, true); if (cueSeen.size > 250) cueSeen.delete(cueSeen.keys().next().value); }
 cueQueue.push(kind);
 pumpCues();
 return true;
}
function clearAudio(stopPrompts = true) {
 store.audioGeneration++;
 store.audioQueue = [];
 store.audioBuffer = '';
 store.audioSeat = null;
 store.audioTurnKey = null;
 store.audioSpeaking = false;
 store.audioReadingPid = null;
 store.audioBacklogSkipped = false;
 store.ttsTestPending = false;
 clearTimeout(voicesTimer);
 clearTimeout(speechWatchdog);
 try { store.synth?.cancel(); } catch (_) {}
 if (stopPrompts) stopCues();
 updateAudioStatus();
}
function enqueueSpeech(text, pid) {
 if (!store.ttsEnabled || !text?.trim()) return;
 store.audioQueue.push({text: text.trim(), pid, generation: store.audioGeneration});
 if (store.audioQueue.length > maxSpeechQueue) {
  // The active utterance has already left this queue, so it is never cut short.
  store.audioQueue.splice(0, store.audioQueue.length - maxSpeechQueue);
  store.audioBacklogSkipped = true;
 }
 updateAudioStatus();
}
function finishSpeechBuffer() {
 flushAudio(store.audioSeat);
}
function prepareSpeech(pid, turnKey) {
 if (store.audioSeat !== pid || store.audioTurnKey !== turnKey) finishSpeechBuffer();
 store.audioSeat = pid;
 store.audioTurnKey = turnKey || null;
}
function utteranceFor(text, pid) {
 const u = new SpeechSynthesisUtterance(text), p = store.state?.players?.find(p => p.id === pid), profile = p?.voice_profile || {}, voices = store.synth?.getVoices?.() || [], zh = voices.filter(v => /^zh([-_]|$)/i.test(v.lang)), index = Math.max(0, Number(pid || 1) - 1);
 u.lang = 'zh-CN';
 u.voice = voices.find(v => v.voiceURI === profile.voice_id || v.name === profile.voice_id) || zh[index % Math.max(zh.length, 1)] || voices[index % Math.max(voices.length, 1)] || null;
 u.rate = Math.max(.6, Math.min(1.5, Number(store.ttsRate) || 1));
 u.pitch = Math.max(.4, Math.min(1.8, Number(profile.pitch) || [1, .8, 1.25, .95, 1.1, .65][index % 6]));
 u.volume = Math.max(0, Math.min(1, profile.volume === undefined ? .8 : Number(profile.volume)));
 return u;
}
function speechFailure(reason) {
 const labels = {'not-allowed': '浏览器阻止朗读（not-allowed），请重新勾选 AI 朗读', 'audio-busy': '声音设备正忙，稍后可重新开启朗读', 'audio-hardware': '声音设备不可用', 'network': '系统语音服务网络不可用', 'synthesis-unavailable': '系统语音服务不可用', 'synthesis-failed': '系统语音合成失败', 'language-unavailable': '系统没有可用的中文语音', 'voice-unavailable': '所选系统语音不可用', 'timeout': '系统语音服务未响应，请重新开启 AI 朗读', 'no-voices': '系统没有可用的 TTS 声音（无 voices），请安装系统语音后重试'};
 store.ttsError = `朗读失败：${labels[reason] || reason || '未知系统错误'}`;
 updateAudioStatus();
}
function pumpAudio() {
 if (!store.ttsEnabled || store.audioSpeaking || !store.audioQueue.length || !store.synth) return;
 if (!(store.synth.getVoices?.() || []).length) { store.audioQueue = []; speechFailure('no-voices'); return; }
 const item = store.audioQueue.shift();
 if (item.generation !== store.audioGeneration) { pumpAudio(); return; }
 let u;
 try { u = utteranceFor(item.text, item.pid); } catch (error) { store.audioQueue = []; speechFailure(error?.name || 'synthesis-failed'); return; }
 store.audioSpeaking = true;
 store.audioReadingPid = item.pid;
 let completed = false;
 const done = () => {
  if (item.generation !== store.audioGeneration || completed) return;
  completed = true;
  clearTimeout(speechWatchdog);
  store.audioSpeaking = false;
  store.audioReadingPid = null;
  updateAudioStatus();
  pumpAudio();
 };
 u.onstart = () => { if (item.generation !== store.audioGeneration || completed) return; clearTimeout(speechWatchdog); store.ttsError = ''; updateAudioStatus(); speechWatchdog = setTimeout(() => { if (item.generation !== store.audioGeneration || completed) return; clearAudio(false); speechFailure('timeout'); }, Math.max(20000, item.text.length * 800)); };
 u.onend = done;
 u.onerror = event => { if (item.generation !== store.audioGeneration || completed) return; if (!['canceled', 'interrupted'].includes(event.error)) speechFailure(event.error); done(); };
 speechWatchdog = setTimeout(() => { if (item.generation !== store.audioGeneration || completed) return; clearAudio(false); speechFailure('timeout'); }, 6000);
 try { store.synth.resume?.(); store.synth.speak(u); } catch (error) { speechFailure(error?.name || 'synthesis-failed'); done(); }
 updateAudioStatus();
}
function queueSpeechDelta(delta, pid, finished) {
 if (!store.ttsEnabled || !store.synth || !store.state?.players?.some(p => p.id === pid && !p.is_human)) return;
 if (store.audioSeat !== pid) prepareSpeech(pid, store.state.turn_id || store.state.turn_sequence || null);
 store.audioBuffer += delta || '';
 let split;
 while ((split = /[。！？!?；;\n]/.exec(store.audioBuffer))) {
  const text = store.audioBuffer.slice(0, split.index + 1).trim();
  store.audioBuffer = store.audioBuffer.slice(split.index + 1);
  if (text) enqueueSpeech(text, pid);
 }
 if (finished) flushAudio(pid);
 pumpAudio();
}
function flushAudio(pid) {
 if (store.ttsEnabled && store.audioBuffer.trim()) enqueueSpeech(store.audioBuffer, pid ?? store.audioSeat);
 store.audioBuffer = '';
 pumpAudio();
}
function speakPet(text) {
 if (!node('petAudioToggle')?.checked || !store.ttsEnabled || !store.synth || !text) return;
 enqueueSpeech(text, store.state?.self?.id || 1);
 pumpAudio();
}
function observeAudioState(s, prev) {
 if (!s) return;
 // Keep a compact copy: packet handlers mutate the previous snapshot in place.
 const prior = observed?.game === s.game_id ? observed : prev?.game_id === s.game_id ? audioObservation(prev) : null;
 const next = audioObservation(s);
 if (prior && prior.speech && prior.speech !== next.speech) playCue('end', `${s.game_id}:${prior.speech}`);
 if (next.speech && next.speech !== prior?.speech) playCue('start');
 if (next.pending && next.pending !== prior?.pending) playCue('turn', `${s.game_id}:${next.pending}`);
 if (prior && next.phase !== prior.phase) {
  if (next.phase === 'day_vote') playCue('vote');
  if (next.night && !prior.night) playCue('night');
  if (!next.night && prior.night && !next.over) playCue('day');
 }
 if (next.over && !prior?.over) playCue('gameover');
 observed = next;
}
function audioObservation(s) {
 const turn = s.turn_id || s.turn_sequence || '', phase = s.phase || '', speech = ['day_speech', 'last_words'].includes(phase) && s.current_turn_player_id ? `${turn}:${s.current_turn_player_id}` : '';
 const p = s.pending_action;
 return {game: s.game_id, phase, night: phase.startsWith('night'), over: Boolean(s.game_over), speech, pending: p ? `${p.turn_id || turn}:${p.type}:${s.self?.id || ''}` : ''};
}
function testSpeech() {
 if (!store.ttsEnabled) return;
 if (!(store.synth?.getVoices?.() || []).length) {
  store.ttsTestPending = true;
  store.ttsError = '正在加载系统 TTS 声音…';
  clearTimeout(voicesTimer);
  voicesTimer = setTimeout(() => { if (store.ttsEnabled && store.ttsTestPending) { store.ttsTestPending = false; speechFailure('no-voices'); } }, 1800);
  updateAudioStatus();
  return;
 }
 store.ttsTestPending = false;
 clearTimeout(voicesTimer);
 enqueueSpeech('AI 朗读已开启。这是一段语音测试。', store.state?.self?.id || 1);
 pumpAudio();
}
function initAudio() {
 try {
  cuePlayer = new Audio('/static/assets/audio/test.wav');
  cuePlayer.preload = 'auto';
  cuePlayer.setAttribute('playsinline', '');
  cuePlayer.volume = .85;
  cuePlayer.load();
 } catch (error) { audioFailure(error); }
 const toggle = node('audioToggle');
 if (toggle) toggle.onclick = () => {
  store.audioEnabled = !store.audioEnabled;
  toggle.textContent = store.audioEnabled ? '关闭提示音' : '启用声音';
  toggle.setAttribute('aria-pressed', String(store.audioEnabled));
  if (store.audioEnabled) {
   store.audioError = '';
   // play() happens synchronously inside this click to unlock iOS / Safari.
   stopCues();
   playCue('test');
  } else stopCues();
  updateAudioStatus();
 };
 const stop = node('audioStop');
 if (stop) stop.onclick = () => { clearAudio(); const status = node('audioStatus'); if (status) status.textContent = '已停止当前声音 · 下次提示和公开 AI 发言继续'; };
 const tts = node('ttsToggle'), rate = node('ttsRate');
 if (tts) {
  tts.checked = store.ttsEnabled;
  tts.onchange = () => {
   store.ttsEnabled = tts.checked;
   clearAudio(false);
   store.ttsError = '';
   store.ttsTestPending = false;
   clearTimeout(voicesTimer);
   if (store.ttsEnabled) testSpeech();
   updateAudioStatus();
  };
 }
 if (rate) {
  rate.value = store.ttsRate;
  const changeRate = () => { store.ttsRate = Math.max(.6, Math.min(1.5, Number(rate.value) || 1)); updateAudioStatus(); };
  rate.oninput = changeRate;
  rate.onchange = changeRate;
 }
 if (!store.synth || !window.SpeechSynthesisUtterance) {
  if (tts) tts.disabled = true;
  if (rate) rate.disabled = true;
  if (node('petAudioToggle')) node('petAudioToggle').disabled = true;
  store.ttsError = '此浏览器不支持 TTS，提示音可正常使用';
 } else {
  const voicesChanged = () => { if (store.ttsTestPending && (store.synth.getVoices?.() || []).length) testSpeech(); };
  store.synth.addEventListener?.('voiceschanged', voicesChanged);
  store.synth.getVoices?.();
 }
 updateAudioStatus();
}

Object.assign(runtime, {clearAudio, prepareSpeech, finishSpeechBuffer, utteranceFor, pumpAudio, queueSpeechDelta, flushAudio, speakPet, playCue, observeAudioState, initAudio});
