import {store, runtime} from './store.js';
const number = (value, digits = 0) => value === null || value === undefined ? '—' : Number(value).toLocaleString('zh-CN', {maximumFractionDigits: digits});
function stopReplay() {
 clearInterval(store.replayTimer); store.replayTimer = null;
 runtime.$('replayPlay').textContent = '播放';
}
function replayText(event) {
 const data = event.data || event;
 if (data.text || data.speech || data.content) return data.text || data.speech || data.content;
 if (event.type === 'phase_changed') return `${data.phase_name || data.phase} · 完成操作后立即推进`;
 if (event.type === 'vote_result') return Object.entries(data.votes || {}).map(([seat, target]) => `${seat}号 → ${target === null ? '弃票' : target + '号'}`).join('；');
 if (event.type === 'game_finished') return ({wolves: '狼人阵营获胜', good: '好人阵营获胜', draw: '本局平局'})[data.winner] || '游戏结束';
 if (event.type === 'speech_chunk') return data.delta || data.chunk || '';
 if (event.type === 'speech_started') return `${data.player_id}号开始发言`;
 if (event.type === 'turn_finished') return `${data.player_id || ''}号完成本轮${data.timeout ? '（超时）' : ''}`;
 if (event.type === 'vote_submitted') return `${data.player_id}号已提交投票`;
 if (event.type === 'model_execution') return `${data.player_id}号使用 ${data.model_used || '模型'} · ${data.status || ''}`;
 return ({turn_started: '新回合开始', vote_started: '放逐投票开始', lobby_configured: '房间配置已更新'})[event.type] || '流程已更新';
}
function renderReplay() {
 const events = store.replayEvents || [], index = Math.max(0, Math.min(events.length - 1, Number(runtime.$('replaySlider').value) || 0)), event = events[index], data = event?.data || event;
 runtime.$('replayCounter').textContent = events.length ? `${index + 1} / ${events.length} 条公开事件` : '本局没有回放事件';
 runtime.$('replayEvent').innerHTML = event ? `<small>第 ${runtime.esc(data.day || event.day || 1)} 天 · ${runtime.esc(data.kind || event.type || '流程')}${data.player_id ? ' · ' + runtime.esc(data.player_id) + ' 号' : ''}</small>${runtime.esc(replayText(event))}` : '等待回放记录';
 runtime.$('replayPrev').disabled = index === 0;
 runtime.$('replayNext').disabled = index >= events.length - 1;
 runtime.$('replayPlay').disabled = !events.length;
}
async function loadAnalysis(gameId) {
 runtime.$('analysisContent').textContent = '正在读取本局数据…';
 runtime.stopReplay(); store.replayEvents = []; runtime.renderReplay();
 try {
  const result = store.analysisHistoryRoom ? await runtime.api(`/api/history/${store.analysisHistoryRoom}/${gameId}`) : await runtime.roomAPI.analysis(store.roomId, gameId), players = result.players || [];
  runtime.$('analysisContent').innerHTML = `<p class="winner">${({wolves:'狼人阵营获胜',good:'好人阵营获胜',draw:'本局平局'})[result.winner] || '本局结果'}</p><p class="muted">身份已公开。用量和延迟来自本局记录；未报价成本显示为 —，价格单位以部署者配置为准。Mock 不代表真实模型能力。</p><div class="analysis-grid">${players.map(p => `<article class="analysis-seat"><strong>${runtime.esc(p.id)} 号 · ${runtime.esc(p.name)} ${p.won ? '✧ 获胜' : ''}</strong><p class="role-line">${runtime.esc(p.role_name || runtime.roleNames[p.role] || p.role || '')} · ${runtime.esc(({wolves:'狼队',good:'好人'})[p.faction] || p.faction || '')}</p><p>${runtime.esc(p.model || p.model_id || '真人')}</p><div class="stat-line"><span>发言 <b>${number(p.speeches)}</b></span><span>投票 <b>${number(p.votes)}</b></span><span>技能 <b>${number(p.skills)}</b></span><span>投中狼人 <b>${number(p.vote_hits)}</b></span></div><div class="stat-line"><span>延迟 <b>${number(p.latency_ms)} ms</b></span><span>首 token <b>${number(p.first_token_ms)} ms</b></span><span>真实生成 <b>${number(p.generation_ms)} ms</b></span><span>字数 <b>${number(p.output_characters)}</b></span><span>输入 / 输出 <b>${number(p.input_tokens)} / ${number(p.output_tokens)}</b></span></div><div class="stat-line"><span>估算成本 <b>${number(p.estimated_cost, 6)}</b></span><span>错误 <b>${number(p.errors)}</b></span><span>兜底 <b>${number(p.fallbacks)}</b></span><span>判断变化 <b>${number(p.judgment_change_count)} 次（公开声明）</b></span></div></article>`).join('')}</div>`;
  store.replayEvents = result.events || [];
  runtime.$('replaySlider').max = Math.max(0, store.replayEvents.length - 1);
  runtime.$('replaySlider').value = 0; runtime.renderReplay();
 } catch (error) { runtime.$('analysisContent').textContent = error.message; }
}
async function openAnalysis() {
 store.analysisHistoryRoom = null;
 runtime.openDrawer('analysisDrawer'); runtime.stopReplay();
 try {
  const response = await runtime.roomAPI.games(store.roomId), games = response.games || [], select = runtime.$('analysisGame');
  select.innerHTML = games.map((g, i) => `<option value="${runtime.esc(g.game_id)}">${i === 0 ? '最近一局' : '历史对局'} · ${runtime.esc(g.game_id.slice(0, 8))} · ${runtime.esc(({wolves:'狼队获胜',good:'好人获胜',draw:'平局'})[g.winner] || '')}</option>`).join('');
  if (!games.length) { runtime.$('analysisContent').textContent = '本房间尚无已完成对局。结束后可在这里查看身份、统计和回放。'; store.replayEvents = []; runtime.renderReplay(); return; }
  if (games.some(g => g.game_id === store.state?.game_id)) select.value = store.state.game_id;
  await loadAnalysis(select.value);
 } catch (error) { runtime.$('analysisContent').textContent = error.message; }
}
async function openHistoricalAnalysis(game) {
 store.analysisHistoryRoom = game.room_id;
 runtime.openDrawer('analysisDrawer');
 runtime.$('analysisGame').innerHTML = store.historyGames.filter(g=>g.room_id===game.room_id).map(g=>`<option value="${runtime.esc(g.game_id)}">${runtime.esc(new Date(g.finished_at*1000).toLocaleString())} · ${runtime.esc(g.game_id.slice(0,8))}</option>`).join('');
 runtime.$('analysisGame').value = game.game_id; await loadAnalysis(game.game_id);
}
function initAnalysis() {
 runtime.$('analysisOpen').onclick = runtime.openAnalysis;
 runtime.$('historyAnalysisOpen').onclick = runtime.openAnalysis;
 runtime.$('analysisGame').onchange = () => loadAnalysis(runtime.$('analysisGame').value);
 runtime.$('replaySlider').oninput = () => { runtime.stopReplay(); runtime.renderReplay(); };
 for (const [id, step] of [['replayPrev', -1], ['replayNext', 1]]) runtime.$(id).onclick = () => { runtime.stopReplay(); runtime.$('replaySlider').value = Number(runtime.$('replaySlider').value) + step; runtime.renderReplay(); };
 runtime.$('replayPlay').onclick = () => {
  if (store.replayTimer) { runtime.stopReplay(); return; }
  if (Number(runtime.$('replaySlider').value) >= store.replayEvents.length - 1) runtime.$('replaySlider').value = 0;
  runtime.renderReplay(); runtime.$('replayPlay').textContent = '暂停';
  store.replayTimer = setInterval(() => { const next = Number(runtime.$('replaySlider').value) + 1; if (next >= store.replayEvents.length) { runtime.stopReplay(); return; } runtime.$('replaySlider').value = next; runtime.renderReplay(); }, 1700);
 };
 runtime.$('analysisDrawer').addEventListener('close', runtime.stopReplay);
}
Object.assign(runtime, {openAnalysis, openHistoricalAnalysis, loadAnalysis, renderReplay, stopReplay, replayText, initAnalysis});
