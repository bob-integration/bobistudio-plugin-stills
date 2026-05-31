// Stills control fragment. Registered on window.MXLPlugins["stills"].
// mount(el, vmid, ctx) appelé par le shell Medias après injection de control.html.
window.MXLPlugins = window.MXLPlugins || {};
window.MXLPlugins["stills"] = {
  mount(el, vmid, ctx){
    const toast = (ctx && ctx.toast) || (()=>{});
    const inst  = (ctx && ctx.instance) || {};

    const _pb = el.querySelector('.stl-project-banner'), _npb = el.querySelector('.stl-noproject-banner');
    if (inst.project) { if (_pb) { _pb.style.display=''; _pb.querySelector('.stl-project-name').textContent=inst.project.name; } }
    else { if (_npb) _npb.style.display=''; }

    const api = (path, body) => fetch(`/api/containers/${vmid}/plugin${path}`, {
      method: body===undefined ? 'GET' : 'POST',
      headers: {'Content-Type':'application/json'},
      body: body===undefined ? undefined : JSON.stringify(body||{})
    });
    const $ = s => el.querySelector(s);
    const status = $('.stl-status');
    let selected = '';   // chemin (relatif) de l'image sélectionnée

    const fit  = () => { const r = el.querySelector('input[name="stl-fit"]:checked'); return r ? r.value : 'native'; };
    const setFit = v => { const r = el.querySelector(`input[name="stl-fit"][value="${v}"]`); if(r) r.checked = true; };
    const xv = () => parseFloat($('.stl-x').value || '0') || 0;
    const yv = () => parseFloat($('.stl-y').value || '0') || 0;
    const sv = () => parseFloat($('.stl-size').value || '100') || 100;

    async function loadFolders(){
      try{
        const d = await (await api('/folders')).json();
        const sel = $('.stl-folder'); const cur = sel.value;
        const list = d.folders || [];
        sel.innerHTML = list.length
          ? list.map(f => `<option value="${f.replace(/"/g,'&quot;')}">${f || '(racine)'}</option>`).join('')
          : '<option value="">(aucun dossier contenant des images)</option>';
        if(cur && list.includes(cur)) sel.value = cur;
        loadImages();
      }catch(e){ status.textContent = 'stockage indisponible'; }
    }

    async function loadImages(){
      const folder = $('.stl-folder').value || '';
      const grid = $('.stl-grid');
      grid.innerHTML = '<div class="stl-empty">Chargement…</div>';
      try{
        const d = await (await api('/images?folder=' + encodeURIComponent(folder))).json();
        const imgs = d.images || [];
        if(!imgs.length){ grid.innerHTML = '<div class="stl-empty">Aucune image dans ce dossier.</div>'; return; }
        grid.innerHTML = imgs.map(p => {
          const pe = p.replace(/"/g,'&quot;');
          return `<button type="button" class="stl-thumb${p===selected?' sel':''}" data-path="${pe}" title="${pe}">
            <img loading="lazy" src="/api/containers/${vmid}/plugin/thumb?path=${encodeURIComponent(p)}" alt="">
            <span>${(p.split('/').pop())}</span>
          </button>`;
        }).join('');
        grid.querySelectorAll('.stl-thumb').forEach(b => b.onclick = () => {
          selected = b.dataset.path;
          grid.querySelectorAll('.stl-thumb').forEach(x => x.classList.toggle('sel', x===b));
          apply();
        });
      }catch(e){ grid.innerHTML = '<div class="stl-empty">Erreur de chargement.</div>'; }
    }

    async function apply(){
      if(!selected){ toast('Choisissez une image','warning'); return; }
      try{
        const r = await (await api('/select', {
          file: selected, folder: $('.stl-folder').value || '',
          fit: fit(), x: xv(), y: yv(), size: sv()
        })).json();
        if(r.ok) toast('Image affichée : ' + selected.split('/').pop(), 'info');
        else     toast('Erreur : ' + (r.detail || '?'), 'error');
        setTimeout(refresh, 300);
      }catch(e){ toast('Erreur réseau','error'); }
    }

    async function refresh(){
      try{
        const s = await (await api('/state')).json();
        if(s.file && !selected){ selected = s.file; setFit(s.fit || 'native'); }
        status.textContent = `${s.file ? ('image: ' + s.file.split('/').pop()) : 'aucune image'}`
          + ` · ${s.width||0}×${s.height||0} · ${s.fps||0} fps`
          + ` · Fill:${s.fill_shm||'—'} Key:${s.key_shm||'—'} · v${s.plugin_version}`;
      }catch(e){ status.textContent = 'état indisponible'; }
    }

    $('.stl-folder').onchange = loadImages;
    $('.stl-refresh').onclick = loadFolders;
    $('.stl-apply').onclick = apply;
    el.querySelectorAll('input[name="stl-fit"]').forEach(r => r.onchange = apply);
    ['.stl-x','.stl-y','.stl-size'].forEach(s => { const e = $(s); if(e) e.onchange = apply; });

    const adv = $('.stl-adv'), advBtn = $('.stl-adv-toggle');
    if(advBtn) advBtn.onclick = () => {
      const open = adv.hasAttribute('hidden');
      if(open) adv.removeAttribute('hidden'); else adv.setAttribute('hidden','');
      advBtn.classList.toggle('is-open', open);
      advBtn.setAttribute('aria-expanded', open ? 'true' : 'false');
    };

    $('.stl-monitor').onclick = () => {
      const hn = inst.hostname || '';
      if(!hn){ toast('Hostname inconnu','warning'); return; }
      if(window.MXLMonitor) window.MXLMonitor.send(hn, hn + ' (Fill)');
      else toast('Moniteur indisponible','warning');
    };

    loadFolders();
    refresh();
    if(this._timer) clearInterval(this._timer);
    this._timer = setInterval(refresh, 2000);
  }
};
