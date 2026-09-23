// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { Progress, Question, QuestionBank } from '../types'
import StudyHome from './StudyHome'

const baseQuestion: Omit<Question, 'id' | 'bankId' | 'type' | 'difficulty' | 'stem'> = {
  sourceRow: 2, answerMode: 'single', category: '示例', options: [{ key: 'A', text: '选项' }],
  answers: ['A'], source: '', lifesaving: false, note: '', caseContext: '',
}
const questions: Question[] = [
  { ...baseQuestion, id: 'one', bankId: 'bank-one', type: '判断题', difficulty: '简单', stem: '第一题' },
  { ...baseQuestion, id: 'two', bankId: 'bank-two', type: '计算题', difficulty: '困难', stem: '第二题' },
]
const banks: QuestionBank[] = [
  { id: 'bank-one', name: '题库一', sourceFile: 'one.xlsx', questionCount: 1, types: ['判断题'], difficulties: ['简单'], importedAt: '2026-01-01' },
  { id: 'bank-two', name: '题库二', sourceFile: 'two.xlsx', questionCount: 1, types: ['计算题'], difficulties: ['困难'], importedAt: '2026-01-01' },
]
const onStart = vi.fn()
const onClearWrong = vi.fn(async (_ids: string[]) => {})
const props = { banks, questions, progress: new Map<string, Progress>(), activeSession: null, onContinue: vi.fn(), onStart, onClearWrong, onSettings: vi.fn() }

beforeEach(() => localStorage.clear())
afterEach(() => { cleanup(); vi.clearAllMocks() })

describe('study home selections', () => {
  it('remembers the chosen bank and controls, and shows only that bank’s types and difficulties', async () => {
    const view = render(<StudyHome {...props} />)
    fireEvent.change(screen.getByLabelText('题库'), { target: { value: 'bank-one' } })
    expect(within(screen.getByLabelText('题型')).queryByText('计算题')).toBeNull()
    expect(within(screen.getByLabelText('难度')).queryByText('困难')).toBeNull()
    fireEvent.change(screen.getByLabelText('题型'), { target: { value: '判断题' } })
    fireEvent.change(screen.getByLabelText('难度'), { target: { value: '简单' } })
    fireEvent.click(screen.getByRole('button', { name: '50' }))
    fireEvent.click(screen.getByRole('button', { name: '顺序' }))
    await waitFor(() => expect(JSON.parse(localStorage.getItem('anzhi-practice-config-v1') ?? '{}')).toMatchObject({ bankId: 'bank-one', type: '判断题', difficulty: '简单', count: 50, order: 'sequential' }))
    view.unmount()

    render(<StudyHome {...props} />)
    expect((screen.getByLabelText('题库') as HTMLSelectElement).value).toBe('bank-one')
    expect((screen.getByLabelText('题型') as HTMLSelectElement).value).toBe('判断题')
    expect((screen.getByLabelText('难度') as HTMLSelectElement).value).toBe('简单')
    expect(screen.getByRole('button', { name: '50' }).className).toContain('active')
    expect(screen.getByRole('button', { name: '顺序' }).className).toContain('active')

    fireEvent.change(screen.getByLabelText('题库'), { target: { value: 'bank-two' } })
    expect((screen.getByLabelText('题型') as HTMLSelectElement).value).toBe('全部题型')
    expect((screen.getByLabelText('难度') as HTMLSelectElement).value).toBe('全部难度')
  })

  it('opens wrong answers by bank and clears only the selected bank', async () => {
    const progress = new Map<string, Progress>([
      ['one', { questionId: 'one', attempts: 1, correct: 0, wrong: 1, starred: false }],
      ['two', { questionId: 'two', attempts: 1, correct: 0, wrong: 1, starred: false }],
    ])
    render(<StudyHome {...props} progress={progress} />)
    fireEvent.change(screen.getByLabelText('题库'), { target: { value: 'bank-one' } })
    fireEvent.click(screen.getByRole('button', { name: /错题集/ }))
    const dialog = screen.getByRole('dialog', { name: '错题集' })
    expect(within(dialog).getByText('第一题')).toBeTruthy()
    expect(within(dialog).queryByText('第二题')).toBeNull()
    fireEvent.click(within(dialog).getByRole('button', { name: /移除错题：第一题/ }))
    await waitFor(() => expect(onClearWrong).toHaveBeenCalledWith(['one']))
  })

  it('groups wrong answers by bank when all banks are selected', () => {
    const progress = new Map<string, Progress>([
      ['one', { questionId: 'one', attempts: 1, correct: 0, wrong: 1, starred: false }],
      ['two', { questionId: 'two', attempts: 1, correct: 0, wrong: 1, starred: false }],
    ])
    render(<StudyHome {...props} progress={progress} />)
    fireEvent.click(screen.getByRole('button', { name: /错题集/ }))
    const dialog = screen.getByRole('dialog', { name: '错题集' })
    expect(within(dialog).getByText('题库一')).toBeTruthy()
    expect(within(dialog).getByText('题库二')).toBeTruthy()
    expect(within(dialog).queryByText('第一题')).toBeNull()
    expect(within(dialog).queryByText('第二题')).toBeNull()
    fireEvent.click(within(dialog).getByRole('button', { name: /题库二/ }))
    expect(within(dialog).getByText('第二题')).toBeTruthy()
    expect(within(dialog).queryByText('第一题')).toBeNull()
  })
})
