import type { AnswerMode, ImportResult, Question, QuestionOption } from '../types'
import { readXlsxFile, type SpreadsheetRow } from './xlsxReader'

const OPTION_KEYS = ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H', 'I']
const NOTE_HEADER = '备注(填空题多个填空答案，请用英文逗号隔开)'
const HEADER_SCAN_LIMIT = 30

const HEADER_ALIASES = {
  type: ['题型', '试题类型', '题目类型', '类型'],
  difficulty: ['难易度', '试题难易', '试题难度', '题目难度', '难度', '难易程度'],
  stem: ['题干', '题目', '试题内容', '题目内容'],
  answer: ['答案', '正确答案', '标准答案', '参考答案'],
  source: ['出自规范', '规范依据', '依据', '出处', '来源'],
  lifesaving: ['是否保命题', '保命题'],
  note: ['备注', NOTE_HEADER],
} as const

const CATEGORY_ALIASES = ['知识类别', '评价内容', '知识点', '能力要素', '能力单元', '模块', '维度', '类别']

interface HeaderSchema {
  rowNumber: number
  type?: number
  difficulty?: number
  stem: number
  answer: number
  source?: number
  lifesaving?: number
  note?: number
  category?: number
  options: Map<string, number>
  score: number
}

function stableHash(value: string) {
  let hash = 2166136261
  for (let index = 0; index < value.length; index += 1) {
    hash ^= value.charCodeAt(index)
    hash = Math.imul(hash, 16777619)
  }
  return (hash >>> 0).toString(36)
}

function normalizeHeader(value: string) {
  return value.normalize('NFKC').toLowerCase().replace(/[^\p{L}\p{N}]/gu, '')
}

function findColumn(row: SpreadsheetRow, aliases: readonly string[], useLastMatch = false) {
  for (const alias of aliases) {
    const normalizedAlias = normalizeHeader(alias)
    const matches = [...row.cells]
      .filter(([, value]) => normalizeHeader(value) === normalizedAlias)
      .map(([column]) => column)
    if (matches.length) return useLastMatch ? matches.at(-1) : matches[0]
  }
  return undefined
}

function optionKeyFromHeader(value: string) {
  const header = normalizeHeader(value).toUpperCase()
  return OPTION_KEYS.find((key) => header === key || header === `选项${key}` || header === `${key}选项`)
}

function inspectHeader(row: SpreadsheetRow): HeaderSchema | undefined {
  const stem = findColumn(row, HEADER_ALIASES.stem)
  const answer = findColumn(row, HEADER_ALIASES.answer)
  if (!stem || !answer) return undefined
  const options = new Map<string, number>()
  for (const [column, value] of row.cells) {
    const key = optionKeyFromHeader(value)
    if (key) options.set(key, column)
  }
  const type = findColumn(row, HEADER_ALIASES.type)
  const difficulty = findColumn(row, HEADER_ALIASES.difficulty)
  const category = findColumn(row, CATEGORY_ALIASES, true)
  return {
    rowNumber: row.number,
    type,
    difficulty,
    stem,
    answer,
    source: findColumn(row, HEADER_ALIASES.source),
    lifesaving: findColumn(row, HEADER_ALIASES.lifesaving),
    note: findColumn(row, HEADER_ALIASES.note),
    category,
    options,
    score: 10 + (type ? 3 : 0) + (difficulty ? 1 : 0) + (category ? 1 : 0) + Math.min(options.size, 4),
  }
}

function findHeader(rows: SpreadsheetRow[]) {
  return rows
    .filter((row) => row.number <= HEADER_SCAN_LIMIT)
    .map(inspectHeader)
    .filter((schema): schema is HeaderSchema => Boolean(schema))
    .sort((left, right) => right.score - left.score || left.rowNumber - right.rowNumber)[0]
}

function isRepeatedHeader(row: SpreadsheetRow, schema: HeaderSchema) {
  return HEADER_ALIASES.stem.some((alias) => normalizeHeader(alias) === normalizeHeader(row.cells.get(schema.stem) ?? ''))
    && HEADER_ALIASES.answer.some((alias) => normalizeHeader(alias) === normalizeHeader(row.cells.get(schema.answer) ?? ''))
}

function cleanOptionText(key: string, value: string) {
  const prefix = new RegExp(`^\\s*(?:[（(【\\[]\\s*)?${key}(?:\\s*[）)】\\]])?\\s*(?:[、.．:：]|\\s+)\\s*`, 'i')
  return value.replace(prefix, '').trim()
}

function judgeValue(value: string) {
  const symbol = value.trim()
  if (symbol === '√') return 'T'
  if (symbol === '×' || symbol === '✕' || symbol === '✖') return 'F'
  const normalized = normalizeHeader(value)
  if (['正确', '对', '是', 'true', 't', 'yes', 'y'].includes(normalized)) return 'T'
  if (['错误', '错', '否', 'false', 'f', 'no', 'n'].includes(normalized)) return 'F'
  return ''
}

function parseAnswer(rawType: string, rawAnswer: string, options: QuestionOption[]): { mode: AnswerMode; answers: string[] } {
  const type = normalizeHeader(rawType)
  if (type.includes('判断')) {
    const directValue = judgeValue(rawAnswer)
    if (directValue) return { mode: 'judge', answers: [directValue] }
    const option = options.find(({ key }) => key === rawAnswer.trim().toUpperCase())
    const optionValue = option ? judgeValue(option.text) : ''
    if (optionValue) return { mode: 'judge', answers: [optionValue] }
    if (rawAnswer.trim().toUpperCase() === 'A') return { mode: 'judge', answers: ['T'] }
    if (rawAnswer.trim().toUpperCase() === 'B') return { mode: 'judge', answers: ['F'] }
    const answerKeys = [...rawAnswer.toUpperCase().matchAll(/[A-I]/g)].map((match) => match[0])
    if (!answerKeys.some((key) => options.some((option) => option.key === key))) return { mode: 'judge', answers: [] }
  }
  if (type.includes('填空')) {
    return { mode: 'fill', answers: rawAnswer.split(/[,，]/).map((item) => item.trim()).filter(Boolean) }
  }
  if (/简答|问答|计算|案例分析|论述/.test(type) || options.length === 0) {
    return { mode: 'fill', answers: rawAnswer.trim() ? [rawAnswer.trim()] : [] }
  }
  const validKeys = new Set(options.map((option) => option.key))
  const answers = [...rawAnswer.toUpperCase().matchAll(/[A-I]/g)]
    .map((match) => match[0])
    .filter((key, index, all) => validKeys.has(key) && all.indexOf(key) === index)
  if (!answers.length) {
    const matchingOption = options.find((option) => normalizeHeader(option.text) === normalizeHeader(rawAnswer))
    if (matchingOption) answers.push(matchingOption.key)
  }
  return { mode: type.includes('多选') || answers.length > 1 ? 'multiple' : 'single', answers }
}

function normalizedType(rawType: string, mode: AnswerMode) {
  const type = normalizeHeader(rawType)
  if (type.includes('案例分析')) return '案例分析题'
  if (type.includes('案例')) return '案例题'
  if (type.includes('简答') || type.includes('问答')) return '简答题'
  if (type.includes('计算')) return '计算题'
  if (type.includes('论述')) return '论述题'
  if (mode === 'multiple') return '多选题'
  if (mode === 'judge') return '判断题'
  if (mode === 'fill') return '填空题'
  return '单选题'
}

export async function parseExcelFile(file: File): Promise<ImportResult> {
  const sheets = await readXlsxFile(file)
  const name = file.name.replace(/\.xlsx?$/i, '').trim() || '未命名题库'
  const bankId = `bank-${stableHash(name)}`
  const questions: Question[] = []
  const warnings: string[] = []

  for (const sheet of sheets) {
    const header = findHeader(sheet.rows)
    if (!header) {
      warnings.push(`${sheet.name} 未找到“题干”和“答案”列，已跳过`)
      continue
    }
    let caseContext = ''

    for (const row of sheet.rows) {
      if (row.number <= header.rowNumber || isRepeatedHeader(row, header)) continue
      const get = (column?: number) => column ? (row.cells.get(column) ?? '').trim() : ''
      const rawType = get(header.type) || sheet.name
      const stem = get(header.stem)
      if (!stem) continue
      const rawAnswer = get(header.answer)
      const options = [...header.options]
        .map(([key, column]) => ({ key, text: cleanOptionText(key, get(column)) }))
        .filter((option) => option.text)

      if (rawType.includes('案例') && !rawAnswer && options.length === 0) {
        caseContext = stem
        continue
      }
      if (!rawType.includes('案例')) caseContext = ''
      const parsed = parseAnswer(rawType, rawAnswer, options)
      if (!parsed.answers.length) {
        warnings.push(`${sheet.name} 第 ${row.number} 行未识别到答案，已跳过`)
        continue
      }
      const type = normalizedType(rawType, parsed.mode)
      questions.push({
        id: `q-${stableHash(`${name}|${sheet.name}|${row.number}|${stem}`)}`,
        bankId,
        sourceRow: row.number,
        type,
        answerMode: parsed.mode,
        difficulty: get(header.difficulty) || '未标注',
        category: get(header.category) || '未分类',
        stem,
        options,
        answers: parsed.answers,
        source: get(header.source),
        lifesaving: ['是', 'true', '1', 'yes'].includes(get(header.lifesaving).toLowerCase()),
        note: get(header.note),
        caseContext: rawType.includes('案例') ? caseContext : '',
      })
    }
  }

  if (!questions.length) {
    const detail = warnings.length ? `：${warnings.slice(0, 2).join('；')}` : ''
    throw new Error(`文件中没有可导入的有效题目${detail}`)
  }
  const difficulties = [...new Set(questions.map((question) => question.difficulty))]
  const types = [...new Set(questions.map((question) => question.type))]
  return {
    bank: {
      id: bankId,
      name,
      sourceFile: file.name,
      questionCount: questions.length,
      difficulties,
      types,
      importedAt: new Date().toISOString(),
      origin: 'upload',
    },
    questions,
    warnings,
  }
}

export async function createTemplateBuffer() {
  const ExcelJSRuntime = (await import('exceljs')).default
  const workbook = new ExcelJSRuntime.Workbook()
  const sheet = workbook.addWorksheet('题库题目')
  const headers = ['题型', '难易度', '知识类别', '题干', ...OPTION_KEYS, '答案', '出自规范', '是否保命题', NOTE_HEADER]
  sheet.addRow(headers)
  sheet.addRow(['单选题', '简单', '小学数学示例', '2 + 3 = ?', '4', '5', '6', '7', '', '', '', '', '', 'B', '', '否', ''])
  sheet.addRow(['多选题', '简单', '小学数学示例', '以下哪些算式的结果等于 6？', '1 + 5', '2 + 4', '3 + 4', '8 - 1', '', '', '', '', '', 'AB', '', '否', ''])
  sheet.addRow(['判断题', '简单', '小学数学示例', '7 - 2 = 5。', '', '', '', '', '', '', '', '', '', '正确', '', '否', ''])
  sheet.addRow(['填空题', '简单', '小学数学示例', '9 - 4 = __。', '', '', '', '', '', '', '', '', '', '5', '', '否', ''])
  sheet.getRow(1).font = { bold: true, color: { argb: 'FFFFFFFF' } }
  sheet.getRow(1).fill = { type: 'pattern', pattern: 'solid', fgColor: { argb: 'FF174B43' } }
  sheet.views = [{ state: 'frozen', ySplit: 1 }]
  sheet.autoFilter = { from: 'A1', to: 'Q1' }
  sheet.columns.forEach((column, index) => {
    column.width = index === 3 || index === 14 || index === 16 ? 42 : 15
    column.numFmt = '@'
  })
  for (let row = 2; row <= 1001; row += 1) {
    sheet.getCell(`A${row}`).dataValidation = { type: 'list', allowBlank: false, formulae: ['"单选题,多选题,判断题,填空题,案例题"'] }
    sheet.getCell(`B${row}`).dataValidation = { type: 'list', allowBlank: false, formulae: ['"简单,中等,困难"'] }
    sheet.getCell(`P${row}`).dataValidation = { type: 'list', allowBlank: true, formulae: ['"是,否"'] }
  }
  return workbook.xlsx.writeBuffer()
}

export async function downloadTemplate() {
  const data = await createTemplateBuffer()
  const url = URL.createObjectURL(new Blob([data], { type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' }))
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = '安知题库导入模板.xlsx'
  anchor.click()
  URL.revokeObjectURL(url)
}
