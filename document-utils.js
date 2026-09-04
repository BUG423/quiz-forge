(function attachDocumentUtils(root) {
  function safeName(name) {
    return String(name).replace(/\.[^.]+$/, '').replace(/[\\/:*?"<>|]/g, '_') || '转写结果';
  }

  function buildMerged(items) {
    return items.map((item, index) => (
      `${'='.repeat(64)}\n${index + 1}. ${item.name}\n${'='.repeat(64)}\n\n${String(item.text).trim()}`
    )).join('\n\n\n');
  }

  const api = { safeName, buildMerged };
  root.DocumentUtils = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
}(globalThis));
