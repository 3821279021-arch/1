import {store, runtime} from './store.js';

store.modelPreferences = {favorites: [], recent: [], lineups: []};
store.platformModels = []; store.catalogLimit = 100;
const statusNames = {success:'最近成功',no_permission:'无权限',quota_exhausted:'额度不足',rate_limited:'限流',not_found:'模型不存在',error:'调用失败'};
function preferenceStatus(key) {
 const record = store.modelPreferences.recent.find(x => x.key === key);
 return record ? `${statusNames[record.status] || '调用失败'} · ${new Date(record.checked_at * 1000).toLocaleString()}` : '未测试';
}
function preferenceGroup(key) {
 if (store.modelPreferences.favorites.includes(key)) return '⭐ 我的常用';
 if (store.modelPreferences.recent.some(x => x.key === key)) return '🕘 最近使用';
 return '📚 全部模型';
}
function modelPriority(key) {
 const fav = store.modelPreferences.favorites.indexOf(key);
 if (fav >= 0) return -1000 + fav;
 const recent = store.modelPreferences.recent.findIndex(x => x.key === key);
 if (recent >= 0) return store.modelPreferences.recent[recent].status === 'success' ? -500 + recent : -200 + recent;
 return 1000;
}
function catalogModels() {
 const rows = (store.state ? runtime.registry(store.state) : store.platformModels).map(m => ({key:m.key,label:m.display_name || m.model || m.key}));
 for (const c of store.credentials) for (const m of c.models || []) {
  const id = m.id || m.model_id || m.model;
  rows.push({key:`credential:${c.id}:${id}`, label:`${m.display_name || id} · 我的 ${runtime.providerNames[c.provider] || c.provider}`});
 }
 for (const key of store.modelPreferences.favorites) if (!rows.some(x => x.key === key)) rows.push({key,label:key});
 return [...new Map(rows.map(row => [row.key,row])).values()];
}
function renderModelCatalog() {
 const q = runtime.$('modelSearch').value.trim().toLowerCase();
 const rows = catalogModels().filter(m => `${m.label} ${m.key}`.toLowerCase().includes(q));
 const groups = ['⭐ 我的常用','🕘 最近使用','📚 全部模型'];
 runtime.$('modelCatalog').innerHTML = groups.map(group => {
  const all = rows.filter(m => preferenceGroup(m.key) === group).sort((a,b) => modelPriority(a.key)-modelPriority(b.key));
  const shown = group === '📚 全部模型' ? all.slice(0,store.catalogLimit) : all;
  return `<section class="model-group"><h3>${group} <small>${all.length}</small></h3>${shown.map(m => `<article class="model-row"><div><strong>${runtime.esc(m.label)}</strong><code>${runtime.esc(m.key)}</code><small>${runtime.esc(preferenceStatus(m.key))}</small></div><button class="btn subtle model-star" data-favorite="${runtime.esc(m.key)}" aria-label="${store.modelPreferences.favorites.includes(m.key)?'取消收藏':'收藏'} ${runtime.esc(m.label)}" aria-pressed="${store.modelPreferences.favorites.includes(m.key)}">${store.modelPreferences.favorites.includes(m.key)?'★':'☆'}</button></article>`).join('') || '<p class="muted">暂无模型</p>'}${shown.length < all.length ? '<button class="btn subtle" data-more-models>显示更多模型</button>' : ''}</section>`;
 }).join('');
 const selected = runtime.$('lineupSelect').value;
 runtime.$('lineupSelect').innerHTML = '<option value="">选择已保存阵容</option>'+store.modelPreferences.lineups.map(l => `<option value="${runtime.esc(l.id)}">${runtime.esc(l.name)} · ${l.player_count} 人</option>`).join('');
 if (store.modelPreferences.lineups.some(l => l.id === selected)) runtime.$('lineupSelect').value = selected;
}
async function loadModelPreferences() {
 await runtime.ensureSession();const token=store.token;
 const preferences=await runtime.api('/api/models/preferences');
 const result=await runtime.api('/api/models');if(token!==store.token)return;store.modelPreferences=preferences;store.platformModels=result.models||[];
 renderModelCatalog(); refreshSeatModelOrder();
}
async function toggleFavorite(key) {
 if (!key || key === 'auto' || key.startsWith('manual:') || key === 'missing') return;
 const favorites = store.modelPreferences.favorites.includes(key) ? store.modelPreferences.favorites.filter(x => x !== key) : [...store.modelPreferences.favorites,key];
 store.modelPreferences = await runtime.api('/api/models/preferences','PUT',{favorites});
 renderModelCatalog(); refreshSeatModelOrder();
}
function refreshSeatModelOrder() {
 for (const select of runtime.$('seatConfigs').querySelectorAll('.bot-model')) {
  const value = select.value, raw = select._allOptions || [...select.options].map(x => x.cloneNode(true));
  select._allOptions = raw;
  const q = select.closest('[data-seat]').querySelector('.seat-model-search')?.value.trim().toLowerCase() || '';
  const options = raw.filter(x => !q || x.value === value || x.value === 'auto' || x.dataset.manual || `${x.value} ${x.textContent}`.toLowerCase().includes(q));
  select.replaceChildren();
  for (const option of options.filter(x => x.value === 'auto')) select.append(option.cloneNode(true));
  for (const group of ['⭐ 我的常用','🕘 最近使用','📚 全部模型']) {
   const rows = options.filter(x => x.value !== 'auto' && preferenceGroup(x.value) === group).sort((a,b) => modelPriority(a.value)-modelPriority(b.value));
   if (!rows.length) continue;
   const optgroup = document.createElement('optgroup'); optgroup.label = group;
   for (const original of rows) { const option = original.cloneNode(true); option.selected = option.value === value; option.textContent = `${original.textContent.split(' · 最近')[0]} · ${preferenceStatus(option.value)}`; optgroup.append(option); }
   select.append(optgroup);
  }
  select.value = value;
  const star = select.closest('[data-seat]').querySelector('.favorite-seat-model');
  if (star) { star.textContent = store.modelPreferences.favorites.includes(value) ? '★' : '☆'; star.setAttribute('aria-pressed',String(store.modelPreferences.favorites.includes(value))); }
 }
}
async function saveLineup() {
 if (!store.state || store.state.phase !== 'lobby' || !store.state.is_host) throw new Error('请先进入自己的大厅分配模型');
 const name = runtime.$('lineupName').value.trim(); if (!name) throw new Error('请输入阵容名称');
 const seats = [...runtime.$('seatConfigs').querySelectorAll('[data-seat]')].map(row => {
  const select = row.querySelector('.bot-model');
  const key = select.selectedOptions[0]?.dataset.manual ? `credential:${select.selectedOptions[0].dataset.credentialId}:${row.querySelector('.bot-model-id').value.trim()}` : select.value;
  return {id:Number(row.dataset.seat),model_key:key,personality:row.querySelector('.bot-personality').value};
 });
 const lineup = {id:runtime.uuid(), name, player_count:store.state.player_count, seats};
 store.modelPreferences = await runtime.api('/api/models/preferences','PUT',{lineups:[...store.modelPreferences.lineups,lineup]});
 renderModelCatalog(); runtime.$('lineupSelect').value = lineup.id; runtime.notice('阵容已绑定身份保存');
}
function applyLineup() {
 const lineup = store.modelPreferences.lineups.find(x => x.id === runtime.$('lineupSelect').value);
 if (!lineup || !store.state) throw new Error('请选择一个阵容');
 if (lineup.player_count !== store.state.player_count) throw new Error(`此阵容适用于 ${lineup.player_count} 人板子`);
 const assignments = lineup.seats.map(seat => {
  const row = runtime.$('seatConfigs').querySelector(`[data-seat="${seat.id}"]`);
  if (!row) return null; // A human occupies this seat now.
  const select = row.querySelector('.bot-model'), option = [...(select._allOptions || select.options)].find(o => o.value === seat.model_key && !o.disabled);
  let manual=null;if(!option&&seat.model_key.startsWith('credential:')){const parts=seat.model_key.split(':');manual=[...(select._allOptions||select.options)].find(o=>o.dataset.manual&&o.dataset.credentialId===parts[1]);}
  if (!option&&!manual) throw new Error(`${seat.id} 号的模型已不可用，请更新阵容`);
  return {row,select,seat,manual};
 }).filter(Boolean);
 for (const {row} of assignments) { const search = row.querySelector('.seat-model-search'); if (search) search.value = ''; }
 refreshSeatModelOrder();
 for (const {row,select,seat,manual} of assignments) { select.value = manual?manual.value:seat.model_key;if(manual)row.querySelector('.bot-model-id').value=seat.model_key.split(':').slice(2).join(':'); row.querySelector('.bot-personality').value = seat.personality || 'detective'; select.onchange(); }
 runtime.notice('阵容已套用，点击保存模型分配生效');
}
function initModelPreferences() {
 runtime.$('modelSearch').oninput = () => { store.catalogLimit = 100; renderModelCatalog(); };
 runtime.$('modelCatalog').onclick = e => { const star = e.target.closest('[data-favorite]'); if (star) runtime.busy(star,()=>toggleFavorite(star.dataset.favorite)); if (e.target.closest('[data-more-models]')) { store.catalogLimit += 100; renderModelCatalog(); } };
 runtime.$('saveLineup').onclick = () => runtime.busy(runtime.$('saveLineup'),saveLineup);
 runtime.$('applyLineup').onclick = () => runtime.busy(runtime.$('applyLineup'),async()=>applyLineup());
 runtime.$('deleteLineup').onclick = () => runtime.busy(runtime.$('deleteLineup'),async()=>{store.modelPreferences=await runtime.api('/api/models/preferences','PUT',{lineups:store.modelPreferences.lineups.filter(x=>x.id!==runtime.$('lineupSelect').value)});renderModelCatalog();});
}
Object.assign(runtime,{preferenceStatus,modelPriority,loadModelPreferences,renderModelCatalog,toggleFavorite,refreshSeatModelOrder,applyLineup,initModelPreferences});
