import { useState, useEffect, useRef } from 'react'
import { apiFetch } from '@/lib/utils'
import { useI18n } from '@/lib/i18n-context'
import type { TranslationKey } from '@/lib/i18n'
import type { ProviderOption, ProviderSetting } from '@/lib/config-options'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Save, Eye, EyeOff, X, Pencil, Plus, Trash2, FlaskConical, Search, Clipboard, CheckCircle2 } from 'lucide-react'
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
  status: 'active' | 'deleted' | 'registered' | 'registered_exhausted' | 'invalid'
  deleted: boolean
}

type GmailApiCodeUsageItem = {
  parent_email: string
  mailbox_type?: string
  alias_limit?: number
  successful_alias_count: number
  allocated_only_count: number
  confirmed_remaining: number
  conservative_remaining: number
  email_status: string
  email_status_reason: string
  status: string
}

type ICloudHMEAccount = {
  id: string
  name: string
  real_email: string
  icloud_email: string
  app_password?: string
  host: string
  proxy?: string
  status: string
  alias_total: number
  alias_active: number
  last_validated?: string
  last_error?: string
  cookies_count?: number
  has_app_password?: boolean
}

type ICloudHMEAlias = {
  email: string
  anonymous_id?: string
  anonymousId?: string
  label?: string
  active?: boolean
  created_at?: string
  createdAt?: string
}

const API_CODE_MAILBOX_DEFAULT_LIMIT = 1
const GMAIL_API_CODE_DELETED_PREFIX = '# deleted '
const GMAIL_API_CODE_REGISTERED_EXHAUSTED_PREFIX = '# registered_exhausted '
const GMAIL_API_CODE_STATUS_RANK: Record<GmailApiCodeRow['status'], number> = {
  active: 0,
  registered: 1,
  registered_exhausted: 2,
  invalid: 3,
  deleted: 4,
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
    let line = rawLine.trim()
    if (!line) continue
    let status: GmailApiCodeRow['status'] = 'active'
    const marker = line.match(/^#\s*(deleted|registered_exhausted|registered|invalid|unavailable|unusable)\s+/i)
    if (marker) {
      const rawStatus = marker[1].toLowerCase()
      status = rawStatus === 'deleted'
        ? 'deleted'
        : rawStatus === 'registered_exhausted'
          ? 'registered_exhausted'
          : rawStatus === 'registered'
            ? 'registered'
          : 'invalid'
      line = line.slice(marker[0].length).trim()
    } else if (line.startsWith('#')) {
      continue
    }
    if (!line.includes('----')) continue
    const [emailPart, ...urlParts] = line.split('----')
    const email = emailPart.trim().toLowerCase()
    const codeUrl = urlParts.join('----').trim()
    if (!email || !email.includes('@') || !/^https?:\/\//i.test(codeUrl) || seen.has(email)) continue
    seen.add(email)
    rows.push({ email, codeUrl, status, deleted: status === 'deleted' })
  }
  return rows
}

function apiCodePoolEmail(email: string) {
  const target = String(email || '').trim().toLowerCase()
  const at = target.lastIndexOf('@')
  if (at <= 0) return target
  const local = target.slice(0, at).split('+', 1)[0]
  const rawDomain = target.slice(at + 1)
  const domain = rawDomain === 'googlemail.com' ? 'gmail.com' : rawDomain
  return `${local}@${domain}`
}

function gmailApiCodeFailureRate(item?: GmailApiCodeUsageItem) {
  const success = Number(item?.successful_alias_count || 0)
  const failed = Number(item?.allocated_only_count || 0)
  const total = success + failed
  return total > 0 ? failed / total : 0
}

function formatPercent(value: number) {
  return `${Math.round(value * 100)}%`
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
  const [asyncErrors, setAsyncErrors] = useState<Record<string, string>>({})
  const [gmailAuthCode, setGmailAuthCode] = useState('')
  const [gmailCodeVerifier, setGmailCodeVerifier] = useState('')
  const [gmailAuthMotherId, setGmailAuthMotherId] = useState('')
  const [gmailCallbackSessionId, setGmailCallbackSessionId] = useState('')
  const [gmailOauthLoading, setGmailOauthLoading] = useState(false)
  const [gmailOauthResult, setGmailOauthResult] = useState<{ ok: boolean; message?: string; error?: string } | null>(null)
  const [gmailApiCodeUsage, setGmailApiCodeUsage] = useState<Record<string, GmailApiCodeUsageItem>>({})
  const [icloudAccounts, setIcloudAccounts] = useState<ICloudHMEAccount[]>([])
  const [icloudAliases, setIcloudAliases] = useState<ICloudHMEAlias[]>([])
  const [icloudSelectedAccountId, setIcloudSelectedAccountId] = useState('')
  const [icloudAliasLabel, setIcloudAliasLabel] = useState('')
  const [icloudLoading, setIcloudLoading] = useState(false)
  const [icloudResult, setIcloudResult] = useState<{ ok: boolean; message?: string; error?: string } | null>(null)
  const [icloudAccountForm, setIcloudAccountForm] = useState({
    id: '',
    name: '',
    real_email: '',
    icloud_email: '',
    host: 'icloud.com',
    proxy: '',
    cookie_header: '',
    app_password: '',
  })
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
  const gmailApiCodeRowsSorted = provider.value === 'gmail_api_code'
    ? [...gmailApiCodeRows].sort((left, right) => {
      if (left.status !== right.status) return GMAIL_API_CODE_STATUS_RANK[left.status] - GMAIL_API_CODE_STATUS_RANK[right.status]
      const leftRate = gmailApiCodeFailureRate(gmailApiCodeUsage[apiCodePoolEmail(left.email)])
      const rightRate = gmailApiCodeFailureRate(gmailApiCodeUsage[apiCodePoolEmail(right.email)])
      if (leftRate !== rightRate) return leftRate - rightRate
      return left.email.localeCompare(right.email)
    })
    : []
  const gmailApiCodeUsableRowCount = gmailApiCodeRows.filter(row => {
    const usage = gmailApiCodeUsage[apiCodePoolEmail(row.email)]
    const registeredIcloudAvailable = (
      row.status === 'registered' || usage?.email_status === 'registered'
    ) && usage?.mailbox_type === 'icloud' && Number(usage?.conservative_remaining || 0) > 0
    return (row.status === 'active' || registeredIcloudAvailable)
      && usage?.email_status !== 'unusable'
      && usage?.email_status !== 'registered_exhausted'
  }).length

  const copyText = async (text: string) => {
    const value = String(text || '')
    if (!value) return
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(value)
        return
      }
    } catch {
      // Fallback below covers non-secure or restricted clipboard contexts.
    }
    const el = document.createElement('textarea')
    el.value = value
    document.body.appendChild(el)
    el.select()
    document.execCommand('copy')
    document.body.removeChild(el)
  }

  const buildProviderPayload = () => {
    const config: Record<string, string> = {}
    const auth: Record<string, string> = {}
    for (const field of fields) {
      const value = provider.value === 'gmail_oauth_fission' && field.key === 'gmail_oauth_pool_json'
        ? serializeGmailPool(gmailMothers)
        : form[field.key] || ''
      if (field.category === 'auth') auth[field.key] = value
      else config[field.key] = value
    }
    return { provider_type: providerType, provider_key: provider.value, config, auth }
  }

  const loadAsyncFieldOptions = async (field: any) => {
    if (!field.asyncUrl) return
    setAsyncLoading(prev => ({ ...prev, [field.key]: true }))
    setAsyncErrors(prev => ({ ...prev, [field.key]: '' }))
    try {
      const init = (field.asyncMethod || 'GET').toUpperCase() === 'POST'
        ? { method: 'POST', body: JSON.stringify(buildProviderPayload()) }
        : undefined
      const data = await apiFetch(field.asyncUrl, init)
      if (data?.ok === false) throw new Error(data.error || '加载选项失败')
      const valueKey = field.asyncValueKey || 'value'
      const labelKey = field.asyncLabelKey || 'label'
      let items: any[] = []
      if (Array.isArray(data)) items = data
      else if (data?.options) items = Array.isArray(data.options) ? data.options : []
      else if (data?.countries) items = data.countries
      else if (data?.services) items = data.services
      else if (data?.data) items = Array.isArray(data.data) ? data.data : []

      let options = items.map((item: any) => {
        if (typeof item === 'object') {
          const v = String(item[valueKey] ?? item.id ?? item.country ?? '')
          const l = String(item[labelKey] ?? item.name ?? item.title ?? item.eng ?? v)
          return { value: v, label: l ? (l.includes(`(${v})`) || l.includes(`（${v}）`) ? l : `${l} (${v})`) : v }
        }
        return { value: String(item), label: String(item) }
      }).filter(o => o.value)
      if (field.key === 'outlook_email_group_id') {
        options = [{ value: '', label: '不限制分组' }, ...options]
      }
      const current = String(form[field.key] || '')
      if (current && !options.some(o => o.value === current)) {
        options = [{ value: current, label: `当前值 ${current}` }, ...options]
      }
      setAsyncOptions(prev => ({ ...prev, [field.key]: options }))
    } catch (e: any) {
      const current = String(form[field.key] || '')
      setAsyncErrors(prev => ({ ...prev, [field.key]: e.message || '加载选项失败' }))
      setAsyncOptions(prev => ({ ...prev, [field.key]: current ? [{ value: current, label: `当前值 ${current}` }] : [] }))
    } finally {
      setAsyncLoading(prev => ({ ...prev, [field.key]: false }))
    }
  }

  // 加载 async-select 字段的选项
  useEffect(() => {
    for (const field of fields) {
      if (field.type === 'async-select' && field.asyncUrl && !asyncOptions[field.key]) {
        loadAsyncFieldOptions(field)
      }
    }
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (provider.value !== 'gmail_api_code') return
    let active = true
    apiFetch('/stats/gmail-api-code-alias-usage')
      .then((data: any) => {
        if (!active) return
        const byEmail: Record<string, GmailApiCodeUsageItem> = {}
        for (const item of Array.isArray(data?.items) ? data.items : []) {
          const email = String(item?.parent_email || '').trim().toLowerCase()
          if (email) byEmail[apiCodePoolEmail(email)] = item
        }
        setGmailApiCodeUsage(byEmail)
      })
      .catch(() => {
        if (active) setGmailApiCodeUsage({})
      })
    return () => { active = false }
  }, [provider.value])

  const loadIcloudAccounts = async () => {
    if (provider.value !== 'icloud_hme') return
    setIcloudLoading(true)
    try {
      const data = await apiFetch('/provider-settings/icloud-hme/accounts')
      const accounts = Array.isArray(data?.accounts) ? data.accounts : []
      setIcloudAccounts(accounts)
      setIcloudSelectedAccountId(current => current || accounts[0]?.id || '')
    } catch (e: any) {
      setIcloudResult({ ok: false, error: e.message || '加载 iCloud 账号失败' })
    } finally {
      setIcloudLoading(false)
    }
  }

  useEffect(() => {
    if (provider.value !== 'icloud_hme') return
    loadIcloudAccounts()
  }, [provider.value]) // eslint-disable-line react-hooks/exhaustive-deps

  const resetIcloudAccountForm = () => {
    setIcloudAccountForm({
      id: '',
      name: '',
      real_email: '',
      icloud_email: '',
      host: 'icloud.com',
      proxy: '',
      cookie_header: '',
      app_password: '',
    })
  }

  const editIcloudAccount = (account: ICloudHMEAccount) => {
    setIcloudAccountForm({
      id: account.id,
      name: account.name || '',
      real_email: account.real_email || '',
      icloud_email: account.icloud_email || '',
      host: account.host || 'icloud.com',
      proxy: account.proxy || '',
      cookie_header: '',
      app_password: account.app_password || '',
    })
  }

  const applyIcloudAccountResult = (account: ICloudHMEAccount, accounts?: ICloudHMEAccount[]) => {
    const nextAccounts = Array.isArray(accounts)
      ? accounts
      : icloudAccounts.map(item => item.id === account.id ? account : item)
    setIcloudAccounts(nextAccounts.some(item => item.id === account.id) ? nextAccounts : [...nextAccounts, account])
    setIcloudSelectedAccountId(account.id)
    setIcloudAccountForm(current => ({
      id: account.id,
      name: account.name || '',
      real_email: account.real_email || '',
      icloud_email: account.icloud_email || '',
      host: account.host || current.host || 'icloud.com',
      proxy: account.proxy || '',
      cookie_header: current.id === account.id || current.cookie_header.trim() ? current.cookie_header : '',
      app_password: account.app_password || (current.id === account.id ? current.app_password : ''),
    }))
  }

  const saveIcloudAccount = async () => {
    setIcloudLoading(true)
    setIcloudResult(null)
    try {
      const result = await apiFetch('/provider-settings/icloud-hme/accounts', {
        method: 'POST',
        body: JSON.stringify({
          ...icloudAccountForm,
          cookies: icloudAccountForm.cookie_header,
          validate: Boolean(icloudAccountForm.cookie_header.trim()),
        }),
      })
      const accounts = Array.isArray(result.accounts) ? result.accounts : []
      if (result.account) {
        applyIcloudAccountResult(result.account, accounts)
      } else {
        setIcloudAccounts(accounts)
        setIcloudSelectedAccountId(icloudSelectedAccountId)
      }
      if (result.ok === false || result.account?.status === 'error') {
        setIcloudResult({ ok: false, error: result.error || result.account?.last_error || 'iCloud 账号保存后仍异常' })
        return
      }
      setIcloudResult({ ok: true, message: 'iCloud 账号已保存。' })
      resetIcloudAccountForm()
    } catch (e: any) {
      setIcloudResult({ ok: false, error: e.message || '保存 iCloud 账号失败' })
    } finally {
      setIcloudLoading(false)
    }
  }

  const deleteIcloudAccount = async (accountId: string) => {
    if (!accountId) return
    setIcloudLoading(true)
    setIcloudResult(null)
    try {
      const result = await apiFetch(`/provider-settings/icloud-hme/accounts/${encodeURIComponent(accountId)}`, { method: 'DELETE' })
      const accounts = Array.isArray(result.accounts) ? result.accounts : []
      setIcloudAccounts(accounts)
      setIcloudSelectedAccountId(accounts[0]?.id || '')
      setIcloudAliases([])
      setIcloudResult({ ok: true, message: 'iCloud 账号已删除。' })
    } catch (e: any) {
      setIcloudResult({ ok: false, error: e.message || '删除 iCloud 账号失败' })
    } finally {
      setIcloudLoading(false)
    }
  }

  const validateIcloudAccount = async (accountId: string) => {
    setIcloudLoading(true)
    setIcloudResult(null)
    try {
      const account = icloudAccounts.find(item => item.id === accountId)
      const shouldUseForm = icloudAccountForm.id === accountId || Boolean(icloudAccountForm.cookie_header.trim())
      const result = await apiFetch(`/provider-settings/icloud-hme/accounts/${encodeURIComponent(accountId)}/validate`, {
        method: 'POST',
        body: JSON.stringify(shouldUseForm
          ? {
              ...icloudAccountForm,
              id: accountId,
              cookies: icloudAccountForm.cookie_header,
              host: icloudAccountForm.host || account?.host || 'icloud.com',
            }
          : {}),
      })
      if (result.account) applyIcloudAccountResult(result.account)
      else await loadIcloudAccounts()
      setIcloudResult(result.ok ? { ok: true, message: 'Cookie 校验成功。' } : { ok: false, error: result.error || 'Cookie 校验失败' })
    } catch (e: any) {
      setIcloudResult({ ok: false, error: e.message || '校验 iCloud 账号失败' })
    } finally {
      setIcloudLoading(false)
    }
  }

  const loadIcloudAliases = async (accountId: string = icloudSelectedAccountId) => {
    if (!accountId) return
    setIcloudLoading(true)
    setIcloudResult(null)
    try {
      const result = await apiFetch(`/provider-settings/icloud-hme/aliases?account_id=${encodeURIComponent(accountId)}`)
      setIcloudAliases(Array.isArray(result.aliases) ? result.aliases : [])
      setIcloudResult({ ok: true, message: `已加载 ${Number(result.count || 0)} 个隐私邮箱。` })
      await loadIcloudAccounts()
    } catch (e: any) {
      setIcloudResult({ ok: false, error: e.message || '加载 iCloud 隐私邮箱失败' })
    } finally {
      setIcloudLoading(false)
    }
  }

  const createIcloudAlias = async () => {
    if (!icloudSelectedAccountId) return
    setIcloudLoading(true)
    setIcloudResult(null)
    try {
      const result = await apiFetch('/provider-settings/icloud-hme/aliases', {
        method: 'POST',
        body: JSON.stringify({ account_id: icloudSelectedAccountId, label: icloudAliasLabel }),
      })
      setIcloudAliases(Array.isArray(result.aliases) ? result.aliases : [])
      setIcloudAliasLabel('')
      setIcloudResult({ ok: true, message: `已创建 ${result.alias?.email || '隐私邮箱'}。` })
      await loadIcloudAccounts()
    } catch (e: any) {
      setIcloudResult({ ok: false, error: e.message || '创建 iCloud 隐私邮箱失败' })
    } finally {
      setIcloudLoading(false)
    }
  }

  const icloudAliasAction = async (alias: ICloudHMEAlias, action: 'deactivate' | 'reactivate' | 'delete') => {
    const anonymousId = alias.anonymous_id || alias.anonymousId || ''
    if (!icloudSelectedAccountId || !anonymousId) return
    setIcloudLoading(true)
    setIcloudResult(null)
    try {
      const url = `/provider-settings/icloud-hme/aliases/${encodeURIComponent(anonymousId)}${action === 'delete' ? '' : `/${action}`}`
      const result = await apiFetch(url, {
        method: action === 'delete' ? 'DELETE' : 'POST',
        body: JSON.stringify({ account_id: icloudSelectedAccountId }),
      })
      if (Array.isArray(result.aliases)) setIcloudAliases(result.aliases)
      else await loadIcloudAliases(icloudSelectedAccountId)
      setIcloudResult({ ok: true, message: '隐私邮箱操作已完成。' })
      await loadIcloudAccounts()
    } catch (e: any) {
      setIcloudResult({ ok: false, error: e.message || '操作 iCloud 隐私邮箱失败' })
    } finally {
      setIcloudLoading(false)
    }
  }

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

  const handleDeleteGmailApiCodeRow = (email: string) => {
    const target = email.trim().toLowerCase()
    if (!target) return
    setForm(current => {
      const lines = String(current.gmail_api_code_pool_text || '').split(/\r?\n/)
      const nextLines = lines.map(line => {
        const trimmed = line.trim()
        if (!trimmed || /^#\s*deleted\s+/i.test(trimmed) || trimmed.startsWith('#') || !trimmed.includes('----')) {
          return line
        }
        const [emailPart] = trimmed.split('----')
        if (emailPart.trim().toLowerCase() !== target) return line
        return `${GMAIL_API_CODE_DELETED_PREFIX}${trimmed}`
      })
      return { ...current, gmail_api_code_pool_text: nextLines.join('\n') }
    })
  }

  const handleMarkGmailApiCodeRowExhausted = (email: string) => {
    const target = email.trim().toLowerCase()
    if (!target) return
    setForm(current => {
      const lines = String(current.gmail_api_code_pool_text || '').split(/\r?\n/)
      const nextLines = lines.map(line => {
        const trimmed = line.trim()
        const normalized = trimmed.replace(/^#\s*(registered_exhausted|registered)\s+/i, '')
        if (!normalized || !normalized.includes('----')) return line
        const [emailPart] = normalized.split('----')
        if (emailPart.trim().toLowerCase() !== target) return line
        return `${GMAIL_API_CODE_REGISTERED_EXHAUSTED_PREFIX}${normalized}`
      })
      return { ...current, gmail_api_code_pool_text: nextLines.join('\n') }
    })
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
        style={
          provider.value === 'gmail_api_code'
            ? { width: '80vw', maxWidth: '80vw' }
            : provider.value === 'icloud_hme'
              ? { width: '80vw', maxWidth: '80vw' }
            : provider.value === 'gmail_oauth_fission'
              ? { width: '50vw', maxWidth: '50vw' }
              : undefined
        }
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
            if (provider.value === 'icloud_hme' && field.key === 'icloud_hme_accounts_json') return null
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
                      <div className="flex gap-2">
                        <div className="min-w-0 flex-1">
                          <SearchableSelect
                            value={form[field.key] || ''}
                            options={asyncOptions[field.key] || []}
                            placeholder={field.placeholder}
                            onChange={v => setForm(f => ({ ...f, [field.key]: v }))}
                          />
                        </div>
                        <Button type="button" variant="outline" size="sm" onClick={() => loadAsyncFieldOptions(field)}>
                          刷新
                        </Button>
                      </div>
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
                {field.type === 'async-select' && asyncErrors[field.key] ? (
                  <p className="mt-1.5 text-xs text-amber-300">{asyncErrors[field.key]}</p>
                ) : null}
              </div>
            )
          })}
          {provider.value === 'icloud_hme' && (
            <div className="space-y-4">
              <div className="rounded-xl border border-[var(--border-soft)] bg-[var(--bg-hover)] p-4">
                <div className="mb-3 flex items-center justify-between gap-3">
                  <div>
                    <h3 className="text-sm font-semibold text-[var(--text-primary)]">iCloud 账号列表</h3>
                    <p className="mt-1 text-xs text-[var(--text-muted)]">
                      Cookie 用于管理 Hide My Email；App 专用密码用于 IMAP 收验证码，可选但推荐配置。
                    </p>
                  </div>
                  <Button type="button" variant="outline" size="sm" onClick={resetIcloudAccountForm}>
                    <Plus className="mr-1 h-3.5 w-3.5" /> 新增账号
                  </Button>
                </div>
                <div className="grid min-w-0 gap-3 lg:grid-cols-[1fr_1.25fr]">
                  <div className="min-w-0 space-y-2">
                    {icloudAccounts.length === 0 ? (
                      <div className="rounded-lg border border-dashed border-[var(--border)] px-3 py-6 text-center text-xs text-[var(--text-muted)]">
                        暂无 iCloud 账号。右侧填写账号名称和 Cookie 后保存。
                      </div>
                    ) : icloudAccounts.map(account => (
                      <div
                        key={account.id}
                        role="button"
                        tabIndex={0}
                        onClick={() => setIcloudSelectedAccountId(account.id)}
                        onKeyDown={e => {
                          if (e.key === 'Enter' || e.key === ' ') {
                            e.preventDefault()
                            setIcloudSelectedAccountId(account.id)
                          }
                        }}
                        className={`w-full min-w-0 rounded-lg border px-3 py-2 text-left transition-colors ${
                          icloudSelectedAccountId === account.id
                            ? 'border-[var(--accent)] bg-[var(--accent)]/10'
                            : 'border-[var(--border-soft)] bg-[var(--bg-card)] hover:bg-[var(--bg-pane)]'
                        }`}
                      >
                        <div className="flex min-w-0 items-center justify-between gap-2">
                          <div className="min-w-0 flex-1">
                            <div className="truncate text-sm font-medium text-[var(--text-primary)]">
                              {account.name || account.real_email || account.icloud_email || account.id}
                            </div>
                            <div className="mt-0.5 truncate text-xs text-[var(--text-muted)]">
                              {account.icloud_email || account.real_email || '未识别邮箱'} · {account.host || 'icloud.com'}
                            </div>
                          </div>
                          <Badge className="shrink-0" variant={account.status === 'active' ? 'success' : account.status === 'error' ? 'danger' : 'secondary'}>
                            {account.status === 'active' ? '可用' : account.status === 'error' ? '异常' : '待配置'}
                          </Badge>
                        </div>
                        <div className="mt-2 flex min-w-0 flex-wrap gap-2 text-[11px] text-[var(--text-muted)]">
                          <span>别名 {account.alias_active || 0}/{account.alias_total || 0}</span>
                          <span>Cookie {account.cookies_count || 0}</span>
                          <span>{account.has_app_password ? 'IMAP 已配置' : '未配 IMAP'}</span>
                        </div>
                        {account.last_error && (
                          <div className="mt-2 flex min-w-0 items-start gap-2 rounded-md bg-red-500/5 px-2 py-1.5 text-[11px] text-red-500">
                            <div className="max-h-16 min-w-0 flex-1 overflow-y-auto whitespace-pre-wrap break-all leading-relaxed">
                              {account.last_error}
                            </div>
                            <button
                              type="button"
                              title="复制完整异常"
                              aria-label="复制完整异常"
                              className="shrink-0 rounded-md p-1 text-red-500/80 transition-colors hover:bg-red-500/10 hover:text-red-600"
                              onClick={e => {
                                e.stopPropagation()
                                copyText(account.last_error || '')
                              }}
                            >
                              <Clipboard className="h-3.5 w-3.5" />
                            </button>
                          </div>
                        )}
                      </div>
                    ))}
                  </div>
                  <div className="rounded-lg border border-[var(--border-soft)] bg-[var(--bg-card)] p-3">
                    <div className="mb-3 text-sm font-medium text-[var(--text-primary)]">
                      {icloudAccountForm.id ? '编辑 iCloud 账号' : '新增 iCloud 账号'}
                    </div>
                    <div className="grid gap-3 sm:grid-cols-2">
                      <input className="control-surface" placeholder="显示名称" value={icloudAccountForm.name} onChange={e => setIcloudAccountForm(v => ({ ...v, name: e.target.value }))} />
                      <select className="control-surface" value={icloudAccountForm.host} onChange={e => setIcloudAccountForm(v => ({ ...v, host: e.target.value }))}>
                        <option value="icloud.com">icloud.com</option>
                        <option value="icloud.com.cn">icloud.com.cn</option>
                      </select>
                      <input className="control-surface" placeholder="Apple ID / 真实邮箱（可选）" value={icloudAccountForm.real_email} onChange={e => setIcloudAccountForm(v => ({ ...v, real_email: e.target.value }))} />
                      <input className="control-surface" placeholder="iCloud 邮箱（IMAP 用，可选）" value={icloudAccountForm.icloud_email} onChange={e => setIcloudAccountForm(v => ({ ...v, icloud_email: e.target.value }))} />
                      <input className="control-surface" placeholder="代理（可选）" value={icloudAccountForm.proxy} onChange={e => setIcloudAccountForm(v => ({ ...v, proxy: e.target.value }))} />
                      <input className="control-surface" placeholder="App 专用密码（可选）" type="text" value={icloudAccountForm.app_password} onChange={e => setIcloudAccountForm(v => ({ ...v, app_password: e.target.value }))} autoComplete="off" />
                    </div>
                    <textarea
                      className="control-surface mt-3 min-h-24 font-mono text-xs"
                      placeholder="Cookie Header 或 JSON；编辑账号时留空会保留旧 Cookie"
                      value={icloudAccountForm.cookie_header}
                      onChange={e => setIcloudAccountForm(v => ({ ...v, cookie_header: e.target.value }))}
                      data-1p-ignore
                      data-lpignore="true"
                    />
                    <div className="mt-3 flex flex-wrap gap-2">
                      <Button type="button" size="sm" onClick={saveIcloudAccount} disabled={icloudLoading || (!icloudAccountForm.id && !icloudAccountForm.cookie_header.trim())}>
                        <Save className="mr-1 h-3.5 w-3.5" /> 保存账号
                      </Button>
                      {icloudSelectedAccountId && (
                        <>
                          <Button type="button" variant="outline" size="sm" onClick={() => {
                            const account = icloudAccounts.find(item => item.id === icloudSelectedAccountId)
                            if (account) editIcloudAccount(account)
                          }}>
                            <Pencil className="mr-1 h-3.5 w-3.5" /> 编辑选中
                          </Button>
                          <Button type="button" variant="outline" size="sm" onClick={() => validateIcloudAccount(icloudSelectedAccountId)} disabled={icloudLoading}>
                            校验 Cookie
                          </Button>
                          <Button type="button" variant="ghost" size="sm" onClick={() => deleteIcloudAccount(icloudSelectedAccountId)} disabled={icloudLoading}>
                            <Trash2 className="mr-1 h-3.5 w-3.5" /> 删除账号
                          </Button>
                        </>
                      )}
                    </div>
                  </div>
                </div>
              </div>

              <div className="rounded-xl border border-[var(--border-soft)] bg-[var(--bg-hover)] p-4">
                <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
                  <div>
                    <h3 className="text-sm font-semibold text-[var(--text-primary)]">隐私邮箱列表</h3>
                    <p className="mt-1 text-xs text-[var(--text-muted)]">选择一个 iCloud 账号后加载、创建、停用、恢复或删除 Hide My Email 别名。</p>
                  </div>
                  <div className="flex flex-wrap items-center gap-2">
                    <select className="control-surface min-w-48" value={icloudSelectedAccountId} onChange={e => setIcloudSelectedAccountId(e.target.value)}>
                      <option value="">选择 iCloud 账号</option>
                      {icloudAccounts.map(account => (
                        <option key={account.id} value={account.id}>{account.name || account.icloud_email || account.real_email || account.id}</option>
                      ))}
                    </select>
                    <Button type="button" variant="outline" size="sm" onClick={() => loadIcloudAliases()} disabled={icloudLoading || !icloudSelectedAccountId}>
                      加载列表
                    </Button>
                  </div>
                </div>
                <div className="mb-3 flex gap-2">
                  <input
                    className="control-surface"
                    placeholder="新建别名标签（可选）"
                    value={icloudAliasLabel}
                    onChange={e => setIcloudAliasLabel(e.target.value)}
                  />
                  <Button type="button" onClick={createIcloudAlias} disabled={icloudLoading || !icloudSelectedAccountId}>
                    <Plus className="mr-1 h-3.5 w-3.5" /> 创建隐私邮箱
                  </Button>
                </div>
                {icloudAliases.length === 0 ? (
                  <div className="rounded-lg border border-dashed border-[var(--border)] px-3 py-6 text-center text-xs text-[var(--text-muted)]">
                    暂无已加载隐私邮箱。点击“加载列表”读取 iCloud 当前别名。
                  </div>
                ) : (
                  <div className="max-h-72 overflow-auto rounded-lg border border-[var(--border-soft)] bg-[var(--bg-card)]">
                    <table className="w-full min-w-[860px] text-left text-xs">
                      <thead className="sticky top-0 bg-[var(--bg-card)] text-[var(--text-muted)] shadow-[0_1px_0_var(--border-soft)]">
                        <tr>
                          <th className="px-3 py-2 font-medium">隐私邮箱</th>
                          <th className="px-3 py-2 font-medium">标签</th>
                          <th className="px-3 py-2 font-medium">状态</th>
                          <th className="px-3 py-2 font-medium">创建时间</th>
                          <th className="px-3 py-2 font-medium">操作</th>
                        </tr>
                      </thead>
                      <tbody className="divide-y divide-[var(--border-soft)]">
                        {icloudAliases.map(alias => {
                          const anonymousId = alias.anonymous_id || alias.anonymousId || alias.email
                          return (
                            <tr key={anonymousId}>
                              <td className="px-3 py-2 font-medium text-[var(--text-primary)]">{alias.email}</td>
                              <td className="px-3 py-2 text-[var(--text-secondary)]">{alias.label || '-'}</td>
                              <td className="px-3 py-2">
                                <Badge variant={alias.active ? 'success' : 'secondary'}>{alias.active ? '启用' : '停用'}</Badge>
                              </td>
                              <td className="px-3 py-2 text-[var(--text-muted)]">{alias.created_at || alias.createdAt || '-'}</td>
                              <td className="px-3 py-2">
                                <div className="flex flex-wrap gap-1">
                                  {alias.active ? (
                                    <Button type="button" variant="ghost" size="sm" onClick={() => icloudAliasAction(alias, 'deactivate')} disabled={icloudLoading}>停用</Button>
                                  ) : (
                                    <Button type="button" variant="ghost" size="sm" onClick={() => icloudAliasAction(alias, 'reactivate')} disabled={icloudLoading}>恢复</Button>
                                  )}
                                  <Button type="button" variant="ghost" size="sm" onClick={() => icloudAliasAction(alias, 'delete')} disabled={icloudLoading}>
                                    <Trash2 className="mr-1 h-3.5 w-3.5" /> 删除
                                  </Button>
                                </div>
                              </td>
                            </tr>
                          )
                        })}
                      </tbody>
                    </table>
                  </div>
                )}
                {icloudResult && (
                  <div className={`mt-3 flex min-w-0 items-start gap-2 rounded-lg px-3 py-2 text-xs ${icloudResult.ok ? 'border border-emerald-500/20 bg-emerald-500/10 text-emerald-600' : 'border border-red-500/20 bg-red-500/10 text-red-600'}`}>
                    <div className="min-w-0 flex-1 whitespace-pre-wrap break-all">
                      {icloudResult.message || icloudResult.error}
                    </div>
                    {!icloudResult.ok && (
                      <button
                        type="button"
                        title="复制完整异常"
                        aria-label="复制完整异常"
                        className="shrink-0 rounded-md p-1 text-red-500/80 transition-colors hover:bg-red-500/10 hover:text-red-600"
                        onClick={() => copyText(icloudResult.error || icloudResult.message || '')}
                      >
                        <Clipboard className="h-3.5 w-3.5" />
                      </button>
                    )}
                  </div>
                )}
              </div>
            </div>
          )}
          {provider.value === 'gmail_api_code' && (
            <div className="rounded-xl border border-[var(--border-soft)] bg-[var(--bg-hover)] p-4">
              <div className="mb-3 flex items-center justify-between gap-3">
                <div>
                  <h3 className="text-sm font-semibold text-[var(--text-primary)]">已识别邮箱列表</h3>
                  <p className="mt-1 text-xs text-[var(--text-muted)]">
                    按“邮箱----接码链接”拆分；自动注册会使用左侧 Gmail/iCloud 邮箱，验证码从右侧链接轮询。
                  </p>
                </div>
                <span className="rounded-full border border-[var(--border-soft)] bg-[var(--bg-card)] px-2.5 py-1 text-xs text-[var(--text-muted)]">
                  {gmailApiCodeUsableRowCount}/{gmailApiCodeRows.length} 个可用
                </span>
              </div>
              {gmailApiCodeRows.length === 0 ? (
                <div className="rounded-lg border border-dashed border-[var(--border)] px-3 py-4 text-center text-xs text-[var(--text-muted)]">
                  暂未识别到有效记录，请按示例每行填写一个 Gmail 或 iCloud 邮箱和接码链接。
                </div>
              ) : (
                <div className="max-h-56 overflow-auto rounded-lg border border-[var(--border-soft)] bg-[var(--bg-card)]">
                  <table className="w-full min-w-[980px] text-left text-xs">
                    <thead className="sticky top-0 bg-[var(--bg-card)] text-[var(--text-muted)] shadow-[0_1px_0_var(--border-soft)]">
                      <tr>
                        <th className="px-3 py-2 font-medium">邮箱</th>
                        <th className="px-3 py-2 font-medium">剩余额度</th>
                        <th className="px-3 py-2 font-medium">接码失败率</th>
                        <th className="px-3 py-2 font-medium">状态</th>
                        <th className="px-3 py-2 font-medium">接码链接</th>
                        <th className="px-3 py-2 font-medium">操作</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-[var(--border-soft)]">
                      {gmailApiCodeRowsSorted.map(row => {
                        const usage = gmailApiCodeUsage[apiCodePoolEmail(row.email)]
                        const success = Number(usage?.successful_alias_count || 0)
                        const failed = Number(usage?.allocated_only_count || 0)
                        const failureRate = gmailApiCodeFailureRate(usage)
                        const registeredIcloudAvailable = (
                          row.status === 'registered' || usage?.email_status === 'registered'
                        ) && usage?.mailbox_type === 'icloud' && Number(usage?.conservative_remaining || 0) > 0
                        const poolInactive = (
                          row.status !== 'active' && !registeredIcloudAvailable
                        ) || usage?.email_status === 'unusable' || usage?.email_status === 'registered_exhausted'
                        const limit = Math.max(1, Number(usage?.alias_limit || API_CODE_MAILBOX_DEFAULT_LIMIT))
                        const remaining = poolInactive ? 0 : Math.max(0, Number(usage?.conservative_remaining ?? (limit - success)))
                        const statusLabel = row.status === 'deleted'
                          ? '已删除'
                          : row.status === 'registered_exhausted' || usage?.email_status === 'registered_exhausted'
                            ? '已注册，子邮箱已耗尽'
                            : row.status === 'registered' || usage?.email_status === 'registered'
                              ? '已注册'
                            : row.status === 'invalid' || usage?.email_status === 'unusable'
                            ? '不可用'
                            : remaining <= 0
                              ? '已满'
                              : '可用'
                        const statusVariant = row.status === 'deleted'
                          ? 'secondary'
                          : row.status === 'registered_exhausted' || usage?.email_status === 'registered_exhausted'
                            ? 'warning'
                            : row.status === 'registered' || usage?.email_status === 'registered'
                              ? 'warning'
                              : row.status === 'invalid' || usage?.email_status === 'unusable' || remaining <= 0
                                ? 'danger'
                                : 'success'
                        return (
                          <tr key={`${row.status}-${row.email}`} className={poolInactive ? 'opacity-60' : ''}>
                            <td className="px-3 py-2 font-medium text-[var(--text-primary)]">{row.email}</td>
                            <td className="px-3 py-2 text-[var(--text-secondary)]">
                              {remaining}/{limit}
                              <span className="ml-1 text-[var(--text-muted)]">成功 {success}</span>
                            </td>
                            <td className="px-3 py-2 text-[var(--text-secondary)]">
                              {formatPercent(failureRate)}
                              <span className="ml-1 text-[var(--text-muted)]">({failed}/{success + failed || 0})</span>
                            </td>
                            <td className="px-3 py-2">
                              <div className="space-y-1">
                                <Badge variant={statusVariant as any}>{statusLabel}</Badge>
                                {row.status === 'active' && usage?.email_status_reason && usage.email_status !== 'usable' && (
                                  <div className="max-w-32 break-words text-[11px] text-[var(--text-muted)]">{usage.email_status_reason}</div>
                                )}
                              </div>
                            </td>
                            <td className="max-w-[360px] break-all px-3 py-2 text-[var(--text-muted)]">{row.codeUrl}</td>
                            <td className="px-3 py-2">
                              <div className="flex flex-wrap gap-1">
                                <Button
                                  type="button"
                                  variant="ghost"
                                  size="sm"
                                  onClick={() => handleMarkGmailApiCodeRowExhausted(row.email)}
                                  disabled={!['active', 'registered'].includes(row.status)}
                                >
                                  <CheckCircle2 className="mr-1 h-3.5 w-3.5" /> 已上限
                                </Button>
                                <Button
                                  type="button"
                                  variant="ghost"
                                  size="sm"
                                  onClick={() => handleDeleteGmailApiCodeRow(row.email)}
                                  disabled={row.status !== 'active'}
                                >
                                  <Trash2 className="mr-1 h-3.5 w-3.5" /> 删除
                                </Button>
                              </div>
                            </td>
                          </tr>
                        )
                      })}
                    </tbody>
                  </table>
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
  const defaultKey = settings.find(s => s.enabled && s.is_default)?.provider_key || ''

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
    } else if (setting) {
      await apiFetch('/provider-settings', {
        method: 'PUT',
        body: JSON.stringify({
          id: setting.id, provider_type: providerType, provider_key: provider.value,
          display_name: setting.display_name || provider.label,
          auth_mode: setting.auth_mode || provider.default_auth_mode || '',
          enabled: enable, is_default: enable && setting.is_default, config: setting.config || {}, auth: setting.auth || {}, metadata: setting.metadata || {},
        }),
      })
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
    const isEnabled = !!setting?.enabled
    const isDefault = key === defaultKey
    const hasFields = (provider.fields || []).length > 0

    return (
      <div key={key}>
        <div className="flex flex-col gap-3 rounded-lg border border-[var(--border-soft)] bg-[var(--bg-card)] px-4 py-3">
          {/* Left: name + desc + badge */}
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-sm font-medium text-[var(--text-primary)]">{provider.label}</span>
              {isDefault && <Badge variant="success">{t('providers.default')}</Badge>}
            </div>
            {provider.description && (
              <p className="mt-0.5 text-xs text-[var(--text-muted)] line-clamp-1">{provider.description}</p>
            )}
          </div>

          {/* Right: actions */}
          <div className="flex flex-wrap items-center gap-2">
            <button
              onClick={() => hasFields && isEnabled ? setEditTarget({ provider, setting }) : undefined}
              disabled={!hasFields || !isEnabled}
              className={`table-action-btn shrink-0 ${(!hasFields || !isEnabled) ? 'opacity-30 cursor-not-allowed' : ''}`}
            >
              <Pencil className="h-3 w-3 mr-1" /> {t('providers.edit')}
            </button>

            <button
              onClick={() => isEnabled ? handleTestInline(provider) : undefined}
              disabled={!isEnabled || testingKeys[key]}
              className={`table-action-btn shrink-0 ${!isEnabled ? 'opacity-30 cursor-not-allowed' : ''}`}
            >
              <FlaskConical className="h-3 w-3 mr-1" /> {testingKeys[key] ? t('providers.testing') : t('providers.test')}
            </button>

            <button
              onClick={() => isEnabled && !isDefault ? handleSetDefault(provider) : undefined}
              disabled={!isEnabled || isDefault || loading[key]}
              className={`table-action-btn shrink-0 ${(!isEnabled || isDefault) ? 'opacity-30 cursor-not-allowed' : ''}`}
            >
              {isDefault ? t('providers.defaultDone') : t('providers.setDefault')}
            </button>

            {allowDelete && (
              <button
                onClick={() => isEnabled ? handleDelete(provider) : undefined}
                disabled={!isEnabled || isDefault || loading[key]}
                className={`table-action-btn table-action-btn-danger shrink-0 ${(!isEnabled || isDefault) ? 'opacity-30 cursor-not-allowed' : ''}`}
              >
                <Trash2 className="h-3 w-3 mr-1" /> {t('common.delete')}
              </button>
            )}

            <Toggle
              checked={isEnabled}
              onChange={v => handleToggle(provider, v)}
              disabled={loading[key]}
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
