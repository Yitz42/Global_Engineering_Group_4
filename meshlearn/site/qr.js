(function(){var els=document.querySelectorAll('[data-qr]');if(!els.length)return;var s=document.createElement('script');s.src='/site/vendor/qrcode_generator/qrcode.js';
s.onload=function(){for(var i=0;i<els.length;i++){var q=qrcode(0,'M');q.addData(els[i].getAttribute('data-qr'));q.make();els[i].innerHTML=q.createSvgTag({cellSize:6,margin:12,scalable:true});}};
document.body.appendChild(s);})();
