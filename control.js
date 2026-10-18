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
    const outFmt = {w:0, h:0};             // format de sortie, relevé par l'état
    const ml = this._ml = window.MediaLibrary.mount($('.stl-lib-host'), {
      vmid, T, toast, kinds:['image'], view:'grid',
      actions:[{id:'show', label:T('plugin.stills.pick','Choisir'), primary:true}],
      onAction:(id, m)=>{ selected = m.path; choose(); ml.markCurrent ? ml.markCurrent() : ml.render(); },   // repère seul : render() rechargeait toutes les vignettes
      current:()=>selected,
      imageFormat:()=>(outFmt.w ? {w:outFmt.w, h:outFmt.h} : undefined),   // l'éditeur d'image propose le format de SORTIE
      hint:T('plugin.stills.lib_hint2','Double-clic ou « Choisir » : l\'image passe en PREVIEW (TAKE pour l\'antenne ; en mode « Take direct », elle part aussitôt). Glisser une image sur un bin : l\'y ranger.'),
    });

    const direct = () => !!($('.stl-direct') && $('.stl-direct').checked);
    const params = () => ({
      file: selected, folder: selected.includes('/') ? selected.slice(0, selected.lastIndexOf('/')) : '',
      fit: fit(), x: xv(), y: yv(), size: sv()
    });
    const syncMode = () => $('.stl-ctl').classList.toggle('is-direct', direct());

    // Mode Take : choisir met en PREVIEW (rien n'est écrit sur le bus), TAKE envoie à l'antenne.
    // Mode direct : choisir = antenne (ancien comportement).
    async function apply(){
      if(!selected){ toast(T('plugin.stills.pick_image','Choisissez une image'),'warning'); return; }
      try{
        const r = await (await api('/select', params())).json();
        if(r.ok) toast(T('plugin.stills.shown','Image affichée : ') + selected.split('/').pop(), 'info');
        else     toast(T('plugin.stills.err_prefix','Erreur : ') + (r.detail || r.error || '?'), 'error');
        setTimeout(refresh, 300);
      }catch(e){ toast(T('plugin.stills.err_network','Erreur réseau'),'error'); }
    }
    async function previewNow(){
      if(!selected) return;
      try{
        const r = await (await api('/preview', params())).json();
        if(!r.ok) toast(T('plugin.stills.err_prefix','Erreur : ') + (r.detail || r.error || '?'), 'error');
      }catch(e){ toast(T('plugin.stills.err_network','Erreur réseau'),'error'); }
      refresh();
    }
    const choose = () => direct() ? apply() : previewNow();
    async function takeOrCut(path){
      try{
        const r = await (await api(path, {})).json();
        if(r.ok) toast(T('plugin.stills.on_air','À l\'antenne : ') + (r.file||'').split('/').pop(), 'info');
        else     toast(T('plugin.stills.err_prefix','Erreur : ') + (r.detail || r.error || '?'), 'error');
      }catch(e){ toast(T('plugin.stills.err_network','Erreur réseau'),'error'); }
      setTimeout(refresh, 150);
    }

    const FIT_LBL = {native:T('plugin.stills.fit_native','Pixel à pixel'), fit:T('plugin.stills.fit_fit','Au plus grand'), stretch:T('plugin.stills.fit_stretch','Plein cadre')};
    function infoLine(m){
      if(!m) return '';
      const pos = (m.x||m.y||m.size!==100) ? ` (${m.x},${m.y} · ${m.size}%)` : '';
      return `${m.name} · ${m.src_w}×${m.src_h} · ${m.format||'?'}${m.alpha?' + alpha':''}`
           + ` · ${T('plugin.stills.framing','cadrage')} ${FIT_LBL[m.fit]||m.fit}${pos}`;
    }
    function sinceTxt(ts){
      if(!ts) return '';
      const d = Math.max(0, Math.round(Date.now()/1000 - ts)), t = new Date(ts*1000).toLocaleTimeString();
      return ` · ${T('plugin.stills.since','depuis')} ${t} (${d < 90 ? d + ' s' : Math.round(d/60) + ' min'})`;
    }
    // Les JPEG ne sont redemandés que si l'image a changé (gen) ou pendant un fondu ; le conteneur
    // met en cache et n'encode qu'au changement.
    const seen = {pv:-1, pg:-1, key:false, fadeTick:0};
    function setImg(cls, url, show){
      const im = $('.stl-'+cls+'-img'), em = $('.stl-'+cls+'-empty');
      if(show){ if(im.dataset.u !== url){ im.dataset.u = url; im.src = url; } im.hidden = false; em.hidden = true; }
      else { im.hidden = true; em.hidden = false; delete im.dataset.u; }
    }

    async function refresh(){
      try{
        const s = await (await api('/state')).json();
        const pvf = s.preview_file || '';
        if(!selected){
          selected = pvf || s.file || '';
          const p = s.preview || {};
          if(selected){ setFit(p.fit || s.fit || 'native');
            $('.stl-x').value = p.x ?? s.x ?? 0; $('.stl-y').value = p.y ?? s.y ?? 0; $('.stl-size').value = p.size ?? s.size ?? 100; ml.render(); }
        }
        const dc = $('.stl-direct'); if(dc && dc !== document.activeElement) dc.checked = !!s.take_direct; syncMode();
        const fe = $('.stl-fade'); if(fe && fe !== document.activeElement) fe.value = Math.round(s.transition_ms || 0);
        const kv = $('.stl-viewkey').checked ? 'key' : 'fill', base = `/api/containers/${vmid}/plugin`;
        const pm = s.preview_meta, gm = s.program_meta;
        // Preview
        if(pm && !s.preview_error){
          const g = `${s.preview_gen}-${kv}`;
          setImg('pv', `${base}/preview.jpg?view=${kv}&g=${g}`, true);
          $('.stl-pv-info').textContent = infoLine(pm);
        } else { setImg('pv', '', false); $('.stl-pv-info').textContent = s.preview_error || ''; }
        // Programme (pendant un fondu : on redemande à chaque relevé)
        if(gm){
          const tick = s.fading ? ++seen.fadeTick : 0;
          setImg('pg', `${base}/program.jpg?view=${kv}&g=${s.program_gen}-${kv}-${tick}`, true);
          $('.stl-pg-info').textContent = infoLine(gm) + sinceTxt(s.on_air_since) + (s.fading ? ' · ' + T('plugin.stills.fading','fondu en cours') : '');
        } else { setImg('pg', '', false); $('.stl-pg-info').textContent = s.file ? s.file.split('/').pop() : ''; }
        $('.stl-take').disabled = $('.stl-cut').disabled = !pvf;
        outFmt.w = s.width || 0; outFmt.h = s.height || 0;
        status.textContent = `${s.width||0}×${s.height||0} · ${s.fps||0} fps`
          + ` · Fill:${s.fill_shm||'—'} Key:${s.key_shm||'—'} · v${s.plugin_version}`;
      }catch(e){ status.textContent = T('plugin.stills.state_na','état indisponible'); }
    }

    $('.stl-take').onclick = () => takeOrCut('/take');
    $('.stl-cut').onclick  = () => takeOrCut('/cut');
    $('.stl-direct').onchange = async () => {
      await api('/take_direct', {enabled: direct()}); syncMode();
      if(direct() && selected) apply();   // bascule en direct : ce qui est choisi part aussitôt
    };
    $('.stl-viewkey').onchange = refresh;
    // Durée de fondu par défaut, à chaud. Elle ne s'applique qu'aux CHANGEMENTS d'image : régler
    // la position reste une coupe (règle côté conteneur).
    const fadeEl = $('.stl-fade');
    if(fadeEl) fadeEl.onchange = () => api('/transition', {ms: Math.max(0, Math.min(10000, parseFloat(fadeEl.value) || 0))});
    el.querySelectorAll('input[name="stl-fit"]').forEach(r => r.onchange = choose);
    ['.stl-x','.stl-y','.stl-size'].forEach(s => { const e = $(s); if(e) e.onchange = choose; });

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
      if(window.MXLMonitor){ window.MXLMonitor.send(hn, hn + ' (Fill)'); if(window.MXLMonitor.toggle) window.MXLMonitor.toggle(); }
      else toast('Moniteur indisponible','warning');
    };

    refresh();
    if(this._timer) clearInterval(this._timer);
    this._timer = setInterval(refresh, 2000);
  }
};
