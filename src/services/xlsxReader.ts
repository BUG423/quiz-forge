import { Unzip, UnzipInflate } from 'fflate'

export interface SpreadsheetRow {
  number: number
  cells: Map<number, string>
}

export interface SpreadsheetSheet {
  name: string
  rows: SpreadsheetRow[]
}

type RawCellValue = { kind: 'shared'; value: number } | { kind: 'text'; value: string }
type RawSpreadsheetRow = { number: number; cells: Map<number, RawCellValue> }

const WORKBOOK_PATH = 'xl/workbook.xml'
const WORKBOOK_RELS_PATH = 'xl/_rels/workbook.xml.rels'
const SHARED_STRINGS_PATH = 'xl/sharedStrings.xml'
const MAX_XML_ENTRY_BYTES = 256 * 1024 * 1024
const MAX_SELECTED_XML_BYTES = 512 * 1024 * 1024
const MAX_RELEVANT_COLUMN = 256
const MAX_NON_EMPTY_ROWS = 250_000

function decodeXml(value: string) {
  return value.replace(/&(?:lt|gt|amp|quot|apos|#\d+|#x[\da-f]+);/gi, (entity) => {
    const normalized = entity.toLowerCase()
    if (normalized === '&lt;') return '<'
    if (normalized === '&gt;') return '>'
    if (normalized === '&amp;') return '&'
    if (normalized === '&quot;') return '"'
    if (normalized === '&apos;') return "'"
    const hexadecimal = normalized.startsWith('&#x')
    const codePoint = Number.parseInt(entity.slice(hexadecimal ? 3 : 2, -1), hexadecimal ? 16 : 10)
    return Number.isFinite(codePoint) ? String.fromCodePoint(codePoint) : entity
  })
}

function xmlAttribute(tag: string, name: string) {
  const escaped = name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  const match = tag.match(new RegExp(`(?:^|\\s)${escaped}=(['"])(.*?)\\1`, 'i'))
  return match ? decodeXml(match[2]) : ''
}

function textNodes(xml: string) {
  let value = ''
  for (const match of xml.matchAll(/<t\b[^>]*>([\s\S]*?)<\/t>/gi)) value += decodeXml(match[1])
  return value
}

function normalizeZipPath(value: string) {
  const parts: string[] = []
  for (const part of value.replaceAll('\\', '/').split('/')) {
    if (!part || part === '.') continue
    if (part === '..') parts.pop()
    else parts.push(part)
  }
  return parts.join('/')
}

function resolveWorkbookTarget(target: string) {
  return normalizeZipPath(target.startsWith('/') ? target : `xl/${target}`)
}

function columnNumber(cellReference: string) {
  const letters = cellReference.toUpperCase().match(/^[A-Z]+/)?.[0]
  if (!letters) return 0
  let number = 0
  for (const letter of letters) number = number * 26 + letter.charCodeAt(0) - 64
  return number
}

function parseRawCell(tag: string, body: string): RawCellValue | undefined {
  const type = xmlAttribute(tag, 't').toLowerCase()
  if (type === 'inlinestr') {
    const value = textNodes(body)
    return value ? { kind: 'text', value } : undefined
  }
  const valueMatch = body.match(/<v\b[^>]*>([\s\S]*?)<\/v>/i)
  if (!valueMatch) return undefined
  const value = decodeXml(valueMatch[1])
  if (type === 's') {
    const index = Number.parseInt(value, 10)
    return Number.isFinite(index) ? { kind: 'shared', value: index } : undefined
  }
  if (type === 'b') return { kind: 'text', value: value === '1' ? 'TRUE' : 'FALSE' }
  return { kind: 'text', value }
}

function parseRawRow(rowXml: string): RawSpreadsheetRow | undefined {
  const rowTagEnd = rowXml.indexOf('>')
  if (rowTagEnd < 0) return undefined
  const number = Number.parseInt(xmlAttribute(rowXml.slice(0, rowTagEnd + 1), 'r'), 10)
  const cells = new Map<number, RawCellValue>()
  let cursor = rowTagEnd + 1
  let previousColumn = 0

  while (cursor < rowXml.length) {
    const start = rowXml.indexOf('<c', cursor)
    if (start < 0) break
    const boundary = rowXml[start + 2]
    if (boundary && boundary !== '>' && !/\s/.test(boundary)) {
      cursor = start + 2
      continue
    }
    const tagEnd = rowXml.indexOf('>', start + 2)
    if (tagEnd < 0) break
    const tag = rowXml.slice(start, tagEnd + 1)
    const reference = xmlAttribute(tag, 'r')
    const column = columnNumber(reference) || previousColumn + 1
    previousColumn = column
    if (tag.endsWith('/>')) {
      cursor = tagEnd + 1
      continue
    }
    const end = rowXml.indexOf('</c>', tagEnd + 1)
    if (end < 0) break
    if (column <= MAX_RELEVANT_COLUMN) {
      const value = parseRawCell(tag, rowXml.slice(tagEnd + 1, end))
      if (value) cells.set(column, value)
    }
    cursor = end + 4
  }

  if (!cells.size) return undefined
  return { number: Number.isFinite(number) ? number : 0, cells }
}

class WorksheetCollector {
  private readonly decoder = new TextDecoder()
  private buffer = ''
  readonly rows: RawSpreadsheetRow[] = []

  push(chunk: Uint8Array, final: boolean) {
    this.buffer += this.decoder.decode(chunk, { stream: !final })
    this.consumeRows(final)
  }

  private consumeRows(final: boolean) {
    while (this.buffer) {
      const match = /<row(?:\s|>)/i.exec(this.buffer)
      if (!match) {
        this.buffer = final ? '' : this.buffer.slice(-8)
        return
      }
      if (match.index > 0) this.buffer = this.buffer.slice(match.index)
      const end = this.buffer.indexOf('</row>')
      if (end < 0) return
      const row = parseRawRow(this.buffer.slice(0, end + 6))
      if (row) {
        this.rows.push(row)
        if (this.rows.length > MAX_NON_EMPTY_ROWS) throw new Error('Excel 有效数据行过多，无法安全导入')
      }
      this.buffer = this.buffer.slice(end + 6)
    }
  }
}

class TextCollector {
  private readonly decoder = new TextDecoder()
  private readonly chunks: string[] = []

  push(chunk: Uint8Array, final: boolean) {
    this.chunks.push(this.decoder.decode(chunk, { stream: !final }))
  }

  value() {
    return this.chunks.join('')
  }
}

async function extractZipEntries<T>(
  file: Blob,
  selectedPaths: Set<string>,
  createCollector: (path: string) => { push: (chunk: Uint8Array, final: boolean) => void } & T,
) {
  const collectors = new Map<string, ReturnType<typeof createCollector>>()
  let selectedXmlBytes = 0
  let extractionError: Error | undefined
  const unzip = new Unzip((entry) => {
    const path = normalizeZipPath(entry.name)
    if (!selectedPaths.has(path)) return
    if (entry.originalSize && entry.originalSize > MAX_XML_ENTRY_BYTES) {
      extractionError = new Error(`Excel 工作表过大：${path}`)
      return
    }
    const collector = createCollector(path)
    collectors.set(path, collector)
    entry.ondata = (error, chunk, final) => {
      if (error) {
        extractionError = error
        return
      }
      selectedXmlBytes += chunk.byteLength
      if (selectedXmlBytes > MAX_SELECTED_XML_BYTES) {
        extractionError = new Error('Excel 有效数据体积过大，无法安全导入')
        entry.terminate()
        return
      }
      try {
        collector.push(chunk, final)
      } catch (error) {
        extractionError = error instanceof Error ? error : new Error('Excel 工作表解析失败')
        entry.terminate()
      }
    }
    entry.start()
  })
  unzip.register(UnzipInflate)

  const reader = file.stream().getReader()
  try {
    while (true) {
      const { done, value } = await reader.read()
      unzip.push(value ?? new Uint8Array(), done)
      if (extractionError) throw extractionError
      if (done) break
    }
  } catch (error) {
    await reader.cancel().catch(() => undefined)
    throw error
  }
  return collectors
}

function parseSharedStrings(xml: string) {
  const values: string[] = []
  for (const match of xml.matchAll(/<si\b[^>]*>([\s\S]*?)<\/si>/gi)) values.push(textNodes(match[1]))
  return values
}

function parseWorkbookSheets(workbookXml: string, relationshipsXml: string) {
  const relationships = new Map<string, string>()
  for (const match of relationshipsXml.matchAll(/<Relationship\b[^>]*\/?\s*>/gi)) {
    const tag = match[0]
    const id = xmlAttribute(tag, 'Id')
    const target = xmlAttribute(tag, 'Target')
    if (id && target) relationships.set(id, resolveWorkbookTarget(target))
  }

  const sheets: Array<{ name: string; path: string; visible: boolean }> = []
  for (const match of workbookXml.matchAll(/<sheet\b[^>]*\/?\s*>/gi)) {
    const tag = match[0]
    const relationshipId = xmlAttribute(tag, 'r:id')
    const path = relationships.get(relationshipId)
    if (!path) continue
    sheets.push({
      name: xmlAttribute(tag, 'name') || '未命名工作表',
      path,
      visible: !xmlAttribute(tag, 'state') || xmlAttribute(tag, 'state').toLowerCase() === 'visible',
    })
  }
  return sheets
}

export async function readXlsxFile(file: Blob): Promise<SpreadsheetSheet[]> {
  const metadataPaths = new Set([WORKBOOK_PATH, WORKBOOK_RELS_PATH, SHARED_STRINGS_PATH])
  const metadata = await extractZipEntries(file, metadataPaths, () => new TextCollector())
  const workbookXml = metadata.get(WORKBOOK_PATH)?.value()
  const relationshipsXml = metadata.get(WORKBOOK_RELS_PATH)?.value()
  if (!workbookXml || !relationshipsXml) throw new Error('文件不是有效的 .xlsx 工作簿')

  const workbookSheets = parseWorkbookSheets(workbookXml, relationshipsXml)
  const visibleSheets = workbookSheets.filter((sheet) => sheet.visible)
  if (!visibleSheets.length) throw new Error('Excel 中没有可读取的工作表')

  const visiblePaths = new Set(visibleSheets.map((sheet) => sheet.path))
  const worksheetCollectors = await extractZipEntries(file, visiblePaths, () => new WorksheetCollector())
  const sharedStrings = parseSharedStrings(metadata.get(SHARED_STRINGS_PATH)?.value() ?? '')

  return visibleSheets.map((sheet) => {
    const rawRows = worksheetCollectors.get(sheet.path)?.rows ?? []
    return {
      name: sheet.name,
      rows: rawRows.map((row) => ({
        number: row.number,
        cells: new Map([...row.cells].map(([column, cell]) => [
          column,
          cell.kind === 'shared' ? sharedStrings[cell.value] ?? '' : cell.value,
        ])),
      })),
    }
  })
}
