(function(){var box=document.getElementById('scratch'),open=document.getElementById('scratchopen');if(!box||!open)return;
var cv=box.querySelector('canvas'),pad=null,f=document.getElementById('aform'),name='';
function which(){var ws=f.querySelectorAll('.qwrap');for(var i=0;i<ws.length;i++)if(ws[i].style.display!=='none')return 'meshlearn-scratch:'+f.getAttribute('data-key')+'#'+i;return 'meshlearn-scratch:'+f.getAttribute('data-key');}
function keep(){if(!pad)return;try{if(pad.isEmpty())sessionStorage.removeItem(name);else sessionStorage.setItem(name,JSON.stringify(pad.toData()));}catch(e){}}
function size(){var r=Math.max(window.devicePixelRatio||1,1),d=pad?pad.toData():null;cv.width=cv.offsetWidth*r;cv.height=cv.offsetHeight*r;cv.getContext('2d').scale(r,r);if(pad){pad.clear();if(d)pad.fromData(d);}}
function show(){box.style.display='';name=which();size();pad.clear();try{var d=JSON.parse(sessionStorage.getItem(name)||'null');if(d)pad.fromData(d);}catch(e){}}
function start(){pad=new SignaturePad(cv,{minWidth:1,maxWidth:2.6,penColor:'#1a1a1a'});pad.addEventListener('endStroke',keep);window.addEventListener('resize',function(){if(box.style.display!=='none')size();});show();}
open.onclick=function(){if(box.style.display!=='none'){keep();box.style.display='none';return;}
if(pad){show();return;}box.style.display='';var s=document.createElement('script');s.src='/site/vendor/signature_pad/signature_pad.umd.min.js';s.onload=start;s.onerror=function(){box.style.display='none';open.style.display='none';};document.body.appendChild(s);};
box.querySelector('[data-undo]').onclick=function(){if(!pad)return;var d=pad.toData();d.pop();pad.fromData(d);keep();};
box.querySelector('[data-clear]').onclick=function(){if(!pad)return;pad.clear();keep();};
box.querySelector('[data-shut]').onclick=function(){keep();box.style.display='none';};
window.meshScratchMoved=function(){if(box.style.display!=='none'&&pad){keep();show();}};
})();
