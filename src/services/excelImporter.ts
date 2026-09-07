import type ExcelJS from 'exceljs'
import type { AnswerMode, ImportResult, Question, QuestionOption } from '../types'

const OPTION_KEYS = ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H', 'I']
const REQUIRED_HEADERS = ['题型', '难易度', '知识类别', '题干', '答案']
const NOTE_HEADER = '备注(填空题多个填空答案，请用英文逗号隔开)'

function stableHash(value: string) {
  let hash = 2166136261
  for (let index = 0; index < value.length; index += 1) {
    hash ^= value.charCodeAt(index)
    hash = Math.imul(hash, 16777619)
  }
  return (hash >>> 0).toString(36)
}

function cellText(value: ExcelJS.CellValue) {
  if (value === null || value === undefined) return ''
  if (typeof value === 'object') {
    if ('richText' in value) return value.richText.map((part) => part.text).join('').trim()
    if ('text' in value) return String(value.text).trim()
    if ('result' in value) return String(value.result ?? '').trim()
  }
  return String(value).trim()
}

function parseAnswer(rawType: string, rawAnswer: string, options: QuestionOption[]): { mode: AnswerMode; answers: string[] } {
  const type = rawType.replaceAll(' ', '')
  if (type.includes('填空')) {
    return { mode: 'fill', answers: rawAnswer.split(/[,，]/).map((item) => item.trim()).filter(Boolean) }
  }
  if (type.includes('判断')) {
    const normalized = rawAnswer.toLowerCase()
    if (['正确', '对', '是', '√', 'true', 't', 'a'].includes(normalized)) return { mode: 'judge', answers: ['T'] }
    if (['错误', '错', '否', '×', 'false', 'f', 'b'].includes(normalized)) return { mode: 'judge', answers: ['F'] }
  }
  const validKeys = new Set(options.map((option) => option.key))
  const answers = [...rawAnswer.toUpperCase().matchAll(/[A-I]/g)]
    .map((match) => match[0])
    .filter((key, index, all) => validKeys.has(key) && all.indexOf(key) === index)
  return { mode: type.includes('多选') || answers.length > 1 ? 'multiple' : 'single', answers }
}

function normalizedType(rawType: string, mode: AnswerMode) {
  if (rawType.includes('案例')) return '案例题'
  if (mode === 'multiple') return '多选题'
  if (mode === 'judge') return '判断题'
  if (mode === 'fill') return '填空题'
  return '单选题'
}

export async function parseExcelFile(file: File): Promise<ImportResult> {
  const ExcelJSRuntime = (await import('exceljs')).default
  const workbook = new ExcelJSRuntime.Workbook()
  await workbook.xlsx.load(await file.arrayBuffer())
  const name = file.name.replace(/\.xlsx?$/i, '').trim() || '未命名题库'
  const bankId = `bank-${stableHash(name)}`
  const questions: Question[] = []
  const warnings: string[] = []

  for (const sheet of workbook.worksheets) {
    const headers = new Map<string, number>()
    sheet.getRow(1).eachCell({ includeEmpty: false }, (cell, number) => headers.set(cellText(cell.value), number))
    const missing = REQUIRED_HEADERS.filter((header) => !headers.has(header))
    if (missing.length) throw new Error(`${sheet.name} 缺少必填列：${missing.join('、')}`)
    const col = (header: string) => headers.get(header)
    let caseContext = ''

    for (let rowNumber = 2; rowNumber <= sheet.actualRowCount; rowNumber += 1) {
      const row = sheet.getRow(rowNumber)
      const get = (header: string) => {
        const column = col(header)
        return column ? cellText(row.getCell(column).value) : ''
      }
      const rawType = get('题型')
      const stem = get('题干')
      if (!stem) continue
      const rawAnswer = get('答案')
      const options = OPTION_KEYS.map((key) => ({ key, text: get(key) })).filter((option) => option.text)

      if (rawType.includes('案例') && !rawAnswer && options.length === 0) {
        caseContext = stem
        continue
      }
      if (!rawType.includes('案例')) caseContext = ''
      const parsed = parseAnswer(rawType, rawAnswer, options)
      if (!parsed.answers.length) {
        warnings.push(`${sheet.name} 第 ${rowNumber} 行未识别到答案，已跳过`)
        continue
      }
      const type = normalizedType(rawType, parsed.mode)
      questions.push({
        id: `q-${stableHash(`${name}|${sheet.name}|${rowNumber}|${stem}`)}`,
        bankId,
        sourceRow: rowNumber,
        type,
        answerMode: parsed.mode,
        difficulty: get('难易度') || '未标注',
        category: get('知识类别') || '未分类',
        stem,
        options,
        answers: parsed.answers,
        source: get('出自规范'),
        lifesaving: get('是否保命题') === '是',
        note: get(NOTE_HEADER),
        caseContext: rawType.includes('案例') ? caseContext : '',
      })
    }
  }

  if (!questions.length) throw new Error('文件中没有可导入的有效题目')
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
