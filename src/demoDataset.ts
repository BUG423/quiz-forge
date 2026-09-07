import type { QuestionDataset } from './types'

export const demoDataset: QuestionDataset = {
  schemaVersion: 1,
  sourceVersion: 'demo-elementary-arithmetic-v1',
  generatedAt: '2026-09-07T00:00:00.000Z',
  banks: [{
    id: 'demo-elementary-arithmetic',
    name: '小学加减法（演示）',
    sourceFile: '内置演示数据',
    questionCount: 4,
    difficulties: ['简单'],
    types: ['单选题', '多选题', '判断题', '填空题'],
    importedAt: '2026-09-07',
  }],
  questions: [
    {
      id: 'demo-arithmetic-single', bankId: 'demo-elementary-arithmetic', sourceRow: 1,
      type: '单选题', answerMode: 'single', difficulty: '简单', category: '小学数学示例',
      stem: '2 + 3 = ?', options: [{ key: 'A', text: '4' }, { key: 'B', text: '5' }, { key: 'C', text: '6' }, { key: 'D', text: '7' }],
      answers: ['B'], source: '', lifesaving: false, note: '', caseContext: '',
    },
    {
      id: 'demo-arithmetic-multiple', bankId: 'demo-elementary-arithmetic', sourceRow: 2,
      type: '多选题', answerMode: 'multiple', difficulty: '简单', category: '小学数学示例',
      stem: '以下哪些算式的结果等于 6？', options: [{ key: 'A', text: '1 + 5' }, { key: 'B', text: '2 + 4' }, { key: 'C', text: '3 + 4' }, { key: 'D', text: '8 - 1' }],
      answers: ['A', 'B'], source: '', lifesaving: false, note: '', caseContext: '',
    },
    {
      id: 'demo-arithmetic-judge', bankId: 'demo-elementary-arithmetic', sourceRow: 3,
      type: '判断题', answerMode: 'judge', difficulty: '简单', category: '小学数学示例',
      stem: '7 - 2 = 5。', options: [], answers: ['T'], source: '', lifesaving: false, note: '', caseContext: '',
    },
    {
      id: 'demo-arithmetic-fill', bankId: 'demo-elementary-arithmetic', sourceRow: 4,
      type: '填空题', answerMode: 'fill', difficulty: '简单', category: '小学数学示例',
      stem: '9 - 4 = __。', options: [], answers: ['5'], source: '', lifesaving: false, note: '', caseContext: '',
    },
  ],
}
