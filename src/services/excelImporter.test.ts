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
})
