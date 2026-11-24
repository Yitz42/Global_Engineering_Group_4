(function(){var p=document.getElementById('readpanel');if(!p)return;var key='meshlearn-reading',st={z:1,f:false,s:false},root=document.documentElement,sizes=[1,1.15,1.3,1.5];
try{var o=JSON.parse(localStorage.getItem(key)||'{}');for(var k in st)if(o.hasOwnProperty(k))st[k]=o[k];}catch(e){}
if(sizes.indexOf(st.z)<0)st.z=1;
function apply(){var m=document.getElementById('main');if(m)m.style.zoom=st.z===1?'':st.z;
var keep=document.querySelectorAll('.qhead,.qrbox,#readpanel,#scratch');for(var i=0;i<keep.length;i++)keep[i].style.zoom=st.z===1?'':1/st.z;
root.classList[st.f?'add':'remove']('odys');root.classList[st.s?'add':'remove']('spacey');
p.querySelector('[data-font]').setAttribute('aria-pressed',st.f?'true':'false');p.querySelector('[data-space]').setAttribute('aria-pressed',st.s?'true':'false');}
function save(){try{localStorage.setItem(key,JSON.stringify(st));}catch(e){}apply();}
function step(d){var i=sizes.indexOf(st.z)+d;st.z=sizes[Math.max(0,Math.min(sizes.length-1,i))];save();}
p.querySelector('[data-up]').onclick=function(){step(1);};p.querySelector('[data-down]').onclick=function(){step(-1);};
p.querySelector('[data-font]').onclick=function(){st.f=!st.f;save();};p.querySelector('[data-space]').onclick=function(){st.s=!st.s;save();};
p.querySelector('[data-shut]').onclick=function(){p.style.display='none';};
var bs=document.querySelectorAll('[data-reading]');for(var i=0;i<bs.length;i++)bs[i].onclick=function(){p.style.display=p.style.display==='none'?'':'none';};
apply();})();
