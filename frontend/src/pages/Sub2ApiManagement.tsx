import { useEffect, useMemo, useState } from 'react'
import { Activity, AlertTriangle, RefreshCw, RotateCw, Search } from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { apiFetch, cn } from '@/lib/utils'

type Sub2ApiGroup = {
  id: number
  name: string
  platform: string
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
}

type ActionResult = {
  account_id: string
  status?: string
  result?: string
  message?: string
  reason?: string
  marked_error?: boolean
}

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

export default function Sub2ApiManagement() {
  const [groups, setGroups] = useState<Sub2ApiGroup[]>([])
  const [accounts, setAccounts] = useState<Sub2ApiAccount[]>([])
  const [selectedIds, setSelectedIds] = useState<Set<string>>(() => new Set())
  const [groupId, setGroupId] = useState('')
  const [status, setStatus] = useState('all')
  const [search, setSearch] = useState('')
  const [workspaceIds, setWorkspaceIds] = useState('')
  const [loading, setLoading] = useState(false)
  const [checking, setChecking] = useState(false)
  const [relogining, setRelogining] = useState(false)
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')
  const [lastResults, setLastResults] = useState<ActionResult[]>([])

  const load = async () => {
    setLoading(true)
    setError('')
    try {
      const params = new URLSearchParams()
      if (groupId) params.set('group_id', groupId)
      if (status && status !== 'all') params.set('status', status)
      if (search.trim()) params.set('search', search.trim())
      const data = await apiFetch(`/sub2api-management/inventory?${params}`)
      setGroups(data.groups || [])
      setAccounts(data.accounts || [])
      setSelectedIds(new Set())
    } catch (err: any) {
      setError(err?.message || String(err))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    load()
  }, [groupId, status])

  const selectedOrVisibleIds = useMemo(() => {
    if (selectedIds.size > 0) return Array.from(selectedIds)
    return accounts.map((item) => item.id).filter(Boolean)
  }, [accounts, selectedIds])

  const stats = useMemo(() => {
    const total = accounts.length
    const active = accounts.filter((item) => statusVariant(item.status) === 'success').length
    const errorCount = accounts.filter((item) => statusVariant(item.status) === 'danger').length
    const k12 = accounts.filter((item) => String(item.plan_type || '').toLowerCase() === 'k12').length
    return { total, active, errorCount, k12 }
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
      if (current.size === accounts.length) return new Set()
      return new Set(accounts.map((item) => item.id).filter(Boolean))
    })
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
    try {
      const data = await apiFetch('/sub2api-management/bulk-check', {
        method: 'POST',
        body: JSON.stringify({ account_ids: selectedOrVisibleIds, concurrency: 10 }),
      })
      const summary = data.summary || {}
      setLastResults(data.results || [])
      setMessage(`测活完成：正常 ${summary.ok || 0}，异常 ${summary.failed || 0}，已标错误 ${summary.marked_error || 0}，跳过 ${summary.skipped || 0}`)
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
    try {
      const data = await apiFetch('/sub2api-management/relogin-errors', {
        method: 'POST',
        body: JSON.stringify({
          account_ids: selectedIds.size > 0 ? Array.from(selectedIds) : [],
          group_id: groupId ? Number(groupId) : null,
          workspace_ids: workspaceIds,
          concurrency: 2,
        }),
      })
      const summary = data.summary || {}
      setLastResults(data.results || [])
      setMessage(`重新登录完成：K12替换 ${summary.replaced || 0}，封禁删除 ${summary.deleted || 0}，手机跳过 ${summary.phone_skipped || 0}，free跳过 ${summary.free_skipped || 0}，失败 ${summary.failed || 0}`)
      await load()
    } catch (err: any) {
      setError(err?.message || String(err))
    } finally {
      setRelogining(false)
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
          <h1 className="text-xl font-semibold text-[var(--text-primary)]">Sub2Api管理</h1>
          <p className="mt-1 text-sm text-[var(--text-muted)]">
            使用设置页的 Sub2API 后台信息读取远端分组和账号，支持按分组筛选、批量测活和错误账号处理。
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button variant="outline" onClick={load} disabled={loading || checking || relogining}>
            <RefreshCw className={cn('mr-1.5 h-3.5 w-3.5', loading && 'animate-spin')} />
            刷新
          </Button>
          <Button onClick={runBulkCheck} disabled={checking || loading || relogining || selectedOrVisibleIds.length === 0}>
            <Activity className={cn('mr-1.5 h-3.5 w-3.5', checking && 'animate-pulse')} />
            批量测活
          </Button>
          <Button variant="outline" onClick={runReloginErrors} disabled={relogining || loading || checking}>
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
        <CardContent className="grid gap-3 lg:grid-cols-[220px_160px_1fr_1fr]">
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
          <input
            value={workspaceIds}
            onChange={(event) => setWorkspaceIds(event.target.value)}
            placeholder="K12 Workspace ID 覆盖值（可选）"
            className="control-surface h-9 w-full"
          />
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
        <div className="overflow-x-auto">
          <table className="w-full min-w-[980px] text-sm">
            <thead className="border-b border-[var(--border)] bg-[var(--bg-pane)] text-left text-xs uppercase tracking-wide text-[var(--text-muted)]">
              <tr>
                <th className="w-10 px-4 py-3">
                  <input
                    type="checkbox"
                    checked={accounts.length > 0 && selectedIds.size === accounts.length}
                    onChange={toggleAll}
                    className="h-4 w-4 accent-[var(--accent)]"
                  />
                </th>
                <th className="px-4 py-3">帐号名</th>
                <th className="px-4 py-3">当前状态</th>
                <th className="px-4 py-3">类型</th>
                <th className="px-4 py-3">分组</th>
                <th className="px-4 py-3">创建时间</th>
                <th className="px-4 py-3">最近使用时间</th>
              </tr>
            </thead>
            <tbody>
              {accounts.map((account) => (
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
                  <td colSpan={7} className="px-4 py-12 text-center text-[var(--text-muted)]">
                    {loading ? '加载中...' : '暂无远端 Sub2API 账号'}
                  </td>
                </tr>
              )}
            </tbody>
          </table>
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
    </div>
  )
}
