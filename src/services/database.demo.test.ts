import 'fake-indexeddb/auto'
import { afterAll, describe, expect, it, vi } from 'vitest'
import { demoDataset } from '../demoDataset'
import { initializeDatabase, loadLibrary } from './database'
import { isAnswerCorrect } from '../utils/answers'

describe('architecture-only demo startup', () => {
  it('loads and grades the elementary arithmetic demo when private data is absent', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, status: 404 }))

    await initializeDatabase()
    const library = await loadLibrary()

    expect(library.banks).toHaveLength(1)
    expect(library.banks[0].id).toBe('demo-elementary-arithmetic')
    expect(library.questions).toHaveLength(4)
    expect(library.questions.map((question) => question.answerMode).sort()).toEqual(['fill', 'judge', 'multiple', 'single'])
    for (const question of demoDataset.questions) {
      expect(isAnswerCorrect(question.answerMode, question.answers, question.answers)).toBe(true)
      expect(question.source).toBe('')
      expect(question.note).toBe('')
      expect(question.caseContext).toBe('')
    }
  })
})

afterAll(() => vi.unstubAllGlobals())
