(function(){var lang=document.documentElement.lang||'en',base=lang.split('-')[0].toLowerCase();
var app=window.MeshSpeak,synth=window.speechSynthesis,bs=document.querySelectorAll('[data-listen]');if(!bs.length)return;
function can(){if(app){try{return app.can(lang);}catch(e){return false;}}
if(!synth||!window.SpeechSynthesisUtterance)return false;var v=synth.getVoices();
for(var i=0;i<v.length;i++)if(v[i].lang.replace('_','-').split('-')[0].toLowerCase()===base)return true;return false;}
function show(){if(can())for(var i=0;i<bs.length;i++)bs[i].style.display='';}
function words(){var f=document.querySelector('.stepping'),q=null,t='';
if(f){var ws=f.querySelectorAll('.qwrap');for(var i=0;i<ws.length;i++)if(ws[i].style.display!=='none')q=ws[i];}
if(q){t=q.getAttribute('data-prompt');var o=q.querySelectorAll('.otx');for(i=0;i<o.length;i++)t+='. '+(o[i].getAttribute('data-raw')||o[i].textContent);
var d=q.querySelector('.qinfo');if(d&&d.open)t=d.querySelector('.txt').textContent+'. '+t;return t;}
var r=document.getElementById('review');if(f&&r)return r.innerText;
return document.getElementById('main').innerText;}
function say(){var t=words().replace(/\\[()]|\$\$/g,' ').replace(/\\frac\{([^{}]*)\}\{([^{}]*)\}/g,'$1 / $2').replace(/\\(times|cdot)/g,' × ').replace(/\\[a-z]+|[{}]/g,' ').replace(/\s+/g,' ').slice(0,3000);
if(app){app.speak(t,lang);return;}
synth.cancel();var u=new SpeechSynthesisUtterance(t);u.lang=lang;u.rate=.9;synth.speak(u);}
for(var i=0;i<bs.length;i++)bs[i].addEventListener('click',say);
show();if(synth&&'onvoiceschanged' in synth)synth.onvoiceschanged=show;if(app)setTimeout(show,1500);
window.addEventListener('pagehide',function(){if(app)app.stop();else if(synth)synth.cancel();});
})();
