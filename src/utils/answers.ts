import type { AnswerMode } from '../types'

export function normalizeFillAnswer(value: string) {
  return value
    .trim()
    .replace(/[，、]/g, ',')
    .replace(/\s+/g, '')
    .toLowerCase()
}

export function isAnswerCorrect(mode: AnswerMode, selected: string[], answers: string[]) {
  if (mode === 'fill') {
    return normalizeFillAnswer(selected[0] ?? '') === normalizeFillAnswer(answers.join(','))
  }
  const left = [...selected].sort().join(',')
  const right = [...answers].sort().join(',')
  return left === right
}

export function displayAnswer(mode: AnswerMode, answers: string[]) {
  if (mode === 'judge') return answers[0] === 'T' ? '正确' : '错误'
  if (mode === 'fill') return answers.join('，')
  return answers.join('、')
}

export function shouldGradeChoice(mode: AnswerMode, selected: string[], clicked: string, answers: string[]) {
  if (mode !== 'multiple') return true
  const nextSelection = [...selected, clicked]
  return !answers.includes(clicked) || answers.every((answer) => nextSelection.includes(answer))
}
