(function(){
  var s=document.currentScript,cur=s.dataset.cur,map=JSON.parse(s.dataset.map||'{}'),KEY='mmr-lang';
  var ls=function(f){try{return f()}catch(e){return null}};
  document.querySelectorAll('.langs a').forEach(function(a){
    a.addEventListener('click',function(){ls(function(){localStorage.setItem(KEY,a.dataset.lang)})});
  });
  document.addEventListener('click',function(e){
    document.querySelectorAll('details.langs[open]').forEach(function(d){if(!d.contains(e.target))d.open=false});
  });
  if(ls(function(){return localStorage.getItem(KEY)}))return;
  var n=((navigator.languages&&navigator.languages[0])||navigator.language||'en').toLowerCase();
  var c=n.slice(0,2);
  if(n.indexOf('zh')===0)c='zh';
  if(c==='nb'||c==='nn')c='no';
  if(c===cur||!map[c])return;
  var bar=document.createElement('div');bar.className='langbar';bar.setAttribute('lang',c);
  var a=document.createElement('a');a.href=map[c][0];a.textContent=map[c][2];
  var x=document.createElement('button');x.type='button';x.setAttribute('aria-label','×');x.textContent='×';
  x.onclick=function(){bar.remove();ls(function(){localStorage.setItem(KEY,cur)})};
  a.onclick=function(){ls(function(){localStorage.setItem(KEY,c)})};
  bar.appendChild(a);bar.appendChild(x);document.body.appendChild(bar);
})();
