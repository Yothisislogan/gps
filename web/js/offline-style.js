/** Keep the downloaded base map usable without requesting unavailable POI tiles. */
export function offlineStyle(style, hasArchive) {
  const result = structuredClone(style);
  for (const [id, source] of Object.entries(result.sources || {})) {
    if (id === 'base' && hasArchive) source.url = 'pmtiles://nicanav://circle.pmtiles';
    else if (source.type !== 'geojson') delete result.sources[id];
  }
  result.layers = (result.layers || []).filter(layer => !layer.source || result.sources[layer.source]);
  return result;
}
