(function(){var m=document.getElementById('main');if(!m)return;var t=m.textContent,ps=m.querySelectorAll('[data-prompt]');for(var i=0;i<ps.length;i++)t+=ps[i].getAttribute('data-prompt');
if(t.indexOf('\\(')<0&&t.indexOf('$$')<0)return;
function add(tag,attrs,done){var e=document.createElement(tag);for(var k in attrs)e[k]=attrs[k];if(done)e.onload=done;document.head.appendChild(e);}
window.meshMath=function(el){if(window.renderMathInElement)renderMathInElement(el,{delimiters:[{left:'$$',right:'$$',display:true},{left:'\\(',right:'\\)',display:false}],throwOnError:false,ignoredTags:['script','noscript','style','textarea','pre','code','option','input']});};
add('link',{rel:'stylesheet',href:'/site/vendor/katex/katex.min.css'});
add('script',{src:'/site/vendor/katex/katex.min.js'},function(){add('script',{src:'/site/vendor/katex/auto-render.min.js'},function(){meshMath(m);});});
})();
