import RFB from './novnc/core/rfb.js';
const token=location.hash.slice(1);
const status=document.querySelector('#status');
let rfb, heartbeat;
async function post(path){
  const response=await fetch(path,{method:'POST',headers:{Authorization:`Bearer ${token}`}});
  if(!response.ok)throw new Error(`Verbinding geweigerd (${response.status})`);
  return response.json();
}
try{
  if(!token)throw new Error('Open de tijdelijke link die VM Bridge heeft aangemaakt.');
  const config=await post('/config');
  document.querySelector('#name').textContent=config.name;
  document.querySelector('#mode').textContent=config.control?'Handmatige bediening':'Alleen meekijken · assistent kan bedienen';
  rfb=new RFB(document.querySelector('#screen'),`ws://${location.host}/console`,{
    shared:true,credentials:config.password?{password:config.password}:{},
    wsProtocols:['binary',`vmbridge.${token}`]
  });
  rfb.viewOnly=!config.control;
  rfb.scaleViewport=true;
  rfb.resizeSession=false;
  rfb.qualityLevel=9;
  rfb.compressionLevel=2;
  rfb.addEventListener('connect',()=>{status.textContent='Live verbonden · gastresolutie blijft behouden';});
  rfb.addEventListener('disconnect',()=>{status.textContent='Verbinding gesloten';clearInterval(heartbeat);});
  rfb.addEventListener('credentialsrequired',()=>{document.querySelector('#credentials').hidden=false;document.querySelector('#password').focus();});
  rfb.addEventListener('securityfailure',()=>{status.textContent='Consoleaanmelding geweigerd';});
  document.querySelector('#credentials').addEventListener('submit',event=>{
    event.preventDefault();rfb.sendCredentials({password:document.querySelector('#password').value});
    document.querySelector('#password').value='';document.querySelector('#credentials').hidden=true;
  });
  document.querySelector('#native').addEventListener('change',event=>{rfb.scaleViewport=!event.target.checked;});
  document.querySelector('#disconnect').addEventListener('click',async()=>{rfb.disconnect();clearInterval(heartbeat);await post('/close');});
  heartbeat=setInterval(()=>post('/heartbeat').catch(()=>{rfb.disconnect();clearInterval(heartbeat);}),15000);
  window.addEventListener('pagehide',()=>{
    clearInterval(heartbeat);rfb.disconnect();
    fetch('/close',{method:'POST',headers:{Authorization:`Bearer ${token}`},keepalive:true}).catch(()=>{});
  });
}catch(error){status.textContent=error.message;}
