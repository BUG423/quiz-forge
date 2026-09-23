import fs from 'node:fs/promises'
import path from 'node:path'
import ExcelJS from 'exceljs'
import { createServer } from 'vite'

// Real question banks and every generated workbook stay under the ignored data/ directory.
const sourceDirectory = path.resolve('data')
const outputDirectory = path.join(sourceDirectory, 'converted')
const optionKeys = ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H', 'I']
const headers = [
  '题型', '难易度', '知识类别', '题干', ...optionKeys, '答案',
  '出自规范', '是否保命题', '备注(填空题多个填空答案，请用英文逗号隔开)',
]

function cellText(cell) {
  const value = cell.value
  if (value == null) return ''
  if (typeof value === 'object') {
    if ('richText' in value) return value.richText.map((part) => part.text).join('').trim()
    if ('text' in value) return String(value.text).trim()
    if ('result' in value) return String(value.result ?? '').trim()
  }
  return String(value).trim()
}

function normalize(value) {
  return value.normalize('NFKC').toLowerCase().replace(/[^\p{L}\p{N}]/gu, '')
}

function findColumn(row, aliases) {
  const wanted = new Set(aliases.map(normalize))
  for (let column = 1; column <= row.cellCount; column += 1) {
    if (wanted.has(normalize(cellText(row.getCell(column))))) return column
  }
  return undefined
}

function findSourceRows(workbook) {
  const records = []
  for (const sheet of workbook.worksheets) {
    if (sheet.state !== 'visible') continue
    let schema
    for (let rowNumber = 1; rowNumber <= Math.min(sheet.rowCount, 30); rowNumber += 1) {
      const row = sheet.getRow(rowNumber)
      const stem = findColumn(row, ['题干', '题目', '试题内容', '题目内容'])
      const answer = findColumn(row, ['答案', '正确答案', '标准答案', '参考答案'])
      if (!stem || !answer) continue
      const options = new Map(optionKeys.map((key) => [key, findColumn(row, [key, `选项${key}`, `${key}选项`])]))
      const candidate = {
        rowNumber, stem, answer, options,
        category: findColumn(row, ['知识类别', '考试类别']),
        source: findColumn(row, ['出自规范', '制度名称及条款内容', '规范依据', '依据', '出处', '来源']),
        lifesaving: findColumn(row, ['是否保命题', '是否保命题型', '保命题']),
        score: [...options.values()].filter(Boolean).length
          + Number(Boolean(findColumn(row, ['题型', '试题类型', '题目类型', '类型']))),
      }
      if (!schema || candidate.score > schema.score) schema = candidate
    }
    if (!schema) continue
    for (let rowNumber = schema.rowNumber + 1; rowNumber <= sheet.rowCount; rowNumber += 1) {
      const row = sheet.getRow(rowNumber)
      const stem = cellText(row.getCell(schema.stem))
      if (stem) records.push({ sheet: sheet.name, rowNumber, row, stem, schema })
    }
  }
  return records
}

async function discover(directory) {
  const entries = await fs.readdir(directory, { withFileTypes: true })
  const results = await Promise.all(entries.map(async (entry) => {
    const fullPath = path.join(directory, entry.name)
    if (entry.isDirectory()) return fullPath === outputDirectory ? [] : discover(fullPath)
    return entry.isFile() && /\.xlsx$/i.test(entry.name) && !entry.name.startsWith('~$') ? [fullPath] : []
  }))
  return results.flat().sort((left, right) => left.localeCompare(right, 'zh-CN'))
}

function comparable(question) {
  return {
    type: question.type,
    answerMode: question.answerMode,
    difficulty: question.difficulty,
    category: question.category,
    stem: question.stem,
    options: question.options,
    answers: question.answers,
    source: question.source,
    lifesaving: question.lifesaving,
    note: question.note,
    caseContext: question.caseContext,
  }
}

async function convertOne(inputPath, parseExcelFile) {
  const relativePath = path.relative(sourceDirectory, inputPath)
  const bankName = relativePath.replace(/\.xlsx$/i, '').split(path.sep).join(' - ')
  const sourceBytes = await fs.readFile(inputPath)
  const imported = await parseExcelFile(new File([sourceBytes], path.basename(inputPath)))
  const sourceWorkbook = new ExcelJS.Workbook()
  await sourceWorkbook.xlsx.readFile(inputPath)
  const sourceRows = findSourceRows(sourceWorkbook)
  const convertedWorkbook = new ExcelJS.Workbook()
  const sheet = convertedWorkbook.addWorksheet('题库题目')
  sheet.addRow(headers)
  sheet.views = [{ state: 'frozen', ySplit: 1 }]
  sheet.autoFilter = { from: 'A1', to: 'Q1' }
  sheet.columns = headers.map((_, index) => ({ width: [3, 14, 16].includes(index) ? 48 : 16 }))

  const expected = []
  const skipped = []
  const review = []
  let sourceCursor = 0
  let activeCaseContext = ''

  for (const question of imported.questions) {
    while (
      sourceCursor < sourceRows.length
      && (sourceRows[sourceCursor].rowNumber !== question.sourceRow || sourceRows[sourceCursor].stem !== question.stem)
    ) sourceCursor += 1
    if (sourceCursor >= sourceRows.length) {
      throw new Error(`${relativePath}: 无法定位源题目第 ${question.sourceRow} 行`)
    }
    const source = sourceRows[sourceCursor]
    sourceCursor += 1
    const get = (column) => column ? cellText(source.row.getCell(column)) : ''
    const rawAnswer = get(source.schema.answer)
    const presentOptions = new Set(question.options.map((option) => option.key))
    const rawKeys = [...new Set([...rawAnswer.toUpperCase().matchAll(/[A-I]/g)].map((match) => match[0]))]
    const missingKeys = question.answerMode === 'single' || question.answerMode === 'multiple'
      ? rawKeys.filter((key) => !presentOptions.has(key))
      : []
    if (missingKeys.length) {
      skipped.push({ sheet: source.sheet, row: source.rowNumber, reason: `答案引用缺失的 ${missingKeys.join('、')} 选项` })
      continue
    }
    if (question.answerMode === 'multiple' && question.answers.length < 2) {
      review.push({ sheet: source.sheet, row: source.rowNumber, reason: '标为多选题，但源答案只有一个选项' })
    }

    const category = get(source.schema.category) || question.category
    const sourceText = get(source.schema.source) || question.source
    const lifesavingText = get(source.schema.lifesaving)
    const lifesaving = lifesavingText ? ['是', 'true', '1', 'yes'].includes(lifesavingText.toLowerCase()) : question.lifesaving
    const enriched = { ...question, category, source: sourceText, lifesaving }

    if (enriched.caseContext && enriched.caseContext !== activeCaseContext) {
      sheet.addRow(['案例题', enriched.difficulty, category, enriched.caseContext])
      activeCaseContext = enriched.caseContext
    }
    if (!enriched.caseContext) activeCaseContext = ''

    const options = Object.fromEntries(enriched.options.map((option) => [option.key, option.text]))
    const answer = enriched.answerMode === 'judge'
      ? enriched.answers[0] === 'T' ? '正确' : '错误'
      : enriched.answerMode === 'fill' ? enriched.answers.join(',') : enriched.answers.join('')
    sheet.addRow([
      enriched.type, enriched.difficulty, enriched.category, enriched.stem,
      ...optionKeys.map((key) => options[key] ?? ''), answer,
      enriched.source, enriched.lifesaving ? '是' : '否', enriched.note,
    ])
    expected.push(enriched)
  }

  if (!expected.length) throw new Error(`${relativePath}: 没有可导出的有效题目`)
  const outputPath = path.join(outputDirectory, `${bankName}.xlsx`)
  await convertedWorkbook.xlsx.writeFile(outputPath)
  const convertedBytes = await fs.readFile(outputPath)
  const verified = await parseExcelFile(new File([convertedBytes], path.basename(outputPath)))
  if (verified.bank.name !== bankName || verified.questions.length !== expected.length || verified.warnings.length) {
    throw new Error(`${relativePath}: 转换后题库名称、题数或导入警告不一致`)
  }
  for (let index = 0; index < expected.length; index += 1) {
    if (JSON.stringify(comparable(verified.questions[index])) !== JSON.stringify(comparable(expected[index]))) {
      throw new Error(`${relativePath}: 第 ${index + 1} 道题转换后内容、选项或答案不一致`)
    }
  }
  const types = Object.fromEntries(verified.bank.types.map((type) => [
    type, verified.questions.filter((question) => question.type === type).length,
  ]))
  return { source: relativePath, output: path.relative(process.cwd(), outputPath), sourceRows: sourceRows.length,
    parsedQuestions: imported.questions.length, exportedQuestions: verified.questions.length, types, skipped, review }
}

async function main() {
  const sourceFiles = await discover(sourceDirectory)
  if (!sourceFiles.length) throw new Error('data/ 下没有找到 .xlsx 源题库')
  const outputNames = sourceFiles.map((file) => path.relative(sourceDirectory, file)
    .replace(/\.xlsx$/i, '').split(path.sep).join(' - '))
  if (new Set(outputNames).size !== outputNames.length) {
    throw new Error('源文件映射后的题库名称有重复，请先调整源文件或文件夹名称')
  }
  await fs.mkdir(outputDirectory, { recursive: true })
  const vite = await createServer({ server: { middlewareMode: true }, appType: 'custom' })
  try {
    const { parseExcelFile } = await vite.ssrLoadModule('/src/services/excelImporter.ts')
    const reports = []
    for (const file of sourceFiles) reports.push(await convertOne(file, parseExcelFile))
    for (const report of reports) console.log(JSON.stringify(report))
    console.log(`完成：${reports.length} 个独立题库，${reports.reduce((total, report) => total + report.exportedQuestions, 0)} 道可作答题目。`)
  } finally {
    await vite.close()
  }
}

main().catch((error) => {
  console.error(error)
  process.exitCode = 1
})
