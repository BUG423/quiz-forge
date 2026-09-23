// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { PracticeSession, Question } from '../types'
import QuizSession from './QuizSession'

const base: Omit<Question, 'id' | 'stem' | 'type' | 'answerMode' | 'options' | 'answers'> = { bankId: 'bank-1', sourceRow: 2, difficulty: '简单', category: '[CATEGORY]', source: '[SOURCE]', lifesaving: false, note: '', caseContext: '' }
const questions: Question[] = [
  { ...base, id: 'single', stem: '[SINGLE_STEM]', type: '单选题', answerMode: 'single', options: [{ key: 'A', text: '[OPTION_A]' }, { key: 'B', text: '[OPTION_B]' }], answers: ['A'] },
  { ...base, id: 'multiple', stem: '[MULTIPLE_STEM]', type: '多选题', answerMode: 'multiple', options: [{ key: 'A', text: '[OPTION_A]' }, { key: 'B', text: '[OPTION_B]' }, { key: 'C', text: '[OPTION_C]' }], answers: ['A', 'B'] },
  { ...base, id: 'last', stem: '[JUDGE_STEM]', type: '判断题', answerMode: 'judge', options: [], answers: ['T'] },
]

const props = { progress: new Map(), onRecord: vi.fn(), onToggleStar: vi.fn(), onExit: vi.fn(), onSettings: vi.fn() }

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('quiz interaction', () => {
  it('grades single choice instantly but waits for explicit multiple-choice submission', () => {
    render(<QuizSession {...props} questions={questions} />)
    fireEvent.click(screen.getByText('[OPTION_A]'))
    expect(screen.getAllByText('回答正确').length).toBeGreaterThan(0)

    fireEvent.click(screen.getByText('下一题'))
    fireEvent.click(screen.getByText('[OPTION_A]'))
    fireEvent.click(screen.getByText('[OPTION_B]'))
    expect(screen.queryByText('正确答案：A、B')).toBeNull()
    fireEvent.click(screen.getByText('提交答案'))
    expect(screen.getByText('正确答案：A、B')).toBeTruthy()
  })

  it('allows skipping with next and navigating back by swipe', () => {
    render(<QuizSession {...props} questions={questions} />)
    fireEvent.click(screen.getByText('下一题'))
    expect(screen.getByText('[MULTIPLE_STEM]')).toBeTruthy()
    const swipeZone = screen.getByText('[MULTIPLE_STEM]').closest('.question-swipe-zone')!
    fireEvent.touchStart(swipeZone, { touches: [{ clientX: 250, clientY: 100 }] })
    fireEvent.touchEnd(swipeZone, { changedTouches: [{ clientX: 330, clientY: 105 }] })
    expect(screen.getByText('[SINGLE_STEM]')).toBeTruthy()
  })

  it('restores the exact saved question and answer state', () => {
    const initialSession: PracticeSession = { id: 'active', mode: 'practice', questionIds: questions.map((question) => question.id), index: 1, responses: { multiple: ['A', 'B'] }, judgements: {}, startedAt: '2026-09-04T08:00:00.000Z', updatedAt: '2026-09-04T08:01:00.000Z' }
    render(<QuizSession {...props} questions={questions} initialSession={initialSession} />)
    expect(screen.getByText('[MULTIPLE_STEM]')).toBeTruthy()
    expect(screen.getByText('[OPTION_A]').closest('button')?.classList.contains('picked')).toBe(true)
    expect(screen.getByText('[OPTION_B]').closest('button')?.classList.contains('picked')).toBe(true)
  })

  it('grades an exam once and scores skipped questions against the full paper', () => {
    const onComplete = vi.fn()
    render(<QuizSession {...props} questions={questions} mode="exam" onComplete={onComplete} />)
    fireEvent.click(screen.getByText('[OPTION_A]'))
    expect(screen.queryByText('回答正确')).toBeNull()
    fireEvent.click(screen.getByText('下一题'))
    fireEvent.click(screen.getByText('[OPTION_C]'))
    fireEvent.click(screen.getByText('下一题'))
    const handIn = screen.getAllByText('交卷')[0]
    act(() => {
      fireEvent.click(handIn)
      fireEvent.click(handIn)
    })
    expect(screen.getByText('33')).toBeTruthy()
    expect(screen.getByText('共 3 题 · 答对 1 题 · 答错 1 题 · 跳过 1 题')).toBeTruthy()
    expect(props.onRecord).toHaveBeenCalledWith('single', true)
    expect(props.onRecord).toHaveBeenCalledWith('multiple', false)
    expect(props.onRecord).toHaveBeenCalledTimes(2)
    expect(onComplete).toHaveBeenCalledTimes(1)
  })

  it('keeps the practice score based on submitted answers and can resume skipped questions', () => {
    render(<QuizSession {...props} questions={questions} />)
    fireEvent.click(screen.getByText('[OPTION_A]'))
    fireEvent.click(screen.getByText('下一题'))
    fireEvent.click(screen.getByText('下一题'))
    fireEvent.click(screen.getByText('完成练习'))
    expect(screen.getByText('100')).toBeTruthy()
    expect(screen.getByText('共 3 题 · 答对 1 题 · 答错 0 题 · 跳过 2 题')).toBeTruthy()
    fireEvent.click(screen.getByText('继续完成 2 道题'))
    expect(screen.getByText('[MULTIPLE_STEM]')).toBeTruthy()
  })
})
