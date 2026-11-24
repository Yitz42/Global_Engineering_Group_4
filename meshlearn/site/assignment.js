(function(){var f=document.getElementById('aform');if(!f)return;var key='meshlearn-draft:'+f.getAttribute('data-key');
var store=null;try{store=window.localStorage;}catch(e){}
var ws=f.querySelectorAll('.qwrap'),N=ws.length,step=0;
function id(x){return document.getElementById(x);}
function all(){return f.querySelectorAll('input[type=text],input[type=radio],textarea');}
function draft(){if(!store)return {};try{return JSON.parse(store.getItem(key)||'{}')||{};}catch(e){return {};}}
function save(){if(!store)return;var d={},els=all(),fl=f.querySelectorAll('input[data-flag]');for(var i=0;i<els.length;i++){var el=els[i];
if(el.type==='radio'){if(el.checked)d[el.name]=el.value;}else if(el.value)d[el.name]=el.value;}
var un=f.querySelectorAll('.unsure input');d['#unsure']=[];for(i=0;i<un.length;i++)if(un[i].checked)d['#unsure'].push(un[i].name);
d['#first']=first;d['#step']=step;d['#flags']=[];for(i=0;i<fl.length;i++)if(fl[i].checked)d['#flags'].push(fl[i].getAttribute('data-flag'));
try{store.setItem(key,JSON.stringify(d));}catch(e){}}
function restore(){var d=draft();
var els=all(),fl=f.querySelectorAll('input[data-flag]');for(var i=0;i<els.length;i++){var el=els[i];if(!d.hasOwnProperty(el.name))continue;
if(el.type==='radio'){if(!f.querySelector('input[name="'+el.name+'"]:checked')&&el.value===d[el.name])el.checked=true;}
else if(!el.value)el.value=d[el.name];}
for(i=0;i<fl.length;i++)fl[i].checked=(d['#flags']||[]).indexOf(fl[i].getAttribute('data-flag'))>=0;
var un=f.querySelectorAll('.unsure input');for(i=0;i<un.length;i++)if((d['#unsure']||[]).indexOf(un[i].name)>=0)un[i].checked=true;
if(d['#first']&&typeof d['#first']==='object')first=d['#first'];
step=parseInt(d['#step'],10)||0;}
var first={};
function val(q){var els=q.querySelectorAll('input[type=text],input[type=radio]');for(var j=0;j<els.length;j++){var el=els[j];
if(el.type==='radio'){if(el.checked)return [el.name,el.value];}else return [el.name,el.value.replace(/^\s+|\s+$/g,'')];}return null;}
function left(i){if(i>=N)return;var v=val(ws[i]);if(v&&v[1]&&!first.hasOwnProperty(v[0]))first[v[0]]=v[1].slice(0,40);}
function changed(){var d={},el=id('first');for(var i=0;i<N;i++){var v=val(ws[i]);if(v&&first.hasOwnProperty(v[0])&&first[v[0]]!==v[1])d[v[0]]=first[v[0]];}
if(el)el.value=JSON.stringify(d);}
function raw(o){if(o.getAttribute('data-raw')===null)o.setAttribute('data-raw',o.textContent);return o.getAttribute('data-raw');}
function said(q){var els=q.querySelectorAll('input[type=text],input[type=radio],textarea');for(var j=0;j<els.length;j++){var el=els[j];
if(el.type==='radio'){if(el.checked)return raw(el.parentNode.querySelector('.otx')).replace(/^\s+|\s+$/g,'');}
else return el.value.replace(/^\s+|\s+$/g,'');}return '';}
function update(){var done=0,els=all();for(var j=0;j<els.length;j++){var el=els[j];
if(el.type==='radio')el.parentNode.className=el.checked?'option on':'option';
if(el.tagName==='TEXTAREA'){var c=id(el.id+'-count');if(c)c.textContent=el.value.length+' / '+el.getAttribute('maxlength');}}
for(var i=0;i<N;i++)if(said(ws[i]))done++;var a=id('answered');
if(a)a.textContent=(a.getAttribute('data-words')||'{done} of {total} answered').replace('{done}',done).replace('{total}',N);}
function span(cls,text){var s=document.createElement('span');s.className=cls;s.textContent=text;return s;}
function list(){var L=id('reviewlist');L.innerHTML='';for(var i=0;i<N;i++)(function(i){var w=ws[i],b=document.createElement('button'),g=span('grow',''),t=said(w);
b.type='button';b.className=t?'':'none';b.appendChild(span('num',String(i+1)));g.appendChild(span('',w.getAttribute('data-prompt')));
g.appendChild(span('ans',t||f.getAttribute('data-none')));
if(w.querySelector('.unsure input').checked)g.appendChild(span('fl','? '+f.getAttribute('data-unsure')));
if(w.querySelector('input[data-flag]').checked)g.appendChild(span('fl','⚑ '+f.getAttribute('data-flagged')));
b.appendChild(g);b.appendChild(span('chg',f.getAttribute('data-change')));b.onclick=function(){show(i,true);};L.appendChild(b);})(i);}
function prog(){var p=id('qprog');if(!p.children.length)for(var i=0;i<N;i++)(function(i){var b=document.createElement('button');
b.type='button';b.innerHTML=String(i+1)+f.getAttribute('data-star');b.setAttribute('aria-label',ws[i].querySelector('.qnum').textContent);
b.onclick=function(){show(i,true);};p.appendChild(b);})(i);
for(i=0;i<N;i++){p.children[i].className=i===step?'now':said(ws[i])?'did':'';
if(i===step)p.children[i].setAttribute('aria-current','step');else p.children[i].removeAttribute('aria-current');}}
function show(i,move){if(move)left(step);step=Math.max(0,Math.min(N,i));var end=step===N;prog();
id('nextlabel').textContent=f.getAttribute(step===N-1?'data-review':'data-next');
for(var j=0;j<N;j++)ws[j].style.display=j===step?'':'none';
if(end){list();if(window.meshMath)meshMath(id('reviewlist'));}if(window.meshScratchMoved)meshScratchMoved();if(window.meshListenMoved)meshListenMoved();id('review').style.display=end?'block':'none';id('submit').style.display=end?'':'none';
id('back').disabled=step===0;id('next').style.display=end?'none':'';id('toreview').style.display=end||step===N-1?'none':'';
if(move){save();window.scrollTo(0,0);var t=end?id('review'):ws[step].querySelector('fieldset');
try{t.setAttribute('tabindex','-1');t.focus({preventScroll:true});}catch(e){}}}
function pull(d){var y=null,s=d.querySelector('summary');
s.addEventListener('touchstart',function(e){y=e.touches[0].clientY;});
s.addEventListener('touchmove',function(e){if(y===null)return;var m=e.touches[0].clientY-y;
if(m>24){d.open=true;y=null;}else if(m<-24){d.open=false;y=null;}});}
var ox=f.querySelectorAll('.otx');for(var o=0;o<ox.length;o++)raw(ox[o]);
restore();update();
if(N){f.className='stepping';f.parentNode.className+=' qpage';document.body.className+=' qbody';
var ds=f.querySelectorAll('.qinfo'),mq=window.matchMedia?window.matchMedia('(min-width:900px)'):null;
for(var k=0;k<ds.length;k++)pull(ds[k]);
var side=function(){if(mq&&mq.matches)for(var k=0;k<ds.length;k++)ds[k].open=true;};side();window.addEventListener('resize',side);
id('back').onclick=function(){show(step-1,true);};id('next').onclick=function(){show(step+1,true);};
id('toreview').onclick=function(){show(N,true);};
f.addEventListener('submit',function(e){if(step<N){e.preventDefault();show(step+1,true);}else changed();});
var bad=f.querySelector('[aria-invalid=true]');
if(bad)for(k=0;k<N;k++)if(ws[k].contains(bad)){step=k;break;}
show(step,false);if(bad)bad.focus();}
f.addEventListener('input',function(){save();update();prog();});f.addEventListener('change',function(){save();update();prog();});})();
