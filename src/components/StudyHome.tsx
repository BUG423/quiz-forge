import { useEffect, useMemo, useState } from 'react'
import { ArrowRight, Bookmark, ChevronRight, RotateCw, Settings, Target, Timer, Trash2, TriangleAlert, X } from 'lucide-react'
import type { PracticeConfig, PracticeSession, Progress, Question, QuestionBank, QuizMode } from '../types'

interface StudyHomeProps {
  banks: QuestionBank[]
  questions: Question[]
  progress: Map<string, Progress>
  activeSession: PracticeSession | null
  onContinue: () => void
  onStart: (questions: Question[], mode?: QuizMode) => void
  onClearWrong: (questionIds: string[]) => Promise<void>
  onSettings: () => void
}

const STORAGE_KEY = 'anzhi-practice-config-v1'
const defaultConfig: PracticeConfig = { bankId: 'all', count: 20, order: 'random', type: '全部题型', difficulty: '全部难度', onlyLifesaving: false }

function savedConfig(): PracticeConfig {
  try {
    const value = JSON.parse(localStorage.getItem(STORAGE_KEY) ?? 'null') as Partial<PracticeConfig> | null
    if (!value || typeof value !== 'object') return defaultConfig
    return {
      bankId: typeof value.bankId === 'string' ? value.bankId : defaultConfig.bankId,
      type: typeof value.type === 'string' ? value.type : defaultConfig.type,
      difficulty: typeof value.difficulty === 'string' ? value.difficulty : defaultConfig.difficulty,
      count: [0, 10, 20, 50].includes(value.count ?? -1) ? value.count! : defaultConfig.count,
      order: value.order === 'sequential' ? 'sequential' : 'random',
      onlyLifesaving: value.onlyLifesaving === true,
    }
  } catch {
    return defaultConfig
  }
}

function randomize<T>(items: T[]) {
  const result = [...items]
  for (let index = result.length - 1; index > 0; index -= 1) {
    const target = Math.floor(Math.random() * (index + 1))
    ;[result[index], result[target]] = [result[target], result[index]]
  }
  return result
}

export default function StudyHome({ banks, questions, progress, activeSession, onContinue, onStart, onClearWrong, onSettings }: StudyHomeProps) {
  const [config, setConfig] = useState<PracticeConfig>(savedConfig)
  const [wrongPanelBank, setWrongPanelBank] = useState<string | null>(null)
  const [clearing, setClearing] = useState(false)
  const [wrongError, setWrongError] = useState('')

  const selectedBank = banks.find((bank) => bank.id === config.bankId)
  const bankQuestions = useMemo(() => questions.filter((question) => config.bankId === 'all' || question.bankId === config.bankId), [config.bankId, questions])
  const types = useMemo(() => ['全部题型', ...new Set(bankQuestions.map((question) => question.type))], [bankQuestions])
  const difficulties = useMemo(() => ['全部难度', ...new Set(bankQuestions.map((question) => question.difficulty))], [bankQuestions])

  useEffect(() => {
    setConfig((current) => {
      const bankId = current.bankId === 'all' || banks.some((bank) => bank.id === current.bankId) ? current.bankId : 'all'
      const bankItems = questions.filter((question) => bankId === 'all' || question.bankId === bankId)
      const type = current.type === '全部题型' || bankItems.some((question) => question.type === current.type) ? current.type : '全部题型'
      const difficulty = current.difficulty === '全部难度' || bankItems.some((question) => question.difficulty === current.difficulty) ? current.difficulty : '全部难度'
      return bankId === current.bankId && type === current.type && difficulty === current.difficulty ? current : { ...current, bankId, type, difficulty }
    })
  }, [banks, questions])

  useEffect(() => {
    try { localStorage.setItem(STORAGE_KEY, JSON.stringify(config)) } catch { /* Storage can be disabled. */ }
  }, [config])

  const available = useMemo(() => bankQuestions.filter((question) =>
    (config.type === '全部题型' || question.type === config.type) &&
    (config.difficulty === '全部难度' || question.difficulty === config.difficulty) &&
    (!config.onlyLifesaving || question.lifesaving),
  ), [bankQuestions, config])

  const wrongByBank = useMemo(() => {
    const grouped = new Map<string, Question[]>()
    for (const question of questions) {
      if ((progress.get(question.id)?.wrong ?? 0) <= 0) continue
      const items = grouped.get(question.bankId) ?? []
      items.push(question)
      grouped.set(question.bankId, items)
    }
    for (const items of grouped.values()) items.sort((left, right) => (progress.get(right.id)?.lastAnsweredAt ?? '').localeCompare(progress.get(left.id)?.lastAnsweredAt ?? ''))
    return grouped
  }, [progress, questions])
  const currentWrong = config.bankId === 'all' ? [] : wrongByBank.get(config.bankId) ?? []
  const wrongBankCount = [...wrongByBank.keys()].length
  const panelQuestions = wrongPanelBank && wrongPanelBank !== 'choose' ? wrongByBank.get(wrongPanelBank) ?? [] : []
  const panelBank = banks.find((bank) => bank.id === wrongPanelBank)
  const starredQuestions = bankQuestions.filter((question) => progress.get(question.id)?.starred)

  const changeBank = (bankId: string) => {
    const bankItems = questions.filter((question) => bankId === 'all' || question.bankId === bankId)
    setConfig((current) => ({
      ...current,
      bankId,
      type: current.type === '全部题型' || bankItems.some((question) => question.type === current.type) ? current.type : '全部题型',
      difficulty: current.difficulty === '全部难度' || bankItems.some((question) => question.difficulty === current.difficulty) ? current.difficulty : '全部难度',
    }))
  }

  const start = () => {
    const ordered = config.order === 'random' ? randomize(available) : [...available]
    onStart(config.count ? ordered.slice(0, config.count) : ordered)
  }

  const startExam = () => {
    const selected = randomize(available)
    onStart(config.count ? selected.slice(0, config.count) : selected, 'exam')
  }

  const clearWrong = async (ids: string[]) => {
    if (!ids.length || clearing) return
    setClearing(true)
    try {
      await onClearWrong(ids)
      setWrongError('')
    } catch {
      setWrongError('移除失败，请重试')
    } finally {
      setClearing(false)
    }
  }

  return (
    <main className="study-home">
      <div className="home-console">
        <section className="practice-console" aria-labelledby="practice-title">
          <div className="console-heading">
            <div><span className="console-icon"><Target size={20} /></span><div><h1 id="practice-title">开始练习</h1><p>当前可练 {available.length.toLocaleString()} 道题</p></div></div>
            <button className="circle-control home-settings" onClick={onSettings} aria-label="打开设置" title="设置"><Settings size={20} /></button>
          </div>

          <div className="console-fields">
            <label className="bank-field"><span>题库</span><select aria-label="题库" value={config.bankId} onChange={(event) => changeBank(event.target.value)}><option value="all">全部题库（{questions.length} 题）</option>{banks.map((bank) => <option key={bank.id} value={bank.id}>{bank.name}（{bank.questionCount} 题）</option>)}</select></label>
            <label><span>题型</span><select aria-label="题型" value={config.type} onChange={(event) => setConfig({ ...config, type: event.target.value })}>{types.map((type) => <option key={type}>{type}</option>)}</select></label>
            <label><span>难度</span><select aria-label="难度" value={config.difficulty} onChange={(event) => setConfig({ ...config, difficulty: event.target.value })}>{difficulties.map((difficulty) => <option key={difficulty}>{difficulty}</option>)}</select></label>
          </div>

          <div className="console-options">
            <div><span className="field-caption">题目数量</span><div className="ios-segment">{[10, 20, 50, 0].map((count) => <button key={count} type="button" className={config.count === count ? 'active' : ''} onClick={() => setConfig({ ...config, count })}>{count || '全部'}</button>)}</div></div>
            <div><span className="field-caption">出题顺序</span><div className="ios-segment"><button type="button" className={config.order === 'random' ? 'active' : ''} onClick={() => setConfig({ ...config, order: 'random' })}><RotateCw size={15} />随机</button><button type="button" className={config.order === 'sequential' ? 'active' : ''} onClick={() => setConfig({ ...config, order: 'sequential' })}><ArrowRight size={15} />顺序</button></div></div>
          </div>
          <label className="life-toggle"><input type="checkbox" checked={config.onlyLifesaving} onChange={(event) => setConfig({ ...config, onlyLifesaving: event.target.checked })} /><span />只看保命题</label>

          <div className={`practice-actions ${activeSession ? 'has-resume' : ''}`}>
            {activeSession && <button className="resume-practice" onClick={onContinue}><span>继续上次</span><small>第 {Math.min(activeSession.index + 1, activeSession.questionIds.length)} / {activeSession.questionIds.length} 题</small><ChevronRight size={20} /></button>}
            <button className={`start-practice ${activeSession ? 'new-practice' : ''}`} onClick={start} disabled={!available.length}><span>{activeSession ? '开始新练习' : '开始答题'}</span><small>{config.count ? Math.min(config.count, available.length) : available.length} 道</small><ChevronRight size={20} /></button>
          </div>
        </section>

        <aside className="record-console" aria-label="快捷入口">
          <div className="record-heading"><div><h2>快捷入口</h2><p>当前题库：{selectedBank?.name ?? '全部题库'}</p></div></div>
          <div className="collection-actions">
            <button className="collection-action wrong" disabled={config.bankId === 'all' ? !wrongBankCount : !currentWrong.length} onClick={() => setWrongPanelBank(config.bankId === 'all' ? 'choose' : config.bankId)}><span><TriangleAlert size={20} /><span><b>错题集</b><small>{config.bankId === 'all' ? `${wrongBankCount} 个题库有错题` : `${currentWrong.length} 道错题`}</small></span></span><ChevronRight size={19} /></button>
            <button className="collection-action favorite" disabled={!starredQuestions.length} onClick={() => onStart(randomize(starredQuestions))}><span><Bookmark size={20} /><span><b>收藏题</b><small>当前题库 {starredQuestions.length} 道</small></span></span><ChevronRight size={19} /></button>
            <button className="collection-action exam" disabled={!available.length} onClick={startExam}><span><Timer size={20} /><span><b>模拟考试</b><small>按上方题库、题型和数量组卷</small></span></span><ChevronRight size={19} /></button>
          </div>
        </aside>
      </div>

      {wrongPanelBank && <div className="wrong-overlay" onClick={() => setWrongPanelBank(null)}><section className="wrong-panel" role="dialog" aria-modal="true" aria-label="错题集" onClick={(event) => event.stopPropagation()}>
        <header><div><small>错题集</small><h2>{wrongPanelBank === 'choose' ? '选择题库' : panelBank?.name ?? '错题集'}</h2></div><button className="circle-control" onClick={() => setWrongPanelBank(null)} aria-label="关闭错题集"><X size={19} /></button></header>
        {wrongPanelBank === 'choose' ? <div className="wrong-list">{banks.filter((bank) => wrongByBank.has(bank.id)).map((bank) => <button className="wrong-bank-row" key={bank.id} onClick={() => setWrongPanelBank(bank.id)}><span>{bank.name}<small>{wrongByBank.get(bank.id)?.length} 道错题</small></span><ChevronRight size={18} /></button>)}</div> : <>
          <div className="wrong-toolbar"><span>{panelQuestions.length} 道错题</span><button disabled={!panelQuestions.length || clearing} onClick={() => { if (window.confirm(`清空“${panelBank?.name ?? '当前题库'}”的 ${panelQuestions.length} 道错题？`)) void clearWrong(panelQuestions.map((question) => question.id)) }}><Trash2 size={15} />清空本题库</button></div>
          {wrongError && <p className="wrong-error" role="alert">{wrongError}</p>}
          <div className="wrong-list">{panelQuestions.length ? panelQuestions.map((question) => <article className="wrong-question" key={question.id}><div><small>{question.type} · {question.difficulty}</small><p>{question.stem}</p></div><button disabled={clearing} onClick={() => void clearWrong([question.id])} aria-label={`移除错题：${question.stem}`}><Trash2 size={17} /><span>移除</span></button></article>) : <p className="wrong-empty">这个题库的错题已清空</p>}</div>
          <footer>{config.bankId === 'all' && <button className="glass-button" onClick={() => setWrongPanelBank('choose')}>返回题库列表</button>}<button className="dark-button" disabled={!panelQuestions.length} onClick={() => { onStart(randomize(panelQuestions)); setWrongPanelBank(null) }}>练习错题</button></footer>
        </>}
      </section></div>}
    </main>
  )
}
