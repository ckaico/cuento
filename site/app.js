// cuento.ai. The demo recording answers to the editor's own shuttle keys.
(() => {
  const video = document.getElementById('demo');
  const rate = document.getElementById('rate');
  const head = document.getElementById('strip-head');
  const strip = document.getElementById('strip');
  const buttons = {};
  document.querySelectorAll('.keys button').forEach((b) => { buttons[b.dataset.key] = b; });

  function say() {
    if (video.paused) rate.textContent = 'Stopped. L plays.';
    else rate.textContent = video.playbackRate === 1 ? 'Playing 1×. L again for 2×, 4×, 8×.' : `Shuttling at ${video.playbackRate}×. K stops.`;
  }

  function press(key) {
    const b = buttons[key];
    if (!b) return false;
    b.classList.add('down');
    setTimeout(() => b.classList.remove('down'), 140);
    if (key === 'l') {
      video.playbackRate = video.paused ? 1 : Math.min(8, video.playbackRate * 2);
      video.play().catch(() => {});
    } else if (key === 'k') {
      video.pause();
      video.playbackRate = 1;
    } else {
      video.currentTime = Math.max(0, video.currentTime - 2);
    }
    say();
    return true;
  }

  addEventListener('keydown', (e) => {
    if (e.metaKey || e.ctrlKey || e.altKey || /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName)) return;
    if (press(e.key.toLowerCase())) e.preventDefault();
  });
  for (const [key, b] of Object.entries(buttons)) b.addEventListener('click', () => press(key));

  // the red playhead under the recording tracks where the recording is
  function tick() {
    if (video.duration) head.style.transform = `translateX(${(video.currentTime / video.duration) * (strip.clientWidth - 8)}px)`;
    requestAnimationFrame(tick);
  }
  requestAnimationFrame(tick);

  if (matchMedia('(prefers-reduced-motion: reduce)').matches) { video.removeAttribute('autoplay'); video.pause(); say(); }

  document.querySelectorAll('.copy').forEach((b) => b.addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText(document.getElementById(b.dataset.copy).innerText);
      b.textContent = 'Copied';
    } catch { b.textContent = 'Select and copy'; }
    setTimeout(() => { b.textContent = 'Copy'; }, 1600);
  }));
})();
