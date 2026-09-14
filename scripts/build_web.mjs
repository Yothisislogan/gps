// Package a release without changing the editable web sources. Old asset URLs
// remain valid while an open tab finishes a trip on the previous release.
import { createHash } from 'node:crypto';
import { readFile, writeFile, mkdir, readdir } from 'node:fs/promises';
import { gzipSync, brotliCompressSync } from 'node:zlib';
import { join } from 'node:path';
const root = 'web', output = 'dist/web';
async function walk(path) {
  const items = await readdir(path, { withFileTypes: true });
  const files = await Promise.all(items.map(async item => item.isDirectory() ? walk(join(path, item.name)) : [join(path, item.name)]));
  return files.flat().sort();
}
const sources = (await walk(root)).filter(p => p !== 'web/config.js');
const generation = process.env.NICANAV_RELEASE_ID || '';
if (generation && !/^[a-z0-9-]+$/.test(generation)) throw new Error('Invalid data release ID');
const dataPrefix = generation ? `/data-releases/${generation}` : '';
const digest = createHash('sha256').update(generation);
digest.update(await readFile(new URL(import.meta.url)));
let runtimeConfig = await readFile('web/config.js', 'utf8').catch(() => 'window.NICANAV_CONFIG = {};\n');
digest.update(runtimeConfig);
for (const path of sources) digest.update(path).update(await readFile(path));
const version = digest.digest('hex').slice(0, 20);
const prefix = `/releases/${version}`;
const assets = sources.filter(p => !['web/index.html', 'web/sw.js', 'web/manifest.webmanifest'].includes(p));
const rewrite = text => text.replace(/pmtiles:\/\/\/tiles\//g, `pmtiles://${dataPrefix}/tiles/`).replace(/(?<=["'`(])\/(js|css|vendor|style|sprites|icons)\//g, `${prefix}/$1/`);
await mkdir(output, { recursive: true });
let compressedBytes = 0;
for (const path of assets) {
  const target = join(output, prefix, path.slice(4));
  await mkdir(target.slice(0, target.lastIndexOf('/')), { recursive: true });
  let content = await readFile(path);
  if (/\.(js|mjs|json|css|svg)$/.test(path)) content = Buffer.from(rewrite(content.toString()));
  await writeFile(target, content);
  if (/\.(js|mjs|json|css|svg)$/.test(path)) {
    await writeFile(`${target}.gz`, gzipSync(content));
    await writeFile(`${target}.br`, brotliCompressSync(content));
    compressedBytes += gzipSync(content).length;
  } else compressedBytes += content.length;
}
await writeFile(`${output}/index.html`, rewrite(await readFile('web/index.html', 'utf8')));
await writeFile(`${output}/manifest.webmanifest`, rewrite(await readFile('web/manifest.webmanifest', 'utf8')));
const worker = rewrite(await readFile('web/sw.js', 'utf8')).replace(/const VERSION = '[^']+';/, `const VERSION = '${version}';`);
await writeFile(`${output}/sw.js`, worker);
await writeFile(`${output}/config.js`, rewrite(runtimeConfig));
await writeFile(`${output}/release.json`, JSON.stringify({ version, assetPrefix: prefix, assets: assets.length, compressedBytes }, null, 2));
console.log(`Packaged web release ${version} (${Math.round(compressedBytes / 1024)} KiB compressed assets, excluding map archives).`);

if (compressedBytes > 900 * 1024) throw new Error('Offline shell exceeds the 900 KiB gzip budget; inspect new dependencies.');
