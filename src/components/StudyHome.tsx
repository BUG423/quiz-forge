import { useMemo, useState } from 'react'
import { ArrowRight, Bookmark, CheckCircle2, ChevronRight, RotateCw, Settings, Target, Timer, TriangleAlert } from 'lucide-react'
import type { PracticeConfig, PracticeSession, Progress, Question, QuestionBank, QuizMode } from '../types'

interface StudyHomeProps {
  banks: QuestionBank[]
  questions: Question[]
  progress: Map<string, Progress>
  activeSession: PracticeSession | null
  onContinue: () => void
  onStart: (questions: Question[], mode?: QuizMode) => void
  onSettings: () => void
}

function randomize<T>(items: T[]) {
  return [...items].sort(() => Math.random() - 0.5)
}

export default function StudyHome({ banks, questions, progress, activeSession, onContinue, onStart, onSettings }: StudyHomeProps) {
  const [config, setConfig] = useState<PracticeConfig>({ bankId: 'all', count: 20, order: 'random', type: '全部题型', difficulty: '全部难度', onlyLifesaving: false })
  const types = ['全部题型', ...new Set(questions.map((question) => question.type))]
  const difficulties = ['全部难度', ...new Set(questions.map((question) => question.difficulty))]
  const available = useMemo(() => questions.filter((question) =>
    (config.bankId === 'all' || question.bankId === config.bankId) &&
    (config.type === '全部题型' || question.type === config.type) &&
    (config.difficulty === '全部难度' || question.difficulty === config.difficulty) &&
    (!config.onlyLifesaving || question.lifesaving),
  ), [config, questions])

  const wrongQuestions = useMemo(() => questions
    .filter((question) => (progress.get(question.id)?.wrong ?? 0) > 0)
    .sort((left, right) => (progress.get(right.id)?.lastAnsweredAt ?? '').localeCompare(progress.get(left.id)?.lastAnsweredAt ?? '')),
  [progress, questions])
  const starredQuestions = useMemo(() => questions.filter((question) => progress.get(question.id)?.starred), [progress, questions])
  const answered = [...progress.values()].filter((item) => item.attempts > 0).length
  const attempts = [...progress.values()].reduce((total, item) => total + item.attempts, 0)
  const correct = [...progress.values()].reduce((total, item) => total + item.correct, 0)
  const starred = [...progress.values()].filter((item) => item.starred).length
  const accuracy = attempts ? Math.round(correct / attempts * 100) : 0

  const start = () => {
    const ordered = config.order === 'random' ? randomize(available) : [...available]
    onStart(config.count ? ordered.slice(0, config.count) : ordered)
  }

  const startExam = () => {
    const selected = randomize(available)
    onStart(config.count ? selected.slice(0, config.count) : selected, 'exam')
  }

  return (
    <main className="study-home">
      <header className="glass-topbar compact-topbar">
        <div className="minimal-brand" aria-label="安知刷题首页"><span>安</span><b>安知刷题</b></div>
        <div className="topbar-summary"><span><b>{answered}</b> 已练</span><i /><span><b>{accuracy}%</b> 正确率</span></div>
        <button className="circle-control" onClick={onSettings} aria-label="打开设置"><Settings size={20} /></button>
      </header>

      <div className="home-console">
        <section className="practice-console" aria-labelledby="practice-title">
          <div className="console-heading">
            <div><span className="console-icon"><Target size={19} /></span><div><h1 id="practice-title">开始练习</h1><p>当前筛选 {available.length.toLocaleString()} 道题</p></div></div>
            <label className="life-toggle"><input type="checkbox" checked={config.onlyLifesaving} onChange={(event) => setConfig({ ...config, onlyLifesaving: event.target.checked })} /><span />保命题</label>
          </div>

          <div className="console-fields">
            <label className="bank-field"><span>题库</span><select aria-label="题库" value={config.bankId} onChange={(event) => setConfig({ ...config, bankId: event.target.value })}><option value="all">全部题库（{questions.length} 题）</option>{banks.map((bank) => <option key={bank.id} value={bank.id}>{bank.name}（{bank.questionCount} 题）</option>)}</select></label>
            <label><span>题型</span><select aria-label="题型" value={config.type} onChange={(event) => setConfig({ ...config, type: event.target.value })}>{types.map((type) => <option key={type}>{type}</option>)}</select></label>
            <label><span>难度</span><select aria-label="难度" value={config.difficulty} onChange={(event) => setConfig({ ...config, difficulty: event.target.value })}>{difficulties.map((difficulty) => <option key={difficulty}>{difficulty}</option>)}</select></label>
          </div>

          <div className="console-options">
            <div><span className="field-caption">题目数量</span><div className="ios-segment">{[10, 20, 50, 0].map((count) => <button key={count} className={config.count === count ? 'active' : ''} onClick={() => setConfig({ ...config, count })}>{count || '全部'}</button>)}</div></div>
            <div><span className="field-caption">出题顺序</span><div className="ios-segment"><button className={config.order === 'random' ? 'active' : ''} onClick={() => setConfig({ ...config, order: 'random' })}><RotateCw size={14} />随机</button><button className={config.order === 'sequential' ? 'active' : ''} onClick={() => setConfig({ ...config, order: 'sequential' })}><ArrowRight size={14} />顺序</button></div></div>
          </div>

          <div className={`practice-actions ${activeSession ? 'has-resume' : ''}`}>
            {activeSession && <button className="resume-practice" onClick={onContinue}><span>继续上次</span><small>第 {Math.min(activeSession.index + 1, activeSession.questionIds.length)} / {activeSession.questionIds.length} 题</small><ChevronRight size={20} /></button>}
            <button className={`start-practice ${activeSession ? 'new-practice' : ''}`} onClick={start} disabled={!available.length}><span>{activeSession ? '开始新练习' : '开始答题'}</span><small>{config.count ? Math.min(config.count, available.length) : available.length} 道</small><ChevronRight size={20} /></button>
          </div>
        </section>

        <aside className="record-console" aria-labelledby="record-title">
          <div className="record-heading"><div><h2 id="record-title">学习记录</h2><p>数据保存在本机</p></div><CheckCircle2 size={21} /></div>
          <div className="record-metrics">
            <div><span>正确率</span><b>{accuracy}<small>%</small></b></div>
            <div><span>累计答对</span><b>{correct}</b></div>
            <div><span>累计作答</span><b>{attempts}</b></div>
            <div><span>收藏题目</span><b>{starred}</b></div>
          </div>
          <div className="collection-actions">
            <button className="collection-action wrong" disabled={!wrongQuestions.length} onClick={() => onStart(randomize(wrongQuestions))}><span><TriangleAlert size={18} /><span><b>错题集</b><small>{wrongQuestions.length} 道</small></span></span><ChevronRight size={18} /></button>
            <button className="collection-action favorite" disabled={!starredQuestions.length} onClick={() => onStart(randomize(starredQuestions))}><span><Bookmark size={18} /><span><b>收藏题</b><small>{starredQuestions.length} 道</small></span></span><ChevronRight size={18} /></button>
            <button className="collection-action exam" disabled={!available.length} onClick={startExam}><span><Timer size={18} /><span><b>模拟考试</b><small>{config.count ? Math.min(config.count, available.length) : available.length} 道</small></span></span><ChevronRight size={18} /></button>
          </div>
        </aside>
      </div>
    </main>
  )
}
