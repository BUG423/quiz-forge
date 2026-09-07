import 'fake-indexeddb/auto'
import { afterAll, describe, expect, it, vi } from 'vitest'
import { clearActivePracticeSession, initializeDatabase, loadActivePracticeSession, loadLibrary, replaceBank, saveActivePracticeSession, saveProgress } from './database'
import type { PracticeSession, Question, QuestionDataset } from '../types'

const sampleQuestion: Question = {
  id: 'q-1', bankId: 'bank-1', sourceRow: 2, type: '单选题', answerMode: 'single',
  difficulty: '简单', category: '[CATEGORY]', stem: '[QUESTION_STEM]', options: [{ key: 'A', text: '[OPTION_A]' }],
  answers: ['A'], source: '', lifesaving: false, note: '', caseContext: '',
}

const dataset: QuestionDataset = {
  schemaVersion: 1, sourceVersion: 'test-version', generatedAt: '2026-09-04',
  banks: [{ id: 'bank-1', name: '[BUNDLED_BANK]', sourceFile: '[BUNDLED_BANK].xlsx', questionCount: 1, difficulties: ['简单'], types: ['单选题'], importedAt: '2026-09-04' }],
  questions: [sampleQuestion],
}

describe('local question repository', () => {
  it('seeds, records progress, and replaces an uploaded bank', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, json: async () => dataset }))
    await initializeDatabase()
    await saveProgress({ questionId: 'q-1', attempts: 1, correct: 1, wrong: 0, starred: true })
    await replaceBank({ id: 'bank-upload', name: '[UPLOADED_BANK]', sourceFile: '[UPLOADED_BANK].xlsx', questionCount: 1, difficulties: ['简单'], types: ['单选题'], importedAt: '2026-09-04', origin: 'upload' }, [{ ...sampleQuestion, id: 'q-2', bankId: 'bank-upload' }])

    const library = await loadLibrary()
    expect(library.banks).toHaveLength(2)
    expect(library.questions.map((question) => question.id)).toEqual(expect.arrayContaining(['q-1', 'q-2']))
    expect(library.progress[0]).toMatchObject({ questionId: 'q-1', correct: 1, starred: true })
  })

  it('persists and clears an unfinished practice session', async () => {
    const session: PracticeSession = { id: 'active', mode: 'practice', questionIds: ['q-1'], index: 0, responses: { 'q-1': ['A'] }, judgements: { 'q-1': true }, startedAt: '2026-09-04T08:00:00.000Z', updatedAt: '2026-09-04T08:01:00.000Z' }
    await saveActivePracticeSession(session)
    expect(await loadActivePracticeSession()).toEqual(session)
    await clearActivePracticeSession()
    expect(await loadActivePracticeSession()).toBeUndefined()
  })
})

afterAll(() => vi.unstubAllGlobals())
