// Timing runtime for a layer page. The editor and the renderer both drive a
// layer by calling cuento.seek(seconds); nothing in a layer runs on wall-clock
// time, so a rendered frame and a previewed frame are the same frame.
//
// CSS animations and Web Animations are seeked automatically. For canvas or
// hand-driven graphics, set  cuento.onframe = (seconds, frame) => { ... }
(() => {
  const C = window.cuento = window.cuento || {};
  const style = document.createElement('style');
  style.textContent = 'html,body{background:transparent!important;margin:0;padding:0;width:1920px;height:1080px;overflow:hidden}';
  document.head.appendChild(style);
  C.time = 0;
  C.seek = (t) => {
    C.time = t;
    for (const a of document.getAnimations()) {
      if (a.playState !== 'paused') a.pause();
      a.currentTime = t * 1000;
    }
    if (typeof C.onframe === 'function') C.onframe(t, Math.round(t * C.fps));
  };
  // the live preview runs this page in a sandboxed frame and seeks it by message
  window.addEventListener('message', (e) => { if (e.data && typeof e.data.cuentoSeek === 'number') C.seek(e.data.cuentoSeek); });
  const first = () => C.seek(C.time);
  document.addEventListener('DOMContentLoaded', first);
  window.addEventListener('load', first);
})();
