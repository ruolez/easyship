/* Multi-select filters (Parcels, Reports): a compact button recalls a
   checkbox popover; empty selection means "All". Needs esc() from api.js. */
const FM_CARET = '<svg class="fm-caret" viewBox="0 0 24 24"><polyline points="6 9 12 15 18 9"/></svg>';

let openFilter = null;
function closeFilterMenu() {
  if (!openFilter) return;
  openFilter.menu.remove();
  openFilter.btn.classList.remove('open');
  openFilter = null;
}

function multiFilter(id, { options = [], onChange, onOpen }) {
  const btn = document.getElementById(id);
  const normalize = (list) => list.map((o) => (typeof o === 'string' ? { value: o, label: o } : o));
  let opts = normalize(options);
  const selected = new Set();

  function updateBtn() {
    let text = 'All';
    if (selected.size === 1) {
      const v = selected.values().next().value;
      text = (opts.find((o) => o.value === v) || { label: v }).label;
    } else if (selected.size > 1) text = `${selected.size} selected`;
    btn.innerHTML = `<span class="fm-label">${esc(text)}</span>${FM_CARET}`;
    btn.classList.toggle('filtered', selected.size > 0);
  }

  function renderMenu(menu) {
    menu.innerHTML = `
      <button class="filter-menu-item${selected.size ? '' : ' checked'}" data-all="1"><span class="fm-check"></span><span>All</span></button>
      <div class="row-menu-sep"></div>
      ${opts.map((o) => `<button class="filter-menu-item${selected.has(o.value) ? ' checked' : ''}" data-v="${esc(o.value)}"><span class="fm-check"></span><span>${esc(o.label)}</span></button>`).join('')}`;
  }

  btn.addEventListener('click', () => {
    if (onOpen) onOpen();
    if (openFilter && openFilter.btn === btn) { closeFilterMenu(); return; }
    closeFilterMenu();
    const menu = document.createElement('div');
    menu.className = 'row-menu filter-menu';
    menu.setAttribute('role', 'menu');
    renderMenu(menu);
    menu.addEventListener('click', (ev) => {
      // The re-render below detaches ev.target, so the document-level
      // outside-click check would no longer see it inside the menu.
      ev.stopPropagation();
      const item = ev.target.closest('.filter-menu-item');
      if (!item) return;
      if (item.dataset.all) selected.clear();
      else if (selected.has(item.dataset.v)) selected.delete(item.dataset.v);
      else selected.add(item.dataset.v);
      renderMenu(menu);
      updateBtn();
      onChange();
    });
    document.body.appendChild(menu);
    const r = btn.getBoundingClientRect();
    const left = Math.max(8, Math.min(r.left, window.innerWidth - menu.offsetWidth - 8));
    let top = r.bottom + 4;
    if (top + menu.offsetHeight > window.innerHeight - 8) top = r.top - menu.offsetHeight - 4;
    menu.style.left = `${left}px`;
    menu.style.top = `${top}px`;
    btn.classList.add('open');
    openFilter = { btn, menu };
  });

  updateBtn();
  return {
    get values() { return selected; },
    setValues(list) {
      selected.clear();
      (list || []).forEach((v) => selected.add(v));
      updateBtn();
    },
    setOptions(list) {
      opts = normalize(list);
      [...selected].forEach((v) => { if (!opts.some((o) => o.value === v)) selected.delete(v); });
      updateBtn();
    },
  };
}

document.addEventListener('click', (e) => {
  if (!e.target.closest('.filter-multi') && !e.target.closest('.filter-menu')) closeFilterMenu();
});
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') closeFilterMenu();
});
document.addEventListener('scroll', (e) => {
  if (openFilter && !openFilter.menu.contains(e.target)) closeFilterMenu();
}, true);
window.addEventListener('resize', () => closeFilterMenu());
