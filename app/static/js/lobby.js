import {store, runtime} from './store.js';
function options(selected){return Object.entries(runtime.personalities).map(([id,name])=>`<option value="${id}" ${id===selected?'selected':''}>${name}</option>`).join('');}
function registry(s){const rows=s.model_registry||s.provider_status?.models||s.providers?.models||[];return Array.isArray(rows)?rows:Object.values(rows);}
function modelAvailable(m){return m.configured&&m.enabled!==false&&m.healthy!==false;}
function modelHealth(m){return !m?.configured?'未配置':m.enabled===false?'已停用':m.healthy===false?'暂时不可用':m.provider==='mock'?'本地模拟':'可用';}
function renderProviders(s){
 const rows=runtime.registry(s),providers=s.provider_status||s.providers||{};
 runtime.$('providerStatus').innerHTML=Object.keys(runtime.providerNames).map(name=>{const candidates=rows.filter(m=>m.provider===name);const p=providers[name];const available=name==='mock'||candidates.some(runtime.modelAvailable)||(!candidates.length&&p?.configured);return `<span class="provider-chip ${available?'available':'unavailable'}">${runtime.providerNames[name]} · ${available?'可用':(candidates.some(m=>m.configured)||p?.configured?'暂时不可用':'未配置')}</span>`;}).join('');
 const petSelect=runtime.$('petProvider');for(const option of petSelect.options){const available=option.value==='mock'||rows.some(m=>m.provider===option.value&&runtime.modelAvailable(m))||providers[option.value]?.configured;option.disabled=!available;option.textContent=`${runtime.providerNames[option.value]||option.value} · ${available?(option.value==='mock'?'本地模拟':'可用'):'未配置'}`;}
}
function seatPreset(s,id){return Array.isArray(s.seat_presets)?s.seat_presets.find(p=>p.id===id):s.seat_presets?.[String(id)];}
function renderConfig(s){
 const rows=runtime.registry(s),key=JSON.stringify({players:s.players.map(p=>[p.id,p.is_human,p.name]),presets:s.seat_presets,models:rows.map(m=>[m.key,m.configured,m.enabled,m.healthy]),unique:s.unique_model_per_ai_seat});
 if(key===store.configKey)return;store.configKey=key;runtime.$('uniqueModels').checked=s.unique_model_per_ai_seat!==false;
 runtime.$('seatConfigs').innerHTML=Array.from({length:6},(_,i)=>{const id=i+1,human=s.players.find(p=>p.id===id&&p.is_human),p=runtime.seatPreset(s,id)||s.players.find(p=>p.id===id)||{};
 if(human)return `<div class="seat-config human-config"><strong>${id}号</strong><span>真人 · ${runtime.esc(human.name)}</span></div>`;
 const selected=p.model_locked===false?'auto':(p.model_key||(p.provider==='mock'?'mock:mock':'auto'));
 let models=rows.slice();if(!models.some(m=>m.provider==='mock'))models.push({key:'mock:mock',provider:'mock',model:'mock',configured:true,enabled:true,healthy:true});
 if(selected!=='auto'&&!models.some(m=>m.key===selected))models.push({key:selected,provider:p.provider,model:p.model||selected.split(':').slice(1).join(':'),configured:false});
 return `<div class="seat-config" data-seat="${id}"><strong>${id}号 AI</strong><label>模型<select aria-label="${id}号模型" class="bot-model"><option value="auto" ${selected==='auto'?'selected':''}>自动分配 · 独立真实模型</option>${models.map(m=>`<option value="${runtime.esc(m.key)}" data-provider="${runtime.esc(m.provider)}" ${selected===m.key?'selected':''} ${!runtime.modelAvailable(m)&&selected!==m.key?'disabled':''}>${runtime.esc(runtime.providerNames[m.provider]||m.provider)} / ${runtime.esc(m.model)} · ${runtime.modelHealth(m)}</option>`).join('')}</select></label><label>性格<select aria-label="${id}号性格" class="bot-personality">${runtime.options(p.personality||Object.keys(runtime.personalities)[i])}</select></label><span class="model-lock muted">${selected==='auto'?'自动分配':'锁定模型'}</span></div>`;}).join('');
 for(const select of runtime.$('seatConfigs').querySelectorAll('.bot-model'))select.onchange=()=>{select.closest('.seat-config').querySelector('.model-lock').textContent=select.value==='auto'?'自动分配':'锁定模型';runtime.renderModelWarning();};runtime.renderModelWarning();
}
function renderModelWarning(){
 const unique=runtime.$('uniqueModels').checked,assignments=[...runtime.$('seatConfigs').querySelectorAll('[data-seat]')].map(el=>({id:el.dataset.seat,key:el.querySelector('.bot-model').value}));
 const groups=new Map();for(const s of assignments){if(s.key==='auto'||s.key.startsWith('mock:'))continue;if(!groups.has(s.key))groups.set(s.key,[]);groups.get(s.key).push(s.id);}const shared=[...groups].filter(([,ids])=>ids.length>1).map(([key,ids])=>ids.join('、')+'号共用 '+key);
 runtime.$('modelWarning').textContent=unique?'严格模式：真实模型不能共用；数量不足时不能开始。Mock 是本地模拟。':`兼容模式已选择：${shared.length?shared.join('；'):'自动分配允许共用实际模型'}；每个座位仍拥有独立身份、记忆与 Agent。`;
 runtime.$('modelWarning').classList.toggle('shared-warning',!unique);
}
async function loadRooms(){try{const rooms=await runtime.roomAPI.list();runtime.$('recentRooms').innerHTML=rooms.length?'<span class="muted">你的房间：</span>'+rooms.filter(r=>r.lifecycle!=='DELETED').slice(-8).reverse().map(r=>`<button class="btn subtle" data-room="${r.room_id}">${runtime.esc(r.title)} · ${r.room_id}</button>`).join(''):'';}catch(e){runtime.notice(e.message);}}
Object.assign(runtime, {options,registry,modelAvailable,modelHealth,renderProviders,seatPreset,renderConfig,renderModelWarning,loadRooms});
