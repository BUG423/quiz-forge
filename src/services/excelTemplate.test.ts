import { describe, expect, it } from 'vitest'
import { createTemplateBuffer, parseExcelFile } from './excelImporter'
import { isAnswerCorrect } from '../utils/answers'

describe('elementary arithmetic Excel demonstration', () => {
  it('generates a real workbook and imports it through the production parser', async () => {
    const template = await createTemplateBuffer()
    const bytes = new Uint8Array(template as ArrayBuffer)
    const file = new File([bytes.buffer as ArrayBuffer], '小学加减法演示.xlsx')

    const result = await parseExcelFile(file)

    expect(result.bank.name).toBe('小学加减法演示')
    expect(result.bank.sourceFile).toBe('小学加减法演示.xlsx')
    expect(result.questions).toHaveLength(4)
    expect(result.questions.map((question) => question.answerMode).sort()).toEqual(['fill', 'judge', 'multiple', 'single'])
    expect(result.questions.map((question) => question.answers.join(''))).toEqual(['B', 'AB', 'T', '5'])
    for (const question of result.questions) {
      expect(question.category).toBe('小学数学示例')
      expect(question.lifesaving).toBe(false)
      expect(isAnswerCorrect(question.answerMode, question.answers, question.answers)).toBe(true)
    }
  })
})
