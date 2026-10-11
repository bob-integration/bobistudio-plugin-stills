// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 BOBI SAS, France
// Auteur : Cyril Mazouer, pour le compte de BOBI SAS
// Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

// Stills control fragment. Registered on window.MXLPlugins["stills"].
// mount(el, vmid, ctx) appelé par le shell Medias après injection de control.html.
window.MXLPlugins = window.MXLPlugins || {};
window.MXLPlugins["stills"] = {
  mount(el, vmid, ctx){
    const toast = (ctx && ctx.toast) || (()=>{});
    const inst  = (ctx && ctx.instance) || {};

    /* i18n (catalogue plugin.stills.*) — repli sur le français du 2e argument. */
    const T = (k, repli) => {
      const v = window.t ? window.t(k) : k;
      return (v && v !== k) ? v : (repli !== undefined ? repli : k);
    };
    if (window.t) {
      el.querySelectorAll('[data-i18n]').forEach(n => { n.textContent = T(n.dataset.i18n, n.textContent); });
      el.querySelectorAll('[data-i18n-title]').forEach(n => { n.title = T(n.dataset.i18nTitle, n.title); });
      el.querySelectorAll('[data-i18n-aria]').forEach(n => n.setAttribute('aria-label', T(n.dataset.i18nAria, n.getAttribute('aria-label'))));
    }

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

    // Images : la bibliothèque commune (base des médias), limitée aux images, en vignettes. Une
    // image doit avoir une copie que ce conteneur voit (son nœud, le dossier de son projet) ; une
    // image d'un serveur réseau est importée puis affichée.
    if(this._ml){ try{ this._ml.destroy(); }catch(_){} }
    const ml = this._ml = window.MediaLibrary.mount($('.stl-lib-host'), {
      vmid, T, toast, kinds:['image'], view:'grid',
      actions:[{id:'show', label:T('plugin.stills.show','Afficher'), primary:true}],
      onAction:(id, m)=>{ selected = m.path; apply(); ml.render(); },
      current:()=>selected,
      hint:T('plugin.stills.lib_hint','Double-clic ou « Afficher » : l\'image part à l\'antenne. Glisser une image sur un bin : l\'y ranger.'),
    });

    async function apply(){
      if(!selected){ toast(T('plugin.stills.pick_image','Choisissez une image'),'warning'); return; }
      try{
        const r = await (await api('/select', {
          file: selected, folder: selected.includes('/') ? selected.slice(0, selected.lastIndexOf('/')) : '',
          fit: fit(), x: xv(), y: yv(), size: sv()
        })).json();
        if(r.ok) toast(T('plugin.stills.shown','Image affichée : ') + selected.split('/').pop(), 'info');
        else     toast(T('plugin.stills.err_prefix','Erreur : ') + (r.detail || '?'), 'error');
        setTimeout(refresh, 300);
      }catch(e){ toast(T('plugin.stills.err_network','Erreur réseau'),'error'); }
    }

    async function refresh(){
      try{
        const s = await (await api('/state')).json();
        if(s.file && !selected){ selected = s.file; setFit(s.fit || 'native'); ml.render(); }
        const fe = $('.stl-fade'); if(fe && fe !== document.activeElement) fe.value = Math.round(s.transition_ms || 0);
        status.textContent = `${s.file ? (T('plugin.stills.img_prefix','image: ') + s.file.split('/').pop()) : T('plugin.stills.no_img','aucune image')}`
          + ` · ${s.width||0}×${s.height||0} · ${s.fps||0} fps`
          + ` · Fill:${s.fill_shm||'—'} Key:${s.key_shm||'—'} · v${s.plugin_version}`;
      }catch(e){ status.textContent = T('plugin.stills.state_na','état indisponible'); }
    }

    $('.stl-apply').onclick = apply;
    // Durée de fondu par défaut, à chaud. Elle ne s'applique qu'aux CHANGEMENTS d'image : régler
    // la position reste une coupe (règle côté conteneur).
    const fadeEl = $('.stl-fade');
    if(fadeEl) fadeEl.onchange = () => api('/transition', {ms: Math.max(0, Math.min(10000, parseFloat(fadeEl.value) || 0))});
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

    refresh();
    if(this._timer) clearInterval(this._timer);
    this._timer = setInterval(refresh, 2000);
  }
};
