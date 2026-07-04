import { useEffect, useMemo, useState } from 'react'
import { AlertTriangle, CheckCircle2, MailCheck, RefreshCw, Search } from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { apiFetch, cn } from '@/lib/utils'

type UsageItem = {
  parent_email: string
  configured: boolean
  alias_limit: number
  successful_alias_count: number
  allocated_only_count: number
  confirmed_remaining: number
  conservative_remaining: number
  main_registered: boolean
  email_status: 'usable' | 'unusable' | 'registered' | string
  email_status_reason: string
  status: 'available' | 'has_unconfirmed' | 'full' | string
  successful_aliases: string[]
  allocated_only_aliases: string[]
  first_seen_at: string
  last_seen_at: string
}

type UsageResponse = {
  alias_limit: number
  config_pool_recorded: boolean
  summary: {
    parent_count: number
    configured_parent_count: number
    usable_parent_count: number
    unusable_parent_count: number
    registered_parent_count: number
    successful_alias_count: number
    allocated_only_count: number
    confirmed_remaining: number
    conservative_remaining: number
    full_parent_count: number
    unconfirmed_parent_count: number
  }
  items: UsageItem[]
}

function formatDate(value: string) {
  if (!value) return '-'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleString()
}

function statusBadge(item: UsageItem) {
  if (item.email_status === 'unusable') return <Badge variant="danger">不可用</Badge>
  if (item.email_status === 'registered') return <Badge variant="warning">主邮箱已注册</Badge>
  if (item.status === 'full') return <Badge variant="danger">已满</Badge>
  if (item.status === 'has_unconfirmed') return <Badge variant="warning">有未确认分配</Badge>
  return <Badge variant="success">可用</Badge>
}

function aliasList(items: string[], empty: string) {
  if (!items.length) return <span className="text-xs text-[var(--text-muted)]">{empty}</span>
  return (
    <div className="flex flex-wrap gap-1.5">
      {items.map((email) => (
        <span
          key={email}
          className="rounded-full border border-[var(--border-soft)] bg-[var(--bg-pane)] px-2 py-0.5 font-mono text-[11px] text-[var(--text-secondary)]"
        >
          {email}
        </span>
      ))}
    </div>
  )
}

export default function GmailApiCodeUsage() {
  const [data, setData] = useState<UsageResponse | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [search, setSearch] = useState('')
  const [status, setStatus] = useState('all')

  const load = async () => {
    setLoading(true)
    setError('')
    try {
      const result = await apiFetch('/stats/gmail-api-code-alias-usage')
      setData(result)
    } catch (exc: any) {
      setError(exc?.message || '加载 Gmail API接码统计失败')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    load()
  }, [])

  const items = useMemo(() => {
    const keyword = search.trim().toLowerCase()
    return (data?.items || []).filter((item) => {
      if (status === 'unusable' || status === 'registered') {
        if (item.email_status !== status) return false
      } else if (status !== 'all' && item.status !== status) {
        return false
      }
      if (!keyword) return true
      return (
        item.parent_email.includes(keyword)
        || item.successful_aliases.some((email) => email.includes(keyword))
        || item.allocated_only_aliases.some((email) => email.includes(keyword))
      )
    })
  }, [data, search, status])

  const summary = data?.summary
  const statCards = [
    {
      label: '母邮箱数',
      value: summary?.parent_count ?? '-',
      description: '当前邮箱池和历史记录里出现过的 Gmail 主邮箱总数。',
      icon: MailCheck,
      tone: 'text-[var(--accent)]',
    },
    {
      label: '已成功别名',
      value: summary?.successful_alias_count ?? 0,
      description: '本地账号表和 gmail_api_code 邮箱资源里已落库的 alias。',
      icon: CheckCircle2,
      tone: 'text-emerald-500',
    },
    {
      label: '不可用母邮箱',
      value: summary?.unusable_parent_count ?? 0,
      description: '已被标记为 invalid 的主邮箱，后续注册会直接跳过。',
      icon: AlertTriangle,
      tone: 'text-red-500',
    },
    {
      label: '未确认分配',
      value: summary?.allocated_only_count ?? 0,
      description: '任务日志里分配过，但本地没有成功账号的 alias。',
      icon: AlertTriangle,
      tone: 'text-amber-500',
    },
    {
      label: '确认剩余额度',
      value: summary?.confirmed_remaining ?? 0,
      description: '只扣已成功 alias，不扣未确认分配。',
      icon: MailCheck,
      tone: 'text-emerald-500',
    },
    {
      label: '保守剩余额度',
      value: summary?.conservative_remaining ?? 0,
      description: '扣已成功 alias，也扣未确认分配。',
      icon: MailCheck,
      tone: 'text-sky-500',
    },
  ]

  return (
    <div className="space-y-5">
      <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
        <div>
          <h1 className="text-xl font-bold text-[var(--text-primary)]">Gmail API接码邮箱池</h1>
          <p className="mt-1 text-sm text-[var(--text-secondary)]">
            按母邮箱统计已成功注册的 alias、任务日志中已分配但未成功落库的 alias，以及剩余额度。
          </p>
        </div>
        <Button variant="outline" size="sm" onClick={load} disabled={loading}>
          <RefreshCw className={cn('mr-1.5 h-3.5 w-3.5', loading && 'animate-spin')} />
          刷新
        </Button>
      </div>

      {data && !data.config_pool_recorded && (
        <div className="rounded-xl border border-amber-500/25 bg-amber-500/10 px-4 py-3 text-sm text-amber-700 dark:text-amber-300">
          当前 Gmail API接码邮箱池配置文本没有在配置表里记录；页面仍会基于历史账号和任务日志展示已出现过的母邮箱。
        </div>
      )}
      {error && (
        <div className="rounded-xl border border-red-500/25 bg-red-500/10 px-4 py-3 text-sm text-red-700 dark:text-red-300">
          {error}
        </div>
      )}

      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-6">
        {statCards.map(({ label, value, description, icon: Icon, tone }) => (
          <Card key={label}>
            <div className="flex items-center justify-between gap-3">
              <div className="min-w-0">
                <div className="text-[11px] uppercase tracking-[0.16em] text-[var(--text-muted)]">{label}</div>
                <div className="mt-1 text-xl font-semibold text-[var(--text-primary)]">{value}</div>
                <div className="mt-2 text-xs leading-5 text-[var(--text-muted)]">{description}</div>
              </div>
              <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg bg-[var(--gradient-accent-soft)]">
                <Icon className={cn('h-4.5 w-4.5', tone)} />
              </div>
            </div>
          </Card>
        ))}
      </div>

      <Card>
        <CardHeader className="gap-3 lg:flex-row lg:items-center lg:justify-between">
          <div>
            <CardTitle>母邮箱用量</CardTitle>
            <p className="mt-1 text-xs text-[var(--text-muted)]">
              确认剩余只扣成功落库；保守剩余会同时扣除未确认分配。
            </p>
          </div>
          <div className="flex flex-col gap-2 sm:flex-row">
            <label className="relative block">
              <Search className="pointer-events-none absolute left-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-[var(--text-muted)]" />
              <input
                value={search}
                onChange={(event) => setSearch(event.target.value)}
                placeholder="搜索母邮箱或别名"
                className="h-9 w-full rounded-lg border border-[var(--border)] bg-[var(--bg-pane)] pl-9 pr-3 text-sm text-[var(--text-primary)] outline-none transition focus:border-[var(--accent-edge)] sm:w-72"
              />
            </label>
            <select
              value={status}
              onChange={(event) => setStatus(event.target.value)}
              className="h-9 rounded-lg border border-[var(--border)] bg-[var(--bg-pane)] px-3 text-sm text-[var(--text-primary)] outline-none transition focus:border-[var(--accent-edge)]"
            >
              <option value="all">全部状态</option>
              <option value="available">可用</option>
              <option value="unusable">不可用</option>
              <option value="registered">主邮箱已注册</option>
              <option value="has_unconfirmed">有未确认分配</option>
              <option value="full">已满</option>
            </select>
          </div>
        </CardHeader>
        <CardContent>
          <div className="overflow-x-auto">
            <table className="w-full min-w-[1080px] text-left text-sm">
              <thead className="border-b border-[var(--border-soft)] text-xs uppercase tracking-[0.12em] text-[var(--text-muted)]">
                <tr>
                  <th className="px-3 py-3">母邮箱</th>
                  <th className="px-3 py-3">状态</th>
                  <th className="px-3 py-3">成功</th>
                  <th className="px-3 py-3">未确认</th>
                  <th className="px-3 py-3">确认剩余</th>
                  <th className="px-3 py-3">保守剩余</th>
                  <th className="px-3 py-3">最后记录</th>
                  <th className="px-3 py-3">alias 明细</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-[var(--border-soft)]">
                {items.map((item) => (
                  <tr key={item.parent_email} className="align-top transition hover:bg-[var(--bg-hover)]">
                    <td className="px-3 py-3">
                      <div className="font-mono text-sm font-semibold text-[var(--text-primary)]">{item.parent_email}</div>
                      <div className="mt-1 flex gap-1.5">
                        {item.configured ? <Badge variant="default">当前池</Badge> : <Badge variant="secondary">历史记录</Badge>}
                        {item.main_registered && <Badge variant="warning">母邮箱已注册</Badge>}
                      </div>
                    </td>
                    <td className="px-3 py-3">
                      <div className="space-y-1">
                        {statusBadge(item)}
                        {item.email_status_reason && item.email_status !== 'usable' && (
                          <div className="max-w-40 break-words text-[11px] text-[var(--text-muted)]">
                            {item.email_status_reason}
                          </div>
                        )}
                      </div>
                    </td>
                    <td className="px-3 py-3 font-semibold text-[var(--text-primary)]">{item.successful_alias_count}/{item.alias_limit}</td>
                    <td className="px-3 py-3 font-semibold text-amber-600 dark:text-amber-300">{item.allocated_only_count}</td>
                    <td className="px-3 py-3 font-semibold text-emerald-600 dark:text-emerald-300">{item.confirmed_remaining}</td>
                    <td className="px-3 py-3 font-semibold text-sky-600 dark:text-sky-300">{item.conservative_remaining}</td>
                    <td className="px-3 py-3 text-xs text-[var(--text-muted)]">{formatDate(item.last_seen_at)}</td>
                    <td className="max-w-[460px] px-3 py-3">
                      <div className="space-y-2">
                        <div>
                          <div className="mb-1 text-[11px] font-semibold text-[var(--text-muted)]">已成功</div>
                          {aliasList(item.successful_aliases, '暂无成功 alias')}
                        </div>
                        <div>
                          <div className="mb-1 text-[11px] font-semibold text-[var(--text-muted)]">已分配未成功落库</div>
                          {aliasList(item.allocated_only_aliases, '暂无未确认分配')}
                        </div>
                      </div>
                    </td>
                  </tr>
                ))}
                {!loading && items.length === 0 && (
                  <tr>
                    <td colSpan={8} className="px-3 py-10 text-center text-sm text-[var(--text-muted)]">
                      暂无 Gmail API接码 alias 统计记录。
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </CardContent>
      </Card>
    </div>
  )
}
