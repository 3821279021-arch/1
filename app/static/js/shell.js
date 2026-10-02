import {store,runtime} from './store.js';

const pages = {home:'entry',friends:'shellFriends',battle:'shellBattle',history:'shellHistory',my:'shellMy'};
store.shellPage = 'home'; store.historyGames = [];
function hideShell() { for (const id of Object.values(pages)) runtime.$(id).classList.add('hidden'); }
function showShellPage(page = 'home') {
 if (store.roomId) return;
 store.shellPage = page in pages ? page : 'home'; hideShell(); runtime.$(pages[store.shellPage]).classList.remove('hidden');
 for (const button of runtime.$('bottomNav').querySelectorAll('button')) {
  if (button.dataset.page === store.shellPage) button.setAttribute('aria-current','page'); else button.removeAttribute('aria-current');
 }
 runtime.closeDrawers(); window.scrollTo(0,0);
 if (['history','friends'].includes(store.shellPage)) loadHistory();
 if (store.shellPage === 'my') {
  runtime.$('myModelSummary').textContent = `${store.modelPreferences.favorites.length} 个常用模型 · 搜索与最近调用状态`;
  runtime.$('myLineupSummary').textContent = `${store.modelPreferences.lineups.length} 个已保存阵容 · 入座后可套用`;
  runtime.$('myLineupCards').innerHTML=store.modelPreferences.lineups.map(l=>`<article class="history-card"><strong>${runtime.esc(l.name)} · ${l.player_count} 人</strong><p class="muted">${l.seats.length} 个 AI 座位配置</p>${[6,9,12].includes(l.player_count)?`<button class="btn subtle" data-launch-lineup="${runtime.esc(l.id)}">使用阵容创建房间 →</button>`:'<p class="muted">请在相同人数的自定义房间中套用</p>'}</article>`).join('') || '<p class="muted">在房间的「分配 AI 模型」中保存你的常用阵容。</p>';
 }
 runtime.observeAudioState?.(null);
}
async function loadHistory() {
 try {
  const {games} = await runtime.api('/api/history'); store.historyGames = games || [];
  runtime.$('historyGames').innerHTML = store.historyGames.map(g => `<article class="history-card"><strong>${g.player_count} 人 · ${runtime.esc(runtime.roleNames[g.role] || g.role)} · ${g.winner==='draw'?'平局':g.won?'获胜':'落败'}</strong><p class="muted">${runtime.esc(new Date(g.finished_at*1000).toLocaleString())}</p><p class="history-models">${runtime.esc(g.models.join(' / '))}</p><button class="btn subtle" data-history-game="${runtime.esc(g.game_id)}">详情 / 分析 / 回放 →</button></article>`).join('') || '<p class="muted">还没有已完成的对局。先开一桌吧。</p>';
  const companions = [...new Set(store.historyGames.flatMap(g=>g.companions))].slice(0,12);
  runtime.$('companions').innerHTML = companions.map(name=>`<p class="companion">♧ ${runtime.esc(name)} <small class="muted">最近同桌</small></p>`).join('') || '<p class="muted">邀请朋友玩完一局后，他们会出现在这里。</p>';
 } catch(error) { runtime.$('historyGames').textContent = error.message; }
}
function chooseBoard(mode, policy='fixed') { showShellPage('home'); runtime.$('boardPolicy').value=policy; runtime.$('modeSelect').value=mode; runtime.updateMode(); runtime.$('createBtn').focus(); }
function initShell() {
 runtime.$('bottomNav').onclick=e=>{const button=e.target.closest('[data-page]');if(button)showShellPage(button.dataset.page);};
 document.querySelectorAll('[data-recommend]').forEach(button=>button.onclick=()=>chooseBoard(button.dataset.recommend));
 runtime.$('quickStart').onclick=()=>{chooseBoard('quick6');runtime.$('createBtn').click();};
 runtime.$('homeSound').onclick=()=>runtime.$('audioToggle').click();
 runtime.$('battleCreate').onclick=()=>chooseBoard('standard12');
 runtime.$('battleJoin').onclick=()=>{showShellPage('home');runtime.$('joinCode').focus();};
 runtime.$('battleArena').onclick=()=>{chooseBoard('standard9');runtime.notice('入座后分配 AI 模型，可在私人搭档中开启托管。');};
 runtime.$('battleRandom').onclick=()=>chooseBoard('standard12','constrained_random');
 runtime.$('friendsInvite').onclick=()=>{chooseBoard('quick6');runtime.$('createBtn').click();};
 runtime.$('myModels').onclick=()=>runtime.openModels();
 runtime.$('myLineups').onclick=()=>{runtime.$('myLineupCards').classList.toggle('hidden');runtime.$('myLineupCards').scrollIntoView({block:'nearest'});};
 runtime.$('myLineupCards').onclick=e=>{const button=e.target.closest('[data-launch-lineup]');if(!button)return;runtime.busy(button,async()=>{const lineup=store.modelPreferences.lineups.find(l=>l.id===button.dataset.launchLineup);if(!lineup)return;const mode=({6:'quick6',9:'standard9',12:'standard12'})[lineup.player_count];runtime.enterRoom(await runtime.roomAPI.create({name:runtime.$('playerName').value.trim()||'玩家',mode,pace:runtime.$('paceSelect').value,action_id:runtime.uuid()}));await runtime.loadCredentials();runtime.$('lineupSelect').value=lineup.id;runtime.applyLineup();runtime.$('saveConfig').click();});};
 runtime.$('mySound').onclick=()=>{for(const id of ['seatTools','modelAudit','lifecycleControls','roomSecurity','costReportBtn'])runtime.$(id).classList.add('hidden');runtime.openDrawer('settingsDrawer');};
 runtime.$('mySession').onclick=()=>{showShellPage('home');runtime.$('entry').querySelector('.session-tools').open=true;runtime.$('recoveryInput').focus();};
 runtime.$('historyGames').onclick=e=>{const button=e.target.closest('[data-history-game]');if(button){const g=store.historyGames.find(x=>x.game_id===button.dataset.historyGame);if(g)runtime.openHistoricalAnalysis(g);}};
 showShellPage('home');
}
Object.assign(runtime,{hideShell,showShellPage,loadHistory,initShell});
