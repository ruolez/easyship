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
  'picklist_number_tag', 'picklist_hidden_tags', 'picklist_rules', 'picklist_presets',
];
let SETTING_IDS = [...BASE_SETTING_IDS];

initNav('settings').then(async () => {
  const isAdmin = window.currentUser && window.currentUser.role === 'admin';
  initTabs(isAdmin);
  renderAccount();
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
      <p class="hint">The Pick lists page still has its filters. Start from an example or a blank rule:</p>
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
      <span class="pl-vtab on plr-preview" title="How the preset's saved-view tab looks on the Pick lists page">${PLP_ICON}<span class="plr-preview-name">${esc(String(p.name || '').trim() || 'Unnamed')}</span></span>
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
  document.querySelectorAll('#pr-modes .pr-mode').forEach((b) => {
    b.classList.toggle('on', b.dataset.mode === mode);
    b.setAttribute('aria-checked', b.dataset.mode === mode);
  });
}

document.getElementById('print_mode').addEventListener('change', updatePrintModeUI);
/* The mode cards drive the hidden select, so Save / Discard and the unsaved bar work as before. */
document.getElementById('pr-modes').addEventListener('click', (e) => {
  const card = e.target.closest('[data-mode]');
  const select = document.getElementById('print_mode');
  if (!card || select.value === card.dataset.mode) return;
  select.value = card.dataset.mode;
  select.dispatchEvent(new Event('change'));
});

/* ---------- Box sizes ---------- */
async function loadBoxes() {
  const boxes = await api('/api/boxes');
  document.getElementById('boxes-count').textContent = boxes.length
    ? `${boxes.length} size${boxes.length === 1 ? '' : 's'} packers pick from on the Ship page, in inches`
    : 'The sizes packers pick from on the Ship page, in inches';
  const icon = '<svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M21 8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16Z"/><path d="m3.3 7 8.7 5 8.7-5"/><path d="M12 22V12"/></svg>';
  const trash = '<svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M3 6h18"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>';
  document.getElementById('boxes-body').innerHTML = boxes.length
    ? boxes.map((b) => `<div class="bx-tile">
        <span class="bx-ico">${icon}</span>
        <span class="bx-dims">${b.length} × ${b.width} × ${b.height}<span class="bx-unit">in</span></span>
        <button class="us-icon us-deact" onclick="deleteBox(${b.id})" title="Delete" aria-label="Delete ${b.length} by ${b.width} by ${b.height} box">${trash}</button>
      </div>`).join('')
    : '<div class="bx-empty">No box sizes yet. Add the boxes you ship in below.</div>';
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
const INT_ICONS = {
  edit: '<svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M21.17 6.81a1 1 0 0 0-3.99-3.99L3.84 16.17a2 2 0 0 0-.5.83l-1.32 4.35a.5.5 0 0 0 .62.62l4.35-1.32a2 2 0 0 0 .83-.5z"/></svg>',
  trash: '<svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M3 6h18"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>',
};

/* Shared row pieces for the Integrations tables (stores, BackOffice databases). */
function intNameCell(name) {
  return `<div class="us-user"><span class="us-avatar" style="--av:${usColor(name)}">${esc(name.charAt(0).toUpperCase())}</span><span class="us-name">${esc(name)}</span></div>`;
}
function intStatusCell(active) {
  return `<span class="us-status ${active ? 'on' : ''}">${active ? 'Active' : 'Inactive'}</span>`;
}
function intActionsCell(kind, item) {
  return `<td class="us-actions">
    <button class="btn btn-text btn-small" data-int="${kind}" data-act="test" data-id="${item.id}">Test</button>
    <button class="us-icon" data-int="${kind}" data-act="edit" data-id="${item.id}" title="Edit" aria-label="Edit ${plAttr(item.name)}">${INT_ICONS.edit}</button>
    <button class="us-icon us-deact" data-int="${kind}" data-act="delete" data-id="${item.id}" title="Delete" aria-label="Delete ${plAttr(item.name)}">${INT_ICONS.trash}</button>
  </td>`;
}
function intEmptyRow(cols, text) {
  return `<tr><td colspan="${cols}" class="int-empty">${text}</td></tr>`;
}

async function loadDbs() {
  const dbs = await api('/api/backoffice-dbs');
  window._dbs = dbs;
  document.getElementById('dbs-count').textContent = dbs.length
    ? `${dbs.length} database${dbs.length === 1 ? '' : 's'} · invoices, quotations and items live here`
    : 'Invoices, quotations and items live here';
  document.getElementById('dbs-body').innerHTML = dbs.length ? dbs.map((d) => `<tr class="${d.is_active ? '' : 'us-off'}">
    <td>${intNameCell(d.name)}</td>
    <td class="int-mono">${esc(d.host)}:${esc(d.port)}</td>
    <td class="int-mono">${esc(d.db_name)}</td>
    <td>${d.prefix ? `<span class="us-chip">${esc(d.prefix)}</span>` : '<span class="us-none">—</span>'}</td>
    <td>${intStatusCell(d.is_active)}</td>
    ${intActionsCell('db', d)}
  </tr>`).join('') : intEmptyRow(6, 'No BackOffice databases yet. Add one to pull invoices and write tracking back.');
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
  window._stores = stores;
  document.getElementById('stores-count').textContent = stores.length
    ? `${stores.length} store${stores.length === 1 ? '' : 's'} · orders and fulfillments come from these`
    : 'Orders and fulfillments come from these stores';
  document.getElementById('stores-body').innerHTML = stores.length ? stores.map((st) => `<tr class="${st.is_active ? '' : 'us-off'}">
    <td>${intNameCell(st.name)}</td>
    <td class="int-mono">${esc(st.shop_domain)}</td>
    <td>${st.prefix ? `<span class="us-chip">${esc(st.prefix)}</span>` : '<span class="us-none">—</span>'}</td>
    <td>${intStatusCell(st.is_active)}</td>
    ${intActionsCell('store', st)}
  </tr>`).join('') : intEmptyRow(5, 'No Shopify stores yet. Add one to pull its orders.');
}

/* Test / Edit / Delete on both Integrations tables. */
['stores-body', 'dbs-body'].forEach((id) => document.getElementById(id).addEventListener('click', (e) => {
  const btn = e.target.closest('[data-int]');
  if (!btn) return;
  const store = btn.dataset.int === 'store';
  const item = (store ? window._stores : window._dbs).find((x) => x.id === Number(btn.dataset.id));
  if (!item) return;
  if (btn.dataset.act === 'test') (store ? window.testStore : window.testDb)(item.id, btn);
  else if (btn.dataset.act === 'edit') (store ? window.editStore : window.editDb)(item.id);
  else (store ? window.deleteStore : window.deleteDb)(item.id, esc(item.name));
}));

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
  if (u.role === 'admin') return '<span class="us-chip all">All sections</span>';
  const names = SECTIONS.filter(([k]) => (u.allowed_sections || []).includes(k)).map(([, l]) => l);
  return names.length ? names.map((l) => `<span class="us-chip">${esc(l)}</span>`).join('') : '<span class="us-none">None</span>';
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
  if (u.role === 'admin' || !u.allowed_providers || !u.allowed_providers.length) return '<span class="us-chip all">All accounts</span>';
  return providerNamesToLabels(u.allowed_providers).map((l) => `<span class="us-chip">${esc(l)}</span>`).join('');
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

const US_COLORS = ['#2563eb', '#d97706', '#16a34a', '#9333ea', '#db2777', '#0891b2', '#65a30d', '#dc2626'];
const usColor = (name) => US_COLORS[[...name].reduce((n, c) => n + c.charCodeAt(0), 0) % US_COLORS.length];
const US_ICONS = {
  access: '<svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z"/><path d="m9 12 2 2 4-4"/></svg>',
  power: '<svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 2v10"/><path d="M18.4 6.6a9 9 0 1 1-12.77.04"/></svg>',
  key: '<svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="m15.5 7.5 2.3 2.3a1 1 0 0 0 1.4 0l2.1-2.1a1 1 0 0 0 0-1.4L19 4"/><path d="m21 2-9.6 9.6"/><circle cx="7.5" cy="15.5" r="5.5"/></svg>',
};

async function loadUsers() {
  const [users] = await Promise.all([api('/api/users'), loadAllProviders()]);
  window.usersById = Object.fromEntries(users.map((u) => [u.id, u]));
  renderInstanceUserBlocks(users);
  const active = users.filter((u) => u.is_active).length;
  document.getElementById('users-count').textContent = `${users.length} user${users.length === 1 ? '' : 's'}`
    + (active === users.length ? '' : ` · ${active} active`);
  document.getElementById('users-body').innerHTML = users.map((u) => `<tr class="${u.is_active ? '' : 'us-off'}">
    <td><div class="us-user">
      <span class="us-avatar" style="--av:${usColor(u.username)}">${esc(u.username.charAt(0).toUpperCase())}</span>
      <span class="us-name">${esc(u.username)}</span>${u.role === 'admin' ? '<span class="us-role">Admin</span>' : ''}</div></td>
    <td><div class="us-chips">${integrationsCell(u)}</div></td>
    <td><div class="us-chips">${sectionsCell(u)}</div></td>
    <td><span class="us-status ${u.is_active ? 'on' : ''}">${u.is_active ? 'Active' : 'Inactive'}</span></td>
    <td class="us-actions">
      ${u.role !== 'admin' ? `<button class="btn btn-text btn-small" data-user-act="access" data-id="${u.id}" title="Shipping accounts and sections">${US_ICONS.access}Access</button>` : ''}
      <button class="us-icon" data-user-act="password" data-id="${u.id}" title="Reset password" aria-label="Reset password for ${plAttr(u.username)}">${US_ICONS.key}</button>
      ${u.is_active
        ? `<button class="us-icon us-deact" data-user-act="deactivate" data-id="${u.id}" title="Deactivate" aria-label="Deactivate ${plAttr(u.username)}">${US_ICONS.power}</button>`
        : `<button class="btn btn-text btn-small us-activate" data-user-act="activate" data-id="${u.id}">Activate</button>`}
    </td>
  </tr>`).join('');
}

document.getElementById('users-body').addEventListener('click', (e) => {
  const btn = e.target.closest('[data-user-act]');
  if (!btn) return;
  const u = window.usersById[btn.dataset.id];
  if (!u) return;
  ({ access: editUserAccess, password: resetPassword, deactivate: deactivateUser, activate: activateUser })[btn.dataset.userAct](u);
});

/* Shipping accounts and sections in one window; each is saved only when it changed. */
async function editUserAccess(u) {
  await loadAllProviders();
  openModal(`
    <h3>Access — ${esc(u.username)}</h3>
    ${providerChecklistHtml(u.allowed_providers || [])}
    ${sectionChecklistHtml(u.allowed_sections || [])}
    <div class="actions">
      <button class="btn btn-text" onclick="closeModal()">Cancel</button>
      <button class="btn btn-primary" id="m-user-access-save">Save</button>
    </div>`);
  const before = { providers: checkedProviders().join(','), sections: checkedSections().join(',') };
  document.getElementById('m-user-access-save').addEventListener('click', async () => {
    if (!checkedProviders().length) {
      snackbar('Keep at least one account — to remove all access, deactivate the user', 'error');
      return;
    }
    try {
      if (checkedProviders().join(',') !== before.providers) {
        await api(`/api/users/${u.id}/providers`, { method: 'PUT', body: { allowed_providers: checkedProviders() } });
      }
      if (checkedSections().join(',') !== before.sections) {
        await api(`/api/users/${u.id}/sections`, { method: 'PUT', body: { allowed_sections: checkedSections() } });
      }
      closeModal();
      snackbar('Access updated', 'success');
      loadUsers();
    } catch (err) {
      snackbar(err.message, 'error');
      loadUsers();
    }
  });
}

/* ---------- Audit log ----------
   A feed grouped by day: each event reads as a sentence (what happened, to
   which order or setting, by whom); a click opens every detail it recorded. */
const AUDIT_PAGE = 100;
// Older settings saves logged every field, not just the changed ones.
const AUDIT_ALL_KEYS = 25;
const plural = (n, word) => `${Number(n).toLocaleString()} ${word}${n === 1 ? '' : 's'}`;
const AUDIT_GROUP_LABELS = {
  label: 'Labels', shipment: 'Shipments', manifest: 'Manifests', profit: 'Profit check', order_gate: 'Order gate',
  picklist: 'Pick lists', settings: 'Settings', user: 'Users', auth: 'Sign-ins', store: 'Stores',
  provider_instance: 'Shipping accounts', box: 'Boxes', backoffice_db: 'BackOffice', backoffice: 'BackOffice', shopify: 'Shopify',
};
const auSvg = (paths) => `<svg class="ico" viewBox="0 0 24 24" aria-hidden="true">${paths}</svg>`;
const AUDIT_ICONS = {
  label: auSvg('<path d="M12.59 2.59A2 2 0 0 0 11.17 2H4a2 2 0 0 0-2 2v7.17a2 2 0 0 0 .59 1.42l8.7 8.7a2.43 2.43 0 0 0 3.42 0l6.58-6.58a2.43 2.43 0 0 0 0-3.42z"/><circle cx="7.5" cy="7.5" r=".5" fill="currentColor"/>'),
  shipment: auSvg('<path d="M21 8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16Z"/><path d="m3.3 7 8.7 5 8.7-5"/><path d="M12 22V12"/>'),
  manifest: auSvg('<rect width="8" height="4" x="8" y="2" rx="1"/><path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"/><path d="m9 14 2 2 4-4"/>'),
  profit: auSvg('<path d="M12 2v20"/><path d="M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6"/>'),
  order_gate: auSvg('<path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z"/><path d="M12 8v4"/><path d="M12 16h.01"/>'),
  picklist: auSvg('<rect width="8" height="4" x="8" y="2" rx="1"/><path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"/><path d="M12 11h4"/><path d="M12 16h4"/><path d="M8 11h.01"/><path d="M8 16h.01"/>'),
  settings: auSvg('<path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z"/><circle cx="12" cy="12" r="3"/>'),
  user: auSvg('<path d="M19 21v-2a4 4 0 0 0-4-4H9a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/>'),
  auth: auSvg('<path d="M15 3h4a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-4"/><polyline points="10 17 15 12 10 7"/><line x1="15" x2="3" y1="12" y2="12"/>'),
  store: auSvg('<path d="m2 7 4.41-4.41A2 2 0 0 1 7.83 2h8.34a2 2 0 0 1 1.42.59L22 7"/><path d="M4 12v8a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-8"/><path d="M2 7h20"/><path d="M15 22v-4a2 2 0 0 0-2-2h-2a2 2 0 0 0-2 2v4"/>'),
  provider_instance: auSvg('<path d="M12 22v-5"/><path d="M9 8V2"/><path d="M15 8V2"/><path d="M18 8v5a4 4 0 0 1-4 4h-4a4 4 0 0 1-4-4V8Z"/>'),
  box: auSvg('<path d="M21 8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16Z"/>'),
  backoffice_db: auSvg('<ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M3 5v14a9 3 0 0 0 18 0V5"/><path d="M3 12a9 3 0 0 0 18 0"/>'),
  chevron: auSvg('<path d="m9 18 6-6-6-6"/>'),
};
AUDIT_ICONS.backoffice = AUDIT_ICONS.backoffice_db;
AUDIT_ICONS.shopify = AUDIT_ICONS.store;

let auditOffset = 0;
let auditRows = [];
let auditShipments = {};
let auditTimer;
const auditOpen = new Set();

const AUDIT_KEY_LABELS = { shipment_ids: 'Shipments', shipment_id: 'Shipment', user_ids: 'Users', group_id: 'Label group' };
const humanKey = (k) => {
  if (AUDIT_KEY_LABELS[k]) return AUDIT_KEY_LABELS[k];
  const words = String(k).replace(/_/g, ' ').replace(/\bid\b/g, 'ID').replace(/\bids\b/g, 'IDs').trim();
  return words.charAt(0).toUpperCase() + words.slice(1);
};
const auList = (items, max = 3) => (items.length > max ? `${items.slice(0, max).join(', ')} +${items.length - max}` : items.join(', '));
const auUser = (id) => ((window.usersById || {})[id] || {}).username || `user #${id}`;
const auStore = (id) => ((window._stores || []).find((s) => s.id === id) || {}).name || `store #${id}`;

function auShipments(d) {
  const ids = (d.shipment_ids || []).concat(d.shipment_id != null ? [d.shipment_id] : []);
  return ids.map((id) => auditShipments[String(id)]).filter(Boolean);
}

/* What the event was, in a few words, and the one line that says to what. */
function auditSummary(r) {
  const d = r.detail || {};
  const ships = auShipments(d);
  const orders = [...new Set(ships.map((s) => s.order).filter(Boolean))];
  const couriers = [...new Set(ships.map((s) => s.courier).filter(Boolean))];
  const cost = ships.reduce((n, s) => n + (s.cost || 0), 0);
  const boxes = d.boxes > 1 ? `${d.boxes} boxes` : '';
  const S = (title, parts = [], tone = '') => ({ title, summary: parts.filter((p) => p !== '' && p != null).join(' · '), tone });
  switch (r.action) {
    case 'label.buy': return S('Bought a label', [auList(orders, 2), auList(couriers, 1), boxes, cost ? money(cost) : '']);
    case 'label.void': return S(d.undo ? 'Undid a label purchase' : 'Voided a label', [auList(orders, 2), boxes], 'warn');
    case 'label.print': return S('Printed a label', [auList(orders, 2)]);
    case 'manifest.create': return S('Created a manifest', [d.provider, d.shipments != null ? plural(d.shipments, 'shipment') : '']);
    case 'shipment.link_shopify': return S('Linked a shipment to Shopify', [d.order_name, d.store_id ? auStore(d.store_id) : '']);
    case 'profit.bypass': return S('Overrode the profit check', [d.order, d.profit != null ? `profit ${signedMoney(d.profit)}` : '', d.margin_pct != null ? pct(d.margin_pct) : '', d.courier_name], 'warn');
    case 'profit.bypass_denied': return S('Profit override refused', [d.order, 'wrong password'], 'err');
    case 'order_gate.refused': return S('Label purchase blocked', [d.order, d.label], 'err');
    case 'settings.update': {
      const keys = d.keys || [];
      if (keys.length > AUDIT_ALL_KEYS) return S('Saved settings', ['every field (logged before changes were tracked)']);
      return S('Changed settings', [keys.length ? auList(keys.map(humanKey), 3) : 'nothing changed']);
    }
    case 'settings.excluded_services': return S('Changed hidden services', [d.provider, d.count != null ? `${d.count} hidden` : '']);
    case 'user.create': return S('Added a user', [d.username, d.role === 'admin' ? 'admin' : '']);
    case 'user.deactivate': return S('Deactivated a user', [auUser(d.id)], 'warn');
    case 'user.activate': return S('Reactivated a user', [auUser(d.id)]);
    case 'user.password_change': return S('Changed a password', [auUser(d.id)]);
    case 'user.providers': return S('Changed shipping accounts', [auUser(d.id), (d.allowed_providers || []).length ? auList(providerNamesToLabels(d.allowed_providers), 3) : 'all accounts']);
    case 'user.sections': return S('Changed sections', [auUser(d.id), (d.allowed_sections || []).length ? d.allowed_sections.map(humanKey).join(', ') : 'none']);
    case 'store.create': return S('Added a store', [d.name, d.domain]);
    case 'store.update': return S('Updated a store', [d.name || auStore(d.id)]);
    case 'store.delete': return S('Removed a store', [auStore(d.id)], 'warn');
    case 'provider_instance.create': return S('Added a shipping account', [d.label || d.key]);
    case 'provider_instance.rename': return S('Renamed a shipping account', [d.label || d.key]);
    case 'provider_instance.delete': return S('Removed a shipping account', [d.label || d.key], 'warn');
    case 'provider_instance.users': return S('Changed who can use a shipping account', [d.label || d.key, (d.user_ids || []).length ? auList(d.user_ids.map(auUser), 3) : 'everyone']);
    case 'box.create': return S('Added a box', [d.name]);
    case 'box.delete': return S('Removed a box', [d.name || `box #${d.id}`]);
    case 'backoffice_db.create': return S('Added a BackOffice database', [d.name]);
    case 'backoffice_db.update': return S('Updated a BackOffice database', [d.name || `#${d.id}`]);
    case 'backoffice_db.delete': return S('Removed a BackOffice database', [d.name || `#${d.id}`], 'warn');
    case 'picklist.create': return S(`Created pick list #${d.picklist}`, [d.quotation, d.orders != null ? plural(d.orders, 'order') : '', d.units != null ? plural(d.units, 'unit') : '']);
    case 'picklist.cancel': return S(`Cancelled pick list #${d.picklist}`, [d.quotation, d.quotation_deleted ? 'quotation deleted' : ''], 'warn');
    case 'picklist.failed': return S(`Pick list #${d.picklist} failed`, [d.error], 'err');
    case 'picklist.tags':
      if (d.picklist) {
        const failed = (d.failed || []).length;
        return S(d.mode === 'remove' ? `Removed pick list #${d.picklist}'s tag` : `Tagged pick list #${d.picklist}'s orders`,
          [d.tag, d.done != null ? plural(d.done, 'order') : '', failed ? `${failed} failed` : ''], failed ? 'warn' : '');
      }
      return S(`Changed ${d.kind === 'customer' ? 'customer' : 'order'} tags`, [d.order,
        ...(d.added || []).map((t) => `+${t}`), ...(d.removed || []).map((t) => `−${t}`)]);
    case 'auth.login': return S('Signed in');
    default: {
      const parts = Object.entries(d).filter(([, v]) => v != null && typeof v !== 'object').slice(0, 3).map(([k, v]) => `${humanKey(k)} ${v}`);
      return S(humanKey(r.action.replace('.', ' ')), parts);
    }
  }
}

const MONEY_KEYS = new Set(['revenue', 'items_cost', 'label_cost', 'profit', 'shipping_cost', 'cost']);

/* Every field the event recorded, readable: shipments as their orders, lists as chips, amounts as money. */
function auditDetailHtml(r) {
  const d = r.detail || {};
  const value = (k, v) => {
    if (v == null || v === '') return '<span class="text-secondary">—</span>';
    if ((k === 'shipment_ids' || k === 'shipment_id') && Object.keys(auditShipments).length) {
      return `<div class="au-ships">${[].concat(v).map((id) => {
        const s = auditShipments[String(id)];
        return s ? `<span class="au-ship"><strong>${esc(s.order || `#${id}`)}</strong>${s.courier ? ` · ${esc(s.courier)}` : ''}${s.tracking ? ` · <span class="au-mono">${esc(s.tracking)}</span>` : ''}${s.cost != null ? ` · ${money(s.cost)}` : ''}${s.status === 'voided' ? ' · <span class="au-voided">voided</span>' : ''}</span>`
          : `<span class="au-ship">Shipment #${esc(id)}</span>`;
      }).join('')}</div>`;
    }
    if (k === 'keys' && Array.isArray(v) && v.length > AUDIT_ALL_KEYS) return `<span class="text-secondary">All ${v.length} fields — this save was logged before EasyShip tracked which ones changed</span>`;
    if (k === 'keys' && Array.isArray(v)) return v.length ? `<div class="us-chips">${v.map((x) => `<span class="us-chip">${esc(humanKey(x))}</span>`).join('')}</div>` : '<span class="text-secondary">Nothing changed</span>';
    if (MONEY_KEYS.has(k) && typeof v === 'number') return k === 'profit' ? signedMoney(v) : money(v);
    if (k === 'margin_pct' && typeof v === 'number') return pct(v);
    if (typeof v === 'boolean') return v ? 'Yes' : 'No';
    if (Array.isArray(v)) return v.length ? `<div class="us-chips">${v.map((x) => `<span class="us-chip">${esc(typeof x === 'object' ? JSON.stringify(x) : x)}</span>`).join('')}</div>` : '<span class="text-secondary">None</span>';
    if (typeof v === 'object') return `<code class="au-json">${esc(JSON.stringify(v, null, 1))}</code>`;
    return /^[0-9a-f]{20,}$/i.test(String(v)) ? `<span class="au-mono">${esc(v)}</span>` : esc(v);
  };
  const fields = Object.entries(d).map(([k, v]) => `<div class="au-k">${esc(humanKey(k))}</div><div class="au-v">${value(k, v)}</div>`).join('');
  return `<div class="au-grid">${fields}
    <div class="au-k">When</div><div class="au-v">${esc(r.created_at)} <span class="text-secondary">(Central)</span></div>
    <div class="au-k">Event</div><div class="au-v"><span class="au-mono">${esc(r.action)}</span> <span class="text-secondary">#${r.id}</span></div></div>`;
}

function auditDayLabel(iso) {
  const day = new Date(`${iso}T12:00:00`);
  const today = new Date();
  today.setHours(12, 0, 0, 0);
  const diff = Math.round((today - day) / 86400000);
  const long = day.toLocaleDateString('en-US', { weekday: 'short', month: 'short', day: 'numeric', year: day.getFullYear() === today.getFullYear() ? undefined : 'numeric' });
  return diff === 0 ? `Today <span>${long}</span>` : diff === 1 ? `Yesterday <span>${long}</span>` : long;
}

function auditRowHtml(r) {
  const group = r.action.split('.')[0];
  const s = auditSummary(r);
  const open = auditOpen.has(r.id);
  const time = (r.created_at || '').split(' ').slice(1).join(' ').replace(/^0/, '');
  const who = r.username
    ? `<span class="us-avatar au-av" style="--av:${usColor(r.username)}">${esc(r.username.charAt(0).toUpperCase())}</span>${esc(r.username)}`
    : '<span class="text-secondary">System</span>';
  return `<div class="au-row${open ? ' open' : ''}${s.tone ? ` ${s.tone}` : ''}" data-audit="${r.id}" role="button" tabindex="0" aria-expanded="${open}">
      <span class="au-time">${esc(time)}</span>
      <span class="au-who">${who}</span>
      <span class="au-ico g-${esc(group)}">${AUDIT_ICONS[group] || AUDIT_ICONS.settings}</span>
      <span class="au-text"><strong>${esc(s.title)}</strong>${s.summary ? `<span class="au-sum">${esc(s.summary)}</span>` : ''}</span>
      <span class="au-chev">${AUDIT_ICONS.chevron}</span>
    </div>
    ${open ? `<div class="au-detail">${auditDetailHtml(r)}</div>` : ''}`;
}

function renderAuditFeed() {
  const feed = document.getElementById('audit-feed');
  if (!auditRows.length) {
    feed.innerHTML = `<div class="au-empty">${auditFiltered() ? 'No events match these filters.' : 'Nothing recorded yet.'}</div>`;
    return;
  }
  let day = null;
  feed.innerHTML = auditRows.map((r) => {
    const head = r.date !== day ? `<div class="au-day">${auditDayLabel(r.date)}</div>` : '';
    day = r.date;
    return head + auditRowHtml(r);
  }).join('');
}

const auditGroup = () => (document.querySelector('#audit-groups .on') || {}).dataset?.group || '';
const auditFiltered = () => auditGroup() || document.getElementById('audit-q').value.trim()
  || document.getElementById('audit-user').value || document.getElementById('audit-days').value;

async function loadAudit(more = false) {
  auditOffset = more ? auditOffset + AUDIT_PAGE : 0;
  const params = new URLSearchParams({ limit: AUDIT_PAGE, offset: auditOffset });
  const filters = { group: auditGroup(), q: document.getElementById('audit-q').value.trim(),
    user: document.getElementById('audit-user').value, days: document.getElementById('audit-days').value };
  Object.entries(filters).forEach(([k, v]) => { if (v) params.set(k, v); });
  let res;
  try {
    res = await api(`/api/audit?${params}`);
  } catch (err) {
    snackbar(`Could not load the audit log: ${err.message}`, 'error');
    return;
  }
  if (!document.querySelector('#audit-groups button')) {
    document.getElementById('audit-groups').innerHTML = [['', 'Everything'], ...res.groups.map((g) => [g, AUDIT_GROUP_LABELS[g] || humanKey(g)])]
      .map(([g, label]) => `<button class="au-group${g === '' ? ' on' : ''}" data-group="${esc(g)}" role="tab" aria-selected="${g === ''}">${esc(label)}</button>`).join('');
  }
  const userSel = document.getElementById('audit-user');
  if (userSel.options.length === 1) {
    res.users.forEach((u) => userSel.insertAdjacentHTML('beforeend', `<option value="${plAttr(u)}">${esc(u)}</option>`));
  }
  auditShipments = more ? { ...auditShipments, ...res.shipments } : res.shipments;
  auditRows = more ? auditRows.concat(res.rows) : res.rows;
  if (!more) auditOpen.clear();
  renderAuditFeed();
  document.getElementById('audit-count').textContent = res.total ? `${res.total.toLocaleString()} event${res.total === 1 ? '' : 's'}` : '';
  document.getElementById('audit-shown').textContent = res.total ? `Showing ${auditRows.length.toLocaleString()} of ${res.total.toLocaleString()}` : '';
  document.getElementById('audit-more').hidden = auditRows.length >= res.total;
  document.getElementById('audit-reset').hidden = !auditFiltered();
}

document.getElementById('audit-groups').addEventListener('click', (e) => {
  const btn = e.target.closest('[data-group]');
  if (!btn) return;
  document.querySelectorAll('#audit-groups .au-group').forEach((b) => {
    b.classList.toggle('on', b === btn);
    b.setAttribute('aria-selected', b === btn);
  });
  loadAudit();
});
document.getElementById('audit-q').addEventListener('input', () => {
  clearTimeout(auditTimer);
  auditTimer = setTimeout(() => loadAudit(), 300);
});
['audit-user', 'audit-days'].forEach((id) => document.getElementById(id).addEventListener('change', () => loadAudit()));
document.getElementById('audit-reset').addEventListener('click', () => {
  document.getElementById('audit-q').value = '';
  document.getElementById('audit-user').value = '';
  document.getElementById('audit-days').value = '';
  document.querySelectorAll('#audit-groups .au-group').forEach((b, i) => b.classList.toggle('on', i === 0));
  loadAudit();
});
document.getElementById('audit-more').addEventListener('click', () => loadAudit(true));
function toggleAuditRow(row) {
  const id = Number(row.dataset.audit);
  if (auditOpen.has(id)) auditOpen.delete(id);
  else auditOpen.add(id);
  const r = auditRows.find((x) => x.id === id);
  const next = row.nextElementSibling;
  if (next && next.classList.contains('au-detail')) next.remove();
  row.outerHTML = auditRowHtml(r);
}
document.getElementById('audit-feed').addEventListener('click', (e) => {
  const row = e.target.closest('[data-audit]');
  if (row && !window.getSelection().toString()) toggleAuditRow(row);
});
document.getElementById('audit-feed').addEventListener('keydown', (e) => {
  const row = e.target.closest('[data-audit]');
  if (!row || (e.key !== 'Enter' && e.key !== ' ')) return;
  e.preventDefault();
  const id = row.dataset.audit;
  toggleAuditRow(row);
  document.querySelector(`[data-audit="${id}"]`).focus();
});

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

function deactivateUser(u) {
  openModal(`
    <h3>Deactivate user</h3>
    <p>Deactivate “<strong>${esc(u.username)}</strong>”? They will no longer be able to sign in.</p>
    <div class="actions">
      <button class="btn btn-text" onclick="closeModal()">Cancel</button>
      <button class="btn btn-danger" id="m-confirm-deact">Deactivate</button>
    </div>`);
  document.getElementById('m-confirm-deact').addEventListener('click', async () => {
    try {
      await api(`/api/users/${u.id}`, { method: 'DELETE' });
      closeModal();
      loadUsers();
    } catch (err) {
      snackbar(err.message, 'error');
    }
  });
}

async function activateUser(u) {
  try {
    await api(`/api/users/${u.id}/activate`, { method: 'POST' });
  } catch (err) {
    snackbar(err.message, 'error');
  }
  loadUsers();
}

function resetPassword(u) {
  openModal(`
    <h3>Reset password for ${esc(u.username)}</h3>
    <div class="field mb-16"><label>New password</label><input type="password" id="m-reset-pass" autocomplete="new-password"></div>
    <div class="actions">
      <button class="btn btn-text" onclick="closeModal()">Cancel</button>
      <button class="btn btn-primary" id="m-reset-save">Set password</button>
    </div>`);
  document.getElementById('m-reset-pass').focus();
  document.getElementById('m-reset-save').addEventListener('click', async () => {
    try {
      await api(`/api/users/${u.id}/password`, {
        method: 'PUT',
        body: { password: document.getElementById('m-reset-pass').value },
      });
      closeModal();
      snackbar('Password updated', 'success');
    } catch (err) {
      snackbar(err.message, 'error');
    }
  });
}

/* ---------- My account ---------- */
const MIN_PASSWORD = 4;

function renderAccount() {
  const me = window.currentUser;
  if (!me) return;
  const admin = me.role === 'admin';
  const avatar = document.getElementById('ac-avatar');
  avatar.textContent = me.username.charAt(0).toUpperCase();
  avatar.style.setProperty('--av', usColor(me.username));
  document.getElementById('ac-name').textContent = me.username;
  document.getElementById('ac-username').value = me.username;
  document.getElementById('ac-role').hidden = !admin;
  const sections = SECTIONS.filter(([k]) => (me.sections || []).includes(k)).map(([, l]) => l);
  document.getElementById('ac-meta').textContent = admin
    ? 'Administrator · every page and setting'
    : `Packer${sections.length ? ` · also ${sections.join(', ')}` : ''}`;
}

function checkPasswordForm() {
  const pass = document.getElementById('my-new-password').value;
  const confirm = document.getElementById('my-confirm-password').value;
  const checks = { length: pass.length >= MIN_PASSWORD, match: !!pass && pass === confirm };
  document.querySelectorAll('#ac-checks [data-check]').forEach((li) => {
    li.classList.toggle('ok', checks[li.dataset.check]);
    li.classList.toggle('bad', li.dataset.check === 'match' && !!confirm && !checks.match);
  });
  document.getElementById('change-my-password').disabled = !(checks.length && checks.match);
}

['my-new-password', 'my-confirm-password'].forEach((id) => document.getElementById(id).addEventListener('input', checkPasswordForm));

document.querySelectorAll('[data-eye]').forEach((btn) => btn.addEventListener('click', () => {
  const input = document.getElementById(btn.dataset.eye);
  const show = input.type === 'password';
  input.type = show ? 'text' : 'password';
  btn.classList.toggle('on', show);
  btn.setAttribute('aria-label', show ? 'Hide password' : 'Show password');
  btn.title = show ? 'Hide password' : 'Show password';
}));

document.getElementById('ac-password-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const btn = document.getElementById('change-my-password');
  if (btn.disabled) return;
  btn.disabled = true;
  try {
    await api(`/api/users/${window.currentUser.id}/password`, {
      method: 'PUT',
      body: { password: document.getElementById('my-new-password').value },
    });
    snackbar('Password changed', 'success');
    ['my-new-password', 'my-confirm-password'].forEach((id) => { document.getElementById(id).value = ''; });
  } catch (err) {
    snackbar(err.message, 'error');
  }
  checkPasswordForm();
});

document.getElementById('ac-signout').addEventListener('click', async () => {
  await api('/api/auth/logout', { method: 'POST' });
  location.href = '/login.html';
});
