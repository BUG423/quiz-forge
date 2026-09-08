import ExcelJS from 'exceljs'
import { describe, expect, it } from 'vitest'
import { parseExcelFile } from './excelImporter'

describe('Excel importer', () => {
  it('imports choices and attaches case material to its sub-question', async () => {
    const workbook = new ExcelJS.Workbook()
    const sheet = workbook.addWorksheet('SHEET')
    sheet.addRow(['题型', '难易度', '知识类别', '题干', 'A', 'B', 'C', 'D', 'E', 'F', 'G', 'H', 'I', '答案', '出自规范', '是否保命题', '备注(填空题多个填空答案，请用英文逗号隔开)'])
    sheet.addRow(['单选题', '简单', '[CATEGORY]', '[QUESTION_1]', '[OPTION_A]', '[OPTION_B]', '', '', '', '', '', '', '', 'A', '[SOURCE_1]', '是', ''])
    sheet.addRow(['案例题', '中等', '[CATEGORY]', '[CASE_CONTEXT]', '', '', '', '', '', '', '', '', '', '', '[SOURCE_2]', '否', ''])
    sheet.addRow(['案例题', '中等', '[CATEGORY]', '[QUESTION_2]', '[OPTION_A]', '[OPTION_B]', '', '', '', '', '', '', '', 'A', '[SOURCE_2]', '否', ''])
    const data = await workbook.xlsx.writeBuffer()
    const bytes = new Uint8Array(data as ArrayBuffer)
    const file = new File([bytes.buffer as ArrayBuffer], '[TEST_BANK].xlsx')

    const result = await parseExcelFile(file)
    expect(result.bank.name).toBe('[TEST_BANK]')
    expect(result.questions).toHaveLength(2)
    expect(result.questions[0]).toMatchObject({ answerMode: 'single', answers: ['A'], lifesaving: true })
    expect(result.questions[1].caseContext).toBe('[CASE_CONTEXT]')
  })

  it('detects shifted alias headers, cleans option prefixes, and ignores hidden legacy sheets', async () => {
    const workbook = new ExcelJS.Workbook()
    const cover = workbook.addWorksheet('README')
    cover.addRow(['[IMPORT_GUIDE]'])

    const sheet = workbook.addWorksheet('CHOICE_SHEET')
    sheet.addRow(['[LOCAL_BANK_TITLE]'])
    const headers = ['序号', '维度', '模块', '能力单元', '', '能力要素', '', '评价内容', '', '所属等级', '试题编号', '题型', '题干', '选项A', '选项B', '选项C', '选项D', '选项E', '选项F', '答案', '试题难易']
    sheet.addRow(headers)
    sheet.addRow(headers)
    sheet.addRow([1, '[DIMENSION]', '[MODULE]', '[UNIT]', '', '[ELEMENT]', '', '[CATEGORY]', '', '[LEVEL]', '[ID_1]', '单选', '[QUESTION_1]', 'A、[OPTION_A]', 'B、[OPTION_B]', '', '', '', '', 'A', '容易'])
    sheet.addRow([2, '[DIMENSION]', '[MODULE]', '[UNIT]', '', '[ELEMENT]', '', '[CATEGORY]', '', '[LEVEL]', '[ID_2]', '判断', '[QUESTION_2]', 'A、错误', 'B、正确', '', '', '', '', 'B', '中等'])
    sheet.addRow([3, '[DIMENSION]', '[MODULE]', '[UNIT]', '', '[ELEMENT]', '', '[CATEGORY]', '', '[LEVEL]', '[ID_3]', '多选', '[QUESTION_3]', 'A、[OPTION_A]', 'B、[OPTION_B]', 'C、[OPTION_C]', '', '', '', 'AC', '较难'])

    const open = workbook.addWorksheet('OPEN_SHEET')
    open.addRow(['[LOCAL_BANK_TITLE]'])
    open.addRow(['序号', '维度', '模块', '能力单元', '', '能力要素', '', '评价内容', '', '所属等级', '试题编号', '题型', '题干', '答案', '试题难易'])
    open.addRow([1, '[DIMENSION]', '[MODULE]', '[UNIT]', '', '[ELEMENT]', '', '[CATEGORY]', '', '[LEVEL]', '[ID_4]', '简答', '[QUESTION_4]', '[LONG_REFERENCE_ANSWER]', '中等'])

    const hidden = workbook.addWorksheet('HIDDEN_LEGACY_SHEET', { state: 'hidden' })
    hidden.addRow(['题型', '题干', 'A', 'B', '答案'])
    hidden.addRow(['单选题', '[SHOULD_NOT_IMPORT]', '[OPTION_A]', '[OPTION_B]', 'A'])
    hidden.getCell('XFB3').value = '[PHANTOM_CELL]'

    const data = await workbook.xlsx.writeBuffer()
    const bytes = new Uint8Array(data as ArrayBuffer)
    const file = new File([bytes.buffer as ArrayBuffer], '[ALIAS_BANK].xlsx')

    const result = await parseExcelFile(file)
    expect(result.questions).toHaveLength(4)
    expect(result.questions.map((question) => question.type)).toEqual(['单选题', '判断题', '多选题', '简答题'])
    expect(result.questions[0]).toMatchObject({
      category: '[CATEGORY]',
      difficulty: '容易',
      options: [{ key: 'A', text: '[OPTION_A]' }, { key: 'B', text: '[OPTION_B]' }],
      answers: ['A'],
    })
    expect(result.questions[1]).toMatchObject({ answerMode: 'judge', answers: ['T'] })
    expect(result.questions[2]).toMatchObject({ answerMode: 'multiple', answers: ['A', 'C'] })
    expect(result.questions[3]).toMatchObject({ answerMode: 'fill', answers: ['[LONG_REFERENCE_ANSWER]'] })
    expect(result.questions.some((question) => question.stem === '[SHOULD_NOT_IMPORT]')).toBe(false)
    expect(result.warnings).toEqual(['README 未找到“题干”和“答案”列，已跳过'])
  })
})
