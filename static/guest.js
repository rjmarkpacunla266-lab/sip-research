/* Guest notice: a slim banner under the header, and a small "Guest" chip once it is closed.
   Only shows for guest accounts (from /api/me). Safe to load on any page. */
(function(){
  var KEY='sturch_guest_note';
  function get(){try{return localStorage.getItem(KEY)}catch(e){return null}}
  function set(v){try{v?localStorage.setItem(KEY,v):localStorage.removeItem(KEY)}catch(e){}}
  fetch('/api/me').then(function(r){return r.ok?r.json():null}).then(function(u){
    if(!u||!u.is_guest)return;
    var days=u.guest_days||30,hd=document.querySelector('header.top')||document.querySelector('header');
    if(!hd)return;
    var st=document.createElement('style');
    st.textContent='.gn{display:flex;align-items:center;gap:10px;margin:10px 0 0;padding:10px 12px;border-radius:14px;border:1px solid var(--orange,#e0852f);background:var(--orange-bg,rgba(240,145,75,.16));color:var(--text);font-size:.86rem;line-height:1.4}'
      +'.gn span.t{flex:1;min-width:0}.gn a.gb{flex:none;display:inline-flex;align-items:center;min-height:40px;padding:0 14px;border-radius:999px;background:var(--accent,#2f5bea);color:#fff!important;font-weight:700;font-size:.82rem;text-decoration:none}'
      +'.gn button{flex:none;width:40px;height:40px;border:0;background:none;color:var(--muted,#7a86a8);font-size:1.05rem;cursor:pointer}'
      +'.gchip{flex:none;display:inline-flex;align-items:center;gap:4px;min-height:36px;padding:0 12px;border-radius:999px;border:1px solid var(--orange,#e0852f);background:var(--orange-bg,rgba(240,145,75,.16));color:var(--text);font:700 .78rem system-ui,sans-serif;cursor:pointer}'
      +'@media(max-width:520px){.gn{flex-wrap:wrap}.gn span.t{flex:1 1 100%}}';
    document.head.appendChild(st);
    var msg="You're using a guest account. Your saved papers stay on this browser for "+days+" days.";
    var bar=null,chip=null;
    function showBar(){
      if(bar)return;set(null);if(chip){chip.remove();chip=null}
      bar=document.createElement('div');bar.className='gn';bar.setAttribute('role','note');
      bar.innerHTML='<span aria-hidden="true">ⓘ</span><span class="t"></span><a class="gb" href="/signup">Create account</a><button type="button" aria-label="Close">✕</button>';
      bar.querySelector('.t').textContent=msg;
      bar.querySelector('button').onclick=function(){bar.remove();bar=null;set('closed');showChip()};
      hd.insertAdjacentElement('afterend',bar);
    }
    function showChip(){
      if(chip)return;var me=hd.querySelector('.me');if(!me)return;
      chip=document.createElement('button');chip.type='button';chip.className='gchip';chip.textContent='Guest';chip.setAttribute('aria-label','Guest account info');
      chip.onclick=showBar;me.parentNode.insertBefore(chip,me);
      /* no room in the header (small phones): drop the chip and use a menu item instead */
      if(hd.scrollWidth>hd.clientWidth+1||(me.offsetTop-hd.offsetTop)>20){chip.remove();chip=null;var m=hd.querySelector('.menu');if(m&&!m.querySelector('.gmi')){var a=document.createElement('a');a.className='gmi';a.href='#';a.setAttribute('role','menuitem');a.textContent='\u24d8 Guest account';a.onclick=function(e){e.preventDefault();showBar();window.scrollTo(0,0)};var w=m.querySelector('.menu-who');w?w.after(a):m.prepend(a);}}
    }
    if(get()==='closed')showChip();else showBar();
  }).catch(function(){});
})();
