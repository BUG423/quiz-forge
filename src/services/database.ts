import type { PracticeSession, Progress, Question, QuestionBank, QuestionDataset } from '../types'
import { demoDataset } from '../demoDataset'

const DATABASE_NAME = 'anzhi-question-bank'
const DATABASE_VERSION = 2
let practiceSessionWriteQueue = Promise.resolve()

type StoreName = 'banks' | 'questions' | 'progress' | 'meta' | 'practiceSessions'

function requestResult<T>(request: IDBRequest<T>) {
  return new Promise<T>((resolve, reject) => {
    request.onsuccess = () => resolve(request.result)
    request.onerror = () => reject(request.error)
  })
}

function transactionDone(transaction: IDBTransaction) {
  return new Promise<void>((resolve, reject) => {
    transaction.oncomplete = () => resolve()
    transaction.onerror = () => reject(transaction.error)
    transaction.onabort = () => reject(transaction.error)
  })
}

async function openDatabase() {
  const request = indexedDB.open(DATABASE_NAME, DATABASE_VERSION)
  request.onupgradeneeded = () => {
    const database = request.result
    if (!database.objectStoreNames.contains('banks')) database.createObjectStore('banks', { keyPath: 'id' })
    if (!database.objectStoreNames.contains('questions')) {
      const questions = database.createObjectStore('questions', { keyPath: 'id' })
      questions.createIndex('bankId', 'bankId', { unique: false })
    }
    if (!database.objectStoreNames.contains('progress')) database.createObjectStore('progress', { keyPath: 'questionId' })
    if (!database.objectStoreNames.contains('meta')) database.createObjectStore('meta', { keyPath: 'key' })
    if (!database.objectStoreNames.contains('practiceSessions')) database.createObjectStore('practiceSessions', { keyPath: 'id' })
  }
  return requestResult(request)
}

async function getMeta(database: IDBDatabase, key: string) {
  const tx = database.transaction('meta', 'readonly')
  return requestResult<{ key: string; value: string } | undefined>(tx.objectStore('meta').get(key))
}

export async function initializeDatabase() {
  const database = await openDatabase()
  const manifestResponse = await fetch('/data/manifest.json')
  if (!manifestResponse.ok && manifestResponse.status !== 404) throw new Error('题库版本信息加载失败')

  let manifest: Pick<QuestionDataset, 'sourceVersion'> | undefined
  if (manifestResponse.ok) {
    try {
      manifest = (await manifestResponse.json()) as Pick<QuestionDataset, 'sourceVersion'>
    } catch {
      // Some SPA hosts return index.html for an absent static JSON file.
    }
  }

  const sourceVersion = manifest?.sourceVersion || demoDataset.sourceVersion
  const currentVersion = await getMeta(database, 'sourceVersion')

  if (currentVersion?.value !== sourceVersion) {
    let dataset = demoDataset
    if (manifest?.sourceVersion) {
      const response = await fetch('/data/questions.json')
      if (!response.ok) throw new Error('内置题库加载失败')
      dataset = (await response.json()) as QuestionDataset
    }
    const snapshotTx = database.transaction(['banks', 'questions'], 'readonly')
    const [existingBanks, existingQuestions] = await Promise.all([
      requestResult<QuestionBank[]>(snapshotTx.objectStore('banks').getAll()),
      requestResult<Question[]>(snapshotTx.objectStore('questions').getAll()),
    ])
    const bundledBankIds = new Set(existingBanks.filter((bank) => bank.origin !== 'upload').map((bank) => bank.id))
    const uploadedBankIds = new Set(existingBanks.filter((bank) => bank.origin === 'upload').map((bank) => bank.id))
    const nextQuestions = new Map(dataset.questions.filter((question) => !uploadedBankIds.has(question.bankId)).map((question) => [question.id, question.bankId]))
    const tx = database.transaction(['banks', 'questions', 'progress', 'meta'], 'readwrite')
    const bankStore = tx.objectStore('banks')
    const questionStore = tx.objectStore('questions')
    const progressStore = tx.objectStore('progress')
    for (const bankId of bundledBankIds) bankStore.delete(bankId)
    for (const question of existingQuestions) {
      if (bundledBankIds.has(question.bankId)) {
        questionStore.delete(question.id)
        if (nextQuestions.get(question.id) !== question.bankId) progressStore.delete(question.id)
      }
    }
    for (const bank of dataset.banks) {
      if (!uploadedBankIds.has(bank.id)) bankStore.put({ ...bank, origin: 'bundled' })
    }
    for (const question of dataset.questions) {
      if (!uploadedBankIds.has(question.bankId)) questionStore.put(question)
    }
    tx.objectStore('meta').put({ key: 'sourceVersion', value: sourceVersion })
    await transactionDone(tx)
  }
  database.close()
}

async function getAll<T>(storeName: StoreName) {
  const database = await openDatabase()
  const tx = database.transaction(storeName, 'readonly')
  const result = await requestResult<T[]>(tx.objectStore(storeName).getAll())
  database.close()
  return result
}

export async function loadLibrary() {
  const [banks, questions, progress] = await Promise.all([
    getAll<QuestionBank>('banks'),
    getAll<Question>('questions'),
    getAll<Progress>('progress'),
  ])
  return { banks, questions, progress }
}

export async function saveProgress(progress: Progress) {
  const database = await openDatabase()
  const tx = database.transaction('progress', 'readwrite')
  tx.objectStore('progress').put(progress)
  await transactionDone(tx)
  database.close()
}

export async function clearWrongProgress(questionIds: string[]) {
  const uniqueIds = [...new Set(questionIds)]
  if (uniqueIds.length === 0) return

  const database = await openDatabase()
  const tx = database.transaction('progress', 'readwrite')
  const store = tx.objectStore('progress')
  const records = await Promise.all(uniqueIds.map((id) => requestResult<Progress | undefined>(store.get(id))))
  for (const progress of records) {
    if (progress?.wrong) store.put({ ...progress, wrong: 0 })
  }
  await transactionDone(tx)
  database.close()
}

export async function loadActivePracticeSession() {
  await practiceSessionWriteQueue
  const database = await openDatabase()
  const tx = database.transaction('practiceSessions', 'readonly')
  const result = await requestResult<PracticeSession | undefined>(tx.objectStore('practiceSessions').get('active'))
  database.close()
  return result
}

export function saveActivePracticeSession(session: PracticeSession) {
  const write = async () => {
    const database = await openDatabase()
    const tx = database.transaction('practiceSessions', 'readwrite')
    tx.objectStore('practiceSessions').put(session)
    await transactionDone(tx)
    database.close()
  }
  practiceSessionWriteQueue = practiceSessionWriteQueue.then(write, write)
  return practiceSessionWriteQueue
}

export function clearActivePracticeSession() {
  const clear = async () => {
    const database = await openDatabase()
    const tx = database.transaction('practiceSessions', 'readwrite')
    tx.objectStore('practiceSessions').delete('active')
    await transactionDone(tx)
    database.close()
  }
  practiceSessionWriteQueue = practiceSessionWriteQueue.then(clear, clear)
  return practiceSessionWriteQueue
}

export async function replaceBank(bank: QuestionBank, questions: Question[]) {
  const database = await openDatabase()
  const tx = database.transaction(['banks', 'questions', 'progress'], 'readwrite')
  const questionStore = tx.objectStore('questions')
  const progressStore = tx.objectStore('progress')
  const index = questionStore.index('bankId')
  const oldKeys = await requestResult<IDBValidKey[]>(index.getAllKeys(bank.id))
  const replacementIds = new Set(questions.map((question) => question.id))
  for (const key of oldKeys) {
    questionStore.delete(key)
    if (typeof key !== 'string' || !replacementIds.has(key)) progressStore.delete(key)
  }
  tx.objectStore('banks').put({ ...bank, origin: 'upload' })
  for (const question of questions) questionStore.put(question)
  await transactionDone(tx)
  database.close()
}

export async function deleteBank(bankId: string) {
  const database = await openDatabase()
  const tx = database.transaction(['banks', 'questions', 'progress'], 'readwrite')
  const questionStore = tx.objectStore('questions')
  const progressStore = tx.objectStore('progress')
  const keys = await requestResult<IDBValidKey[]>(questionStore.index('bankId').getAllKeys(bankId))
  for (const key of keys) {
    questionStore.delete(key)
    progressStore.delete(key)
  }
  tx.objectStore('banks').delete(bankId)
  await transactionDone(tx)
  database.close()
}
