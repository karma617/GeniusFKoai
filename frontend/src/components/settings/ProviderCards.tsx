import { useState, useEffect, useRef } from 'react'
import { apiFetch } from '@/lib/utils'
import { useI18n } from '@/lib/i18n-context'
import type { TranslationKey } from '@/lib/i18n'
import type { ProviderOption, ProviderSetting } from '@/lib/config-options'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Save, Eye, EyeOff, X, Pencil, Plus, Trash2, FlaskConical, Search } from 'lucide-react'
import { invalidateConfigOptionsCache } from '@/lib/app-data'

const CATEGORY_GROUPS = [
  { key: 'free', labelKey: 'providers.category.free', descKey: 'providers.category.freeDesc' },
  { key: 'selfhost', labelKey: 'providers.category.selfhost', descKey: 'providers.category.selfhostDesc' },
  { key: 'thirdparty', labelKey: 'providers.category.thirdparty', descKey: 'providers.category.thirdpartyDesc' },
  { key: 'local', labelKey: 'providers.category.local', descKey: 'providers.category.localDesc' },
  { key: 'custom', labelKey: 'providers.category.custom', descKey: 'providers.category.customDesc' },
] satisfies Array<{ key: string; labelKey: TranslationKey; descKey: TranslationKey }>

type GmailMother = {
  id: string
  master_email: string
  credentials_json: string
  token_json: string
  aliases: string[]
}

type GmailApiCodeRow = {
  email: string
  codeUrl: string
}

function newGmailMother(): GmailMother {
  return {
    id: `${Date.now()}-${Math.random().toString(16).slice(2)}`,
    master_email: '',
    credentials_json: '',
    token_json: '',
    aliases: [],
  }
}

function parseGmailPool(value: string): GmailMother[] {
  const text = (value || '').trim()
  if (!text) return []
  try {
    const payload = JSON.parse(text)
    const items = Array.isArray(payload) ? payload : Array.isArray(payload?.accounts) ? payload.accounts : []
    return items.map((item: any, index: number) => ({
      id: `${Date.now()}-${index}-${Math.random().toString(16).slice(2)}`,
      master_email: String(item?.master_email || item?.email || ''),
      credentials_json: typeof item?.credentials_json === 'string'
        ? item.credentials_json
        : JSON.stringify(item?.credentials_json || item?.credentials || {}, null, 2),
      token_json: typeof item?.token_json === 'string'
        ? item.token_json
        : JSON.stringify(item?.token_json || item?.token || {}, null, 2),
      aliases: Array.isArray(item?.aliases)
        ? item.aliases.map((alias: any) => String(alias || '').trim()).filter(Boolean).slice(0, 5)
        : [],
    }))
  } catch {
    return []
  }
}

function serializeGmailPool(items: GmailMother[]): string {
  const payload = items
    .map(item => ({
      master_email: item.master_email.trim(),
      credentials_json: item.credentials_json.trim(),
      token_json: item.token_json.trim(),
      aliases: item.aliases.map(alias => alias.trim()).filter(Boolean).slice(0, 5),
    }))
    .filter(item => item.master_email || item.credentials_json || item.token_json || item.aliases.length)
  return payload.length ? JSON.stringify(payload, null, 2) : ''
}

function parseGmailApiCodeRows(value: string): GmailApiCodeRow[] {
  const rows: GmailApiCodeRow[] = []
  const seen = new Set<string>()
  for (const rawLine of String(value || '').split(/\r?\n/)) {
    const line = rawLine.trim()
    if (!line || line.startsWith('#') || !line.includes('----')) continue
    const [emailPart, ...urlParts] = line.split('----')
    const email = emailPart.trim().toLowerCase()
    const codeUrl = urlParts.join('----').trim()
    if (!email || !email.includes('@') || !/^https?:\/\//i.test(codeUrl) || seen.has(email)) continue
    seen.add(email)
    rows.push({ email, codeUrl })
  }
  return rows
}

/* ------------------------------------------------------------------ */
/*  Toggle                                                             */
/* ------------------------------------------------------------------ */
function Toggle({ checked, onChange, disabled }: { checked: boolean; onChange: (v: boolean) => void; disabled?: boolean }) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className={`relative inline-flex h-5 w-9 shrink-0 items-center rounded-full transition-colors ${
        disabled ? 'opacity-40 cursor-not-allowed' : 'cursor-pointer'
      } ${checked ? 'bg-[var(--accent)]' : 'bg-[var(--chip-bg)] border border-[var(--border)]'}`}
    >
      <span className={`inline-block h-3.5 w-3.5 rounded-full bg-white shadow transition-transform ${checked ? 'translate-x-[18px]' : 'translate-x-[3px]'}`} />
    </button>
  )
}

/* ------------------------------------------------------------------ */
/*  Searchable Select                                                  */
/* ------------------------------------------------------------------ */
function SearchableSelect({ value, options, placeholder, onChange }: {
  value: string
  options: Array<{ value: string; label: string }>
  placeholder?: string
  onChange: (v: string) => void
}) {
  const { t } = useI18n()
  const [open, setOpen] = useState(false)
  const [search, setSearch] = useState('')
  const containerRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLInputElement>(null)

  const filtered = search
    ? options.filter(o => o.label.toLowerCase().includes(search.toLowerCase()) || o.value.includes(search))
    : options

  const selectedLabel = options.find(o => o.value === value)?.label || ''

  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [])

  useEffect(() => {
    if (open && inputRef.current) inputRef.current.focus()
  }, [open])

  return (
    <div ref={containerRef} className="relative">
      <button
        type="button"
        onClick={() => { setOpen(!open); setSearch('') }}
        className="control-surface w-full text-left flex items-center justify-between"
      >
        <span className={selectedLabel ? 'text-[var(--text-primary)]' : 'text-[var(--text-muted)]'}>
          {selectedLabel || placeholder || t('providers.selectPlaceholder')}
        </span>
        <svg className="h-4 w-4 text-[var(--text-muted)]" fill="none" viewBox="0 0 24 24" stroke="currentColor">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d={open ? 'M5 15l7-7 7 7' : 'M19 9l-7 7-7-7'} />
        </svg>
      </button>
      {open && (
        <div className="absolute z-50 mt-1 w-full rounded-lg border border-[var(--border-soft)] bg-[var(--bg-pane)] shadow-lg">
          <div className="p-2 border-b border-[var(--border)]">
            <div className="relative">
              <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 h-3.5 w-3.5 text-[var(--text-muted)]" />
              <input
                ref={inputRef}
                type="text"
                value={search}
                onChange={e => setSearch(e.target.value)}
                placeholder={t('providers.searchPlaceholder')}
                className="w-full rounded-md border border-[var(--border-soft)] bg-[var(--bg-base)] pl-8 pr-3 py-1.5 text-sm text-[var(--text-primary)] placeholder:text-[var(--text-muted)] focus:outline-none focus:ring-1 focus:ring-[var(--accent)]"
              />
            </div>
          </div>
          <div className="max-h-48 overflow-y-auto py-1">
            {filtered.length === 0 ? (
              <div className="px-3 py-2 text-sm text-[var(--text-muted)]">{t('providers.noMatches')}</div>
            ) : filtered.map(o => (
              <button
                key={o.value}
                type="button"
                onClick={() => { onChange(o.value); setOpen(false); setSearch('') }}
                className={`w-full text-left px-3 py-1.5 text-sm hover:bg-[var(--chip-bg)] ${
                  o.value === value ? 'bg-[var(--accent)]/10 text-[var(--accent)] font-medium' : 'text-[var(--text-primary)]'
                }`}
              >
                {o.label}
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}

/* ------------------------------------------------------------------ */
/*  Edit modal                                                         */
/* ------------------------------------------------------------------ */
function EditModal({
  provider, setting, providerType, onClose, onSaved,
}: {
  provider: ProviderOption; setting: ProviderSetting | null; providerType: string
  onClose: () => void; onSaved: () => void
}) {
  const { t } = useI18n()
  const fields = provider.fields || []
  const [form, setForm] = useState<Record<string, string>>(() => {
    const data: Record<string, string> = {}
    for (const field of fields) {
      data[field.key] = (setting?.auth?.[field.key] || '') || (setting?.config?.[field.key] || '')
    }
    return data
  })
  const [showSecret, setShowSecret] = useState<Record<string, boolean>>({})
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)
  const [testing, setTesting] = useState(false)
  const [testResult, setTestResult] = useState<{ ok: boolean; message?: string; error?: string } | null>(null)
  const [asyncOptions, setAsyncOptions] = useState<Record<string, Array<{ value: string; label: string }>>>({})
  const [asyncLoading, setAsyncLoading] = useState<Record<string, boolean>>({})
  const [gmailAuthCode, setGmailAuthCode] = useState('')
  const [gmailCodeVerifier, setGmailCodeVerifier] = useState('')
  const [gmailAuthMotherId, setGmailAuthMotherId] = useState('')
  const [gmailCallbackSessionId, setGmailCallbackSessionId] = useState('')
  const [gmailOauthLoading, setGmailOauthLoading] = useState(false)
  const [gmailOauthResult, setGmailOauthResult] = useState<{ ok: boolean; message?: string; error?: string } | null>(null)
  const [gmailMothers, setGmailMothers] = useState<GmailMother[]>(() => {
    const parsed = parseGmailPool((setting?.auth?.gmail_oauth_pool_json || '') || (setting?.config?.gmail_oauth_pool_json || ''))
    if (parsed.length > 0) return parsed
    const legacyMaster = setting?.config?.gmail_oauth_master_email || ''
    const legacyCredentials = setting?.auth?.gmail_oauth_credentials_json || ''
    const legacyToken = setting?.auth?.gmail_oauth_token_json || ''
    if (!legacyMaster && !legacyCredentials && !legacyToken) return []
    return [{ ...newGmailMother(), master_email: legacyMaster, credentials_json: legacyCredentials, token_json: legacyToken }]
  })
  const gmailApiCodeRows = provider.value === 'gmail_api_code'
    ? parseGmailApiCodeRows(form.gmail_api_code_pool_text || '')
    : []

  // 加载 async-select 字段的选项
  useEffect(() => {
    for (const field of fields) {
      if (field.type === 'async-select' && field.asyncUrl && !asyncOptions[field.key]) {
        setAsyncLoading(prev => ({ ...prev, [field.key]: true }))
        apiFetch(field.asyncUrl)
          .then((data: any) => {
            const valueKey = field.asyncValueKey || 'value'
            const labelKey = field.asyncLabelKey || 'label'
            // 支持多种响应格式
            let items: any[] = []
            if (Array.isArray(data)) items = data
            else if (data?.countries) items = data.countries
            else if (data?.services) items = data.services
            else if (data?.data) items = Array.isArray(data.data) ? data.data : []

            const options = items.map((item: any) => {
              if (typeof item === 'object') {
                const v = String(item[valueKey] ?? item.id ?? item.country ?? '')
                const l = String(item[labelKey] ?? item.name ?? item.title ?? item.eng ?? v)
                return { value: v, label: l ? `${l} (${v})` : v }
              }
              return { value: String(item), label: String(item) }
            }).filter(o => o.value)
            setAsyncOptions(prev => ({ ...prev, [field.key]: options }))
          })
          .catch(() => setAsyncOptions(prev => ({ ...prev, [field.key]: [] })))
          .finally(() => setAsyncLoading(prev => ({ ...prev, [field.key]: false })))
      }
    }
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  const handleSave = async () => {
    const config: Record<string, string> = {}
    const auth: Record<string, string> = {}
    for (const field of fields) {
      const value = provider.value === 'gmail_oauth_fission' && field.key === 'gmail_oauth_pool_json'
        ? serializeGmailPool(gmailMothers)
        : form[field.key] || ''
      if (field.category === 'auth') auth[field.key] = value
      else config[field.key] = form[field.key] || ''
    }
    setSaving(true)
    try {
      if (setting) {
        await apiFetch('/provider-settings', {
          method: 'PUT',
          body: JSON.stringify({
            id: setting.id, provider_type: providerType, provider_key: provider.value,
            display_name: setting.display_name || provider.label,
            auth_mode: setting.auth_mode || provider.default_auth_mode || '',
            enabled: true, is_default: setting.is_default, config, auth, metadata: {},
          }),
        })
      } else {
        await apiFetch('/provider-settings', {
          method: 'POST',
          body: JSON.stringify({
            provider_type: providerType, provider_key: provider.value,
            display_name: provider.label, auth_mode: provider.default_auth_mode || '',
            enabled: true, is_default: false, config, auth, metadata: {},
          }),
        })
      }
      invalidateConfigOptionsCache()
      setSaved(true)
      setTimeout(() => { onSaved(); onClose() }, 500)
    } catch (e) { console.error(e) } finally { setSaving(false) }
  }

  const handleTest = async () => {
    const config: Record<string, string> = {}
    const auth: Record<string, string> = {}
    for (const field of fields) {
      const value = provider.value === 'gmail_oauth_fission' && field.key === 'gmail_oauth_pool_json'
        ? serializeGmailPool(gmailMothers)
        : form[field.key] || ''
      if (field.category === 'auth') auth[field.key] = value
      else config[field.key] = form[field.key] || ''
    }
    setTesting(true)
    setTestResult(null)
    try {
      const result = await apiFetch('/provider-settings/test', {
        method: 'POST',
        body: JSON.stringify({
          provider_type: providerType,
          provider_key: provider.value,
          config, auth,
        }),
      })
      setTestResult(result)
    } catch (e: any) {
      setTestResult({ ok: false, error: e.message || t('providers.testRequestFailed') })
    } finally {
      setTesting(false)
    }
  }

  const handleGmailExchangeCode = async () => {
    setGmailOauthLoading(true)
    setGmailOauthResult(null)
    try {
      const result = await apiFetch('/provider-settings/gmail-oauth/exchange-code', {
        method: 'POST',
        body: JSON.stringify({
          credentials_json: gmailAuthMotherId
            ? gmailMothers.find(item => item.id === gmailAuthMotherId)?.credentials_json || ''
            : '',
          code: gmailAuthCode,
          code_verifier: gmailCodeVerifier,
        }),
      })
      if (!result.ok) {
        setGmailOauthResult({ ok: false, error: result.error || 'Gmail 授权码换 Token 失败' })
        return
      }
      setGmailMothers(items => items.map(item => item.id === gmailAuthMotherId ? { ...item, token_json: result.token_json || '' } : item))
      setGmailOauthResult({ ok: true, message: '已换取 Token 并回填，请保存配置。' })
    } catch (e: any) {
      setGmailOauthResult({ ok: false, error: e.message || 'Gmail 授权码换 Token 失败' })
    } finally {
      setGmailOauthLoading(false)
    }
  }

  const updateGmailMother = (id: string, patch: Partial<GmailMother>) => {
    setGmailMothers(items => items.map(item => item.id === id ? { ...item, ...patch } : item))
  }

  const handleGmailMotherAuthUrl = async (mother: GmailMother) => {
    setGmailOauthLoading(true)
    setGmailOauthResult(null)
    try {
      const result = await apiFetch('/provider-settings/gmail-oauth/auth-url', {
        method: 'POST',
        body: JSON.stringify({ credentials_json: mother.credentials_json || '', auto_callback: true }),
      })
      if (!result.ok) {
        setGmailOauthResult({ ok: false, error: result.error || '生成 Gmail 授权链接失败' })
        return
      }
      setGmailAuthMotherId(mother.id)
      setGmailCallbackSessionId(result.session_id || '')
      setGmailCodeVerifier(result.code_verifier || '')
      setGmailAuthCode('')
      window.open(result.url, '_blank', 'noopener,noreferrer')
      setGmailOauthResult({ ok: true, message: `已打开 ${mother.master_email || '该母号'} 的授权链接；授权成功后会自动回填 Token。` })
    } catch (e: any) {
      setGmailOauthResult({ ok: false, error: e.message || '生成 Gmail 授权链接失败' })
    } finally {
      setGmailOauthLoading(false)
    }
  }

  useEffect(() => {
    if (!gmailCallbackSessionId || !gmailAuthMotherId) return
    let stopped = false
    const timer = window.setInterval(async () => {
      try {
        const result = await apiFetch(`/provider-settings/gmail-oauth/callback-status/${gmailCallbackSessionId}`)
        if (!result.ok || stopped) return
        if (result.status === 'success') {
          setGmailMothers(items => items.map(item => item.id === gmailAuthMotherId ? { ...item, token_json: result.token_json || '' } : item))
          setGmailOauthResult({ ok: true, message: 'Gmail 授权成功，Token 已自动回填，请保存配置。' })
          setGmailCallbackSessionId('')
          window.clearInterval(timer)
        } else if (result.status === 'error') {
          setGmailOauthResult({ ok: false, error: result.message || 'Gmail 授权失败' })
          setGmailCallbackSessionId('')
          window.clearInterval(timer)
        } else if (!result.listener_ready) {
          setGmailOauthResult({ ok: true, message: '正在启动 127.0.0.1:53682 回调监听；如果稍后失败，可复制 code 手动换 Token。' })
        }
      } catch (e: any) {
        if (!stopped) setGmailOauthResult({ ok: false, error: e.message || '查询 Gmail 授权状态失败' })
      }
    }, 1500)
    return () => {
      stopped = true
      window.clearInterval(timer)
    }
  }, [gmailCallbackSessionId, gmailAuthMotherId])

  const handleGmailCredentialsFile = async (motherId: string, file?: File) => {
    if (!file) return
    try {
      const text = await file.text()
      JSON.parse(text)
      updateGmailMother(motherId, { credentials_json: text })
      setGmailOauthResult({ ok: true, message: 'credentials.json 已导入该母号。' })
    } catch (e: any) {
      setGmailOauthResult({ ok: false, error: e.message || 'credentials.json 文件不是合法 JSON' })
    }
  }

  const addGmailAlias = (motherId: string) => {
    setGmailMothers(items => items.map(item => {
      if (item.id !== motherId || item.aliases.length >= 5) return item
      return { ...item, aliases: [...item.aliases, ''] }
    }))
  }

  const updateGmailAlias = (motherId: string, index: number, value: string) => {
    setGmailMothers(items => items.map(item => {
      if (item.id !== motherId) return item
      const aliases = [...item.aliases]
      aliases[index] = value
      return { ...item, aliases }
    }))
  }

  const removeGmailAlias = (motherId: string, index: number) => {
    setGmailMothers(items => items.map(item => item.id === motherId
      ? { ...item, aliases: item.aliases.filter((_, i) => i !== index) }
      : item
    ))
  }

  return (
    <div className="dialog-backdrop" onClick={onClose}>
      <div
        className="dialog-panel dialog-panel-sm flex flex-col"
        style={provider.value === 'gmail_oauth_fission' ? { width: '50vw', maxWidth: '50vw' } : undefined}
        onClick={e => e.stopPropagation()}
      >
        <div className="flex items-center justify-between border-b border-[var(--border)] px-5 py-4">
          <div>
            <h2 className="text-base font-semibold text-[var(--text-primary)]">{provider.label}</h2>
            {provider.description && <p className="mt-0.5 text-xs text-[var(--text-muted)]">{provider.description}</p>}
          </div>
          <button onClick={onClose} className="text-[var(--text-muted)] hover:text-[var(--text-primary)]"><X className="h-4 w-4" /></button>
        </div>
        <div className="flex-1 overflow-y-auto px-5 py-4 space-y-4">
          {fields.length === 0 ? (
            <p className="text-sm text-[var(--text-muted)]">{t('providers.noConfig')}</p>
          ) : fields.map(field => {
            if (
              provider.value === 'gmail_oauth_fission'
              && ['gmail_oauth_master_email', 'gmail_oauth_pool_json', 'gmail_oauth_credentials_json', 'gmail_oauth_token_json'].includes(field.key)
            ) return null
            const sk = `${provider.value}:${field.key}`
            return (
              <div key={field.key}>
                <label className="mb-1.5 block text-sm font-medium text-[var(--text-secondary)]">{field.label}</label>
                {field.hint && <p className="mb-1.5 text-xs text-[var(--text-muted)]">{field.hint}</p>}
                <div className="relative">
                  {field.type === 'toggle' ? (
                    <div className="flex items-center gap-3">
                      <Toggle
                        checked={['true', '1', 'yes', 'on'].includes((form[field.key] || '').toLowerCase())}
                        onChange={v => setForm(f => ({ ...f, [field.key]: v ? 'true' : 'false' }))}
                      />
                      <span className="text-sm text-[var(--text-muted)]">
                        {['true', '1', 'yes', 'on'].includes((form[field.key] || '').toLowerCase()) ? t('providers.enabledState') : t('providers.disabledState')}
                      </span>
                    </div>
                  ) : field.type === 'async-select' ? (
                    asyncLoading[field.key] ? (
                      <div className="control-surface text-[var(--text-muted)] text-sm py-2">{t('common.loading')}</div>
                    ) : (
                      <SearchableSelect
                        value={form[field.key] || ''}
                        options={asyncOptions[field.key] || []}
                        placeholder={field.placeholder}
                        onChange={v => setForm(f => ({ ...f, [field.key]: v }))}
                      />
                    )
                  ) : field.type === 'select' && field.options?.length ? (
                    <select value={form[field.key] || ''} onChange={e => setForm(f => ({ ...f, [field.key]: e.target.value }))} className="control-surface appearance-none">
                      {field.options.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
                    </select>
                  ) : field.type === 'textarea' ? (
                    <textarea
                      value={form[field.key] || ''}
                      onChange={e => setForm(f => ({ ...f, [field.key]: e.target.value }))}
                      placeholder={field.placeholder || ''}
                      className="control-surface min-h-32 font-mono text-xs"
                      autoComplete="off"
                      data-1p-ignore
                      data-lpignore="true"
                    />
                  ) : (
                    <>
                      <input type={field.secret && !showSecret[sk] ? 'password' : 'text'} value={form[field.key] || ''}
                        onChange={e => setForm(f => ({ ...f, [field.key]: e.target.value }))}
                        placeholder={field.placeholder || ''} className="control-surface pr-9" autoComplete="new-password"
                        data-1p-ignore data-lpignore="true" />
                      {field.secret && (
                        <button onClick={() => setShowSecret(s => ({ ...s, [sk]: !s[sk] }))}
                          className="absolute right-2.5 top-1/2 -translate-y-1/2 text-[var(--text-muted)] hover:text-[var(--text-secondary)]">
                          {showSecret[sk] ? <EyeOff className="h-3.5 w-3.5" /> : <Eye className="h-3.5 w-3.5" />}
                        </button>
                      )}
                    </>
                  )}
                </div>
              </div>
            )
          })}
          {provider.value === 'gmail_api_code' && (
            <div className="rounded-xl border border-[var(--border-soft)] bg-[var(--bg-hover)] p-4">
              <div className="mb-3 flex items-center justify-between gap-3">
                <div>
                  <h3 className="text-sm font-semibold text-[var(--text-primary)]">已识别邮箱列表</h3>
                  <p className="mt-1 text-xs text-[var(--text-muted)]">
                    按“邮箱----接码链接”拆分；自动注册会使用左侧 Gmail，验证码从右侧链接轮询。
                  </p>
                </div>
                <span className="rounded-full border border-[var(--border-soft)] bg-[var(--bg-card)] px-2.5 py-1 text-xs text-[var(--text-muted)]">
                  {gmailApiCodeRows.length} 个
                </span>
              </div>
              {gmailApiCodeRows.length === 0 ? (
                <div className="rounded-lg border border-dashed border-[var(--border)] px-3 py-4 text-center text-xs text-[var(--text-muted)]">
                  暂未识别到有效记录，请按示例每行填写一个 Gmail 和接码链接。
                </div>
              ) : (
                <div className="max-h-56 space-y-2 overflow-y-auto">
                  {gmailApiCodeRows.map(row => (
                    <div key={row.email} className="grid gap-2 rounded-lg border border-[var(--border-soft)] bg-[var(--bg-card)] p-3 text-xs sm:grid-cols-[220px_1fr]">
                      <div className="font-medium text-[var(--text-primary)]">{row.email}</div>
                      <div className="break-all text-[var(--text-muted)]">{row.codeUrl}</div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}
          {provider.value === 'gmail_oauth_fission' && (
            <div className="rounded-xl border border-[var(--border-soft)] bg-[var(--bg-hover)] p-4">
              <div className="mb-5 rounded-lg border border-blue-500/20 bg-blue-500/10 p-3">
                <div className="mb-3 flex items-start justify-between gap-3">
                  <div>
                    <h3 className="text-sm font-semibold text-[var(--text-primary)]">开通母邮箱 Gmail API</h3>
                    <p className="mt-1 text-xs leading-5 text-[var(--text-muted)]">
                      先在 Google Cloud 给每个 Gmail 母号创建 OAuth Client，下载到的 JSON 文件就是下面要上传的 credentials.json。
                    </p>
                  </div>
                  <a
                    href="https://console.cloud.google.com/"
                    target="_blank"
                    rel="noreferrer"
                    className="shrink-0 rounded-lg border border-[var(--border-soft)] bg-[var(--bg-card)] px-3 py-2 text-xs font-medium text-[var(--text-secondary)] shadow-[var(--shadow-soft)] hover:text-[var(--text-primary)]"
                  >
                    打开控制台
                  </a>
                </div>
                <div className="grid gap-2 text-xs text-[var(--text-secondary)] sm:grid-cols-2">
                  {[
                    '1. 进入 Google Cloud Console，创建或选择一个项目。',
                    '2. APIs & Services -> Library，搜索并启用 Gmail API。',
                    '3. APIs & Services -> OAuth consent screen，配置同意屏幕。',
                    '4. 左侧点“目标对象”，直接正式发布项目。',
                    '5. APIs & Services -> Credentials，Create Credentials -> OAuth client ID。',
                    '6. 应用类型选择 Desktop app，创建后点击下载 JSON。',
                  ].map(step => (
                    <div key={step} className="rounded-lg border border-[var(--border-soft)] bg-[var(--bg-card)] px-3 py-2 leading-5">
                      {step}
                    </div>
                  ))}
                </div>
                <div className="mt-3 flex flex-wrap gap-2">
                  <a
                    href="https://console.cloud.google.com/apis/library/gmail.googleapis.com"
                    target="_blank"
                    rel="noreferrer"
                    className="rounded-full border border-blue-500/20 bg-[var(--bg-card)] px-3 py-1.5 text-xs font-medium text-blue-600 hover:bg-blue-500/10 dark:text-blue-300"
                  >
                    启用 Gmail API
                  </a>
                  <a
                    href="https://console.cloud.google.com/apis/credentials/consent"
                    target="_blank"
                    rel="noreferrer"
                    className="rounded-full border border-blue-500/20 bg-[var(--bg-card)] px-3 py-1.5 text-xs font-medium text-blue-600 hover:bg-blue-500/10 dark:text-blue-300"
                  >
                    同意屏幕 / 目标对象
                  </a>
                  <a
                    href="https://console.cloud.google.com/apis/credentials"
                    target="_blank"
                    rel="noreferrer"
                    className="rounded-full border border-blue-500/20 bg-[var(--bg-card)] px-3 py-1.5 text-xs font-medium text-blue-600 hover:bg-blue-500/10 dark:text-blue-300"
                  >
                    创建 OAuth Client
                  </a>
                </div>
              </div>
              <div className="mb-5 rounded-lg border border-[var(--border-soft)] bg-[var(--bg-card)] p-3">
                <div className="mb-3 flex items-center justify-between gap-3">
                  <div>
                    <h3 className="text-sm font-semibold text-[var(--text-primary)]">Gmail 母号池</h3>
                    <p className="mt-1 text-xs text-[var(--text-muted)]">添加多个 Gmail 母号；每个母号最多 5 个手动子号，使用总数达到 5 后自动跳过。</p>
                  </div>
                  <Button type="button" variant="outline" size="sm" onClick={() => setGmailMothers(items => [...items, newGmailMother()])}>
                    <Plus className="mr-1 h-3.5 w-3.5" /> 添加母号
                  </Button>
                </div>
                {gmailMothers.length === 0 ? (
                  <div className="rounded-lg border border-dashed border-[var(--border)] px-3 py-4 text-center text-xs text-[var(--text-muted)]">
                    暂无母号。点击“添加母号”后填写 Gmail 地址并上传 credentials.json。
                  </div>
                ) : (
                  <div className="space-y-3">
                    {gmailMothers.map((mother, index) => (
                      <div key={mother.id} className="rounded-lg border border-[var(--border-soft)] bg-[var(--bg-pane)] p-3">
                        <div className="mb-3 flex items-center justify-between gap-3">
                          <div className="text-sm font-medium text-[var(--text-primary)]">母号 {index + 1}</div>
                          <Button type="button" variant="ghost" size="sm" onClick={() => setGmailMothers(items => items.filter(item => item.id !== mother.id))}>
                            <Trash2 className="mr-1 h-3.5 w-3.5" /> 删除
                          </Button>
                        </div>
                        <div className="space-y-3">
                          <div>
                            <label className="mb-1.5 block text-xs font-medium text-[var(--text-secondary)]">Gmail 母号</label>
                            <input
                              type="text"
                              value={mother.master_email}
                              onChange={e => updateGmailMother(mother.id, { master_email: e.target.value })}
                              placeholder="your@gmail.com"
                              className="control-surface"
                              autoComplete="off"
                            />
                          </div>
                          <div className="grid gap-2 sm:grid-cols-2">
                            <label className="inline-flex cursor-pointer items-center justify-center rounded-lg border border-[var(--border-soft)] bg-[var(--bg-card)] px-3 py-2 text-xs font-medium text-[var(--text-secondary)] hover:text-[var(--text-primary)]">
                              上传 credentials.json
                              <input
                                type="file"
                                accept="application/json,.json"
                                className="hidden"
                                onChange={e => {
                                  handleGmailCredentialsFile(mother.id, e.target.files?.[0])
                                  e.currentTarget.value = ''
                                }}
                              />
                            </label>
                            <Button type="button" variant="outline" size="sm" onClick={() => handleGmailMotherAuthUrl(mother)} disabled={gmailOauthLoading || !mother.credentials_json.trim()}>
                              生成该母号授权链接
                            </Button>
                          </div>
                          <div className="grid gap-2 sm:grid-cols-2">
                            <div className={`rounded-lg px-3 py-2 text-xs ${mother.credentials_json.trim() ? 'bg-emerald-500/10 text-emerald-600 dark:text-emerald-400' : 'bg-[var(--bg-hover)] text-[var(--text-muted)]'}`}>
                              credentials: {mother.credentials_json.trim() ? '已导入' : '未导入'}
                            </div>
                            <div className={`rounded-lg px-3 py-2 text-xs ${mother.token_json.trim() ? 'bg-emerald-500/10 text-emerald-600 dark:text-emerald-400' : 'bg-[var(--bg-hover)] text-[var(--text-muted)]'}`}>
                              token: {mother.token_json.trim() ? '已授权' : '未授权'}
                            </div>
                          </div>
                          <div>
                            <div className="mb-2 flex items-center justify-between gap-3">
                              <label className="text-xs font-medium text-[var(--text-secondary)]">手动子号（最多 5 个）</label>
                              <Button type="button" variant="outline" size="sm" onClick={() => addGmailAlias(mother.id)} disabled={mother.aliases.length >= 5}>
                                <Plus className="mr-1 h-3.5 w-3.5" /> 添加子号
                              </Button>
                            </div>
                            {mother.aliases.length === 0 ? (
                              <div className="rounded-lg border border-dashed border-[var(--border)] px-3 py-2 text-xs text-[var(--text-muted)]">
                                未添加手动子号；注册时会自动随机生成 Gmail 别名。
                              </div>
                            ) : (
                              <div className="space-y-2">
                                {mother.aliases.map((alias, aliasIndex) => (
                                  <div key={`${mother.id}-${aliasIndex}`} className="flex gap-2">
                                    <input
                                      type="text"
                                      value={alias}
                                      onChange={e => updateGmailAlias(mother.id, aliasIndex, e.target.value)}
                                      placeholder={`${mother.master_email.split('@')[0] || 'your'}+001@gmail.com`}
                                      className="control-surface"
                                      autoComplete="off"
                                    />
                                    <Button type="button" variant="ghost" size="icon" onClick={() => removeGmailAlias(mother.id, aliasIndex)}>
                                      <Trash2 className="h-3.5 w-3.5" />
                                    </Button>
                                  </div>
                                ))}
                              </div>
                            )}
                          </div>
                        </div>
                      </div>
                    ))}
                  </div>
                )}
              </div>
              <div className="mb-3">
                <h3 className="text-sm font-semibold text-[var(--text-primary)]">Gmail OAuth 授权</h3>
                <p className="mt-1 text-xs text-[var(--text-muted)]">
                  对母号列表中的某个母号点击授权链接后，系统会监听 127.0.0.1:53682 回调并自动回填 Token；下方 code 输入仅作为自动回调失败时的兜底。
                </p>
              </div>
              <div className="space-y-3">
                <div className="rounded-lg border border-[var(--border-soft)] bg-[var(--bg-card)] px-3 py-2 text-xs text-[var(--text-muted)]">
                  当前授权母号：{gmailMothers.find(item => item.id === gmailAuthMotherId)?.master_email || '请先在上方某个母号点击“生成该母号授权链接”'}
                </div>
                <div>
                  <label className="mb-1.5 block text-sm font-medium text-[var(--text-secondary)]">授权码 code（兜底）</label>
                  <input
                    type="text"
                    value={gmailAuthCode}
                    onChange={e => setGmailAuthCode(e.target.value)}
                    placeholder="粘贴 redirect URL 中的 code 参数"
                    className="control-surface"
                    autoComplete="off"
                  />
                </div>
                <Button type="button" variant="outline" onClick={handleGmailExchangeCode} disabled={gmailOauthLoading || !gmailAuthMotherId || !gmailAuthCode.trim()} className="w-full">
                  用授权码换取 Token
                </Button>
                {gmailOauthResult && (
                  <div className={`rounded-lg px-3 py-2 text-xs ${
                    gmailOauthResult.ok
                      ? 'border border-emerald-500/20 bg-emerald-500/10 text-emerald-600 dark:text-emerald-400'
                      : 'border border-red-500/20 bg-red-500/10 text-red-600 dark:text-red-400'
                  }`}>
                    {gmailOauthResult.ok ? gmailOauthResult.message : gmailOauthResult.error}
                  </div>
                )}
              </div>
            </div>
          )}
        </div>
        {/* Test result */}
        {testResult && (
          <div className={`mx-5 rounded-lg px-3 py-2 text-xs ${
            testResult.ok
              ? 'border border-emerald-500/20 bg-emerald-500/10 text-emerald-600 dark:text-emerald-400'
              : 'border border-red-500/20 bg-red-500/10 text-red-600 dark:text-red-400'
          }`}>
            {testResult.ok ? testResult.message : testResult.error}
          </div>
        )}

        {/* Footer */}
        <div className="flex gap-2 border-t border-[var(--border)] px-5 py-3">
          <Button onClick={handleSave} disabled={saving} className="flex-1">
            <Save className="h-3.5 w-3.5 mr-1.5" />
            {saved ? `${t('common.saved')} ✓` : saving ? t('common.saving') : t('common.save')}
          </Button>
          <Button variant="outline" onClick={handleTest} disabled={testing || fields.length === 0} className="flex-1">
            <FlaskConical className="h-3.5 w-3.5 mr-1.5" />
            {testing ? t('providers.testing') : t('providers.testConnection')}
          </Button>
          <Button variant="outline" onClick={onClose}>{t('common.cancel')}</Button>
        </div>
      </div>
    </div>
  )
}

/* ------------------------------------------------------------------ */
/*  Main                                                               */
/* ------------------------------------------------------------------ */
type Props = {
  providerType: string
  catalog: ProviderOption[]
  settings: ProviderSetting[]
  onReload: () => Promise<void>
  onCreateCustom?: () => void
}

export default function ProviderCards({ providerType, catalog, settings, onReload, onCreateCustom }: Props) {
  const { t } = useI18n()
  const [editTarget, setEditTarget] = useState<{ provider: ProviderOption; setting: ProviderSetting | null } | null>(null)
  const [loading, setLoading] = useState<Record<string, boolean>>({})
  const [testResults, setTestResults] = useState<Record<string, { ok: boolean; message?: string; error?: string }>>({})
  const [testingKeys, setTestingKeys] = useState<Record<string, boolean>>({})

  const settingsMap: Record<string, ProviderSetting> = {}
  for (const s of settings) settingsMap[s.provider_key] = s
  const defaultKey = settings.find(s => s.is_default)?.provider_key || ''

  const grouped: Record<string, ProviderOption[]> = {}
  for (const p of catalog) {
    const cat = p.category || 'custom'
    if (!grouped[cat]) grouped[cat] = []
    grouped[cat].push(p)
  }

  const withLoading = async (key: string, fn: () => Promise<void>) => {
    setLoading(p => ({ ...p, [key]: true }))
    try { await fn() } finally { setLoading(p => ({ ...p, [key]: false })) }
  }

  const handleToggle = (provider: ProviderOption, enable: boolean) => withLoading(provider.value, async () => {
    const setting = settingsMap[provider.value]
    if (enable && !setting) {
      await apiFetch('/provider-settings', {
        method: 'POST',
        body: JSON.stringify({
          provider_type: providerType, provider_key: provider.value,
          display_name: provider.label, auth_mode: provider.default_auth_mode || '',
          enabled: true, is_default: settings.length === 0, config: {}, auth: {}, metadata: {},
        }),
      })
    } else if (!enable && setting) {
      await apiFetch(`/provider-settings/${setting.id}`, { method: 'DELETE' })
    }
    invalidateConfigOptionsCache()
    await onReload()
  })

  const handleSetDefault = (provider: ProviderOption) => withLoading(provider.value, async () => {
    const setting = settingsMap[provider.value]
    if (!setting) return
    await apiFetch('/provider-settings', {
      method: 'PUT',
      body: JSON.stringify({
        id: setting.id, provider_type: providerType, provider_key: provider.value,
        display_name: setting.display_name, auth_mode: setting.auth_mode,
        enabled: true, is_default: true, config: setting.config, auth: setting.auth, metadata: {},
      }),
    })
    invalidateConfigOptionsCache()
    await onReload()
  })

  const handleTestInline = async (provider: ProviderOption) => {
    const setting = settingsMap[provider.value]
    if (!setting) return
    const key = provider.value
    setTestingKeys(p => ({ ...p, [key]: true }))
    setTestResults(p => { const n = { ...p }; delete n[key]; return n })
    try {
      const result = await apiFetch('/provider-settings/test', {
        method: 'POST',
        body: JSON.stringify({
          provider_type: providerType,
          provider_key: key,
          config: setting.config || {},
          auth: setting.auth || {},
        }),
      })
      setTestResults(p => ({ ...p, [key]: result }))
    } catch (e: any) {
      setTestResults(p => ({ ...p, [key]: { ok: false, error: e.message || t('providers.testFailed') } }))
    } finally {
      setTestingKeys(p => ({ ...p, [key]: false }))
    }
  }

  const handleDelete = (provider: ProviderOption) => withLoading(provider.value, async () => {
    const setting = settingsMap[provider.value]
    if (!setting) return
    // Delete the setting
    await apiFetch(`/provider-settings/${setting.id}`, { method: 'DELETE' })
    // Delete the definition (only works for non-builtin)
    const def = catalog.find(p => p.value === provider.value)
    if (def && !def.is_builtin && (def as any).id) {
      try {
        await apiFetch(`/provider-definitions/${(def as any).id}`, { method: 'DELETE' })
      } catch {
        // definition delete may fail if it's builtin, ignore
      }
    }
    invalidateConfigOptionsCache()
    await onReload()
  })

  const renderCard = (provider: ProviderOption, allowDelete = false) => {
    const key = provider.value
    const setting = settingsMap[key]
    const isEnabled = !!setting
    const isDefault = key === defaultKey
    const hasFields = (provider.fields || []).length > 0

    return (
      <div key={key}>
        <div className="flex items-center gap-3 rounded-lg border border-[var(--border-soft)] bg-[var(--bg-card)] px-4 py-3">
          {/* Left: name + desc + badge */}
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2">
              <span className="text-sm font-medium text-[var(--text-primary)]">{provider.label}</span>
              {isDefault && <Badge variant="success">{t('providers.default')}</Badge>}
            </div>
            {provider.description && (
              <p className="mt-0.5 text-xs text-[var(--text-muted)] line-clamp-1">{provider.description}</p>
            )}
          </div>

          {/* Right: actions */}
          <div className="flex items-center gap-2 shrink-0">
            <button
              onClick={() => hasFields && isEnabled ? setEditTarget({ provider, setting }) : undefined}
              disabled={!hasFields || !isEnabled}
              className={`table-action-btn ${(!hasFields || !isEnabled) ? 'opacity-30 cursor-not-allowed' : ''}`}
            >
              <Pencil className="h-3 w-3 mr-1" /> {t('providers.edit')}
            </button>

            <button
              onClick={() => isEnabled ? handleTestInline(provider) : undefined}
              disabled={!isEnabled || testingKeys[key]}
              className={`table-action-btn ${!isEnabled ? 'opacity-30 cursor-not-allowed' : ''}`}
            >
              <FlaskConical className="h-3 w-3 mr-1" /> {testingKeys[key] ? t('providers.testing') : t('providers.test')}
            </button>

            <button
              onClick={() => isEnabled && !isDefault ? handleSetDefault(provider) : undefined}
              disabled={!isEnabled || isDefault || loading[key]}
              className={`table-action-btn ${(!isEnabled || isDefault) ? 'opacity-30 cursor-not-allowed' : ''}`}
            >
              {isDefault ? t('providers.defaultDone') : t('providers.setDefault')}
            </button>

            {allowDelete && (
              <button
                onClick={() => isEnabled ? handleDelete(provider) : undefined}
                disabled={!isEnabled || isDefault || loading[key]}
                className={`table-action-btn table-action-btn-danger ${(!isEnabled || isDefault) ? 'opacity-30 cursor-not-allowed' : ''}`}
              >
                <Trash2 className="h-3 w-3 mr-1" /> {t('common.delete')}
              </button>
            )}

            <Toggle
              checked={isEnabled}
              onChange={v => handleToggle(provider, v)}
              disabled={loading[key] || isDefault}
            />
          </div>
        </div>
        {/* Inline test result */}
        {testResults[key] && (
          <div className={`mt-1 rounded-lg px-3 py-2 text-xs ${
            testResults[key].ok
              ? 'border border-emerald-500/20 bg-emerald-500/10 text-emerald-600'
              : 'border border-red-500/20 bg-red-500/10 text-red-600'
          }`}>
            {testResults[key].ok ? testResults[key].message : testResults[key].error}
          </div>
        )}
      </div>
    )
  }

  return (
    <>
      <div className="space-y-6">
        {CATEGORY_GROUPS.map(({ key: cat, labelKey, descKey }) => {
          const providers = grouped[cat]
          if (!providers || providers.length === 0) return null

          // Hide "通用 HTTP 邮箱" from the list — it's the engine behind custom providers
          const visible = cat === 'custom'
            ? providers.filter(p => p.value !== 'generic_http_mailbox')
            : providers

          return (
            <div key={cat}>
              <div className="mb-2">
                <h3 className="text-sm font-semibold text-[var(--text-primary)]">{t(labelKey)}</h3>
                <p className="text-xs text-[var(--text-muted)]">{t(descKey)}</p>
              </div>
              <div className="space-y-1.5">
                {visible.map(p => renderCard(p, cat === 'custom'))}
                {cat === 'custom' && (
                  <button
                    className="flex w-full items-center justify-center gap-2 rounded-lg border border-dashed border-[var(--border)] px-4 py-3 text-sm text-[var(--text-muted)] transition-colors hover:border-[var(--accent)] hover:text-[var(--accent)]"
                    onClick={() => onCreateCustom?.()}
                  >
                    <Plus className="h-4 w-4" />
                    {t('providers.addCustom', {
                      type: providerType === 'mailbox'
                        ? t('providers.type.mailbox')
                        : providerType === 'captcha'
                          ? t('providers.type.captcha')
                          : providerType === 'sms'
                            ? t('providers.type.sms')
                            : '',
                    })}
                  </button>
                )}
              </div>
            </div>
          )
        })}
      </div>

      {editTarget && (
        <EditModal
          provider={editTarget.provider}
          setting={editTarget.setting}
          providerType={providerType}
          onClose={() => setEditTarget(null)}
          onSaved={onReload}
        />
      )}
    </>
  )
}
