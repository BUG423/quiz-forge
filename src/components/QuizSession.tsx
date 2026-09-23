import { useEffect, useMemo, useRef, useState } from 'react'
import { ArrowLeft, ArrowRight, Check, CheckCircle2, ChevronLeft, ChevronRight, CircleX, Flag, Heart, Layers3, Settings, Sparkles, X } from 'lucide-react'
import type { PracticeSession, Progress, Question, QuizMode } from '../types'
import { displayAnswer, isAnswerCorrect } from '../utils/answers'

interface QuizSessionProps {
  questions: Question[]
  progress: Map<string, Progress>
  mode?: QuizMode
  initialSession?: PracticeSession
  onSessionChange?: (session: PracticeSession) => void
  onComplete?: () => void
  onRecord: (questionId: string, correct: boolean) => void
  onToggleStar: (questionId: string) => void
  onExit: () => void
  onSettings: () => void
}

export default function QuizSession({ questions, progress, mode = 'practice', initialSession, onSessionChange, onComplete, onRecord, onToggleStar, onExit, onSettings }: QuizSessionProps) {
  const [index, setIndex] = useState(() => Math.min(initialSession?.index ?? 0, questions.length - 1))
  const [responses, setResponses] = useState<Record<string, string[]>>(() => initialSession?.responses ?? {})
  const [judgements, setJudgements] = useState<Record<string, boolean>>(() => initialSession?.judgements ?? {})
  const [result, setResult] = useState<{ answeredCount: number; correctCount: number; totalCount: number } | null>(null)
  const completed = useRef(false)
  const startedAt = useRef(initialSession?.startedAt ?? new Date().toISOString())
  const touchStart = useRef<{ x: number; y: number } | null>(null)
  const question = questions[index]
  const selected = responses[question?.id] ?? []
  const submitted = question ? Object.hasOwn(judgements, question.id) : false
  const correctCount = Object.values(judgements).filter(Boolean).length
  const answeredCount = Object.keys(judgements).length
  const snapshot = (): PracticeSession => ({ id: 'active', mode, questionIds: questions.map((item) => item.id), index, responses, judgements, startedAt: startedAt.current, updatedAt: new Date().toISOString() })

  useEffect(() => {
    if (!result) onSessionChange?.(snapshot())
  }, [result, index, judgements, responses])

  const setAnswer = (answer: string[]) => {
    setResponses((current) => ({ ...current, [question.id]: answer }))
  }

  const grade = (answer: string[]) => {
    if (submitted || !answer.length || (question.answerMode === 'fill' && !answer[0].trim())) return
    const correct = isAnswerCorrect(question.answerMode, answer, question.answers)
    setAnswer(answer)
    setJudgements((current) => ({ ...current, [question.id]: correct }))
    onRecord(question.id, correct)
  }

  const choose = (key: string) => {
    if (submitted) return
    if (question.answerMode === 'multiple') {
      setAnswer(selected.includes(key) ? selected.filter((item) => item !== key) : [...selected, key])
      return
    }
    if (mode === 'exam') {
      setAnswer([key])
      return
    }
    grade([key])
  }

  const move = (direction: -1 | 1) => {
    const nextIndex = index + direction
    if (nextIndex >= 0 && nextIndex < questions.length) setIndex(nextIndex)
  }

  const handleTouchEnd = (event: React.TouchEvent) => {
    if (!touchStart.current) return
    const deltaX = event.changedTouches[0].clientX - touchStart.current.x
    const deltaY = event.changedTouches[0].clientY - touchStart.current.y
    touchStart.current = null
    if (Math.abs(deltaX) < 55 || Math.abs(deltaX) < Math.abs(deltaY) * 1.25) return
    move(deltaX > 0 ? -1 : 1)
  }

  const unanswered = useMemo(() => questions.filter((item) => !Object.hasOwn(judgements, item.id)), [judgements, questions])

  const exitSession = () => {
    if (!result) onSessionChange?.(snapshot())
    onExit()
  }

  const finishSession = () => {
    if (completed.current) return
    completed.current = true
    let finalJudgements = judgements
    if (mode === 'exam') {
      finalJudgements = {}
      for (const item of questions) {
        const answer = responses[item.id] ?? []
        if (!answer.length || (item.answerMode === 'fill' && !answer[0]?.trim())) continue
        const correct = isAnswerCorrect(item.answerMode, answer, item.answers)
        finalJudgements[item.id] = correct
        onRecord(item.id, correct)
      }
    }
    setResult({
      answeredCount: questions.filter((item) => Object.hasOwn(finalJudgements, item.id)).length,
      correctCount: questions.filter((item) => finalJudgements[item.id]).length,
      totalCount: questions.length,
    })
    onComplete?.()
  }

  if (result) {
    const { answeredCount: resultAnsweredCount, correctCount: resultCorrectCount, totalCount } = result
    const scoreDenominator = mode === 'exam' ? totalCount : resultAnsweredCount
    const score = scoreDenominator ? Math.round(resultCorrectCount / scoreDenominator * 100) : 0
    return <main className="quiz-screen result-screen"><section className="frost-card simple-result"><span className="result-spark"><Sparkles size={30} /></span><small>{mode === 'exam' ? '模拟考试' : '本次练习'}</small><h1>{score}<em>分</em></h1><p>共 {totalCount} 题 · 答对 {resultCorrectCount} 题 · 答错 {resultAnsweredCount - resultCorrectCount} 题 · 跳过 {totalCount - resultAnsweredCount} 题</p><div className="result-bars"><span><i style={{ width: `${totalCount ? resultAnsweredCount / totalCount * 100 : 0}%` }} /></span></div><div className="result-buttons">{mode === 'practice' && unanswered.length > 0 && <button className="glass-button" onClick={() => { setIndex(questions.findIndex((item) => item.id === unanswered[0].id)); completed.current = false; setResult(null) }}>继续完成 {unanswered.length} 道题</button>}<button className="dark-button" onClick={onExit}>返回首页</button></div></section></main>
  }

  const displayOptions = question.answerMode === 'judge' ? [{ key: 'T', text: '正确' }, { key: 'F', text: '错误' }] : question.options
  return (
    <main className="quiz-screen">
      <header className="quiz-topbar">
        <button className="circle-control" onClick={exitSession} aria-label="退出练习"><X size={19} /></button>
        <div className="quiz-counter"><b>{index + 1}</b><span>/ {questions.length}</span><i><em style={{ width: `${answeredCount / questions.length * 100}%` }} /></i></div>
        {mode === 'exam' ? <button className="exam-hand-in" onClick={finishSession}>交卷</button> : <button className="circle-control" onClick={onSettings} aria-label="打开设置"><Settings size={19} /></button>}
      </header>

      <section className="question-swipe-zone" onTouchStart={(event) => { touchStart.current = { x: event.touches[0].clientX, y: event.touches[0].clientY } }} onTouchEnd={handleTouchEnd}>
        <article className="frost-card clean-question">
          <div className="clean-meta"><span>{question.type}</span><span>{question.difficulty}</span><span>{question.category}</span>{question.lifesaving && <span className="life-mark"><Flag size={11} fill="currentColor" />保命题</span>}<button className={progress.get(question.id)?.starred ? 'active' : ''} onClick={() => onToggleStar(question.id)}><Heart size={18} fill={progress.get(question.id)?.starred ? 'currentColor' : 'none'} /></button></div>
          {question.caseContext && <div className="clean-case"><span><Layers3 size={14} />案例材料</span><p>{question.caseContext}</p></div>}
          <h1>{question.stem}</h1>
          {question.answerMode === 'fill' ? <input className="clean-fill" value={selected[0] ?? ''} onChange={(event) => setAnswer([event.target.value])} disabled={submitted} placeholder="请输入答案" /> : <div className="clean-options">{displayOptions.map((option) => {
            const picked = selected.includes(option.key)
            const isCorrectOption = submitted && question.answers.includes(option.key)
            const isWrongOption = submitted && picked && !question.answers.includes(option.key)
            return <button key={option.key} className={`${picked ? 'picked' : ''} ${isCorrectOption ? 'right' : ''} ${isWrongOption ? 'wrong' : ''}`} onClick={() => choose(option.key)}><i>{question.answerMode === 'judge' ? (option.key === 'T' ? <Check size={17} /> : <CircleX size={17} />) : option.key}</i><span>{option.text}</span>{isCorrectOption && <CheckCircle2 size={19} />}{isWrongOption && <CircleX size={19} />}</button>
          })}</div>}
          {submitted && <section className={`clean-analysis ${judgements[question.id] ? 'right' : 'wrong'}`}><div><span>{judgements[question.id] ? <CheckCircle2 size={20} /> : <CircleX size={20} />}</span><p><b>{judgements[question.id] ? '回答正确' : '回答错误'}</b><small>正确答案：{displayAnswer(question.answerMode, question.answers)}</small></p></div>{question.source && <details open><summary>规范依据</summary><p>{question.source}</p></details>}{question.note && <details><summary>备注</summary><p>{question.note}</p></details>}</section>}
          <div className="swipe-hint"><ArrowLeft size={12} /> 左右滑动切题 <ArrowRight size={12} /></div>
        </article>
      </section>

      <footer className="answer-dock">
        <button className="dock-arrow" onClick={() => move(-1)} disabled={index === 0}><ChevronLeft size={22} /><span>上一题</span></button>
        <div className="dock-center">{mode === 'exam' ? <span>考试中不显示答案</span> : !submitted && (question.answerMode === 'multiple' || question.answerMode === 'fill') ? <button className="submit-choice" disabled={!selected.length || (question.answerMode === 'fill' && !selected[0]?.trim())} onClick={() => grade(selected)}>{question.answerMode === 'multiple' ? '提交答案' : '确认答案'}</button> : !submitted ? <span>点击选项立即判题</span> : <span className={judgements[question.id] ? 'answered-right' : 'answered-wrong'}>{judgements[question.id] ? '回答正确' : '已查看解析'}</span>}</div>
        {index === questions.length - 1 ? <button className="dock-arrow finish" onClick={finishSession}><span>{mode === 'exam' ? '交卷' : '完成练习'}</span><CheckCircle2 size={21} /></button> : <button className="dock-arrow" onClick={() => move(1)}><span>下一题</span><ChevronRight size={22} /></button>}
      </footer>
    </main>
  )
}
