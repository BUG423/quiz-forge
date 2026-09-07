import fs from 'node:fs/promises'
import path from 'node:path'
import process from 'node:process'
import ExcelJS from 'exceljs'

const outputFile = path.resolve(process.argv[2] ?? 'release/quiz-forge-import-template.xlsx')
const optionKeys = ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H', 'I']
const noteHeader = '备注(填空题多个填空答案，请用英文逗号隔开)'
const headers = ['题型', '难易度', '知识类别', '题干', ...optionKeys, '答案', '出自规范', '是否保命题', noteHeader]

const workbook = new ExcelJS.Workbook()
const sheet = workbook.addWorksheet('题库题目')
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

await fs.mkdir(path.dirname(outputFile), { recursive: true })
await workbook.xlsx.writeFile(outputFile)
console.log(`已生成导入模板：${outputFile}`)
