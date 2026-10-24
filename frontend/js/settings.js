// Non-provider settings. Provider fields (tokens, mode, enabled, custom
// fields) are appended after the provider cards render, so new platforms need
// no edits here.
const BASE_SETTING_IDS = [
  'origin_company', 'origin_contact', 'origin_address1', 'origin_address2',
  'origin_city', 'origin_state', 'origin_zip', 'origin_phone', 'origin_email',
  'placeholder_email', 'print_mode', 'printer_host', 'printer_port', 'printer_dpi',
  'label_timeout_seconds', 'countdown_seconds', 'order_tag_rules',
  'profit_min_amount', 'profit_min_margin_pct', 'profit_bypass_password',
  'shipper_host', 'shipper_port', 'shipper_db', 'shipper_user', 'shipper_password',
  'picklist_backoffice_db_id', 'picklist_quote_status', 'picklist_title_prefix',
  'picklist_number_tag', 'picklist_rules', 'picklist_presets',
];
let SETTING_IDS = [...BASE_SETTING_IDS];

initNav('settings').then(async () => {
  const isAdmin = window.currentUser && window.currentUser.role === 'admin';
  initTabs(isAdmin);
  if (!isAdmin) return;
  await renderProviders();
  await loadPicklistOptions();
  await loadSettings();
  updatePrintModeUI();
  renderTagRules();
  loadServiceNames();
  watchDirty();
  await loadStores();
  await loadDbs();
  await loadBoxes();
  await loadUsers();
  await loadAudit();
});

/* ---------- Tabs ---------- */
const ADMIN_TABS = ['providers', 'shipping', 'printing', 'boxes', 'integrations', 'picklists', 'users', 'audit'];

function initTabs(isAdmin) {
  const buttons = [...document.querySelectorAll('#settings-tabs .tab')];
  buttons.forEach((b) => {
    if (!isAdmin && ADMIN_TABS.includes(b.dataset.tab)) b.style.display = 'none';
    b.addEventListener('click', () => showTab(b.dataset.tab));
  });
  const allowed = buttons.map((b) => b.dataset.tab)
    .filter((t) => isAdmin || !ADMIN_TABS.includes(t));
  const fromHash = location.hash.replace('#', '');
  showTab(allowed.includes(fromHash) ? fromHash : allowed[0]);
}

function showTab(tab) {
  document.querySelectorAll('#settings-tabs .tab')
    .forEach((b) => b.classList.toggle('active', b.dataset.tab === tab));
  document.querySelectorAll('.tab-panel')
    .forEach((p) => p.classList.toggle('active', p.dataset.panel === tab));
  if (location.hash !== `#${tab}`) history.replaceState(null, '', `#${tab}`);
}

/* ---------- Unsaved-changes save bar ---------- */
let dirty = false;

function setDirty(v) {
  dirty = v;
  document.getElementById('savebar').classList.toggle('show', v);
}

function watchDirty() {
  SETTING_IDS.forEach((id) => {
    const el = document.getElementById(id);
    if (!el || el.dataset.dirtyWired) return;
    el.dataset.dirtyWired = '1';
    el.addEventListener('input', () => setDirty(true));
    el.addEventListener('change', () => setDirty(true));
  });
}

window.addEventListener('beforeunload', (e) => {
  if (dirty) e.preventDefault();
});

document.getElementById('discard-settings').addEventListener('click', async () => {
  await loadSettings();
  updatePrintModeUI();
  renderTagRules();
  setDirty(false);
});

/* ---------- Shopify order tag rules ----------
   Stored as JSON in the hidden #order_tag_rules input so they ride along with
   the regular Save / Discard flow. */
const SIGNATURE_OPTIONS = [
  ['none', 'No signature requirement'],
  ['signature', 'Signature required'],
  ['adult', 'Adult signature (21+)'],
];

function readTagRules() {
  try {
    const rules = JSON.parse(document.getElementById('order_tag_rules').value || '[]');
    return Array.isArray(rules) ? rules : [];
  } catch {
    return [];
  }
}

function writeTagRules(rules) {
  document.getElementById('order_tag_rules').value = JSON.stringify(rules);
  setDirty(true);
}

function renderTagRules() {
  const rules = readTagRules();
  const list = document.getElementById('tag-rules-list');
  if (!rules.length) {
    list.innerHTML = '<p class="text-secondary mb-16">No rules yet — orders ship with the packer’s own choices.</p>';
    return;
  }
  list.innerHTML = rules.map((r, i) => `
    <div class="row tag-rule" data-idx="${i}">
      <div class="field fixed" style="min-width:180px"><label>Order tag</label><input class="tr-tag" value="${esc(r.tag || '')}" placeholder="adult-signature"></div>
      <div class="field"><label>Preferred service (optional)</label><input class="tr-service" list="service-names" value="${esc(r.service || '')}" placeholder="Any — packer chooses"></div>
      <div class="field fixed" style="min-width:220px"><label>Signature</label>
        <select class="tr-signature">${SIGNATURE_OPTIONS.map(([v, l]) => `<option value="${v}" ${r.signature === v ? 'selected' : ''}>${l}</option>`).join('')}</select>
      </div>
      <div class="fixed" style="align-self:flex-end"><button class="btn btn-danger btn-small tr-remove" title="Remove rule">Remove</button></div>
    </div>`).join('');
}

function syncTagRulesFromDom() {
  const rules = [...document.querySelectorAll('#tag-rules-list .tag-rule')].map((row) => ({
    tag: row.querySelector('.tr-tag').value.trim(),
    service: row.querySelector('.tr-service').value.trim(),
    signature: row.querySelector('.tr-signature').value,
  }));
  writeTagRules(rules);
}

document.getElementById('tag-rules-list').addEventListener('input', syncTagRulesFromDom);
document.getElementById('tag-rules-list').addEventListener('change', syncTagRulesFromDom);
document.getElementById('tag-rules-list').addEventListener('click', (e) => {
  const btn = e.target.closest('.tr-remove');
  if (!btn) return;
  const rules = readTagRules();
  rules.splice(Number(btn.closest('.tag-rule').dataset.idx), 1);
  writeTagRules(rules);
  renderTagRules();
});
document.getElementById('add-tag-rule').addEventListener('click', () => {
  writeTagRules([...readTagRules(), { tag: '', service: '', signature: 'none' }]);
  renderTagRules();
  const rows = document.querySelectorAll('#tag-rules-list .tr-tag');
  if (rows.length) rows[rows.length - 1].focus();
});

/* ---------- Pick list rules ----------
   Stored as JSON in the hidden #picklist_rules input, like the tag rules, so
   they ride along with Save / Discard. The server assigns each rule its id and
   drops rules without a name or a complete condition, so the editor says so
   before that happens. Each card reads as a sentence and shows the chip the
   rule becomes on the Pick lists page. */
const PL_FIELDS = {
  order_tag: { label: 'Order tag', group: 'Tags', kind: 'text', placeholder: 'e.g. wholesale',
    hint: 'Matches part of a tag, any case', ops: [['contains', 'contains'], ['not_contains', 'doesn’t contain']] },
  customer_tag: { label: 'Customer tag', group: 'Tags', kind: 'text', placeholder: 'e.g. vip',
    hint: 'Matches part of a tag, any case', ops: [['contains', 'contains'], ['not_contains', 'doesn’t contain']] },
  order_total: { label: 'Order total', group: 'Order', kind: 'money', placeholder: '100',
    hint: '“At least 100” includes $100.00', ops: [['at_least', 'is at least'], ['under', 'is under']] },
  shipping_method: { label: 'Shipping method', group: 'Order', kind: 'text', placeholder: 'e.g. Economy',
    hint: 'Matches part of the name', ops: [['contains', 'contains'], ['not_contains', 'doesn’t contain']] },
  store: { label: 'Store', group: 'Order', kind: 'stores', hint: 'Pick one or more',
    ops: [['in', 'is one of'], ['not_in', 'is not one of']] },
  ship_state: { label: 'Ship-to state', group: 'Address', kind: 'states', placeholder: 'TX, CA',
    hint: 'Two-letter codes, comma-separated', ops: [['in', 'is one of'], ['not_in', 'is not one of']] },
  address_match: { label: 'Billing address', group: 'Address', kind: 'none',
    hint: 'Hide these to leave orders whose addresses differ', ops: [['match', 'is the same as ship-to']] },
};
const PL_TEMPLATES = [
  { title: 'Hide wholesale orders', text: 'Order or customer tag contains “wholesale”',
    rule: { name: 'Wholesale', action: 'hide', match: 'any', conditions: [
      { field: 'order_tag', op: 'contains', value: 'wholesale' }, { field: 'customer_tag', op: 'contains', value: 'wholesale' }] } },
  { title: 'Big orders', text: 'Order total is at least $300',
    rule: { name: 'Big orders', action: 'keep', match: 'all', conditions: [{ field: 'order_total', op: 'at_least', value: 300 }] } },
  { title: 'Same billing & ship-to', text: 'Switch it on to leave only orders whose addresses differ',
    rule: { name: 'Same address', action: 'hide', match: 'all', conditions: [{ field: 'address_match', op: 'match', value: 'yes' }] } },
];
const plSvg = (paths) => `<svg class="ico" viewBox="0 0 24 24" aria-hidden="true">${paths}</svg>`;
const PL_ICONS = {
  chevron: plSvg('<path d="m9 18 6-6-6-6"/>'),
  up: plSvg('<path d="m18 15-6-6-6 6"/>'),
  down: plSvg('<path d="m6 9 6 6 6-6"/>'),
  copy: plSvg('<rect width="14" height="14" x="8" y="8" rx="2"/><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/>'),
  trash: plSvg('<path d="M3 6h18"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>'),
  x: plSvg('<path d="M18 6 6 18"/><path d="m6 6 12 12"/>'),
  plus: plSvg('<path d="M12 5v14"/><path d="M5 12h14"/>'),
  minus: plSvg('<path d="M5 12h14"/>'),
  warn: plSvg('<path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3"/><path d="M12 9v4"/><path d="M12 17h.01"/>'),
};
let plStores = [];
const plOpen = new Set(); // keys of the rule cards shown expanded
let plNewKey = 0;
const plKey = (r) => r.id || r._key;

/* BackOffice databases and stores for the Pick lists tab; filled before the
   saved values are applied so the selects can show them. */
async function loadPicklistOptions() {
  let dbs = [];
  try {
    [dbs, plStores] = await Promise.all([api('/api/backoffice-dbs'), api('/api/shopify-stores')]);
  } catch { /* selects stay empty */ }
  document.getElementById('picklist_backoffice_db_id').innerHTML = '<option value="">— Not set —</option>'
    + dbs.map((d) => `<option value="${d.id}">${esc(d.name)}${d.is_active ? '' : ' (inactive)'}</option>`).join('');
}

function readPlRules() {
  try {
    const rules = JSON.parse(document.getElementById('picklist_rules').value || '[]');
    return Array.isArray(rules) ? rules : [];
  } catch {
    return [];
  }
}

function writePlRules(rules) {
  document.getElementById('picklist_rules').value = JSON.stringify(rules);
  setDirty(true);
}

const plAttr = (v) => esc(v).replace(/"/g, '&quot;');
const plStoreName = (id) => (plStores.find((s) => String(s.id) === String(id)) || {}).name || `Store #${id} (removed)`;

/* A condition the server will keep: the same checks as picklist_rules._parse_condition. */
function plConditionComplete(c) {
  const v = c.value;
  if (c.field === 'address_match') return true;
  if (c.field === 'order_total') return v !== '' && v != null && Number(v) >= 0 && !Number.isNaN(Number(v));
  if (Array.isArray(v)) return v.some((x) => String(x).trim());
  return String(v ?? '').trim() !== '';
}

function plConditionText(c) {
  const def = PL_FIELDS[c.field];
  if (!def) return '';
  const op = (def.ops.find(([v]) => v === c.op) || def.ops[0])[1];
  let value = '';
  if (def.kind === 'money') value = money(c.value);
  else if (def.kind === 'stores') value = (c.value || []).map(plStoreName).join(', ');
  else if (def.kind === 'states') value = (Array.isArray(c.value) ? c.value : String(c.value || '').split(',')).map((s) => s.trim().toUpperCase()).filter(Boolean).join(', ');
  else if (def.kind === 'text') value = `“${String(c.value).trim()}”`;
  return `${def.label.toLowerCase()} ${op}${value ? ` ${value}` : ''}`;
}

/* "Hides orders where order tag contains “wholesale” or customer tag …" */
function plRuleSummary(r) {
  const done = (r.conditions || []).filter(plConditionComplete);
  if (!done.length) return '';
  const verb = r.action === 'hide' ? 'Hides orders where' : 'Shows orders where';
  return `${verb} ${done.map((c) => esc(plConditionText(c))).join(` <b>${r.match === 'any' ? 'or' : 'and'}</b> `)}`;
}

function plRuleProblems(r) {
  const problems = [];
  if (!String(r.name || '').trim()) problems.push('Give the rule a name — rules without one aren’t saved.');
  const conds = r.conditions || [];
  const done = conds.filter(plConditionComplete);
  if (!done.length) problems.push('Add a condition with a value — a rule without one isn’t saved.');
  else if (done.length < conds.length) problems.push('Conditions left without a value are dropped when you save.');
  return problems;
}

function plValueHtml(c) {
  const def = PL_FIELDS[c.field];
  if (def.kind === 'none') return '';
  if (def.kind === 'stores') {
    const picked = (c.value || []).map(String);
    const known = plStores.map((s) => ({ id: String(s.id), name: s.name }));
    const gone = picked.filter((id) => !known.some((s) => s.id === id)).map((id) => ({ id, name: plStoreName(id) }));
    const all = [...known, ...gone];
    return all.length
      ? `<div class="plr-pills pl-value" role="group" aria-label="Stores">${all.map((s) => `<button type="button" class="plr-pill${picked.includes(s.id) ? ' on' : ''}" data-store="${plAttr(s.id)}" aria-pressed="${picked.includes(s.id)}">${esc(s.name)}</button>`).join('')}</div>`
      : '<span class="text-secondary">No Shopify stores yet</span>';
  }
  if (def.kind === 'money') {
    return `<label class="plr-money"><span>$</span><input class="pl-value" type="number" min="0" step="0.01" inputmode="decimal" value="${plAttr(c.value ?? '')}" placeholder="${def.placeholder}"></label>`;
  }
  const value = Array.isArray(c.value) ? c.value.join(', ') : (c.value ?? '');
  return `<input class="pl-value${def.kind === 'states' ? ' plr-states' : ''}" value="${plAttr(value)}" placeholder="${def.placeholder}" spellcheck="false">`;
}

function plFieldOptions(current) {
  const groups = {};
  Object.entries(PL_FIELDS).forEach(([v, d]) => { (groups[d.group] = groups[d.group] || []).push([v, d.label]); });
  return Object.entries(groups).map(([g, items]) => `<optgroup label="${g}">${items.map(([v, l]) =>
    `<option value="${v}" ${current === v ? 'selected' : ''}>${l}</option>`).join('')}</optgroup>`).join('');
}

function plConditionHtml(c, j, match) {
  const def = PL_FIELDS[c.field];
  const join = j ? `<div class="plr-join"><span>${match === 'any' ? 'or' : 'and'}</span></div>` : '';
  return `${join}<div class="plr-cond${plConditionComplete(c) ? '' : ' incomplete'}" data-cidx="${j}">
    <select class="pl-field" aria-label="Field">${plFieldOptions(c.field)}</select>
    <select class="pl-op" aria-label="Comparison" ${def.ops.length === 1 ? 'disabled' : ''}>${def.ops.map(([v, l]) => `<option value="${v}" ${c.op === v ? 'selected' : ''}>${l}</option>`).join('')}</select>
    <div class="plr-value">${plValueHtml(c)}<span class="plr-hint">${def.hint}</span></div>
    <button type="button" class="plr-icon pl-cond-remove" title="Remove condition" aria-label="Remove condition">${PL_ICONS.x}</button>
  </div>`;
}

function plChipPreview(r) {
  const hide = r.action === 'hide';
  return `<span class="chip pl-rule-chip ${hide ? 'mode-hide' : 'mode-add'} plr-preview" title="How the chip looks on the Pick lists page when switched on">${hide ? PL_ICONS.minus : PL_ICONS.plus}<span class="plr-preview-name">${esc(String(r.name || '').trim() || 'Unnamed')}</span></span>`;
}

function plSeg(cls, current, options) {
  return `<div class="plr-seg ${cls}" role="radiogroup">${options.map(([v, label]) =>
    `<button type="button" role="radio" data-v="${v}" class="${current === v ? 'on' : ''}" aria-checked="${current === v}">${label}</button>`).join('')}</div>`;
}

function plUsesHtml(r) {
  const uses = plPresetsUsing(r.id);
  return uses.length
    ? `<span class="plr-uses" title="Used in: ${plAttr(uses.map((p) => p.name || 'Unnamed').join(', '))}">${PLP_ICON}${uses.length}</span>`
    : '';
}

function plRuleHtml(r, i, total) {
  const open = plOpen.has(plKey(r));
  const action = r.action === 'hide' ? 'hide' : 'keep';
  const match = r.match === 'any' ? 'any' : 'all';
  const problems = plRuleProblems(r);
  const summary = plRuleSummary(r);
  return `<div class="plr plr-${action}${open ? ' open' : ''}${problems.length ? ' has-problems' : ''}" data-idx="${i}" data-key="${plAttr(plKey(r))}">
    <div class="plr-head">
      <button type="button" class="plr-icon plr-toggle" aria-expanded="${open}" title="${open ? 'Collapse' : 'Edit this rule'}">${PL_ICONS.chevron}</button>
      <div class="plr-titles">
        <input class="plr-name" value="${plAttr(r.name || '')}" placeholder="Name this rule" maxlength="40" aria-label="Rule name">
        <div class="plr-summary">${summary || '<span class="plr-summary-empty">No complete conditions yet</span>'}</div>
      </div>
      ${plChipPreview(r)}
      ${plUsesHtml(r)}
      <div class="plr-tools">
        <button type="button" class="plr-icon pl-up" title="Move up" aria-label="Move up" ${i === 0 ? 'disabled' : ''}>${PL_ICONS.up}</button>
        <button type="button" class="plr-icon pl-down" title="Move down" aria-label="Move down" ${i === total - 1 ? 'disabled' : ''}>${PL_ICONS.down}</button>
        <button type="button" class="plr-icon pl-dup" title="Duplicate" aria-label="Duplicate rule">${PL_ICONS.copy}</button>
        <button type="button" class="plr-icon danger pl-remove" title="Delete rule" aria-label="Delete rule">${PL_ICONS.trash}</button>
      </div>
    </div>
    <div class="plr-problems">${problems.map((p) => `<div>${PL_ICONS.warn}<span>${p}</span></div>`).join('')}</div>
    <div class="plr-body">
      <div class="plr-setting"><span class="plr-label">When switched on</span>
        ${plSeg('pl-action', action, [['keep', `${PL_ICONS.plus} Show matching orders`], ['hide', `${PL_ICONS.minus} Hide matching orders`]])}</div>
      <div class="plr-setting"><span class="plr-label">An order matches when</span>
        ${plSeg('pl-match', match, [['all', 'all conditions are true'], ['any', 'any condition is true']])}</div>
      <div class="plr-conds">${(r.conditions || []).map((c, j) => plConditionHtml(c, j, match)).join('')}</div>
      <button type="button" class="btn btn-text btn-small pl-cond-add">${PL_ICONS.plus} Add condition</button>
    </div>
  </div>`;
}

function renderPlRules() {
  const list = document.getElementById('pl-rules-list');
  if (!list) return;
  const rules = readPlRules();
  document.getElementById('add-pl-rule').style.display = rules.length ? '' : 'none';
  if (!rules.length) {
    renderPlPresets();
    list.innerHTML = `<div class="plr-empty">
      <div class="plr-empty-title">No rules yet</div>
      <p class="hint">The Pick lists page still has its quick filters. Start from an example or a blank rule:</p>
      <div class="plr-templates">${PL_TEMPLATES.map((t, i) => `<button type="button" class="plr-template" data-template="${i}">
        <strong>${esc(t.title)}</strong><span>${esc(t.text)}</span></button>`).join('')}
        <button type="button" class="plr-template blank" data-template="blank"><strong>${PL_ICONS.plus} Blank rule</strong><span>Build it from scratch</span></button></div>
    </div>`;
    return;
  }
  list.innerHTML = rules.map((r, i) => plRuleHtml(r, i, rules.length)).join('');
  renderPlPresets();
}

/* The editor's current state → the rules (without re-rendering). */
function plRulesFromDom() {
  const before = readPlRules();
  return [...document.querySelectorAll('#pl-rules-list .plr')].map((el, i) => ({
    ...(before[i] && before[i].id ? { id: before[i].id } : { _key: el.dataset.key }),
    name: el.querySelector('.plr-name').value,
    action: el.querySelector('.pl-action .on').dataset.v,
    match: el.querySelector('.pl-match .on').dataset.v,
    conditions: [...el.querySelectorAll('.plr-cond')].map((c) => {
      const field = c.querySelector('.pl-field').value;
      const op = c.querySelector('.pl-op').value;
      const valueEl = c.querySelector('.pl-value');
      let value = '';
      if (field === 'address_match') value = 'yes';
      else if (field === 'store') value = valueEl ? [...valueEl.querySelectorAll('.plr-pill.on')].map((p) => p.dataset.store) : [];
      else if (field === 'ship_state') value = valueEl.value.split(',').map((v) => v.trim().toUpperCase()).filter(Boolean);
      else value = valueEl.value;
      return { field, op, value };
    }),
  }));
}

/* Summary, chip preview and warnings follow every keystroke; the inputs stay put. */
function syncPlRules() {
  const rules = plRulesFromDom();
  writePlRules(rules);
  document.querySelectorAll('#pl-rules-list .plr').forEach((el, i) => {
    const r = rules[i];
    const problems = plRuleProblems(r);
    el.querySelector('.plr-summary').innerHTML = plRuleSummary(r) || '<span class="plr-summary-empty">No complete conditions yet</span>';
    el.querySelector('.plr-preview').outerHTML = plChipPreview(r);
    el.querySelector('.plr-problems').innerHTML = problems.map((p) => `<div>${PL_ICONS.warn}<span>${p}</span></div>`).join('');
    el.classList.toggle('has-problems', problems.length > 0);
    el.classList.toggle('plr-hide', r.action === 'hide');
    el.classList.toggle('plr-keep', r.action !== 'hide');
    el.querySelectorAll('.plr-join span').forEach((s) => { s.textContent = r.match === 'any' ? 'or' : 'and'; });
    el.querySelectorAll('.plr-cond').forEach((c, j) => c.classList.toggle('incomplete', !plConditionComplete(r.conditions[j])));
  });
  renderPlPresets(); // rule names show inside the presets
}

function defaultPlCondition(field = 'order_tag') {
  const def = PL_FIELDS[field];
  let value = '';
  if (field === 'address_match') value = 'yes';
  else if (field === 'store') value = plStores.length ? [String(plStores[0].id)] : [];
  return { field, op: def.ops[0][0], value };
}

function addPlRule(rule) {
  const key = `new${++plNewKey}`;
  writePlRules([...plRulesFromDom(), { ...JSON.parse(JSON.stringify(rule)), _key: key }]);
  plOpen.add(key);
  renderPlRules();
  const card = document.querySelector(`#pl-rules-list .plr[data-key="${key}"]`);
  if (card) {
    card.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    const name = card.querySelector('.plr-name');
    name.focus();
    name.select();
  }
}

const plList = document.getElementById('pl-rules-list');
plList.addEventListener('input', (e) => {
  if (e.target.classList.contains('plr-states')) {
    const at = e.target.selectionStart;
    e.target.value = e.target.value.toUpperCase();
    e.target.setSelectionRange(at, at);
  }
  syncPlRules();
});
plList.addEventListener('change', (e) => {
  if (!e.target.classList.contains('pl-field')) {
    syncPlRules();
    return;
  }
  // A new field brings its own comparisons and value input.
  const rules = plRulesFromDom();
  const cond = e.target.closest('.plr-cond');
  rules[cond.closest('.plr').dataset.idx].conditions[cond.dataset.cidx] = defaultPlCondition(e.target.value);
  writePlRules(rules);
  renderPlRules();
  const fresh = plList.querySelector(`.plr[data-idx="${cond.closest('.plr').dataset.idx}"] .plr-cond[data-cidx="${cond.dataset.cidx}"] .pl-value`);
  if (fresh && fresh.focus) fresh.focus();
});
plList.addEventListener('click', (e) => {
  const template = e.target.closest('[data-template]');
  if (template) {
    const t = template.dataset.template;
    addPlRule(t === 'blank' ? { name: '', action: 'keep', match: 'all', conditions: [defaultPlCondition()] } : PL_TEMPLATES[t].rule);
    return;
  }
  const ruleEl = e.target.closest('.plr');
  if (!ruleEl) return;
  const seg = e.target.closest('.plr-seg button');
  if (seg) {
    seg.parentElement.querySelectorAll('button').forEach((b) => {
      b.classList.toggle('on', b === seg);
      b.setAttribute('aria-checked', String(b === seg));
    });
    syncPlRules();
    return;
  }
  const pill = e.target.closest('.plr-pill');
  if (pill) {
    pill.classList.toggle('on');
    pill.setAttribute('aria-pressed', String(pill.classList.contains('on')));
    syncPlRules();
    return;
  }
  const idx = Number(ruleEl.dataset.idx);
  const key = ruleEl.dataset.key;
  const head = e.target.closest('.plr-head');
  if (e.target.closest('.plr-toggle') || (head && !e.target.closest('input, button, .plr-preview'))) {
    if (plOpen.has(key)) plOpen.delete(key);
    else plOpen.add(key);
    ruleEl.classList.toggle('open', plOpen.has(key));
    ruleEl.querySelector('.plr-toggle').setAttribute('aria-expanded', String(plOpen.has(key)));
    return;
  }
  const button = e.target.closest('button');
  if (!button) return;
  const rules = plRulesFromDom();
  if (button.classList.contains('pl-remove')) rules.splice(idx, 1);
  else if (button.classList.contains('pl-dup')) {
    const copyKey = `new${++plNewKey}`;
    const { id, _key, ...rest } = rules[idx];
    rules.splice(idx + 1, 0, { ...rest, name: `${rest.name} (copy)`.slice(0, 40), _key: copyKey });
    plOpen.add(copyKey);
  } else if (button.classList.contains('pl-up') && idx > 0) [rules[idx - 1], rules[idx]] = [rules[idx], rules[idx - 1]];
  else if (button.classList.contains('pl-down') && idx < rules.length - 1) [rules[idx + 1], rules[idx]] = [rules[idx], rules[idx + 1]];
  else if (button.classList.contains('pl-cond-add')) {
    rules[idx].conditions.push(defaultPlCondition());
    plOpen.add(key);
  } else if (button.classList.contains('pl-cond-remove')) rules[idx].conditions.splice(button.closest('.plr-cond').dataset.cidx, 1);
  else return;
  writePlRules(rules);
  renderPlRules();
});
document.getElementById('add-pl-rule').addEventListener('click', () => {
  addPlRule({ name: '', action: 'keep', match: 'all', conditions: [defaultPlCondition()] });
});

/* ---------- Rule presets ----------
   Saved rules combined into one filter: groups of rules joined by AND / OR
   (each rule can be "doesn't match"), and the groups joined the same way.
   Stored in the hidden #picklist_presets input. Presets point at rules by id,
   so only saved rules can be picked; the server drops parts that point at a
   rule that is gone, and the editor says so first. */
const plpOpen = new Set();
let plpNewKey = 0;
const PLP_ICON = plSvg('<path d="m12.83 2.18a2 2 0 0 0-1.66 0L2.6 6.08a1 1 0 0 0 0 1.83l8.58 3.91a2 2 0 0 0 1.66 0l8.58-3.9a1 1 0 0 0 0-1.83Z"/><path d="m22 17.65-9.17 4.16a2 2 0 0 1-1.66 0L2 17.65"/><path d="m22 12.65-9.17 4.16a2 2 0 0 1-1.66 0L2 12.65"/>');

function readPlPresets() {
  try {
    const presets = JSON.parse(document.getElementById('picklist_presets').value || '[]');
    return Array.isArray(presets) ? presets : [];
  } catch {
    return [];
  }
}

function writePlPresets(presets) {
  document.getElementById('picklist_presets').value = JSON.stringify(presets);
  setDirty(true);
  // Rule cards show how many presets use them.
  const rules = readPlRules();
  document.querySelectorAll('#pl-rules-list .plr').forEach((el, i) => {
    const old = el.querySelector('.plr-uses');
    if (old) old.remove();
    if (rules[i]) el.querySelector('.plr-preview').insertAdjacentHTML('afterend', plUsesHtml(rules[i]));
  });
}

const plSavedRules = () => readPlRules().filter((r) => r.id);
const plRuleById = (id) => readPlRules().find((r) => r.id === id);

function plpExpressionHtml(p) {
  const join = (j) => `<span class="pl-pjoin">${j === 'or' ? 'or' : 'and'}</span>`;
  const groups = (p.groups || []).filter((g) => g.items.length).map((g) => {
    const body = g.items.map((it) => {
      const rule = plRuleById(it.rule);
      return `${it.negate ? '<span class="pl-pnot">not</span>' : ''}<span class="pl-ptoken${rule ? '' : ' gone'}">${esc(rule ? rule.name || 'Unnamed rule' : 'deleted rule')}</span>`;
    }).join(join(g.join));
    return p.groups.length > 1 && g.items.length > 1 ? `<span class="pl-pgroup">${body}</span>` : body;
  });
  return groups.join(join(p.join));
}

function plpProblems(p) {
  const problems = [];
  if (!String(p.name || '').trim()) problems.push('Give the preset a name — presets without one aren’t saved.');
  const items = (p.groups || []).flatMap((g) => g.items);
  if (!items.length) problems.push('Add at least one rule.');
  if (items.some((it) => !plRuleById(it.rule))) problems.push('It uses a rule that was deleted — that part is dropped when you save.');
  return problems;
}

function plpRuleOptions(current) {
  const all = readPlRules();
  const opts = all.map((r) => (r.id
    ? `<option value="${plAttr(r.id)}" ${r.id === current ? 'selected' : ''}>${esc(r.name || 'Unnamed rule')}</option>`
    : `<option disabled>${esc(r.name || 'New rule')} — save rules first</option>`));
  if (current && !all.some((r) => r.id === current)) opts.unshift(`<option value="${plAttr(current)}" selected>Deleted rule</option>`);
  return opts.join('');
}

function plpItemHtml(it, k, join) {
  const gone = !plRuleById(it.rule);
  const action = (plRuleById(it.rule) || {}).action;
  return `${k ? `<div class="plr-join"><span>${join === 'or' ? 'or' : 'and'}</span></div>` : ''}
    <div class="plp-item${gone ? ' gone' : ''}" data-iidx="${k}">
      ${plSeg('plp-neg', it.negate ? 'not' : 'is', [['is', 'matches'], ['not', 'doesn’t match']])}
      <div class="plp-rule-wrap">
        <span class="plp-rule-dot ${action === 'hide' ? 'hide' : action ? 'keep' : ''}" title="${action === 'hide' ? 'Hide rule' : action ? 'Show rule' : 'Deleted rule'}"></span>
        <select class="plp-rule" aria-label="Rule">${plpRuleOptions(it.rule)}</select>
      </div>
      <button type="button" class="plr-icon plp-item-remove" title="Remove from group" aria-label="Remove rule from group">${PL_ICONS.x}</button>
    </div>`;
}

function plpGroupHtml(g, gi, total, topJoin) {
  return `${gi ? `<div class="plp-gjoin"><span>${topJoin === 'or' ? 'or' : 'and'}</span></div>` : ''}
    <div class="plp-group" data-gidx="${gi}">
      <div class="plp-ghead">
        <span class="plp-gtitle">${total > 1 ? `Group ${gi + 1}` : 'Rules'}</span>
        ${g.items.length > 1 ? '<span class="plp-glead">match</span>' : '<span class="plp-glead">this rule must be true</span>'}
        <span ${g.items.length > 1 ? '' : 'hidden'}>${plSeg('plp-gjoin-seg', g.join === 'or' ? 'or' : 'and', [['and', 'all of'], ['or', 'any of']])}</span>
        ${g.items.length > 1 ? '<span class="plp-glead">these rules</span>' : ''}
        <span class="plr-spacer"></span>
        ${total > 1 ? `<button type="button" class="plr-icon danger plp-group-remove" title="Remove group" aria-label="Remove group">${PL_ICONS.trash}</button>` : ''}
      </div>
      <div class="plp-items">${g.items.map((it, k) => plpItemHtml(it, k, g.join)).join('')}</div>
      <button type="button" class="btn btn-text btn-small plp-item-add">${PL_ICONS.plus} Add rule</button>
    </div>`;
}

function plpPresetHtml(p, i, total) {
  const key = p.id || p._key;
  const open = plpOpen.has(key);
  const problems = plpProblems(p);
  const groups = p.groups || [];
  const expr = plpExpressionHtml(p);
  return `<div class="plr plp${open ? ' open' : ''}${problems.length ? ' has-problems' : ''}" data-idx="${i}" data-key="${plAttr(key)}">
    <div class="plr-head">
      <button type="button" class="plr-icon plr-toggle" aria-expanded="${open}" title="${open ? 'Collapse' : 'Edit this preset'}">${PL_ICONS.chevron}</button>
      <div class="plr-titles">
        <input class="plr-name" value="${plAttr(p.name || '')}" placeholder="Name this preset" maxlength="40" aria-label="Preset name">
        <div class="plr-summary plp-expr">${expr ? `<span>Shows orders that match</span> ${expr}` : '<span class="plr-summary-empty">No rules in it yet</span>'}</div>
      </div>
      <span class="chip pl-preset-chip on plr-preview" title="How the preset looks on the Pick lists page when it is on">${PLP_ICON}<span class="plr-preview-name">${esc(String(p.name || '').trim() || 'Unnamed')}</span></span>
      <div class="plr-tools">
        <button type="button" class="plr-icon pl-up" title="Move up" aria-label="Move up" ${i === 0 ? 'disabled' : ''}>${PL_ICONS.up}</button>
        <button type="button" class="plr-icon pl-down" title="Move down" aria-label="Move down" ${i === total - 1 ? 'disabled' : ''}>${PL_ICONS.down}</button>
        <button type="button" class="plr-icon pl-dup" title="Duplicate" aria-label="Duplicate preset">${PL_ICONS.copy}</button>
        <button type="button" class="plr-icon danger pl-remove" title="Delete preset" aria-label="Delete preset">${PL_ICONS.trash}</button>
      </div>
    </div>
    <div class="plr-problems">${problems.map((t) => `<div>${PL_ICONS.warn}<span>${t}</span></div>`).join('')}</div>
    <div class="plr-body">
      ${groups.length > 1 ? `<div class="plr-setting"><span class="plr-label">Groups combine as</span>
        ${plSeg('plp-join', p.join === 'or' ? 'or' : 'and', [['and', 'every group must match'], ['or', 'any group can match']])}</div>` : ''}
      <div class="plp-groups">${groups.map((g, gi) => plpGroupHtml(g, gi, groups.length, p.join)).join('')}</div>
      <button type="button" class="btn btn-outlined btn-small plp-group-add">${PL_ICONS.plus} Add group</button>
      <span class="plp-group-hint hint">A group is a set of rules in brackets — e.g. (Wholesale or Big orders) and not Same address</span>
    </div>
  </div>`;
}

function renderPlPresets() {
  const list = document.getElementById('pl-presets-list');
  if (!list) return;
  const presets = readPlPresets();
  const saved = plSavedRules();
  document.getElementById('add-pl-preset').style.display = presets.length ? '' : 'none';
  document.getElementById('add-pl-preset').disabled = !saved.length;
  if (!presets.length) {
    list.innerHTML = `<div class="plr-empty plp-empty">
      <div class="plp-empty-art">
        <span class="pl-ptoken">Wholesale</span><span class="pl-pjoin">or</span><span class="pl-ptoken">Big orders</span>
        <span class="pl-pjoin">and</span><span class="pl-pnot">not</span><span class="pl-ptoken">Same address</span>
      </div>
      <div class="plr-empty-title">No presets yet</div>
      <p class="hint">${saved.length
        ? 'A preset combines your rules into one filter that anyone can switch on with a click on the Pick lists page.'
        : 'Presets are built from saved rules — add a rule above and save first.'}</p>
      <button type="button" class="btn btn-primary plp-create" ${saved.length ? '' : 'disabled'}>${PL_ICONS.plus} Create a preset</button>
    </div>`;
    return;
  }
  list.innerHTML = presets.map((p, i) => plpPresetHtml(p, i, presets.length)).join('');
}

function plpFromDom() {
  const before = readPlPresets();
  return [...document.querySelectorAll('#pl-presets-list .plp')].map((el, i) => {
    const prev = before[i] || {};
    const top = el.querySelector('.plp-join .on');
    return {
      ...(prev.id ? { id: prev.id } : { _key: el.dataset.key }),
      name: el.querySelector('.plr-name').value,
      join: top ? top.dataset.v : prev.join || 'and',
      groups: [...el.querySelectorAll('.plp-group')].map((g) => ({
        join: g.querySelector('.plp-gjoin-seg .on').dataset.v,
        items: [...g.querySelectorAll('.plp-item')].map((it) => ({
          rule: it.querySelector('.plp-rule').value,
          negate: it.querySelector('.plp-neg .on').dataset.v === 'not',
        })),
      })),
    };
  });
}

function syncPlPresets() {
  const presets = plpFromDom();
  writePlPresets(presets);
  document.querySelectorAll('#pl-presets-list .plp').forEach((el, i) => {
    const p = presets[i];
    const problems = plpProblems(p);
    const expr = plpExpressionHtml(p);
    el.querySelector('.plp-expr').innerHTML = expr ? `<span>Shows orders that match</span> ${expr}` : '<span class="plr-summary-empty">No rules in it yet</span>';
    el.querySelector('.plr-preview-name').textContent = String(p.name || '').trim() || 'Unnamed';
    el.querySelector('.plr-problems').innerHTML = problems.map((t) => `<div>${PL_ICONS.warn}<span>${t}</span></div>`).join('');
    el.classList.toggle('has-problems', problems.length > 0);
    el.querySelectorAll('.plp-group').forEach((g, gi) => {
      g.querySelectorAll(':scope .plp-items > .plr-join span').forEach((s) => { s.textContent = p.groups[gi].join === 'or' ? 'or' : 'and'; });
    });
    el.querySelectorAll('.plp-gjoin span').forEach((s) => { s.textContent = p.join === 'or' ? 'or' : 'and'; });
  });
}

/* A rule for a new line: the first saved rule the group doesn't use yet. */
function plpNextRule(items) {
  const saved = plSavedRules();
  const free = saved.find((r) => !items.some((it) => it.rule === r.id));
  return (free || saved[0] || {}).id || '';
}

function addPlPreset() {
  const key = `newp${++plpNewKey}`;
  writePlPresets([...plpFromDom(), { _key: key, name: '', join: 'and', groups: [{ join: 'or', items: [{ rule: plpNextRule([]), negate: false }] }] }]);
  plpOpen.add(key);
  renderPlPresets();
  const card = document.querySelector(`#pl-presets-list .plp[data-key="${key}"]`);
  if (card) {
    card.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    card.querySelector('.plr-name').focus();
  }
}

const plpList = document.getElementById('pl-presets-list');
plpList.addEventListener('input', syncPlPresets);
plpList.addEventListener('change', () => {
  syncPlPresets();
  renderPlPresets(); // the rule's colour dot and "deleted" state follow the pick
});
plpList.addEventListener('click', (e) => {
  if (e.target.closest('.plp-create')) {
    addPlPreset();
    return;
  }
  const card = e.target.closest('.plp');
  if (!card) return;
  const seg = e.target.closest('.plr-seg button');
  if (seg) {
    seg.parentElement.querySelectorAll('button').forEach((b) => {
      b.classList.toggle('on', b === seg);
      b.setAttribute('aria-checked', String(b === seg));
    });
    syncPlPresets();
    return;
  }
  const idx = Number(card.dataset.idx);
  const key = card.dataset.key;
  const head = e.target.closest('.plr-head');
  if (e.target.closest('.plr-toggle') || (head && !e.target.closest('input, button, .plr-preview'))) {
    if (plpOpen.has(key)) plpOpen.delete(key);
    else plpOpen.add(key);
    card.classList.toggle('open', plpOpen.has(key));
    card.querySelector('.plr-toggle').setAttribute('aria-expanded', String(plpOpen.has(key)));
    return;
  }
  const button = e.target.closest('button');
  if (!button) return;
  const presets = plpFromDom();
  const p = presets[idx];
  const group = button.closest('.plp-group');
  const g = group ? p.groups[group.dataset.gidx] : null;
  if (button.classList.contains('pl-remove')) presets.splice(idx, 1);
  else if (button.classList.contains('pl-dup')) {
    const copyKey = `newp${++plpNewKey}`;
    const { id, _key, ...rest } = p;
    presets.splice(idx + 1, 0, { ...JSON.parse(JSON.stringify(rest)), name: `${rest.name} (copy)`.slice(0, 40), _key: copyKey });
    plpOpen.add(copyKey);
  } else if (button.classList.contains('pl-up') && idx > 0) [presets[idx - 1], presets[idx]] = [presets[idx], presets[idx - 1]];
  else if (button.classList.contains('pl-down') && idx < presets.length - 1) [presets[idx + 1], presets[idx]] = [presets[idx], presets[idx + 1]];
  else if (button.classList.contains('plp-group-add')) p.groups.push({ join: 'or', items: [{ rule: plpNextRule([]), negate: false }] });
  else if (button.classList.contains('plp-group-remove')) p.groups.splice(group.dataset.gidx, 1);
  else if (button.classList.contains('plp-item-add')) g.items.push({ rule: plpNextRule(g.items), negate: false });
  else if (button.classList.contains('plp-item-remove')) {
    g.items.splice(button.closest('.plp-item').dataset.iidx, 1);
    if (!g.items.length && p.groups.length > 1) p.groups.splice(group.dataset.gidx, 1);
  } else return;
  writePlPresets(presets);
  renderPlPresets();
});
document.getElementById('add-pl-preset').addEventListener('click', addPlPreset);

/* Which presets use a rule, for the badge on its card. */
function plPresetsUsing(ruleId) {
  if (!ruleId) return [];
  return readPlPresets().filter((p) => (p.groups || []).some((g) => g.items.some((it) => it.rule === ruleId)));
}

/* Service-name suggestions for the rule editor: every courier service the
   enabled providers expose, deduped by name. */
async function loadServiceNames() {
  let descriptors = [];
  try { descriptors = await api('/api/providers'); } catch { return; }
  const names = new Set();
  await Promise.all(descriptors
    .filter((p) => p.enabled && p.supports && p.supports.service_exclusions && p.services_endpoint)
    .map(async (p) => {
      try {
        const res = await api(p.services_endpoint);
        (res.services || []).forEach((s) => { if (s.name) names.add(s.name); });
      } catch { /* provider offline — suggestions just stay shorter */ }
    }));
  document.getElementById('service-names').innerHTML =
    [...names].sort().map((n) => `<option value="${esc(n)}"></option>`).join('');
}

/* ---------- Print mode: show only what the selected mode needs ---------- */
const PRINT_HINTS = {
  browser: 'After purchase the label opens in this browser’s print dialog — works with any printer, one click per label.',
  network: 'The server sends labels straight to a Zebra (ZPL) printer on the network — no dialog. Any provider label format works: PDF and PNG labels are converted to 4x6 ZPL at the printer DPI; native ZPL is sent as-is.',
  browserprint: 'Labels print silently through the Zebra Browser Print app on each packing station. The app must be running with the Zebra set as its default printer; the first print asks to accept this website inside the app. Any provider label format works — labels are converted to 4x6 ZPL at the printer DPI. Test from the packing station, not the server.',
};

function updatePrintModeUI() {
  const mode = document.getElementById('print_mode').value || 'browser';
  document.getElementById('print-network').style.display = mode === 'network' ? '' : 'none';
  document.getElementById('print-browserprint').style.display = mode === 'browserprint' ? '' : 'none';
  document.getElementById('print-dpi').style.display = mode === 'browser' ? 'none' : '';
  document.getElementById('print-hint').textContent = PRINT_HINTS[mode] || '';
}

document.getElementById('print_mode').addEventListener('change', updatePrintModeUI);

/* ---------- Box sizes ---------- */
async function loadBoxes() {
  const boxes = await api('/api/boxes');
  const tbody = document.getElementById('boxes-body');
  tbody.innerHTML = boxes.length
    ? boxes.map((b) => `<tr>
        <td class="mono"><strong>${b.length} × ${b.width} × ${b.height}</strong></td>
        <td><button class="btn btn-danger btn-small" onclick="deleteBox(${b.id})">Delete</button></td>
      </tr>`).join('')
    : '<tr><td colspan="2" class="text-secondary">No box sizes yet.</td></tr>';
}

document.getElementById('add-box').addEventListener('click', async () => {
  try {
    await api('/api/boxes', {
      method: 'POST',
      body: {
        length: document.getElementById('box-length').value,
        width: document.getElementById('box-width').value,
        height: document.getElementById('box-height').value,
      },
    });
    ['box-length', 'box-width', 'box-height'].forEach((id) => {
      document.getElementById(id).value = '';
    });
    snackbar('Box added', 'success');
    loadBoxes();
  } catch (err) {
    snackbar(err.message, 'error');
  }
});

window.deleteBox = async (id) => {
  await api(`/api/boxes/${id}`, { method: 'DELETE' });
  loadBoxes();
};

/* ---------- Shipping providers (one card per configured instance) ----------
   Cards render from GET /api/providers descriptors; an instance's fields are
   plain settings keys prefixed with its key, saved through the regular
   Save / Discard flow. Add / rename / delete go through /api/provider-instances
   and re-render the cards. */
let instanceByKey = {};
let renderedProviders = [];

async function renderProviders() {
  const container = document.getElementById('providers-container');
  let list, meta;
  try {
    [list, meta] = await Promise.all([api('/api/providers'), api('/api/provider-instances')]);
  } catch (err) {
    container.innerHTML = `<div class="card"><p class="text-secondary">Could not load providers: ${esc(err.message)}</p></div>`;
    return;
  }
  instanceByKey = Object.fromEntries((meta.instances || []).map((i) => [i.key, i]));
  renderedProviders = list;
  // Register every instance field as a persistable setting (fresh on each re-render).
  SETTING_IDS = [...BASE_SETTING_IDS];
  list.forEach((p) => {
    SETTING_IDS.push(p.enabled_key);
    if (p.modes && p.modes.length) SETTING_IDS.push(p.mode_key);
    p.fields.forEach((f) => SETTING_IDS.push(f.key));
    if (p.origin_override_key) {
      SETTING_IDS.push(p.origin_override_key);
      (p.origin_fields || []).forEach((f) => SETTING_IDS.push(f.key));
    }
  });
  container.innerHTML = list.length
    ? list.map(providerCardHtml).join('')
    : '<div class="card"><p class="text-secondary">No shipping integrations yet — add one above.</p></div>';
  renderOriginAccounts(list);
  for (const p of list) {
    wireProviderCard(p);
    for (const f of p.fields) {
      if (f.type === 'select' && f.options_endpoint) await loadFieldOptions(f);
    }
    if (p.supports && p.supports.service_exclusions) loadServices(p);
  }
  wireProviderPicker(list);
  wireAddInstance(meta.platforms || []);
}

function instanceOptionText(p) {
  return p.label === p.platform_label ? p.label : `${p.label} — ${p.platform_label}`;
}

/* Show one instance's config card at a time, chosen from the picker dropdown.
   Keeps the current choice across re-renders; `preferred` selects a given key. */
function wireProviderPicker(list, preferred) {
  const picker = document.getElementById('provider-config-select');
  if (!picker) return;
  const previous = preferred || picker.value;
  picker.innerHTML = list.map((p) => `<option value="${esc(p.name)}">${esc(instanceOptionText(p))}</option>`).join('');
  const showOnly = (name) => {
    list.forEach((p) => {
      document.querySelectorAll(`[data-provider="${p.name}"], [data-services="${p.name}"]`)
        .forEach((el) => { el.style.display = p.name === name ? '' : 'none'; });
    });
  };
  if (!list.length) return;
  picker.value = list.some((p) => p.name === previous) ? previous : list[0].name;
  showOnly(picker.value);
  picker.onchange = () => showOnly(picker.value);
}

function wireAddInstance(platforms) {
  const select = document.getElementById('new-instance-platform');
  const label = document.getElementById('new-instance-label');
  const button = document.getElementById('add-instance');
  if (!select || !button) return;
  select.innerHTML = platforms.map((p) => `<option value="${esc(p.name)}">${esc(p.label)}</option>`).join('');
  button.onclick = async () => {
    if (!settingsClean()) return;
    try {
      const row = await api('/api/provider-instances', {
        method: 'POST',
        body: { platform: select.value, label: label.value },
      });
      label.value = '';
      snackbar(`${row.label} added — enter its credentials, then enable it and save`, 'success');
      await refreshProviders(row.key);
    } catch (err) {
      snackbar(err.message, 'error');
    }
  };
}

/* Instance changes re-render the cards, which would discard unsaved edits. */
function settingsClean() {
  if (dirty) snackbar('Save or discard your settings changes first', 'error');
  return !dirty;
}

async function refreshProviders(selectKey) {
  allProviders = [];
  await renderProviders();
  await loadSettings();
  watchDirty();
  const picker = document.getElementById('provider-config-select');
  if (selectKey && picker && [...picker.options].some((o) => o.value === selectKey)) {
    picker.value = selectKey;
    picker.onchange();
  }
  loadServiceNames();
  loadUsers();
  initNav('settings');
}

/* `{key}_origin_zip` → `zip`, the matching global field is `origin_zip`. */
function originSuffix(p, f) {
  return f.key.slice(p.name.length + '_origin_'.length);
}

function originFieldsHtml(p) {
  const byKey = Object.fromEntries((p.origin_fields || []).map((f) => [originSuffix(p, f), f]));
  const input = (name, extra = '') => {
    const f = byKey[name];
    return f ? `<div class="field${extra ? ' fixed' : ''}"${extra ? ` style="${extra}"` : ''}><label>${esc(f.label)}</label><input id="${esc(f.key)}"${name === 'state' ? ' maxlength="2" style="text-transform:uppercase"' : ''}${name === 'email' ? ' type="email"' : ''}></div>` : '';
  };
  return `
    <div id="origin-fields-${esc(p.name)}" style="display:none">
      <div class="row mt-16">${input('company')}${input('contact')}</div>
      <div class="row mt-16">${input('address1')}${input('address2')}</div>
      <div class="row mt-16">${input('city')}${input('state', 'min-width:90px')}${input('zip', 'min-width:120px')}</div>
      <div class="row mt-16">${input('phone')}${input('email')}</div>
    </div>`;
}

/* Shipping tab: one ship-from block per instance, one shown at a time. */
function renderOriginAccounts(list) {
  const container = document.getElementById('origin-accounts-container');
  const picker = document.getElementById('origin-account-select');
  const withOrigin = list.filter((p) => p.origin_override_key);
  container.innerHTML = withOrigin.length ? withOrigin.map((p) => `
    <div data-origin-account="${esc(p.name)}">
      <div class="row mt-16">
        <label class="svc-selectall"><input type="checkbox" id="${esc(p.origin_override_key)}" data-origin-toggle="${esc(p.name)}"> Use a different ship-from for this account</label>
      </div>
      ${originFieldsHtml(p)}
    </div>`).join('')
    : '<p class="text-secondary mt-16">No shipping integrations yet — add one on the Providers tab.</p>';
  const previous = picker.value;
  picker.innerHTML = withOrigin.map((p) => `<option value="${esc(p.name)}">${esc(instanceOptionText(p))}</option>`).join('');
  picker.dataset.labels = JSON.stringify(Object.fromEntries(withOrigin.map((p) => [p.name, instanceOptionText(p)])));
  if (withOrigin.length) picker.value = withOrigin.some((p) => p.name === previous) ? previous : withOrigin[0].name;
  picker.onchange = () => {
    container.querySelectorAll('[data-origin-account]').forEach((el) => {
      el.style.display = el.dataset.originAccount === picker.value ? '' : 'none';
    });
  };
  picker.onchange();
  container.oninput = syncOriginPlaceholders;
  container.onchange = (e) => { if (e.target.dataset.originToggle) syncOriginToggles(); };
}

function showOriginAccount(name) {
  showTab('shipping');
  const picker = document.getElementById('origin-account-select');
  picker.value = name;
  picker.onchange();
  document.getElementById('card-origin-accounts').scrollIntoView({ behavior: 'smooth', block: 'start' });
}

/* Greyed placeholders show the global value an instance field falls back to.
   The street address falls back as a unit, so once any of the instance's
   address fields is filled the other address fields show nothing. */
function syncOriginPlaceholders() {
  renderedProviders.forEach((p) => {
    const fields = p.origin_fields || [];
    const ownAddress = fields.some((f) => f.address && (document.getElementById(f.key)?.value || '').trim());
    fields.forEach((f) => {
      const el = document.getElementById(f.key);
      const globalEl = document.getElementById(`origin_${originSuffix(p, f)}`);
      if (el) el.placeholder = f.address && ownAddress ? '' : (globalEl?.value || '').trim();
    });
  });
}

/* Origin-override fields only show while the override box is ticked; the
   global card, the account picker and each Providers card say which
   accounts ship from their own details. */
function syncOriginToggles() {
  const picker = document.getElementById('origin-account-select');
  const labels = JSON.parse(picker.dataset.labels || '{}');
  const own = [];
  document.querySelectorAll('[data-origin-toggle]').forEach((box) => {
    const name = box.dataset.originToggle;
    const fields = document.getElementById(`origin-fields-${name}`);
    if (fields) fields.style.display = box.checked ? '' : 'none';
    const option = [...picker.options].find((o) => o.value === name);
    if (option) option.textContent = `${labels[name] || name}${box.checked ? ' · own ship-from' : ''}`;
    const summary = document.getElementById(`origin-summary-${name}`);
    if (summary) summary.textContent = box.checked ? 'Ship from: own details for this account' : 'Ship from: global origin';
    if (box.checked) own.push(labels[name] || name);
  });
  document.getElementById('origin-overrides-note').textContent = own.length
    ? `Used by every shipping account except ${own.join(', ')}, which ${own.length === 1 ? 'has its' : 'have their'} own ship-from below (blank fields there still fall back to this address).`
    : 'Used by every shipping account. To ship from different details for one account, use Per-account ship-from below.';
  syncOriginPlaceholders();
}

document.getElementById('card-origin').addEventListener('input', syncOriginPlaceholders);

/* Delete is locked while the SAVED enabled flag is on (the server refuses
   with 409 anyway). Runs after load/save/discard, when the checkboxes
   reflect what's stored. */
function syncDeleteButtons() {
  document.querySelectorAll('[data-delete]').forEach((btn) => {
    const enabled = document.getElementById(`${btn.dataset.delete}_enabled`);
    const locked = !!(enabled && enabled.checked);
    btn.disabled = locked;
    btn.title = locked ? 'Disable this integration and save before deleting it'
      : 'Remove this integration and its credentials';
  });
}

function fieldHtml(f) {
  const hint = f.hint ? `<span class="hint">${esc(f.hint)}</span>` : '';
  if (f.type === 'secret') {
    return `<div class="field"><label>${esc(f.label)}</label>
      <input type="password" id="${esc(f.key)}" autocomplete="off">${hint}</div>`;
  }
  if (f.type === 'select') {
    // Inline options render immediately; otherwise loadFieldOptions fills from options_endpoint.
    const opts = (f.options || [])
      .map((o) => `<option value="${esc(o.value)}">${esc(o.label)}</option>`).join('');
    return `<div class="field fixed" style="min-width:260px"><label>${esc(f.label)}</label>
      <select id="${esc(f.key)}">${opts}</select>${hint}</div>`;
  }
  return `<div class="field"><label>${esc(f.label)}</label><input id="${esc(f.key)}">${hint}</div>`;
}

function providerCardHtml(p) {
  const modeField = p.modes && p.modes.length ? `
    <div class="field fixed" style="min-width:220px">
      <label>Mode</label>
      <select id="${esc(p.mode_key)}">
        ${p.modes.map((m) => `<option value="${esc(m.value)}">${esc(m.label)}</option>`).join('')}
      </select>
    </div>` : '';
  const testBtn = p.test_endpoint ? `
    <div class="fixed"><button class="btn btn-outlined" data-test="${esc(p.name)}">Test connection</button></div>
    <div class="fixed" id="test-result-${esc(p.name)}"></div>` : '';
  const services = p.supports && p.supports.service_exclusions ? `
    <div class="card" data-services="${esc(p.name)}">
      <h2>${esc(p.label)} shipping services
        <button class="btn btn-outlined btn-small" data-reload-services="${esc(p.name)}" style="margin-left:auto">Reload</button>
      </h2>
      <p class="hint mb-16">Checked services are hidden from the rate list on the shipping page. Fetched live from your ${esc(p.label)} account (current mode).</p>
      <div id="services-list-${esc(p.name)}"><p class="text-secondary">Loading services…</p></div>
      <div class="row mt-16">
        <div class="fixed"><button class="btn btn-primary" data-save-services="${esc(p.name)}">Save exclusions</button></div>
        <div class="fixed" id="services-status-${esc(p.name)}"></div>
      </div>
    </div>` : '';
  const origin = p.origin_override_key ? `
    <div class="row mt-16" style="align-items:center">
      <div class="fixed"><span class="hint" id="origin-summary-${esc(p.name)}">Ship from: global origin</span></div>
      <div class="fixed"><button class="btn btn-text btn-small" data-origin-edit="${esc(p.name)}">Change</button></div>
    </div>` : '';
  const deleteAttrs = p.enabled
    ? ' disabled title="Disable this integration and save before deleting it"'
    : ' title="Remove this integration and its credentials"';
  return `
    <div class="card" data-provider="${esc(p.name)}">
      <h2>${esc(p.label)} <span class="text-secondary" style="font-weight:400">· ${esc(p.platform_label)}</span>
        <label class="svc-selectall" style="margin-left:auto"><input type="checkbox" id="${esc(p.enabled_key)}"> Enabled</label>
      </h2>
      <div class="row mb-16">
        ${modeField}${testBtn}
      </div>
      <div class="row">${p.fields.map(fieldHtml).join('')}</div>
      ${origin}
      <div class="mt-16" data-inst-users="${esc(p.name)}">
        <h4 class="mb-16">Assigned users</h4>
        <div id="inst-users-${esc(p.name)}"><p class="text-secondary">Loading users…</p></div>
      </div>
      <div class="row mt-16">
        <div class="fixed"><button class="btn btn-text btn-small" data-rename="${esc(p.name)}">Rename</button></div>
        <div class="fixed"><button class="btn btn-danger btn-small" data-delete="${esc(p.name)}"${deleteAttrs}>Delete</button></div>
      </div>
    </div>
    ${services}`;
}

function wireProviderCard(p) {
  const originEdit = document.querySelector(`[data-origin-edit="${p.name}"]`);
  if (originEdit) originEdit.addEventListener('click', () => showOriginAccount(p.name));
  // Delegated: the block's innerHTML is replaced on every loadUsers().
  document.getElementById(`inst-users-${p.name}`).addEventListener('click', (e) => {
    if (e.target.closest('[data-save-inst-users]')) saveInstanceUsers(p);
  });
  document.querySelector(`[data-rename="${p.name}"]`).addEventListener('click', () => renameInstance(p));
  document.querySelector(`[data-delete="${p.name}"]`).addEventListener('click', () => deleteInstance(p));
  if (p.test_endpoint) {
    const btn = document.querySelector(`[data-test="${p.name}"]`);
    btn.addEventListener('click', () => {
      const modeEl = document.getElementById(p.mode_key);
      const mode = modeEl ? (modeEl.value || (p.modes[0] && p.modes[0].value)) : undefined;
      const tokenField = p.fields.find((f) => f.type === 'secret' && f.mode === mode);
      const token = tokenField ? document.getElementById(tokenField.key).value : undefined;
      testResult(`test-result-${p.name}`, api(p.test_endpoint, { method: 'POST', body: { mode, token } }));
    });
  }
  if (p.supports && p.supports.service_exclusions) {
    document.querySelector(`[data-reload-services="${p.name}"]`)
      .addEventListener('click', () => loadServices(p));
    document.querySelector(`[data-save-services="${p.name}"]`)
      .addEventListener('click', () => saveServices(p));
    document.getElementById(`services-list-${p.name}`)
      .addEventListener('change', onServiceToggle);
  }
}

/* ---------- Assigned users, per account card ---------- */
function instanceUsersHtml(key, users) {
  const admins = users.filter((u) => u.role === 'admin' && u.is_active).map((u) => u.username);
  const packers = users.filter((u) => u.role !== 'admin' && u.is_active);
  const adminLine = admins.length
    ? `<p class="hint mb-16">Admins always have access: ${esc(admins.join(', '))}</p>` : '';
  if (allProviders.length <= 1) {
    return `${adminLine}<p class="text-secondary">This is the only shipping account, so every user can ship with it.</p>`;
  }
  if (!packers.length) return `${adminLine}<p class="text-secondary">No packer accounts yet — add users on the Users tab.</p>`;
  return `${adminLine}
    <div class="svc-checklist">${packers.map((u) => `
      <label class="svc-item"><input type="checkbox" class="inst-user" value="${u.id}"
        ${!u.allowed_providers || u.allowed_providers.includes(key) ? 'checked' : ''}> ${esc(u.username)}</label>`).join('')}
    </div>
    <div class="row mt-16">
      <div class="fixed"><button class="btn btn-outlined btn-small" data-save-inst-users="${esc(key)}">Save assignments</button></div>
    </div>
    <span class="hint">Unchecked users keep their other accounts. A user must keep at least one account — deactivate them to remove all access.</span>`;
}

function renderInstanceUserBlocks(users) {
  document.querySelectorAll('[data-inst-users]').forEach((block) => {
    const key = block.dataset.instUsers;
    const target = document.getElementById(`inst-users-${key}`);
    if (target) target.innerHTML = instanceUsersHtml(key, users);
  });
}

async function saveInstanceUsers(p) {
  const inst = instanceByKey[p.name];
  if (!inst) return;
  const block = document.getElementById(`inst-users-${p.name}`);
  const userIds = [...block.querySelectorAll('.inst-user:checked')].map((el) => Number(el.value));
  try {
    await api(`/api/provider-instances/${inst.id}/users`, { method: 'PUT', body: { user_ids: userIds } });
    snackbar(`${inst.label}: assignments saved`, 'success');
  } catch (err) {
    snackbar(err.message, 'error');
  }
  loadUsers(); // refreshes the Users table and every card's block from the DB
}

function renameInstance(p) {
  const inst = instanceByKey[p.name];
  if (!inst || !settingsClean()) return;
  openModal(`
    <h3>Rename integration</h3>
    <div class="field mb-16"><label>Name</label><input id="m-instance-label" value="${esc(inst.label)}" maxlength="60" autocomplete="off"></div>
    <div class="actions">
      <button class="btn btn-text" onclick="closeModal()">Cancel</button>
      <button class="btn btn-primary" id="m-instance-save">Save</button>
    </div>`);
  document.getElementById('m-instance-label').focus();
  document.getElementById('m-instance-save').addEventListener('click', async () => {
    try {
      await api(`/api/provider-instances/${inst.id}`, {
        method: 'PUT',
        body: { label: document.getElementById('m-instance-label').value },
      });
      closeModal();
      snackbar('Integration renamed', 'success');
      await refreshProviders(p.name);
    } catch (err) {
      snackbar(err.message, 'error');
    }
  });
}

function deleteInstance(p) {
  const inst = instanceByKey[p.name];
  if (!inst || !settingsClean()) return;
  openModal(`
    <h3>Delete ${esc(inst.label)}?</h3>
    <p class="text-secondary mb-16">Its credentials, service exclusions and ship-from override are removed and it disappears from the “Shipping with” selector and from users’ allowed integrations. Parcels already shipped through it keep their history, but their labels can no longer be voided or re-bought here.</p>
    <div class="actions">
      <button class="btn btn-text" onclick="closeModal()">Cancel</button>
      <button class="btn btn-danger" id="m-instance-delete">Delete</button>
    </div>`);
  document.getElementById('m-instance-delete').addEventListener('click', async () => {
    try {
      await api(`/api/provider-instances/${inst.id}`, { method: 'DELETE' });
      closeModal();
      snackbar(`${inst.label} deleted`, 'success');
      await refreshProviders();
    } catch (err) {
      snackbar(err.message, 'error');
    }
  });
}

async function loadFieldOptions(f) {
  const select = document.getElementById(f.key);
  try {
    const options = await api(f.options_endpoint);
    select.innerHTML = options
      .map((o) => `<option value="${esc(o.slug ?? o.value)}">${esc(o.name ?? o.label)}</option>`)
      .join('');
  } catch {
    select.innerHTML = '';
  }
}

/* ---------- Shipping services (rate exclusions), per provider ---------- */
async function loadServices(p) {
  const list = document.getElementById(`services-list-${p.name}`);
  list.innerHTML = '<p class="text-secondary">Loading services…</p>';
  try {
    const res = await api(p.services_endpoint);
    renderServices(list, res.services || [], new Set(res.excluded || []));
  } catch (err) {
    list.innerHTML = `<p class="text-secondary">Could not load services: ${esc(err.message)}</p>`;
  }
}

function renderServices(list, services, excluded) {
  if (!services.length) {
    list.innerHTML = '<p class="text-secondary">No services returned. Check the token and mode above.</p>';
    return;
  }
  const groups = new Map();
  services.forEach((s) => {
    const key = s.umbrella_name || 'Other';
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(s);
  });
  list.innerHTML = [...groups.entries()].map(([carrier, items]) => `
    <div class="svc-group">
      <div class="svc-group-head">
        <h4>${esc(carrier)}</h4>
        <label class="svc-selectall"><input type="checkbox" class="svc-group-toggle"> Exclude all</label>
      </div>
      <div class="svc-grid">
        ${items.map((s) => `
          <label class="svc-item">
            <input type="checkbox" class="svc-exclude" value="${esc(s.id)}" ${excluded.has(s.id) ? 'checked' : ''}>
            <span>${esc(s.name)}</span>
          </label>`).join('')}
      </div>
    </div>`).join('');
  list.querySelectorAll('.svc-group').forEach(syncGroupToggle);
}

function svcGroupBoxes(group) {
  return [...group.querySelectorAll('.svc-exclude')];
}

// Reflect the carrier's "Exclude all" checkbox from its services: checked when
// all are excluded, indeterminate when some are.
function syncGroupToggle(group) {
  const boxes = svcGroupBoxes(group);
  const checked = boxes.filter((b) => b.checked).length;
  const toggle = group.querySelector('.svc-group-toggle');
  toggle.checked = boxes.length > 0 && checked === boxes.length;
  toggle.indeterminate = checked > 0 && checked < boxes.length;
}

function onServiceToggle(e) {
  const group = e.target.closest('.svc-group');
  if (!group) return;
  if (e.target.classList.contains('svc-group-toggle')) {
    svcGroupBoxes(group).forEach((b) => { b.checked = e.target.checked; });
  } else if (e.target.classList.contains('svc-exclude')) {
    syncGroupToggle(group);
  }
}

async function saveServices(p) {
  const listEl = document.getElementById(`services-list-${p.name}`);
  const excluded = [...listEl.querySelectorAll('.svc-exclude:checked')].map((el) => el.value);
  document.getElementById(`services-status-${p.name}`).textContent = '';
  try {
    await api(p.excluded_endpoint, { method: 'POST', body: { excluded } });
    snackbar(`Saved — ${excluded.length} service${excluded.length === 1 ? '' : 's'} hidden`, 'success');
  } catch (err) {
    snackbar(err.message, 'error');
  }
}

async function loadSettings() {
  const settings = await api('/api/settings');
  SETTING_IDS.forEach((id) => {
    const el = document.getElementById(id);
    if (!el) return;
    if (el.type === 'checkbox') el.checked = settings[id] === 'true';
    else el.value = settings[id] || '';
    // A select with no matching saved value falls back to its first option.
    if (el.tagName === 'SELECT' && !el.value) el.selectedIndex = 0;
  });
  syncOriginToggles();
  syncDeleteButtons();
  renderPlRules();
}

document.getElementById('save-settings').addEventListener('click', async () => {
  const body = {};
  SETTING_IDS.forEach((id) => {
    const el = document.getElementById(id);
    if (!el) return;
    body[id] = el.type === 'checkbox' ? (el.checked ? 'true' : '') : el.value;
  });
  Object.keys(body).forEach((id) => {
    if (id.endsWith('origin_state') && body[id]) body[id] = body[id].toUpperCase();
  });
  try {
    await api('/api/settings', { method: 'PUT', body });
    snackbar('Settings saved', 'success');
    // Option lists that depend on a credential (e.g. a carrier picker) can
    // only fill once the key is saved — refresh them before restoring values.
    for (const p of renderedProviders) {
      for (const f of p.fields) {
        if (f.type === 'select' && f.options_endpoint) await loadFieldOptions(f);
      }
    }
    await loadSettings();
    setDirty(false);
    initNav('settings');
  } catch (err) {
    snackbar(err.message, 'error');
  }
});

function testResult(elId, promise) {
  const el = document.getElementById(elId);
  el.innerHTML = '<span class="spinner"></span>';
  promise
    .then((res) => {
      el.innerHTML = `<span class="chip static ok">✓ Connected${res.account ? ' — ' + esc(res.account) : ''}${res.shop ? ' — ' + esc(res.shop) : ''}</span>`;
    })
    .catch((err) => {
      el.innerHTML = `<span class="chip static err">✕ ${esc(err.message)}</span>`;
    });
}

document.getElementById('test-printer').addEventListener('click', () => {
  testResult('printer-test-result', api('/api/settings/test/printer', {
    method: 'POST',
    body: {
      host: document.getElementById('printer_host').value,
      port: document.getElementById('printer_port').value,
    },
  }).then((r) => ({ ...r, account: 'test label sent' })));
});

document.getElementById('test-shipper').addEventListener('click', () => {
  testResult('shipper-test-result', api('/api/settings/test/shipper', {
    method: 'POST',
    body: {
      host: document.getElementById('shipper_host').value,
      port: document.getElementById('shipper_port').value,
      db: document.getElementById('shipper_db').value,
      user: document.getElementById('shipper_user').value,
      password: document.getElementById('shipper_password').value,
    },
  }));
});

document.getElementById('test-browserprint').addEventListener('click', () => {
  testResult('browserprint-test-result',
    ZebraPrint.test().then(() => ({ account: 'test label sent' })));
});

/* ---------- BackOffice databases ---------- */
async function loadDbs() {
  const dbs = await api('/api/backoffice-dbs');
  const tbody = document.getElementById('dbs-body');
  if (!dbs.length) {
    tbody.innerHTML = '<tr><td colspan="6" class="text-secondary">No databases configured.</td></tr>';
    return;
  }
  tbody.innerHTML = dbs.map((d) => `<tr>
    <td><strong>${esc(d.name)}</strong></td>
    <td class="mono">${esc(d.host)}:${esc(d.port)}</td>
    <td class="mono">${esc(d.db_name)}</td>
    <td class="mono">${esc(d.prefix || '')}</td>
    <td>${d.is_active ? '<span class="chip static ok">active</span>' : '<span class="chip static">inactive</span>'}</td>
    <td>
      <button class="btn btn-text btn-small" onclick="testDb(${d.id}, this)">Test</button>
      <button class="btn btn-text btn-small" onclick="editDb(${d.id})">Edit</button>
      <button class="btn btn-danger btn-small" onclick="deleteDb(${d.id}, '${esc(d.name)}')">Delete</button>
    </td>
  </tr>`).join('');
  window._dbs = dbs;
}

window.testDb = async (id, btn) => {
  btn.disabled = true;
  try {
    await api(`/api/backoffice-dbs/${id}/test`, { method: 'POST' });
    snackbar('Connected', 'success');
  } catch (err) {
    snackbar(err.message, 'error');
  } finally {
    btn.disabled = false;
  }
};

function dbForm(dbRow) {
  openModal(`
    <h3>${dbRow ? 'Edit database' : 'Add BackOffice database'}</h3>
    <div class="field mb-16"><label>Name (shown in dropdowns)</label><input id="m-db-name" value="${dbRow ? esc(dbRow.name) : ''}"></div>
    <div class="field mb-16"><label>Host / IP</label><input id="m-db-host" value="${dbRow ? esc(dbRow.host) : ''}"></div>
    <div class="field mb-16"><label>Port</label><input id="m-db-port" value="${dbRow ? esc(dbRow.port) : '1433'}"></div>
    <div class="field mb-16"><label>Database name</label><input id="m-db-dbname" value="${dbRow ? esc(dbRow.db_name) : ''}"></div>
    <div class="field mb-16"><label>Order-number prefix (for scan auto-detect)</label><input id="m-db-prefix" autocomplete="off" value="${dbRow ? esc(dbRow.prefix || '') : ''}"></div>
    <div class="field mb-16"><label class="svc-selectall"><input type="checkbox" id="m-db-nocompany" ${dbRow && dbRow.no_company ? 'checked' : ''}> No Company — leave the customer's company name blank on shipments</label></div>
    <div class="field mb-16"><label>Username</label><input id="m-db-user" autocomplete="off" value="${dbRow ? esc(dbRow.username) : ''}"></div>
    <div class="field mb-16"><label>Password${dbRow ? ' (leave blank to keep current)' : ''}</label><input type="password" id="m-db-pass" autocomplete="off"></div>
    ${dbRow ? `<div class="field mb-16"><label>Status</label><select id="m-db-active"><option value="true" ${dbRow.is_active ? 'selected' : ''}>Active</option><option value="false" ${!dbRow.is_active ? 'selected' : ''}>Inactive</option></select></div>` : ''}
    <div class="actions">
      <button class="btn btn-text" onclick="closeModal()">Cancel</button>
      <button class="btn btn-primary" id="m-db-save">Save</button>
    </div>`);
  document.getElementById('m-db-save').addEventListener('click', async () => {
    const body = {
      name: document.getElementById('m-db-name').value,
      host: document.getElementById('m-db-host').value,
      port: document.getElementById('m-db-port').value,
      db_name: document.getElementById('m-db-dbname').value,
      username: document.getElementById('m-db-user').value,
      prefix: document.getElementById('m-db-prefix').value,
      no_company: document.getElementById('m-db-nocompany').checked,
      password: document.getElementById('m-db-pass').value,
    };
    if (dbRow) body.is_active = document.getElementById('m-db-active').value === 'true';
    try {
      if (dbRow) {
        await api(`/api/backoffice-dbs/${dbRow.id}`, { method: 'PUT', body });
      } else {
        await api('/api/backoffice-dbs', { method: 'POST', body });
      }
      closeModal();
      snackbar('Database saved', 'success');
      loadDbs();
    } catch (err) {
      snackbar(err.message, 'error');
    }
  });
}

document.getElementById('add-db').addEventListener('click', () => dbForm(null));
window.editDb = (id) => dbForm(window._dbs.find((d) => d.id === id));
window.deleteDb = (id, name) => {
  openModal(`
    <h3>Delete database</h3>
    <p>Delete "<strong>${name}</strong>"? Connections with existing shipments are deactivated instead.</p>
    <div class="actions">
      <button class="btn btn-text" onclick="closeModal()">Cancel</button>
      <button class="btn btn-danger" id="m-db-delete">Delete</button>
    </div>`);
  document.getElementById('m-db-delete').addEventListener('click', async () => {
    try {
      await api(`/api/backoffice-dbs/${id}`, { method: 'DELETE' });
      closeModal();
      loadDbs();
    } catch (err) {
      snackbar(err.message, 'error');
    }
  });
};

/* ---------- Modal helpers ---------- */
function openModal(html) {
  document.getElementById('modal').innerHTML = html;
  document.getElementById('modal-backdrop').classList.add('show');
}
function closeModal() {
  document.getElementById('modal-backdrop').classList.remove('show');
}
document.getElementById('modal-backdrop').addEventListener('click', (e) => {
  if (e.target.id === 'modal-backdrop') closeModal();
});

/* ---------- Shopify stores ---------- */
async function loadStores() {
  const stores = await api('/api/shopify-stores');
  const tbody = document.getElementById('stores-body');
  if (!stores.length) {
    tbody.innerHTML = '<tr><td colspan="5" class="text-secondary">No stores configured.</td></tr>';
    return;
  }
  tbody.innerHTML = stores.map((s) => `<tr>
    <td><strong>${esc(s.name)}</strong></td>
    <td class="mono">${esc(s.shop_domain)}</td>
    <td class="mono">${esc(s.prefix || '')}</td>
    <td>${s.is_active ? '<span class="chip static ok">active</span>' : '<span class="chip static">inactive</span>'}</td>
    <td>
      <button class="btn btn-text btn-small" onclick="testStore(${s.id}, this)">Test</button>
      <button class="btn btn-text btn-small" onclick="editStore(${s.id})">Edit</button>
      <button class="btn btn-danger btn-small" onclick="deleteStore(${s.id}, '${esc(s.name)}')">Delete</button>
    </td>
  </tr>`).join('');
  window._stores = stores;
}

window.testStore = async (id, btn) => {
  btn.disabled = true;
  try {
    const res = await api(`/api/shopify-stores/${id}/test`, { method: 'POST' });
    snackbar(`Connected to "${res.shop}"`, 'success');
  } catch (err) {
    snackbar(err.message, 'error');
  } finally {
    btn.disabled = false;
  }
};

function storeForm(store) {
  openModal(`
    <h3>${store ? 'Edit store' : 'Add Shopify store'}</h3>
    <div class="field mb-16"><label>Name</label><input id="m-store-name" value="${store ? esc(store.name) : ''}"></div>
    <div class="field mb-16"><label>Shop domain (mystore.myshopify.com)</label><input id="m-store-domain" value="${store ? esc(store.shop_domain) : ''}"></div>
    <div class="field mb-16"><label>Order-number prefix (for scan auto-detect)</label><input id="m-store-prefix" autocomplete="off" value="${store ? esc(store.prefix || '') : ''}"></div>
    <div class="field mb-16"><label class="svc-selectall"><input type="checkbox" id="m-store-nocompany" ${store && store.no_company ? 'checked' : ''}> No Company — leave the customer's company name blank on shipments</label></div>
    <div class="field mb-16"><label>Admin API access token${store ? ' (leave blank to keep current)' : ''}</label><input type="password" id="m-store-token" autocomplete="off"></div>
    ${store ? `<div class="field mb-16"><label>Status</label><select id="m-store-active"><option value="true" ${store.is_active ? 'selected' : ''}>Active</option><option value="false" ${!store.is_active ? 'selected' : ''}>Inactive</option></select></div>` : ''}
    <div class="actions">
      <button class="btn btn-text" onclick="closeModal()">Cancel</button>
      <button class="btn btn-primary" id="m-store-save">Save</button>
    </div>`);
  document.getElementById('m-store-save').addEventListener('click', async () => {
    const body = {
      name: document.getElementById('m-store-name').value,
      shop_domain: document.getElementById('m-store-domain').value,
      prefix: document.getElementById('m-store-prefix').value,
      no_company: document.getElementById('m-store-nocompany').checked,
      access_token: document.getElementById('m-store-token').value,
    };
    if (store) body.is_active = document.getElementById('m-store-active').value === 'true';
    try {
      if (store) {
        await api(`/api/shopify-stores/${store.id}`, { method: 'PUT', body });
      } else {
        await api('/api/shopify-stores', { method: 'POST', body });
      }
      closeModal();
      snackbar('Store saved', 'success');
      loadStores();
    } catch (err) {
      snackbar(err.message, 'error');
    }
  });
}

document.getElementById('add-store').addEventListener('click', () => storeForm(null));
window.editStore = (id) => storeForm(window._stores.find((s) => s.id === id));
window.deleteStore = (id, name) => {
  openModal(`
    <h3>Delete store</h3>
    <p>Delete store "<strong>${name}</strong>"? Stores with existing shipments are deactivated instead of deleted.</p>
    <div class="actions">
      <button class="btn btn-text" onclick="closeModal()">Cancel</button>
      <button class="btn btn-danger" id="m-confirm-delete">Delete</button>
    </div>`);
  document.getElementById('m-confirm-delete').addEventListener('click', async () => {
    try {
      await api(`/api/shopify-stores/${id}`, { method: 'DELETE' });
      closeModal();
      snackbar('Store deleted', 'success');
      loadStores();
    } catch (err) {
      snackbar(err.message, 'error');
    }
  });
};

/* ---------- Users ---------- */
/* Registered providers ({name, label}), for the per-user integration pickers. */
let allProviders = [];

async function loadAllProviders() {
  if (!allProviders.length) {
    try {
      allProviders = (await api('/api/providers'))
        .map((p) => ({ name: p.name, label: p.label, platform: p.platform, platform_label: p.platform_label }));
    } catch { /* pickers degrade to "All" */ }
  }
  return allProviders;
}

function providerNamesToLabels(names) {
  return names.map((n) => (allProviders.find((p) => p.name === n) || { label: n }).label);
}

/* Sections an admin grants per user; admins always have every one. */
const SECTIONS = [['picklists', 'Pick lists']];

function sectionsCell(u) {
  if (u.role === 'admin') return 'All';
  const names = SECTIONS.filter(([k]) => (u.allowed_sections || []).includes(k)).map(([, l]) => l);
  return names.length ? names.map((l) => `<span class="chip static">${esc(l)}</span>`).join(' ') : '<span class="text-secondary">—</span>';
}

function sectionChecklistHtml(checked) {
  return `<div class="field mb-16" id="m-user-sections"><label>Sections</label>
    <div class="svc-checklist">${SECTIONS.map(([k, l]) => `
      <label class="svc-item"><input type="checkbox" class="m-user-section" value="${k}" ${(checked || []).includes(k) ? 'checked' : ''}> ${l}</label>`).join('')}
    </div>
    <span class="hint">Extra pages this user may open. Admins always see every section.</span>
  </div>`;
}

function checkedSections() {
  return [...document.querySelectorAll('.m-user-section:checked')].map((el) => el.value);
}

function integrationsCell(u) {
  if (u.role === 'admin' || !u.allowed_providers || !u.allowed_providers.length) return 'All accounts';
  return providerNamesToLabels(u.allowed_providers)
    .map((l) => `<span class="chip static">${esc(l)}</span>`).join(' ');
}

/* One checkbox per configured ACCOUNT (its own API key), grouped by platform
   so two ShipStation accounts read as two distinct choices. */
function providerChecklistHtml(checkedNames) {
  const all = !checkedNames || !checkedNames.length;
  const groups = new Map();
  allProviders.forEach((p) => {
    const g = p.platform_label || p.platform || 'Other';
    if (!groups.has(g)) groups.set(g, []);
    groups.get(g).push(p);
  });
  const item = (p) => `
      <label class="svc-item"><input type="checkbox" class="m-user-provider" value="${esc(p.name)}"
        ${all || checkedNames.includes(p.name) ? 'checked' : ''}> ${esc(p.label)}${p.label !== p.platform_label ? ` <span class="text-secondary">· ${esc(p.platform_label)}</span>` : ''}</label>`;
  return `<div class="field mb-16" id="m-user-providers"><label>Allowed shipping accounts</label>
    <div class="svc-checklist">${[...groups.entries()].map(([g, items]) => `
      <div class="svc-group">
        <div class="svc-group-head"><h4>${esc(g)} account${items.length === 1 ? '' : 's'}</h4></div>
        ${items.map(item).join('')}
      </div>`).join('')}
    </div>
    <span class="hint">Each box is one account with its own API key. All checked = no restriction; a user must keep at least one.</span>
  </div>`;
}

function checkedProviders() {
  return [...document.querySelectorAll('.m-user-provider:checked')].map((el) => el.value);
}

async function loadUsers() {
  const [users] = await Promise.all([api('/api/users'), loadAllProviders()]);
  window.usersById = Object.fromEntries(users.map((u) => [u.id, u]));
  renderInstanceUserBlocks(users);
  const tbody = document.getElementById('users-body');
  tbody.innerHTML = users.map((u) => `<tr>
    <td><strong>${esc(u.username)}</strong></td>
    <td>${esc(u.role)}</td>
    <td>${integrationsCell(u)}</td>
    <td>${sectionsCell(u)}</td>
    <td>${u.is_active ? '<span class="chip static ok">active</span>' : '<span class="chip static">inactive</span>'}</td>
    <td>
      ${u.role !== 'admin' ? `<button class="btn btn-text btn-small" onclick="editUserProviders(${u.id}, '${esc(u.username)}')">Integrations</button>` : ''}
      ${u.role !== 'admin' ? `<button class="btn btn-text btn-small" onclick="editUserSections(${u.id}, '${esc(u.username)}')">Sections</button>` : ''}
      <button class="btn btn-text btn-small" onclick="resetPassword(${u.id}, '${esc(u.username)}')">Reset password</button>
      ${u.is_active
        ? `<button class="btn btn-danger btn-small" onclick="deactivateUser(${u.id}, '${esc(u.username)}')">Deactivate</button>`
        : `<button class="btn btn-text btn-small" onclick="activateUser(${u.id})">Activate</button>`}
    </td>
  </tr>`).join('');
}

window.editUserProviders = async (id, username) => {
  await loadAllProviders();
  const u = (window.usersById || {})[id] || {};
  openModal(`
    <h3>Integrations — ${esc(username)}</h3>
    ${providerChecklistHtml(u.allowed_providers || [])}
    <div class="actions">
      <button class="btn btn-text" onclick="closeModal()">Cancel</button>
      <button class="btn btn-primary" id="m-user-providers-save">Save</button>
    </div>`);
  document.getElementById('m-user-providers-save').addEventListener('click', async () => {
    if (!checkedProviders().length) {
      snackbar('Keep at least one account — to remove all access, deactivate the user', 'error');
      return;
    }
    try {
      await api(`/api/users/${id}/providers`, {
        method: 'PUT',
        body: { allowed_providers: checkedProviders() },
      });
      closeModal();
      snackbar('Integrations updated', 'success');
      loadUsers();
    } catch (err) {
      snackbar(err.message, 'error');
    }
  });
};

window.editUserSections = (id, username) => {
  const u = (window.usersById || {})[id] || {};
  openModal(`
    <h3>Sections — ${esc(username)}</h3>
    ${sectionChecklistHtml(u.allowed_sections || [])}
    <div class="actions">
      <button class="btn btn-text" onclick="closeModal()">Cancel</button>
      <button class="btn btn-primary" id="m-user-sections-save">Save</button>
    </div>`);
  document.getElementById('m-user-sections-save').addEventListener('click', async () => {
    try {
      await api(`/api/users/${id}/sections`, { method: 'PUT', body: { allowed_sections: checkedSections() } });
      closeModal();
      snackbar('Sections updated', 'success');
      loadUsers();
    } catch (err) {
      snackbar(err.message, 'error');
    }
  });
};

/* ---------- Audit log ---------- */
const AUDIT_PAGE = 100;
const AUDIT_GROUP_LABELS = {
  profit: 'Profit check', order_gate: 'Order gate', label: 'Labels', shipment: 'Shipments', manifest: 'Manifests',
  settings: 'Settings', user: 'Users', auth: 'Sign-ins', backoffice: 'BackOffice', shopify: 'Shopify',
  provider_instance: 'Providers', picklist: 'Pick lists',
};
let auditOffset = 0;

async function loadAudit(more = false) {
  const select = document.getElementById('audit-group');
  auditOffset = more ? auditOffset + AUDIT_PAGE : 0;
  let res;
  try {
    res = await api(`/api/audit?group=${encodeURIComponent(select.value)}&limit=${AUDIT_PAGE}&offset=${auditOffset}`);
  } catch (err) {
    snackbar(`Could not load the audit log: ${err.message}`, 'error');
    return;
  }
  if (select.options.length === 1) {
    res.groups.forEach((g) => {
      const opt = document.createElement('option');
      opt.value = g;
      opt.textContent = AUDIT_GROUP_LABELS[g] || g;
      select.appendChild(opt);
    });
  }
  const tbody = document.getElementById('audit-body');
  const html = res.rows.map(auditRowHtml).join('');
  if (more) tbody.insertAdjacentHTML('beforeend', html);
  else tbody.innerHTML = html || '<tr><td colspan="4" class="text-secondary">Nothing recorded yet</td></tr>';
  const shown = Math.min(auditOffset + res.rows.length, res.total);
  document.getElementById('audit-count').textContent = res.total ? `${shown} of ${res.total}` : '';
  document.getElementById('audit-more').style.display = shown < res.total ? '' : 'none';
}

function auditRowHtml(r) {
  const tone = r.action === 'profit.bypass' ? ' warn'
    : ['profit.bypass_denied', 'order_gate.refused', 'label.void'].includes(r.action) ? ' err' : '';
  return `<tr>
    <td style="white-space:nowrap">${esc(r.created_at)}</td>
    <td>${esc(r.username || '—')}</td>
    <td><span class="chip static${tone}">${esc(r.action)}</span></td>
    <td class="wrap">${auditDetailHtml(r.action, r.detail || {})}</td>
  </tr>`;
}

/* Profit overrides get a readable breakdown; everything else lists its
   detail fields as key: value. */
function auditDetailHtml(action, d) {
  if (action === 'profit.bypass') {
    const margin = d.margin_pct != null ? ` (${pct(d.margin_pct)})` : '';
    const service = [d.courier_name, d.provider].filter(Boolean).join(' · ');
    return `<strong>${esc(d.order || '')}</strong>${service ? ` · ${esc(service)}` : ''} · profit <strong>${signedMoney(d.profit)}</strong>${margin}
      <div class="text-secondary">Revenue ${money(d.revenue)} − items ${money(d.items_cost)} − label ${money(d.label_cost)}${(d.reasons || []).length ? ' — ' + d.reasons.map(esc).join('; ') : ''}</div>`;
  }
  if (action === 'order_gate.refused') {
    return `<strong>${esc(d.order || '')}</strong> · ${esc(d.label || '')} <span class="text-secondary">(${d.live ? 'Shopify live' : 'rate-time snapshot'})</span>
      <div class="text-secondary">${(d.reasons || []).map(esc).join('; ')}</div>`;
  }
  return Object.entries(d).map(([k, v]) =>
    `<span class="audit-kv"><span class="text-secondary">${esc(k)}:</span> ${esc(v != null && typeof v === 'object' ? JSON.stringify(v) : v)}</span>`).join(' ');
}

document.getElementById('audit-group').addEventListener('change', () => loadAudit());
document.getElementById('audit-more').addEventListener('click', () => loadAudit(true));

document.getElementById('add-user').addEventListener('click', async () => {
  await loadAllProviders();
  openModal(`
    <h3>Add user</h3>
    <div class="field mb-16"><label>Username</label><input id="m-user-name" autocomplete="off"></div>
    <div class="field mb-16"><label>Password</label><input type="password" id="m-user-pass" autocomplete="new-password"></div>
    <div class="field mb-16"><label>Role</label><select id="m-user-role"><option value="user">User</option><option value="admin">Admin</option></select></div>
    ${providerChecklistHtml([])}
    ${sectionChecklistHtml([])}
    <div class="actions">
      <button class="btn btn-text" onclick="closeModal()">Cancel</button>
      <button class="btn btn-primary" id="m-user-save">Create</button>
    </div>`);
  // Admins always have every integration — hide the picker for the admin role.
  const roleSel = document.getElementById('m-user-role');
  roleSel.addEventListener('change', () => {
    document.getElementById('m-user-providers').style.display = roleSel.value === 'admin' ? 'none' : '';
    document.getElementById('m-user-sections').style.display = roleSel.value === 'admin' ? 'none' : '';
  });
  document.getElementById('m-user-save').addEventListener('click', async () => {
    try {
      await api('/api/users', {
        method: 'POST',
        body: {
          username: document.getElementById('m-user-name').value,
          password: document.getElementById('m-user-pass').value,
          role: document.getElementById('m-user-role').value,
          allowed_providers: checkedProviders(),
          allowed_sections: checkedSections(),
        },
      });
      closeModal();
      snackbar('User created', 'success');
      loadUsers();
    } catch (err) {
      snackbar(err.message, 'error');
    }
  });
});

window.deactivateUser = (id, username) => {
  openModal(`
    <h3>Deactivate user</h3>
    <p>Deactivate "<strong>${username}</strong>"? They will no longer be able to sign in.</p>
    <div class="actions">
      <button class="btn btn-text" onclick="closeModal()">Cancel</button>
      <button class="btn btn-danger" id="m-confirm-deact">Deactivate</button>
    </div>`);
  document.getElementById('m-confirm-deact').addEventListener('click', async () => {
    try {
      await api(`/api/users/${id}`, { method: 'DELETE' });
      closeModal();
      loadUsers();
    } catch (err) {
      snackbar(err.message, 'error');
    }
  });
};

window.activateUser = async (id) => {
  await api(`/api/users/${id}/activate`, { method: 'POST' });
  loadUsers();
};

window.resetPassword = (id, username) => {
  openModal(`
    <h3>Reset password for ${username}</h3>
    <div class="field mb-16"><label>New password</label><input type="password" id="m-reset-pass" autocomplete="new-password"></div>
    <div class="actions">
      <button class="btn btn-text" onclick="closeModal()">Cancel</button>
      <button class="btn btn-primary" id="m-reset-save">Set password</button>
    </div>`);
  document.getElementById('m-reset-save').addEventListener('click', async () => {
    try {
      await api(`/api/users/${id}/password`, {
        method: 'PUT',
        body: { password: document.getElementById('m-reset-pass').value },
      });
      closeModal();
      snackbar('Password updated', 'success');
    } catch (err) {
      snackbar(err.message, 'error');
    }
  });
};

document.getElementById('change-my-password').addEventListener('click', async () => {
  const password = document.getElementById('my-new-password').value;
  try {
    const me = await api('/api/auth/me');
    await api(`/api/users/${me.id}/password`, {
      method: 'PUT',
      body: { password },
    });
    snackbar('Password changed', 'success');
    document.getElementById('my-new-password').value = '';
  } catch (err) {
    snackbar(err.message, 'error');
  }
});
