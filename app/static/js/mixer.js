import {store, runtime} from './store.js';

let saved = {};
try { saved = JSON.parse(localStorage.getItem('werewolf-mix') || '{}'); } catch (_) {}
export const mixPreferences = {bgm: .3, host: .8, sfx: .85, ...saved};
let bgm = [], active = 0, track = null, host, hostPlaying = false, hostGeneration = 0, musicGeneration = 0, observed = null, unlocking = false;
const queue = [], seen = new Set();
const privateRoles = {
 wolf_discuss: ['wolf','wolf_king','white_wolf_king','wolf_beauty','hidden_wolf'],
 wolf_kill: ['wolf','wolf_king','white_wolf_king','wolf_beauty','hidden_wolf'],
 seer_inspect: ['seer'], witch: ['witch'], guard_protect: ['guard'], wolf_beauty_charm: ['wolf_beauty'],
 hunter_shoot: ['hunter'], wolf_king_shoot: ['wolf_king'], dream_visit: ['dreamer'],
 grave_inspect: ['gravekeeper'], crow_mark: ['crow'], bear_watch: ['bear_trainer']
};
const musicFor = s => !s || s.phase === 'lobby' ? 'lobby' : s.game_over ? 'settlement' : s.phase === 'day_vote' ? 'vote' : s.phase.startsWith('night') ? 'night' : 'day';
function failed(error) { runtime.audioFailure?.(error); }
export function stopMixer() {
 track = null; hostGeneration++; musicGeneration++; queue.length = 0; hostPlaying = false; unlocking = false;
 for (const player of bgm) player.pause();
 if (host) { host.pause(); host.onended = null; }
}
function pumpHost() {
 if (!store.audioEnabled || unlocking || hostPlaying || !queue.length || !host) return;
 const generation = ++hostGeneration, name = queue.shift(); hostPlaying = true;
 host.src = `/static/assets/audio/host/${name}.mp3`; host.volume = Number(mixPreferences.host);
 const done = () => { if (generation !== hostGeneration) return; hostPlaying = false; pumpHost(); };
 host.onended = done; host.onerror = () => { if (generation === hostGeneration) { failed(host.error); done(); } };
 try { host.play()?.catch(error => { if (generation === hostGeneration) { failed(error); done(); } }); } catch (error) { failed(error); done(); }
}
export function playHost(name, identity) {
 if (!store.audioEnabled || document.hidden || unlocking) return;
 const key = `${identity}:${name}`; if (seen.has(key)) return;
 seen.add(key); if (seen.size > 250) seen.delete(seen.values().next().value);
 if (queue.length < 24) queue.push(name);
 pumpHost();
}
function setMusic(name) {
 if (!store.audioEnabled || document.hidden || unlocking || name === track || !bgm.length) return;
 track = name; active = 1 - active;
 const generation=++musicGeneration;
 const player = bgm[active]; player.pause(); player.src = `/static/assets/audio/bgm/${name}.mp3`; player.volume = 0;
 const reject=error=>{if(generation===musicGeneration && track===name && store.audioEnabled)failed(error);};
 try { player.play()?.catch(reject); } catch (error) { reject(error); }
}
function privateAnnouncement(s) {
 const pending = s?.pending_action, role = s?.self?.role_key;
 if (pending && privateRoles[pending.type]?.includes(role)) playHost(pending.type, `${s.game_id}:${s.turn_id}:${s.self.id}`);
}
export function observeMixer(s) {
 if (!s || unlocking) return;
 if (s.lifecycle === 'SUSPENDED' || s.lifecycle === 'ARCHIVED') { stopMixer(); observed = null; return; }
 const next = {game: s.game_id, phase: s.phase, turn: s.turn_id, night: s.phase.startsWith('night'), over: s.game_over};
 const prior = observed?.game === next.game ? observed : null;
 setMusic(musicFor(s));
 const identity = `${next.game}:${next.turn}`;
 if (next.phase === 'lobby' && !prior) playHost('welcome',identity);
 if (next.night && (!prior || !prior.night)) playHost('night', identity);
 if (prior?.night && !next.night && !next.over) playHost('day', identity);
 if (next.phase === 'day_speech' && prior?.phase !== next.phase) playHost('speech', identity);
 if (next.phase === 'day_vote' && prior?.phase !== next.phase) playHost('vote', identity);
 if (prior?.phase === 'day_vote' && next.phase !== prior.phase) playHost('vote_end', `${prior.game}:${prior.turn}`);
 if (next.phase === 'last_words' && s.current_turn_player_id && next.turn !== prior?.turn) playHost(`out-${s.current_turn_player_id}`, identity);
 if (next.over && !prior?.over) playHost('gameover', identity);
 privateAnnouncement(s);
 observed = next;
}
export function unlockMixer() {
 // Unlock each HTMLMediaElement synchronously in the click, including Safari.
 const generation = ++hostGeneration;
 const players=[host,...bgm];track=null;unlocking=true;
 try {
  const pending=players.map(player=>{player.pause();player.src='/static/assets/audio/test.wav';player.volume=0;return player.play();});
  Promise.all(pending).then(()=>{
   if(generation!==hostGeneration || !store.audioEnabled)return;
   unlocking=false;
   for(const player of players)player.pause();hostPlaying=false;observed=null;
   if(store.state)observeMixer(store.state);else setMusic('lobby');
  }).catch(error=>{if(generation===hostGeneration)failed(error);});
 } catch(error) { if(generation===hostGeneration)failed(error); }
}
export function initMixer() {
 host = new Audio('/static/assets/audio/test.wav'); host.preload = 'auto'; host.setAttribute('playsinline','');
 bgm = [new Audio(), new Audio()]; for (const player of bgm) { player.loop = true; player.preload = 'auto'; player.setAttribute('playsinline',''); }
 for (const channel of ['bgm','host','sfx']) {
  const control = runtime.$(`${channel}Volume`); if (!control) continue;
  control.value = mixPreferences[channel];
  control.oninput = () => { mixPreferences[channel] = Math.max(0, Math.min(1, Number(control.value))); localStorage.setItem('werewolf-mix', JSON.stringify(mixPreferences)); if (host) host.volume = mixPreferences.host; runtime.updateAudioStatus?.(); };
 }
 setInterval(() => {
  if (unlocking) return;
  const duck = hostPlaying || store.audioSpeaking || Boolean(runtime.presentationPlayer?.());
  for (let i = 0; i < bgm.length; i++) {
   const target = store.audioEnabled && !document.hidden && track && i === active ? mixPreferences.bgm * (duck ? .3 : 1) : 0;
   bgm[i].volume += (target - bgm[i].volume) * .14;
   if (i !== active && bgm[i].volume < .001) bgm[i].pause();
  }
 }, 50);
 document.addEventListener('visibilitychange', () => { if (document.hidden) stopMixer(); else if (store.audioEnabled) { if (store.state) observeMixer(store.state); else setMusic('lobby'); } });
}
Object.assign(runtime, {playHost,observeMixer,startLobbyMusic:()=>setMusic('lobby')});
