(function(){function store(){var d={},m={getItem:function(k){k=String(k);return Object.prototype.hasOwnProperty.call(d,k)?d[k]:null;},setItem:function(k,v){d[String(k)]=String(v);},removeItem:function(k){delete d[String(k)];},clear:function(){d={};},key:function(i){var k=Object.keys(d)[i];return k===undefined?null:k;}};
Object.defineProperty(m,'length',{get:function(){return Object.keys(d).length;}});return m;}
['localStorage','sessionStorage'].forEach(function(n){var ok=false;try{ok=!!window[n];}catch(e){}if(!ok)try{Object.defineProperty(window,n,{value:store(),configurable:true});}catch(e){}});
var c=false;try{c=typeof document.cookie==='string';}catch(e){}
if(!c)try{Object.defineProperty(document,'cookie',{get:function(){return '';},set:function(){},configurable:true});}catch(e){}
})();
