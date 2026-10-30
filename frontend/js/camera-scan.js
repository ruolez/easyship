/* Camera barcode scanning with the browser's own BarcodeDetector (Chrome on
   Android). Where the browser has none, CameraScan.supported is false and
   the page hides its camera buttons. Needs HTTPS. One stream per opening;
   the same code is reported again only after it left the view for a moment.

   CameraScan.open({ onCode, canScan }) — onCode(code) for each read; canScan()
   false pauses reading (an error is on screen). CameraScan.close(). */
const CameraScan = (() => {
  const WANTED = ['ean_13', 'ean_8', 'upc_a', 'upc_e', 'code_128', 'code_39', 'code_93', 'itf', 'codabar', 'qr_code', 'data_matrix'];
  const INTERVAL_MS = 120;
  const REPEAT_MS = 1500;
  const supported = 'BarcodeDetector' in window && !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia);
  const view = () => document.getElementById('vf-camera-view');
  const video = () => document.getElementById('vf-video');
  let stream = null;
  let timer = null;
  let detector = null;
  let last = { code: '', at: 0 };
  let torchOn = false;

  /* UPC-E as printed (8 digits) → the UPC-A it stands for, so it matches
     the 12-digit barcode stored on the product. */
  function upcEToA(code) {
    if (!/^[01]\d{7}$/.test(code)) return code;
    const [s, d1, d2, d3, d4, d5, d6, check] = code.split('');
    let body;
    if ('012'.includes(d6)) body = `${d1}${d2}${d6}0000${d3}${d4}${d5}`;
    else if (d6 === '3') body = `${d1}${d2}${d3}00000${d4}${d5}`;
    else if (d6 === '4') body = `${d1}${d2}${d3}${d4}00000${d5}`;
    else body = `${d1}${d2}${d3}${d4}${d5}0000${d6}`;
    return `${s}${body}${check}`;
  }

  async function makeDetector() {
    let formats = WANTED;
    try {
      const available = await BarcodeDetector.getSupportedFormats();
      formats = WANTED.filter((f) => available.includes(f));
    } catch { /* use the wanted list */ }
    return new BarcodeDetector({ formats });
  }

  async function open({ onCode, canScan }) {
    if (!supported) throw new Error('This browser cannot read barcodes with the camera');
    detector = detector || await makeDetector();
    stream = await navigator.mediaDevices.getUserMedia({
      audio: false,
      video: { facingMode: { ideal: 'environment' }, width: { ideal: 1280 }, height: { ideal: 720 } },
    });
    const v = video();
    v.srcObject = stream;
    view().hidden = false;
    await v.play();
    const track = stream.getVideoTracks()[0];
    const caps = track.getCapabilities ? track.getCapabilities() : {};
    const torch = document.getElementById('vf-torch');
    torch.hidden = !caps.torch;
    torchOn = false;
    torch.onclick = async () => {
      torchOn = !torchOn;
      try { await track.applyConstraints({ advanced: [{ torch: torchOn }] }); } catch { torchOn = false; }
      torch.classList.toggle('on', torchOn);
    };
    const tick = async () => {
      if (!stream) return;
      if (canScan() && v.readyState >= 2) {
        try {
          const found = await detector.detect(v);
          const now = Date.now();
          if (found.length) {
            const hit = found[0];
            const code = hit.format === 'upc_e' ? upcEToA(hit.rawValue) : hit.rawValue;
            if (code !== last.code || now - last.at > REPEAT_MS) onCode(code);
            last = { code, at: now };
          } else if (now - last.at > 400) {
            last = { code: '', at: 0 };
          }
        } catch { /* a frame that could not be read */ }
      }
      timer = setTimeout(tick, INTERVAL_MS);
    };
    tick();
  }

  function close() {
    clearTimeout(timer);
    timer = null;
    if (stream) stream.getTracks().forEach((t) => t.stop());
    stream = null;
    const v = video();
    if (v) v.srcObject = null;
    if (view()) view().hidden = true;
    last = { code: '', at: 0 };
  }

  return { supported, open, close, isOpen: () => !!stream, upcEToA };
})();
