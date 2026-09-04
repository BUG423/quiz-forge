const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { safeName, buildMerged } = require('../document-utils.js');

const root = path.resolve(__dirname, '..');
const reviewedDir = path.join(root, 'test-data', 'reviewed');
const exportDir = path.join(root, 'test-data', 'exports');
fs.mkdirSync(exportDir, { recursive: true });

const sourceNames = [
  'VOA 中国官方媒体为逮捕艾未未辩解.ogv',
  'VOA 上海为罢工卡车司机削减收费.ogv',
];

const documents = sourceNames.map((name) => {
  const textName = `${safeName(name)}.txt`;
  const text = fs.readFileSync(path.join(reviewedDir, textName), 'utf8').trim();
  assert.ok(text.length > 100, `${textName} 应包含有效转写结果`);
  fs.writeFileSync(path.join(exportDir, textName), text, 'utf8');
  return { name, text };
});

const merged = buildMerged(documents);
assert.ok(merged.indexOf(sourceNames[0]) < merged.indexOf(sourceNames[1]), '合并文档顺序应与上传顺序一致');
assert.ok(merged.includes('艾未未'), '合并文档应包含第一份复核结果');
assert.ok(merged.includes('卡车司机'), '合并文档应包含第二份复核结果');
fs.writeFileSync(path.join(exportDir, '全部转写结果.txt'), merged, 'utf8');

const outputFiles = fs.readdirSync(exportDir).sort();
assert.equal(outputFiles.length, 3, '应生成两份单独文档和一份合并文档');
console.log(`export test passed: ${outputFiles.join(', ')}`);
