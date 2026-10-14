(() => {
  const root = document.documentElement;
  const style = document.createElement('style');
  style.textContent = `html{-webkit-text-size-adjust:100%;text-size-adjust:100%}
    @media(max-width:800px){input:not([type=checkbox]):not([type=radio]),textarea,select{font-size:16px!important}
    .chat-shell{height:calc(var(--app-height,100dvh) - 56px - env(safe-area-inset-top))!important}
    .modal-overlay{top:var(--app-offset-top,0px);bottom:auto;height:var(--app-height,100dvh)}
    .modal-box{max-height:calc(var(--app-height,100dvh) - 24px);grid-template-rows:auto minmax(0,1fr) auto}
    .modal-box>.modal-body{min-height:0;overscroll-behavior:contain}}
  `;
  document.head.append(style);
  function sync() {
    const viewport = window.visualViewport;
    // Keep the layout stable during pinch zoom; browser chrome and keyboards still resize it.
    if (viewport && Math.abs(viewport.scale - 1) > 0.01) return;
    root.style.setProperty('--app-height', `${Math.round(viewport?.height || innerHeight)}px`);
    root.style.setProperty('--app-offset-top', `${Math.round(viewport?.offsetTop || 0)}px`);
  }
  sync();
  window.addEventListener('resize', sync);
  window.addEventListener('orientationchange', sync);
  window.visualViewport?.addEventListener('resize', sync);
  window.visualViewport?.addEventListener('scroll', sync);
})();
