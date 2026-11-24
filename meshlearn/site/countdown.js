(function(){var el=document.getElementById('sendin');if(!el)return;
var left=parseInt(el.getAttribute('data-in'),10)||0,t0=new Date().getTime();
function tick(){var s=Math.max(0,Math.round(left-(new Date().getTime()-t0)/1000));
if(s<=0){el.textContent=el.getAttribute('data-now');return;}
var m=Math.floor(s/60),r=s%60;el.textContent=el.getAttribute('data-words').replace('{time}',m+':'+(r<10?'0':'')+r);
setTimeout(tick,1000);}tick();})();
