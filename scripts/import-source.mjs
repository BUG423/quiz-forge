import fs from 'node:fs/promises'
import { createHash } from 'node:crypto'
import path from 'node:path'
import process from 'node:process'
import ExcelJS from 'exceljs'
import JSZip from 'jszip'
import iconv from 'iconv-lite'

const sourceArchiveArgument = process.argv[2]
if (!sourceArchiveArgument) {
  throw new Error('用法：npm run import:source -- /path/to/question-bank.zip')
}

const SOURCE_ARCHIVE = path.resolve(sourceArchiveArgument)
const OUTPUT_FILE = path.resolve('public/data/questions.json')
const MANIFEST_FILE = path.resolve('public/data/manifest.json')
const OPTION_KEYS = ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H', 'I']

const text = (value) => {
  if (value === null || value === undefined) return ''
  if (typeof value === 'object') {
    if ('richText' in value) return value.richText.map((part) => part.text).join('').trim()
    if ('text' in value) return String(value.text).trim()
    if ('result' in value) return String(value.result ?? '').trim()
  }
  return String(value).trim()
}

const shortHash = (value, length = 12) =>
  createHash('sha1').update(value).digest('hex').slice(0, length)

const stableId = (value) => {
  let hash = 2166136261
  for (let index = 0; index < value.length; index += 1) {
    hash ^= value.charCodeAt(index)
    hash = Math.imul(hash, 16777619)
  }
  return (hash >>> 0).toString(36)
}

function parseAnswer(rawType, rawAnswer, options) {
  const type = rawType.replaceAll(' ', '')
  if (type.includes('填空')) {
    return {
      mode: 'fill',
      answers: rawAnswer.split(/[,，]/).map((item) => item.trim()).filter(Boolean),
    }
  }

  if (type.includes('判断')) {
    const normalized = rawAnswer.toLowerCase()
    const truthy = ['正确', '对', '是', '√', 'true', 't', 'a']
    const falsy = ['错误', '错', '否', '×', 'false', 'f', 'b']
    if (truthy.includes(normalized)) return { mode: 'judge', answers: ['T'] }
    if (falsy.includes(normalized)) return { mode: 'judge', answers: ['F'] }
  }

  const optionKeys = new Set(options.map((option) => option.key))
  const letters = [...rawAnswer.toUpperCase().matchAll(/[A-I]/g)]
    .map((match) => match[0])
    .filter((letter, index, all) => optionKeys.has(letter) && all.indexOf(letter) === index)

  return {
    mode: type.includes('多选') || letters.length > 1 ? 'multiple' : 'single',
    answers: letters,
  }
}

function typeLabel(rawType, answerMode) {
  if (rawType.includes('案例')) return '案例题'
  if (answerMode === 'multiple') return '多选题'
  if (answerMode === 'judge') return '判断题'
  if (answerMode === 'fill') return '填空题'
  return '单选题'
}

async function main() {
  const archive = await fs.readFile(SOURCE_ARCHIVE)
  const sourceVersion = `${shortHash(archive, 16)}.schema1`
  const zip = await JSZip.loadAsync(archive, {
    decodeFileName: (bytes) => iconv.decode(Buffer.from(bytes), 'gbk'),
  })

  const workbookEntries = Object.values(zip.files)
    .filter((entry) => !entry.dir && /\.xlsx$/i.test(entry.name))
    .sort((a, b) => a.name.localeCompare(b.name, 'zh-CN'))

  const banks = []
  const questions = []
  const warnings = []

  for (const entry of workbookEntries) {
    const bankName = path.basename(entry.name, path.extname(entry.name))
    const bankId = `bank-${stableId(bankName)}`
    const workbook = new ExcelJS.Workbook()
    const bytes = await entry.async('uint8array')
    await workbook.xlsx.load(bytes)

    let bankQuestionCount = 0
    const bankDifficulties = new Set()
    const bankTypes = new Set()

    for (const worksheet of workbook.worksheets) {
      const headers = new Map()
      worksheet.getRow(1).eachCell({ includeEmpty: false }, (cell, colNumber) => {
        headers.set(text(cell.value), colNumber)
      })
      const col = (name) => headers.get(name)
      const required = ['题型', '难易度', '知识类别', '题干', '答案']
      const missing = required.filter((name) => !col(name))
      if (missing.length) {
        warnings.push(`${entry.name}/${worksheet.name} 缺少列：${missing.join('、')}`)
        continue
      }

      let caseContext = ''
      for (let rowNumber = 2; rowNumber <= worksheet.actualRowCount; rowNumber += 1) {
        const row = worksheet.getRow(rowNumber)
        const rawType = text(row.getCell(col('题型')).value)
        const stem = text(row.getCell(col('题干')).value)
        if (!stem) continue

        const rawAnswer = text(row.getCell(col('答案')).value)
        const options = OPTION_KEYS.map((key) => ({
          key,
          text: text(row.getCell(col(key) ?? -1).value),
        })).filter((option) => option.text)

        if (rawType.includes('案例') && !rawAnswer && options.length === 0) {
          caseContext = stem
          continue
        }
        if (!rawType.includes('案例')) caseContext = ''

        const parsed = parseAnswer(rawType, rawAnswer, options)
        if (parsed.answers.length === 0) {
          warnings.push(`${entry.name}/${worksheet.name} 第 ${rowNumber} 行没有可识别答案`)
        }

        const difficulty = text(row.getCell(col('难易度')).value) || '未标注'
        const normalizedType = typeLabel(rawType, parsed.mode)
        const question = {
          id: `q-${stableId(`${bankName}|${worksheet.name}|${rowNumber}|${stem}`)}`,
          bankId,
          sourceRow: rowNumber,
          type: normalizedType,
          answerMode: parsed.mode,
          difficulty,
          category: text(row.getCell(col('知识类别')).value) || '未分类',
          stem,
          options,
          answers: parsed.answers,
          source: text(row.getCell(col('出自规范')).value),
          lifesaving: text(row.getCell(col('是否保命题')).value) === '是',
          note: text(row.getCell(col('备注(填空题多个填空答案，请用英文逗号隔开)')).value),
          caseContext: rawType.includes('案例') ? caseContext : '',
        }
        questions.push(question)
        bankQuestionCount += 1
        bankDifficulties.add(difficulty)
        bankTypes.add(normalizedType)
      }
    }

    banks.push({
      id: bankId,
      name: bankName,
      sourceFile: path.basename(entry.name),
      questionCount: bankQuestionCount,
      difficulties: [...bankDifficulties],
      types: [...bankTypes],
      importedAt: new Date().toISOString().slice(0, 10),
    })
  }

  const payload = {
    schemaVersion: 1,
    sourceVersion,
    generatedAt: new Date().toISOString(),
    banks,
    questions,
  }
  await fs.mkdir(path.dirname(OUTPUT_FILE), { recursive: true })
  await fs.writeFile(OUTPUT_FILE, JSON.stringify(payload))
  await fs.writeFile(MANIFEST_FILE, JSON.stringify({ schemaVersion: payload.schemaVersion, sourceVersion, bankCount: banks.length, questionCount: questions.length }))

  console.log(`已导入 ${banks.length} 个题库，共 ${questions.length} 道可作答题目。`)
  for (const bank of banks) console.log(`- ${bank.name}: ${bank.questionCount} 题`)
  const typeStats = Object.groupBy(questions, (question) => question.type)
  console.log('题型：', Object.fromEntries(Object.entries(typeStats).map(([key, value]) => [key, value.length])))
  if (warnings.length) {
    console.log(`警告 ${warnings.length} 条：`)
    warnings.slice(0, 20).forEach((warning) => console.log(`- ${warning}`))
  }
}

main().catch((error) => {
  console.error(error)
  process.exitCode = 1
})
