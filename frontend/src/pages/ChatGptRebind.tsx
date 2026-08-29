import { useEffect, useMemo, useState } from 'react'
import { createPortal } from 'react-dom'
import {
  AlertTriangle,
  CheckCircle2,
  Clock,
  Forward,
  Loader2,
  RefreshCw,
  Search,
  X,
  XCircle,
} from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader } from '@/components/ui/card'
import { apiFetch, cn } from '@/lib/utils'
import { formatDateTime, translateAccountStatus, type Language } from '@/lib/i18n'
import { getTaskStatusText, isTerminalTaskStatus } from '@/lib/tasks'
import { useI18n } from '@/lib/i18n-context'

type RebindAccountItem = {
  id?: number | string
  email?: string
  current_email?: string
  status?: string
  registered_at?: string
  created_at?: string
}

type AccountsResponse = {
  items?: RebindAccountItem[]
  total?: number
  page?: number
  page_size?: number
}

type MailConfigResponse = {
  domains?: string[]
  api_url?: string
  api_token_masked?: string
  cloudflare_api_token_masked?: string
  cloudflare_account_id?: string
  forward_to?: string
}

const PAGE_SIZE_OPTIONS = [10, 20, 50, 100]
const CONCURRENCY_OPTIONS = [1, 2, 3, 4, 5]
const TASK_POLL_INTERVAL_MS = 2000

function accountEmail(item: RebindAccountItem) {
  return item.email || item.current_email || ''
}

function formatTime(value: string | undefined, language: Language) {
  if (!value) return '-'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return formatDateTime(date, language)
}

function errorText(exc: unknown, fallback: string) {
  const message = exc instanceof Error ? exc.message : String(exc || '')
  return message ? fallback + ': ' + message : fallback
}

export default function ChatGptRebind() {
  const { t, language } = useI18n()

  // ---- 账号列表 ------------------------------------------------------------
  const [items, setItems] = useState<RebindAccountItem[]>([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(20)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [searchInput, setSearchInput] = useState('')
  const [appliedSearch, setAppliedSearch] = useState('')
  const [reloadTick, setReloadTick] = useState(0)

  // ---- 选择与操作 ----------------------------------------------------------
  const [selectedIds, setSelectedIds] = useState<Set<number>>(new Set())
  const [concurrency, setConcurrency] = useState(1)
  const [creating, setCreating] = useState(false)
  const [actionError, setActionError] = useState('')

  // ---- 任务状态跟踪 --------------------------------------------------------
  const [activeTaskId, setActiveTaskId] = useState('')
  const [activeTaskStatus, setActiveTaskStatus] = useState('')
  const [taskError, setTaskError] = useState('')

  // ---- 换绑邮箱配置弹窗 ----------------------------------------------------
  const [mailOpen, setMailOpen] = useState(false)
  const [mailLoading, setMailLoading] = useState(false)
  const [mailSaving, setMailSaving] = useState(false)
  const [mailError, setMailError] = useState('')
  const [mailSaved, setMailSaved] = useState('')
  const [mailDomains, setMailDomains] = useState('')
  const [mailApiUrl, setMailApiUrl] = useState('')
  const [mailApiToken, setMailApiToken] = useState('')
  const [mailCloudflareToken, setMailCloudflareToken] = useState('')
  const [mailCloudflareAccountId, setMailCloudflareAccountId] = useState('')
  const [mailForwardTo, setMailForwardTo] = useState('')
  const [maskedCloudMailToken, setMaskedCloudMailToken] = useState('')
  const [maskedCloudflareToken, setMaskedCloudflareToken] = useState('')

  const pageCount = Math.max(1, Math.ceil(total / pageSize))

  useEffect(() => {
    let cancelled = false
    const run = async () => {
      setLoading(true)
      setError('')
      try {
        const params = new URLSearchParams({
          page: String(page),
          page_size: String(pageSize),
        })
        if (appliedSearch.trim()) params.set('email', appliedSearch.trim())
        const data: AccountsResponse = await apiFetch(
          '/chatgpt-rebind/accounts?' + params.toString(),
        )
        if (cancelled) return
        const nextItems = Array.isArray(data.items) ? data.items : []
        const nextTotal = Number(data.total || 0)
        setItems(nextItems)
        setTotal(nextTotal)
        // 搜索或数据变化导致当前页越界时回退到最后一页
        if (
          nextItems.length === 0 &&
          nextTotal > 0 &&
          page > 1 &&
          page > Math.ceil(nextTotal / pageSize)
        ) {
          setPage(Math.max(1, Math.ceil(nextTotal / pageSize)))
        }
      } catch (exc) {
        if (!cancelled) {
          setItems([])
          setTotal(0)
          setError(errorText(exc, t('chatgptRebind.loadFailed')))
        }
      } finally {
        if (!cancelled) setLoading(false)
      }
    }
    run()
    return () => {
      cancelled = true
    }
  }, [page, pageSize, appliedSearch, reloadTick, t])

  // 轮询当前换绑任务状态，终态后刷新列表
  useEffect(() => {
    if (!activeTaskId) return
    let cancelled = false
    let timer: number | undefined
    const poll = async () => {
      let nextStatus = ''
      let nextError = ''
      try {
        const data = await apiFetch('/tasks/' + activeTaskId)
        nextStatus = String(data?.status || '')
        nextError = String(data?.error || data?.message || '')
      } catch {
        nextStatus = ''
      }
      if (cancelled) return
      if (nextStatus && nextStatus !== activeTaskStatus) {
        setActiveTaskStatus(nextStatus)
      }
      if (nextStatus && isTerminalTaskStatus(nextStatus)) {
        if (nextStatus !== 'succeeded' && nextError) setTaskError(nextError)
        setReloadTick((value) => value + 1)
        return
      }
      timer = window.setTimeout(poll, TASK_POLL_INTERVAL_MS)
    }
    timer = window.setTimeout(poll, TASK_POLL_INTERVAL_MS)
    return () => {
      cancelled = true
      if (timer) window.clearTimeout(timer)
    }
  }, [activeTaskId, activeTaskStatus])

  const taskTerminal = activeTaskId !== '' && isTerminalTaskStatus(activeTaskStatus)
  const taskSucceeded = activeTaskStatus === 'succeeded'
  const failedStatusLabel =
    taskTerminal && !taskSucceeded
      ? (activeTaskStatus === 'failed'
          ? t('chatgptRebind.task.failed')
          : t('chatgptRebind.task.interrupted')) +
        ' (' + getTaskStatusText(activeTaskStatus, language) + ')'
      : ''

  const applySearch = () => {
    if (searchInput.trim() === appliedSearch.trim()) return
    setPage(1)
    setAppliedSearch(searchInput)
  }

  const toggleSelectAllPage = () => {
    setSelectedIds((current) => {
      const pageIds = items
        .map((item) => Number(item.id))
        .filter((id) => Number.isFinite(id))
      const allSelected = pageIds.length > 0 && pageIds.every((id) => current.has(id))
      const next = new Set(current)
      if (allSelected) {
        pageIds.forEach((id) => next.delete(id))
      } else {
        pageIds.forEach((id) => next.add(id))
      }
      return next
    })
  }

  const toggleSelectOne = (id: number) => {
    setSelectedIds((current) => {
      const next = new Set(current)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  const createRebindTask = async (payload: {
    account_id?: number
    ids?: number[]
  }) => {
    if (creating) return
    setCreating(true)
    setActionError('')
    setActiveTaskId('')
    setActiveTaskStatus('')
    setTaskError('')
    try {
      const data = await apiFetch('/tasks/email-rebind', {
        method: 'POST',
        body: JSON.stringify({
          platform: 'chatgpt',
          concurrency,
          ...payload,
        }),
      })
      const taskId = String(data?.task_id ?? data?.id ?? '')
      if (!taskId) throw new Error(t('chatgptRebind.task.createFailed'))
      // 创建成功：立即刷新选择与列表，并进入任务状态跟踪
      setSelectedIds(new Set())
      setReloadTick((value) => value + 1)
      setActiveTaskId(taskId)
      setActiveTaskStatus(String(data?.status || 'pending'))
    } catch (exc) {
      setActionError(errorText(exc, t('chatgptRebind.task.createFailed')))
    } finally {
      setCreating(false)
    }
  }

  const submitBatchRebind = () => {
    const ids = [...selectedIds]
    if (ids.length === 0) {
      setActionError(t('chatgptRebind.selectRequired'))
      return
    }
    createRebindTask({ ids })
  }

  // ---- 换绑邮箱配置 --------------------------------------------------------
  const loadMailConfig = async () => {
    setMailError('')
    setMailLoading(true)
    try {
      const data: MailConfigResponse = await apiFetch('/chatgpt-rebind/mail-config')
      const domains = Array.isArray(data.domains) ? data.domains : []
      setMailDomains(domains.join('\n'))
      setMailApiUrl(String(data.api_url || ''))
      setMailApiToken('')
      setMailCloudflareToken('')
      setMailCloudflareAccountId(String(data.cloudflare_account_id || ''))
      setMailForwardTo(String(data.forward_to || ''))
      setMaskedCloudMailToken(String(data.api_token_masked || ''))
      setMaskedCloudflareToken(String(data.cloudflare_api_token_masked || ''))
    } catch (exc) {
      setMailError(errorText(exc, t('chatgptRebind.mailConfig.loadFailed')))
    } finally {
      setMailLoading(false)
    }
  }

  const openMailConfig = () => {
    setMailSaved('')
    setMailOpen(true)
    loadMailConfig()
  }

  const saveMailConfig = async () => {
    if (mailSaving) return
    setMailSaving(true)
    setMailError('')
    setMailSaved('')
    try {
      await apiFetch('/chatgpt-rebind/mail-config', {
        method: 'PUT',
        body: JSON.stringify({
          domains: mailDomains
            .split(/\r?\n/)
            .map((line) => line.trim())
            .filter(Boolean),
          api_url: mailApiUrl.trim(),
          api_token: mailApiToken,
          cloudflare_api_token: mailCloudflareToken,
          cloudflare_account_id: mailCloudflareAccountId.trim(),
          forward_to: mailForwardTo.trim(),
        }),
      })
      setMailSaved(t('chatgptRebind.mailConfig.saved'))
      await loadMailConfig()
    } catch (exc) {
      setMailError(errorText(exc, t('chatgptRebind.mailConfig.saveFailed')))
    } finally {
      setMailSaving(false)
    }
  }

  const allPageSelected = useMemo(() => {
    const pageIds = items
      .map((item) => Number(item.id))
      .filter((id) => Number.isFinite(id))
    return pageIds.length > 0 && pageIds.every((id) => selectedIds.has(id))
  }, [items, selectedIds])

  const tokenHint = (masked: string) =>
    masked
      ? t('chatgptRebind.mailConfig.currentToken', { masked })
      : t('chatgptRebind.mailConfig.tokenKeepHint')

  return (
    <div className="space-y-4">
      {/* 页头工具栏 */}
      <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
        <div className="min-w-0">
          <h1 className="text-xl font-bold text-[var(--text-primary)]">
            {t('chatgptRebind.title')}
          </h1>
          <p className="mt-1 text-sm text-[var(--text-secondary)]">
            {t('chatgptRebind.subtitle')}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Button variant="outline" size="sm" onClick={openMailConfig}>
            <Forward className="mr-1.5 h-3.5 w-3.5" />
            {t('chatgptRebind.action.mailConfig')}
          </Button>
          <Button
            variant="outline"
            size="sm"
            onClick={() => setReloadTick((value) => value + 1)}
            disabled={loading}
          >
            <RefreshCw className={cn('mr-1.5 h-3.5 w-3.5', loading && 'animate-spin')} />
            {t('common.refresh')}
          </Button>
        </div>
      </div>

      {error && (
        <div className="flex items-start gap-2 rounded-xl border border-red-500/25 bg-red-500/10 px-4 py-3 text-sm text-red-700 dark:text-red-300">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
          <span className="min-w-0 break-words">{error}</span>
        </div>
      )}

      {/* 任务状态横幅：创建成功 / 执行中 / 终态成功失败 */}
      {activeTaskId && (
        <div
          className={cn(
            'flex items-start justify-between gap-3 rounded-xl border px-4 py-3 text-sm',
            taskTerminal
              ? taskSucceeded
                ? 'border-emerald-500/25 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300'
                : 'border-red-500/25 bg-red-500/10 text-red-700 dark:text-red-300'
              : 'border-[var(--border)] bg-[var(--bg-pane)] text-[var(--text-secondary)]',
          )}
        >
          <div className="flex min-w-0 items-start gap-2">
            {taskTerminal ? (
              taskSucceeded ? (
                <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0" />
              ) : (
                <XCircle className="mt-0.5 h-4 w-4 shrink-0" />
              )
            ) : (
              <Loader2 className="mt-0.5 h-4 w-4 shrink-0 animate-spin" />
            )}
            <div className="min-w-0">
              <div className="font-medium">
                {t('chatgptRebind.task.created', { taskId: activeTaskId })}
              </div>
              <div className="mt-0.5 break-all text-xs">
                {taskTerminal
                  ? taskSucceeded
                    ? t('chatgptRebind.task.succeeded')
                    : failedStatusLabel
                  : getTaskStatusText(activeTaskStatus || 'pending', language)}
              </div>
              {taskTerminal && !taskSucceeded && taskError && (
                <div className="mt-0.5 break-all text-xs opacity-80">{taskError}</div>
              )}
            </div>
          </div>
          <button
            onClick={() => {
              setActiveTaskId('')
              setActiveTaskStatus('')
              setTaskError('')
            }}
            className="shrink-0 rounded p-1 opacity-60 transition hover:opacity-100"
          >
            <X className="h-3.5 w-3.5" />
          </button>
        </div>
      )}

      {actionError && (
        <div className="flex items-start justify-between gap-3 rounded-xl border border-red-500/25 bg-red-500/10 px-4 py-3 text-sm text-red-700 dark:text-red-300">
          <div className="flex min-w-0 items-start gap-2">
            <XCircle className="mt-0.5 h-4 w-4 shrink-0" />
            <span className="min-w-0 break-words">{actionError}</span>
          </div>
          <button
            onClick={() => setActionError('')}
            className="shrink-0 rounded p-1 opacity-60 transition hover:opacity-100"
          >
            <X className="h-3.5 w-3.5" />
          </button>
        </div>
      )}

      <Card>
        <CardHeader className="gap-3">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <form
              className="relative w-full sm:w-72"
              onSubmit={(event) => {
                event.preventDefault()
                applySearch()
              }}
            >
              <Search className="pointer-events-none absolute left-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-[var(--text-muted)]" />
              <input
                value={searchInput}
                onChange={(event) => setSearchInput(event.target.value)}
                placeholder={t('chatgptRebind.searchPlaceholder')}
                className="control-surface h-9 w-full pl-9 pr-3"
              />
            </form>
            <div className="flex flex-wrap items-center gap-2">
              <label className="flex items-center gap-1.5 whitespace-nowrap text-xs text-[var(--text-secondary)]">
                {t('chatgptRebind.concurrency')}
                <select
                  value={concurrency}
                  onChange={(event) => setConcurrency(Number(event.target.value))}
                  className="control-surface h-8 w-16 py-0 text-xs"
                >
                  {CONCURRENCY_OPTIONS.map((value) => (
                    <option key={value} value={value}>
                      {value}
                    </option>
                  ))}
                </select>
              </label>
              <label className="flex cursor-pointer items-center gap-1.5 whitespace-nowrap text-xs text-[var(--text-secondary)]">
                <input
                  type="checkbox"
                  checked={allPageSelected}
                  onChange={toggleSelectAllPage}
                  disabled={items.length === 0}
                  className="h-4 w-4 cursor-pointer accent-[var(--accent)]"
                />
                {t('chatgptRebind.selectAllPage')}
              </label>
              <span className="whitespace-nowrap text-xs text-[var(--text-muted)]">
                {t('chatgptRebind.selectedCount', { count: selectedIds.size })}
              </span>
              <Button
                size="sm"
                onClick={submitBatchRebind}
                disabled={creating || selectedIds.size === 0}
                className="whitespace-nowrap"
              >
                {creating ? (
                  <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />
                ) : (
                  <Forward className="mr-1.5 h-3.5 w-3.5" />
                )}
                {t('chatgptRebind.action.batchRebind')}
              </Button>
            </div>
          </div>
        </CardHeader>
        <CardContent>
          <div className="overflow-x-auto">
            <table className="w-full min-w-[720px] text-left text-sm">
              <thead className="border-b border-[var(--border-soft)] text-xs uppercase tracking-[0.12em] text-[var(--text-muted)]">
                <tr>
                  <th className="w-10 px-3 py-2.5">
                    <input
                      type="checkbox"
                      checked={allPageSelected}
                      onChange={toggleSelectAllPage}
                      disabled={items.length === 0}
                      className="h-4 w-4 cursor-pointer accent-[var(--accent)]"
                    />
                  </th>
                  <th className="px-3 py-2.5">{t('chatgptRebind.col.id')}</th>
                  <th className="px-3 py-2.5">{t('chatgptRebind.col.currentEmail')}</th>
                  <th className="px-3 py-2.5">{t('chatgptRebind.col.status')}</th>
                  <th className="px-3 py-2.5">{t('chatgptRebind.col.registeredAt')}</th>
                  <th className="px-3 py-2.5">{t('common.actions')}</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-[var(--border-soft)]">
                {items.map((item) => {
                  const id = Number(item.id)
                  const selected = Number.isFinite(id) && selectedIds.has(id)
                  return (
                    <tr key={id} className={cn('transition', selected && 'bg-[var(--bg-hover)]')}>
                      <td className="px-3 py-2">
                        <input
                          type="checkbox"
                          checked={selected}
                          onChange={() => toggleSelectOne(id)}
                          className="h-4 w-4 cursor-pointer accent-[var(--accent)]"
                        />
                      </td>
                      <td className="px-3 py-2 font-mono text-xs text-[var(--text-muted)]">{id}</td>
                      <td className="max-w-[280px] px-3 py-2">
                        <span className="block truncate font-mono text-[13px] font-medium text-[var(--text-primary)]" title={accountEmail(item)}>
                          {accountEmail(item) || '-'}
                        </span>
                      </td>
                      <td className="px-3 py-2">
                        <Badge variant="default">
                          {translateAccountStatus(item.status || 'registered', language)}
                        </Badge>
                      </td>
                      <td className="whitespace-nowrap px-3 py-2 text-xs text-[var(--text-muted)]">
                        {formatTime(item.registered_at || item.created_at, language)}
                      </td>
                      <td className="px-3 py-2">
                        <button
                          onClick={() => createRebindTask({ account_id: id })}
                          disabled={creating}
                          className="table-action-btn"
                        >
                          {t('chatgptRebind.action.rebind')}
                        </button>
                      </td>
                    </tr>
                  )
                })}
                {!loading && items.length === 0 && !error && (
                  <tr>
                    <td colSpan={6} className="px-3 py-10 text-center text-sm text-[var(--text-muted)]">
                      {t('chatgptRebind.empty')}
                    </td>
                  </tr>
                )}
                {loading && items.length === 0 && (
                  <tr>
                    <td colSpan={6} className="px-3 py-10 text-center text-sm text-[var(--text-muted)]">
                      <Loader2 className="mr-2 inline h-4 w-4 animate-spin" />
                      {t('common.loading')}
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>

          {/* 分页 */}
          <div className="mt-3 flex flex-wrap items-center justify-between gap-2 border-t border-[var(--border-soft)] pt-3">
            <div className="flex flex-wrap items-center gap-2">
              <label className="flex items-center gap-1.5 whitespace-nowrap text-xs text-[var(--text-secondary)]">
                {t('chatgptRebind.pageSizeLabel')}
                <select
                  value={pageSize}
                  onChange={(event) => {
                    setPageSize(Number(event.target.value))
                    setPage(1)
                  }}
                  className="control-surface h-8 w-20 py-0 text-xs"
                >
                  {PAGE_SIZE_OPTIONS.map((value) => (
                    <option key={value} value={value}>
                      {value}
                    </option>
                  ))}
                </select>
              </label>
              <span className="whitespace-nowrap text-xs text-[var(--text-muted)]">
                {t('chatgptRebind.pageInfo', {
                  page,
                  pages: pageCount,
                  total,
                })}
              </span>
            </div>
            <div className="flex items-center gap-2">
              <Button
                variant="outline"
                size="sm"
                onClick={() => setPage((value) => Math.max(1, value - 1))}
                disabled={page <= 1 || loading}
              >
                {t('chatgptRebind.prevPage')}
              </Button>
              <Button
                variant="outline"
                size="sm"
                onClick={() => setPage((value) => Math.min(pageCount, value + 1))}
                disabled={page >= pageCount || loading}
              >
                {t('chatgptRebind.nextPage')}
              </Button>
            </div>
          </div>
        </CardContent>
      </Card>

      {/* 换绑邮箱管理弹窗 */}
      {mailOpen &&
        createPortal(
          <div
            className="dialog-backdrop"
            onClick={() => !mailSaving && setMailOpen(false)}
          >
            <div
              className="dialog-panel flex flex-col"
              style={{ width: 'min(680px, 94vw)' }}
              onClick={(event) => event.stopPropagation()}
            >
              <div className="flex items-start justify-between gap-3 border-b border-[var(--border)] px-6 py-4">
                <div className="min-w-0">
                  <h2 className="text-base font-semibold text-[var(--text-primary)]">
                    {t('chatgptRebind.mailConfig.title')}
                  </h2>
                  <p className="mt-1 text-xs text-[var(--text-muted)]">
                    {t('chatgptRebind.mailConfig.description')}
                  </p>
                </div>
                <button
                  onClick={() => !mailSaving && setMailOpen(false)}
                  className="shrink-0 text-[var(--text-muted)] hover:text-[var(--text-primary)]"
                >
                  <X className="h-4 w-4" />
                </button>
              </div>
              <div className="min-h-0 flex-1 space-y-4 overflow-y-auto px-6 py-4">
                {mailLoading ? (
                  <div className="py-8 text-center text-sm text-[var(--text-muted)]">
                    <Loader2 className="mr-2 inline h-4 w-4 animate-spin" />
                    {t('common.loading')}
                  </div>
                ) : (
                  <>
                    <label className="block text-xs font-medium text-[var(--text-secondary)]">
                      {t('chatgptRebind.mailConfig.domains')}
                      <textarea
                        value={mailDomains}
                        onChange={(event) => setMailDomains(event.target.value)}
                        rows={5}
                        spellCheck={false}
                        placeholder={t('chatgptRebind.mailConfig.domainsPlaceholder')}
                        className="control-surface control-surface-compact mt-1 w-full font-mono text-xs leading-relaxed"
                      />
                    </label>
                    <div className="grid gap-3 sm:grid-cols-2">
                      <label className="block text-xs font-medium text-[var(--text-secondary)]">
                        {t('chatgptRebind.mailConfig.apiUrl')}
                        <input
                          value={mailApiUrl}
                          onChange={(event) => setMailApiUrl(event.target.value)}
                          spellCheck={false}
                          className="control-surface control-surface-compact mt-1 w-full font-mono text-xs"
                        />
                      </label>
                      <label className="block text-xs font-medium text-[var(--text-secondary)]">
                        {t('chatgptRebind.mailConfig.forwardTo')}
                        <input
                          value={mailForwardTo}
                          onChange={(event) => setMailForwardTo(event.target.value)}
                          spellCheck={false}
                          className="control-surface control-surface-compact mt-1 w-full font-mono text-xs"
                        />
                      </label>
                      <label className="block text-xs font-medium text-[var(--text-secondary)]">
                        {t('chatgptRebind.mailConfig.apiToken')}
                        <input
                          type="password"
                          value={mailApiToken}
                          onChange={(event) => setMailApiToken(event.target.value)}
                          autoComplete="new-password"
                          className="control-surface control-surface-compact mt-1 w-full font-mono text-xs"
                        />
                        <span className="mt-1 block break-all text-[11px] font-normal text-[var(--text-muted)]">
                          {tokenHint(maskedCloudMailToken)}
                        </span>
                      </label>
                      <label className="block text-xs font-medium text-[var(--text-secondary)]">
                        {t('chatgptRebind.mailConfig.cloudflareApiToken')}
                        <input
                          type="password"
                          value={mailCloudflareToken}
                          onChange={(event) => setMailCloudflareToken(event.target.value)}
                          autoComplete="new-password"
                          className="control-surface control-surface-compact mt-1 w-full font-mono text-xs"
                        />
                        <span className="mt-1 block break-all text-[11px] font-normal text-[var(--text-muted)]">
                          {tokenHint(maskedCloudflareToken)}
                        </span>
                      </label>
                      <label className="block text-xs font-medium text-[var(--text-secondary)] sm:col-span-2">
                        {t('chatgptRebind.mailConfig.cloudflareAccountId')}
                        <input
                          value={mailCloudflareAccountId}
                          onChange={(event) => setMailCloudflareAccountId(event.target.value)}
                          spellCheck={false}
                          className="control-surface control-surface-compact mt-1 w-full font-mono text-xs"
                        />
                      </label>
                    </div>
                    {/* MX / Email Routing 自动配置：待接入（仅展示，不提供操作入口） */}
                    <div className="flex flex-wrap items-center gap-2 rounded-lg border border-[var(--border-soft)] bg-[var(--bg-pane)] px-3 py-2.5">
                      <Clock className="h-3.5 w-3.5 shrink-0 text-[var(--text-muted)]" />
                      <Badge variant="warning">
                        {t('chatgptRebind.mailConfig.mxPending')}
                      </Badge>
                      <Badge variant="warning">
                        {t('chatgptRebind.mailConfig.emailRoutingPending')}
                      </Badge>
                    </div>
                    {mailSaved && (
                      <div className="flex items-center gap-2 rounded-lg border border-emerald-500/25 bg-emerald-500/10 px-3 py-2 text-xs text-emerald-700 dark:text-emerald-300">
                        <CheckCircle2 className="h-3.5 w-3.5 shrink-0" />
                        {mailSaved}
                      </div>
                    )}
                    {mailError && (
                      <div className="flex items-start gap-2 rounded-lg border border-red-500/25 bg-red-500/10 px-3 py-2 text-xs text-red-700 dark:text-red-300">
                        <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                        <span className="min-w-0 break-words">{mailError}</span>
                      </div>
                    )}
                  </>
                )}
              </div>
              <div className="flex justify-end gap-2 border-t border-[var(--border)] px-6 py-3">
                <Button variant="outline" size="sm" onClick={() => setMailOpen(false)} disabled={mailSaving}>
                  {t('common.cancel')}
                </Button>
                <Button size="sm" onClick={saveMailConfig} disabled={mailLoading || mailSaving}>
                  {mailSaving && <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />}
                  {mailSaving ? t('common.saving') : t('common.save')}
                </Button>
              </div>
            </div>
          </div>,
          document.body,
        )}
    </div>
  )
}
