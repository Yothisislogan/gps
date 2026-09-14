import { test, expect } from '@playwright/test';

async function search(page) {
  await page.getByRole('searchbox').fill('clínica');
  await page.getByRole('button', { name: /Clínica de prueba/ }).click();
  await expect(page.getByRole('heading', { name: 'Clínica de prueba' })).toBeVisible();
}

test('search and settings work while the renderer is unavailable', async ({ page }) => {
  await page.route('**/vendor/v1/maplibre-gl.mjs', route => route.abort());
  await page.goto('/');
  await search(page);
  await page.getByRole('button', { name: 'Cerrar', exact: true }).click();
  await page.getByRole('button', { name: 'Ajustes', exact: true }).click();
  await expect(page.getByText('Mapa sin conexión', { exact: true })).toBeVisible();
});

test('map renders, search preserves map space, and navigation uses the chosen route', async ({ page, context }) => {
  await context.grantPermissions(['geolocation']);
  await context.setGeolocation({ latitude: 12.11, longitude: -86.26 });
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.goto('/?sim=1');
  await expect(page.locator('#map canvas')).toBeVisible();
  await expect(page.locator('#map-status')).toBeHidden({ timeout: 15000 });
  const sheet = await page.locator('#search-results').boundingBox();
  expect(sheet.height).toBeLessThan(page.viewportSize().height * .55);
  await search(page);
  await page.getByRole('button', { name: 'Cómo llegar', exact: true }).click();
  await page.getByRole('button', { name: 'Confirmar destino', exact: true }).click();
  await page.locator('.route__alt').nth(1).click();
  await expect(page.locator('.route__alt').nth(1)).toHaveAttribute('aria-pressed', 'true');
  await page.getByRole('button', { name: 'Empezar', exact: true }).click();
  await expect(page.locator('#nav-root')).toBeVisible();
  await expect(page.locator('#topbar')).toBeHidden();
  expect(errors).toEqual([]);
});

test('GPS failure preserves destination and offers a manual origin', async ({ page }) => {
  await page.addInitScript(() => { navigator.geolocation.getCurrentPosition = (_success, failure) => failure({ code: 1 }); });
  await page.goto('/');
  await expect(page.locator('#map-status')).toBeHidden({ timeout: 15000 });
  await search(page);
  await page.getByRole('button', { name: 'Cómo llegar', exact: true }).click();
  await page.getByRole('button', { name: 'Confirmar destino', exact: true }).click();
  await page.getByRole('button', { name: 'Elegir punto de salida', exact: true }).click();
  await page.getByRole('button', { name: 'Confirmar destino', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Empezar', exact: true })).toBeVisible();
});

test('installed shell reopens offline and contains valid installation icons', async ({ page, context }) => {
  await page.goto('/');
  await page.evaluate(async () => { await navigator.serviceWorker.ready; });
  await page.reload();
  await page.evaluate(async () => {
    const manifest = await (await fetch('/manifest.webmanifest')).json();
    for (const icon of manifest.icons) {
      const response = await fetch(icon.src);
      if (!response.ok || !response.headers.get('content-type')?.includes('image/png')) throw new Error('Missing PWA icon');
    }
  });
  await context.setOffline(true);
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(page.getByRole('searchbox')).toBeVisible();
  await page.getByRole('button', { name: 'Ajustes', exact: true }).click();
  await expect(page.getByText('Mapa sin conexión', { exact: true })).toBeVisible();
});

test('small screens do not overflow and preserve 48-pixel primary targets', async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 640 });
  await page.goto('/');
  await expect(page.getByRole('button', { name: 'Casa', exact: true })).toBeVisible();
  const measures = await page.evaluate(() => ({ width: document.documentElement.scrollWidth,
    targets: [...document.querySelectorAll('.icon-button,.chip')].filter(e => !e.hidden).map(e => e.getBoundingClientRect().height) }));
  expect(measures.width).toBeLessThanOrEqual(320);
  expect(measures.targets.every(h => h >= 48)).toBe(true);
  await page.screenshot({ path: `test-results/mobile-${test.info().project.name}.png` });
});
