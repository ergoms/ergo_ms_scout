export function flattenValues(values, id, missing) {
  const row = { id }
  Object.entries(values || {}).forEach(([key, cell]) => {
    row[key] = String(cell?.normalized || cell?.raw || '').trim() || missing
  })
  return row
}
