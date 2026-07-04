import { useEffect, useMemo, useRef, useState } from 'react'
import { Activity, AlertTriangle, Clipboard, Download, Pencil, Plus, RefreshCw, RotateCw, Search, Tag, Trash2 } from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { API_BASE, apiDownload, apiFetch, cn, getAuthToken, triggerBrowserDownload } from '@/lib/utils'

type Sub2ApiGroup = {
  id: number
  name: string
  platform: string
}

type Sub2ApiTag = {
  id: number
  name: string
  color: string
  account_count?: number
}

type Sub2ApiAccount = {
  id: string
  name: string
  email: string
  status: string
  plan_type: string
  created_at: string
  last_used_at: string
  group_ids: number[]
  groups: Sub2ApiGroup[]
  workspace_id: string
  tags: Sub2ApiTag[]
}

type ActionResult = {
  account_id: string
  status?: string
  result?: string
  message?: string
  reason?: string
  marked_error?: boolean
}

type CheckLog = {
  id: number
  account_id?: string
  message: string
  result?: string
  tone: 'success' | 'danger' | 'warning' | 'secondary'
  at: string
}

type CheckSummary = {
  ok?: number
  failed?: number
  rate_limited?: number
  skipped?: number
  marked_error?: number
  deleted?: number
  replaced?: number
  phone_skipped?: number
  free_skipped?: number
}

type InventoryCacheEntry = {
  groups: Sub2ApiGroup[]
  tags: Sub2ApiTag[]
  accounts: Sub2ApiAccount[]
}

const inventoryCache = new Map<string, InventoryCacheEntry>()
const VISIBLE_LOG_LIMIT = 120
const VISIBLE_LOG_MESSAGE_LIMIT = 800

function statusVariant(status: string): 'success' | 'warning' | 'danger' | 'secondary' {
  const value = String(status || '').toLowerCase()
  if (value === 'active' || value === 'ok' || value === '正常') return 'success'
  if (value === 'error' || value === 'errored' || value === 'failed' || value === 'invalid' || value === '错误') return 'danger'
  if (value === 'inactive' || value === 'disabled') return 'warning'
  return 'secondary'
}

function formatDate(value: string) {
  if (!value) return '-'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleString()
}

function inventoryCacheKey(groupId: string, status: string, search: string, tagFilter: string) {
  return JSON.stringify({
    groupId: groupId || '',
    status: status || 'all',
    search: search.trim(),
    tagFilter: tagFilter || '',
  })
}

function toVisibleLog(item: CheckLog): CheckLog {
  if (item.message.length <= VISIBLE_LOG_MESSAGE_LIMIT) return item
  return {
    ...item,
    message: `${item.message.slice(0, VISIBLE_LOG_MESSAGE_LIMIT)}\n... 页面展示已截断，复制日志可获取完整内容`,
  }
}

export default function Sub2ApiManagement() {
  const [groups, setGroups] = useState<Sub2ApiGroup[]>([])
  const [tags, setTags] = useState<Sub2ApiTag[]>([])
  const [accounts, setAccounts] = useState<Sub2ApiAccount[]>([])
  const [selectedIds, setSelectedIds] = useState<Set<string>>(() => new Set())
  const [groupId, setGroupId] = useState('')
  const [status, setStatus] = useState('all')
  const [tagFilter, setTagFilter] = useState('')
  const [search, setSearch] = useState('')
  const [workspaceIds, setWorkspaceIds] = useState('')
  const [selectedTagId, setSelectedTagId] = useState('')
  const [exportTagId, setExportTagId] = useState('')
  const [exportDialogOpen, setExportDialogOpen] = useState(false)
  const [exporting, setExporting] = useState(false)
  const [tagFormName, setTagFormName] = useState('')
  const [tagFormColor, setTagFormColor] = useState('')
  const [editingTagId, setEditingTagId] = useState<number | null>(null)
  const [tagBusy, setTagBusy] = useState(false)
  const [loading, setLoading] = useState(false)
  const [checking, setChecking] = useState(false)
  const [relogining, setRelogining] = useState(false)
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')
  const [lastResults, setLastResults] = useState<ActionResult[]>([])
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(10)
  const [checkLogs, setCheckLogs] = useState<CheckLog[]>([])
  const [liveSummary, setLiveSummary] = useState<CheckSummary | null>(null)
  const [logPaused, setLogPaused] = useState(false)
  const logPanelRef = useRef<HTMLDivElement | null>(null)
  const logBottomRef = useRef<HTMLDivElement | null>(null)
  const fullCheckLogsRef = useRef<CheckLog[]>([])
  const shouldFollowLogRef = useRef(true)

  const applyInventory = (entry: InventoryCacheEntry) => {
    setGroups(entry.groups)
    setTags(entry.tags)
    setAccounts(entry.accounts)
    setSelectedIds(new Set())
    setPage(1)
  }

  const load = async (options: { force?: boolean } = {}) => {
    setError('')
    const cacheKey = inventoryCacheKey(groupId, status, search, tagFilter)
    if (!options.force) {
      const cached = inventoryCache.get(cacheKey)
      if (cached) {
        applyInventory(cached)
        return
      }
    } else {
      inventoryCache.clear()
    }
    setLoading(true)
    try {
      const params = new URLSearchParams()
      if (groupId) params.set('group_id', groupId)
      if (status && status !== 'all') params.set('status', status)
      if (tagFilter) params.set('tag_id', tagFilter)
      if (search.trim()) params.set('search', search.trim())
      const data = await apiFetch(`/sub2api-management/inventory?${params}`)
      const entry = {
        groups: data.groups || [],
        tags: data.tags || [],
        accounts: data.accounts || [],
      }
      inventoryCache.set(cacheKey, entry)
      applyInventory(entry)
    } catch (err: any) {
      setError(err?.message || String(err))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    load()
  }, [groupId, status, tagFilter])

  useEffect(() => {
    if (!shouldFollowLogRef.current) return
    requestAnimationFrame(() => {
      logBottomRef.current?.scrollIntoView({ block: 'end' })
    })
  }, [checkLogs.length])

  const selectedOrVisibleIds = useMemo(() => {
    if (selectedIds.size > 0) return Array.from(selectedIds)
    return accounts.map((item) => item.id).filter(Boolean)
  }, [accounts, selectedIds])

  const reloginTargetIds = useMemo(() => {
    if (selectedIds.size > 0) return Array.from(selectedIds)
    return accounts
      .filter((item) => statusVariant(item.status) === 'danger')
      .map((item) => item.id)
      .filter(Boolean)
  }, [accounts, selectedIds])

  const stats = useMemo(() => {
    const total = accounts.length
    const active = accounts.filter((item) => statusVariant(item.status) === 'success').length
    const errorCount = accounts.filter((item) => statusVariant(item.status) === 'danger').length
    const k12 = accounts.filter((item) => String(item.plan_type || '').toLowerCase() === 'k12').length
    return { total, active, errorCount, k12 }
  }, [accounts])

  const pageCount = Math.max(1, Math.ceil(accounts.length / pageSize))
  const currentPage = Math.min(page, pageCount)
  const pageAccounts = useMemo(() => {
    const start = (currentPage - 1) * pageSize
    return accounts.slice(start, start + pageSize)
  }, [accounts, currentPage, pageSize])
  const accountLabelById = useMemo(() => {
    const labels = new Map<string, string>()
    accounts.forEach((item) => {
      if (!item.id) return
      labels.set(item.id, item.email || item.name || `#${item.id}`)
    })
    return labels
  }, [accounts])

  const toggleSelected = (accountId: string) => {
    setSelectedIds((current) => {
      const next = new Set(current)
      if (next.has(accountId)) next.delete(accountId)
      else next.add(accountId)
      return next
    })
  }

  const toggleAll = () => {
    setSelectedIds((current) => {
      const pageIds = pageAccounts.map((item) => item.id).filter(Boolean)
      const allPageSelected = pageIds.length > 0 && pageIds.every((id) => current.has(id))
      const next = new Set(current)
      if (allPageSelected) {
        pageIds.forEach((id) => next.delete(id))
      } else {
        pageIds.forEach((id) => next.add(id))
      }
      return next
    })
  }

  const resetLiveLogs = () => {
    fullCheckLogsRef.current = []
    shouldFollowLogRef.current = true
    setLogPaused(false)
    setCheckLogs([])
  }

  const appendLog = (item: CheckLog) => {
    fullCheckLogsRef.current.push(item)
    setCheckLogs((current) => [...current, toVisibleLog(item)].slice(-VISIBLE_LOG_LIMIT))
  }

  const handleLogScroll = () => {
    const panel = logPanelRef.current
    if (!panel) return
    const distanceToBottom = panel.scrollHeight - panel.scrollTop - panel.clientHeight
    const shouldFollow = distanceToBottom < 48
    shouldFollowLogRef.current = shouldFollow
    setLogPaused((current) => (current === !shouldFollow ? current : !shouldFollow))
  }

  const resumeLogFollow = () => {
    shouldFollowLogRef.current = true
    setLogPaused(false)
    requestAnimationFrame(() => {
      logBottomRef.current?.scrollIntoView({ block: 'end', behavior: 'smooth' })
    })
  }

  const appendCheckResultLog = (event: any) => {
    const accountId = event.account_id ? String(event.account_id) : ''
    const label = accountLabelById.get(accountId) || (accountId ? `#${accountId}` : '未知账号')
    const result = String(event.result || '')
    const markedError = Boolean(event.marked_error)
    let message = `${label} 测活跳过`
    let tone: CheckLog['tone'] = 'secondary'
    if (result === 'ok') {
      message = `${label} 请求对话成功，状态正常`
      tone = 'success'
    } else if (result === 'dead') {
      message = `${label} 请求对话失败，状态异常${markedError ? '，已标记错误' : '，标记错误失败'}`
      tone = 'danger'
    } else if (result === 'rate_limited') {
      message = `${label} 请求对话达到额度限制，等待冷却，未标记错误`
      tone = 'warning'
    } else if (event.reason) {
      message = `${label} 测活跳过：${String(event.reason)}`
    }
    const item: CheckLog = {
      id: Date.now() + Math.random(),
      account_id: accountId,
      message,
      result,
      tone,
      at: new Date().toLocaleTimeString(),
    }
    appendLog(item)
  }

  const appendReloginLog = (event: any) => {
    const accountId = event.account_id ? String(event.account_id) : ''
    const label = accountLabelById.get(accountId) || (accountId ? `#${accountId}` : '未知账号')
    const rawMessage = event.message ? String(event.message) : '重新登录处理中'
    const message = rawMessage.includes(label) ? rawMessage : `${label} ${rawMessage}`
    const item: CheckLog = {
      id: Date.now() + Math.random(),
      account_id: accountId,
      message,
      result: event.status || event.event,
      tone: 'secondary',
      at: new Date().toLocaleTimeString(),
    }
    appendLog(item)
  }

  const appendReloginResultLog = (event: any) => {
    const accountId = event.account_id ? String(event.account_id) : ''
    const label = accountLabelById.get(accountId) || (accountId ? `#${accountId}` : '未知账号')
    const status = String(event.status || '')
    let tone: CheckLog['tone'] = 'secondary'
    if (status === 'replaced' || status === 'deleted' || status === 'free_skipped') tone = 'success'
    else if (status === 'failed') tone = 'danger'
    else if (status === 'phone_skipped') tone = 'warning'
    const item: CheckLog = {
      id: Date.now() + Math.random(),
      account_id: accountId,
      message: `${label} ${event.message || status || '重新登录完成'}`,
      result: status,
      tone,
      at: new Date().toLocaleTimeString(),
    }
    appendLog(item)
  }

  const handleCheckEvent = (event: any) => {
    if (!event || typeof event !== 'object') return
    if (event.summary) setLiveSummary(event.summary)
    if (event.event === 'bulk_finished') {
      setLastResults(event.results || [])
      const summary = event.summary || {}
      setMessage(`测活完成：正常 ${summary.ok || 0}，异常 ${summary.failed || 0}，限流 ${summary.rate_limited || 0}，已标错误 ${summary.marked_error || 0}，跳过 ${summary.skipped || 0}`)
      return
    }
    if (event.event === 'bulk_failed') {
      setError(event.message || '批量测活失败')
      const item: CheckLog = {
        id: Date.now() + Math.random(),
        message: event.message || '批量测活失败',
        result: 'failed',
        tone: 'danger',
        at: new Date().toLocaleTimeString(),
      }
      appendLog(item)
      return
    }
    if (event.event === 'account_finished') {
      appendCheckResultLog(event)
    }
  }

  const handleReloginEvent = (event: any) => {
    if (!event || typeof event !== 'object') return
    if (event.summary) setLiveSummary(event.summary)
    if (event.event === 'relogin_log') {
      appendReloginLog(event)
      return
    }
    if (event.event === 'relogin_account_finished') {
      appendReloginResultLog(event)
      return
    }
    if (event.event === 'relogin_finished') {
      setLastResults(event.results || [])
      const summary = event.summary || {}
      setMessage(`重新登录完成：K12替换 ${summary.replaced || 0}，封禁删除 ${summary.deleted || 0}，手机跳过 ${summary.phone_skipped || 0}，free跳过 ${summary.free_skipped || 0}，失败 ${summary.failed || 0}`)
      return
    }
    if (event.event === 'relogin_failed') {
      setError(event.message || '重新登录错误账号失败')
      const item: CheckLog = {
        id: Date.now() + Math.random(),
        message: event.message || '重新登录错误账号失败',
        result: 'failed',
        tone: 'danger',
        at: new Date().toLocaleTimeString(),
      }
      appendLog(item)
    }
  }

  const runBulkCheck = async () => {
    if (selectedOrVisibleIds.length === 0) {
      setError('没有可测活的 Sub2API 账号')
      return
    }
    setChecking(true)
    setError('')
    setMessage('')
    setLastResults([])
    setLiveSummary(null)
    resetLiveLogs()
    try {
      const headers: Record<string, string> = { 'Content-Type': 'application/json' }
      const token = getAuthToken()
      if (token) headers.Authorization = `Bearer ${token}`
      const response = await fetch(`${API_BASE}/sub2api-management/bulk-check/stream`, {
        method: 'POST',
        headers,
        body: JSON.stringify({ account_ids: selectedOrVisibleIds, concurrency: 10 }),
      })
      if (!response.ok) throw new Error(await response.text())
      if (!response.body) throw new Error('浏览器不支持流式读取测活日志')
      const reader = response.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''
      while (true) {
        const { value, done } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })
        const chunks = buffer.split('\n\n')
        buffer = chunks.pop() || ''
        for (const chunk of chunks) {
          const data = chunk
            .split('\n')
            .filter((line) => line.startsWith('data:'))
            .map((line) => line.slice(5).trim())
            .join('\n')
          if (!data) continue
          handleCheckEvent(JSON.parse(data))
        }
      }
      if (buffer.trim()) {
        const data = buffer
          .split('\n')
          .filter((line) => line.startsWith('data:'))
          .map((line) => line.slice(5).trim())
          .join('\n')
        if (data) handleCheckEvent(JSON.parse(data))
      }
      await load()
    } catch (err: any) {
      setError(err?.message || String(err))
    } finally {
      setChecking(false)
    }
  }

  const runReloginErrors = async () => {
    setRelogining(true)
    setError('')
    setMessage('')
    setLastResults([])
    setLiveSummary(null)
    resetLiveLogs()
    try {
      const headers: Record<string, string> = { 'Content-Type': 'application/json' }
      const token = getAuthToken()
      if (token) headers.Authorization = `Bearer ${token}`
      const response = await fetch(`${API_BASE}/sub2api-management/relogin-errors/stream`, {
        method: 'POST',
        headers,
        body: JSON.stringify({
          account_ids: reloginTargetIds,
          group_id: groupId ? Number(groupId) : null,
          workspace_ids: workspaceIds,
          concurrency: 1,
        }),
      })
      if (!response.ok) throw new Error(await response.text())
      if (!response.body) throw new Error('浏览器不支持流式读取重新登录日志')
      const reader = response.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''
      while (true) {
        const { value, done } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })
        const chunks = buffer.split('\n\n')
        buffer = chunks.pop() || ''
        for (const chunk of chunks) {
          const data = chunk
            .split('\n')
            .filter((line) => line.startsWith('data:'))
            .map((line) => line.slice(5).trim())
            .join('\n')
          if (!data) continue
          handleReloginEvent(JSON.parse(data))
        }
      }
      if (buffer.trim()) {
        const data = buffer
          .split('\n')
          .filter((line) => line.startsWith('data:'))
          .map((line) => line.slice(5).trim())
          .join('\n')
        if (data) handleReloginEvent(JSON.parse(data))
      }
      await load()
    } catch (err: any) {
      setError(err?.message || String(err))
    } finally {
      setRelogining(false)
    }
  }

  const copyLogs = async () => {
    const text = fullCheckLogsRef.current
      .map((item) => `[${item.at}] ${item.message}`)
      .join('\n')
    if (!text) return
    await navigator.clipboard.writeText(text)
    setMessage('实时日志已复制到剪贴板')
  }

  const openExportDialog = () => {
    if (selectedIds.size === 0) {
      setError('请先勾选要导出的账号')
      return
    }
    setError('')
    setMessage('')
    setExportTagId(selectedTagId || '')
    setExportDialogOpen(true)
  }

  const exportSelectedAccounts = async () => {
    if (selectedIds.size === 0) {
      setError('请先勾选要导出的账号')
      return
    }
    if (!exportTagId) {
      setError('请选择导出标签')
      return
    }
    setExporting(true)
    setError('')
    setMessage('')
    try {
      const tag = tags.find((item) => String(item.id) === exportTagId)
      const { blob, filename } = await apiDownload('/sub2api-management/export-data', {
        method: 'POST',
        body: JSON.stringify({
          account_ids: Array.from(selectedIds),
          tag_ids: [Number(exportTagId)],
          timezone: 'Asia/Shanghai',
          include_proxies: true,
        }),
      })
      triggerBrowserDownload(blob, filename)
      setMessage(`已导出 ${selectedIds.size} 个账号，并打标签：${tag?.name || exportTagId}`)
      setExportDialogOpen(false)
      inventoryCache.clear()
      await load({ force: true })
    } catch (err: any) {
      setError(err?.message || String(err))
    } finally {
      setExporting(false)
    }
  }

  const resetTagForm = () => {
    setEditingTagId(null)
    setTagFormName('')
    setTagFormColor('')
  }

  const editTag = (tag: Sub2ApiTag) => {
    setEditingTagId(tag.id)
    setTagFormName(tag.name)
    setTagFormColor(tag.color || '')
  }

  const saveTag = async () => {
    const name = tagFormName.trim()
    if (!name) {
      setError('标签名称不能为空')
      return
    }
    setTagBusy(true)
    setError('')
    setMessage('')
    try {
      const body = JSON.stringify({ name, color: tagFormColor.trim() })
      if (editingTagId) {
        await apiFetch(`/sub2api-management/tags/${editingTagId}`, { method: 'PUT', body })
        setMessage(`标签已更新：${name}`)
      } else {
        await apiFetch('/sub2api-management/tags', { method: 'POST', body })
        setMessage(`标签已创建：${name}`)
      }
      resetTagForm()
      inventoryCache.clear()
      await load({ force: true })
    } catch (err: any) {
      setError(err?.message || String(err))
    } finally {
      setTagBusy(false)
    }
  }

  const deleteTag = async (tag: Sub2ApiTag) => {
    if (!window.confirm(`确认删除标签“${tag.name}”？已打标账号会同时移除该标签。`)) return
    setTagBusy(true)
    setError('')
    setMessage('')
    try {
      await apiFetch(`/sub2api-management/tags/${tag.id}`, { method: 'DELETE' })
      const wasFiltering = tagFilter === String(tag.id)
      if (wasFiltering) setTagFilter('')
      if (selectedTagId === String(tag.id)) setSelectedTagId('')
      if (editingTagId === tag.id) resetTagForm()
      setMessage(`标签已删除：${tag.name}`)
      inventoryCache.clear()
      if (!wasFiltering) await load({ force: true })
    } catch (err: any) {
      setError(err?.message || String(err))
    } finally {
      setTagBusy(false)
    }
  }

  const updateSelectedAccountTags = async (action: 'add' | 'remove') => {
    if (selectedIds.size === 0) {
      setError('请先勾选要打标的账号')
      return
    }
    if (!selectedTagId) {
      setError('请先选择标签')
      return
    }
    setTagBusy(true)
    setError('')
    setMessage('')
    try {
      const data = await apiFetch('/sub2api-management/account-tags', {
        method: 'POST',
        body: JSON.stringify({
          account_ids: Array.from(selectedIds),
          tag_ids: [Number(selectedTagId)],
          action,
        }),
      })
      const tag = tags.find((item) => String(item.id) === selectedTagId)
      setMessage(`${action === 'add' ? '已添加' : '已移除'}标签：${tag?.name || selectedTagId}，影响 ${data.changed || 0} 条关系`)
      inventoryCache.clear()
      await load({ force: true })
    } catch (err: any) {
      setError(err?.message || String(err))
    } finally {
      setTagBusy(false)
    }
  }

  return (
    <div className="space-y-5">
      <div className="flex flex-col gap-3 lg:flex-row lg:items-end lg:justify-between">
        <div>
          <div className="mb-2 inline-flex items-center gap-2 rounded-full border border-[var(--accent-edge)] bg-[var(--accent-soft)] px-3 py-1 text-xs font-semibold text-[var(--accent)]">
            <Activity className="h-3.5 w-3.5" />
            Sub2API 远端仓管
          </div>
          <h1 className="text-xl font-bold text-[var(--text-primary)]">Sub2Api管理</h1>
          <p className="mt-1 text-sm text-[var(--text-muted)]">
            使用设置页的 Sub2API 后台信息读取远端分组和账号，支持按分组筛选、批量测活和错误账号处理。
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button variant="outline" onClick={() => load({ force: true })} disabled={loading || checking || relogining}>
            <RefreshCw className={cn('mr-1.5 h-3.5 w-3.5', loading && 'animate-spin')} />
            刷新
          </Button>
          <Button onClick={runBulkCheck} disabled={checking || loading || relogining || selectedOrVisibleIds.length === 0}>
            <Activity className={cn('mr-1.5 h-3.5 w-3.5', checking && 'animate-pulse')} />
            批量测活
          </Button>
          <Button variant="outline" onClick={openExportDialog} disabled={exporting || checking || loading || relogining || selectedIds.size === 0}>
            <Download className={cn('mr-1.5 h-3.5 w-3.5', exporting && 'animate-pulse')} />
            导出选中
          </Button>
          <Button variant="outline" onClick={runReloginErrors} disabled={relogining || loading || checking || reloginTargetIds.length === 0}>
            <RotateCw className={cn('mr-1.5 h-3.5 w-3.5', relogining && 'animate-spin')} />
            重新登录错误帐号
          </Button>
        </div>
      </div>

      <div className="grid gap-3 md:grid-cols-4">
        {[
          ['远端账号', stats.total, 'text-[var(--accent)]'],
          ['正常', stats.active, 'text-emerald-500'],
          ['错误', stats.errorCount, 'text-red-500'],
          ['K12', stats.k12, 'text-sky-500'],
        ].map(([label, value, tone]) => (
          <Card key={String(label)} className="border border-[var(--border)]">
            <CardContent className="flex items-center justify-between">
              <span className="text-sm text-[var(--text-muted)]">{label}</span>
              <span className={cn('text-2xl font-bold', String(tone))}>{value}</span>
            </CardContent>
          </Card>
        ))}
      </div>

      <Card className="border border-[var(--border)]">
        <CardHeader className="mb-4">
          <CardTitle>筛选与处理范围</CardTitle>
        </CardHeader>
        <CardContent className="grid gap-3 lg:grid-cols-[220px_160px_220px_1fr_1fr]">
          <select
            value={groupId}
            onChange={(event) => setGroupId(event.target.value)}
            className="control-surface h-9"
          >
            <option value="">全部分组</option>
            {groups.map((group) => (
              <option key={group.id} value={group.id}>{group.name}</option>
            ))}
          </select>
          <select
            value={status}
            onChange={(event) => setStatus(event.target.value)}
            className="control-surface h-9"
          >
            <option value="all">全部状态</option>
            <option value="active">active</option>
            <option value="error">error</option>
            <option value="inactive">inactive</option>
          </select>
          <select
            value={tagFilter}
            onChange={(event) => setTagFilter(event.target.value)}
            className="control-surface h-9"
          >
            <option value="">全部标签</option>
            {tags.map((tag) => (
              <option key={tag.id} value={tag.id}>{tag.name} ({tag.account_count || 0})</option>
            ))}
          </select>
          <div className="relative">
            <Search className="pointer-events-none absolute left-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-[var(--text-muted)]" />
            <input
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === 'Enter') load()
              }}
              placeholder="搜索帐号名 / 邮箱，回车刷新"
              className="control-surface h-9 w-full pl-9"
            />
          </div>
          <textarea
            value={workspaceIds}
            onChange={(event) => setWorkspaceIds(event.target.value)}
            placeholder="K12 Workspace ID 覆盖值（可选，多个用换行或逗号分隔）"
            className="control-surface min-h-20 w-full resize-y"
          />
        </CardContent>
      </Card>

      <Card className="border border-[var(--border)]">
        <CardHeader className="mb-4">
          <CardTitle>标签管理</CardTitle>
          <p className="text-xs text-[var(--text-muted)]">
            标签只保存在本地 DB；先勾选账号，再选择标签进行批量添加或移除。
          </p>
        </CardHeader>
        <CardContent className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_minmax(360px,420px)]">
          <div className="space-y-3">
            <div className="flex flex-wrap items-center gap-2">
              <select
                value={selectedTagId}
                onChange={(event) => setSelectedTagId(event.target.value)}
                className="control-surface h-9 min-w-[220px]"
              >
                <option value="">选择要操作的标签</option>
                {tags.map((tag) => (
                  <option key={tag.id} value={tag.id}>{tag.name}</option>
                ))}
              </select>
              <Button
                variant="outline"
                onClick={() => updateSelectedAccountTags('add')}
                disabled={tagBusy || selectedIds.size === 0 || !selectedTagId}
              >
                <Tag className="mr-1.5 h-3.5 w-3.5" />
                给已选账号打标签
              </Button>
              <Button
                variant="outline"
                onClick={() => updateSelectedAccountTags('remove')}
                disabled={tagBusy || selectedIds.size === 0 || !selectedTagId}
              >
                移除已选账号标签
              </Button>
              <Badge variant="secondary">已选 {selectedIds.size}</Badge>
            </div>
            <div className="flex flex-wrap gap-2">
              {tags.length > 0 ? tags.map((tag) => (
                <Badge key={tag.id} variant="secondary" className="gap-1">
                  {tag.color && <span className="h-2 w-2 rounded-full" style={{ backgroundColor: tag.color }} />}
                  {tag.name}
                  <span className="text-[var(--text-muted)]">({tag.account_count || 0})</span>
                </Badge>
              )) : (
                <span className="text-xs text-[var(--text-muted)]">暂无标签，先在右侧创建。</span>
              )}
            </div>
          </div>
          <div className="space-y-3 rounded-xl border border-[var(--border-soft)] bg-[var(--bg-pane)] p-3">
            <div className="grid gap-2 sm:grid-cols-[1fr_120px_auto]">
              <input
                value={tagFormName}
                onChange={(event) => setTagFormName(event.target.value)}
                placeholder="标签名称"
                className="control-surface h-9 w-full"
              />
              <input
                value={tagFormColor}
                onChange={(event) => setTagFormColor(event.target.value)}
                placeholder="颜色，可选"
                className="control-surface h-9 w-full"
              />
              <Button onClick={saveTag} disabled={tagBusy || !tagFormName.trim()}>
                {editingTagId ? <Pencil className="mr-1.5 h-3.5 w-3.5" /> : <Plus className="mr-1.5 h-3.5 w-3.5" />}
                {editingTagId ? '保存' : '新增'}
              </Button>
            </div>
            {editingTagId && (
              <Button variant="ghost" size="sm" onClick={resetTagForm} disabled={tagBusy}>
                取消编辑
              </Button>
            )}
            <div className="max-h-40 space-y-2 overflow-y-auto">
              {tags.map((tag) => (
                <div key={tag.id} className="flex items-center justify-between gap-2 rounded-lg border border-[var(--border-soft)] bg-[var(--bg-card)] px-3 py-2 text-sm">
                  <div className="flex min-w-0 items-center gap-2">
                    {tag.color && <span className="h-2.5 w-2.5 rounded-full" style={{ backgroundColor: tag.color }} />}
                    <span className="truncate font-medium text-[var(--text-primary)]">{tag.name}</span>
                    <span className="text-xs text-[var(--text-muted)]">{tag.account_count || 0} 个账号</span>
                  </div>
                  <div className="flex shrink-0 items-center gap-1">
                    <Button variant="ghost" size="sm" onClick={() => editTag(tag)} disabled={tagBusy}>
                      <Pencil className="h-3.5 w-3.5" />
                    </Button>
                    <Button variant="ghost" size="sm" onClick={() => deleteTag(tag)} disabled={tagBusy}>
                      <Trash2 className="h-3.5 w-3.5 text-red-500" />
                    </Button>
                  </div>
                </div>
              ))}
              {tags.length === 0 && (
                <div className="rounded-lg border border-dashed border-[var(--border)] px-3 py-6 text-center text-xs text-[var(--text-muted)]">
                  暂无标签
                </div>
              )}
            </div>
          </div>
        </CardContent>
      </Card>

      {(error || message) && (
        <div
          className={cn(
            'rounded-xl border px-4 py-3 text-sm',
            error
              ? 'border-red-500/30 bg-red-500/10 text-red-700 dark:text-red-300'
              : 'border-emerald-500/30 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300',
          )}
        >
          {error || message}
        </div>
      )}

      <Card className="overflow-hidden border border-[var(--border)] p-0">
        <div className="grid gap-0 lg:grid-cols-[minmax(0,1fr)_420px]">
        <div className="overflow-x-auto">
          <table className="w-full min-w-[1120px] text-sm">
            <thead className="border-b border-[var(--border)] bg-[var(--bg-pane)] text-left text-xs uppercase tracking-wide text-[var(--text-muted)]">
              <tr>
                <th className="w-10 px-4 py-3">
                  <input
                    type="checkbox"
                    checked={pageAccounts.length > 0 && pageAccounts.every((item) => selectedIds.has(item.id))}
                    onChange={toggleAll}
                    className="h-4 w-4 accent-[var(--accent)]"
                  />
                </th>
                <th className="px-4 py-3">帐号名</th>
                <th className="px-4 py-3">当前状态</th>
                <th className="px-4 py-3">类型</th>
                <th className="px-4 py-3">标签</th>
                <th className="px-4 py-3">分组</th>
                <th className="px-4 py-3">创建时间</th>
                <th className="px-4 py-3">最近使用时间</th>
              </tr>
            </thead>
            <tbody>
              {pageAccounts.map((account) => (
                <tr key={account.id} className="border-b border-[var(--border-soft)] hover:bg-[var(--bg-hover)]">
                  <td className="px-4 py-3">
                    <input
                      type="checkbox"
                      checked={selectedIds.has(account.id)}
                      onChange={() => toggleSelected(account.id)}
                      className="h-4 w-4 accent-[var(--accent)]"
                    />
                  </td>
                  <td className="px-4 py-3">
                    <div className="font-medium text-[var(--text-primary)]">{account.name || '-'}</div>
                    <div className="text-xs text-[var(--text-muted)]">#{account.id}</div>
                  </td>
                  <td className="px-4 py-3">
                    <Badge variant={statusVariant(account.status)}>{account.status || 'unknown'}</Badge>
                  </td>
                  <td className="px-4 py-3 text-[var(--text-secondary)]">{account.plan_type || '-'}</td>
                  <td className="px-4 py-3">
                    <div className="flex max-w-[220px] flex-wrap gap-1">
                      {(account.tags || []).length > 0
                        ? account.tags.map((tag) => (
                          <Badge key={tag.id} variant="secondary" className="gap-1">
                            {tag.color && <span className="h-2 w-2 rounded-full" style={{ backgroundColor: tag.color }} />}
                            {tag.name}
                          </Badge>
                        ))
                        : <span className="text-[var(--text-muted)]">-</span>}
                    </div>
                  </td>
                  <td className="px-4 py-3">
                    <div className="flex max-w-[240px] flex-wrap gap-1">
                      {(account.groups || []).length > 0
                        ? account.groups.map((group) => <Badge key={group.id} variant="secondary">{group.name}</Badge>)
                        : <span className="text-[var(--text-muted)]">-</span>}
                    </div>
                  </td>
                  <td className="px-4 py-3 text-[var(--text-muted)]">{formatDate(account.created_at)}</td>
                  <td className="px-4 py-3 text-[var(--text-muted)]">{formatDate(account.last_used_at)}</td>
                </tr>
              ))}
              {accounts.length === 0 && (
                <tr>
                  <td colSpan={8} className="px-4 py-12 text-center text-[var(--text-muted)]">
                    {loading ? '加载中...' : '暂无远端 Sub2API 账号'}
                  </td>
                </tr>
              )}
            </tbody>
          </table>
          {accounts.length > 0 && (
            <div className="flex flex-wrap items-center justify-between gap-2 border-t border-[var(--border)] px-4 py-3 text-xs text-[var(--text-muted)]">
              <div className="flex flex-wrap items-center gap-2">
                <span>
                  当前 {((currentPage - 1) * pageSize) + 1}-{Math.min(currentPage * pageSize, accounts.length)} / {accounts.length}
                </span>
                <label className="flex items-center gap-1">
                  <span>每页</span>
                  <select
                    value={pageSize}
                    onChange={(event) => {
                      setPageSize(Number(event.target.value))
                      setPage(1)
                    }}
                    className="control-surface h-8 w-20 py-0 text-xs"
                  >
                    {[10, 20, 50, 100].map((value) => (
                      <option key={value} value={value}>{value}</option>
                    ))}
                  </select>
                </label>
              </div>
              <div className="flex items-center gap-2">
                <Button variant="outline" size="sm" onClick={() => setPage(1)} disabled={currentPage <= 1}>首页</Button>
                <Button variant="outline" size="sm" onClick={() => setPage((value) => Math.max(1, value - 1))} disabled={currentPage <= 1}>上一页</Button>
                <span className="px-2">第 {currentPage} / {pageCount} 页</span>
                <Button variant="outline" size="sm" onClick={() => setPage((value) => Math.min(pageCount, value + 1))} disabled={currentPage >= pageCount}>下一页</Button>
                <Button variant="outline" size="sm" onClick={() => setPage(pageCount)} disabled={currentPage >= pageCount}>末页</Button>
              </div>
            </div>
          )}
        </div>
        <aside className="border-t border-[var(--border)] bg-[var(--bg-pane)] lg:border-l lg:border-t-0">
          <div className="flex items-center justify-between border-b border-[var(--border)] px-4 py-3">
            <div>
              <div className="text-sm font-semibold text-[var(--text-primary)]">实时日志</div>
              <div className="text-xs text-[var(--text-muted)]">
                {checking ? '测活进行中' : relogining ? '错误账号重新登录中' : '仅展示最近日志，复制可获取完整日志'}
              </div>
            </div>
            <div className="flex items-center gap-2">
              {liveSummary && (
                <Badge variant="secondary">
                  {checking
                    ? `正常 ${liveSummary.ok || 0} / 异常 ${liveSummary.failed || 0} / 限流 ${liveSummary.rate_limited || 0}`
                    : `替换 ${liveSummary.replaced || 0} / 删除 ${liveSummary.deleted || 0} / 失败 ${liveSummary.failed || 0}`}
                </Badge>
              )}
              <Button variant="outline" size="sm" onClick={copyLogs} disabled={checkLogs.length === 0}>
                <Clipboard className="mr-1.5 h-3.5 w-3.5" />
                复制日志
              </Button>
              {logPaused && (
                <Button variant="outline" size="sm" onClick={resumeLogFollow}>
                  回到底部
                </Button>
              )}
            </div>
          </div>
          <div ref={logPanelRef} onScroll={handleLogScroll} className="max-h-[620px] space-y-2 overflow-y-auto p-3">
            {checkLogs.length === 0 && (
              <div className="rounded-lg border border-dashed border-[var(--border)] px-3 py-8 text-center text-xs text-[var(--text-muted)]">
                点击“批量测活”后只显示每个账号的最终测活结果；点击“重新登录错误帐号”后显示协议登录和 K12 处理日志。页面只保留最近日志用于展示，完整日志可通过复制按钮获取。
              </div>
            )}
            {checkLogs.map((item) => (
              <div key={item.id} className="rounded-lg border border-[var(--border-soft)] bg-[var(--bg-card)] px-3 py-2 text-xs">
                <div className="mb-1 flex items-center justify-between gap-2">
                  <span className="font-mono text-[var(--text-muted)]">{item.at}</span>
                  {item.result && (
                    <Badge variant={item.tone}>
                      {item.result}
                    </Badge>
                  )}
                </div>
                <div
                  className={cn(
                    'mt-1 whitespace-pre-wrap break-words font-medium',
                    item.tone === 'success' && 'text-emerald-600 dark:text-emerald-300',
                    item.tone === 'danger' && 'text-red-600 dark:text-red-300',
                    item.tone === 'warning' && 'text-amber-600 dark:text-amber-300',
                    item.tone === 'secondary' && 'text-[var(--text-primary)]',
                  )}
                >
                  {item.message}
                </div>
              </div>
            ))}
            <div ref={logBottomRef} />
          </div>
        </aside>
        </div>
      </Card>

      {lastResults.length > 0 && (
        <Card className="border border-[var(--border)]">
          <CardHeader>
            <CardTitle>最近一次操作结果</CardTitle>
          </CardHeader>
          <CardContent className="space-y-2">
            {lastResults.slice(0, 20).map((item) => (
              <div key={item.account_id} className="flex items-start gap-2 rounded-lg bg-[var(--bg-pane)] px-3 py-2 text-xs">
                <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-[var(--text-muted)]" />
                <div>
                  <span className="font-mono text-[var(--text-secondary)]">#{item.account_id}</span>
                  <span className="ml-2 text-[var(--text-primary)]">{item.status || item.result}</span>
                  <span className="ml-2 text-[var(--text-muted)]">{item.message || item.reason || ''}</span>
                </div>
              </div>
            ))}
          </CardContent>
        </Card>
      )}

      {exportDialogOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 px-4">
          <div className="w-full max-w-md rounded-2xl border border-[var(--border)] bg-[var(--bg-card)] p-5 shadow-2xl">
            <div className="mb-4">
              <h2 className="text-lg font-semibold text-[var(--text-primary)]">导出 Sub2API JSON</h2>
              <p className="mt-1 text-sm text-[var(--text-muted)]">
                将 {selectedIds.size} 个已选账号打上标签后，导出为一个 Sub2API JSON 文件。
              </p>
            </div>
            <label className="mb-2 block text-sm font-medium text-[var(--text-secondary)]">导出标签</label>
            <select
              value={exportTagId}
              onChange={(event) => setExportTagId(event.target.value)}
              className="control-surface h-10 w-full"
            >
              <option value="">选择标签</option>
              {tags.map((tag) => (
                <option key={tag.id} value={tag.id}>{tag.name}</option>
              ))}
            </select>
            {tags.length === 0 && (
              <p className="mt-2 text-xs text-amber-600 dark:text-amber-300">
                暂无可选标签，请先在页面的“标签管理”里创建标签。
              </p>
            )}
            <p className="mt-3 text-xs text-[var(--text-muted)]">
              导出会调用远端 Sub2API 的账号 data 接口，多个已选账号会打包进同一个 JSON 文件。
            </p>
            <div className="mt-5 flex justify-end gap-2">
              <Button variant="outline" onClick={() => setExportDialogOpen(false)} disabled={exporting}>
                取消
              </Button>
              <Button onClick={exportSelectedAccounts} disabled={exporting || !exportTagId}>
                <Download className={cn('mr-1.5 h-3.5 w-3.5', exporting && 'animate-pulse')} />
                {exporting ? '导出中...' : '确定导出'}
              </Button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
