import { useEffect, useState } from 'react'
import { apiFetch } from '@/lib/utils'
import { useI18n } from '@/lib/i18n-context'
import { formatDateTime, type TranslationKey } from '@/lib/i18n'
import { Card } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import {
  Activity,
  Clock3,
  FileText,
  History,
  Loader2,
  RefreshCw,
  RotateCcw,
  Trash2,
  XCircle,
} from 'lucide-react'

type ReleaseQueueItem = {
  provider: string
  order_id: string
  phone: string
  base_url: string
  api_key_masked: string
  reason: string
  attempts: number
  next_attempt_at: number
  next_attempt_at_iso: string
  wait_seconds: number
  last_response_preview: string
  status: string
  created_at_iso: string
  updated_at_iso: string
}

type ReleaseLogItem = {
  ts: number
  ts_iso: string
  provider: string
  status: string
  level: string
  order_id: string
  phone: string
  attempt: number
  next_attempt_at_iso: string
  reason: string
  response_preview: string
  message: string
}

type ReleaseQueuePayload = {
  items?: ReleaseQueueItem[]
  logs?: ReleaseLogItem[]
  total?: number
  pending?: number
  succeeded_recent?: number
  failed_recent?: number
  queue_path?: string
  log_path?: string
}

const statusVariant = (status: string): 'default' | 'success' | 'warning' | 'danger' | 'secondary' => {
  if (status === 'success') return 'success'
  if (status === 'failed') return 'danger'
  if (status === 'retrying') return 'warning'
  if (status === 'queued') return 'default'
  return 'secondary'
}

const statusKey = (status: string): TranslationKey => {
  if (status === 'success') return 'smsPool.status.success'
  if (status === 'failed') return 'smsPool.status.failed'
  if (status === 'retrying') return 'smsPool.status.retrying'
  if (status === 'waiting') return 'smsPool.status.waiting'
  if (status === 'removed') return 'smsPool.status.removed'
  if (status === 'queued') return 'smsPool.status.queued'
  return 'smsPool.status.unknown'
}

const providerLabel = (provider: string) => {
  if (provider === 'smspool') return 'SMSPool'
  return provider || '-'
}

export default function SmsPoolBlacklist() {
  const { t, language } = useI18n()
  const [items, setItems] = useState<ReleaseQueueItem[]>([])
  const [logs, setLogs] = useState<ReleaseLogItem[]>([])
  const [stats, setStats] = useState<ReleaseQueuePayload>({})
  const [loading, setLoading] = useState(false)
  const [processing, setProcessing] = useState(false)
  const [clearingLogs, setClearingLogs] = useState(false)
  const [busyOrderId, setBusyOrderId] = useState('')

  const applyPayload = (data: ReleaseQueuePayload) => {
    setItems(Array.isArray(data?.items) ? data.items : [])
    setLogs(Array.isArray(data?.logs) ? data.logs : [])
    setStats(data || {})
  }

  const load = async (silent = false) => {
    if (!silent) setLoading(true)
    try {
      const data = await apiFetch('/sms-pool/release-queue')
      applyPayload(data)
    } finally {
      if (!silent) setLoading(false)
    }
  }

  useEffect(() => {
    load()
    const timer = window.setInterval(() => {
      load(true).catch(() => {})
    }, 5000)
    return () => window.clearInterval(timer)
  }, [])

  const processAll = async () => {
    setProcessing(true)
    try {
      const data = await apiFetch('/sms-pool/release-queue/process', { method: 'POST' })
      applyPayload(data)
    } finally {
      setProcessing(false)
    }
  }

  const processOne = async (orderId: string) => {
    setBusyOrderId(orderId)
    try {
      const data = await apiFetch(`/sms-pool/release-queue/${encodeURIComponent(orderId)}/process`, {
        method: 'POST',
      })
      applyPayload(data)
    } finally {
      setBusyOrderId('')
    }
  }

  const removeOne = async (orderId: string) => {
    if (!window.confirm(t('smsPool.confirmRemoveRelease'))) return
    setBusyOrderId(orderId)
    try {
      const data = await apiFetch(`/sms-pool/release-queue/${encodeURIComponent(orderId)}`, {
        method: 'DELETE',
      })
      applyPayload(data)
    } finally {
      setBusyOrderId('')
    }
  }

  const clearLogs = async () => {
    if (!window.confirm(t('smsPool.confirmClearReleaseLogs'))) return
    setClearingLogs(true)
    try {
      const data = await apiFetch('/sms-pool/release-logs', { method: 'DELETE' })
      applyPayload(data)
    } finally {
      setClearingLogs(false)
    }
  }

  const formatIso = (value?: string) => {
    if (!value) return '-'
    return formatDateTime(value, language)
  }

  const renderStatus = (status: string) => (
    <Badge variant={statusVariant(status)}>{t(statusKey(status))}</Badge>
  )

  const renderWait = (item: ReleaseQueueItem) => {
    if (!item.wait_seconds) return t('smsPool.readyNow')
    return t('smsPool.retryIn', { seconds: item.wait_seconds })
  }

  return (
    <div className="space-y-4">
      <Card className="overflow-hidden border border-[var(--border-soft)] bg-[var(--bg-card)] p-0">
        <div className="relative overflow-hidden px-5 py-4">
          <div className="absolute inset-y-0 right-0 w-1/3 bg-[radial-gradient(circle_at_top_right,rgba(var(--accent-rgb),0.18),transparent_60%)]" />
          <div className="relative flex flex-col gap-4 lg:flex-row lg:items-center lg:justify-between">
            <div className="min-w-0">
              <div className="flex flex-wrap items-center gap-2">
                <div className="flex h-9 w-9 items-center justify-center rounded-xl border border-[var(--border-soft)] bg-[var(--chip-bg)] text-[var(--accent)]">
                  <Activity className="h-4 w-4" />
                </div>
                <div className="text-base font-semibold text-[var(--text-primary)]">
                  {t('smsPool.title')}
                </div>
                <Badge variant="default">
                  {t('common.total')} {stats.total ?? items.length}
                </Badge>
                <Badge variant="warning">
                  {t('smsPool.stat.pending')} {stats.pending ?? items.length}
                </Badge>
              </div>
              <p className="mt-2 max-w-3xl text-xs leading-5 text-[var(--text-muted)]">
                {t('smsPool.subtitle')}
              </p>
              <div className="mt-3 flex flex-wrap gap-2 text-[11px] text-[var(--text-muted)]">
                <span className="rounded-md border border-[var(--border-soft)] bg-[var(--chip-bg)] px-2 py-1">
                  {t('smsPool.stat.successRecent')}: {stats.succeeded_recent ?? 0}
                </span>
                <span className="rounded-md border border-[var(--border-soft)] bg-[var(--chip-bg)] px-2 py-1">
                  {t('smsPool.stat.failedRecent')}: {stats.failed_recent ?? 0}
                </span>
                <span className="rounded-md border border-[var(--border-soft)] bg-[var(--chip-bg)] px-2 py-1">
                  {t('smsPool.pollingHint')}
                </span>
              </div>
            </div>
            <div className="flex flex-wrap items-center gap-2">
              <Button variant="outline" size="sm" onClick={() => load()} disabled={loading}>
                <RefreshCw className={`mr-1.5 h-4 w-4 ${loading ? 'animate-spin' : ''}`} />
                {t('smsPool.refresh')}
              </Button>
              <Button variant="outline" size="sm" onClick={processAll} disabled={processing || items.length === 0}>
                {processing ? <Loader2 className="mr-1.5 h-4 w-4 animate-spin" /> : <RotateCcw className="mr-1.5 h-4 w-4" />}
                {t('smsPool.action.processAll')}
              </Button>
              <Button variant="outline" size="sm" onClick={clearLogs} disabled={clearingLogs || logs.length === 0}>
                <Trash2 className="mr-1.5 h-4 w-4" />
                {t('smsPool.action.clearLogs')}
              </Button>
            </div>
          </div>
        </div>
      </Card>

      <div className="grid min-h-[620px] gap-4 xl:grid-cols-[minmax(0,1fr)_380px]">
        <Card className="flex min-w-0 flex-col overflow-hidden border border-[var(--border-soft)] p-0">
          <div className="flex items-center justify-between gap-3 border-b border-[var(--border)] px-4 py-3">
            <div>
              <div className="text-sm font-semibold text-[var(--text-primary)]">
                {t('smsPool.queueTitle')}
              </div>
              <div className="mt-1 text-xs text-[var(--text-muted)]">
                {t('smsPool.queueSubtitle')}
              </div>
            </div>
            <Badge variant="secondary">{items.length}</Badge>
          </div>

          {items.length === 0 ? (
            <div className="flex flex-1 flex-col items-center justify-center gap-3 p-10 text-center">
              <div className="flex h-12 w-12 items-center justify-center rounded-2xl border border-[var(--border-soft)] bg-[var(--chip-bg)] text-[var(--text-muted)]">
                <Clock3 className="h-5 w-5" />
              </div>
              <p className="max-w-md text-sm text-[var(--text-muted)]">{t('smsPool.empty')}</p>
            </div>
          ) : (
            <div className="min-h-0 flex-1 overflow-auto">
              <table className="w-full min-w-[980px] text-sm">
                <thead className="sticky top-0 z-10 border-b border-[var(--border)] bg-[var(--bg-pane)]/95 backdrop-blur">
                  <tr className="text-left text-xs uppercase tracking-wide text-[var(--text-muted)]">
                    <th className="px-3 py-2 font-medium">{t('smsPool.col.index')}</th>
                    <th className="px-3 py-2 font-medium">{t('smsPool.col.provider')}</th>
                    <th className="px-3 py-2 font-medium">{t('smsPool.col.phone')}</th>
                    <th className="px-3 py-2 text-right font-medium">{t('smsPool.col.attempts')}</th>
                    <th className="px-3 py-2 font-medium">{t('smsPool.col.releaseStatus')}</th>
                    <th className="px-3 py-2 font-medium">{t('smsPool.col.nextRetry')}</th>
                    <th className="px-3 py-2 font-medium">{t('smsPool.col.releaseResponse')}</th>
                    <th className="px-3 py-2 text-right font-medium">{t('smsPool.col.actions')}</th>
                  </tr>
                </thead>
                <tbody>
                  {items.map((item, index) => (
                    <tr key={item.order_id || `${item.phone}-${index}`} className="border-b border-[var(--border)]/60 hover:bg-[var(--bg-hover)]">
                      <td className="px-3 py-3 text-xs tabular-nums text-[var(--text-muted)]">{index + 1}</td>
                      <td className="px-3 py-3">
                        <div className="font-medium text-[var(--text-primary)]">{providerLabel(item.provider)}</div>
                        <div className="mt-1 max-w-[180px] truncate text-[11px] text-[var(--text-muted)]" title={item.base_url}>
                          {item.base_url || '-'}
                        </div>
                      </td>
                      <td className="px-3 py-3">
                        <div className="font-mono text-[var(--text-primary)]">{item.phone || '-'}</div>
                        <div className="mt-1 max-w-[180px] truncate font-mono text-[11px] text-[var(--text-muted)]" title={item.order_id}>
                          {item.order_id || '-'}
                        </div>
                      </td>
                      <td className="px-3 py-3 text-right tabular-nums text-[var(--text-secondary)]">{item.attempts ?? 0}</td>
                      <td className="px-3 py-3">{renderStatus(item.status)}</td>
                      <td className="px-3 py-3">
                        <div className="text-xs text-[var(--text-secondary)]">{renderWait(item)}</div>
                        <div className="mt-1 text-[11px] text-[var(--text-muted)]">{formatIso(item.next_attempt_at_iso)}</div>
                      </td>
                      <td className="px-3 py-3">
                        <div className="max-w-[280px] rounded-md border border-[var(--border-soft)] bg-[var(--chip-bg)] px-2 py-1.5 font-mono text-[11px] leading-5 text-[var(--text-secondary)]">
                          <span className="line-clamp-2 break-all">{item.last_response_preview || '-'}</span>
                        </div>
                      </td>
                      <td className="px-3 py-3">
                        <div className="flex justify-end gap-2">
                          <Button
                            variant="outline"
                            size="sm"
                            onClick={() => processOne(item.order_id)}
                            disabled={!item.order_id || busyOrderId === item.order_id}
                          >
                            {busyOrderId === item.order_id ? (
                              <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" />
                            ) : (
                              <RotateCcw className="mr-1 h-3.5 w-3.5" />
                            )}
                            {t('smsPool.action.processOne')}
                          </Button>
                          <Button
                            variant="ghost"
                            size="sm"
                            onClick={() => removeOne(item.order_id)}
                            disabled={!item.order_id || busyOrderId === item.order_id}
                          >
                            <XCircle className="mr-1 h-3.5 w-3.5" />
                            {t('smsPool.action.remove')}
                          </Button>
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>

        <Card className="flex min-h-0 flex-col overflow-hidden border border-[var(--border-soft)] p-0">
          <div className="flex items-center justify-between gap-3 border-b border-[var(--border)] px-4 py-3">
            <div className="flex items-center gap-2">
              <History className="h-4 w-4 text-[var(--accent)]" />
              <div className="text-sm font-semibold text-[var(--text-primary)]">
                {t('smsPool.logsTitle')}
              </div>
            </div>
            <Badge variant="secondary">{logs.length}</Badge>
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto bg-[var(--bg-pane)]/25 p-3">
            {logs.length === 0 ? (
              <div className="flex h-full min-h-[260px] flex-col items-center justify-center gap-3 text-center">
                <FileText className="h-8 w-8 text-[var(--text-muted)]" />
                <p className="text-sm text-[var(--text-muted)]">{t('smsPool.logsEmpty')}</p>
              </div>
            ) : (
              <div className="space-y-2">
                {logs.map((item, index) => (
                  <div key={`${item.ts}-${item.order_id}-${index}`} className="rounded-xl border border-[var(--border-soft)] bg-[var(--bg-card)] p-3 shadow-sm">
                    <div className="flex items-start justify-between gap-2">
                      <div className="flex min-w-0 items-center gap-2">
                        {renderStatus(item.status)}
                        <span className="truncate font-mono text-[11px] text-[var(--text-muted)]">
                          {item.phone || item.order_id || '-'}
                        </span>
                      </div>
                      <span className="shrink-0 text-[11px] text-[var(--text-muted)]">
                        {formatIso(item.ts_iso)}
                      </span>
                    </div>
                    <div className="mt-2 text-xs leading-5 text-[var(--text-secondary)]">
                      {item.message || '-'}
                    </div>
                    {item.response_preview ? (
                      <div className="mt-2 rounded-md border border-[var(--border-soft)] bg-[var(--chip-bg)] px-2 py-1.5 font-mono text-[11px] leading-5 text-[var(--text-muted)] break-all">
                        {item.response_preview}
                      </div>
                    ) : null}
                    <div className="mt-2 flex flex-wrap gap-2 text-[11px] text-[var(--text-muted)]">
                      <span>{providerLabel(item.provider)}</span>
                      <span>order={item.order_id || '-'}</span>
                      <span>attempt={item.attempt || 0}</span>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        </Card>
      </div>
    </div>
  )
}
