import { useRef, useState } from 'react'
import { CheckCircle2, Download, FileSpreadsheet, LoaderCircle, Smartphone, Trash2, Upload, X } from 'lucide-react'
import type { ImportResult, QuestionBank } from '../types'
import { downloadTemplate, parseExcelFile } from '../services/excelImporter'

interface SettingsSheetProps {
  open: boolean
  banks: QuestionBank[]
  onClose: () => void
  onImport: (result: ImportResult, mode: 'add' | 'replace') => Promise<void>
  onDelete: (bank: QuestionBank) => Promise<void>
  onInstall?: () => void
}

export default function SettingsSheet({ open, banks, onClose, onImport, onDelete, onInstall }: SettingsSheetProps) {
  const inputRef = useRef<HTMLInputElement>(null)
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState('')
  const [conflict, setConflict] = useState<ImportResult | null>(null)
  const conflictResolver = useRef<((choice: 'add' | 'replace' | 'cancel') => void) | null>(null)

  const askConflictChoice = (result: ImportResult) => new Promise<'add' | 'replace' | 'cancel'>((resolve) => {
    conflictResolver.current = resolve
    setConflict(result)
  })

  const resolveConflict = (choice: 'add' | 'replace' | 'cancel') => {
    conflictResolver.current?.(choice)
    conflictResolver.current = null
    setConflict(null)
  }

  const processFiles = async (files: File[]) => {
    const excelFiles = files.filter((file) => /\.xlsx$/i.test(file.name))
    if (!excelFiles.length) { setMessage('请选择 .xlsx 格式文件'); return }
    setBusy(true); setMessage('')
    let imported = 0
    const knownNames = new Set(banks.map((bank) => bank.name))
    try {
      for (const file of excelFiles) {
        const result = await parseExcelFile(file)
        let mode: 'add' | 'replace' = 'add'
        if (knownNames.has(result.bank.name)) {
          const choice = await askConflictChoice(result)
          if (choice === 'cancel') continue
          mode = choice
        }
        await onImport(result, mode)
        knownNames.add(result.bank.name)
        imported += result.questions.length
      }
      setMessage(imported ? `导入完成，共 ${imported} 道题` : '已取消导入')
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '导入失败，请检查文件格式')
    } finally {
      setBusy(false)
      if (inputRef.current) inputRef.current.value = ''
    }
  }

  if (!open) return null
  const close = () => conflict ? resolveConflict('cancel') : onClose()
  return <div className="settings-overlay" onClick={close}><aside className="settings-sheet" onClick={(event) => event.stopPropagation()}>
    <header><div><small>SETTINGS</small><h2>设置</h2></div><button className="circle-control" onClick={close}><X size={19} /></button></header>
    <div className="settings-scroll">
      {onInstall && <button className="setting-row" onClick={onInstall}><span><Smartphone size={19} /></span><div><b>安装到手机</b><small>添加到主屏幕，像普通 App 一样使用</small></div><Download size={17} /></button>}
      <section className="settings-group"><div className="settings-group-title"><div><b>Excel 题库</b><small>同名时询问新增或覆盖</small></div><button onClick={() => void downloadTemplate()}><Download size={15} />下载模板</button></div>
        <button className="compact-upload" onClick={() => inputRef.current?.click()} disabled={busy}>{busy ? <LoaderCircle className="spin" size={23} /> : <FileSpreadsheet size={23} />}<span><b>{busy ? '正在读取题库…' : '导入 Excel 文件'}</b><small>支持同时选择多个 .xlsx 文件</small></span><Upload size={17} /></button>
        <input ref={inputRef} type="file" accept=".xlsx" multiple hidden onChange={(event) => void processFiles([...event.target.files ?? []])} />
        {message && <p className="settings-message"><CheckCircle2 size={15} />{message}</p>}
      </section>
      <section className="settings-group"><div className="settings-group-title"><div><b>当前题库</b><small>{banks.length} 个题库</small></div></div><div className="compact-banks">{banks.map((bank) => <div key={bank.id}><span>{bank.name.slice(0, 1)}</span><p><b>{bank.name}</b><small>{bank.questionCount} 道题 · {bank.origin === 'upload' ? '本地上传' : '内置'}</small></p>{bank.origin === 'upload' && <button onClick={() => { if (window.confirm(`确定删除“${bank.name}”吗？`)) void onDelete(bank) }}><Trash2 size={16} /></button>}</div>)}</div></section>
    </div>
    {conflict && <div className="conflict-layer"><div className="conflict-card"><span className="conflict-icon"><FileSpreadsheet size={23} /></span><h3>发现同名题库</h3><p>“{conflict.bank.name}”已经存在。你想新增一份，还是用这份文件覆盖原题库？</p><button className="replace-bank" onClick={() => resolveConflict('replace')}>覆盖原题库<small>原题库题目将被替换</small></button><button className="add-bank-copy" onClick={() => resolveConflict('add')}>新增一份<small>保留原题库，两份独立存在</small></button><button className="cancel-conflict" onClick={() => resolveConflict('cancel')}>取消本次导入</button></div></div>}
  </aside></div>
}
