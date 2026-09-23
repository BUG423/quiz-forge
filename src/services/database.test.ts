import 'fake-indexeddb/auto'
import { afterAll, describe, expect, it, vi } from 'vitest'
import { clearActivePracticeSession, clearWrongProgress, deleteBank, initializeDatabase, loadActivePracticeSession, loadLibrary, replaceBank, saveActivePracticeSession, saveProgress } from './database'
import type { PracticeSession, Question, QuestionBank, QuestionDataset } from '../types'

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

function uploadedBank(id: string, questionCount: number): QuestionBank {
  return { id, name: id, sourceFile: `${id}.xlsx`, questionCount, difficulties: ['简单'], types: ['单选题'], importedAt: '2026-09-04', origin: 'upload' }
}

function question(id: string, bankId: string): Question {
  return { ...sampleQuestion, id, bankId }
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

  it('clears one wrong answer or a bank selection without changing other progress', async () => {
    await replaceBank(uploadedBank('bank-wrong-a', 2), [question('wrong-a-1', 'bank-wrong-a'), question('wrong-a-2', 'bank-wrong-a')])
    await replaceBank(uploadedBank('bank-wrong-b', 1), [question('wrong-b-1', 'bank-wrong-b')])
    await saveProgress({ questionId: 'wrong-a-1', attempts: 5, correct: 3, wrong: 2, starred: true, lastAnsweredAt: '2026-09-04T08:00:00.000Z' })
    await saveProgress({ questionId: 'wrong-a-2', attempts: 1, correct: 0, wrong: 1, starred: false })
    await saveProgress({ questionId: 'wrong-b-1', attempts: 2, correct: 1, wrong: 1, starred: true })

    await clearWrongProgress(['wrong-a-1'])
    let progress = new Map((await loadLibrary()).progress.map((item) => [item.questionId, item]))
    expect(progress.get('wrong-a-1')).toEqual({ questionId: 'wrong-a-1', attempts: 5, correct: 3, wrong: 0, starred: true, lastAnsweredAt: '2026-09-04T08:00:00.000Z' })
    expect(progress.get('wrong-a-2')?.wrong).toBe(1)
    expect(progress.get('wrong-b-1')?.wrong).toBe(1)

    await clearWrongProgress(['wrong-a-1', 'wrong-a-2', 'wrong-a-2', 'missing-question'])
    progress = new Map((await loadLibrary()).progress.map((item) => [item.questionId, item]))
    expect(progress.get('wrong-a-2')).toMatchObject({ attempts: 1, correct: 0, wrong: 0, starred: false })
    expect(progress.get('wrong-b-1')).toMatchObject({ attempts: 2, correct: 1, wrong: 1, starred: true })
    expect(progress.has('missing-question')).toBe(false)
  })

  it('removes orphan progress when an uploaded bank is replaced or deleted', async () => {
    await replaceBank(uploadedBank('bank-replaced', 2), [question('replace-keep', 'bank-replaced'), question('replace-remove', 'bank-replaced')])
    await replaceBank(uploadedBank('bank-other', 1), [question('other-keep', 'bank-other')])
    for (const questionId of ['replace-keep', 'replace-remove', 'other-keep']) {
      await saveProgress({ questionId, attempts: 1, correct: 0, wrong: 1, starred: true })
    }

    await replaceBank(uploadedBank('bank-replaced', 2), [question('replace-keep', 'bank-replaced'), question('replace-new', 'bank-replaced')])
    let progress = new Map((await loadLibrary()).progress.map((item) => [item.questionId, item]))
    expect(progress.has('replace-keep')).toBe(true)
    expect(progress.has('replace-remove')).toBe(false)
    expect(progress.has('other-keep')).toBe(true)

    await deleteBank('bank-replaced')
    progress = new Map((await loadLibrary()).progress.map((item) => [item.questionId, item]))
    expect(progress.has('replace-keep')).toBe(false)
    expect(progress.has('other-keep')).toBe(true)
  })

  it('removes progress for questions dropped by a bundled update', async () => {
    const original: QuestionDataset = {
      ...dataset,
      sourceVersion: 'refresh-original-version',
      banks: [{ ...dataset.banks[0], questionCount: 2 }],
      questions: [question('refresh-keep', 'bank-1'), question('refresh-remove', 'bank-1')],
    }
    const updated: QuestionDataset = {
      ...original,
      sourceVersion: 'refresh-updated-version',
      questions: [question('refresh-keep', 'bank-1'), question('refresh-new', 'bank-1')],
    }
    let current = original
    vi.stubGlobal('fetch', vi.fn().mockImplementation(async (url: string) => ({
      ok: true,
      json: async () => url.endsWith('manifest.json') ? { sourceVersion: current.sourceVersion } : current,
    })))
    await initializeDatabase()
    await replaceBank(uploadedBank('bank-refresh-upload', 1), [question('refresh-upload', 'bank-refresh-upload')])
    for (const questionId of ['refresh-keep', 'refresh-remove', 'refresh-upload']) {
      await saveProgress({ questionId, attempts: 1, correct: 0, wrong: 1, starred: false })
    }

    current = updated
    await initializeDatabase()
    const progress = new Map((await loadLibrary()).progress.map((item) => [item.questionId, item]))
    expect(progress.has('refresh-keep')).toBe(true)
    expect(progress.has('refresh-remove')).toBe(false)
    expect(progress.has('refresh-upload')).toBe(true)
  })
})

afterAll(() => vi.unstubAllGlobals())
