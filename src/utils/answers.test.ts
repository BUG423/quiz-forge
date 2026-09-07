import { describe, expect, it } from 'vitest'
import { displayAnswer, isAnswerCorrect, normalizeFillAnswer, shouldGradeChoice } from './answers'

describe('answer evaluation', () => {
  it('ignores option order for multiple-choice questions', () => {
    expect(isAnswerCorrect('multiple', ['B', 'A'], ['A', 'B'])).toBe(true)
    expect(isAnswerCorrect('multiple', ['A'], ['A', 'B'])).toBe(false)
  })

  it('normalizes whitespace and Chinese commas in fill answers', () => {
    expect(normalizeFillAnswer(' alpha， beta ')).toBe('alpha,beta')
    expect(isAnswerCorrect('fill', ['alpha，beta'], ['alpha', 'beta'])).toBe(true)
  })

  it('renders judgement answers in Chinese', () => {
    expect(displayAnswer('judge', ['T'])).toBe('正确')
    expect(displayAnswer('judge', ['F'])).toBe('错误')
  })

  it('grades single choices immediately and multiple choices when complete or wrong', () => {
    expect(shouldGradeChoice('single', [], 'A', ['A'])).toBe(true)
    expect(shouldGradeChoice('multiple', [], 'A', ['A', 'B'])).toBe(false)
    expect(shouldGradeChoice('multiple', ['A'], 'B', ['A', 'B'])).toBe(true)
    expect(shouldGradeChoice('multiple', [], 'C', ['A', 'B'])).toBe(true)
  })
})
