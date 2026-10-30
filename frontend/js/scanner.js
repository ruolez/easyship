/* Barcode scanner input (keyboard wedge): the scanner types the code and
   presses Enter (or Tab). A hidden input keeps the focus so no scan is lost
   and, with inputmode="none", the phone keyboard stays down. Keys that land
   on something else (a button just tapped) are buffered and still count.
   Real fields (a dialog's input) keep their keys.

   createScanCapture({ onScan }) → { focus, setEnabled } */
function createScanCapture({ onScan }) {
  const BUFFER_IDLE_MS = 1000;
  const input = document.createElement('input');
  input.className = 'scan-capture';
  input.setAttribute('inputmode', 'none');
  input.setAttribute('autocomplete', 'off');
  input.setAttribute('autocorrect', 'off');
  input.setAttribute('autocapitalize', 'off');
  input.setAttribute('spellcheck', 'false');
  input.setAttribute('aria-label', 'Scanner input');
  input.tabIndex = -1;
  document.body.appendChild(input);

  let enabled = true;
  let buffer = '';
  let lastKeyAt = 0;

  const isField = (el) => el && el !== input
    && (el.matches('input, textarea, select') || el.isContentEditable);

  function focus() {
    if (!enabled || isField(document.activeElement) || document.activeElement === input) return;
    input.focus({ preventScroll: true });
  }

  function take() {
    const code = (buffer + input.value).trim();
    buffer = '';
    input.value = '';
    return code;
  }

  document.addEventListener('keydown', (e) => {
    if (!enabled || isField(e.target)) return;
    const now = Date.now();
    if (now - lastKeyAt > BUFFER_IDLE_MS) buffer = '';
    lastKeyAt = now;
    if (e.key === 'Enter' || e.key === 'Tab') {
      const code = take();
      if (code) {
        e.preventDefault();
        onScan(code);
      } else if (e.key === 'Tab') {
        e.preventDefault();
      }
      return;
    }
    // Keys on the hidden input type into it (some Android scanners only fill
    // its value); anywhere else they are buffered.
    if (e.target !== input && e.key.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey) {
      buffer += e.key;
      e.preventDefault();
      focus();
    }
  }, true);

  input.addEventListener('blur', () => setTimeout(focus, 0));
  document.addEventListener('pointerup', () => setTimeout(focus, 0));
  window.addEventListener('focus', () => setTimeout(focus, 0));

  return {
    focus,
    setEnabled(value) {
      enabled = value;
      if (value) focus();
    },
  };
}
