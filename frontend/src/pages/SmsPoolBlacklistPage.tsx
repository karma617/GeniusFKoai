import { useEffect, useState } from 'react'
import { apiFetch } from '@/lib/utils'
import { useI18n } from '@/lib/i18n-context'
import { formatDateTime, type TranslationKey } from '@/lib/i18n'
import { Card } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Ban, RefreshCw, Trash2, RotateCcw } from 'lucide-react'

type BlacklistItem = {
  phone: string
  relay_url: string
  reason: string
  error_code: string
  fail_count: number
  last_attempted_at?: string
  task_id?: string
  error_message?: string
}

const reasonKey = (reason: string): TranslationKey => {
  if (reason === 'oas_error') return 'smsPool.reason.oas_error'
  if (reason === 'manual') return 'smsPool.reason.manual'
  return 'smsPool.reason.manual'
}

export default function SmsPoolBlacklistPage() {
  const { t, language } = useI18n()
  const [items, setItems] = useState<BlacklistItem[]>([])
  const [loading, setLoading] = useState(false)
  const [busyPhone, setBusyPhone] = useState('')

  const load = async () => {
    setLoading(true)
    try {
      const data = await apiFetch('/sms-pool/blacklist')
      setItems(Array.isArray(data?.items) ? data.items : [])
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    load()
  }, [])

  const restore = async (phone: string) => {
    if (!window.confirm(t('smsPool.confirmRestore'))) return
    setBusyPhone(phone)
    try {
      await apiFetch(`/sms-pool/blacklist/${encodeURIComponent(phone)}`, { method: 'DELETE' })
      await load()
    } finally {
      setBusyPhone('')
    }
  }

  const clearAll = async () => {
    if (!window.confirm(t('smsPool.confirmClearAll'))) return
    setLoading(true)
    try {
      await apiFetch('/sms-pool/blacklist', { method: 'DELETE' })
      await load()
    } finally {
      setLoading(false)
    }
  }

  const formatIso = (value?: string) => {
    if (!value) return '-'
    return formatDateTime(value, language)
  }

  return (
    <div className="flex h-full flex-col gap-4 overflow-hidden p-6">
      <div className="flex items-center justify-between gap-4">
        <div className="flex items-center gap-3">
          <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-[var(--accent)]/10">
            <Ban className="h-5 w-5 text-[var(--accent)]" />
          </div>
          <div>
            <h1 className="text-lg font-semibold text-[var(--text-primary)]">
              {t('smsPool.blacklistTitle')}
            </h1>
            <p className="text-xs text-[var(--text-muted)]">
              {t('smsPool.blacklistSubtitle')}
            </p>
          </div>
        </div>
        <div className="flex gap-2">
          <Button variant="outline" size="sm" onClick={load} disabled={loading}>
            <RefreshCw className="mr-1.5 h-3.5 w-3.5" />
            {t('smsPool.refresh')}
          </Button>
          <Button variant="ghost" size="sm" onClick={clearAll} disabled={loading || items.length === 0}>
            <Trash2 className="mr-1.5 h-3.5 w-3.5" />
            {t('smsPool.action.clearAll')}
          </Button>
        </div>
      </div>

      <Card className="min-h-0 flex-1 overflow-hidden border border-[var(--border-soft)] p-0">
        {items.length === 0 ? (
          <div className="flex h-full min-h-[400px] flex-col items-center justify-center gap-3 text-center">
            <Ban className="h-10 w-10 text-[var(--text-muted)]" />
            <p className="text-sm text-[var(--text-muted)]">{t('smsPool.blacklistEmpty')}</p>
          </div>
        ) : (
          <div className="h-full overflow-auto">
            <table className="w-full text-[13px]">
              <thead className="sticky top-0 z-10 bg-[var(--bg-pane)]/95 backdrop-blur-sm">
                <tr className="border-b border-[var(--border)]">
                  <th className="px-3 py-2.5 text-left font-medium text-[var(--text-muted)]">
                    {t('smsPool.col.index')}
                  </th>
                  <th className="px-3 py-2.5 text-left font-medium text-[var(--text-muted)]">
                    {t('smsPool.col.phone')}
                  </th>
                  <th className="px-3 py-2.5 text-left font-medium text-[var(--text-muted)]">
                    {t('smsPool.col.reason')}
                  </th>
                  <th className="px-3 py-2.5 text-left font-medium text-[var(--text-muted)]">
                    {t('smsPool.col.errorCode')}
                  </th>
                  <th className="px-3 py-2.5 text-left font-medium text-[var(--text-muted)]">
                    {t('smsPool.col.failCount')}
                  </th>
                  <th className="px-3 py-2.5 text-left font-medium text-[var(--text-muted)]">
                    {t('smsPool.col.lastAttemptedAt')}
                  </th>
                  <th className="px-3 py-2.5 text-left font-medium text-[var(--text-muted)]">
                    {t('smsPool.col.taskId')}
                  </th>
                  <th className="px-3 py-2.5 text-right font-medium text-[var(--text-muted)]">
                    {t('smsPool.col.actions')}
                  </th>
                </tr>
              </thead>
              <tbody>
                {items.map((item, idx) => (
                  <tr
                    key={item.phone}
                    className="border-b border-[var(--border-soft)] transition-colors hover:bg-[var(--bg-hover)]"
                  >
                    <td className="px-3 py-3 text-[var(--text-muted)]">{idx + 1}</td>
                    <td className="px-3 py-3">
                      <code className="rounded bg-[var(--chip-bg)] px-1.5 py-0.5 font-mono text-xs text-[var(--text-primary)]">
                        {item.phone}
                      </code>
                    </td>
                    <td className="px-3 py-3">
                      <Badge variant="secondary">{t(reasonKey(item.reason))}</Badge>
                    </td>
                    <td className="px-3 py-3">
                      {item.error_code ? (
                        <code className="text-xs text-[var(--text-muted)]">{item.error_code}</code>
                      ) : (
                        <span className="text-[var(--text-muted)]">-</span>
                      )}
                    </td>
                    <td className="px-3 py-3 text-[var(--text-secondary)]">{item.fail_count || 0}</td>
                    <td className="px-3 py-3 text-xs text-[var(--text-muted)]">
                      {formatIso(item.last_attempted_at)}
                    </td>
                    <td className="px-3 py-3">
                      {item.task_id ? (
                        <span className="text-xs text-[var(--text-secondary)]">{item.task_id}</span>
                      ) : (
                        <span className="text-[var(--text-muted)]">-</span>
                      )}
                    </td>
                    <td className="px-3 py-3 text-right">
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={() => restore(item.phone)}
                        disabled={busyPhone === item.phone}
                      >
                        <RotateCcw className="mr-1 h-3.5 w-3.5" />
                        {t('smsPool.action.restore')}
                      </Button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  )
}
