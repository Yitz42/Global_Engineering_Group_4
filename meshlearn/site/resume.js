(function(){var bs=document.querySelectorAll('[data-draft]');for(var i=0;i<bs.length;i++){try{
var d=JSON.parse(localStorage.getItem('meshlearn-draft:'+bs[i].getAttribute('data-draft'))||'{}'),n=0;
for(var k in d)if(k.charAt(0)!=='#')n++;if(n)bs[i].querySelector('.lb').textContent=bs[i].getAttribute('data-resume');
}catch(e){}}})();
