import {store, runtime} from './store.js';
runtime.$ = id=>document.getElementById(id);
runtime.esc = s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
runtime.personalities = {detective:'逻辑侦探',hunter:'激进猎手',trickster:'欺诈大师',cautious:'谨慎型',performer:'表演型',commander:'指挥官'};
runtime.roleEmoji = {'狼人':'🐺','预言家':'🔮','女巫':'🧪','村民':'🌾','猎人':'🏹','守卫':'🛡','骑士':'⚔','白痴':'🎭','狼王':'🐺','白狼王':'🐺','狼美人':'🌹','隐狼':'🌘','摄梦人':'☁','守墓人':'🪦','乌鸦':'🐦','驯熊师':'🐻'};
runtime.modes = {copilot:'副驾',consult:'商议',autopilot:'托管'};
runtime.providerNames = {mock:'Mock',dashscope:'Qwen',openai:'OpenAI',anthropic:'Claude',gemini:'Gemini'};
runtime.phaseNames = {lobby:'等待玩家',night:'夜间行动',night_dreamer:'摄梦人行动',night_grave:'守墓人行动',night_crow:'乌鸦行动',night_discussion:'狼人讨论',night_wolves:'狼人行动',night_seer:'预言家行动',night_witch:'女巫行动',night_guard:'守卫行动',night_beauty:'狼美人行动',death_skill:'死亡技能',day_speech:'白天发言',day_vote:'投票',last_words:'遗言',game_over:'游戏结束'};
runtime.title = '月下狼人杀 · AI 社交推理竞技场 V3';
runtime.uuid = ()=>globalThis.crypto?.randomUUID?.()||`${Date.now().toString(16)}-${Math.random().toString(16).slice(2)}`;
runtime.rev = s=>Number(s?.state_revision??0);
function notice(text){runtime.$('notice').textContent=text;runtime.$('notice').classList.remove('hidden');clearTimeout(store.noticeTimer);store.noticeTimer=setTimeout(()=>runtime.$('notice').classList.add('hidden'),7000);}
function status(text,online=false){runtime.$('connection').textContent=text;runtime.$('connection').classList.toggle('online',online);}
async function busy(button,operation){if(button.disabled)return;button.disabled=true;try{await operation();}catch(e){runtime.notice(e.message);}finally{button.disabled=false;}}
function safeValue(id,value){if(document.activeElement!==runtime.$(id))runtime.$(id).value=value;}
Object.assign(runtime, {notice,status,busy,safeValue});

function openDrawer(id){for(const dialog of document.querySelectorAll('dialog[open]'))dialog.close();const d=runtime.$(id);if(d&&typeof d.showModal==='function')d.showModal();}
function closeDrawers(){for(const d of document.querySelectorAll('dialog[open]'))d.close();}
Object.assign(runtime,{openDrawer,closeDrawers});
