import { useCallback, useEffect, useState } from 'react'
import { createPortal } from 'react-dom'
import { apiFetch, cn } from '@/lib/utils'
import { getConfigOptions } from '@/lib/app-data'
import { Button } from '@/components/ui/button'
import { Copy, Loader2, X } from 'lucide-react'

export type PpPlusSettings = {
  sms_provider: string
  sms_country: string
  flow_country: string
  max_card_attempts: number
  max_phone_changes: number
  proxy_enabled: boolean
  proxy_mode: string
  proxy_api_url: string
  proxy_pool_text: string
  debug: boolean
}

export type PpPlusRuntime = {
  last_phone?: string
  last_phone_success_count?: number
  last_sms_provider?: string
}

export type PpPlusStatus = {
  running?: boolean
  stopping?: boolean
  settings?: PpPlusSettings
  runtime?: PpPlusRuntime
  current?: any
  accounts?: Record<string, any>
  sms_service_code?: string
  last_error?: string
}

export type PpAccountTask = {
  account_id?: number
  email?: string
  status?: string
  stage?: string
  ba_token?: string
  phone?: string
  error?: string
  logs?: Array<{ time?: number; status?: string; stage?: string; message?: string }>
}

const FLOW_COUNTRY_OPTIONS = [
  { value: 'BR', label: '巴西 BR' },
  { value: 'US', label: '美国 US' },
  { value: 'BA', label: '波黑 BA' },
  { value: 'ID', label: '印尼 ID' },
  { value: 'GB', label: '英国 GB' },
]

const SMS_PROVIDER_FALLBACK = [
  { value: 'herosms_api', label: 'HeroSMS' },
  { value: 'smsbower_api', label: 'SMSBower' },
  { value: 'grizzlysms_api', label: 'GrizzlySMS' },
  { value: 'sms_activate_api', label: 'SMS-Activate' },
  { value: 'sms_verification_number_api', label: 'SMS Verification Number' },
  { value: 'smspool_api', label: 'SMSPool' },
  { value: 'five_sim_api', label: '5sim' },
  { value: 'nexsms_api', label: 'NexSMS' },
]

function resolveSmsCountriesUrl(provider: string): string {
  const key = String(provider || '').toLowerCase()
  if (key.includes('smsbower')) return '/sms/smsbower/countries'
  if (key.includes('grizzly')) return '/sms/grizzlysms/countries'
  if (key.includes('verification_number')) return '/sms/sms-verification-number/countries'
  if (key.includes('smspool')) return '/sms/smspool/countries'
  if (key.includes('five_sim') || key === '5sim' || key.includes('fivesim')) return '/sms/five-sim/countries'
  if (key.includes('nexsms')) return '/sms/nexsms/countries'
  // HeroSMS / SMS-Activate 等默认走 herosms countries 接口形态；无专用接口时前端允许手填
  if (key.includes('herosms') || key.includes('sms_activate') || key.includes('smsactivate')) return '/sms/herosms/countries'
  return ''
}

const DEFAULT_SETTINGS: PpPlusSettings = {
  sms_provider: 'herosms_api',
  sms_country: '73',
  flow_country: 'BR',
  max_card_attempts: 5,
  max_phone_changes: 5,
  proxy_enabled: true,
  proxy_mode: 'api',
  proxy_api_url: '',
  proxy_pool_text: '',
  debug: false,
}

export function getAccountBaToken(acc: any): string {
  const overview = acc?.overview && typeof acc.overview === 'object' ? acc.overview : {}
  return String(overview.pp_ba_token || overview.ba_token || overview.ba_chain || '').trim()
}

export function getAccountPpTask(acc: any, live?: PpAccountTask | null): PpAccountTask {
  const overview = acc?.overview && typeof acc.overview === 'object' ? acc.overview : {}
  return {
    account_id: Number(acc?.id || live?.account_id || 0),
    email: String(acc?.email || live?.email || ''),
    status: String(live?.status || overview.pp_task_status || 'idle'),
    stage: String(live?.stage || overview.pp_task_stage || ''),
    ba_token: String(live?.ba_token || overview.pp_ba_token || overview.ba_token || ''),
    phone: String(live?.phone || overview.pp_last_phone || ''),
    error: String(live?.error || overview.pp_task_error || ''),
    logs: Array.isArray(live?.logs)
      ? live?.logs
      : Array.isArray(overview.pp_task_logs)
        ? overview.pp_task_logs
        : [],
  }
}

export function formatPpLogs(logs: PpAccountTask['logs'] = []): string {
  return (logs || [])
    .map((item) => {
      const ts = item?.time ? new Date(Number(item.time) * 1000).toLocaleString() : ''
      return `[${ts}] ${item?.status || '-'} ${item?.stage || item?.message || ''}${item?.message && item?.message !== item?.stage ? ` | ${item.message}` : ''}`
    })
    .join('\n')
}

export async function fetchPpPlusStatus(): Promise<PpPlusStatus> {
  return apiFetch('/pp-plus/status')
}

export function PpPlusSettingsDialog({
  open,
  onClose,
  onStarted,
}: {
  open: boolean
  onClose: () => void
  onStarted: (status: PpPlusStatus) => void
}) {
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [starting, setStarting] = useState(false)
  const [error, setError] = useState('')
  const [settings, setSettings] = useState<PpPlusSettings>(DEFAULT_SETTINGS)
  const [runtime, setRuntime] = useState<PpPlusRuntime>({})
  const [smsServiceCode, setSmsServiceCode] = useState('pp')
  const [smsProviderOptions, setSmsProviderOptions] = useState<Array<{ value: string; label: string; service_code?: string }>>(SMS_PROVIDER_FALLBACK)
  const [countries, setCountries] = useState<Array<{ id: string; chn?: string; eng?: string }>>([])
  const [countryManual, setCountryManual] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const [data, configOptions] = await Promise.all([
        apiFetch('/pp-plus/settings'),
        getConfigOptions().catch(() => null),
      ])
      const nextSettings = { ...DEFAULT_SETTINGS, ...(data?.settings || {}) }

      // 优先后端返回的已启用配置；否则用系统 sms_settings 已启用项；再退回定义列表
      let options: Array<{ value: string; label: string; service_code?: string }> = []
      if (Array.isArray(data?.sms_provider_options) && data.sms_provider_options.length > 0) {
        options = data.sms_provider_options.map((item: any) => ({
          value: String(item?.value || item?.provider_key || ''),
          label: String(item?.label || item?.display_name || item?.value || ''),
          service_code: String(item?.service_code || 'pp'),
        })).filter((item: any) => item.value)
      } else {
        const enabledSettings = Array.isArray(configOptions?.sms_settings)
          ? configOptions.sms_settings.filter((item: any) => item?.enabled && item?.provider_key && item.provider_key !== 'codex_sms_pool')
          : []
        if (enabledSettings.length > 0) {
          options = enabledSettings.map((item: any) => ({
            value: String(item.provider_key),
            label: String(item.display_name || item.catalog_label || item.provider_key),
          }))
        } else {
          const defs = Array.isArray(configOptions?.sms_providers) ? configOptions.sms_providers : []
          options = defs
            .filter((item: any) => String(item?.value || item?.provider_key || '') !== 'codex_sms_pool')
            .map((item: any) => ({
              value: String(item?.value || item?.provider_key || ''),
              label: String(item?.label || item?.value || ''),
            }))
            .filter((item: any) => item.value)
        }
      }
      if (options.length === 0) options = SMS_PROVIDER_FALLBACK
      setSmsProviderOptions(options)

      // 若当前选择不在列表里，自动落到默认/第一项
      if (!options.some((item) => item.value === nextSettings.sms_provider)) {
        const preferred = options.find((item: any) => item.is_default) || options[0]
        if (preferred?.value) nextSettings.sms_provider = preferred.value
      }

      setSettings(nextSettings)
      setRuntime(data?.runtime || {})
      const matched = options.find((item) => item.value === nextSettings.sms_provider)
      setSmsServiceCode(String(matched?.service_code || data?.sms_service_code || 'pp'))
    } catch (exc: any) {
      setError(exc?.message || '加载设置失败')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    if (!open) return
    load()
  }, [open, load])

  useEffect(() => {
    if (!open) return
    let active = true
    const provider = settings.sms_provider || 'herosms_api'
    const url = resolveSmsCountriesUrl(provider)
    if (!url) {
      setCountries([])
      setCountryManual(true)
      return () => { active = false }
    }
    setCountryManual(false)
    apiFetch(url)
      .then((resp) => {
        if (!active) return
        const list = Array.isArray(resp?.countries) ? resp.countries : Array.isArray(resp) ? resp : []
        const mapped = list.map((item: any) => ({
          id: String(item?.id ?? item?.code ?? item?.value ?? item?.iso ?? ''),
          chn: String(item?.chn || item?.name || item?.label || item?.title || item?.id || ''),
          eng: String(item?.eng || item?.iso || ''),
        })).filter((item: any) => item.id)
        setCountries(mapped)
        setCountryManual(mapped.length === 0)
      })
      .catch(() => {
        if (!active) return
        setCountries([])
        setCountryManual(true)
      })
    return () => {
      active = false
    }
  }, [open, settings.sms_provider])

  useEffect(() => {
    const matched = smsProviderOptions.find((item) => item.value === settings.sms_provider)
    if (matched?.service_code) setSmsServiceCode(String(matched.service_code))
  }, [settings.sms_provider, smsProviderOptions])

  if (!open) return null

  const save = async () => {
    setSaving(true)
    setError('')
    try {
      const data = await apiFetch('/pp-plus/settings', {
        method: 'POST',
        body: JSON.stringify(settings),
      })
      setSettings({ ...DEFAULT_SETTINGS, ...(data?.settings || settings) })
      setRuntime(data?.runtime || runtime)
      setSmsServiceCode(String(data?.sms_service_code || smsServiceCode))
    } catch (exc: any) {
      setError(exc?.message || '保存失败')
    } finally {
      setSaving(false)
    }
  }

  const start = async () => {
    setStarting(true)
    setError('')
    try {
      await apiFetch('/pp-plus/settings', {
        method: 'POST',
        body: JSON.stringify(settings),
      })
      const status = await apiFetch('/pp-plus/start', { method: 'POST', body: '{}' })
      onStarted(status)
      onClose()
    } catch (exc: any) {
      setError(exc?.message || '开启任务失败')
    } finally {
      setStarting(false)
    }
  }

  return createPortal(
    <div className="dialog-backdrop" onClick={onClose}>
      <div
        className="w-[min(720px,94vw)] max-h-[90vh] overflow-auto rounded-2xl border border-[var(--border)] bg-[var(--bg-base)] p-5 shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="mb-4 flex items-center justify-between gap-3">
          <div>
            <h2 className="text-base font-semibold text-[var(--text-primary)]">PLUS 开通设置</h2>
            <p className="mt-1 text-xs text-[var(--text-muted)]">
              所有账号共用接码平台与代理池；API Key 使用系统已有配置，PayPal service 固定为平台对应代码。
            </p>
          </div>
          <button onClick={onClose} className="rounded-lg p-1 text-[var(--text-muted)] hover:bg-[var(--bg-pane)]">
            <X className="h-4 w-4" />
          </button>
        </div>

        {loading ? (
          <div className="flex items-center gap-2 py-10 text-sm text-[var(--text-muted)]">
            <Loader2 className="h-4 w-4 animate-spin" /> 加载中...
          </div>
        ) : (
          <div className="space-y-4">
            <div className="rounded-xl border border-[var(--border-soft)] bg-[var(--bg-pane)]/40 p-3 text-xs text-[var(--text-secondary)]">
              <div>最近接码手机号：{runtime.last_phone || '-'}</div>
              <div className="mt-1">接码成功次数：{Number(runtime.last_phone_success_count || 0)}</div>
              <div className="mt-1">PayPal service 代码：{smsServiceCode || 'pp'}（写死，不可改）</div>
            </div>

            <div className="grid gap-3 md:grid-cols-2">
              <label className="space-y-1 text-xs">
                <span className="text-[var(--text-secondary)]">接码平台</span>
                <select
                  value={settings.sms_provider}
                  onChange={(e) => setSettings((s) => ({ ...s, sms_provider: e.target.value }))}
                  className="h-9 w-full rounded-lg border border-[var(--border)] bg-[var(--bg-input)] px-3 text-sm"
                >
                  {smsProviderOptions.map((item) => (
                    <option key={item.value} value={item.value}>{item.label}</option>
                  ))}
                </select>
              </label>

              <label className="space-y-1 text-xs">
                <span className="text-[var(--text-secondary)]">接码地区</span>
                {countryManual || countries.length === 0 ? (
                  <input
                    value={settings.sms_country}
                    onChange={(e) => setSettings((s) => ({ ...s, sms_country: e.target.value }))}
                    placeholder="如 73 / brazil / BR"
                    className="h-9 w-full rounded-lg border border-[var(--border)] bg-[var(--bg-input)] px-3 text-sm"
                  />
                ) : (
                  <select
                    value={settings.sms_country}
                    onChange={(e) => setSettings((s) => ({ ...s, sms_country: e.target.value }))}
                    className="h-9 w-full rounded-lg border border-[var(--border)] bg-[var(--bg-input)] px-3 text-sm"
                  >
                    {countries.map((item) => (
                      <option key={item.id} value={item.id}>{item.chn || item.id} ({item.id})</option>
                    ))}
                  </select>
                )}
              </label>

              <label className="space-y-1 text-xs">
                <span className="text-[var(--text-secondary)]">支付地区</span>
                <select
                  value={settings.flow_country}
                  onChange={(e) => setSettings((s) => ({ ...s, flow_country: e.target.value }))}
                  className="h-9 w-full rounded-lg border border-[var(--border)] bg-[var(--bg-input)] px-3 text-sm"
                >
                  {FLOW_COUNTRY_OPTIONS.map((item) => (
                    <option key={item.value} value={item.value}>{item.label}</option>
                  ))}
                </select>
              </label>

              <label className="space-y-1 text-xs">
                <span className="text-[var(--text-secondary)]">最大换卡次数</span>
                <input
                  type="number"
                  min={1}
                  max={20}
                  value={settings.max_card_attempts}
                  onChange={(e) => setSettings((s) => ({ ...s, max_card_attempts: Number(e.target.value || 5) }))}
                  className="h-9 w-full rounded-lg border border-[var(--border)] bg-[var(--bg-input)] px-3 text-sm"
                />
              </label>

              <label className="space-y-1 text-xs">
                <span className="text-[var(--text-secondary)]">最大换号次数</span>
                <input
                  type="number"
                  min={0}
                  max={20}
                  value={settings.max_phone_changes}
                  onChange={(e) => setSettings((s) => ({ ...s, max_phone_changes: Number(e.target.value || 5) }))}
                  className="h-9 w-full rounded-lg border border-[var(--border)] bg-[var(--bg-input)] px-3 text-sm"
                />
              </label>

              <label className="flex items-center gap-2 pt-6 text-xs text-[var(--text-secondary)]">
                <input
                  type="checkbox"
                  checked={settings.proxy_enabled}
                  onChange={(e) => setSettings((s) => ({ ...s, proxy_enabled: e.target.checked }))}
                />
                启用代理
              </label>
            </div>

            {settings.proxy_enabled && (
              <div className="space-y-3 rounded-xl border border-[var(--border-soft)] p-3">
                <label className="space-y-1 text-xs block">
                  <span className="text-[var(--text-secondary)]">代理模式</span>
                  <select
                    value={settings.proxy_mode}
                    onChange={(e) => setSettings((s) => ({ ...s, proxy_mode: e.target.value }))}
                    className="h-9 w-full rounded-lg border border-[var(--border)] bg-[var(--bg-input)] px-3 text-sm"
                  >
                    <option value="api">API 接口获取</option>
                    <option value="pool">代理池文本</option>
                  </select>
                </label>
                {settings.proxy_mode === 'api' ? (
                  <label className="space-y-1 text-xs block">
                    <span className="text-[var(--text-secondary)]">代理 API 地址</span>
                    <textarea
                      rows={3}
                      value={settings.proxy_api_url}
                      onChange={(e) => setSettings((s) => ({ ...s, proxy_api_url: e.target.value }))}
                      placeholder="http://... 可用 {country} 占位"
                      className="w-full rounded-lg border border-[var(--border)] bg-[var(--bg-input)] px-3 py-2 text-sm"
                    />
                  </label>
                ) : (
                  <label className="space-y-1 text-xs block">
                    <span className="text-[var(--text-secondary)]">代理池</span>
                    <textarea
                      rows={5}
                      value={settings.proxy_pool_text}
                      onChange={(e) => setSettings((s) => ({ ...s, proxy_pool_text: e.target.value }))}
                      placeholder="host:port:user:pass 每行一条"
                      className="w-full rounded-lg border border-[var(--border)] bg-[var(--bg-input)] px-3 py-2 text-sm font-mono"
                    />
                  </label>
                )}
              </div>
            )}

            {error && <div className="rounded-lg border border-red-500/20 bg-red-500/10 px-3 py-2 text-sm text-red-300">{error}</div>}

            <div className="flex justify-end gap-2 pt-2">
              <Button variant="outline" onClick={onClose}>关闭</Button>
              <Button variant="outline" disabled={saving || starting} onClick={save}>
                {saving ? <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" /> : null}
                保存设置
              </Button>
              <Button disabled={saving || starting} onClick={start}>
                {starting ? <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" /> : null}
                开启任务
              </Button>
            </div>
          </div>
        )}
      </div>
    </div>,
    document.body,
  )
}

export function PpBaTokenDialog({
  open,
  account,
  onClose,
  onSaved,
}: {
  open: boolean
  account: any
  onClose: () => void
  onSaved: () => void
}) {
  const [value, setValue] = useState('')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    if (!open) return
    setValue(getAccountBaToken(account))
    setError('')
  }, [open, account])

  if (!open || !account) return null

  const save = async () => {
    setSaving(true)
    setError('')
    try {
      await apiFetch(`/pp-plus/accounts/${account.id}/ba-token`, {
        method: 'POST',
        body: JSON.stringify({ ba_token: value }),
      })
      onSaved()
      onClose()
    } catch (exc: any) {
      setError(exc?.message || '保存 BA 链失败')
    } finally {
      setSaving(false)
    }
  }

  return createPortal(
    <div className="dialog-backdrop" onClick={onClose}>
      <div className="w-[min(560px,94vw)] rounded-2xl border border-[var(--border)] bg-[var(--bg-base)] p-5 shadow-2xl" onClick={(e) => e.stopPropagation()}>
        <div className="mb-3 flex items-center justify-between">
          <div>
            <h2 className="text-base font-semibold">填写 BA 链</h2>
            <p className="mt-1 text-xs text-[var(--text-muted)]">{account.email}</p>
          </div>
          <button onClick={onClose} className="rounded-lg p-1 text-[var(--text-muted)] hover:bg-[var(--bg-pane)]"><X className="h-4 w-4" /></button>
        </div>
        <textarea
          rows={5}
          value={value}
          onChange={(e) => setValue(e.target.value)}
          placeholder="BA-xxxxxxxx 或完整收银台链接"
          className="w-full rounded-lg border border-[var(--border)] bg-[var(--bg-input)] px-3 py-2 text-sm font-mono"
        />
        {error && <div className="mt-2 rounded-lg border border-red-500/20 bg-red-500/10 px-3 py-2 text-sm text-red-300">{error}</div>}
        <div className="mt-4 flex justify-end gap-2">
          <Button variant="outline" onClick={onClose}>取消</Button>
          <Button disabled={saving || !value.trim()} onClick={save}>
            {saving ? <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" /> : null}
            保存
          </Button>
        </div>
      </div>
    </div>,
    document.body,
  )
}

export function PpTaskLogDialog({
  open,
  task,
  onClose,
}: {
  open: boolean
  task: PpAccountTask | null
  onClose: () => void
}) {
  if (!open || !task) return null
  const text = formatPpLogs(task.logs)
  return createPortal(
    <div className="dialog-backdrop" onClick={onClose}>
      <div className="w-[min(720px,94vw)] max-h-[88vh] overflow-auto rounded-2xl border border-[var(--border)] bg-[var(--bg-base)] p-5 shadow-2xl" onClick={(e) => e.stopPropagation()}>
        <div className="mb-3 flex items-center justify-between gap-3">
          <div>
            <h2 className="text-base font-semibold">任务日志</h2>
            <p className="mt-1 text-xs text-[var(--text-muted)]">{task.email} · {task.status} · {task.stage || '-'}</p>
          </div>
          <div className="flex items-center gap-2">
            <Button
              size="sm"
              variant="outline"
              onClick={() => {
                try { navigator.clipboard.writeText(text || '') } catch {}
              }}
            >
              <Copy className="mr-1.5 h-3.5 w-3.5" /> 复制全部
            </Button>
            <button onClick={onClose} className="rounded-lg p-1 text-[var(--text-muted)] hover:bg-[var(--bg-pane)]"><X className="h-4 w-4" /></button>
          </div>
        </div>
        {task.error && <div className="mb-3 rounded-lg border border-red-500/20 bg-red-500/10 px-3 py-2 text-sm text-red-300">{task.error}</div>}
        <pre className="max-h-[60vh] overflow-auto rounded-xl border border-[var(--border-soft)] bg-[var(--bg-pane)]/40 p-3 text-xs leading-6 whitespace-pre-wrap">{text || '暂无日志'}</pre>
      </div>
    </div>,
    document.body,
  )
}

export function PpPlusFloatingWidget({
  status,
  onStop,
  onOpenLogs,
}: {
  status: PpPlusStatus | null
  onStop: () => void
  onOpenLogs: (task: PpAccountTask) => void
}) {
  const [open, setOpen] = useState(false)
  const [stopping, setStopping] = useState(false)
  const running = Boolean(status?.running)
  if (!running && !status?.stopping) return null

  const current = status?.current || null

  return (
    <>
      <button
        onClick={() => setOpen(true)}
        className="fixed bottom-6 right-6 z-[80] flex h-14 w-14 items-center justify-center rounded-full border border-amber-400/40 bg-[#1b1408] text-amber-300 shadow-[0_0_24px_rgba(245,158,11,0.35)]"
        title="PLUS 开通任务进行中"
      >
        <svg viewBox="0 0 64 64" className="h-10 w-10">
          <circle cx="32" cy="32" r="26" fill="none" stroke="rgba(251,191,36,0.25)" strokeWidth="4" />
          <circle cx="32" cy="32" r="26" fill="none" stroke="#fbbf24" strokeWidth="4" strokeDasharray="40 120" strokeLinecap="round">
            <animateTransform attributeName="transform" type="rotate" from="0 32 32" to="360 32 32" dur="1.2s" repeatCount="indefinite" />
          </circle>
          <path d="M20 34c4 6 8 8 12 8s8-2 12-8" fill="none" stroke="#fde68a" strokeWidth="3" strokeLinecap="round">
            <animate attributeName="opacity" values="0.4;1;0.4" dur="1.4s" repeatCount="indefinite" />
          </path>
          <circle cx="32" cy="26" r="4" fill="#fbbf24">
            <animate attributeName="cy" values="24;28;24" dur="1.1s" repeatCount="indefinite" />
          </circle>
        </svg>
      </button>

      {open && createPortal(
        <div className="dialog-backdrop" onClick={() => setOpen(false)}>
          <div className="w-[min(640px,94vw)] rounded-2xl border border-[var(--border)] bg-[var(--bg-base)] p-5 shadow-2xl" onClick={(e) => e.stopPropagation()}>
            <div className="mb-3 flex items-center justify-between">
              <div>
                <h2 className="text-base font-semibold">PLUS 开通任务</h2>
                <p className="mt-1 text-xs text-[var(--text-muted)]">
                  {status?.stopping ? '停止中：等待当前账号任务结束后终止' : '任务进行中（串行）'}
                </p>
              </div>
              <button onClick={() => setOpen(false)} className="rounded-lg p-1 text-[var(--text-muted)] hover:bg-[var(--bg-pane)]"><X className="h-4 w-4" /></button>
            </div>

            <div className="rounded-xl border border-[var(--border-soft)] bg-[var(--bg-pane)]/40 p-3 text-sm">
              <div>当前账号：{current?.email || '-'}</div>
              <div className="mt-1 text-[var(--text-secondary)]">节点：{current?.stage || '-'}</div>
              <div className="mt-1 text-[var(--text-secondary)]">状态：{current?.status || '-'}</div>
              {current?.phone ? <div className="mt-1 text-[var(--text-secondary)]">手机号：{current.phone}</div> : null}
              {current?.error ? <div className="mt-2 text-red-300">{current.error}</div> : null}
            </div>

            <div className="mt-4 flex justify-end gap-2">
              {current && (
                <Button variant="outline" onClick={() => onOpenLogs(current)}>查看日志</Button>
              )}
              <Button
                variant="outline"
                disabled={stopping || Boolean(status?.stopping)}
                onClick={async () => {
                  setStopping(true)
                  try { await onStop() } finally { setStopping(false) }
                }}
              >
                {stopping || status?.stopping ? <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" /> : null}
                停止任务
              </Button>
            </div>
          </div>
        </div>,
        document.body,
      )}
    </>
  )
}

export function PpTaskCell({
  task,
  onViewLogs,
}: {
  task: PpAccountTask
  onViewLogs: () => void
}) {
  const status = String(task.status || 'idle')
  const stage = String(task.stage || '')
  if (status === 'error') {
    return (
      <button
        onClick={onViewLogs}
        className="max-w-[76px] truncate text-left text-[11px] font-medium text-red-400 underline-offset-2 hover:underline"
        title="查看日志"
      >
        查看日志
      </button>
    )
  }
  if (!stage && status === 'idle') {
    return <span className="text-[11px] text-[var(--text-muted)]">-</span>
  }
  return (
    <div className="max-w-[76px]">
      <div
        className={cn(
          'truncate text-[11px] leading-4',
          status === 'running' ? 'text-amber-400' : status === 'success' ? 'text-emerald-400' : 'text-[var(--text-secondary)]',
        )}
        title={stage || status}
      >
        {stage || status}
      </div>
    </div>
  )
}
