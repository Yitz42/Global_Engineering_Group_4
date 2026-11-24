(function(){var lang=document.documentElement.lang||'en',base=lang.split('-')[0].toLowerCase();
var app=window.MeshSpeak,synth=window.speechSynthesis,bs=document.querySelectorAll('[data-listen]'),audio=null,run=0,timer=null;if(!bs.length)return;
function can(){if(app){try{return app.can(lang);}catch(e){return false;}}
if(!synth||!window.SpeechSynthesisUtterance)return false;var v=synth.getVoices();
for(var i=0;i<v.length;i++)if(v[i].lang.replace('_','-').split('-')[0].toLowerCase()===base)return true;return false;}
function question(){var f=document.querySelector('.stepping');if(!f)return null;var ws=f.querySelectorAll('.qwrap');
for(var i=0;i<ws.length;i++)if(ws[i].style.display!=='none')return ws[i];return null;}
// What to say, piece by piece: t the words, v the teacher's recording of them when the phone has it.
function pieces(){var q=question(),r=document.getElementById('review'),out=[];
if(q){var d=q.querySelector('.qinfo');if(d&&d.open)out.push({t:d.querySelector('.txt').textContent});
out.push({t:q.getAttribute('data-prompt'),v:q.getAttribute('data-voice')});var o=q.querySelectorAll('.otx');
for(var i=0;i<o.length;i++)out.push({t:o[i].getAttribute('data-raw')||o[i].textContent,v:o[i].getAttribute('data-voice')});return out;}
if(document.querySelector('.stepping')&&r)return [{t:r.innerText}];
return [{t:document.getElementById('main').innerText}];}
function show(){var q=question(),yes=can()||(q&&(q.hasAttribute('data-voice')||q.querySelector('[data-voice]')));for(var i=0;i<bs.length;i++)bs[i].style.display=yes?'':'none';}
function plain(t){return t.replace(/\\[()]|\$\$/g,' ').replace(/\\frac\{([^{}]*)\}\{([^{}]*)\}/g,'$1 / $2').replace(/\\(times|cdot)/g,' × ').replace(/\\[a-z]+|[{}]/g,' ').replace(/\s+/g,' ').slice(0,3000);}
function stop(){run++;if(timer)clearTimeout(timer);if(audio){audio.pause();audio=null;}if(app)app.stop();else if(synth)synth.cancel();}
function speak(t,next){t=plain(t);if(app){app.speak(t,lang);timer=setTimeout(next,Math.min(60000,900+t.length*75));return;}   // Android says nothing when it has finished
var u=new SpeechSynthesisUtterance(t);u.lang=lang;u.rate=.9;u.onend=next;synth.speak(u);}
function play(list,voice){var me=++run;(function next(i){if(me!==run||i>=list.length)return;var p=list[i],go=function(){next(i+1);};
if(p.v){audio=new Audio(p.v);audio.onended=go;audio.onerror=function(){if(voice)speak(p.t,go);else go();};var a=audio.play();if(a&&a['catch'])a['catch'](function(){});return;}
if(voice)speak(p.t,go);else go();})(0);}
function say(){stop();var list=pieces(),recorded=false,t='';for(var i=0;i<list.length;i++){if(list[i].v)recorded=true;t+=(t?'. ':'')+list[i].t;}
play(recorded?list:[{t:t}],can());}
for(var i=0;i<bs.length;i++)bs[i].addEventListener('click',say);
show();if(synth&&'onvoiceschanged' in synth)synth.onvoiceschanged=show;if(app)setTimeout(show,1500);
window.meshListenMoved=function(){stop();show();};
window.addEventListener('pagehide',stop);
})();
