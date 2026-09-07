export type AnswerMode = 'single' | 'multiple' | 'judge' | 'fill'
export type QuizMode = 'practice' | 'exam'

export interface QuestionOption {
  key: string
  text: string
}

export interface Question {
  id: string
  bankId: string
  sourceRow: number
  type: string
  answerMode: AnswerMode
  difficulty: string
  category: string
  stem: string
  options: QuestionOption[]
  answers: string[]
  source: string
  lifesaving: boolean
  note: string
  caseContext: string
}

export interface QuestionBank {
  id: string
  name: string
  sourceFile: string
  questionCount: number
  difficulties: string[]
  types: string[]
  importedAt: string
  origin?: 'bundled' | 'upload'
}

export interface Progress {
  questionId: string
  attempts: number
  correct: number
  wrong: number
  lastAnsweredAt?: string
  starred: boolean
}

export interface QuestionDataset {
  schemaVersion: number
  sourceVersion: string
  generatedAt: string
  banks: QuestionBank[]
  questions: Question[]
}

export interface ImportResult {
  bank: QuestionBank
  questions: Question[]
  warnings: string[]
}

export interface PracticeConfig {
  bankId: string
  count: number
  order: 'random' | 'sequential'
  type: string
  difficulty: string
  onlyLifesaving: boolean
}

export interface PracticeSession {
  id: 'active'
  mode: QuizMode
  questionIds: string[]
  index: number
  responses: Record<string, string[]>
  judgements: Record<string, boolean>
  startedAt: string
  updatedAt: string
}
