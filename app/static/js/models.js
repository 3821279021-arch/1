import {store,runtime} from './store.js';
store.credentials=[];
function modelRows(models){return (models||[]).map(m=>runtime.esc(m.display_name||m.id||m.model_id||m.model)).join(' · ');}
function renderCredentials(){
 const rows=store.credentials;runtime.$('credentialList').innerHTML=rows.length?rows.map(c=>`<article class="credential-card" data-credential="${runtime.esc(c.id)}"><div class="credential-heading"><strong>${runtime.esc(runtime.providerNames[c.provider]||c.provider)} ${c.temporary?'<small>· 当前房间</small>':''}</strong><span>${c.status==='connected'||c.last_verified_at?'已连接':'待验证'} · ${c.models?.length||c.model_count||0} 个模型</span></div><p class="masked-key">${runtime.esc(c.masked_label||'••••••')}</p><p class="credential-url">${runtime.esc(c.base_url||'官方 API')}</p><div class="credential-buttons"><button class="btn subtle" data-credential-action="test">测试连接</button><button class="btn subtle" data-credential-action="models">刷新模型</button><button class="btn subtle" data-credential-action="replace">更换密钥</button><button class="btn subtle" data-credential-action="delete">删除</button></div><details><summary>查看账号可用模型</summary><p class="credential-models">${modelRows(c.models)||'目录尚未获取；可在 AI 座位中手动输入 model ID。'}</p></details><p class="credential-error" data-result></p></article>`).join(''):'<p class="muted">还没有连接自己的 API。你也可以直接使用平台模型或本地 Mock 开始一局。</p>';
}
async function loadCredentials(){await runtime.ensureSession();const token=store.token;const response=await runtime.credentialsAPI.list();if(token!==store.token)return [];store.credentials=response.credentials||[];runtime.renderCredentials();runtime.renderModelCatalog?.();if(store.state?.phase==='lobby'&&store.state.is_host){runtime.renderConfig(store.state);}return store.credentials;}
async function openModels(){runtime.openDrawer('modelDrawer');runtime.$('temporaryOption').classList.toggle('hidden',!store.roomId);try{await runtime.loadCredentials();await runtime.loadModelPreferences();}catch(e){runtime.$('credentialList').textContent=e.message;}}
function resetCredentialForm(){store.editCredentialId=null;runtime.$('credentialForm').reset();runtime.$('credentialProvider').disabled=false;runtime.$('credentialTemporary').disabled=false;runtime.$('credentialFormTitle').textContent='添加 API 连接';runtime.$('credentialSave').textContent='保存并测试连接';runtime.$('credentialCancel').classList.add('hidden');runtime.$('credentialStatus').textContent='';}
async function credentialAction(button){
 const id=button.closest('[data-credential]').dataset.credential,kind=button.dataset.credentialAction,c=store.credentials.find(c=>c.id===id);
 if(kind==='replace'){store.editCredentialId=id;runtime.$('credentialProvider').value=c.provider;runtime.$('credentialProvider').disabled=true;runtime.$('credentialBase').value=c.base_url||'';runtime.$('credentialKey').value='';runtime.$('credentialTemporary').checked=!!c.temporary;runtime.$('credentialTemporary').disabled=true;runtime.$('credentialFormTitle').textContent='更换 API 密钥';runtime.$('credentialSave').textContent='保存新密钥并测试';runtime.$('credentialCancel').classList.remove('hidden');runtime.$('credentialKey').focus();return;}
 await runtime.busy(button,async()=>{
  if(kind==='delete'){await runtime.credentialsAPI.remove(id);await runtime.loadCredentials();runtime.notice('API 连接已删除');return;}
  const result=kind==='models'?await runtime.credentialsAPI.models(id):await runtime.credentialsAPI.test(id);
  if(result.credential){const index=store.credentials.findIndex(c=>c.id===id);if(index>=0)store.credentials[index]={...store.credentials[index],...result.credential,models:result.models||result.credential.models||[]};}
  else if(result.models)c.models=result.models;
  runtime.renderCredentials();store.configKey='';if(store.state?.phase==='lobby'&&store.state.is_host)runtime.renderConfig(store.state);
  const card=[...runtime.$('credentialList').querySelectorAll('[data-credential]')].find(el=>el.dataset.credential===id);
  if(card)card.querySelector('[data-result]').textContent=result.error||(!result.verified?'模型目录未获取，可手动输入 model ID。':'连接已验证，模型目录已更新。');
 });
}
function initModelCenter(){
 runtime.$('modelsOpen').onclick=runtime.openModels;runtime.$('assignmentModels').onclick=runtime.openModels;
 runtime.$('credentialCancel').onclick=runtime.resetCredentialForm;
 runtime.$('credentialList').onclick=e=>{const button=e.target.closest('[data-credential-action]');if(button)runtime.credentialAction(button);};
 runtime.$('credentialForm').onsubmit=e=>{e.preventDefault();runtime.busy(runtime.$('credentialSave'),async()=>{
  const secret=runtime.$('credentialKey').value.trim(),base=runtime.$('credentialBase').value.trim(),provider=runtime.$('credentialProvider').value;
  if(!secret)throw new Error('请输入 API Key');if(provider==='openai-compatible'&&!base)throw new Error('兼容服务需要填写 Base URL');
  runtime.$('credentialStatus').textContent='正在安全保存并验证连接…';
  let saved;
  try{const result=store.editCredentialId?await runtime.credentialsAPI.update(store.editCredentialId,{api_key:secret,base_url:base||undefined}):await runtime.credentialsAPI.create({provider,api_key:secret,base_url:base||undefined,temporary:runtime.$('credentialTemporary').checked&&!!store.roomId,...(runtime.$('credentialTemporary').checked&&store.roomId?{scope_id:store.roomId}:{})});saved=result.credential||result;}
  finally{runtime.$('credentialKey').value='';}
  runtime.resetCredentialForm();runtime.$('credentialStatus').textContent='API 已保存，正在发现可用模型…';
  try{const checked=await runtime.credentialsAPI.test(saved.id);await runtime.loadCredentials();runtime.$('credentialStatus').textContent=checked.error?`API 已保存。${checked.error} 可在座位分配中手动输入 model ID。`:checked.verified?'已连接，账号可用模型已加载。':'API 已保存；服务商未返回目录，可手动输入 model ID。';}
  catch(error){await runtime.loadCredentials();runtime.$('credentialStatus').textContent=`API 已保存，验证未完成：${error.message}。可重试或手动输入 model ID。`;}
 });};
}
Object.assign(runtime,{renderCredentials,loadCredentials,openModels,resetCredentialForm,credentialAction,initModelCenter});
