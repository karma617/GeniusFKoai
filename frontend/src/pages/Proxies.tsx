import { useEffect, useState } from 'react'
import { apiFetch } from '@/lib/utils'
import { useI18n } from '@/lib/i18n-context'
import { Card } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Plus, Trash2, RefreshCw, ToggleLeft, ToggleRight, Globe2, ShieldCheck, CircleOff, Activity, Save } from 'lucide-react'

const DEFAULT_FALLBACK_PROXY_URL = 'http://127.0.0.1:7897'

export default function Proxies() {
  const { t } = useI18n()
  const [proxies, setProxies] = useState<any[]>([])
  const [newProxy, setNewProxy] = useState('')
  const [region, setRegion] = useState('')
  const [checking, setChecking] = useState(false)
  const [proxyStrategy, setProxyStrategy] = useState('pool_then_default')
  const [fallbackProxyUrl, setFallbackProxyUrl] = useState(DEFAULT_FALLBACK_PROXY_URL)
  const [savingProxyConfig, setSavingProxyConfig] = useState(false)
  const [proxyConfigSaved, setProxyConfigSaved] = useState(false)

  const load = async () => {
    const [proxyItems, config] = await Promise.all([
      apiFetch('/proxies'),
      apiFetch('/config'),
    ])
    setProxies(proxyItems)
    setProxyStrategy(config.proxy_strategy || 'pool_then_default')
    setFallbackProxyUrl(config.proxy_fallback_url || DEFAULT_FALLBACK_PROXY_URL)
  }

  useEffect(() => { load() }, [])

  const add = async () => {
    if (!newProxy.trim()) return
    const lines = newProxy.trim().split('\n').map(l => l.trim()).filter(Boolean)
    if (lines.length > 1) {
      await apiFetch('/proxies/bulk', {
        method: 'POST',
        body: JSON.stringify({ proxies: lines, region }),
      })
    } else {
      await apiFetch('/proxies', {
        method: 'POST',
        body: JSON.stringify({ url: lines[0], region }),
      })
    }
    setNewProxy('')
    load()
  }

  const del = async (id: number) => {
    await apiFetch(`/proxies/${id}`, { method: 'DELETE' })
    load()
  }

  const toggle = async (id: number) => {
    await apiFetch(`/proxies/${id}/toggle`, { method: 'PATCH' })
    load()
  }

  const check = async () => {
    setChecking(true)
    await apiFetch('/proxies/check', { method: 'POST' })
    setTimeout(() => { load(); setChecking(false) }, 3000)
  }

  const saveProxyConfig = async () => {
    setSavingProxyConfig(true)
    try {
      await apiFetch('/config', {
        method: 'PUT',
        body: JSON.stringify({
          data: {
            proxy_strategy: proxyStrategy,
            proxy_fallback_url: fallbackProxyUrl,
          },
        }),
      })
      setProxyConfigSaved(true)
      setTimeout(() => setProxyConfigSaved(false), 1800)
    } finally {
      setSavingProxyConfig(false)
    }
  }

  const activeCount = proxies.filter((item) => item.is_active).length
  const totalSuccess = proxies.reduce((sum, item) => sum + Number(item.success_count || 0), 0)
  const totalFail = proxies.reduce((sum, item) => sum + Number(item.fail_count || 0), 0)
  const metricCards = [
    { label: t('proxies.metric.count'), value: proxies.length, icon: Globe2, tone: 'text-[var(--accent)]' },
    { label: t('proxies.metric.enabled'), value: activeCount, icon: ShieldCheck, tone: 'text-emerald-400' },
    { label: t('proxies.metric.success'), value: totalSuccess, icon: Activity, tone: 'text-[var(--accent)]' },
    { label: t('proxies.metric.fail'), value: totalFail, icon: CircleOff, tone: 'text-red-400' },
  ]

  return (
    <div className="space-y-4">
      <Card className="overflow-hidden p-2.5">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="flex flex-wrap items-center gap-2">
            <div className="text-sm font-semibold text-[var(--text-primary)]">{t('proxies.title')}</div>
            <Badge variant="default">{t('common.total')} {proxies.length}</Badge>
            <Badge variant="secondary">{t('proxies.activeBadge', { count: activeCount })}</Badge>
          </div>
          <Button variant="outline" size="sm" onClick={check} disabled={checking}>
            <RefreshCw className={`h-4 w-4 mr-1.5 ${checking ? 'animate-spin' : ''}`} />
            {t('proxies.checkAll')}
          </Button>
        </div>
      </Card>

      <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
        {metricCards.map(({ label, value, icon: Icon, tone }) => (
          <Card key={label} className="bg-transparent">
            <div className="flex items-center justify-between gap-3">
              <div>
                <div className="text-[11px] uppercase tracking-[0.18em] text-[var(--text-muted)]">{label}</div>
                <div className="mt-1.5 text-xl font-semibold tracking-[-0.03em] text-[var(--text-primary)]">{value}</div>
              </div>
              <div className="flex h-9 w-9 items-center justify-center rounded-lg border border-[var(--border-soft)] bg-[var(--chip-bg)]">
                <Icon className={`h-5 w-5 ${tone}`} />
              </div>
            </div>
          </Card>
        ))}
      </div>

      <div className="grid gap-4 xl:grid-cols-[minmax(0,330px)_minmax(0,1fr)]">
        <Card className="bg-[var(--bg-pane)]/60">
          <div className="space-y-4">
            <div className="space-y-3 rounded-lg border border-[var(--border-soft)] bg-[var(--bg-elevated)]/45 p-3">
              <div>
                <div className="text-[11px] uppercase tracking-[0.18em] text-[var(--text-muted)]">代理策略</div>
                <div className="mt-1 text-sm font-medium text-[var(--text-primary)]">默认访问代理</div>
              </div>
              <select
                value={proxyStrategy}
                onChange={e => setProxyStrategy(e.target.value)}
                className="control-surface"
              >
                <option value="pool_then_default">代理池优先，空则本地代理</option>
                <option value="pool_only">只使用代理池</option>
                <option value="default_only">只使用默认代理</option>
                <option value="direct">不使用代理</option>
              </select>
              <input
                value={fallbackProxyUrl}
                onChange={e => setFallbackProxyUrl(e.target.value)}
                placeholder={DEFAULT_FALLBACK_PROXY_URL}
                className="control-surface control-surface-mono"
              />
              <div className="text-xs leading-5 text-[var(--text-muted)]">
                代理池无可用代理时，将按这里的默认代理访问授权页并交换 token。
              </div>
              <Button onClick={saveProxyConfig} disabled={savingProxyConfig} className="w-full">
                <Save className="h-4 w-4 mr-1.5" />
                {proxyConfigSaved ? '已保存' : (savingProxyConfig ? '保存中...' : '保存代理策略')}
              </Button>
            </div>
            <div>
              <div className="text-[11px] uppercase tracking-[0.18em] text-[var(--text-muted)]">{t('common.add')}</div>
              <div className="mt-1 text-sm font-medium text-[var(--text-primary)]">{t('proxies.addTitle')}</div>
            </div>
            <textarea
              value={newProxy}
              onChange={e => setNewProxy(e.target.value)}
              placeholder="http://user:pass@host:port"
              rows={8}
              className="control-surface control-surface-mono resize-none"
            />
            <input
              value={region}
              onChange={e => setRegion(e.target.value)}
              placeholder={t('proxies.regionPlaceholder')}
              className="control-surface"
            />
            <div className="rounded-lg border border-[var(--border-soft)] bg-[var(--bg-pane)]/45 px-3.5 py-2.5 text-xs leading-5 text-[var(--text-secondary)]">
              {t('proxies.hint')}
            </div>
            <Button onClick={add} className="w-full">
              <Plus className="h-4 w-4 mr-1.5" />
              {t('proxies.addToPool')}
            </Button>
          </div>
        </Card>

        <Card className="overflow-hidden p-0">
          <div className="border-b border-[var(--border)] px-4 py-3 text-sm font-medium text-[var(--text-primary)]">
            {t('proxies.list')}
          </div>
        <div className="glass-table-wrap">
        <table className="w-full min-w-[760px] text-sm">
          <thead>
            <tr className="border-b border-[var(--border)] text-[var(--text-muted)]">
              <th className="px-4 py-2.5 text-left">{t('proxies.address')}</th>
              <th className="px-4 py-2.5 text-left">{t('proxies.region')}</th>
              <th className="px-4 py-2.5 text-left">{t('proxies.successFailure')}</th>
              <th className="px-4 py-2.5 text-left">{t('common.status')}</th>
              <th className="px-4 py-2.5 text-left">{t('common.actions')}</th>
            </tr>
          </thead>
          <tbody>
            {proxies.length === 0 && (
              <tr>
                <td colSpan={5} className="px-4 py-8">
                  <div className="empty-state-panel">{t('proxies.empty')}</div>
                </td>
              </tr>
            )}
            {proxies.map(p => (
              <tr key={p.id} className="border-b border-[var(--border)]/40 hover:bg-[var(--bg-hover)]/70">
                <td className="px-4 py-2.5 font-mono text-xs text-[var(--text-secondary)]">{p.url}</td>
                <td className="px-4 py-2.5 text-[var(--text-muted)]">{p.region || '-'}</td>
                <td className="px-4 py-2.5">
                  <span className="text-emerald-400">{p.success_count}</span>
                  <span className="text-[var(--text-muted)]"> / </span>
                  <span className="text-red-400">{p.fail_count}</span>
                </td>
                <td className="px-4 py-2.5">
                  <Badge variant={p.is_active ? 'success' : 'danger'}>
                    {p.is_active ? t('common.active') : t('common.disabled')}
                  </Badge>
                </td>
                <td className="px-4 py-2.5">
                  <div className="flex items-center gap-2">
                    <button onClick={() => toggle(p.id)} className="table-action-btn">
                      {p.is_active ? <ToggleRight className="mr-1.5 h-4 w-4" /> : <ToggleLeft className="mr-1.5 h-4 w-4" />}
                      {p.is_active ? t('proxies.disable') : t('common.enabled')}
                    </button>
                    <button onClick={() => del(p.id)} className="table-action-btn table-action-btn-danger">
                      <Trash2 className="mr-1.5 h-4 w-4" />
                      {t('common.delete')}
                    </button>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        </div>
        </Card>
      </div>
    </div>
  )
}
