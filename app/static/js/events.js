import {store, runtime} from './store.js';
function eventIdentity(e,channel=''){return e.event_id||e.id||(e.seq!==undefined?`${channel}:${e.seq}`:null);}
function rememberEvent(packet){const id=packet.event_id||packet.data?.event_id,seq=packet.seq;if((id&&store.seenEvents.has(id))||(!id&&seq!==undefined&&store.seenSequence.has(seq)))return false;if(id)store.seenEvents.add(id);if(seq!==undefined)store.seenSequence.add(seq);if(store.seenEvents.size>5000)store.seenEvents.delete(store.seenEvents.values().next().value);if(store.seenSequence.size>5000)store.seenSequence.delete(store.seenSequence.values().next().value);return true;}
function markHistory(s){for(const e of [...(s.events||[]),...(s.wolf_chat||[]),...(s.pet?.private_chat_history||[])])if(e.event_id)store.seenEvents.add(e.event_id);}
function appendUnique(list,entry,channel){const id=runtime.eventIdentity(entry,channel);const index=list.findIndex(e=>(id&&runtime.eventIdentity(e,channel)===id)||(entry.client_message_id&&e.client_message_id===entry.client_message_id&&e.role===entry.role));if(index>=0){list[index]={...list[index],...entry};return false;}list.push(entry);return true;}
function packetCurrent(packet){if(!store.state)return false;const gid=packet.game_id||packet.data?.game_id;if(gid&&gid!==store.state.game_id)return false;const r=packet.state_revision??packet.data?.state_revision;if(r!==undefined&&r<runtime.rev(store.state))return false;return true;}
function handlePacket(packet){
 if(packet.event_id)store.lastEventId=packet.event_id;
 if(packet.type==='state_snapshot'&&packet.data?.event_seq)store.lastEventId=`${packet.data.game_id}:${packet.data.event_seq}`;
 if(packet.type==='timer_sync'){store.clockOffset=packet.data.server_time*1000-Date.now();return;}
 if(packet.type==='room_left'){runtime.returnHome();runtime.notice('已离开房间');return;}
 if(packet.type==='reconnect'){runtime.clearAudio();store.syncing=true;runtime.status('状态同步中…');if(packet.data?.room_id)runtime.render(packet.data);return;}
 if(packet.type==='state_snapshot'){const s=packet.data;if(!s||s.room_id!==store.roomId)return;if(runtime.render(s)!==false){store.syncing=false;store.connectionAttempt=0;runtime.status('● 实时连接',true);runtime.restorePendingAction();}return;}
 if(!runtime.packetCurrent(packet)||!runtime.rememberEvent(packet))return;
 const d=packet.data||{},r=packet.state_revision??d.state_revision;if(r!==undefined)store.state.state_revision=Math.max(runtime.rev(store.state),r);
 switch(packet.type){
  case 'speech_started':
   store.state.current_speech=d.content||'';store.state.current_turn_player_id=d.player_id;
   runtime.paceStart(packet.turn_id||d.turn_id||store.state.turn_id,d.player_id,store.state.current_speech);runtime.$('streamStatus').classList.add('hidden');runtime.playCue?.('start');runtime.renderPlayers(store.state);runtime.followStage();break;
  case 'speech_chunk':{
   const pid=d.player_id,turn=packet.turn_id||d.turn_id;if(turn&&store.state.turn_id&&turn!==store.state.turn_id)return;
   if(pid&&store.state.current_turn_player_id&&pid!==store.state.current_turn_player_id)return;
   const delta=d.delta??d.chunk??'';store.state.current_speech=(store.state.current_speech||'')+delta;runtime.paceDelta(turn||store.state.turn_id,pid,delta);
   runtime.followStage();break;
  }
  case 'speech_finished':{
   const content=d.content??d.speech??store.state.current_speech??'',pid=d.player_id;
   if(pid===store.state.current_turn_player_id){const prior=store.state.current_speech||'';store.state.current_speech=content||prior;runtime.paceFinish(packet.turn_id||d.turn_id||store.state.turn_id,pid,store.state.current_speech);}
   runtime.playCue?.('end');runtime.followStage();if(d.error||d.partial){runtime.$('streamStatus').textContent='这段发言暂时中断，已输出文字保留。';runtime.$('streamStatus').classList.remove('hidden');}break;
  }
  case 'chat_message':{
   const e={...d,event_id:d.event_id||packet.event_id};runtime.appendUnique(store.state.events,e,'public');if(d.kind==='skill')runtime.playCue?.('skill',packet.event_id);if(d.kind==='death')runtime.playCue?.('death',packet.event_id);runtime.renderHistory(store.state);break;
  }
  case 'private_pet_message':{
   if(d.configured){if(d.pet){store.state.pet={...store.state.pet,...d.pet};runtime.renderPet(store.state.pet);}break;}
   const e={...d,event_id:d.event_id||packet.event_id};if(!e.text)return;runtime.appendUnique(store.state.pet.private_chat_history,e,'pet');runtime.reconcileOptimistic(store.state.pet);
   if(e.role==='assistant'){store.petBusy=false;store.petRequestId=null;runtime.setPetPending();if(runtime.$('petAudioToggle').checked&&store.ttsEnabled)runtime.speakPet(e.text);}
   runtime.renderPet(store.state.pet);break;
  }
  case 'wolf_chat_message':
   if(store.state.self.alive&&Array.isArray(store.state.wolf_chat)){runtime.appendUnique(store.state.wolf_chat,{...d,event_id:d.event_id||packet.event_id},'wolf');runtime.renderWolf(store.state);}break;
  case 'phase_changed':
   if(d.phase){store.state.phase=d.phase;store.state.phase_name=d.phase_name||runtime.phaseNames[d.phase]||d.phase;runtime.$('phaseText').textContent=store.state.phase_name;document.body.classList.toggle('night',d.phase.startsWith('night'));}
   if(d.turn_id)store.state.turn_id=d.turn_id;if(packet.turn_id)store.state.turn_id=packet.turn_id;
   if('current_turn_player_id'in d)store.state.current_turn_player_id=d.current_turn_player_id;store.state.current_speech='';runtime.$('streamStatus').classList.add('hidden');document.body.dataset.phase=store.state.phase;runtime.observeAudioState?.(store.state);break;
  case 'vote_result':runtime.playHost?.('vote_end',`${store.state.game_id}:${packet.turn_id}`);runtime.playCue?.('vote',packet.event_id);break;
  case 'speech_error':runtime.$('streamStatus').textContent=d.message||'这段发言暂时中断，已输出文字保留。';runtime.$('streamStatus').classList.remove('hidden');break;
  case 'player_died':case 'death':runtime.playCue?.('death',packet.event_id);break;
  case 'model_status':case 'ai_execution':case 'model_execution':
   if(d.player_id){const p=store.state.players.find(p=>p.id===d.player_id);if(p)p.execution_status=d.execution_status||d;runtime.renderPlayers(store.state);runtime.renderExecutions(store.state);}break;
  case 'error':runtime.notice(d.message||d.detail||'操作未完成，请重试');break;
 }
}
Object.assign(runtime, {eventIdentity,rememberEvent,markHistory,appendUnique,packetCurrent,handlePacket});
