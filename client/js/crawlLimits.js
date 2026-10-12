export function defaultCrawlLimits(saved = {}) {
  const defaults = { max_seconds: 600, max_total_bytes: 20000000, frontier_cap: 200, fetch_delay_ms: 0, parallel_fetch: 2, preview_pages: 3 }
  return Object.fromEntries(Object.entries(defaults).map(([key, value]) => [key, saved[key] ?? value]))
}
