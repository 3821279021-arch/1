import {store, runtime} from './store.js';
async function ensureSession(){if(store.token)return store.token;if(!store.sessionPromise)store.sessionPromise=runtime.api('/api/session','POST').then(s=>{store.token=s.token;if(s.recovery_code)store.publish("recovery_code",s.recovery_code);localStorage.setItem('werewolf-v2-token',store.token);return store.token;}).finally(()=>{store.sessionPromise=null;});return store.sessionPromise;}
async function recoverSession(){store.token=null;localStorage.removeItem('werewolf-v2-token');return runtime.ensureSession();}
async function api(path,method='GET',body,allowRenew=true){
 if(path!=='/api/session'&&!store.token)await runtime.ensureSession();
 const response=await fetch(path,{method,headers:{'Content-Type':'application/json',...(store.token?{'Authorization':'Bearer '+store.token}:{})},...(body===undefined?{}:{body:JSON.stringify(body)})});
 const data=await response.json().catch(()=>({detail:'网络暂时不可用'}));
 if(response.status===401&&path!=='/api/session'){if(method==='GET'&&path==='/api/rooms'&&allowRenew){await runtime.recoverSession();return runtime.api(path,method,body,false);}store.token=null;localStorage.removeItem('werewolf-v2-token');store.publish("session_expired");const expired=new Error('会话已过期，请重新加入或创建房间');expired.status=401;throw expired;}
 if(!response.ok){const error=new Error(typeof data.detail==='string'?data.detail:(data.detail?.message||'输入格式不正确，请检查后重试'));error.status=response.status;throw error;}
 return data;
}
Object.assign(runtime, {ensureSession,recoverSession,api});

// Controllers call domain operations; request paths and methods live here.
runtime.roomAPI={
 create:body=>api('/api/rooms','POST',body),
 join:(id,body)=>api(`/api/rooms/${id}/join`,'POST',body),
 get:id=>api(`/api/rooms/${id}`),
 list:()=>api('/api/rooms'),
 command:(id,kind,method,body)=>api(`/api/rooms/${id}/${kind}`,method,body),
 memory:()=>api('/api/pet/memory'),
 savePreferences:body=>api('/api/pet/memory','PUT',body),
 costReport:id=>api(`/api/rooms/${id}/cost-report`)
};
runtime.sessionAPI={
 recover:code=>api('/api/session/recover','POST',{code}),
 newRecoveryCode:()=>api('/api/session/recovery-code','POST'),
 rotate:()=>api('/api/session/rotate','POST'),
 revoke:()=>api('/api/session/revoke','POST')
};

const credentialScope=()=>store.roomId?`?scope_id=${encodeURIComponent(store.roomId)}`:'';
runtime.credentialsAPI={
 list:()=>api('/api/credentials'+credentialScope()),
 create:body=>api('/api/credentials','POST',body),
 update:(id,body)=>api(`/api/credentials/${encodeURIComponent(id)}`+credentialScope(),'PUT',body),
 test:(id,body={})=>api(`/api/credentials/${encodeURIComponent(id)}/test`+credentialScope(),'POST',body),
 models:id=>api(`/api/credentials/${encodeURIComponent(id)}/models`+credentialScope(),'POST',{}),
 remove:id=>api(`/api/credentials/${encodeURIComponent(id)}`+credentialScope(),'DELETE')
};
runtime.roomAPI.analysis=(id,gameId)=>api(`/api/rooms/${id}/analysis${gameId?'?game_id='+encodeURIComponent(gameId):''}`);
runtime.roomAPI.games=id=>api(`/api/rooms/${id}/games`);
