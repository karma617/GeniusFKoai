import { useEffect, useState } from 'react'
import { apiFetch } from '@/lib/utils'
import { useI18n } from '@/lib/i18n-context'
import { Card } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Plus, Trash2, RefreshCw, ToggleLeft, ToggleRight, Globe2, ShieldCheck, CircleOff, Activity, Save, DownloadCloud, SearchCheck, Database } from 'lucide-react'

const DEFAULT_FALLBACK_PROXY_URL = 'http://127.0.0.1:7897'
const FREE_PROXY_TEXT = {
  title: '\u514d\u8d39\u4ee3\u7406',
  subtitle: '\u62c9\u53d6\u516c\u5f00\u4ee3\u7406\u6e90\uff0c\u68c0\u6d4b\u53ef\u7528\u540e\u5165\u5e93',
  allSources: '\u5168\u90e8\u6765\u6e90',
  source: '\u6765\u6e90',
  limit: '\u6570\u91cf',
  rounds: '\u8f6e\u6570',
  region: '\u5730\u533a\u6807\u8bb0',
  candidates: '\u5019\u9009\u4ee3\u7406',
  fetch: '\u62c9\u53d6',
  checking: '\u68c0\u6d4b\u4e2d...',
  check: '\u68c0\u6d4b\u5019\u9009',
  importValid: '\u6dfb\u52a0\u53ef\u7528\u4ee3\u7406',
  unavailable: '\u672a\u68c0\u6d4b\u5230 proxy-checker/fetch_proxies.py',
  emptyCandidates: '\u5148\u62c9\u53d6\u6216\u7c98\u8d34\u5019\u9009\u4ee3\u7406',
  fetched: '\u5df2\u62c9\u53d6',
  checked: '\u5df2\u68c0\u6d4b',
  valid: '\u53ef\u7528',
  added: '\u5df2\u5165\u5e93',
  failed: '\u64cd\u4f5c\u5931\u8d25',
  placeholder: 'http://1.2.3.4:8080\nsocks5://1.2.3.4:1080',
}

type FreeProxySource = { id: string; name: string }

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
  const [freeSources, setFreeSources] = useState<FreeProxySource[]>([])
  const [freeFetchAvailable, setFreeFetchAvailable] = useState(false)
  const [freeSource, setFreeSource] = useState('proxifly')
  const [freeLimit, setFreeLimit] = useState(200)
  const [freeRounds, setFreeRounds] = useState(1)
  const [freeRegion, setFreeRegion] = useState('FREE')
  const [freeCandidates, setFreeCandidates] = useState('')
  const [freeResults, setFreeResults] = useState<any[]>([])
  const [freeBusy, setFreeBusy] = useState('')
  const [freeNotice, setFreeNotice] = useState('')
  const [freeError, setFreeError] = useState('')

  const load = async () => {
    const [proxyItems, config, freeCaps] = await Promise.all([
      apiFetch('/proxies'),
      apiFetch('/config'),
      apiFetch('/proxies/free/capabilities').catch(() => null),
    ])
    setProxies(proxyItems)
    setProxyStrategy(config.proxy_strategy || 'pool_then_default')
    setFallbackProxyUrl(config.proxy_fallback_url || DEFAULT_FALLBACK_PROXY_URL)
    const sources = (freeCaps?.sources || []) as FreeProxySource[]
    setFreeFetchAvailable(Boolean(freeCaps?.available))
    setFreeSources([{ id: 'all', name: FREE_PROXY_TEXT.allSources }, ...sources])
    if (sources.length > 0 && !sources.some(item => item.id === freeSource)) {
      setFreeSource(sources[0].id)
    }
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

  const parseFreeCandidates = () => (
    freeCandidates
      .split(/\r?\n/)
      .map(line => line.trim())
      .filter(Boolean)
  )

  const fetchFreeCandidates = async () => {
    setFreeBusy('fetch')
    setFreeNotice('')
    setFreeError('')
    try {
      const data = await apiFetch('/proxies/free/fetch', {
        method: 'POST',
        body: JSON.stringify({ source: freeSource, limit: freeLimit }),
      })
      const lines = (data.proxies || [])
        .map((item: any) => String(item.proxy || item || '').trim())
        .filter(Boolean)
      setFreeCandidates(lines.join('\n'))
      setFreeResults([])
      setFreeNotice(`${FREE_PROXY_TEXT.fetched} ${lines.length}`)
      if (data.errors?.length) {
        setFreeError(data.errors.map((item: any) => `${item.source}: ${item.error}`).join(' | '))
      }
    } catch (error: any) {
      setFreeError(`${FREE_PROXY_TEXT.failed}: ${error?.message || error}`)
    } finally {
      setFreeBusy('')
    }
  }

  const checkFreeCandidates = async () => {
    const lines = parseFreeCandidates()
    if (lines.length === 0) {
      setFreeError(FREE_PROXY_TEXT.emptyCandidates)
      return
    }
    setFreeBusy('check')
    setFreeNotice('')
    setFreeError('')
    try {
      const data = await apiFetch('/proxies/free/check', {
        method: 'POST',
        body: JSON.stringify({
          proxies: lines,
          rounds: freeRounds,
          timeout: 10,
          concurrency: 20,
          limit: Math.max(1, freeLimit),
        }),
      })
      setFreeResults(data.results || [])
      setFreeNotice(`${FREE_PROXY_TEXT.checked} ${data.checked || 0} / ${FREE_PROXY_TEXT.valid} ${data.valid_count || 0}`)
    } catch (error: any) {
      setFreeError(`${FREE_PROXY_TEXT.failed}: ${error?.message || error}`)
    } finally {
      setFreeBusy('')
    }
  }

  const importValidFreeProxies = async () => {
    const validUrls = freeResults
      .filter(item => item.valid)
      .map(item => String(item.proxy || '').trim())
      .filter(Boolean)
    if (validUrls.length === 0) return
    setFreeBusy('import')
    setFreeNotice('')
    setFreeError('')
    try {
      const data = await apiFetch('/proxies/free/import-valid', {
        method: 'POST',
        body: JSON.stringify({ proxies: validUrls, region: freeRegion || 'FREE' }),
      })
      setFreeNotice(`${FREE_PROXY_TEXT.added} ${data.added || 0}`)
      await load()
    } catch (error: any) {
      setFreeError(`${FREE_PROXY_TEXT.failed}: ${error?.message || error}`)
    } finally {
      setFreeBusy('')
    }
  }

  const activeCount = proxies.filter((item) => item.is_active).length
  const totalSuccess = proxies.reduce((sum, item) => sum + Number(item.success_count || 0), 0)
  const totalFail = proxies.reduce((sum, item) => sum + Number(item.fail_count || 0), 0)
  const freeCandidateCount = parseFreeCandidates().length
  const validFreeCount = freeResults.filter(item => item.valid).length
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
          <Card key={label} className="bg-transparent transition-all duration-200 hover:shadow-[var(--shadow-hard)] hover:border-[var(--accent-edge)]">
            <div className="flex items-center justify-between gap-3">
              <div>
                <div className="text-[11px] uppercase tracking-[0.18em] text-[var(--text-muted)]">{label}</div>
                <div className="mt-1.5 text-xl font-semibold tracking-[-0.03em] text-[var(--text-primary)]">{value}</div>
              </div>
              <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-[var(--gradient-accent-soft)]">
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

            <div className="space-y-3 rounded-lg border border-[var(--border-soft)] bg-[var(--bg-elevated)]/45 p-3">
              <div className="flex items-start justify-between gap-3">
                <div>
                  <div className="text-[11px] uppercase tracking-[0.18em] text-[var(--text-muted)]">{FREE_PROXY_TEXT.title}</div>
                  <div className="mt-1 text-sm font-medium text-[var(--text-primary)]">{FREE_PROXY_TEXT.subtitle}</div>
                </div>
                <Badge variant={freeFetchAvailable ? 'success' : 'secondary'}>
                  {freeFetchAvailable ? 'ON' : 'OFF'}
                </Badge>
              </div>

              {!freeFetchAvailable && (
                <div className="rounded-md border border-amber-400/25 bg-amber-400/10 px-3 py-2 text-xs leading-5 text-amber-500">
                  {FREE_PROXY_TEXT.unavailable}
                </div>
              )}

              <div className="grid grid-cols-2 gap-2">
                <label className="space-y-1 text-xs text-[var(--text-muted)]">
                  <span>{FREE_PROXY_TEXT.source}</span>
                  <select
                    value={freeSource}
                    onChange={event => setFreeSource(event.target.value)}
                    className="control-surface"
                  >
                    {freeSources.map(item => (
                      <option key={item.id} value={item.id}>{item.name || item.id}</option>
                    ))}
                  </select>
                </label>
                <label className="space-y-1 text-xs text-[var(--text-muted)]">
                  <span>{FREE_PROXY_TEXT.limit}</span>
                  <input
                    type="number"
                    min={1}
                    max={500}
                    value={freeLimit}
                    onChange={event => setFreeLimit(Number(event.target.value || 1))}
                    className="control-surface"
                  />
                </label>
                <label className="space-y-1 text-xs text-[var(--text-muted)]">
                  <span>{FREE_PROXY_TEXT.rounds}</span>
                  <select
                    value={freeRounds}
                    onChange={event => setFreeRounds(Number(event.target.value || 1))}
                    className="control-surface"
                  >
                    <option value={1}>1</option>
                    <option value={2}>2</option>
                    <option value={3}>3</option>
                  </select>
                </label>
                <label className="space-y-1 text-xs text-[var(--text-muted)]">
                  <span>{FREE_PROXY_TEXT.region}</span>
                  <input
                    value={freeRegion}
                    onChange={event => setFreeRegion(event.target.value)}
                    className="control-surface"
                  />
                </label>
              </div>

              <textarea
                value={freeCandidates}
                onChange={event => setFreeCandidates(event.target.value)}
                placeholder={FREE_PROXY_TEXT.placeholder}
                rows={5}
                className="control-surface control-surface-mono resize-none"
              />

              <div className="flex flex-wrap items-center gap-2">
                <Button
                  variant="outline"
                  size="sm"
                  onClick={fetchFreeCandidates}
                  disabled={!freeFetchAvailable || Boolean(freeBusy)}
                >
                  <DownloadCloud className={`h-4 w-4 mr-1.5 ${freeBusy === 'fetch' ? 'animate-pulse' : ''}`} />
                  {FREE_PROXY_TEXT.fetch}
                </Button>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={checkFreeCandidates}
                  disabled={freeCandidateCount === 0 || Boolean(freeBusy)}
                >
                  <SearchCheck className={`h-4 w-4 mr-1.5 ${freeBusy === 'check' ? 'animate-spin' : ''}`} />
                  {freeBusy === 'check' ? FREE_PROXY_TEXT.checking : FREE_PROXY_TEXT.check}
                </Button>
                <Button
                  size="sm"
                  onClick={importValidFreeProxies}
                  disabled={validFreeCount === 0 || Boolean(freeBusy)}
                >
                  <Database className="h-4 w-4 mr-1.5" />
                  {FREE_PROXY_TEXT.importValid}
                </Button>
              </div>

              <div className="flex flex-wrap gap-2 text-xs">
                <Badge variant="secondary">{FREE_PROXY_TEXT.candidates} {freeCandidateCount}</Badge>
                <Badge variant={validFreeCount > 0 ? 'success' : 'secondary'}>
                  {FREE_PROXY_TEXT.valid} {validFreeCount}
                </Badge>
              </div>

              {(freeNotice || freeError) && (
                <div className={`rounded-md border px-3 py-2 text-xs leading-5 ${
                  freeError
                    ? 'border-red-400/25 bg-red-400/10 text-red-400'
                    : 'border-emerald-400/25 bg-emerald-400/10 text-emerald-500'
                }`}>
                  {freeError || freeNotice}
                </div>
              )}

              {freeResults.length > 0 && (
                <div className="max-h-44 overflow-auto rounded-md border border-[var(--border-soft)]">
                  {freeResults.slice(0, 8).map(item => (
                    <div key={`${item.proxy}-${item.grade}`} className="flex items-center justify-between gap-2 border-b border-[var(--border-soft)]/70 px-2.5 py-2 text-xs last:border-b-0">
                      <div className="min-w-0">
                        <div className="truncate font-mono text-[var(--text-secondary)]">{item.proxy}</div>
                        <div className="mt-0.5 text-[var(--text-muted)]">
                          {item.latency ? `${item.latency}ms` : '-'} · {item.registration_ready ? 'REG' : item.error || '-'}
                        </div>
                      </div>
                      <Badge variant={item.valid ? 'success' : 'danger'}>{item.grade || 'F'}</Badge>
                    </div>
                  ))}
                </div>
              )}
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
