import fs from 'node:fs/promises'
import { createHash } from 'node:crypto'
import path from 'node:path'
import { createServer } from 'vite'

const inputDirectory = path.resolve('data/converted')
const outputDirectory = path.resolve('public/data')

async function main() {
  const files = (await fs.readdir(inputDirectory))
    .filter((name) => name.toLowerCase().endsWith('.xlsx') && !name.startsWith('~$'))
    .sort((left, right) => left.localeCompare(right, 'zh-CN'))
  if (!files.length) throw new Error('data/converted/ 下没有可打包的题库，请先运行 npm run convert:banks')

  const vite = await createServer({ server: { middlewareMode: true }, appType: 'custom' })
  const banks = []
  const questions = []
  try {
    const { parseExcelFile } = await vite.ssrLoadModule('/src/services/excelImporter.ts')
    for (const name of files) {
      const bytes = await fs.readFile(path.join(inputDirectory, name))
      const result = await parseExcelFile(new File([bytes], name))
      if (result.warnings.length) throw new Error(`${name} 导入警告：${result.warnings.join('；')}`)
      banks.push({ ...result.bank, origin: 'bundled', importedAt: new Date().toISOString().slice(0, 10) })
      questions.push(...result.questions)
    }
  } finally {
    await vite.close()
  }

  if (new Set(banks.map((bank) => bank.id)).size !== banks.length) throw new Error('存在重复题库 ID')
  if (new Set(questions.map((question) => question.id)).size !== questions.length) throw new Error('存在重复题目 ID')
  if (banks.reduce((sum, bank) => sum + bank.questionCount, 0) !== questions.length) throw new Error('题库题数与题目列表不一致')

  const sourceVersion = `bundled-${createHash('sha256').update(JSON.stringify({
    banks: banks.map(({ importedAt, ...bank }) => bank), questions,
  })).digest('hex').slice(0, 20)}`
  const dataset = { schemaVersion: 1, sourceVersion, generatedAt: new Date().toISOString(), banks, questions }
  const manifest = { schemaVersion: 1, sourceVersion, bankCount: banks.length, questionCount: questions.length }
  await fs.mkdir(outputDirectory, { recursive: true })
  await fs.writeFile(path.join(outputDirectory, 'questions.json'), JSON.stringify(dataset))
  await fs.writeFile(path.join(outputDirectory, 'manifest.json'), JSON.stringify(manifest))

  console.log(`已生成 ${banks.length} 个内置题库、${questions.length} 道题，版本 ${sourceVersion}`)
  for (const bank of banks) console.log(`${bank.name}: ${bank.questionCount} 题`)
}

main().catch((error) => {
  console.error(error)
  process.exitCode = 1
})
