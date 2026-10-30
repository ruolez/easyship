/* Scan feedback for packers: tones, vibration and keeping the screen awake.

   Tones are generated (no files to load). Browsers only allow sound after the
   user has touched the page or pressed a key, so unlock() runs on every tap
   and scan; a hardware scanner's keystrokes count. Vibration works on Android
   only. Sound can be muted per device (localStorage). */
const Feedback = (() => {
  const MUTE_KEY = 'easyship.verifyMuted';
  // [frequency Hz, start s, duration s, wave]
  const SOUNDS = {
    ok: [[1900, 0, 0.07, 'sine']],
    line: [[1500, 0, 0.07, 'sine'], [2100, 0.09, 0.1, 'sine']],
    done: [[1047, 0, 0.12, 'sine'], [1319, 0.13, 0.12, 'sine'], [1568, 0.26, 0.26, 'sine']],
    wrong: [[180, 0, 0.45, 'square']],
    over: [[300, 0, 0.14, 'square'], [220, 0.2, 0.2, 'square']],
    blocked: [[150, 0, 0.12, 'square']],
    undo: [[900, 0, 0.06, 'sine'], [650, 0.08, 0.07, 'sine']],
    open: [[1300, 0, 0.06, 'sine'], [1300, 0.09, 0.06, 'sine']],
  };
  const VIBRATE = {
    ok: [40],
    line: [40, 60, 40],
    done: [80, 60, 80, 60, 180],
    wrong: [250, 100, 250, 100, 350],
    over: [150, 100, 250],
    blocked: [60],
    undo: [20],
    open: [30],
  };
  let ctx = null;
  let wakeLock = null;
  let wantAwake = false;

  function muted() {
    try { return localStorage.getItem(MUTE_KEY) === '1'; } catch { return false; }
  }

  function setMuted(value) {
    try { localStorage.setItem(MUTE_KEY, value ? '1' : '0'); } catch { /* private mode */ }
  }

  function unlock() {
    try {
      // iPhone: play through the silent switch like a media app.
      if ('audioSession' in navigator) navigator.audioSession.type = 'playback';
    } catch { /* not supported */ }
    try {
      if (!ctx) {
        const AC = window.AudioContext || window.webkitAudioContext;
        if (!AC) return;
        ctx = new AC();
      }
      if (ctx.state !== 'running') ctx.resume().catch(() => {});
    } catch { /* audio unavailable */ }
    if (wantAwake && !wakeLock) keepAwake();
  }

  function tone(freq, start, duration, wave) {
    const t0 = ctx.currentTime + start;
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.type = wave;
    osc.frequency.value = freq;
    const peak = wave === 'square' ? 0.12 : 0.3;
    gain.gain.setValueAtTime(0.0001, t0);
    gain.gain.exponentialRampToValueAtTime(peak, t0 + 0.008);
    gain.gain.setValueAtTime(peak, t0 + Math.max(0.01, duration - 0.02));
    gain.gain.exponentialRampToValueAtTime(0.0001, t0 + duration);
    osc.connect(gain).connect(ctx.destination);
    osc.start(t0);
    osc.stop(t0 + duration + 0.02);
  }

  function play(name) {
    if (navigator.vibrate && VIBRATE[name]) {
      try { navigator.vibrate(VIBRATE[name]); } catch { /* needs a prior gesture */ }
    }
    if (muted() || !SOUNDS[name]) return;
    unlock();
    if (!ctx || ctx.state !== 'running') return;
    SOUNDS[name].forEach(([freq, start, duration, wave]) => tone(freq, start, duration, wave));
  }

  async function keepAwake() {
    wantAwake = true;
    if (!('wakeLock' in navigator) || wakeLock || document.visibilityState !== 'visible') return;
    try {
      wakeLock = await navigator.wakeLock.request('screen');
      wakeLock.addEventListener('release', () => { wakeLock = null; });
    } catch { /* denied or not supported: the screen may dim */ }
  }

  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') {
      if (wantAwake) keepAwake();
      if (ctx && ctx.state !== 'running') ctx.resume().catch(() => {});
    }
  });

  return { play, unlock, keepAwake, muted, setMuted };
})();
