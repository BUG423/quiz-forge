import { useEffect, useMemo, useState } from 'react'
import { AlertTriangle, LoaderCircle } from 'lucide-react'
import QuizSession from './components/QuizSession'
import SettingsSheet from './components/SettingsSheet'
import StudyHome from './components/StudyHome'
import { clearActivePracticeSession, deleteBank, initializeDatabase, loadActivePracticeSession, loadLibrary, replaceBank, saveActivePracticeSession, saveProgress } from './services/database'
import type { ImportResult, PracticeSession, Progress, Question, QuestionBank, QuizMode } from './types'

interface InstallPromptEvent extends Event {
  prompt: () => Promise<void>
  userChoice: Promise<{ outcome: 'accepted' | 'dismissed' }>
}

export default function App() {
  const [banks, setBanks] = useState<QuestionBank[]>([])
  const [questions, setQuestions] = useState<Question[]>([])
  const [progress, setProgress] = useState<Map<string, Progress>>(new Map())
  const [session, setSession] = useState<{ questions: Question[]; snapshot: PracticeSession } | null>(null)
  const [savedSession, setSavedSession] = useState<PracticeSession | null>(null)
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [installPrompt, setInstallPrompt] = useState<InstallPromptEvent | null>(null)
  const [runningAsApp] = useState(() => {
    const capacitorWindow = window as typeof window & { Capacitor?: { isNativePlatform?: () => boolean } }
    return window.matchMedia('(display-mode: standalone)').matches || Boolean(capacitorWindow.Capacitor?.isNativePlatform?.())
  })

  const reload = async () => {
    const library = await loadLibrary()
    setBanks(library.banks.sort((left, right) => left.name.localeCompare(right.name, 'zh-CN')))
    setQuestions(library.questions)
    setProgress(new Map(library.progress.map((item) => [item.questionId, item])))
  }

  useEffect(() => {
    const boot = async () => {
      await initializeDatabase()
      await reload()
      const storedSession = await loadActivePracticeSession()
      setSavedSession(storedSession ? { ...storedSession, mode: storedSession.mode ?? 'practice' } : null)
    }
    boot().catch((reason) => setError(reason instanceof Error ? reason.message : '应用初始化失败')).finally(() => setLoading(false))
  }, [])

  useEffect(() => {
    const capturePrompt = (event: Event) => {
      event.preventDefault()
      setInstallPrompt(event as InstallPromptEvent)
    }
    window.addEventListener('beforeinstallprompt', capturePrompt)
    return () => window.removeEventListener('beforeinstallprompt', capturePrompt)
  }, [])

  useEffect(() => {
    document.body.classList.toggle('quiz-active', Boolean(session))
    return () => document.body.classList.remove('quiz-active')
  }, [session])

  const recordAnswer = (questionId: string, correct: boolean) => {
    setProgress((current) => {
      const next = new Map(current)
      const old = next.get(questionId) ?? { questionId, attempts: 0, correct: 0, wrong: 0, starred: false }
      const updated = { ...old, attempts: old.attempts + 1, correct: old.correct + (correct ? 1 : 0), wrong: old.wrong + (correct ? 0 : 1), lastAnsweredAt: new Date().toISOString() }
      next.set(questionId, updated)
      void saveProgress(updated)
      return next
    })
  }

  const toggleStar = (questionId: string) => {
    setProgress((current) => {
      const next = new Map(current)
      const old = next.get(questionId) ?? { questionId, attempts: 0, correct: 0, wrong: 0, starred: false }
      const updated = { ...old, starred: !old.starred }
      next.set(questionId, updated)
      void saveProgress(updated)
      return next
    })
  }

  const importBank = async (result: ImportResult, mode: 'add' | 'replace') => {
    const currentLibrary = await loadLibrary()
    const sameNameBanks = currentLibrary.banks.filter((bank) => bank.name === result.bank.name)
    const existing = sameNameBanks.find((bank) => bank.id === result.bank.id) ?? sameNameBanks.sort((left, right) => right.importedAt.localeCompare(left.importedAt))[0]
    if (existing && mode === 'add') {
      const suffix = `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 6)}`
      const bank = { ...result.bank, id: `${result.bank.id}-${suffix}` }
      const importedQuestions = result.questions.map((question) => ({ ...question, id: `${question.id}-${suffix}`, bankId: bank.id }))
      await replaceBank(bank, importedQuestions)
      await reload()
      return
    }
    const bank = existing && mode === 'replace' ? { ...result.bank, id: existing.id } : result.bank
    const replacementSuffix = existing?.id.startsWith(`${result.bank.id}-`) ? existing.id.slice(result.bank.id.length + 1) : ''
    const importedQuestions = existing && mode === 'replace' ? result.questions.map((question) => ({ ...question, id: replacementSuffix ? `${question.id}-${replacementSuffix}` : question.id, bankId: existing.id })) : result.questions
    await replaceBank(bank, importedQuestions)
    await reload()
  }

  const installApp = async () => {
    if (installPrompt) {
      await installPrompt.prompt()
      const choice = await installPrompt.userChoice
      if (choice.outcome === 'accepted') setInstallPrompt(null)
      return
    }
    window.alert('安卓：请使用浏览器菜单中的“安装应用”或“添加到主屏幕”。\niPhone：请用 Safari 打开，点击分享按钮，再选择“添加到主屏幕”。')
  }

  const installAction = useMemo(() => runningAsApp ? undefined : installApp, [runningAsApp, installPrompt])

  const startPractice = (selectedQuestions: Question[], mode: QuizMode = 'practice') => {
    if (!selectedQuestions.length) return
    const now = new Date().toISOString()
    const snapshot: PracticeSession = { id: 'active', mode, questionIds: selectedQuestions.map((question) => question.id), index: 0, responses: {}, judgements: {}, startedAt: now, updatedAt: now }
    setSavedSession(snapshot)
    setSession({ questions: selectedQuestions, snapshot })
    void saveActivePracticeSession(snapshot)
  }

  const continuePractice = () => {
    if (!savedSession) return
    const questionById = new Map(questions.map((question) => [question.id, question]))
    const remainingQuestions = savedSession.questionIds.map((id) => questionById.get(id)).filter((question): question is Question => Boolean(question))
    if (!remainingQuestions.length) {
      setSavedSession(null)
      void clearActivePracticeSession()
      return
    }
    const validIds = new Set(remainingQuestions.map((question) => question.id))
    const snapshot = {
      ...savedSession,
      questionIds: remainingQuestions.map((question) => question.id),
      index: Math.min(savedSession.index, remainingQuestions.length - 1),
      responses: Object.fromEntries(Object.entries(savedSession.responses).filter(([id]) => validIds.has(id))),
      judgements: Object.fromEntries(Object.entries(savedSession.judgements).filter(([id]) => validIds.has(id))),
    }
    setSavedSession(snapshot)
    setSession({ questions: remainingQuestions, snapshot })
    void saveActivePracticeSession(snapshot)
  }

  const persistSession = (snapshot: PracticeSession) => {
    setSavedSession(snapshot)
    void saveActivePracticeSession(snapshot)
  }

  const completeSession = () => {
    setSavedSession(null)
    void clearActivePracticeSession()
  }

  if (loading) return <div className="app-state"><LoaderCircle className="spin" size={34} /><b>正在打开安知</b><p>题库马上就好</p></div>
  if (error) return <div className="app-state error"><AlertTriangle size={36} /><b>题库打开失败</b><p>{error}</p><button className="dark-button" onClick={() => window.location.reload()}>重新加载</button></div>

  return <div className="single-page-app">
    {session ? <QuizSession questions={session.questions} progress={progress} mode={session.snapshot.mode} initialSession={session.snapshot} onSessionChange={persistSession} onComplete={completeSession} onRecord={recordAnswer} onToggleStar={toggleStar} onExit={() => setSession(null)} onSettings={() => setSettingsOpen(true)} /> : <StudyHome banks={banks} questions={questions} progress={progress} activeSession={savedSession} onContinue={continuePractice} onStart={startPractice} onSettings={() => setSettingsOpen(true)} />}
    <SettingsSheet open={settingsOpen} banks={banks} onClose={() => setSettingsOpen(false)} onImport={importBank} onDelete={async (bank) => { await deleteBank(bank.id); await reload() }} onInstall={installAction} />
  </div>
}
