import {openRoomSocket} from './socket.js';
import './ui.js';
import './api.js';
import './events.js';
import './lobby.js';
import './game.js';
import './pet.js';
import './audio.js';
import './pacing.js';
import './pwa.js';
import './models.js';
import './model-preferences.js';
import './analysis.js';
import './setup.js';
import './shell.js';
import {store, runtime} from './store.js';
function closeSocket(){clearTimeout(store.reconnect);clearInterval(store.heartbeat);if(store.ws){store.ws.onclose=null;store.ws.close();store.ws=null;}}
function connect(){
 runtime.closeSocket();if(!store.roomId)return;runtime.clearAudio();store.syncing=true;runtime.status(navigator.onLine?'正在重连…':'已断开 · 等待网络');
 if(!navigator.onLine)return;
 const capturedRoom=store.roomId;
 store.ws=openRoomSocket({roomId:store.roomId,token:store.token,lastEventId:store.lastEventId,
  onOpen:()=>runtime.status('状态同步中…'),
  onPacket:packet=>{if(capturedRoom===store.roomId)runtime.handlePacket(packet);},
  onError:error=>{runtime.status(navigator.onLine?'正在重连…':'已断开 · 等待网络');if(error instanceof Error){store.syncing=true;runtime.closeSocket();store.reconnect=setTimeout(runtime.connect,1000);}},
  onClose:event=>{
   runtime.clearAudio();if(capturedRoom!==store.roomId)return;store.syncing=true;
   if(event.code===4401){store.token=null;localStorage.removeItem('werewolf-v2-token');runtime.returnHome();runtime.notice('会话已过期，请使用恢复码恢复原座位');return;}
   if(event.code===4403){runtime.status('已断开');runtime.notice('房间不可用，请返回我的房间。');return;}
   runtime.status(navigator.onLine?'正在重连 · 无人时将暂停':'已断开 · 无人时将暂停');
   store.reconnect=setTimeout(runtime.connect,event.code===4408?10000:Math.min(8000,1000*2**Math.min(store.connectionAttempt++,3)));
  }
 });
}

function enterRoom(s){runtime.resetPresentation?.();runtime.closeDrawers();document.body.classList.add('in-room');runtime.hideShell();store.stageFollowing=true;runtime.clearAudio();store.roomId=s.room_id;store.lastEventId=null;store.state=null;store.formKey='';store.configKey='';store.seenEvents.clear();store.seenSequence.clear();store.retiredGameIds.clear();store.optimisticPet=[];store.pendingAction=null;localStorage.setItem('werewolf-v2-room',store.roomId);runtime.$('entry').classList.add('hidden');runtime.$('game').classList.remove('hidden');runtime.render(s);runtime.connect();history.replaceState(null,'',`?room=${store.roomId}`);}
function returnHome(refresh=true){localStorage.removeItem('werewolf-v2-room');runtime.resetPresentation?.();runtime.closeDrawers();document.body.classList.remove('in-room','night');delete document.body.dataset.phase;runtime.stopReplay();runtime.closeSocket();runtime.clearAudio();runtime.releaseWakeLock();store.roomId=null;store.state=null;store.optimisticPet=[];store.petBusy=false;store.pendingAction=null;document.title=runtime.title;runtime.$('game').classList.add('hidden');runtime.showShellPage('home');runtime.closePet();history.replaceState(null,'',location.pathname);if(refresh)runtime.loadRooms();else runtime.$('recentRooms').textContent='';runtime.status('选择房间');}

Object.assign(runtime, {enterRoom,returnHome,closeSocket,connect});
store.subscribe("session_expired",()=>{store.credentials=[];store.modelPreferences={favorites:[],recent:[],lineups:[]};runtime.renderModelCatalog();if(store.roomId)runtime.returnHome();});

store.subscribe('recovery_code',code=>{runtime.$('recoveryDisplay').textContent=code;runtime.$('recoveryNotice').classList.remove('hidden');});
runtime.$('hideRecovery').onclick=()=>{runtime.$('recoveryDisplay').textContent='';runtime.$('recoveryNotice').classList.add('hidden');};
runtime.$('newRecoveryBtn').onclick=()=>runtime.busy(runtime.$('newRecoveryBtn'),async()=>{const result=await runtime.sessionAPI.newRecoveryCode();store.publish('recovery_code',result.recovery_code);});
runtime.$('recoverBtn').onclick=()=>runtime.busy(runtime.$('recoverBtn'),async()=>{
 const code=runtime.$('recoveryInput').value.trim();if(!code)throw new Error('请输入已保存的恢复码');
 const result=await runtime.sessionAPI.recover(code);runtime.closeSocket();store.token=result.token;localStorage.setItem('werewolf-v2-token',store.token);
 runtime.$('recoveryInput').value='';store.publish('recovery_code',result.recovery_code);runtime.returnHome(false);await runtime.loadModelPreferences();await runtime.loadCredentials();await runtime.loadRooms();runtime.notice('身份已恢复，请从我的房间继续；请保存新的恢复码。');
});
runtime.$('logoutBtn').onclick=()=>runtime.busy(runtime.$('logoutBtn'),async()=>{await runtime.sessionAPI.revoke();runtime.closeSocket();store.token=null;localStorage.removeItem('werewolf-v2-token');localStorage.removeItem('werewolf-v2-room');store.credentials=[];store.modelPreferences={favorites:[],recent:[],lineups:[]};runtime.renderModelCatalog();runtime.returnHome(false);runtime.notice('已退出，原会话已撤销；可用恢复码恢复身份。');});
runtime.$('lockRoomBtn').onclick=()=>runtime.busy(runtime.$('lockRoomBtn'),()=>runtime.action('lock','POST',{locked:!store.state.locked}));
runtime.$('savePasswordBtn').onclick=()=>runtime.busy(runtime.$('savePasswordBtn'),async()=>{await runtime.action('password','POST',{password:runtime.$('roomPassword').value});runtime.$('roomPassword').value='';runtime.notice('房间密码已更新');});
runtime.$('moderateSeatBtn').onclick=()=>runtime.busy(runtime.$('moderateSeatBtn'),()=>runtime.action('kick','POST',{seat:Number(runtime.$('moderateSeat').value)}));
runtime.$('reopenSeatBtn').onclick=()=>runtime.busy(runtime.$('reopenSeatBtn'),()=>runtime.action('reopen','POST',{seat:Number(runtime.$('moderateSeat').value)}));
runtime.$('costReportBtn').onclick=()=>runtime.busy(runtime.$('costReportBtn'),async()=>{const report=await runtime.roomAPI.costReport(store.roomId);const blob=new Blob([JSON.stringify(report,null,2)],{type:'application/json'});const link=document.createElement('a');link.href=URL.createObjectURL(blob);link.download=`werewolf-${store.roomId}-cost.json`;link.click();setTimeout(()=>URL.revokeObjectURL(link.href),1000);});

runtime.initAudio();
runtime.initPacing();
runtime.initPwa();
runtime.initV3();
runtime.initModelCenter();
runtime.initModelPreferences();
runtime.initAnalysis();
runtime.initShell();
'use strict';
runtime.$('createBtn').onclick=()=>runtime.busy(runtime.$('createBtn'),async()=>runtime.enterRoom(await runtime.roomAPI.create({name:runtime.$('playerName').value.trim()||'玩家',...runtime.createMode(),pace:runtime.$('paceSelect').value,action_id:runtime.uuid()})));
runtime.$('joinBtn').onclick=()=>runtime.busy(runtime.$('joinBtn'),async()=>{const id=runtime.$('joinCode').value.trim().toLowerCase();if(!/^[a-f0-9]{10}$/.test(id))throw new Error('请输入有效的 10 位房间号');runtime.enterRoom(await runtime.roomAPI.join(id,{name:runtime.$('playerName').value.trim()||'玩家',password:runtime.$('joinPassword').value}));});
runtime.$('recentRooms').onclick=e=>{const button=e.target.closest('[data-room]');if(button)runtime.busy(button,async()=>runtime.enterRoom(await runtime.roomAPI.get(button.dataset.room)));};
runtime.$('startBtn').onclick=()=>runtime.busy(runtime.$('startBtn'),()=>runtime.action('start','POST',{action_id:runtime.uuid(),expected_state_revision:runtime.rev(store.state)}));
runtime.$('uniqueModels').onchange=runtime.renderModelWarning;
runtime.$('saveConfig').onclick=()=>runtime.busy(runtime.$('saveConfig'),async()=>{const seats=[...runtime.$('seatConfigs').querySelectorAll('[data-seat]')].map(el=>{const select=el.querySelector('.bot-model'),key=select.value,option=select.selectedOptions[0],credential_id=option?.dataset.credentialId,model_id=option?.dataset.manual?el.querySelector('.bot-model-id').value.trim():option?.dataset.modelId;if(credential_id&&!model_id)throw new Error(`${el.dataset.seat} 号需要填写完整 model ID`);if(key==='missing')throw new Error(`${el.dataset.seat} 号需要重新分配模型`);return {id:Number(el.dataset.seat),provider:credential_id?undefined:key==='auto'?'auto':option?.dataset.provider,model_key:credential_id?undefined:key,model_locked:key!=='auto',personality:el.querySelector('.bot-personality').value,...(credential_id?{credential_id,model_id}:{})};});await runtime.action('configure','POST',{pace:store.state.pace,seats,unique_model_per_ai_seat:runtime.$('uniqueModels').checked});runtime.notice('AI 模型分配已保存');runtime.$('assignmentDrawer').close();});
runtime.$('copyBtn').onclick=()=>runtime.busy(runtime.$('copyBtn'),async()=>{const invite=`${location.origin}/?room=${store.roomId}`;try{await navigator.clipboard.writeText(invite);runtime.notice('邀请链接已复制，朋友将随机分配空位。');}catch(_){runtime.notice(`房间号：${store.roomId}`);}});
runtime.$('homeBtn').onclick=()=>runtime.busy(runtime.$('homeBtn'),async()=>{if(store.state&&(['ACTIVE','SUSPENDED','FINISHED'].includes(store.state.lifecycle)||!store.state.is_host))await runtime.action('leave');runtime.returnHome();});
runtime.$('resumeBtn').onclick=()=>runtime.busy(runtime.$('resumeBtn'),()=>runtime.action('resume'));
runtime.$('pauseRoomBtn').onclick=()=>runtime.busy(runtime.$('pauseRoomBtn'),()=>runtime.action('pause'));
runtime.$('deleteRoomBtn').onclick=()=>{if(confirm('关闭并永久删除此房间和私有记录？'))runtime.busy(runtime.$('deleteRoomBtn'),async()=>{await runtime.api(`/api/rooms/${store.roomId}`,'DELETE');runtime.returnHome();});};
runtime.$('rematchBtn').onclick=()=>runtime.busy(runtime.$('rematchBtn'),()=>runtime.action('rematch'));
runtime.$('closeRoomBtn').onclick=()=>runtime.busy(runtime.$('closeRoomBtn'),async()=>{await runtime.action('close');runtime.returnHome();});
runtime.$('turnForm').onsubmit=e=>{e.preventDefault();const pending=store.state?.pending_action;if(!pending||store.actionSending||store.pendingAction?.waiting)return;const payload={action:pending.type,game_id:pending.game_id||store.state.game_id,turn_sequence:pending.turn_sequence??store.state.turn_sequence,turn_id:pending.turn_id||store.state.turn_id,expected_state_revision:runtime.rev(store.state),action_id:runtime.uuid()};if(pending.type==='speech')payload.speech=runtime.$('speechInput').value.trim();else if(pending.type==='wolf_discuss')payload.text=runtime.$('wolfDiscussInput').value.trim();else if(pending.type==='witch'){payload.save=runtime.$('saveInput').checked;payload.poison_target=runtime.$('poisonInput').value?Number(runtime.$('poisonInput').value):null;}else payload.target=runtime.$('targetInput').value?Number(runtime.$('targetInput').value):null;store.pendingAction={payload,waiting:true,retry:false};runtime.savePendingAction();runtime.sendPendingAction();};
runtime.$('wolfForm').onsubmit=e=>{e.preventDefault();const text=runtime.$('wolfInput').value.trim();if(!text||!store.state.self.alive)return;runtime.busy(runtime.$('wolfForm').querySelector('button'),async()=>{await runtime.action('wolf-chat','POST',{text});runtime.$('wolfInput').value='';});};
runtime.$('petOpen').onclick=runtime.openPet;
runtime.$('petClose').onclick=runtime.closePet;
runtime.$('petOverlay').onclick=runtime.closePet;
document.addEventListener('keydown',e=>{if(e.key==='Escape')runtime.closePet();if(e.key==='Tab'&&!runtime.$('petDrawer').classList.contains('hidden')){const focusable=[...runtime.$('petDrawer').querySelectorAll('button,input,select,textarea,summary')].filter(el=>!el.disabled&&el.offsetParent!==null),first=focusable[0],last=focusable.at(-1);if(e.shiftKey&&document.activeElement===first){last.focus();e.preventDefault();}else if(!e.shiftKey&&document.activeElement===last){first.focus();e.preventDefault();}}});
runtime.$('petPersonality').onchange=()=>runtime.petConfig({personality:runtime.$('petPersonality').value});
runtime.$('petMode').onchange=()=>runtime.petConfig({control_mode:runtime.$('petMode').value});
runtime.$('delegateBtn').onclick=()=>runtime.busy(runtime.$('delegateBtn'),()=>runtime.petConfig({delegate_next:!store.state.pet.delegate_next}));
runtime.$('takeBackBtn').onclick=()=>runtime.busy(runtime.$('takeBackBtn'),()=>runtime.petConfig({control_mode:'copilot',delegate_next:false}));
runtime.$('savePetPrefs').onclick=()=>runtime.busy(runtime.$('savePetPrefs'),async()=>{await runtime.action('pet','PATCH',{name:runtime.$('petName').value.trim()||'月牙',provider:runtime.$('petProvider').value,play_style:{aggression:Number(runtime.$('petAggression').value),caution:Number(runtime.$('petCaution').value)}});await runtime.roomAPI.savePreferences({advice_length:runtime.$('adviceLength').value});runtime.notice('搭档偏好已保存');});
runtime.$('petForm').onsubmit=e=>{e.preventDefault();runtime.askPet(runtime.$('petInput').value.trim());};
document.querySelectorAll('[data-question]').forEach(btn=>btn.onclick=()=>runtime.askPet(btn.dataset.question));
runtime.$('petPersonality').innerHTML=runtime.options('detective');
setInterval(runtime.updateTimer,200);
window.addEventListener('online',()=>{if(store.roomId)runtime.connect();});
window.addEventListener('offline',()=>{runtime.clearAudio();runtime.closeSocket();store.syncing=true;runtime.status('已断开 · 无人时将暂停');});
document.addEventListener('visibilitychange',()=>{runtime.updateWakeLock();if(document.hidden)runtime.clearAudio();else if(store.roomId&&store.ws?.readyState!==WebSocket.OPEN)runtime.connect();});
(async()=>{try{await runtime.ensureSession();await runtime.loadRooms();await runtime.loadModelPreferences();const invitation=new URLSearchParams(location.search).get('room'),saved=localStorage.getItem('werewolf-v2-room'),id=invitation||saved;if(id&&/^[a-f0-9]{10}$/.test(id)){try{runtime.enterRoom(await runtime.roomAPI.get(id));}catch(_){runtime.$('joinCode').value=id;runtime.status('选择房间');if(invitation)runtime.notice('输入昵称后点击加入，系统将随机分配空座。');}}else runtime.status('选择房间');const mem=await runtime.roomAPI.memory();runtime.$('adviceLength').value=mem.preferences.advice_length||'short';}catch(e){runtime.status('连接失败');runtime.notice(e.message);}})();
