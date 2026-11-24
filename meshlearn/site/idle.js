(function(){var f=document.getElementById('idleout');if(!f)return;var ms=parseInt(f.getAttribute('data-min'),10)*60000,t0=new Date().getTime();
function touch(){t0=new Date().getTime();}
var es=['touchstart','mousedown','keydown','scroll','input'];for(var i=0;i<es.length;i++)document.addEventListener(es[i],touch,true);
setInterval(function(){if(new Date().getTime()-t0>=ms)f.submit();},5000);})();
