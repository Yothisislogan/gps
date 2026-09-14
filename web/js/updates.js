import { t, toast } from './ui.js';

export function setupUpdates() {
  if (!('serviceWorker' in navigator)) return;
  const busy = () => document.body.classList.contains('navigating');
  navigator.serviceWorker.addEventListener('message', (event) => {
    if (event.data === 'update-check') event.ports[0]?.postMessage({ busy: busy() });
  });
  // A worker update is offered explicitly. Navigation never triggers a reload.
  let requested = false;
  navigator.serviceWorker.addEventListener('controllerchange', () => {
    if (requested && !busy()) window.location.reload();
  });
  const register = () => navigator.serviceWorker.register('/sw.js').then((registration) => {
    const offer = () => {
      if (!registration.waiting || !navigator.serviceWorker.controller || busy()) return;
      toast(t('update.ready'), { durationMs: 15000, actionLabel: t('update.apply'), onAction: () => {
        if (busy()) return;
        requested = true;
        registration.waiting?.postMessage('activate-update');
      } });
    };
    registration.addEventListener('updatefound', () => registration.installing?.addEventListener('statechange', offer));
    window.addEventListener('nicanav-navigation-ended', offer);
    offer();
  }).catch(() => {});
  // Let the initial interface and map fetch go first on a narrow connection.
  if ('requestIdleCallback' in window) window.requestIdleCallback(register, { timeout: 5000 });
  else setTimeout(register, 1200);
}
