(function(){var box=document.getElementById('kids');if(!box)return;var rows=box.querySelectorAll('[data-name]'),q=document.getElementById('find'),none=document.getElementById('nobody');
var ls=document.querySelectorAll('[data-letter]'),last=document.getElementById('lastone');
function plain(s){s=s.toLowerCase();return s.normalize?s.normalize('NFD').replace(/[̀-ͯ]/g,''):s;}
function show(test){var n=0;for(var i=0;i<rows.length;i++){var ok=test(plain(rows[i].getAttribute('data-name')));rows[i].style.display=ok?'':'none';if(ok)n++;}
if(none)none.style.display=n?'none':'';}
function type(){var t=plain(q.value.replace(/^\s+|\s+$/g,''));show(function(n){if(!t)return true;var w=n.split(' ');for(var i=0;i<w.length;i++)if(w[i].indexOf(t)===0)return true;return n.indexOf(t)===0;});}
if(q){q.addEventListener('input',type);type();}
for(var i=0;i<ls.length;i++)ls[i].addEventListener('click',function(){var l=this.getAttribute('data-letter');
for(var j=0;j<ls.length;j++)ls[j].setAttribute('aria-pressed',ls[j]===this?'true':'false');show(function(n){return !l||plain(n).charAt(0)===plain(l);});});
for(i=0;i<rows.length;i++)rows[i].addEventListener('click',function(){try{localStorage.setItem('meshlearn-last',this.getAttribute('data-name'));}catch(e){}});
try{var name=localStorage.getItem('meshlearn-last');if(last&&name)for(i=0;i<rows.length;i++)if(rows[i].getAttribute('data-name')===name){
var c=rows[i].cloneNode(true);c.className='lastkid';c.insertBefore(document.createTextNode(''),c.firstChild);
var g=c.querySelector('.grow');if(g)g.innerHTML='<small></small><strong></strong>';if(g){g.firstChild.textContent=last.getAttribute('data-label');g.lastChild.textContent=last.getAttribute('data-im').replace('{name}',name);}
last.appendChild(c);last.style.display='';}}catch(e){}
})();
