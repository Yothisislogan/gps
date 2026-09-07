// Locked local dependencies: no CDN requests during startup or offline use.
import { build } from 'esbuild';
import { mkdir, copyFile, readFile, writeFile, readdir } from 'node:fs/promises';
const out = 'web/vendor/v1';
await mkdir(out, { recursive: true });
for (const file of ['maplibre-gl.mjs', 'maplibre-gl-shared.mjs', 'maplibre-gl-worker.mjs', 'maplibre-gl.css']) {
  await copyFile(`node_modules/maplibre-gl/dist/${file}`, `${out}/${file}`);
}
await copyFile('node_modules/pmtiles/dist/pmtiles.js', `${out}/pmtiles.js`);
await build({ entryPoints: ['node_modules/opening_hours/build/opening_hours.js'], bundle: true,
  platform: 'browser', format: 'esm', outfile: `${out}/opening-hours.mjs`, minify: true, legalComments: 'inline' });
for (const [name, weight, style] of [['regular', 400, 'normal'], ['bold', 700, 'normal'], ['italic', 400, 'italic']]) {
  await copyFile(`node_modules/@fontsource/noto-sans/files/noto-sans-latin-${weight}-${style}.woff2`, `${out}/noto-${name}.woff2`);
}
const lock = JSON.parse(await readFile('package-lock.json', 'utf8'));
let licenses = 'NicaNav browser dependency notices\n';
for (const [path, pkg] of Object.entries(lock.packages)) {
  if (!path) continue;
  licenses += `\n${path} ${pkg.version} (${pkg.license || 'see package'})\n`;
  for (const file of await readdir(path).catch(error => { if (error.code === 'ENOENT') return []; throw error; })) {
    if (/^(license|copying|notice|ofl)(\.|$)/i.test(file)) {
      licenses += await readFile(`${path}/${file}`, 'utf8');
      licenses += '\n';
    }
  }
}
await writeFile(`${out}/LICENSES.txt`, licenses);
console.log('Built local map renderer, worker, PMTiles, hours parser and Latin fonts.');

// The source sprites have pixelRatio=1. Publish that same declared density for
// high-DPI requests too, rather than returning HTML for a missing @2x file.
for (const ext of ['json', 'png']) {
  await copyFile(`web/sprites/nicanav.${ext}`, `web/sprites/nicanav@2x.${ext}`);
}
