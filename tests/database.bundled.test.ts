import 'fake-indexeddb/auto'
import { readFile } from 'node:fs/promises'
import { afterAll, describe, expect, it, vi } from 'vitest'
import type { QuestionDataset } from '../src/types'
import { isAnswerCorrect } from '../src/utils/answers'
import { initializeDatabase, loadLibrary } from '../src/services/database'

const dataDirectory = new URL('../public/data/', import.meta.url)

describe('published bundled question banks', () => {
  it('contains seven complete banks and replaces the demo on upgrade', async () => {
    const dataset = JSON.parse(await readFile(new URL('questions.json', dataDirectory), 'utf8')) as QuestionDataset
    const manifest = JSON.parse(await readFile(new URL('manifest.json', dataDirectory), 'utf8')) as {
      sourceVersion: string; bankCount: number; questionCount: number
    }

    expect(manifest).toMatchObject({ sourceVersion: dataset.sourceVersion, bankCount: 7, questionCount: 2183 })
    expect(dataset.banks).toHaveLength(7)
    expect(dataset.questions).toHaveLength(2183)
    expect(new Set(dataset.banks.map((bank) => bank.id)).size).toBe(7)
    expect(new Set(dataset.questions.map((question) => question.id)).size).toBe(2183)
    for (const bank of dataset.banks) {
      expect(dataset.questions.filter((question) => question.bankId === bank.id)).toHaveLength(bank.questionCount)
    }
    for (const question of dataset.questions) {
      expect(question.answers.length).toBeGreaterThan(0)
      expect(isAnswerCorrect(question.answerMode, question.answers, question.answers)).toBe(true)
      if (question.answerMode === 'single' || question.answerMode === 'multiple') {
        expect(question.answers.every((answer) => question.options.some((option) => option.key === answer))).toBe(true)
      }
    }

    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, status: 404 }))
    await initializeDatabase()
    expect((await loadLibrary()).banks.some((bank) => bank.id === 'demo-elementary-arithmetic')).toBe(true)

    vi.stubGlobal('fetch', vi.fn().mockImplementation(async (url: string) => ({
      ok: true,
      status: 200,
      json: async () => url.endsWith('manifest.json') ? manifest : dataset,
    })))
    await initializeDatabase()
    const library = await loadLibrary()
    expect(library.banks).toHaveLength(7)
    expect(library.questions).toHaveLength(2183)
    expect(library.banks.every((bank) => bank.origin === 'bundled')).toBe(true)
    expect(library.banks.some((bank) => bank.id === 'demo-elementary-arithmetic')).toBe(false)
  })
})

afterAll(() => vi.unstubAllGlobals())
