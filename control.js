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
    if (inst.project) { if (_pb) { _pb.hidden = false; _pb.querySelector('.stl-project-name').textContent=inst.project.name; } }
    else { if (_npb) _npb.hidden = false; }

    const api = (path, body) => fetch(`/api/containers/${vmid}/plugin${path}`, {
      method: body===undefined ? 'GET' : 'POST',
      headers: {'Content-Type':'application/json'},
      body: body===undefined ? undefined : JSON.stringify(body||{})
    });
    const $ = s => el.querySelector(s);
    const status = $('.stl-status');
    let selected = '';   // chemin (relatif) de l'image sélectionnée

    const fit  = () => { const r = el.querySelector('input[name="stl-fit"]:checked'); return r ? r.value : 'fit'; };
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
      vmid, T, toast, kinds:['image'], view:'grid', fill:true,   // fills .stl-lib-host, sized below
      autoRefresh:2000,                                          // new media appear by themselves (2026-11-26)
      // Share import (a folder or a selection of a network server): also into a grid (2026-11-22).
      importTargets: (name, opt) => {
        const out = [opt && opt.subfolders
          ? {label:T('plugin.stills.imp_grid_per_dir','Une grille par sous-dossier (« {n} », « {n} / … »)').replace(/\{n\}/g, name), run:rs => gridsFromImport(name, rs, true)}
          : {label:T('plugin.stills.imp_new_grid','Nouvelle grille « {n} »').replace('{n}', name), run:rs => gridsFromImport(name, rs, false)}];
        if(G.grid) out.push({label:T('plugin.stills.imp_open_grid','Grille « {g} », dans les cases vides').replace('{g}', G.grid.name), run:rs => gridFromImport(name, rs, G.grid.id)});
        return out;
      },
      actions:[{id:'show', label:T('plugin.stills.pick','Choisir'), primary:true}],
      onAction:(id, m)=>{ selected = m.path; choose(); ml.markCurrent ? ml.markCurrent() : ml.render(); },   // repère seul : render() rechargeait toutes les vignettes
      current:()=>selected,
      imageFormat:()=>(outFmt.w ? {w:outFmt.w, h:outFmt.h} : undefined),   // l'éditeur d'image propose le format de SORTIE
      hint:T('plugin.stills.lib_hint3','Double-clic ou « Choisir » : l\'image passe en PREVIEW (TAKE pour l\'antenne ; en mode « Take direct », elle part aussitôt). Un fichier de l\'ordinateur se dépose sur l\'écran PREVIEW, ici (onglet Base) ou dans une case de grille en préparation. Glisser une image sur un bin : l\'y ranger.'),
    });

    // Écrans PREVIEW / PROGRAMME redimensionnables : glisser la poignée (souris ou doigt), flèches au
    // clavier, double-clic = taille par défaut (CSS : ~45 % de la hauteur de la fenêtre). Confort de l'opérateur, propre à ce navigateur (pas un
    // réglage du conteneur) ; stockage indisponible (navigation privée) : taille par défaut.
    const PP_KEY = 'stills-pp-width', PP_MIN = 240, ctlEl = el.querySelector('.stl-ctl'), pp = $('.stl-pp'), grip = $('.stl-pp-grip');
    const setPp = w => {
      const max = ctlEl.clientWidth || 0;
      if(!(w > 0)){ pp.style.maxWidth = ''; ctlEl.classList.toggle('is-compact', pp.getBoundingClientRect().width < 520); return 0; }
      if(max && w >= max){ pp.style.maxWidth = 'none'; ctlEl.classList.remove('is-compact'); return max; }
      w = Math.round(Math.max(PP_MIN, w));
      pp.style.maxWidth = w + 'px'; ctlEl.classList.toggle('is-compact', w < 520);
      return w;
    };
    // AUX beside the PGM (asked 2026-11-14, then 2026-11-26):
    //  - tall screens: small AUX (AUX_W) in the fewest columns that keep their stack within the height
    //    of the screens — one column, then side by side;
    //  - otherwise ONE row where every screen has the same size: PREVIEW, PROGRAMME and each AUX.
    // Each candidate is applied and measured (the AUX width changes the screens' width).
    const AUX_W = 150, AUX_GAP = 6, PP_GAP = 10, SCR_MIN = 160, pgScr = $('.stl-pane-pg .stl-screen');
    const auxCols = (k, t) => { pp.style.setProperty('--stl-aux-cols', k); pp.style.setProperty('--stl-aux-tile', t + 'px');
      pp.style.setProperty('--stl-aux-w', (k * t + (k - 1) * AUX_GAP) + 'px'); };
    function auxLayout(){
      const box = pp.querySelector('.stl-aux'), n = box && !box.hidden ? box.children.length : 0;
      const side = n && getComputedStyle(box).display === 'grid';   // row modes (PGM right, narrow) use flex
      pp.classList.remove('is-aux-row');
      if(!side){ auxCols(1, AUX_W); return keepScreens(); }
      for(let k = 1; k < n; k++){
        auxCols(k, AUX_W);
        const rows = Math.ceil(n / k), fh = box.firstElementChild.offsetHeight, scr = pgScr.getBoundingClientRect();
        if(scr.width >= SCR_MIN - 1 && rows * fh + (rows - 1) * AUX_GAP <= scr.height + 1) return pp.getBoundingClientRect().width;
      }
      // One row, all screens alike: S = (row − TAKE − gaps) / (2 + n), never under SCR_MIN while the row can widen.
      pp.classList.add('is-aux-row');
      const take = $('.stl-take-col'), fixed = () => (take ? take.getBoundingClientRect().width : 0) + 3 * PP_GAP + (n - 1) * AUX_GAP;
      const equal = () => { const S = Math.max(1, Math.floor((pp.getBoundingClientRect().width - fixed()) / (2 + n))); auxCols(n, S); return S; };
      let S = equal();
      for(let i = 0; i < 4 && S < SCR_MIN && pp.style.maxWidth && pp.style.maxWidth !== 'none'; i++){
        setPp(fixed() + (2 + n) * SCR_MIN + 2); S = equal();
      }
      return pp.getBoundingClientRect().width;
    }
    // The grip width covers the whole row: the TAKE column and the AUX (150 px each) are fixed, so a
    // small width left the PREVIEW / PROGRAMME screens at 0 px — they vanished (2026-11-26). Widen the
    // row until each screen keeps SCR_MIN; returns the width actually applied (saved by the grip).
    function keepScreens(){
      let cur = pp.getBoundingClientRect().width;
      for(let i = 0; i < 6; i++){          // a squeezed screen measures 0: widen step by step
        const sw = pgScr.getBoundingClientRect().width;
        if(ctlEl.classList.contains('is-pgm-right') || !cur || sw >= SCR_MIN - 1 || !pp.style.maxWidth || pp.style.maxWidth === 'none') break;
        setPp(cur + 2 * (SCR_MIN - sw)); cur = pp.getBoundingClientRect().width;
      }
      return cur;
    }
    let ctlW = 0;
    new ResizeObserver(() => { const w = ctlEl.clientWidth; if(w !== ctlW){ ctlW = w; auxLayout(); } }).observe(ctlEl);
    // Library down to the bottom of the window (2026-11-15): its host gets the height left below its
    // own top, recomputed whenever something above it changes height (screens, grip, layout).
    const libTop = () => { const h = $('.stl-lib-host'); if(h) ctlEl.style.setProperty('--stl-lib-top', Math.round(h.getBoundingClientRect().top + window.scrollY) + 'px'); };
    { const ro = new ResizeObserver(libTop); ['.stl-side', '.stl-pp-opts', '.stl-grid-wrap'].forEach(q => { const n = $(q); if(n) ro.observe(n); }); }
    requestAnimationFrame(libTop);
    const onWinResize = () => { if(!el.isConnected){ window.removeEventListener('resize', onWinResize); return; } auxLayout(); libTop(); };
    window.addEventListener('resize', onWinResize);   // window height: the screens follow 45vh
    const savePp = w => { try{ if(w) localStorage.setItem(PP_KEY, String(w)); else localStorage.removeItem(PP_KEY); }catch(_){} };
    try{ setPp(parseFloat(localStorage.getItem(PP_KEY))); }catch(_){}
    // Deux écrans 16:9 côte à côte : 1 px de hauteur ≈ 2 × 16/9 px de largeur → la bordure basse suit le doigt.
    const PX_PER_DY = 32 / 9;
    grip.addEventListener('pointerdown', ev => {
      ev.preventDefault(); grip.setPointerCapture(ev.pointerId); grip.classList.add('is-drag');
      const y0 = ev.clientY, w0 = pp.getBoundingClientRect().width;
      let w = w0;
      const move = e => { setPp(w0 + (e.clientY - y0) * PX_PER_DY); w = auxLayout(); };
      const up = () => { grip.classList.remove('is-drag'); savePp(w);
        grip.removeEventListener('pointermove', move); grip.removeEventListener('pointerup', up); grip.removeEventListener('pointercancel', up); };
      grip.addEventListener('pointermove', move); grip.addEventListener('pointerup', up); grip.addEventListener('pointercancel', up);
    });
    grip.addEventListener('dblclick', () => { savePp(setPp(0)); auxLayout(); });
    grip.addEventListener('keydown', e => {
      const d = e.key === 'ArrowUp' ? -40 : e.key === 'ArrowDown' ? 40 : 0; if(!d) return;
      e.preventDefault(); setPp(pp.getBoundingClientRect().width + d * (e.shiftKey ? 4 : 1)); savePp(auxLayout());
    });

    // Disposition « PGM seul, à droite » : la bibliothèque prend toute la hauteur à gauche. Préférence
    // de ce navigateur, comme la taille des écrans. Poignée verticale : largeur de la colonne de droite.
    const PGM_KEY = 'stills-pgm-right', SIDE_KEY = 'stills-side-width', SIDE_MIN = 260, sideGrip = $('.stl-side-grip');
    const setSide = w => {
      const max = Math.max(SIDE_MIN, (ctlEl.clientWidth || 0) * 0.6);
      if(!(w > 0)){ ctlEl.style.removeProperty('--stl-side-w'); return 0; }
      w = Math.round(Math.min(max, Math.max(SIDE_MIN, w)));
      ctlEl.style.setProperty('--stl-side-w', w + 'px'); return w;
    };
    const saveSide = w => { try{ if(w) localStorage.setItem(SIDE_KEY, String(w)); else localStorage.removeItem(SIDE_KEY); }catch(_){} };
    const setPgmRight = on => {
      ctlEl.classList.toggle('is-pgm-right', !!on); $('.stl-pgm-right').checked = !!on;
      if(on){ try{ setSide(parseFloat(localStorage.getItem(SIDE_KEY))); }catch(_){} }
      auxLayout();
    };
    try{ setPgmRight(localStorage.getItem(PGM_KEY) === '1'); }catch(_){}
    $('.stl-pgm-right').onchange = e => { setPgmRight(e.target.checked);
      try{ localStorage.setItem(PGM_KEY, e.target.checked ? '1' : '0'); }catch(_){} };
    sideGrip.addEventListener('pointerdown', ev => {
      ev.preventDefault(); sideGrip.setPointerCapture(ev.pointerId); sideGrip.classList.add('is-drag');
      const x0 = ev.clientX, w0 = $('.stl-side').getBoundingClientRect().width;
      let w = w0;
      const move = e => { w = setSide(w0 - (e.clientX - x0)); };   // vers la gauche = plus large
      const up = () => { sideGrip.classList.remove('is-drag'); saveSide(w);
        sideGrip.removeEventListener('pointermove', move); sideGrip.removeEventListener('pointerup', up); sideGrip.removeEventListener('pointercancel', up); };
      sideGrip.addEventListener('pointermove', move); sideGrip.addEventListener('pointerup', up); sideGrip.addEventListener('pointercancel', up);
    });
    sideGrip.addEventListener('dblclick', () => saveSide(setSide(0)));
    sideGrip.addEventListener('keydown', e => {
      const d = e.key === 'ArrowLeft' ? 40 : e.key === 'ArrowRight' ? -40 : 0; if(!d) return;
      e.preventDefault(); saveSide(setSide($('.stl-side').getBoundingClientRect().width + d * (e.shiftKey ? 4 : 1)));
    });

    // Erreur du conteneur. « fichier introuvable » pour une image que la base dit présente : on demande
    // à l'orchestrateur si le conteneur voit son volume média (démarré avant le montage d'un disque ?).
    async function errToast(r){
      const d = r.detail || r.error || '?';
      const why = d === 'fichier introuvable' && window.MediaLibrary && window.MediaLibrary.explainMissing
        ? await window.MediaLibrary.explainMissing(vmid) : null;
      toast(why || (T('plugin.stills.err_prefix','Erreur : ') + d), 'error');
    }
    // Menus déroulants de la ligne d'options : un seul ouvert à la fois, fermés par un clic ailleurs
    // ou par Échap ; un geste dans le menu (bouton) le referme.
    const pops = [...el.querySelectorAll('.stl-pop')];
    pops.forEach(d => d.addEventListener('toggle', () => { if(d.open) pops.forEach(o => o !== d && (o.open = false)); }));
    const popClose = e => { if(!el.isConnected) return document.removeEventListener('mousedown', popClose, true);
      pops.forEach(d => { if(d.open && !d.contains(e.target)) d.open = false; }); };
    if(this._popClose) document.removeEventListener('mousedown', this._popClose, true);
    this._popClose = popClose; document.addEventListener('mousedown', popClose, true);
    el.querySelectorAll('.stl-pop-body').forEach(b => b.addEventListener('click', e => { if(e.target.closest('button')) b.parentElement.open = false; }));
    el.addEventListener('keydown', e => { if(e.key === 'Escape') pops.forEach(d => d.open = false); });
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
        if(!r.ok) errToast(r);   // success is not announced: the PROGRAMME screen shows it
        setTimeout(refresh, 300);
      }catch(e){ toast(T('plugin.stills.err_network','Erreur réseau'),'error'); }
    }
    async function previewNow(){
      if(!selected) return;
      try{
        const r = await (await api('/preview', params())).json();
        if(!r.ok) errToast(r);
      }catch(e){ toast(T('plugin.stills.err_network','Erreur réseau'),'error'); }
      refresh();
    }
    const choose = () => direct() ? apply() : previewNow();
    async function takeOrCut(path){
      try{
        const r = await (await api(path, {})).json();
        if(r.ok){
          // Les deux images s'échangent : la preview montre désormais l'ancienne image d'antenne.
          if('preview_file' in r){ selected = r.preview_file || ''; adoptPv = true; ml.markCurrent ? ml.markCurrent() : ml.render(); }
        }
        else     errToast(r);
      }catch(e){ toast(T('plugin.stills.err_network','Erreur réseau'),'error'); }
      setTimeout(refresh, 150);
    }

    // Info clamped to two lines by the CSS: the full text stays in the tooltip.
    function setInfo(sel, txt){ const e = $(sel); if(e.textContent !== txt) e.textContent = e.title = txt; }
    const FIT_LBL = {native:T('plugin.stills.fit_native','Pixel à pixel'), fit:T('plugin.stills.fit_fit','Au plus grand'), stretch:T('plugin.stills.fit_stretch','Plein cadre')};
    function infoLine(m){
      if(!m) return '';
      const pos = (m.x||m.y||m.size!==100) ? ` (${m.x},${m.y} · ${m.size}%)` : '';
      return `${m.name} · ${m.src_w}×${m.src_h} · ${m.format||'?'}${m.alpha?' + alpha':''}`
           + (m.rotated ? ` · ${T('plugin.stills.rotated','redressée (EXIF)')}` : '')
           + ` · ${T('plugin.stills.framing','cadrage')} ${FIT_LBL[m.fit]||m.fit}${pos}`;
    }
    // What the operator must know before a TAKE (codes from the container's _framing_warnings). The
    // button refits the preview "Au plus grand" with the same position and scale reset.
    const WARN = {
      cropped:   m => T('plugin.stills.warn_cropped', 'Image rognée : {w}×{h} ne tient pas dans la sortie {ow}×{oh}'),
      upscaled:  m => T('plugin.stills.warn_upscaled', 'Image agrandie au-delà de sa définition ({w}×{h}) : elle sera floue'),
      distorted: m => T('plugin.stills.warn_distorted', 'Image déformée : son rapport largeur/hauteur n\'est pas celui de la sortie'),
    };
    // From the media base (2026-11-22), for a portrait library: rights expired, and the year of an old
    // photo (2 years or more, from the date written in the file, never the copy date).
    function baseWarn(m){
      const a = m && m.file && ml.byPath ? ml.byPath(m.file) : null, md = (a && a.meta) || {}, out = [];
      if(md.rights_until && String(md.rights_until) < new Date().toISOString().slice(0, 10))
        out.push(T('plugin.stills.warn_rights', 'Droits expirés le {d}').replace('{d}', md.rights_until));
      const y = a && a.file_created && a.file_created_src === 'meta' ? parseInt(a.file_created.slice(0, 4), 10) : 0;
      if(y && new Date().getFullYear() - y >= 2) out.push(T('plugin.stills.warn_old', 'Photo de {y}').replace('{y}', y));
      return out;
    }
    function pvWarn(m){
      const box = $('.stl-pv-warn'), codes = ((m && m.warn) || []).filter(c => WARN[c]), extra = baseWarn(m);
      box.hidden = !codes.length && !extra.length; if(box.hidden) return;
      $('.stl-pv-warn-txt').textContent = [...extra, ...codes.map(c => WARN[c](m).replace('{w}', m.src_w).replace('{h}', m.src_h)
        .replace('{ow}', m.out_w).replace('{oh}', m.out_h))].join(' · ');
      $('.stl-pv-fitbtn').hidden = !codes.length || (m.fit === 'fit' && !(m.x || m.y || m.size !== 100));
    }
    $('.stl-pv-fitbtn').onclick = () => { setFit('fit'); $('.stl-x').value = 0; $('.stl-y').value = 0; $('.stl-size').value = 100; previewNow(); };
    // Files dropped from the computer (0.19.25): sent to the base like « Importer depuis l'ordinateur… »,
    // then used where they were dropped — the PREVIEW screen (the last one received goes to preview,
    // never to air) or a grid cell. Until then a desktop file was only taken on the library's Base tab.
    const isFiles = dt => !!dt && [...(dt.types || [])].includes('Files') && ![...dt.types].some(t => t.startsWith('application/x-'));
    const pvScr = $('.stl-pane-pv .stl-screen');
    pvScr.addEventListener('dragover', e => { if(!isFiles(e.dataTransfer)) return; e.preventDefault(); e.dataTransfer.dropEffect = 'copy'; pvScr.classList.add('is-drop'); });
    pvScr.addEventListener('dragleave', e => { if(!pvScr.contains(e.relatedTarget)) pvScr.classList.remove('is-drop'); });
    pvScr.addEventListener('drop', e => {
      pvScr.classList.remove('is-drop'); if(!isFiles(e.dataTransfer)) return;
      e.preventDefault();
      ml.upload(e.dataTransfer.files, r => { selected = r.path; ml.markCurrent ? ml.markCurrent() : ml.render(); previewNow(); });
    });
    function sinceTxt(ts){
      if(!ts) return '';
      const d = Math.max(0, Math.round(Date.now()/1000 - ts)), t = new Date(ts*1000).toLocaleTimeString();
      return ` · ${T('plugin.stills.since','depuis')} ${t} (${d < 90 ? d + ' s' : Math.round(d/60) + ' min'})`;
    }
    // Les JPEG ne sont redemandés que si l'image a changé (gen) ; le conteneur met en cache et
    // n'encode qu'au changement. PENDANT UN FONDU, l'écran PROGRAMME ne montre PAS d'image
    // intermédiaire : relevée toutes les 2 s, elle restait figée à mi-fondu puis sautait — le fondu
    // semblait ne pas marcher (signalé 2026-10-19). L'écran garde l'image d'avant, annonce « fondu en
    // cours », et passe à la nouvelle dès la fin (état relevé toutes les 250 ms tant que ça dure).
    const seen = {fadeT:null};
    function setImg(cls, url, show){
      const im = $('.stl-'+cls+'-img'), em = $('.stl-'+cls+'-empty');
      if(show){ if(im.dataset.u !== url){ im.dataset.u = url; im.src = url; } im.hidden = false; em.hidden = true; }
      else { im.hidden = true; em.hidden = false; delete im.dataset.u; }
    }

    let adoptPv = false, lastPv = null;   // preview changée ailleurs (Take, macro) : la reprendre
    async function refresh(){
      try{
        const s = await (await api('/state')).json();
        const pvf = s.preview_file || '';
        if(lastPv !== null && pvf !== lastPv && pvf !== selected){ selected = pvf; adoptPv = true; ml.markCurrent ? ml.markCurrent() : ml.render(); }
        lastPv = pvf;
        gridTally(s);
        auxRender(s);
        if(adoptPv){
          adoptPv = false; const p = s.preview || {};
          if(pvf){ setFit(p.fit || 'fit'); $('.stl-x').value = p.x ?? 0; $('.stl-y').value = p.y ?? 0; $('.stl-size').value = p.size ?? 100; }
        }
        const df = s.default_file || '';
        $('.stl-default-name').textContent = df ? df.split('/').pop() : '—';
        $('.stl-default-recall').disabled = !df;
        $('.stl-default-set').disabled = !pvf;
        if(!selected){
          selected = pvf || s.file || '';
          const p = s.preview || {};
          if(selected){ setFit(p.fit || s.fit || 'fit');
            $('.stl-x').value = p.x ?? s.x ?? 0; $('.stl-y').value = p.y ?? s.y ?? 0; $('.stl-size').value = p.size ?? s.size ?? 100; ml.render(); }
        }
        const dc = $('.stl-direct'); if(dc && dc !== document.activeElement) dc.checked = !!s.take_direct; syncMode();
        const fe = $('.stl-fade'); if(fe && fe !== document.activeElement) fe.value = Math.round(s.transition_ms || 0);
        { const tt = s.transition_type || 'dissolve', box = $('.stl-tr');
          if(!box.contains(document.activeElement)){
            const r = el.querySelector(`input[name="stl-trtype"][value="${tt}"]`); if(r) r.checked = true;
            $('.stl-wshape').value = s.wipe_direction || 'linear';
            $('.stl-wangle').value = Math.round(s.wipe_angle || 0);
            $('.stl-wsoft').value = Math.round((s.wipe_softness ?? 0.1) * 100);
            $('.stl-wrev').checked = !!s.wipe_reverse;
          }
          $('.stl-wipe-opts').hidden = tt !== 'wipe';
          const nm = {dissolve:T('plugin.stills.tr_dissolve','Fondu'), additive:T('plugin.stills.tr_additive','Fondu additif'),
                      wipe:T('plugin.stills.tr_wipe','Volet')}[tt] || tt;
          $('.stl-tr-sum').textContent = (s.transition_ms ? nm + ' ' + Math.round(s.transition_ms) + ' ms' : T('plugin.stills.tr_cut','Coupe'));
        }
        const kv = $('.stl-viewkey').checked ? 'key' : 'fill', base = `/api/containers/${vmid}/plugin`;
        const pm = s.preview_meta, gm = s.program_meta;
        // Preview. PENDANT UN FONDU, sa vignette n'est pas redemandée : l'encoder (JPEG d'une image
        // pleine définition) prend au conteneur le temps qu'il doit à la publication du fondu.
        if(s.fading && $('.stl-pv-img').dataset.u){ /* reprise à la fin du fondu */ }
        else if(pm && !s.preview_error){
          const g = `${s.preview_gen}-${kv}`;
          setImg('pv', `${base}/preview.jpg?view=${kv}&g=${g}`, true);
          setInfo('.stl-pv-info', infoLine(pm)); pvWarn(pm);
        } else { setImg('pv', '', false); setInfo('.stl-pv-info', s.preview_error || ''); pvWarn(null); }
        // Programme
        clearTimeout(seen.fadeT);
        if(s.fading) seen.fadeT = setTimeout(refresh, 250);
        if(gm){
          if(!s.fading || !$('.stl-pg-img').dataset.u)
            setImg('pg', `${base}/program.jpg?view=${kv}&g=${s.program_gen}-${kv}`, true);
          setInfo('.stl-pg-info', infoLine(gm) + sinceTxt(s.on_air_since) + (s.fading ? ' · ' + T('plugin.stills.fading','fondu en cours') : ''));
        } else { setImg('pg', '', false); setInfo('.stl-pg-info', s.file ? s.file.split('/').pop() : ''); }
        $('.stl-take').disabled = $('.stl-cut').disabled = !pvf;
        outFmt.w = s.width || 0; outFmt.h = s.height || 0;
        status.textContent = `${s.width||0}×${s.height||0} · ${s.fps||0} fps`
          + ` · Fill:${s.fill_shm||'—'} Key:${s.key_shm||'—'} · v${s.plugin_version}`;
      }catch(e){ status.textContent = T('plugin.stills.state_na','état indisponible'); }
    }

    $('.stl-take').onclick = () => takeOrCut('/take');
    $('.stl-cut').onclick  = () => takeOrCut('/cut');
    $('.stl-default-recall').onclick = () => takeOrCut('/default/recall');
    $('.stl-default-set').onclick = async () => {
      try{
        const r = await (await api('/default/set', {from:'preview'})).json();
        if(r.ok) toast(T('plugin.stills.default_done','Image par défaut : ') + (r.default.file||'').split('/').pop(), 'info');
        else     errToast(r);
      }catch(e){ toast(T('plugin.stills.err_network','Erreur réseau'),'error'); }
      refresh();
    };

    // ── Grille (2026-11-14) : des cases figées, plein écran, façon sampleur. Une grille est une FICHE
    // de la base des médias (bins, partage, export) ; chaque utilisateur ouvre les siennes sur ce
    // panneau (retenu côté serveur, d'un poste à l'autre). Une case = une image + son cadrage.
    const escH = v => String(v == null ? '' : v).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    const G = {view:'lib', open:[], names:{}, active:null, grid:null, prep:false, saveT:null, pg:'', pv:''};
    const gwrap = $('.stl-grid-wrap'), gel = $('.stl-grid'), libHost = $('.stl-lib-host'), browse = $('.stl-browse');
    const VIEW_KEY = 'stills-view-' + vmid;
    const KEY_OF = i => i < 9 ? String(i + 1) : i === 9 ? '0' : '';
    async function gcall(method, url, body){
      try{
        const r = await fetch(url, {method, headers:{'Content-Type':'application/json'}, body: body === undefined ? undefined : JSON.stringify(body)});
        const j = await r.json().catch(() => ({}));
        if(!r.ok){ toast(T('plugin.stills.err_prefix','Erreur : ') + (j.error || j.missing_permission || ('HTTP ' + r.status)), 'error'); return null; }
        return j;
      }catch(e){ toast(T('plugin.stills.err_network','Erreur réseau'), 'error'); return null; }
    }
    async function listGrids(){
      const j = await gcall('POST', '/api/media/search', {vmid, query:{kinds:['grid']}});
      const out = ((j || {}).assets || []).sort((a, b) => a.name.localeCompare(b.name));
      out.forEach(a => { G.names[a.id] = a.name; });
      return out;
    }
    const saveOpen = () => gcall('PUT', '/api/media/grids/open', {vmid, open:G.open, active:G.active});
    async function loadGrid(){
      G.grid = null;
      if(G.active){
        const r = await fetch(`/api/media/grids/${encodeURIComponent(G.active)}?vmid=${encodeURIComponent(vmid)}`).catch(() => null);
        if(r && r.ok){ G.grid = await r.json(); G.names[G.grid.id] = G.grid.name; }
        else if(r && r.status === 404){          // supprimée, ou plus visible : on la ferme
          G.open = G.open.filter(x => x !== G.active); G.active = G.open[0] || null; saveOpen();
          if(G.active) return loadGrid();
        }
      }
      renderTabs(); renderGrid();
    }
    async function openGrid(id){
      if(!G.open.includes(id)) G.open.push(id);
      G.active = id; await saveOpen(); await loadGrid();
    }
    function closeGrid(id){
      G.open = G.open.filter(x => x !== id);
      if(G.active === id) G.active = G.open[0] || null;
      saveOpen(); loadGrid();
    }
    function renderTabs(){
      const box = $('.stl-gtabs');
      box.innerHTML = G.open.map(id => `<button type="button" role="tab" data-gid="${escH(id)}" aria-selected="${id === G.active}">${escH(G.names[id] || '…')}</button>`).join('');
      box.querySelectorAll('[data-gid]').forEach(b => b.onclick = () => { G.active = b.dataset.gid; saveOpen(); loadGrid(); });
      const g = G.grid, opt = (n, lo, hi) => Array.from({length:hi - lo + 1}, (_, k) => `<option${k + lo === n ? ' selected' : ''}>${k + lo}</option>`).join('');
      $('.stl-gcols').innerHTML = opt(g ? g.cols : 6, 1, 10);
      $('.stl-grows').innerHTML = opt(g ? g.rows : 4, 1, 8);
      $('.stl-gsize').hidden = !(G.prep && g);
      $('.stl-gmore').hidden = !g;
    }
    function cellName(c){ return c.label || c.name || (c.file || '').split('/').pop() || '?'; }
    function renderGrid(){
      const g = G.grid;
      $('.stl-gempty').hidden = !!g;
      $('.stl-ghint').hidden = !(g && G.prep);
      gel.hidden = !g;
      if(!g){ gel.innerHTML = ''; return; }
      gel.style.setProperty('--stl-gcols', g.cols); gel.style.setProperty('--stl-grows', g.rows);
      gel.innerHTML = g.cells.map((c, i) => {
        const k = KEY_OF(i), kb = k ? `<kbd title="${escH(T('plugin.stills.numpad','Pavé numérique'))} ${k}">${k}</kbd>` : '';
        if(!c) return `<button type="button" class="ctl-push ctl-push--tile stl-cell is-empty" data-cell="${i}"${G.prep ? '' : ' disabled'}>${i + 1}${G.prep ? '' : ' · ' + escH(T('plugin.stills.gcell_empty','vide'))}</button>`;
        if(c.hidden) return `<button type="button" class="ctl-push ctl-push--tile stl-cell is-missing" data-cell="${i}" disabled>${escH(T('plugin.stills.gcell_hidden','image hors de votre portée'))}</button>`;
        const thumb = c.asset ? `/api/media/assets/${encodeURIComponent(c.asset)}/thumb` : '';
        const nm = cellName(c) + (c.missing ? ' — ' + T('plugin.stills.gcell_missing','introuvable') : '');
        return `<button type="button" class="ctl-push ctl-push--tile stl-cell${c.missing ? ' is-missing' : ''}" data-cell="${i}" aria-pressed="false" draggable="true"
          aria-label="${escH(T('plugin.stills.gcell_aria','Case {n} : {name}').replace('{n}', i + 1).replace('{name}', nm))}">
          ${thumb ? `<img src="${thumb}" alt="" loading="lazy" decoding="async">` : '<span></span>'}
          <span class="ctl-push__foot"><span>${escH(nm)}</span><span class="ctl-flag ctl-flag--1">PVW</span><span class="ctl-flag ctl-flag--2">PGM</span>${kb}</span>
        </button>`;
      }).join('');
      gel.style.setProperty('--stl-grid-top', Math.round(gel.getBoundingClientRect().top + window.scrollY + 16) + 'px');
      paintTally();
    }
    const cellPath = c => c && !c.hidden ? (c.local_path || (!c.asset ? c.file : '') || '') : '';
    function paintTally(){
      if(!G.grid) return;
      gel.querySelectorAll('[data-cell]').forEach(b => {
        const p = cellPath(G.grid.cells[+b.dataset.cell]);
        b.classList.toggle('on2', !!p && p === G.pg);
        if(b.hasAttribute('aria-pressed')) b.setAttribute('aria-pressed', String(!!p && p === G.pv));
      });
    }
    function gridTally(s){ G.pg = s.file || ''; G.pv = s.preview_file || ''; paintTally(); }

    async function playCell(i){
      const g = G.grid, c = g && g.cells[i];
      if(!c || c.hidden) return;
      if(c.missing){ toast(T('plugin.stills.gcell_missing_t','Cette image n\'est plus dans la base'), 'error'); return; }
      let path = cellPath(c);
      if(!path && c.asset){
        if(!(c.available_nodes || []).length){ toast(T('plugin.stills.gcell_nocopy','Aucune copie de cette image à rapatrier'), 'error'); return; }
        path = await ml.fetchAsset(c.asset);
        if(path){ c.local_path = path; loadGrid(); }
      }
      if(!path) return;
      selected = path;
      setFit(c.fit || 'fit'); $('.stl-x').value = c.x || 0; $('.stl-y').value = c.y || 0; $('.stl-size').value = c.size || 100;
      if(ml.markCurrent) ml.markCurrent();
      choose();
    }
    function gridStep(d){
      const g = G.grid; if(!g) return false;
      const filled = g.cells.map((c, i) => cellPath(c) ? i : -1).filter(i => i >= 0);
      const cur = filled.findIndex(i => cellPath(g.cells[i]) === selected);
      const n = cur < 0 ? (d > 0 ? 0 : filled.length - 1) : cur + d;
      if(n < 0 || n >= filled.length) return false;
      playCell(filled[n]); return true;
    }

    // ── Préparation : chaque geste modifie la fiche, enregistrée aussitôt (par lot de 600 ms).
    const bare = c => c ? {asset:c.asset || null, file:c.file || '', label:c.label || '', fit:c.fit || 'fit', x:c.x || 0, y:c.y || 0, size:c.size || 100} : null;
    function changed(){
      renderGrid();
      clearTimeout(G.saveT);
      const g = G.grid;
      G.saveT = setTimeout(async () => {
        const ok = await gcall('PUT', '/api/media/grids/' + encodeURIComponent(g.id), {doc:{cols:g.cols, rows:g.rows, cells:g.cells.map(bare)}});
        if(ok && G.grid && G.grid.id === g.id) loadGrid();        // noms et copies locales des nouvelles cases
      }, 600);
    }
    const frameNow = () => ({fit:fit(), x:xv(), y:yv(), size:sv()});
    function place(ids, i){
      const g = G.grid; if(!g || !ids.length) return;
      g.cells[i] = Object.assign({asset:ids[0]}, frameNow());
      let k = i + 1;
      for(const id of ids.slice(1)){
        while(k < g.cells.length && g.cells[k]) k++;
        if(k >= g.cells.length){ toast(T('plugin.stills.gfull','Plus de case vide : le reste n\'est pas placé'), 'warning'); break; }
        g.cells[k++] = Object.assign({asset:id}, frameNow());
      }
      changed();
    }
    function resize(cols, rows){
      const g = G.grid, out = new Array(cols * rows).fill(null), lost = [];
      g.cells.forEach((c, i) => { if(!c) return; const r = Math.floor(i / g.cols), q = i % g.cols;
        if(r < rows && q < cols) out[r * cols + q] = c; else lost.push(i + 1); });
      if(lost.length){ toast(T('plugin.stills.gshrink','Videz d\'abord les cases {n} : elles sortiraient de la grille').replace('{n}', lost.join(', ')), 'warning'); renderTabs(); return; }
      g.cols = cols; g.rows = rows; g.cells = out; changed();
    }
    $('.stl-gcols').onchange = e => resize(+e.target.value, G.grid.rows);
    $('.stl-grows').onchange = e => resize(G.grid.cols, +e.target.value);

    const dlg = $('.stl-gdlg');
    function askName(title, value){
      return new Promise(ok => {
        $('.stl-gdlg-title').textContent = title; const inp = $('.stl-gdlg-name'); inp.value = value || '';
        const done = v => { dlg.onclose = null; ok(v); };
        $('.stl-gdlg-cancel').onclick = () => { dlg.close(); done(null); };
        dlg.querySelector('form').onsubmit = () => { const v = inp.value.trim(); setTimeout(() => done(v || null)); };
        dlg.onclose = () => done(null);
        dlg.showModal(); inp.focus(); inp.select();
      });
    }
    async function newGrid(){
      const name = await askName(T('plugin.stills.gnew_title','Nom de la nouvelle grille'), '');
      if(!name) return;
      const j = await gcall('POST', '/api/media/grids', {name, vmid, doc:{cols:6, rows:4}});
      if(!j) return;
      G.names[j.id] = name; G.prep = true; await openGrid(j.id); setView('grid');
    }
    // Imported share files into NEW grids, each sized to its images: one grid, or one per subfolder
    // (« Mariage », « Mariage / Soirée »…). A grid holds 80 cells (10 × 8): beyond, « Nom (2) »… The
    // first grid made is the one shown.
    const GRID_CELLS = 80;
    async function gridsFromImport(name, rs, perDir){
      const groups = new Map();
      rs.forEach(r => { const k = perDir ? (r.dir || '') : ''; (groups.get(k) || groups.set(k, []).get(k)).push(r); });
      let first = null;
      for(const [dir, list] of groups){
        const base = dir ? name + ' / ' + dir.split('/').join(' / ') : name;
        for(let i = 0; i < list.length; i += GRID_CELLS){
          const id = await gridFromImport(i ? `${base} (${i / GRID_CELLS + 1})` : base, list.slice(i, i + GRID_CELLS), null, true);
          if(id && !first) first = id;
        }
      }
      if(first && G.active !== first) await openGrid(first);
      setView('grid');
    }
    // Imported share files into a grid: a new one sized to them (at most 10 × 8), or the open one from
    // its first empty cell. The cells take the current framing, like a drop from the library.
    async function gridFromImport(name, rs, gid, quiet){
      const ids = rs.map(r => r.asset && r.asset.id).filter(Boolean); if(!ids.length) return null;
      if(gid == null){
        const n = Math.min(ids.length, GRID_CELLS);
        let cols = Math.min(10, Math.max(1, Math.ceil(Math.sqrt(n * 1.6))));
        const rows = Math.min(8, Math.ceil(n / cols)); if(cols * rows < n) cols = 10;
        const j = await gcall('POST', '/api/media/grids', {name, vmid, doc:{cols, rows, cells:ids.slice(0, n).map(id => Object.assign({asset:id}, frameNow()))}});
        if(!j) return null;
        G.names[j.id] = name; if(!G.open.includes(j.id)) G.open.push(j.id);
        if(quiet){ await saveOpen(); return j.id; }
        await openGrid(j.id); setView('grid'); return j.id;
      } else if(G.active !== gid) await openGrid(gid);
      const g = G.grid; if(!g) return;
      const k = g.cells.findIndex(c => !c);
      if(k < 0){ toast(T('plugin.stills.gfull','Plus de case vide : le reste n\'est pas placé'), 'warning'); return; }
      place(ids, k); setView('grid');
      return gid;
    }
    async function renameGrid(){
      const g = G.grid; if(!g) return;
      const name = await askName(T('plugin.stills.grename_title','Nouveau nom de la grille'), g.name);
      if(!name || name === g.name) return;
      if(await gcall('PUT', '/api/media/grids/' + encodeURIComponent(g.id), {doc:{cols:g.cols, rows:g.rows, cells:g.cells.map(bare)}, name})){
        G.names[g.id] = name; loadGrid(); }
    }
    const imp = $('.stl-gimport');
    imp.onchange = async () => {
      const f = imp.files && imp.files[0]; if(!f) return;
      let data; try{ data = JSON.parse(await f.text()); }catch(_){ toast(T('plugin.stills.gimport_bad','Fichier de grille illisible'), 'error'); return; }
      const j = await gcall('POST', '/api/media/grids/import', {data, vmid});
      if(!j) return;
      const r = j.report || {};
      toast(T('plugin.stills.gimported','Grille importée : {n} case(s), {u} image(s) introuvable(s)').replace('{n}', r.items || 0).replace('{u}', r.unresolved || 0), r.unresolved ? 'warning' : 'info');
      await openGrid(j.id);
    };
    $('.stl-gopen').onclick = async e => {
      const anchor = e.currentTarget, all = await listGrids();
      ml.openMenu(anchor, [
        {label:T('plugin.stills.gnew','Nouvelle grille…'), run:newGrid},
        {label:T('plugin.stills.gimport','Importer un fichier de grille…'), run:() => { imp.value = ''; imp.click(); }},
        {header: all.length ? T('plugin.stills.gopen_h','Ouvrir') : T('plugin.stills.gnone','Aucune grille pour l\'instant')},
        ...all.map(a => ({label:(G.open.includes(a.id) ? '✓ ' : '') + a.name, run:() => openGrid(a.id)})),
      ]);
    };
    $('.stl-gmore').onclick = e => {
      const g = G.grid; if(!g) return;
      ml.openMenu(e.currentTarget, [
        {label:T('plugin.stills.grename','Renommer…'), run:renameGrid},
        {label:T('plugin.stills.gexport','Exporter…'), run:() => { location.href = '/api/media/grids/' + encodeURIComponent(g.id) + '/export'; }},
        {label:T('plugin.stills.gclose','Fermer l\'onglet'), run:() => closeGrid(g.id)},
      ]);
    };
    $('.stl-gprep').onclick = () => { G.prep = !G.prep; $('.stl-gprep').setAttribute('aria-pressed', String(G.prep)); setView('grid'); renderTabs(); };
    function cellMenu(btn, i){
      const g = G.grid, c = g.cells[i];
      const items = [];
      if(c){
        items.push({label:T('plugin.stills.gcell_frame','Cadrage actuel pour cette case'), run:() => { Object.assign(c, frameNow()); changed(); }});
        items.push({label:T('plugin.stills.gcell_label','Nommer la case…'), run:async () => {
          const v = await askName(T('plugin.stills.gcell_label_t','Nom affiché sur la case (vide : nom de l\'image)'), c.label || '');
          if(v !== null){ c.label = v; changed(); } }});
        items.push({label:T('plugin.stills.gcell_clear','Vider la case'), run:() => { g.cells[i] = null; changed(); }});
      } else items.push({header:T('plugin.stills.gcell_pick_hint','Choisissez une image dans la bibliothèque, puis touchez la case')});
      ml.openMenu(btn, items);
    }
    gel.addEventListener('click', e => {
      const b = e.target.closest('[data-cell]'); if(!b || b.disabled) return;
      const i = +b.dataset.cell;
      if(!G.prep) return playCell(i);
      // Préparation au doigt : une image choisie dans la bibliothèque, puis la case.
      const sel = (ml.selected ? ml.selected() : []).filter(r => r.asset && (r.asset.tech || {}).kind !== 'grid').map(r => r.asset.id);
      if(sel.length){ place(sel, i); if(ml.clearSelection) ml.clearSelection(); return; }
      cellMenu(b, i);
    });
    gel.addEventListener('dragstart', e => {
      const b = e.target.closest('[data-cell]'); if(!b) return;          // vers une autre case (préparation) ou une sortie AUX
      e.dataTransfer.setData('application/x-stills-cell', b.dataset.cell); e.dataTransfer.effectAllowed = 'copyMove';
    });
    const accepts = dt => isFiles(dt) || [...(dt.types || [])].some(t => t === 'application/x-stills-cell' || t === 'application/x-bobi-assets' || t === 'application/x-bobi-asset');
    gel.addEventListener('dragover', e => {
      const b = e.target.closest('[data-cell]'); if(!b || !G.prep || !accepts(e.dataTransfer)) return;
      e.preventDefault(); gel.querySelectorAll('.is-drop').forEach(x => x !== b && x.classList.remove('is-drop')); b.classList.add('is-drop');
    });
    gel.addEventListener('dragleave', e => { const b = e.target.closest('[data-cell]'); if(b && !b.contains(e.relatedTarget)) b.classList.remove('is-drop'); });
    gel.addEventListener('drop', e => {
      const b = e.target.closest('[data-cell]'); if(!b || !G.prep) return;
      e.preventDefault(); b.classList.remove('is-drop');
      const i = +b.dataset.cell, dt = e.dataTransfer, from = dt.getData('application/x-stills-cell');
      if(isFiles(dt)){
        // First file into this cell, the next ones into the following empty cells, as they arrive.
        const g = G.grid; let first = true;
        ml.upload(dt.files, r => {
          if(G.grid !== g) return;
          if(first){ first = false; place([r.asset.id], i); return; }
          const k = g.cells.findIndex((c, j) => j > i && !c);
          if(k < 0){ toast(T('plugin.stills.gfull','Plus de case vide : le reste n\'est pas placé'), 'warning'); return; }
          place([r.asset.id], k);
        });
        return;
      }
      if(from !== ''){ const j = +from, g = G.grid; if(j !== i){ [g.cells[i], g.cells[j]] = [g.cells[j], g.cells[i]]; changed(); } return; }
      let ids = []; try{ ids = JSON.parse(dt.getData('application/x-bobi-assets') || '[]'); }catch(_){}
      if(!ids.length && dt.getData('application/x-bobi-asset')) ids = [dt.getData('application/x-bobi-asset')];
      place(ids, i);
    });

    function setView(v){
      G.view = v; try{ localStorage.setItem(VIEW_KEY, v); }catch(_){}
      const grid = v === 'grid';
      $('.stl-view-lib').setAttribute('aria-selected', String(!grid)); $('.stl-view-grid').setAttribute('aria-selected', String(grid));
      gwrap.hidden = !grid; $('.stl-gtabs').hidden = !grid; $('.stl-gtools').hidden = !grid;
      // Repliée, pas masquée : son menu (Grilles ▾, menu d'une case) et sa barre de rapatriement
      // vivent dans la bibliothèque — masquée, ils s'ouvraient invisibles (signalé le 2026-11-15).
      libHost.classList.toggle('is-folded', grid && !G.prep);
      browse.classList.toggle('is-prep', grid && G.prep);
      if(grid){ renderTabs(); renderGrid(); }
    }
    $('.stl-view-lib').onclick = () => setView('lib');
    $('.stl-view-grid').onclick = () => setView('grid');
    (async () => {
      let v = 'lib'; try{ v = localStorage.getItem(VIEW_KEY) === 'grid' ? 'grid' : 'lib'; }catch(_){}
      // Cœur sans les grilles (antérieur à elles) : pas d'onglet Grille, plutôt qu'un onglet en erreur.
      const r = await fetch(`/api/media/grids/open?vmid=${encodeURIComponent(vmid)}`).catch(() => null);
      if(!r || r.status === 404){ $('.stl-view').hidden = true; setView('lib'); return; }
      const j = r.ok ? await r.json().catch(() => null) : null;
      if(j){ G.open = j.open || []; G.active = j.active || null; }
      if(G.open.length) await listGrids();
      setView(v); await loadGrid();
    })();

    // ── Sorties AUX (2026-11-14) : 0 à 4 selon le réglage du conteneur, en petit dans le coin. Chacune
    // garde l'image qu'on y dépose (bibliothèque, case de grille, ou la preview) — coupe franche.
    const AX = {n:-1, gen:[]};
    const auxBox = $('.stl-aux');
    async function auxPost(n, body){
      try{
        const r = await (await api('/aux', Object.assign({n}, body))).json();
        if(!r.ok) errToast(r);
      }catch(e){ toast(T('plugin.stills.err_network','Erreur réseau'),'error'); }
      setTimeout(refresh, 150);
    }
    function auxRender(s){
      const n = s.aux_count || 0, kv = $('.stl-viewkey').checked ? 'key' : 'fill';
      if(n !== AX.n){
        AX.n = n; AX.gen = [];
        auxBox.hidden = !n;
        // Same design as PREVIEW / PROGRAMME (2026-11-26): tag above, framed screen below.
        auxBox.innerHTML = Array.from({length:n}, (_, i) => `<figure class="stl-pane stl-aux-out" data-aux="${i + 1}">
            <figcaption><span class="stl-tag stl-tag-aux">AUX ${i + 1}</span><span class="stl-info stl-aux-info"></span>
              <button type="button" data-aux-pv title="${escH(T('plugin.stills.aux_from_pv','Mettre l\'image de la preview sur cette sortie'))}">${escH(T('plugin.stills.aux_pv','PVW'))}</button>
              <button type="button" data-aux-clear aria-label="${escH(T('plugin.stills.aux_clear','Vider la sortie (noir)'))}">✕</button>
            </figcaption>
            <div class="stl-screen stl-aux-scr"><img alt="" hidden><span class="stl-empty">${escH(T('plugin.stills.aux_drop','Glisser une image ici'))}</span></div></figure>`).join('');
        auxLayout();
      }
      (s.aux || []).forEach((fr, i) => {
        const fig = auxBox.querySelector(`[data-aux="${i + 1}"]`); if(!fig) return;
        const img = fig.querySelector('img'), hint = fig.querySelector('.stl-aux-scr > span'), g = (s.aux_gen || [])[i] + '-' + kv;
        if(fr.file){ if(AX.gen[i] !== g){ AX.gen[i] = g; img.src = `/api/containers/${vmid}/plugin/aux.jpg?n=${i + 1}&view=${kv}&g=${g}`; } img.hidden = false; hint.hidden = true; }
        else { img.hidden = true; hint.hidden = false; AX.gen[i] = null; }
        const err = (s.aux_error || [])[i];
        { const inf = fig.querySelector('.stl-aux-info'); inf.textContent = inf.title = err ? '⚠ ' + err : (fr.file ? fr.file.split('/').pop() : ''); }
        fig.querySelector('[data-aux-clear]').disabled = !fr.file;
        fig.title = (s.aux_shm || [])[i] || '';
      });
    }
    auxBox.addEventListener('click', e => {
      const fig = e.target.closest('[data-aux]'); if(!fig) return;
      const n = +fig.dataset.aux;
      if(e.target.closest('[data-aux-pv]')) auxPost(n, {from:'preview'});
      else if(e.target.closest('[data-aux-clear]')) auxPost(n, {file:''});
    });
    const auxAccepts = dt => [...(dt.types || [])].some(t => ['application/x-stills-cell', 'application/x-bobi-media', 'application/x-bobi-asset'].includes(t));
    auxBox.addEventListener('dragover', e => {
      const fig = e.target.closest('[data-aux]'); if(!fig || !auxAccepts(e.dataTransfer)) return;
      e.preventDefault(); auxBox.querySelectorAll('.is-drop').forEach(x => x !== fig && x.classList.remove('is-drop')); fig.classList.add('is-drop');
    });
    auxBox.addEventListener('dragleave', e => { const fig = e.target.closest('[data-aux]'); if(fig && !fig.contains(e.relatedTarget)) fig.classList.remove('is-drop'); });
    auxBox.addEventListener('drop', async e => {
      const fig = e.target.closest('[data-aux]'); if(!fig) return;
      e.preventDefault(); fig.classList.remove('is-drop');
      const n = +fig.dataset.aux, dt = e.dataTransfer, cell = dt.getData('application/x-stills-cell');
      if(cell !== '' && G.grid){                         // une case de grille : son image ET son cadrage
        const c = G.grid.cells[+cell]; if(!c) return;
        let p = cellPath(c);
        if(!p && c.asset) p = await ml.fetchAsset(c.asset);
        if(p) auxPost(n, {file:p, fit:c.fit, x:c.x, y:c.y, size:c.size});
        return;
      }
      let p = dt.getData('application/x-bobi-media');    // une image de la bibliothèque : le cadrage réglé
      const aid = dt.getData('application/x-bobi-asset');
      if(!p && aid) p = await ml.fetchAsset(aid);
      if(p) auxPost(n, Object.assign({file:p}, frameNow()));
    });

    // Raccourcis clavier : / = recherche, Espace = TAKE, Entrée = CUT, ← → = image précédente / suivante (ordre de la
    // bibliothèque affichée), D = rappel de l'image par défaut. Actifs tant que ce panneau est affiché,
    // jamais pendant une saisie, dans une fenêtre ouverte, ni avec Ctrl / Alt / Cmd.
    const onKey = e => {
      if(!keysOn || e.defaultPrevented || e.ctrlKey || e.altKey || e.metaKey || !el.isConnected || !el.offsetParent) return;
      const t = e.target;
      if(t && t.closest && (t.closest('input,textarea,select,[contenteditable="true"],dialog') || (t !== document.body && !el.contains(t) && t.closest('[role="dialog"],.modal')))) return;
      if(document.querySelector('dialog[open]')) return;
      const k = e.key, go = f => { e.preventDefault(); f(); };
      // « / » : the library search (2026-11-22). Grid view folds the library: back to it first.
      if(k === '/') return go(() => { if(G.view === 'grid' && !G.prep) setView('lib'); ml.focusSearch && ml.focusSearch(); });
      // Vue Grille : pavé numérique 1 à 9 et 0 = cases 1 à 10 ; flèches = case voisine.
      if(G.view === 'grid' && !G.prep){
        const m = /^Numpad([0-9])$/.exec(e.code || '');
        if(m) return go(() => { const d = +m[1]; playCell(d === 0 ? 9 : d - 1); });
        if(k === 'ArrowRight' || k === 'ArrowLeft') return go(() => {
          if(!gridStep(k === 'ArrowRight' ? 1 : -1)) toast(T('plugin.stills.step_edge','Plus d\'image dans ce sens'), 'info'); });
      }
      if(k === ' ') go(() => { if(!$('.stl-take').disabled) takeOrCut('/take'); });
      else if(k === 'Enter') go(() => { if(!$('.stl-cut').disabled) takeOrCut('/cut'); });
      else if(k === 'ArrowRight' || k === 'ArrowLeft') go(() => {
        if(!ml.step(k === 'ArrowRight' ? 1 : -1, 'show')) toast(T('plugin.stills.step_edge','Plus d\'image dans ce sens'), 'info'); });
      else if(k === 'd' || k === 'D') go(() => { if(!$('.stl-default-recall').disabled) takeOrCut('/default/recall'); });
    };
    // On / off: preference of this browser, like the screen layout. Off also hides the key badges.
    const KEYS_KEY = 'stills-keys';
    let keysOn = true;
    try{ keysOn = localStorage.getItem(KEYS_KEY) !== '0'; }catch(_){}
    const setKeys = on => { keysOn = !!on; $('.stl-keys').checked = keysOn; ctlEl.classList.toggle('is-nokeys', !keysOn); };
    setKeys(keysOn);
    $('.stl-keys').onchange = e => { setKeys(e.target.checked); try{ localStorage.setItem(KEYS_KEY, keysOn ? '1' : '0'); }catch(_){} };
    if(this._onKey) document.removeEventListener('keydown', this._onKey);
    this._onKey = onKey;
    document.addEventListener('keydown', onKey);
    $('.stl-direct').onchange = async () => {
      await api('/take_direct', {enabled: direct()}); syncMode();
      if(direct() && selected) apply();   // bascule en direct : ce qui est choisi part aussitôt
    };
    $('.stl-viewkey').onchange = refresh;
    // Durée de fondu par défaut, à chaud. Elle ne s'applique qu'aux CHANGEMENTS d'image : régler
    // la position reste une coupe (règle côté conteneur).
    const fadeEl = $('.stl-fade');
    // Transition (0.19.18) : durée, type (fondu, additif, volet) et réglage du volet — les formes du
    // mélangeur. Tout part sur /transition (persisté par l'orchestrateur).
    const trType = () => { const r = el.querySelector('input[name="stl-trtype"]:checked'); return r ? r.value : 'dissolve'; };
    const sendTransition = () => api('/transition', {
      ms: Math.max(0, Math.min(10000, parseFloat(fadeEl.value) || 0)), type: trType(),
      direction: $('.stl-wshape').value, angle: parseFloat($('.stl-wangle').value) || 0,
      softness: Math.max(0, Math.min(100, parseFloat($('.stl-wsoft').value) || 0)) / 100,
      reverse: $('.stl-wrev').checked}).then(() => refresh());
    [fadeEl, $('.stl-wshape'), $('.stl-wangle'), $('.stl-wsoft'), $('.stl-wrev'),
     ...el.querySelectorAll('input[name="stl-trtype"]')].forEach(x => x && (x.onchange = sendTransition));
    el.querySelectorAll('input[name="stl-fit"]').forEach(r => r.onchange = choose);
    ['.stl-x','.stl-y','.stl-size'].forEach(s => { const e = $(s); if(e) e.onchange = choose; });

    // Position et taille : une fenêtre ouverte depuis le menu « Cadrage et fondu » (2026-11-15) —
    // les réglages du bas de page sont montés en menus, la bibliothèque est désormais tout en bas.
    const posDlg = $('.stl-posdlg');
    $('.stl-pos-open').onclick = () => { if(!posDlg.open) posDlg.showModal(); };

    $('.stl-monitor').onclick = () => {
      const hn = inst.hostname || '';
      if(!hn){ toast('Hostname inconnu','warning'); return; }
      if(window.MXLMonitor){ window.MXLMonitor.send(hn, hn + ' (Fill)'); if(window.MXLMonitor.toggle) window.MXLMonitor.toggle(); }
      else toast('Moniteur indisponible','warning');
    };

    if(this._timer) clearInterval(this._timer);
    if(this._seen) clearTimeout(this._seen.fadeT);
    refresh();
    this._timer = setInterval(refresh, 2000);
    this._seen = seen;
  }
};
