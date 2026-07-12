/* USB HID scale via WebHID — live-fills the focused .p-weight input on the ship page.
   Primary device: Fairbanks Ultegra (vendor 0x0B67); any HID scale (usage page 0x8D) works.
   Requires a secure context (https:// or localhost) and a Chromium browser. */
(function () {
  'use strict';

  const SCALE_USAGE_PAGE = 0x8d;
  const FAIRBANKS_VENDOR = 0x0b67;
  const UNIT_TO_LB = { 2: 1 / 453.59237, 3: 2.2046226218, 11: 1 / 16, 12: 1 }; // g, kg, oz, lb

  let device = null;
  let activeField = null;
  let manualOverride = false;

  const widget = document.getElementById('scale-widget');
  const chip = document.getElementById('scale-chip');
  const btn = document.getElementById('scale-connect');
  const list = document.getElementById('parcel-list');

  const isScale = (d) => d.collections.some((c) => c.usagePage === SCALE_USAGE_PAGE);

  function pick(devices) {
    const scales = devices.filter(isScale);
    return scales.find((d) => d.vendorId === FAIRBANKS_VENDOR) || scales[0] || null;
  }

  function setChip(cls, text) {
    chip.className = `chip static ${cls}`;
    chip.textContent = text;
    chip.style.display = '';
  }

  /* Input report per HID POS spec: [status, unit, exponent(int8), weightLo, weightHi].
     WebHID strips the report ID into e.reportId. */
  function onReport(e) {
    const b = e.data;
    if (b.byteLength < 5) return;
    const status = b.getUint8(0); // 2 stable-zero, 3 in motion, 4 stable, 5 under zero, 6+ fault
    const toLb = UNIT_TO_LB[b.getUint8(1)];
    if (toLb === undefined) return;
    const lb = b.getUint16(3, true) * Math.pow(10, b.getInt8(2)) * toLb;

    if (status === 5) setChip('warn', 'Scale below zero — re-zero it');
    else if (status >= 6) setChip('err', 'Scale fault');
    else if (status === 3) setChip('warn', `Weighing… ${lb.toFixed(1)} lb`);
    else setChip('ok', `Scale · ${lb.toFixed(1)} lb`);

    if (status === 4 && lb > 0) fill(lb);
  }

  function fill(lb) {
    if (!activeField || manualOverride || !activeField.isConnected) return;
    const v = lb.toFixed(1);
    if (activeField.value === v) return;
    activeField.value = v;
    activeField.dispatchEvent(new Event('input', { bubbles: true }));
  }

  list.addEventListener('focusin', (e) => {
    if (e.target.classList.contains('p-weight')) {
      activeField = e.target;
      manualOverride = false;
    }
  });
  list.addEventListener('focusout', (e) => {
    if (e.target === activeField) activeField = null;
  });
  list.addEventListener('keydown', (e) => {
    if (e.target === activeField && e.key !== 'Enter' && e.key !== 'Tab') manualOverride = true;
  });

  async function attach(d) {
    if (!d) {
      btn.style.display = '';
      setChip('warn', 'Scale not connected');
      return;
    }
    try {
      if (!d.opened) await d.open();
    } catch (err) {
      btn.style.display = '';
      setChip('err', `Scale: ${err.message}`);
      return;
    }
    device = d;
    device.addEventListener('inputreport', onReport);
    btn.style.display = 'none';
    setChip('ok', 'Scale ready');
  }

  async function init() {
    if (!widget || !list) return;
    if (!('hid' in navigator)) {
      if (!window.isSecureContext) {
        widget.style.display = '';
        setChip('warn', 'Scale needs https://');
      }
      return;
    }
    widget.style.display = '';
    navigator.hid.addEventListener('disconnect', (e) => {
      if (e.device !== device) return;
      device = null;
      btn.style.display = '';
      setChip('err', 'Scale unplugged');
    });
    navigator.hid.addEventListener('connect', async (e) => {
      if (!device && isScale(e.device)) attach(pick(await navigator.hid.getDevices()));
    });
    attach(pick(await navigator.hid.getDevices()));
  }

  if (btn) {
    btn.addEventListener('click', async () => {
      try {
        const chosen = await navigator.hid.requestDevice({
          filters: [{ usagePage: SCALE_USAGE_PAGE }],
        });
        if (!chosen.length) return;
        attach(pick(await navigator.hid.getDevices()));
      } catch (err) {
        snackbar(`Scale: ${err.message}`, 'error');
      }
    });
  }

  init();
  window.Scale = { get device() { return device; }, _onReport: onReport };
})();
