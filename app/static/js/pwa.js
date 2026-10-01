import {store, runtime} from './store.js';
async function releaseWakeLock(){if(store.wakeLock){const held=store.wakeLock;store.wakeLock=null;await held.release().catch(()=>{});}}
async function updateWakeLock(){const active=store.state&&store.state.phase!=='lobby'&&!store.state.game_over&&!['ARCHIVED','DELETED'].includes(store.state.lifecycle),wanted=active&&!document.hidden&&runtime.$('wakeToggle').checked;if(!wanted){await runtime.releaseWakeLock();return;}if(!navigator.wakeLock||store.wakeLock||store.wakeRequest)return;store.wakeRequest=navigator.wakeLock.request('screen').then(lock=>{store.wakeLock=lock;lock.addEventListener('release',()=>{if(store.wakeLock===lock)store.wakeLock=null;});if(document.hidden||!runtime.$('wakeToggle').checked||!store.state||store.state.game_over)return runtime.releaseWakeLock();}).catch(()=>{}).finally(()=>{store.wakeRequest=null;});await store.wakeRequest;}
Object.assign(runtime, {releaseWakeLock,updateWakeLock});
runtime.initPwa=()=>{
runtime.$('wakeToggle').onchange=runtime.updateWakeLock;
if(!navigator.wakeLock)runtime.$('wakeToggle').parentElement.title='此浏览器不支持保持亮屏';
window.addEventListener('beforeinstallprompt',e=>{e.preventDefault();store.installPrompt=e;if(!sessionStorage.getItem('werewolf-install-dismissed'))runtime.$('installBtn').classList.remove('hidden');});
runtime.$('installBtn').onclick=async()=>{if(store.installPrompt){await store.installPrompt.prompt();await store.installPrompt.userChoice;store.installPrompt=null;}else runtime.notice('在 Safari 点击「分享」，再选择「添加到主屏幕」');sessionStorage.setItem('werewolf-install-dismissed','1');runtime.$('installBtn').classList.add('hidden');};
window.addEventListener('appinstalled',()=>{runtime.$('installBtn').classList.add('hidden');store.installPrompt=null;});
if(/iPad|iPhone|iPod/.test(navigator.userAgent)&&!navigator.standalone&&location.protocol==='https:'&&!sessionStorage.getItem('werewolf-install-dismissed'))runtime.$('installBtn').classList.remove('hidden');
if('serviceWorker'in navigator)navigator.serviceWorker.register('/sw.js').catch(()=>{});
};
