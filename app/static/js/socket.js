// WebSocket transport emits packets and lifecycle callbacks; it has no UI code.
export function openRoomSocket({roomId, token, lastEventId, onOpen, onPacket, onClose, onError}) {
 const socket = new WebSocket(`${location.protocol==='https:'?'wss:':'ws:'}//${location.host}/ws/${roomId}`);
 let heartbeat;
 socket.onopen=()=>{
  socket.send(JSON.stringify({token,last_event_id:lastEventId||null}));
  heartbeat=setInterval(()=>{if(socket.readyState===WebSocket.OPEN)socket.send('ping');},20000);
  onOpen();
 };
 socket.onmessage=event=>{try{onPacket(JSON.parse(event.data));}catch(error){onError(error);}};
 socket.onerror=onError;
 socket.onclose=event=>{clearInterval(heartbeat);onClose(event);};
 const close=socket.close.bind(socket);
 socket.close=(...args)=>{clearInterval(heartbeat);close(...args);};
 return socket;
}
