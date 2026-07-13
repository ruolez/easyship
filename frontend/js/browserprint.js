/* Zebra Browser Print — silent ZPL printing to a locally attached Zebra printer.
   Talks to the Browser Print agent on the client PC (http://127.0.0.1:9100).
   Loopback is exempt from mixed-content blocking, so this works from https pages.
   POST bodies are sent as plain strings (no JSON content-type) — the agent does
   not answer CORS preflights, and this matches Zebra's own SDK behavior. */
(function () {
  'use strict';

  const AGENT = 'http://127.0.0.1:9100';
  const TEST_ZPL = '^XA^CF0,40^FO50,60^FDEasyShip test label^FS^CF0,28^FO50,120^FDZebra Browser Print OK^FS^XZ';

  let cachedDevice = null;

  async function agentFetch(path, options) {
    let res;
    try {
      res = await fetch(`${AGENT}${path}`, options);
    } catch {
      throw new Error('Zebra Browser Print is not running on this computer');
    }
    if (!res.ok) throw new Error(`Zebra Browser Print error (HTTP ${res.status})`);
    return res.text();
  }

  async function getPrinter(force) {
    if (cachedDevice && !force) return cachedDevice;
    const text = await agentFetch('/default?type=printer');
    if (!text.trim()) throw new Error('No default printer set in Zebra Browser Print');
    try {
      cachedDevice = JSON.parse(text);
    } catch {
      throw new Error('Unexpected reply from Zebra Browser Print');
    }
    return cachedDevice;
  }

  async function write(device, zpl) {
    return agentFetch('/write', { method: 'POST', body: JSON.stringify({ device, data: zpl }) });
  }

  async function printZpl(zpl) {
    let device = await getPrinter();
    try {
      await write(device, zpl);
    } catch (err) {
      if (err.message.includes('not running')) throw err;
      device = await getPrinter(true); // stale device (printer re-plugged) — retry once
      await write(device, zpl);
    }
  }

  async function printLabelUrl(url) {
    const res = await fetch(url, { credentials: 'same-origin' });
    if (!res.ok) throw new Error(`Could not load label (HTTP ${res.status})`);
    const text = await res.text();
    if (!text.trimStart().startsWith('^XA')) {
      throw new Error('Label is not ZPL — set Easyship dashboard → Printing Options to ZPL 4x6');
    }
    await printZpl(text);
  }

  window.ZebraPrint = { printZpl, printLabelUrl, test: () => printZpl(TEST_ZPL) };
})();
