const CACHE='ai-werewolf-shell-v3.1';
const SHELL=['/','/static/app.css','/static/app.js','/static/js/socket.js','/static/js/ui.js','/static/js/events.js','/static/js/lobby.js','/static/js/audio.js','/static/js/game.js','/static/js/store.js','/static/js/main.js','/static/js/pet.js','/static/js/api.js','/static/js/pwa.js','/static/js/models.js','/static/js/analysis.js','/static/js/setup.js','/static/assets/audio/test.wav','/static/assets/audio/turn.wav','/static/assets/audio/start.wav','/static/assets/audio/end.wav','/static/assets/audio/vote.wav','/static/assets/audio/night.wav','/static/assets/audio/day.wav','/static/assets/audio/gameover.wav','/manifest.webmanifest','/static/icon-192.png','/static/icon-512.png'];
self.addEventListener('install',event=>event.waitUntil(caches.open(CACHE).then(cache=>cache.addAll(SHELL)).then(()=>self.skipWaiting())));
self.addEventListener('activate',event=>event.waitUntil(caches.keys().then(keys=>Promise.all(keys.filter(key=>key.startsWith('ai-werewolf-shell-')&&key!==CACHE).map(key=>caches.delete(key)))).then(()=>self.clients.claim())));
self.addEventListener('fetch',event=>{
 const url=new URL(event.request.url);
 if(url.origin!==location.origin||url.pathname.startsWith('/api/')||url.pathname.startsWith('/ws/')||event.request.method!=='GET')return;
 event.respondWith(fetch(event.request).then(response=>{if(response.ok){const copy=response.clone();caches.open(CACHE).then(cache=>cache.put(event.request,copy));}return response;}).catch(async()=>await caches.match(event.request)||(event.request.mode==='navigate'?await caches.match('/'):Response.error())));
});
