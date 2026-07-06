import { useEffect, useState, useRef, useCallback } from 'react'
import { createPortal } from 'react-dom'
import { useParams } from 'react-router-dom'
import { getConfig, getConfigOptions, getPlatforms } from '@/lib/app-data'
import type { ConfigOptionsResponse } from '@/lib/config-options'
import { getCaptchaStrategyLabel } from '@/lib/config-options'
import { apiDownload, apiFetch, triggerBrowserDownload, cn } from '@/lib/utils'
import { formatDateTime, translateAccountStatus } from '@/lib/i18n'
import { useI18n } from '@/lib/i18n-context'
import { buildExecutorOptions, buildRegistrationOptions, hasReusableOAuthBrowser, pickOAuthExecutor } from '@/lib/registration'
import { TaskLogPanel } from '@/components/tasks/TaskLogPanel'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { getTaskStatusText, TASK_STATUS_VARIANTS } from '@/lib/tasks'
import { RefreshCw, Copy, ExternalLink, Download, Upload, Plus, X, Mail, Trash2, Zap, Loader2, ShieldCheck, Search, ListChecks } from 'lucide-react'

const STATUS_VARIANT: Record<string, any> = {
  registered: 'default', authorized: 'success', rt_pending_upload: 'warning', rt_uploaded: 'success', trial: 'success', subscribed: 'success',
  expired: 'warning', invalid: 'danger', banned: 'danger',
  free: 'secondary', eligible: 'secondary', valid: 'success', unknown: 'secondary',
}

const platformActionsCache = new Map<string, any[]>()
const platformActionsPromiseCache = new Map<string, Promise<any[]>>()
const CHATGPT_K12_WORKSPACE_IDS_STORAGE_KEY = 'accounts.chatgpt.k12WorkspaceIds'

const BROWSER_MODE_OPTIONS = [
  { value: 'camoufox_headed', label: 'Camoufox Headed' },
  { value: 'camoufox_headless', label: 'Camoufox Headless' },
  { value: 'bitbrowser_headed', label: 'BitBrowser Headed' },
  { value: 'bitbrowser_hidden', label: 'BitBrowser Hidden' },
  { value: 'bitbrowser_headless', label: 'BitBrowser Headless' },
]

type GetRtSmsBalanceAction = 'auto_switch' | 'wait_release' | 'terminate'

const GET_RT_SMS_BALANCE_ACTION_OPTIONS: {
  value: GetRtSmsBalanceAction
  title: string
  desc: string
}[] = [
  {
    value: 'auto_switch',
    title: '\u81ea\u52a8\u5207\u6362\u5e73\u53f0',
    desc: '\u5f53\u524d\u63a5\u7801\u5e73\u53f0\u4f59\u989d\u4e0d\u8db3\u65f6\uff0c\u7acb\u5373\u5207\u6362\u5230\u4e0b\u4e00\u4e2a\u5df2\u542f\u7528\u63a5\u7801\u5e73\u53f0\u3002',
  },
  {
    value: 'wait_release',
    title: '\u7b49\u5f85\u91ca\u653e\u540e\u91cd\u8bd5',
    desc: '\u56fa\u5b9a\u5f53\u524d\u5e73\u53f0\u548c\u56fd\u5bb6\u7a77\u4e3e\uff0c\u6253\u6ee1\u624b\u673a\u53f7\u66f4\u6362\u6b21\u6570\u540e\u7b49\u5f85 10s\uff0c\u4ece\u5934\u767b\u5f55\u5e76\u518d\u6b21\u5c1d\u8bd5\u5f53\u524d\u56fd\u5bb6\u3002',
  },
  {
    value: 'terminate',
    title: '\u76f4\u63a5\u7ec8\u6b62\u4efb\u52a1',
    desc: '\u9047\u5230\u4f59\u989d\u4e0d\u8db3\u540e\u4e0d\u518d\u91cd\u8bd5\uff0c\u7acb\u5373\u7ed3\u675f\u76ee\u6807\u6a21\u5f0f\u4efb\u52a1\u3002',
  },
]

const ACCOUNT_TOOL_BUTTON_CLASS = 'h-8 shrink-0 whitespace-nowrap bg-transparent'
const ACCOUNT_STATUS_FILTER_OPTIONS = [
  'registered',
  'rt_pending_upload',
  'rt_uploaded',
  'subscribed',
  'eligible',
  'expired',
  'invalid',
  'banned',
]
const CHATGPT_BATCH_STATUS_OPTIONS = [
  'registered',
  'rt_pending_upload',
  'rt_uploaded',
  'trial',
  'subscribed',
  'expired',
  'invalid',
  'banned',
]

function getAccountOverview(acc: any) {
  return acc?.overview || {}
}

function getDisplaySummary(acc: any) {
  return acc?.display_summary && typeof acc.display_summary === 'object' ? acc.display_summary : {}
}

function getVerificationMailbox(acc: any) {
  const providerResources = Array.isArray(acc?.provider_resources) ? acc.provider_resources : []
  const normalized = providerResources.find((item: any) => item?.resource_type === 'mailbox')
  if (normalized) {
    return {
      provider: normalized.provider_name,
      email: normalized.handle || normalized.display_name,
      account_id: normalized.resource_identifier,
    }
  }
  return null
}

function getLifecycleStatus(acc: any) {
  return getDisplaySummary(acc)?.status?.lifecycle || acc?.lifecycle_status || 'registered'
}

function getDisplayStatus(acc: any) {
  return getDisplaySummary(acc)?.status?.display || acc?.display_status || acc?.plan_state || getLifecycleStatus(acc)
}

function isRegisteredOnlyAccount(acc: any) {
  return String(getDisplayStatus(acc) || '').trim().toLowerCase() === 'registered'
}

function isRtPendingUploadAccount(acc: any) {
  const status = String(getDisplayStatus(acc) || getLifecycleStatus(acc) || '').trim().toLowerCase()
  return status === 'rt_pending_upload'
}

function isGetRtTargetModeAccount(acc: any) {
  return isRegisteredOnlyAccount(acc) || isRtPendingUploadAccount(acc)
}

function normalizeGetRtSmsProviderKey(value: any) {
  const key = String(value || '').trim().toLowerCase()
  if (['smspool', 'smspool_api', 'sms_pool', 'sms_pool_api'].includes(key)) return 'smspool'
  if (['smsapi', 'sms_api'].includes(key)) return 'smsapi'
  return key
}

function appendUniqueProviderOption(
  options: { value: string; label: string }[],
  seen: Set<string>,
  option: { value: string; label: string },
) {
  if (!option.value || seen.has(option.value)) return
  seen.add(option.value)
  options.push(option)
}

function getPlanState(acc: any) {
  return getDisplaySummary(acc)?.status?.plan_state || acc?.plan_state || acc?.overview?.plan_state || 'unknown'
}

function getValidityStatus(acc: any) {
  return getDisplaySummary(acc)?.status?.validity || acc?.validity_status || acc?.overview?.validity_status || 'unknown'
}

function getCompactStatusMeta(acc: any) {
  const summary = getDisplaySummary(acc)
  const primaryMetrics = Array.isArray(summary?.primary_metrics) ? summary.primary_metrics : []
  if (primaryMetrics.length > 0) {
    return primaryMetrics.slice(0, 2).map((item: any) => {
      const sub = item?.sub ? ` · ${item.sub}` : ''
      return `${item?.label || ''}:${item?.value || '-'}${sub}`
    }).join(' / ')
  }
  const overview = getAccountOverview(acc)
  const parts = [
    `生命周期:${getLifecycleStatus(acc)}`,
    `套餐:${getPlanState(acc)}`,
    `有效:${getValidityStatus(acc)}`,
  ]
  const remainingCredits = overview?.remaining_credits
  const usageTotal = overview?.usage_total
  if (remainingCredits || usageTotal) {
    parts.push(`额度:${remainingCredits || '-'} / 已用:${usageTotal || '-'}`)
  }
  return parts.join(' / ')
}

function getPrimaryMetrics(acc: any) {
  const metrics = getDisplaySummary(acc)?.primary_metrics
  return Array.isArray(metrics) ? metrics : []
}

function getSecondaryMetrics(acc: any) {
  const metrics = getDisplaySummary(acc)?.secondary_metrics
  return Array.isArray(metrics) ? metrics : []
}

function getDisplayWarnings(acc: any) {
  const warnings = getDisplaySummary(acc)?.warnings
  return Array.isArray(warnings) ? warnings : []
}

function getDisplayBadges(acc: any) {
  const badges = getDisplaySummary(acc)?.badges
  return normalizeAccountBadges(acc, Array.isArray(badges) ? badges : [])
}

function isChatgptK12Account(acc: any) {
  if (String(acc?.platform || '').trim().toLowerCase() !== 'chatgpt') return false
  const overview = getAccountOverview(acc)
  const legacyExtra = overview?.legacy_extra && typeof overview.legacy_extra === 'object' ? overview.legacy_extra : {}
  if (!isEmptyPayload(overview?.k12_session) || !isEmptyPayload(overview?.k12?.session) || !isEmptyPayload(legacyExtra?.k12_session)) return true
  if (String(overview?.k12_workspace_id || legacyExtra?.k12_workspace_id || '').trim()) return true
  return getCredentials(acc).some((item: any) =>
    String(item?.key || '').trim().toLowerCase() === 'plan_type' &&
    String(item?.value || '').trim().toLowerCase() === 'k12',
  )
}

function normalizeAccountBadges(acc: any, badges: any[]) {
  if (!isChatgptK12Account(acc)) return badges
  let hasK12Badge = false
  let replacedFree = false
  const next = badges.map((badge: any) => {
    const label = String(badge?.label || '').trim()
    const lowerLabel = label.toLowerCase()
    if (lowerLabel === 'k12') {
      hasK12Badge = true
      return { ...badge, label: 'K12', tone: 'success' }
    }
    if (!replacedFree && lowerLabel === 'free') {
      replacedFree = true
      hasK12Badge = true
      return { ...badge, label: 'K12', tone: 'success' }
    }
    return badge
  })
  if (!hasK12Badge) {
    const mailboxBadgeIndex = next.findIndex((badge: any) => String(badge?.label || '').trim() === '邮箱验证')
    const k12Badge = { label: 'K12', tone: 'success' }
    if (mailboxBadgeIndex >= 0) next.splice(mailboxBadgeIndex, 0, k12Badge)
    else next.unshift(k12Badge)
  }
  return next
}

function getAccountBadgeClassName(badge: any, mode: 'detail' | 'modern' | 'legacy') {
  const tone = String(badge?.tone || '').trim().toLowerCase()
  const isK12 = String(badge?.label || '').trim().toLowerCase() === 'k12'
  const successClass = 'border-emerald-500/30 bg-emerald-500/15 text-emerald-700 dark:text-emerald-300'
  if (mode === 'detail') {
    return cn(
      'rounded-full border px-2 py-0.5 text-[11px]',
      tone === 'success' || isK12 ? successClass : 'border-[var(--border)] bg-[var(--bg-hover)] text-[var(--text-secondary)]',
    )
  }
  if (mode === 'modern') {
    return cn(
      'rounded border px-1.5 py-0.5 text-[10px] font-medium',
      tone === 'success' || isK12 ? successClass : 'border-[var(--border-soft)] bg-[var(--bg-pane)] text-[var(--text-secondary)]',
    )
  }
  return cn(
    'rounded border px-1 py-0.5 text-[11px] font-medium shadow-sm',
    tone === 'success' || isK12 ? successClass : 'border-[var(--border)]/50 bg-[var(--bg-pane)]/40 text-[var(--text-muted)]',
  )
}

function getDisplaySections(acc: any) {
  const sections = getDisplaySummary(acc)?.sections
  return Array.isArray(sections) ? sections : []
}

function getProviderAccounts(acc: any) {
  return Array.isArray(acc?.provider_accounts) ? acc.provider_accounts : []
}

function getCredentials(acc: any) {
  return Array.isArray(acc?.credentials) ? acc.credentials : []
}

function getCashierUrl(acc: any) {
  const overview = getAccountOverview(acc)
  return overview?.cashier_url || acc?.cashier_url || ''
}

function getPrimaryToken(acc: any) {
  if (acc?.primary_token) return acc.primary_token
  const credential = getCredentials(acc).find((item: any) => item?.scope === 'platform' && item?.credential_type === 'token' && item?.value)
  return credential?.value || ''
}

function isEmptyPayload(value: any) {
  return value == null || value === '' || (Array.isArray(value) && value.length === 0) || (typeof value === 'object' && !Array.isArray(value) && Object.keys(value).length === 0)
}

function getChatgptSessionPayload(acc: any) {
  const overview = getAccountOverview(acc)
  const legacyExtra = overview?.legacy_extra && typeof overview.legacy_extra === 'object' ? overview.legacy_extra : {}
  const candidates = [
    overview?.session,
    overview?.chatgpt_session,
    legacyExtra?.session,
    legacyExtra?.chatgpt_session,
  ]
  return candidates.find(item => !isEmptyPayload(item)) || null
}

function stringifyChatgptSessionPayload(payload: any) {
  if (isEmptyPayload(payload)) return ''
  if (typeof payload === 'string') {
    const text = payload.trim()
    if (!text) return ''
    try {
      return JSON.stringify(JSON.parse(text), null, 2)
    } catch {
      return text
    }
  }
  return JSON.stringify(payload, null, 2)
}

function getChatgptSessionText(acc: any) {
  return stringifyChatgptSessionPayload(getChatgptSessionPayload(acc))
}

function getChatgptK12SessionPayload(acc: any) {
  const overview = getAccountOverview(acc)
  const legacyExtra = overview?.legacy_extra && typeof overview.legacy_extra === 'object' ? overview.legacy_extra : {}
  const candidates = [
    overview?.k12_session,
    overview?.k12?.session,
    legacyExtra?.k12_session,
  ]
  return candidates.find(item => !isEmptyPayload(item)) || null
}

function getChatgptK12SessionText(acc: any) {
  return stringifyChatgptSessionPayload(getChatgptK12SessionPayload(acc))
}

async function writeClipboardText(text: string) {
  if (typeof navigator !== 'undefined' && navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(text)
    return
  }
  const el = document.createElement('textarea')
  el.value = text
  document.body.appendChild(el)
  el.select()
  document.execCommand('copy')
  document.body.removeChild(el)
}

function escapeCsvField(value: unknown) {
  const text = value == null ? '' : String(value)
  if (!/[",\n\r]/.test(text)) return text
  return `"${text.replace(/"/g, '""')}"`
}

async function loadPlatformActions(platform: string, options?: { force?: boolean }) {
  const key = String(platform || '').trim()
  if (!key) return []
  const force = Boolean(options?.force)
  if (!force && platformActionsCache.has(key)) {
    return platformActionsCache.get(key) || []
  }
  if (!force && platformActionsPromiseCache.has(key)) {
    return platformActionsPromiseCache.get(key) || []
  }
  const pending = apiFetch(`/actions/${key}`)
    .then((data) => {
      const actions = Array.isArray(data?.actions) ? data.actions : []
      platformActionsCache.set(key, actions)
      platformActionsPromiseCache.delete(key)
      return actions
    })
    .catch((error) => {
      platformActionsPromiseCache.delete(key)
      throw error
    })
  platformActionsPromiseCache.set(key, pending)
  return pending
}

function buildActionParamDraft(action: any, acc: any) {
  const params = Array.isArray(action?.params) ? action.params : []
  const emailPrefix = String(acc?.email || '').split('@')[0] || 'Development'
  const draft: Record<string, string> = {}
  params.forEach((param: any) => {
    if (action?.id === 'create_api_key' && param?.key === 'name') {
      draft[param.key] = `${emailPrefix}Development`
      return
    }
    if (action?.id === 'k12_join_upload' && param?.key === 'workspace_ids') {
      draft[param.key] = readStoredChatgptK12WorkspaceIds()
      return
    }
    if (Array.isArray(param?.options) && param.options.length > 0) {
      draft[param?.key || ''] = String(param.options[0] ?? '')
      return
    }
    draft[param?.key || ''] = ''
  })
  return draft
}

function readStoredChatgptK12WorkspaceIds() {
  if (typeof window === 'undefined') return ''
  try {
    return window.localStorage.getItem(CHATGPT_K12_WORKSPACE_IDS_STORAGE_KEY) || ''
  } catch {
    return ''
  }
}

function writeStoredChatgptK12WorkspaceIds(value: string) {
  if (typeof window === 'undefined') return
  try {
    const text = value.trim()
    if (text) {
      window.localStorage.setItem(CHATGPT_K12_WORKSPACE_IDS_STORAGE_KEY, text)
    } else {
      window.localStorage.removeItem(CHATGPT_K12_WORKSPACE_IDS_STORAGE_KEY)
    }
  } catch {
    // Ignore browsers that block localStorage.
  }
}

// ── 注册弹框 ────────────────────────────────────────────────
function RegisterModal({
  platform,
  platformMeta,
  onClose,
  onDone,
}: {
  platform: string
  platformMeta: any
  onClose: () => void
  onDone: () => void
}) {
  const { t, language } = useI18n()
  const [config, setConfig] = useState<any | null>(null)
  const [configOptions, setConfigOptions] = useState<ConfigOptionsResponse>({
    mailbox_providers: [],
    captcha_providers: [],
    mailbox_settings: [],
    captcha_settings: [],
    captcha_policy: {},
    executor_options: [],
    identity_mode_options: [],
    oauth_provider_options: [],
  })
  const [configLoading, setConfigLoading] = useState(true)
  const [regCount, setRegCount] = useState(1)
  const [concurrency, setConcurrency] = useState(1)
  // chatgpt 平台特定：注册成功后是否自动获取支付链接（保存到账号 cashier_url 字段，
  // 后续点"打开支付链接"直接复用）。仅当 platform === 'chatgpt' 时显示开关。
  const [autoPaymentLink, setAutoPaymentLink] = useState(false)
  const [remoteUploadEnabled, setRemoteUploadEnabled] = useState(false)
  const [k12BatchUploadEnabled, setK12BatchUploadEnabled] = useState(true)
  const [k12Join, setK12Join] = useState(false)
  const [k12WorkspaceIds, setK12WorkspaceIds] = useState(() => platform === 'chatgpt' ? readStoredChatgptK12WorkspaceIds() : '')
  const [authflowExperimental] = useState(false)
  const [recordHar, setRecordHar] = useState(false)
  const [registerPhoneChangeLimit, setRegisterPhoneChangeLimit] = useState(10)
  const [enableEmailAlias, setEnableEmailAlias] = useState(true)
  const [emailAliasLimit, setEmailAliasLimit] = useState(6)
  // GoPay 专属：PIN（6 位数字）、Hero-SMS API key、注册代理。仅当
  // platform === 'gopay' 时显示，未填时后端走环境变量回退。
  const [gopayPin, setGopayPin] = useState('147258')
  const [gopayApiKey, setGopayApiKey] = useState('')
  const [gopayProxy, setGopayProxy] = useState('')
  // Hero-SMS getNumber 价格上限（USD，小数）。0.011 ≈ 175 IDR，对 GoPay
  // service=ni 完全够，0 表示不限。
  const [gopayMaxPrice, setGopayMaxPrice] = useState('0.011')
  const [selection, setSelection] = useState({
    identityProvider: '',
    oauthProvider: '',
    executorType: '',
  })
  const [taskId, setTaskId] = useState<string | null>(null)
  const [done, setDone] = useState(false)
  const [starting, setStarting] = useState(false)

  const supportedExecutors: string[] = platformMeta?.supported_executors || []
  const registrationOptions = buildRegistrationOptions(platformMeta, language)
  const reusableBrowser = hasReusableOAuthBrowser(config || {})
  const executorOptions = buildExecutorOptions(
    selection.identityProvider,
    supportedExecutors,
    reusableBrowser,
    platformMeta?.supported_executor_options || [],
    language,
  )
  const selectedRegistration = registrationOptions.find(option =>
    option.identityProvider === selection.identityProvider && option.oauthProvider === selection.oauthProvider,
  )
  const selectedExecutor = executorOptions.find(option => option.value === selection.executorType)

  useEffect(() => {
    let active = true
    setConfigLoading(true)
    Promise.all([
      getConfig().catch(() => ({})),
      getConfigOptions().catch(() => null),
    ])
      .then(([cfg, options]) => {
        if (!active) return
        setConfig(cfg || {})
        if (options) {
          setConfigOptions(options)
        }
      })
      .catch(() => {
        if (!active) return
        setConfig({})
        setConfigOptions({
          mailbox_providers: [],
          captcha_providers: [],
          mailbox_settings: [],
          captcha_settings: [],
          captcha_policy: {},
          executor_options: [],
          identity_mode_options: [],
          oauth_provider_options: [],
        })
      })
      .finally(() => {
        if (active) setConfigLoading(false)
      })
    return () => { active = false }
  }, [])

  useEffect(() => {
    if (configLoading || registrationOptions.length === 0) return
    const cfg = config || {}
    const defaultRegistration = registrationOptions.find(option =>
      option.identityProvider === cfg.default_identity_provider &&
      (option.identityProvider !== 'oauth_browser' || option.oauthProvider === (cfg.default_oauth_provider || '')),
    ) || registrationOptions[0]
    setSelection((current) => {
      const identityProvider = current.identityProvider || defaultRegistration.identityProvider
      const oauthProvider = identityProvider === 'oauth_browser'
        ? (current.oauthProvider || defaultRegistration.oauthProvider)
        : ''
      const validExecutorOptions = buildExecutorOptions(
        identityProvider,
        supportedExecutors,
        hasReusableOAuthBrowser(cfg),
        platformMeta?.supported_executor_options || [],
        language,
      )
        .filter(option => !option.disabled)
      const preferredExecutor = identityProvider === 'oauth_browser'
        ? pickOAuthExecutor(supportedExecutors, cfg.default_executor || '', hasReusableOAuthBrowser(cfg))
        : ((cfg.default_executor && supportedExecutors.includes(cfg.default_executor)) ? cfg.default_executor : supportedExecutors[0] || '')
      const executorType = validExecutorOptions.some(option => option.value === current.executorType)
        ? current.executorType
        : (validExecutorOptions.find(option => option.value === preferredExecutor)?.value || validExecutorOptions[0]?.value || '')
      if (
        current.identityProvider === identityProvider &&
        current.oauthProvider === oauthProvider &&
        current.executorType === executorType
      ) {
        return current
      }
      return { identityProvider, oauthProvider, executorType }
    })
  }, [config, configLoading, registrationOptions, supportedExecutors])

  useEffect(() => {
    if (!selection.identityProvider) return
    const validExecutorOptions = buildExecutorOptions(
      selection.identityProvider,
      supportedExecutors,
      reusableBrowser,
      platformMeta?.supported_executor_options || [],
      language,
    )
      .filter(option => !option.disabled)
    if (!validExecutorOptions.some(option => option.value === selection.executorType)) {
      setSelection(current => {
        const nextExecutorType = validExecutorOptions[0]?.value || ''
        if (current.executorType === nextExecutorType) {
          return current
        }
        return {
          ...current,
          executorType: nextExecutorType,
        }
      })
    }
  }, [selection.identityProvider, selection.oauthProvider, selection.executorType, supportedExecutors, reusableBrowser])

  useEffect(() => {
    if (selection.executorType === 'protocol' && recordHar) {
      setRecordHar(false)
    }
  }, [selection.executorType, recordHar])

  const defaultMailboxProvider = (configOptions.mailbox_settings || []).find(item => item.is_default) || configOptions.mailbox_settings?.[0] || null

  const start = async () => {
    setStarting(true)
    try {
      const cfg = config || {}
      const extra: Record<string, any> = {
        identity_provider: selection.identityProvider,
        oauth_provider: selection.oauthProvider,
        oauth_email_hint: cfg.oauth_email_hint,
        chrome_user_data_dir: cfg.chrome_user_data_dir,
        chrome_cdp_url: cfg.chrome_cdp_url,
      }
      if (platform === 'chatgpt' && selection.executorType !== 'protocol') {
        extra.record_har = recordHar ? 'true' : ''
        extra.phone_change_limit = Math.max(Number(registerPhoneChangeLimit || 10), 1)
      }
      if (platform === 'chatgpt' && selection.executorType === 'protocol' && authflowExperimental) {
        extra.chatgpt_protocol_variant = 'authflow_experimental'
      }
      if (platform === 'chatgpt' && enableEmailAlias) {
        extra.enable_email_alias = true
        extra.email_alias_limit = Math.min(Math.max(Number(emailAliasLimit || 6), 1), 6)
      }
      if (selection.identityProvider === 'mailbox') {
        if (!defaultMailboxProvider?.provider_key) {
          throw new Error(t('accounts.missingDefaultMailbox'))
        }
        extra.mail_provider = defaultMailboxProvider.provider_key
      }
      // GoPay 专属：手机号接码注册需要 PIN / API key / 代理
      if (platform === 'gopay') {
        if (!gopayApiKey.trim()) {
          throw new Error('GoPay 注册必须填写 Hero-SMS API key')
        }
        if (!/^\d{6}$/.test(gopayPin.trim())) {
          throw new Error('GoPay PIN 必须是 6 位数字')
        }
        extra.herosms_api_key = gopayApiKey.trim()
        extra.gopay_pin = gopayPin.trim()
        if (gopayProxy.trim()) extra.gopay_proxy = gopayProxy.trim()
        // maxPrice 走 Hero-SMS getNumber 参数，单位 USD，0/空表示不限
        const mp = parseFloat((gopayMaxPrice || '').trim())
        if (!isNaN(mp) && mp >= 0) extra.herosms_max_price_usd = mp
      }
      // chatgpt + 勾选"注册完后获取支付链接"：注册成功后自动调
      // payment_link action 生成 cashier_url 并写回账号 extra。
      // ``auto_checkout: false`` 表示**只生成链接**不自动 checkout，因为
      // Accounts 页只是想拿到"打开支付链接"用，PayPal 自动化在 CtfGptPlus
      // 页面才走。
      if (platform === 'chatgpt' && autoPaymentLink) {
        extra.auto_chatgpt_plus_payment = true
        extra.chatgpt_payment = {
          plan: 'plus',
          country: 'ID',
          currency: 'IDR',
          auto_checkout: 'false',
          payment_method: 'paypal',
          headless: 'false',
          checkout_mode: 'protocol',
        }
      }
      if (platform === 'chatgpt' && k12Join) {
        extra.k12_join = true
        extra.k12_workspace_ids = k12WorkspaceIds.trim()
      }
      if (platform === 'chatgpt') {
        extra.remote_upload_enabled = remoteUploadEnabled
        extra.k12_batch_upload_enabled = k12BatchUploadEnabled
      }
      const res = await apiFetch('/tasks/register', {
        method: 'POST',
        body: JSON.stringify({
          platform, count: regCount, concurrency,
          executor_type: selection.executorType,
          captcha_solver: 'auto',
          proxy: null,
          extra,
        }),
      })
      setTaskId(res.task_id)
      if (platform === 'chatgpt') {
        writeStoredChatgptK12WorkspaceIds(k12WorkspaceIds)
      }
    } finally { setStarting(false) }
  }

  const handleDone = () => {
    setDone(true)
    onDone()
  }

  const dialog = (
    <div className="dialog-backdrop" onClick={!taskId ? onClose : undefined}>
      <div className="dialog-panel dialog-panel-md flex flex-col"
           onClick={e => e.stopPropagation()} style={{maxHeight: '88vh'}}>
        <div className="flex items-center justify-between px-6 py-4 border-b border-[var(--border)]">
          <h2 className="text-base font-semibold text-[var(--text-primary)]">{t('accounts.autoRegister')} {platformMeta?.display_name || platform}</h2>
          <button onClick={onClose} className="text-[var(--text-muted)] hover:text-[var(--text-primary)]"><X className="h-4 w-4" /></button>
        </div>
        <div className="px-6 py-4 flex-1 overflow-y-auto flex flex-col gap-5">
          {!taskId ? (
            configLoading ? (
              <div className="text-sm text-[var(--text-muted)]">{t('accounts.loadingRegistrationConfig')}</div>
            ) : (
              <>
                <div>
                  <div className="text-xs uppercase tracking-[0.16em] text-[var(--text-muted)]">Step 1</div>
                  <div className="mt-1 text-sm font-semibold text-[var(--text-primary)]">{t('accounts.selectIdentity')}</div>
                  <div className="mt-1 text-xs text-[var(--text-muted)]">{t('accounts.selectIdentityDesc')}</div>
                  <div className="mt-3 grid gap-3 md:grid-cols-2">
                    {registrationOptions.map(option => {
                      const active = selection.identityProvider === option.identityProvider && selection.oauthProvider === option.oauthProvider
                      return (
                        <button
                          key={option.key}
                          type="button"
                          onClick={() => setSelection(current => ({
                            ...current,
                            identityProvider: option.identityProvider,
                            oauthProvider: option.oauthProvider,
                          }))}
                          className={`rounded-xl border px-4 py-3 text-left transition-colors ${
                            active
                              ? 'border-[var(--accent)] bg-[var(--accent-soft)]'
                              : 'border-[var(--border)] bg-[var(--bg-pane)]/45 hover:border-[var(--accent)]/60'
                          }`}
                        >
                          <div className="flex items-center gap-2 text-sm font-medium text-[var(--text-primary)]">
                            {option.identityProvider === 'mailbox' ? <Mail className="h-4 w-4" /> : null}
                            {option.label}
                          </div>
                          <div className="mt-1 text-xs text-[var(--text-muted)]">{option.description}</div>
                        </button>
                      )
                    })}
                  </div>
                </div>

                <div>
                  <div className="text-xs uppercase tracking-[0.16em] text-[var(--text-muted)]">Step 2</div>
                  <div className="mt-1 text-sm font-semibold text-[var(--text-primary)]">{t('accounts.selectExecutor')}</div>
                  <div className="mt-1 text-xs text-[var(--text-muted)]">{t('accounts.selectExecutorDesc')}</div>
                  <div className="mt-3 grid gap-3 md:grid-cols-3">
                    {executorOptions.map(option => {
                      const active = selection.executorType === option.value
                      return (
                        <button
                          key={option.value}
                          type="button"
                          disabled={option.disabled}
                          onClick={() => !option.disabled && setSelection(current => ({ ...current, executorType: option.value }))}
                          className={`rounded-xl border px-4 py-3 text-left transition-colors ${
                            option.disabled
                              ? 'cursor-not-allowed border-[var(--border)] bg-[var(--bg-hover)] opacity-50'
                              : active
                                ? 'border-[var(--accent)] bg-[var(--accent-soft)]'
                                : 'border-[var(--border)] bg-[var(--bg-pane)]/45 hover:border-[var(--accent)]/60'
                          }`}
                        >
                          <div className="text-sm font-medium text-[var(--text-primary)]">{option.label}</div>
                          <div className="mt-1 text-xs text-[var(--text-muted)]">{option.description}</div>
                          {option.reason ? (
                            <div className="mt-2 text-xs text-amber-400">{option.reason}</div>
                          ) : null}
                        </button>
                      )
                    })}
                  </div>
                  {platform === 'chatgpt' && (
                    <div className="mt-3 grid gap-3 md:grid-cols-2">
                      <label
                        className={`flex items-start gap-2 rounded-xl border px-4 py-3 ${
                          selection.executorType === 'protocol'
                            ? 'cursor-not-allowed border-[var(--border)] bg-[var(--bg-pane)]/45 opacity-60'
                            : 'cursor-pointer border-[var(--border)] bg-[var(--bg-hover)] hover:border-[var(--accent)]/60'
                        }`}
                      >
                        <input
                          type="checkbox"
                          checked={recordHar}
                          onChange={event => setRecordHar(event.target.checked)}
                          disabled={selection.executorType === 'protocol'}
                          className="mt-0.5 h-4 w-4 cursor-pointer accent-[var(--accent)]"
                        />
                        <div className="flex-1 text-xs text-[var(--text-secondary)]">
                          <div className="text-sm font-medium text-[var(--text-primary)]">
                            {t('accounts.captureCamoufoxHar')}
                          </div>
                          <div className="mt-0.5">
                            {t('accounts.captureCamoufoxHarDesc')}
                          </div>
                          {selection.executorType === 'protocol' ? (
                            <div className="mt-2 text-[11px] text-amber-500">
                              {'\u9700\u8981\u5148\u5207\u6362\u5230 Camoufox / BitBrowser \u6d4f\u89c8\u5668\u6a21\u5f0f\uff0c\u534f\u8bae\u6a21\u5f0f\u4e0d\u652f\u6301 HAR \u5f55\u5236\u3002'}
                            </div>
                          ) : null}
                        </div>
                      </label>
                      <div className="rounded-xl border border-[var(--border-soft)] bg-[var(--bg-hover)] px-4 py-3">
                        <label className="mb-1 block text-xs font-medium text-[var(--text-secondary)]">
                          {t('accounts.phoneChangeLimit')}
                        </label>
                        <input
                          type="number"
                          min={1}
                          value={registerPhoneChangeLimit}
                          onChange={event =>
                            setRegisterPhoneChangeLimit(Math.max(Number(event.target.value || 10), 1))
                          }
                          className="control-surface control-surface-compact w-full text-center"
                        />
                        <div className="mt-1 help-text-xs">
                          {t('accounts.phoneChangeLimitHint')}
                        </div>
                      </div>
                    </div>
                  )}
                </div>

                <div className="grid grid-cols-2 gap-3">
                  <div>
                    <label className="text-xs text-[var(--text-muted)] block mb-1">{t('accounts.registrationCount')}</label>
                    <input type="number" min={1} max={99} value={regCount}
                      onChange={e => setRegCount(Number(e.target.value))}
                      className="control-surface control-surface-compact text-center" />
                  </div>
                  <div>
                    <label className="text-xs text-[var(--text-muted)] block mb-1">{t('accounts.concurrency')}</label>
                    <input type="number" min={1} max={5} value={concurrency}
                      onChange={e => setConcurrency(Number(e.target.value))}
                      className="control-surface control-surface-compact text-center" />
                  </div>
                </div>

                {platform === 'chatgpt' && (
                  <div className="grid gap-3 md:grid-cols-2">
                    <label className="flex items-start gap-2 rounded-xl border border-[var(--border-soft)] bg-[var(--bg-hover)] px-4 py-3 cursor-pointer hover:border-[var(--accent)]/60">
                      <input
                        type="checkbox"
                        checked={enableEmailAlias}
                        onChange={(e) => setEnableEmailAlias(e.target.checked)}
                        className="mt-0.5 h-4 w-4 cursor-pointer accent-[var(--accent)]"
                      />
                      <div className="flex-1 text-xs text-[var(--text-secondary)]">
                        <div className="text-sm font-medium text-[var(--text-primary)]">
                          {t('accounts.enableEmailAlias')}
                        </div>
                        <div className="mt-0.5">
                          {t('accounts.enableEmailAliasHint')}
                        </div>
                      </div>
                    </label>
                    <div className={`rounded-xl border border-[var(--border-soft)] bg-[var(--bg-hover)] px-4 py-3 ${enableEmailAlias ? '' : 'opacity-60'}`}>
                      <label className="mb-1 block text-xs font-medium text-[var(--text-secondary)]">
                        {t('accounts.emailAliasLimit')}
                      </label>
                      <input
                        type="number"
                        min={1}
                        max={6}
                        disabled={!enableEmailAlias}
                        value={emailAliasLimit}
                        onChange={(e) => setEmailAliasLimit(Math.min(Math.max(Number(e.target.value || 6), 1), 6))}
                        className="control-surface control-surface-compact w-full text-center"
                      />
                      <div className="mt-1 help-text-xs">
                        {t('accounts.emailAliasLimitHint')}
                      </div>
                    </div>
                  </div>
                )}

                {/* GoPay 专属：手机号接码 + PIN + 代理（platform === 'gopay'） */}
                {platform === 'gopay' && (
                  <div className="rounded-xl border border-[var(--border-soft)] bg-[var(--bg-hover)] px-4 py-3 space-y-3">
                    <div className="text-sm font-medium text-[var(--text-primary)]">GoPay 注册参数</div>
                    <div>
                      <label className="text-xs text-[var(--text-muted)] block mb-1">Hero-SMS API key（必填）</label>
                      <input
                        type="text"
                        value={gopayApiKey}
                        onChange={(e) => setGopayApiKey(e.target.value)}
                        placeholder="herosms 接码平台 API key"
                        className="control-surface control-surface-compact w-full"
                      />
                    </div>
                    <div className="grid grid-cols-2 gap-3">
                      <div>
                        <label className="text-xs text-[var(--text-muted)] block mb-1">PIN（6 位数字）</label>
                        <input
                          type="text"
                          maxLength={6}
                          value={gopayPin}
                          onChange={(e) => setGopayPin(e.target.value.replace(/\D/g, ''))}
                          placeholder="147258"
                          className="control-surface control-surface-compact w-full text-center font-mono"
                        />
                      </div>
                      <div>
                        <label className="text-xs text-[var(--text-muted)] block mb-1">接码价格上限（USD）</label>
                        <input
                          type="text"
                          value={gopayMaxPrice}
                          onChange={(e) => setGopayMaxPrice(e.target.value.replace(/[^0-9.]/g, ''))}
                          placeholder="0.011"
                          className="control-surface control-surface-compact w-full text-center font-mono"
                        />
                      </div>
                    </div>
                    <div>
                      <label className="text-xs text-[var(--text-muted)] block mb-1">注册代理（可选）</label>
                      <input
                        type="text"
                        value={gopayProxy}
                        onChange={(e) => setGopayProxy(e.target.value)}
                        placeholder="http://user:pass@host:port"
                        className="control-surface control-surface-compact w-full"
                      />
                    </div>
                    <div className="text-xs text-[var(--text-muted)]">
                      留空时分别回退到环境变量 OPAI_HEROSMS_API_KEY / OPAI_GOPAY_DEFAULT_PIN / OPAI_GOPAY_REGISTER_PROXY / OPAI_HEROSMS_MAX_PRICE_USD。maxPrice 设 0 不限价。
                    </div>
                  </div>
                )}

                {/* chatgpt 平台特定：强入 K12 空间（注册后跳过接码，取 session 上传 sub2api + 向 workspace 发加入申请） */}
                {platform === 'chatgpt' && (
                  <div className='rounded-xl border border-[var(--border-soft)] bg-[var(--bg-hover)] px-4 py-3'>
                    <label className='flex items-start gap-2 cursor-pointer'>
                      <input type='checkbox' checked={k12Join} onChange={(e) => setK12Join(e.target.checked)} className='mt-0.5 h-4 w-4 cursor-pointer accent-[var(--accent)]' />
                      <div className='flex-1 text-xs text-[var(--text-secondary)]'>
                        <div className='text-sm font-medium text-[var(--text-primary)]'>强入K12空间</div>
                        <div className='mt-0.5'>注册成功后跳过接码，直接请求 session -&gt; 向 Workspace 发加入申请 -&gt; 转 sub2api 格式上传到云端。需先在设置页配置 sub2api。</div>
                      </div>
                    </label>
                    <div className='mt-2'>
                      <label className='text-xs text-[var(--text-muted)] block mb-1'>母号 Workspace ID（逗号或换行分隔，留空则只上传 sub2api）</label>
                      <textarea
                        value={k12WorkspaceIds}
                        onChange={(e) => setK12WorkspaceIds(e.target.value)}
                        rows={3}
                        placeholder={'一行一个 UUID，或逗号分隔\nca0e29ed-a54c-42d9-a50b-2ba5e065296d'}
                        className='control-surface control-surface-compact w-full resize-y'
                      />
                    </div>
                  </div>
                )}

                {platform === 'chatgpt' && (
                  <label className="flex items-start gap-2 rounded-xl border border-[var(--border-soft)] bg-[var(--bg-hover)] px-4 py-3 cursor-pointer hover:border-[var(--accent)]/60">
                    <input
                      type="checkbox"
                      checked={k12BatchUploadEnabled}
                      onChange={(e) => setK12BatchUploadEnabled(e.target.checked)}
                      className="mt-0.5 h-4 w-4 cursor-pointer accent-[var(--accent)]"
                    />
                    <div className="flex-1 text-xs text-[var(--text-secondary)]">
                      <div className="text-sm font-medium text-[var(--text-primary)]">
                        是否打包上传
                      </div>
                      <div className="mt-0.5">
                        勾选后按当前流程在任务结束后合并 SUB2API JSON 并统一上传；未勾选则恢复为每个账号成功后逐个上传。仅在强入K12空间并启用远端上传时生效。
                      </div>
                    </div>
                  </label>
                )}

                {platform === 'chatgpt' && (
                  <label className="flex items-start gap-2 rounded-xl border border-[var(--border-soft)] bg-[var(--bg-hover)] px-4 py-3 cursor-pointer hover:border-[var(--accent)]/60">
                    <input
                      type="checkbox"
                      checked={remoteUploadEnabled}
                      onChange={(e) => setRemoteUploadEnabled(e.target.checked)}
                      className="mt-0.5 h-4 w-4 cursor-pointer accent-[var(--accent)]"
                    />
                    <div className="flex-1 text-xs text-[var(--text-secondary)]">
                      <div className="text-sm font-medium text-[var(--text-primary)]">
                        是否启用上传到远端
                      </div>
                      <div className="mt-0.5">
                        默认不上传远端，只在本地生成 data/sub2api 和 data/cpa JSON；勾选后按当前流程上传到远端。
                      </div>
                    </div>
                  </label>
                )}

                {platform === 'chatgpt' && (
                  <label className="flex items-start gap-2 rounded-xl border border-[var(--border-soft)] bg-[var(--bg-hover)] px-4 py-3 cursor-pointer hover:border-[var(--accent)]/60">
                    <input
                      type="checkbox"
                      checked={autoPaymentLink}
                      onChange={(e) => setAutoPaymentLink(e.target.checked)}
                      className="mt-0.5 h-4 w-4 cursor-pointer accent-[var(--accent)]"
                    />
                    <div className="flex-1 text-xs text-[var(--text-secondary)]">
                      <div className="text-sm font-medium text-[var(--text-primary)]">
                        注册成功后自动获取支付链接
                      </div>
                      <div className="mt-0.5">
                        生成 Plus 支付链接（不自动 checkout）并保存到账号，后续点"打开支付链接"直接复用。
                      </div>
                    </div>
                  </label>
                )}

                <div className="rounded-xl border border-[var(--border-soft)] bg-[var(--bg-hover)] px-4 py-3 text-xs text-[var(--text-secondary)]">
                  <div>{t('accounts.identitySummary')}: <span className="text-[var(--text-primary)]">{selectedRegistration?.label || '-'}</span></div>
                  <div className="mt-1">{t('accounts.executorSummary')}: <span className="text-[var(--text-primary)]">{selectedExecutor?.label || '-'}</span></div>
                  <div className="mt-1">{t('accounts.verificationSummary')}: <span className="text-[var(--text-primary)]">{getCaptchaStrategyLabel(selection.executorType, configOptions.captcha_policy, configOptions.captcha_providers, language)}</span></div>
                  {selection.identityProvider === 'oauth_browser' && !reusableBrowser && (
                    <div className="mt-2 text-amber-400">后台浏览器自动依赖 Chrome Profile 或 Chrome CDP，未配置时只允许可视浏览器自动。</div>
                  )}
                </div>

                <Button
                  onClick={start}
                  disabled={starting || !selection.identityProvider || !selection.executorType}
                  className="w-full"
                >
                  {starting ? t('accounts.starting') : t('accounts.startAutoRegister')}
                </Button>
              </>
            )
          ) : (
            <TaskLogPanel taskId={taskId} onDone={handleDone} />
          )}
        </div>
        <div className="px-6 py-3 border-t border-[var(--border)] flex justify-end">
          <Button variant="outline" size="sm" onClick={onClose}>
            {done ? t('common.close') : t('common.cancel')}
          </Button>
        </div>
      </div>
    </div>
  )

  return typeof document !== 'undefined' ? createPortal(dialog, document.body) : dialog
}

// ── 新增账号弹框 ─────────────────────────────────────────
function AddModal({ platform, onClose, onDone }: { platform: string; onClose: () => void; onDone: () => void }) {
  const [form, setForm] = useState({ email: '', password: '', lifecycle_status: 'registered', primary_token: '', cashier_url: '' })
  const [saving, setSaving] = useState(false)
  const set = (k: string, v: string) => setForm(f => ({ ...f, [k]: v }))

  const save = async () => {
    setSaving(true)
    try {
      await apiFetch('/accounts', {
        method: 'POST',
        body: JSON.stringify({ ...form, platform }),
      })
      onDone()
    } finally { setSaving(false) }
  }

  return (
    <div className="dialog-backdrop" onClick={onClose}>
      <div className="dialog-panel dialog-panel-sm"
           onClick={e => e.stopPropagation()}>
        <div className="flex items-center justify-between px-6 py-4 border-b border-[var(--border)]">
          <h2 className="text-base font-semibold text-[var(--text-primary)]">手动新增账号</h2>
          <button onClick={onClose} className="text-[var(--text-muted)] hover:text-[var(--text-primary)]"><X className="h-4 w-4" /></button>
        </div>
        <div className="px-6 py-4 space-y-3">
          {[['email','邮箱','text'],['password','密码','text'],['primary_token','主凭证','text'],['cashier_url','试用链接','text']].map(([k,l,t]) => (
            <div key={k}>
              <label className="text-xs text-[var(--text-muted)] block mb-1">{l}</label>
              <input type={t} value={(form as any)[k]} onChange={e => set(k, e.target.value)}
                className="control-surface" />
            </div>
          ))}
          <div>
            <label className="text-xs text-[var(--text-muted)] block mb-1">生命周期状态</label>
            <select value={form.lifecycle_status} onChange={e => set('lifecycle_status', e.target.value)}
              className="control-surface appearance-none">
              <option value="registered">仅注册</option>
              <option value="trial">试用中</option>
              <option value="subscribed">已订阅</option>
            </select>
          </div>
        </div>
        <div className="flex gap-3 px-6 py-4 border-t border-[var(--border)]">
          <Button onClick={save} disabled={saving} className="flex-1">{saving ? '保存中...' : '保存'}</Button>
          <Button variant="outline" onClick={onClose} className="flex-1">取消</Button>
        </div>
      </div>
    </div>
  )
}

function formatResultValue(value: any) {
  if (value === null || value === undefined || value === '') return '-'
  if (typeof value === 'boolean') return value ? '是' : '否'
  return String(value)
}

function ResultStat({ label, value }: { label: string; value: any }) {
  return (
    <div className="rounded-xl border border-[var(--border-soft)] bg-[var(--bg-hover)] px-3 py-2">
      <div className="text-[11px] uppercase tracking-[0.16em] text-[var(--text-muted)]">{label}</div>
      <div className="mt-1 text-sm font-medium text-[var(--text-primary)] break-all">{formatResultValue(value)}</div>
    </div>
  )
}

function metricToneClass(tone?: string) {
  if (tone === 'good') return 'border-emerald-500/25 bg-emerald-500/10 text-emerald-200'
  if (tone === 'warning') return 'border-amber-500/30 bg-amber-500/10 text-amber-700 dark:text-amber-200'
  if (tone === 'danger') return 'border-red-500/25 bg-red-500/10 text-red-200'
  return 'border-[var(--border)] bg-[var(--bg-hover)] text-[var(--text-primary)]'
}

function metricAccentClass(tone?: string) {
  if (tone === 'good') return 'from-emerald-400/70 to-cyan-300/50'
  if (tone === 'warning') return 'from-amber-300/80 to-orange-300/50'
  if (tone === 'danger') return 'from-red-400/80 to-rose-300/50'
  return 'from-[var(--accent)]/80 to-[var(--accent-strong)]/45'
}

function DisplayMetricCard({ metric, compact = false }: { metric: any; compact?: boolean }) {
  return (
    <div className={`group relative overflow-hidden rounded-lg border px-3.5 py-3 ${metricToneClass(metric?.tone)}`}>
      <div className={`pointer-events-none absolute inset-y-0 left-0 w-1 bg-gradient-to-b ${metricAccentClass(metric?.tone)}`} />
      <div className="relative flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="text-[10px] uppercase tracking-[0.18em] opacity-65">{metric?.label || '-'}</div>
          {metric?.sub ? <div className="mt-1 truncate text-[11px] opacity-65">{metric.sub}</div> : null}
        </div>
        <div className={`${compact ? 'text-sm' : 'text-lg'} shrink-0 font-semibold tracking-[-0.03em]`}>{formatResultValue(metric?.value)}</div>
      </div>
      {typeof metric?.percent === 'number' ? (
        <div className="relative mt-3 h-1.5 overflow-hidden rounded-full bg-black/25">
          <div className={`h-full rounded-full bg-gradient-to-r ${metricAccentClass(metric?.tone)}`} style={{ width: `${Math.max(0, Math.min(100, metric.percent))}%` }} />
        </div>
      ) : null}
    </div>
  )
}

function DisplayWarnings({ warnings }: { warnings: any[] }) {
  if (!warnings.length) return null
  return (
    <div className="space-y-2">
      {warnings.map((item: any, index: number) => (
        <div key={`${item?.key || 'warning'}-${index}`} className={`rounded-xl border px-3 py-2 text-xs ${metricToneClass(item?.tone || 'warning')}`}>
          {item?.message || '-'}
        </div>
      ))}
    </div>
  )
}

function DisplaySections({ sections }: { sections: any[] }) {
  if (!sections.length) return null
  return (
    <div className="space-y-3">
      {sections.map((section: any) => (
        <div key={section?.key || section?.title} className="rounded-xl border border-[var(--border-soft)] bg-[var(--bg-hover)] p-3">
          <div className="text-xs font-semibold text-[var(--text-primary)]">{section?.title || '明细'}</div>
          <div className="mt-3 grid gap-3 md:grid-cols-2">
            {(Array.isArray(section?.items) ? section.items : []).map((item: any, index: number) => (
              <div key={`${item?.title || 'item'}-${index}`} className="rounded-lg border border-[var(--border-soft)] bg-black/20 p-3">
                <div className="text-xs font-semibold text-[var(--text-primary)]">{item?.title || '-'}</div>
                <div className="mt-2 grid grid-cols-2 gap-2 text-xs text-[var(--text-secondary)]">
                  {(Array.isArray(item?.metrics) ? item.metrics : []).map((metric: any) => (
                    <div key={metric?.key || metric?.label}>
                      <span className="text-[var(--text-muted)]">{metric?.label || '-'}: </span>
                      <span>{formatResultValue(metric?.value)}</span>
                    </div>
                  ))}
                </div>
              </div>
            ))}
          </div>
        </div>
      ))}
    </div>
  )
}

function ActionResultHighlights({ payload }: { payload: any }) {
  if (!payload || typeof payload !== 'object') return null

  const stats: Array<{ label: string; value: any }> = []
  if ('valid' in payload) stats.push({ label: '账号有效', value: payload.valid })
  if (payload.membership_type) stats.push({ label: '套餐', value: payload.membership_type })
  if (payload.plan) stats.push({ label: '套餐', value: payload.plan })
  if (payload.plan_id) stats.push({ label: 'Plan ID', value: payload.plan_id })
  if (typeof payload.has_valid_payment_method === 'boolean') stats.push({ label: '已绑卡', value: payload.has_valid_payment_method })
  if ('trial_eligible' in payload) stats.push({ label: '可试用', value: payload.trial_eligible })
  if (payload.trial_length_days) stats.push({ label: '试用天数', value: payload.trial_length_days })
  if (payload.remaining_credits) stats.push({ label: '剩余额度', value: payload.remaining_credits })
  if (payload.usage_total) stats.push({ label: '已用额度', value: payload.usage_total })
  if (payload.plan_credits) stats.push({ label: '总额度', value: payload.plan_credits })
  if (payload.usage_summary?.plan_title) stats.push({ label: 'Kiro 套餐', value: payload.usage_summary.plan_title })
  if ('days_until_reset' in (payload.usage_summary || {})) stats.push({ label: '重置倒计时', value: payload.usage_summary?.days_until_reset })
  if (payload.usage_summary?.next_reset_at) stats.push({ label: '下次重置', value: payload.usage_summary.next_reset_at })
  if ('available' in (payload.portal_session || {})) stats.push({ label: 'Portal 可用', value: payload.portal_session?.available })
  if (payload.desktop_app_state?.app_name) stats.push({ label: '桌面应用', value: payload.desktop_app_state?.app_name })
  if ('running' in (payload.desktop_app_state || {})) stats.push({ label: '桌面已打开', value: payload.desktop_app_state?.running })
  if ('ready' in (payload.desktop_app_state || {})) stats.push({ label: '桌面就绪', value: payload.desktop_app_state?.ready })
  if (payload.key_prefix) stats.push({ label: 'API Key 前缀', value: payload.key_prefix })
  if (payload.key_prefix && payload.name) stats.push({ label: 'Key 名称', value: payload.name })
  if (payload.key_prefix && payload.id) stats.push({ label: 'Key ID', value: payload.id })

  const cursorModels = payload.usage_summary?.models && typeof payload.usage_summary.models === 'object'
    ? Object.entries(payload.usage_summary.models)
    : []
  const kiroBreakdowns = Array.isArray(payload.usage_summary?.breakdowns)
    ? payload.usage_summary.breakdowns
    : []
  const kiroPlans = Array.isArray(payload.usage_summary?.plans)
    ? payload.usage_summary.plans
    : []

  if (stats.length === 0 && cursorModels.length === 0 && kiroBreakdowns.length === 0 && kiroPlans.length === 0 && !payload.quota_note) {
    return null
  }

  return (
    <div className="space-y-4 mb-4">
      {stats.length > 0 && (
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
          {stats.map(item => <ResultStat key={item.label} label={item.label} value={item.value} />)}
        </div>
      )}

      {cursorModels.length > 0 && (
        <div className="rounded-xl border border-[var(--border-soft)] bg-[var(--bg-hover)] p-4">
          <div className="text-sm font-semibold text-[var(--text-primary)]">Cursor Usage</div>
          <div className="mt-3 grid gap-3 md:grid-cols-2">
            {cursorModels.map(([model, info]: [string, any]) => (
              <div key={model} className="rounded-lg border border-[var(--border-soft)] bg-black/20 p-3">
                <div className="text-xs font-semibold text-[var(--text-primary)]">{model}</div>
                <div className="mt-2 grid grid-cols-2 gap-2 text-xs text-[var(--text-secondary)]">
                  <div>请求数: {formatResultValue(info?.num_requests)}</div>
                  <div>总请求: {formatResultValue(info?.num_requests_total)}</div>
                  <div>Token: {formatResultValue(info?.num_tokens)}</div>
                  <div>剩余请求: {formatResultValue(info?.remaining_requests)}</div>
                  <div>请求上限: {formatResultValue(info?.max_request_usage)}</div>
                  <div>Token 上限: {formatResultValue(info?.max_token_usage)}</div>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {kiroBreakdowns.length > 0 && (
        <div className="rounded-xl border border-[var(--border-soft)] bg-[var(--bg-hover)] p-4">
          <div className="text-sm font-semibold text-[var(--text-primary)]">Kiro Usage</div>
          <div className="mt-3 grid gap-3 md:grid-cols-2">
            {kiroBreakdowns.map((item: any, index: number) => (
              <div key={`${item.resource_type || item.display_name}-${index}`} className="rounded-lg border border-[var(--border-soft)] bg-black/20 p-3">
                <div className="text-xs font-semibold text-[var(--text-primary)]">{item.display_name || item.resource_type}</div>
                <div className="mt-2 grid grid-cols-2 gap-2 text-xs text-[var(--text-secondary)]">
                  <div>已用: {formatResultValue(item.current_usage)}</div>
                  <div>上限: {formatResultValue(item.usage_limit)}</div>
                  <div>剩余: {formatResultValue(item.remaining_usage)}</div>
                  <div>单位: {formatResultValue(item.unit)}</div>
                  <div>试用状态: {formatResultValue(item.trial_status)}</div>
                  <div>试用到期: {formatResultValue(item.trial_expiry)}</div>
                  <div>试用上限: {formatResultValue(item.trial_usage_limit)}</div>
                  <div>试用剩余: {formatResultValue(item.trial_remaining_usage)}</div>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {kiroPlans.length > 0 && (
        <div className="rounded-xl border border-[var(--border-soft)] bg-[var(--bg-hover)] p-4">
          <div className="text-sm font-semibold text-[var(--text-primary)]">Kiro Plans</div>
          <div className="mt-3 grid gap-3 md:grid-cols-2">
            {kiroPlans.map((plan: any) => (
              <div key={plan.name} className="rounded-lg border border-[var(--border-soft)] bg-black/20 p-3">
                <div className="flex items-center justify-between gap-3">
                  <div className="text-xs font-semibold text-[var(--text-primary)]">{plan.title || plan.name}</div>
                  <div className="text-xs text-emerald-400">{formatResultValue(plan.amount)} {plan.currency || ''}</div>
                </div>
                <div className="mt-1 text-[11px] text-[var(--text-muted)]">{plan.billing_interval || '-'}</div>
                {Array.isArray(plan.features) && plan.features.length > 0 && (
                  <div className="mt-2 text-xs text-[var(--text-secondary)] break-words">
                    {plan.features.join(' · ')}
                  </div>
                )}
              </div>
            ))}
          </div>
        </div>
      )}

      {payload.quota_note && (
        <div className="notice-panel notice-panel-warning px-4 py-3 text-xs">
          {payload.quota_note}
        </div>
      )}
    </div>
  )
}

function ActionResultModal({
  title,
  payload,
  onClose,
}: {
  title: string
  payload: any
  onClose: () => void
}) {
  const content = typeof payload === 'string' ? payload : JSON.stringify(payload, null, 2)

  return (
    <div className="dialog-backdrop" onClick={onClose}>
      <div
        className="dialog-panel dialog-panel-lg"
        onClick={e => e.stopPropagation()}
      >
        <div className="flex items-center justify-between px-6 py-4 border-b border-[var(--border)]">
          <div>
            <h2 className="text-base font-semibold text-[var(--text-primary)]">{title}</h2>
            <p className="text-xs text-[var(--text-muted)] mt-0.5">操作结果</p>
          </div>
          <div className="flex items-center gap-2">
            <Button variant="outline" size="sm" onClick={() => navigator.clipboard.writeText(content)}>
              <Copy className="h-4 w-4 mr-1" />
              复制
            </Button>
            <button onClick={onClose} className="text-[var(--text-muted)] hover:text-[var(--text-primary)]">
              <X className="h-4 w-4" />
            </button>
          </div>
        </div>
        <div className="px-6 py-4">
          <ActionResultHighlights payload={payload} />
          <pre className="bg-[var(--bg-hover)] border border-[var(--border)] rounded-xl p-4 text-xs text-[var(--text-secondary)] whitespace-pre-wrap break-all overflow-auto max-h-[65vh]">
            {content}
          </pre>
        </div>
      </div>
    </div>
  )
}

function ActionTaskModal({
  title,
  taskId,
  taskStatus,
  onClose,
  onDone,
}: {
  title: string
  taskId: string
  taskStatus: string | null
  onClose: () => void
  onDone: (status: string) => void
}) {
  const { t } = useI18n()
  return (
    <TaskLogDialog
      title={title}
      taskId={taskId}
      taskStatus={taskStatus}
      onClose={onClose}
      onDone={onDone}
      footerLabel={t('taskHistory.taskId')}
    />
  )
}

function BatchStatusModal({
  count,
  value,
  submitting,
  language,
  onChange,
  onClose,
  onSubmit,
}: {
  count: number
  value: string
  submitting: boolean
  language: any
  onChange: (value: string) => void
  onClose: () => void
  onSubmit: () => void
}) {
  const { t } = useI18n()
  const statusLabel = translateAccountStatus(value, language)

  return (
    <div className="dialog-backdrop" onClick={() => !submitting && onClose()}>
      <div
        className="dialog-panel dialog-panel-md flex flex-col overflow-hidden rounded-xl border-[var(--border-soft)] bg-[var(--bg-card)] shadow-[var(--shadow-hard)]"
        onClick={event => event.stopPropagation()}
        style={{ width: 'min(520px, calc(100vw - 32px))', maxHeight: 'min(560px, calc(100dvh - 48px))' }}
      >
        <div className="shrink-0 flex items-center justify-between border-b border-[var(--border-soft)] bg-[var(--bg-elevated)] px-6 py-4">
          <div>
            <h2 className="text-base font-semibold text-[var(--text-primary)]">
              {'\u6279\u91cf\u4fee\u6539\u8d26\u53f7\u72b6\u6001'}
            </h2>
            <div className="mt-1 text-xs help-text">
              {t('accounts.selected', { count })}
            </div>
          </div>
          <button
            onClick={() => !submitting && onClose()}
            className="text-[var(--text-muted)] hover:text-[var(--text-primary)]"
          >
            <X className="h-4 w-4" />
          </button>
        </div>
        <div className="min-h-0 flex-1 space-y-4 overflow-y-auto bg-[var(--bg-base)] px-6 py-5">
          <div>
            <label className="mb-1 block text-xs font-medium text-[var(--text-secondary)]">
              {'\u76ee\u6807\u72b6\u6001'}
            </label>
            <select
              value={value}
              onChange={event => onChange(event.target.value)}
              className="control-surface control-surface-compact w-full"
              disabled={submitting}
            >
              {CHATGPT_BATCH_STATUS_OPTIONS.map(status => (
                <option key={status} value={status}>
                  {translateAccountStatus(status, language)}
                </option>
              ))}
            </select>
          </div>
          <div className="notice-panel notice-panel-info px-4 py-3 text-xs">
            {'\u5c06\u9009\u4e2d\u8d26\u53f7\u624b\u52a8\u8c03\u6574\u4e3a\u201c'}{statusLabel}{'\u201d\u3002\u8be5\u64cd\u4f5c\u53ea\u4fee\u6539\u5217\u8868\u72b6\u6001\u548c\u8fd0\u884c\u6458\u8981\uff0c\u4e0d\u5220\u9664\u5df2\u4fdd\u5b58\u7684 token / rt\u3002'}
          </div>
          {value === 'registered' ? (
            <div className="notice-panel notice-panel-warning px-4 py-3 text-xs">
              {'\u6539\u56de\u201c'}{statusLabel}{'\u201d\u65f6\u4f1a\u6e05\u7406 rt \u4e0a\u4f20\u72b6\u6001\u3001rt \u83b7\u53d6\u65f6\u95f4\u7b49\u8fd0\u884c\u6458\u8981\uff0c\u4f46\u4e0d\u4f1a\u5220\u9664\u8d26\u53f7\u51ed\u636e\u3002'}
            </div>
          ) : null}
        </div>
        <div className="shrink-0 flex justify-end gap-2 border-t border-[var(--border-soft)] bg-[var(--bg-elevated)] px-6 py-3">
          <Button
            variant="outline"
            size="sm"
            onClick={onClose}
            disabled={submitting}
          >
            {t('common.cancel')}
          </Button>
          <Button
            size="sm"
            onClick={onSubmit}
            disabled={submitting || count === 0 || !value}
          >
            {submitting ? (
              <Loader2 className="mr-2 h-4 w-4 animate-spin" />
            ) : (
              <ListChecks className="mr-2 h-4 w-4" />
            )}
            {'\u786e\u8ba4\u4fee\u6539'}
          </Button>
        </div>
      </div>
    </div>
  )
}

function TaskLogDialog({
  title,
  taskId,
  taskStatus,
  onClose,
  onDone,
  footerLabel,
}: {
  title: string
  taskId: string
  taskStatus?: string | null
  onClose: () => void
  onDone: (status: string) => void
  footerLabel?: string
}) {
  const { t, language } = useI18n()
  return (
    <div className="dialog-backdrop" onClick={onClose}>
      <div
        className="dialog-panel dialog-panel-md flex flex-col overflow-hidden rounded-xl border-[var(--border-soft)] bg-[var(--bg-card)] shadow-[var(--shadow-hard)]"
        onClick={e => e.stopPropagation()}
        style={{ width: 'min(860px, calc(100vw - 32px))', height: 'min(760px, calc(100dvh - 48px))' }}
      >
        <div className="relative shrink-0 overflow-hidden border-b border-[var(--border-soft)] bg-[var(--bg-elevated)] px-6 py-4">
          <div className="relative flex items-start justify-between gap-4">
            <div className="min-w-0">
              <div className="mb-2 inline-flex rounded-full bg-[rgba(var(--accent-rgb),0.1)] px-3 py-0.5 text-[10px] font-semibold uppercase tracking-[0.14em] text-[var(--accent)]">
                {t('taskLog.platformAction')}
              </div>
              <h2 className="truncate text-[16px] font-bold text-[var(--text-primary)]">{title}</h2>
              <p className="mt-1 text-[12px] font-medium text-[var(--text-secondary)]">{t('taskLog.dialogSubtitle')}</p>
            </div>
            <div className="flex items-center gap-2">
              {taskStatus ? (
                <Badge variant={TASK_STATUS_VARIANTS[taskStatus] || 'secondary'}>
                  {getTaskStatusText(taskStatus, language)}
                </Badge>
              ) : null}
              <button onClick={onClose} className="rounded-full border border-transparent bg-[var(--bg-pane)] p-2 text-[var(--text-secondary)] hover:text-[var(--text-primary)]">
                <X className="h-4 w-4" />
              </button>
            </div>
          </div>
        </div>
        <div className="min-h-0 flex-1 overflow-hidden bg-[var(--bg-base)] px-6 py-5">
          <TaskLogPanel taskId={taskId} onDone={onDone} />
        </div>
        <div className="flex shrink-0 items-center justify-between border-t border-[var(--border-soft)] bg-[var(--bg-elevated)] px-6 py-3 text-xs font-medium text-[var(--text-secondary)]">
          <span className="min-w-0 truncate">{footerLabel || t('taskHistory.taskId')}: {taskId}</span>
          <Button variant="outline" size="sm" onClick={onClose}>
            {t('common.close')}
          </Button>
        </div>
      </div>
    </div>
  )
}

function ActionParamsModal({
  action,
  initialValues,
  submitting,
  onClose,
  onSubmit,
}: {
  action: any
  initialValues: Record<string, string>
  submitting: boolean
  onClose: () => void
  onSubmit: (params: Record<string, string>) => void
}) {
  const [form, setForm] = useState<Record<string, string>>(initialValues)

  useEffect(() => {
    setForm(initialValues)
  }, [action?.id, initialValues])

  const params = Array.isArray(action?.params) ? action.params : []

  return (
    <div className="dialog-backdrop" onClick={onClose}>
      <div
        className="dialog-panel dialog-panel-md"
        onClick={e => e.stopPropagation()}
      >
        <div className="flex items-center justify-between px-6 py-4 border-b border-[var(--border)]">
          <div>
            <h2 className="text-base font-semibold text-[var(--text-primary)]">{action?.label || '动作参数'}</h2>
            <p className="text-xs text-[var(--text-muted)] mt-0.5">填写执行该动作所需的参数</p>
          </div>
          <button onClick={onClose} className="text-[var(--text-muted)] hover:text-[var(--text-primary)]">
            <X className="h-4 w-4" />
          </button>
        </div>
        <div className="px-6 py-4 space-y-4">
          {params.map((param: any) => {
            const value = form[param.key] ?? ''
            if (Array.isArray(param.options) && param.options.length > 0) {
              return (
                <label key={param.key} className="block">
                  <div className="mb-1 text-xs text-[var(--text-muted)]">{param.label || param.key}</div>
                  <select
                    value={value}
                    onChange={e => setForm(current => ({ ...current, [param.key]: e.target.value }))}
                    className="w-full rounded-xl border border-[var(--border-soft)] bg-[var(--bg-hover)] px-3 py-2 text-sm outline-none focus:border-[var(--text-accent)]"
                  >
                    {param.options.map((option: string) => (
                      <option key={option} value={option}>{option}</option>
                    ))}
                  </select>
                </label>
              )
            }
            if (param.type === 'textarea') {
              return (
                <label key={param.key} className="block">
                  <div className="mb-1 text-xs text-[var(--text-muted)]">{param.label || param.key}</div>
                  <textarea
                    value={value}
                    onChange={e => setForm(current => ({ ...current, [param.key]: e.target.value }))}
                    rows={3}
                    className="w-full rounded-xl border border-[var(--border-soft)] bg-[var(--bg-hover)] px-3 py-2 text-sm outline-none focus:border-[var(--text-accent)]"
                  />
                </label>
              )
            }
            return (
              <label key={param.key} className="block">
                <div className="mb-1 text-xs text-[var(--text-muted)]">{param.label || param.key}</div>
                <input
                  type={param.type === 'number' ? 'number' : 'text'}
                  value={value}
                  onChange={e => setForm(current => ({ ...current, [param.key]: e.target.value }))}
                  className="w-full rounded-xl border border-[var(--border-soft)] bg-[var(--bg-hover)] px-3 py-2 text-sm outline-none focus:border-[var(--text-accent)]"
                />
              </label>
            )
          })}
        </div>
        <div className="px-6 py-4 border-t border-[var(--border)] flex gap-3">
          <Button onClick={() => onSubmit(form)} disabled={submitting} className="flex-1">
            {submitting ? '执行中...' : '执行'}
          </Button>
          <Button variant="outline" onClick={onClose} disabled={submitting} className="flex-1">取消</Button>
        </div>
      </div>
    </div>
  )
}
// ── 行操作菜单 ─────────────────────────────────────────────
function ActionMenu({
  acc,
  onDetail,
  onDelete,
  onResult,
  onChanged,
  onTriggerGetRt,
}: {
  acc: any
  onDetail: () => void
  onDelete: () => void
  onResult: (title: string, payload: any) => void
  onChanged: () => void
  onTriggerGetRt?: (acc: any, kind: 'get_rt' | 'get_rt_bypass') => void
}) {
  const { t, language } = useI18n()
  const [open, setOpen] = useState(false)
  const [actions, setActions] = useState<any[]>([])
  const [running, setRunning] = useState<string | null>(null)
  const [toast, setToast] = useState<{ type: 'success' | 'error'; text: string } | null>(null)
  const [actionTask, setActionTask] = useState<{ taskId: string; title: string } | null>(null)
  const [actionTaskStatus, setActionTaskStatus] = useState<string | null>(null)
  const [pendingAction, setPendingAction] = useState<{ action: any; params: Record<string, string> } | null>(null)
  const [menuPosition, setMenuPosition] = useState({ top: 0, left: 0, maxHeight: 320 })
  const menuRef = useRef<HTMLDivElement>(null)
  const triggerRef = useRef<HTMLButtonElement>(null)
  const canCopySession = String(acc?.platform || '').trim().toLowerCase() === 'chatgpt'
  const canCopyK12Session = canCopySession && !isEmptyPayload(getChatgptK12SessionPayload(acc))
  const hasMenuItems = actions.length > 0 || canCopySession || canCopyK12Session

  const runAction = (action: any, params: Record<string, any>) => {
    setRunning(action.id)
    setActionTaskStatus(null)
    apiFetch(`/actions/${acc.platform}/${acc.id}/${action.id}`, { method: 'POST', body: JSON.stringify({ params }) })
      .then(resp => {
        if (resp?.sync) {
          setRunning(null)
          if (!resp.ok) {
            setToast({ type: 'error', text: resp.error || t('accounts.operationFailed') })
            return
          }
          onChanged()
          if (resp.data?.url || resp.data?.checkout_url || resp.data?.cashier_url) {
            const actionUrl = resp.data?.url || resp.data?.checkout_url || resp.data?.cashier_url
            window.open(actionUrl, '_blank')
            try {
              navigator.clipboard.writeText(actionUrl)
            } catch {
              // Ignore clipboard errors
            }
          }
          onResult(action.label, resp.data)
          return
        }
        setActionTask({
          taskId: resp.task_id,
          title: `${acc.email} - ${action.label}`,
        })
      })
      .catch(() => {
        setRunning(null)
        setToast({ type: 'error', text: t('login.requestFailed') })
      })
  }

  const updateMenuPosition = useCallback(() => {
    const trigger = triggerRef.current
    if (!trigger) return

    const rect = trigger.getBoundingClientRect()
    const viewportPadding = 12
    const menuWidth = 220
    const copyActionCount = (canCopySession ? 1 : 0) + (canCopyK12Session ? 1 : 0)
    const estimatedHeight = Math.min(320, (actions.length + copyActionCount) * 40 + 56)

    let left = rect.right - menuWidth
    if (left < viewportPadding) left = viewportPadding
    if (left + menuWidth > window.innerWidth - viewportPadding) {
      left = Math.max(viewportPadding, window.innerWidth - menuWidth - viewportPadding)
    }

    let top = rect.bottom + 8
    if (top + estimatedHeight > window.innerHeight - viewportPadding) {
      top = Math.max(viewportPadding, rect.top - estimatedHeight - 8)
    }

    setMenuPosition({
      top: Math.round(top),
      left: Math.round(left),
      maxHeight: Math.max(160, window.innerHeight - viewportPadding * 2),
    })
  }, [actions.length, canCopySession, canCopyK12Session])

  useEffect(() => {
    let active = true
    loadPlatformActions(acc.platform)
      .then((items) => {
        if (active) setActions(items)
      })
      .catch(() => {
        if (active) setActions([])
      })
    return () => {
      active = false
    }
  }, [acc.platform])
  useEffect(() => {
    if (toast) { const t = setTimeout(() => setToast(null), 4000); return () => clearTimeout(t) }
  }, [toast])
  useEffect(() => {
    if (!open) return
    let active = true
    loadPlatformActions(acc.platform, { force: true })
      .then((items) => {
        if (active) setActions(items)
      })
      .catch(() => {
        if (active) setActions([])
      })
    updateMenuPosition()
    const handler = (e: MouseEvent) => {
      const target = e.target as Node
      if (menuRef.current?.contains(target) || triggerRef.current?.contains(target)) return
      setOpen(false)
    }
    const reposition = () => updateMenuPosition()
    document.addEventListener('mousedown', handler)
    window.addEventListener('resize', reposition)
    window.addEventListener('scroll', reposition, true)
    return () => {
      active = false
      document.removeEventListener('mousedown', handler)
      window.removeEventListener('resize', reposition)
      window.removeEventListener('scroll', reposition, true)
    }
  }, [open, acc.platform, updateMenuPosition])

  const handleActionDone = async (status: string) => {
    if (!actionTask) return
    setActionTaskStatus(status)
    setRunning(null)
    try {
      const task = await apiFetch(`/tasks/${actionTask.taskId}`)
      const data = task?.data ?? task?.result?.data
      if (status !== 'succeeded') {
        setToast({ type: 'error', text: task?.error || getTaskStatusText(status, language) })
        return
      }
      onChanged()
      const actionUrl = data?.url || data?.checkout_url || data?.cashier_url
      if (actionUrl) {
        window.open(actionUrl, '_blank')
        try {
          await navigator.clipboard.writeText(actionUrl)
        } catch {
          // ignore clipboard failures
        }
      }
      if (data && typeof data === 'object') {
        if (actionUrl) {
          setToast({ type: 'success', text: data.message || t('accounts.paymentLinkOpened') })
          return
        }
        const detailKeys = Object.keys(data).filter(key => !['message', 'url', 'checkout_url', 'cashier_url'].includes(key))
        if (detailKeys.length > 0) {
          onResult(actionTask.title, data)
        }
        setToast({ type: 'success', text: data.message || t('accounts.actionSuccess') })
        return
      }
      setToast({ type: 'success', text: typeof data === 'string' && data ? data : t('accounts.actionSuccess') })
    } catch (error: any) {
      setToast({ type: 'error', text: error?.message || t('login.requestFailed') })
    }
  }

  return (
    <div className="relative flex min-w-[112px] items-center justify-end gap-1.5 whitespace-nowrap">
      {toast && (
        <div
          className="fixed top-5 right-5 z-[9999] flex items-center gap-2.5 rounded-xl border px-4 py-3 text-[13px] font-medium shadow-lg  cursor-pointer transition-all"
          style={{
            background: toast.type === 'success' ? 'rgba(16,185,129,0.12)' : 'rgba(239,68,68,0.12)',
            borderColor: toast.type === 'success' ? 'rgba(16,185,129,0.25)' : 'rgba(239,68,68,0.25)',
            color: toast.type === 'success' ? '#6ee7b7' : '#fca5a5',
          }}
          onClick={() => setToast(null)}
        >
          <span className="text-base">{toast.type === 'success' ? '✓' : '✗'}</span>
          <span>{toast.text}</span>
        </div>
      )}
      {actionTask && (
        <ActionTaskModal
          title={actionTask.title}
          taskId={actionTask.taskId}
          taskStatus={actionTaskStatus}
          onClose={() => {
            setActionTask(null)
            setActionTaskStatus(null)
          }}
          onDone={handleActionDone}
        />
      )}
      {pendingAction && (
        <ActionParamsModal
          action={pendingAction.action}
          initialValues={pendingAction.params}
          submitting={running === pendingAction.action?.id}
          onClose={() => {
            if (!running) setPendingAction(null)
          }}
          onSubmit={(params) => {
            const action = pendingAction.action
            setPendingAction(null)
            if (action?.id === 'k12_join_upload') {
              writeStoredChatgptK12WorkspaceIds(params.workspace_ids || '')
            }
            runAction(action, params)
          }}
        />
      )}
      <button onClick={onDetail} className="table-action-btn">{t('accounts.details')}</button>
      {hasMenuItems && (
        <div className="relative">
          <button ref={triggerRef} onClick={() => setOpen(o => !o)}
            className="table-action-btn">{t('common.more')} v</button>
          {open && typeof document !== 'undefined' && createPortal(
            <div
              ref={menuRef}
              className="fixed z-[9999] w-[220px] overflow-y-auto rounded-2xl border border-[var(--border)] bg-[var(--bg-card)]/96 py-1.5 shadow-[var(--shadow-soft)] "
              style={{ top: menuPosition.top, left: menuPosition.left, maxHeight: menuPosition.maxHeight }}
            >
              {actions.map(a => (
                <button key={a.id}
                  onClick={() => {
                    setOpen(false)
                    if (
                      onTriggerGetRt &&
                      (a.id === 'get_rt' || a.id === 'get_rt_bypass')
                    ) {
                      onTriggerGetRt(acc, a.id)
                      return
                    }
                    if (Array.isArray(a.params) && a.params.length > 0) {
                      setPendingAction({
                        action: a,
                        params: buildActionParamDraft(a, acc),
                      })
                      return
                    }
                    runAction(a, {})
                  }}
                  disabled={!!running}
                  className="w-full px-3 py-2 text-left text-xs text-[var(--text-secondary)] transition-colors hover:bg-[var(--bg-hover)] hover:text-[var(--text-primary)] disabled:opacity-50">
                  {running === a.id ? t('taskStatus.running') : a.label}
                </button>
              ))}
              {canCopySession && (
                <>
                  {actions.length > 0 && <div className="my-1 border-t border-[var(--border)]/70" />}
                  <button
                    onClick={async () => {
                      setOpen(false)
                      const sessionText = getChatgptSessionText(acc)
                      if (!sessionText) {
                        setToast({ type: 'error', text: '\u672a\u4fdd\u5b58session' })
                        return
                      }
                      try {
                        await writeClipboardText(sessionText)
                        setToast({ type: 'success', text: '\u5df2\u590d\u5236session' })
                      } catch (error: any) {
                        setToast({ type: 'error', text: error?.message || t('login.requestFailed') })
                      }
                    }}
                    className="w-full px-3 py-2 text-left text-xs text-[var(--text-secondary)] transition-colors hover:bg-[var(--bg-hover)] hover:text-[var(--text-primary)]"
                  >
                    {'\u590d\u5236session'}
                  </button>
                  {canCopyK12Session && (
                    <button
                      onClick={async () => {
                        setOpen(false)
                        const sessionText = getChatgptK12SessionText(acc)
                        if (!sessionText) {
                          setToast({ type: 'error', text: '未保存K12 session' })
                          return
                        }
                        try {
                          await writeClipboardText(sessionText)
                          setToast({ type: 'success', text: '已复制K12 session' })
                        } catch (error: any) {
                          setToast({ type: 'error', text: error?.message || t('login.requestFailed') })
                        }
                      }}
                      className="w-full px-3 py-2 text-left text-xs text-[var(--text-secondary)] transition-colors hover:bg-[var(--bg-hover)] hover:text-[var(--text-primary)]"
                    >
                      复制K12 Session
                    </button>
                  )}
                </>
              )}
              <div className="my-1 border-t border-[var(--border)]/70" />
              <button
                onClick={() => {
                  setOpen(false)
                  if (confirm(t('accounts.deleteConfirm', { email: acc.email }))) {
                    apiFetch(`/accounts/${acc.id}`, { method: 'DELETE' }).then(onDelete)
                  }
                }}
                className="w-full px-3 py-2 text-left text-xs text-[#f0b0b0] transition-colors hover:bg-[rgba(239,68,68,0.08)] hover:text-[#ffd5d5]"
              >
                {t('common.delete')}
              </button>
            </div>,
            document.body,
          )}
        </div>
      )}
      {!hasMenuItems && (
        <button
          onClick={() => { if (confirm(t('accounts.deleteConfirm', { email: acc.email }))) apiFetch(`/accounts/${acc.id}`, { method: 'DELETE' }).then(onDelete) }}
          className="table-action-btn table-action-btn-danger"
        >
          {t('common.delete')}
        </button>
      )}
    </div>
  )
}

// ── 账号详情弹框 ───────────────────────────────────────────
function DetailModal({ acc, onClose, onSave }: { acc: any; onClose: () => void; onSave: () => void }) {
  const [form, setForm] = useState({
    lifecycle_status: getLifecycleStatus(acc),
    primary_token: getPrimaryToken(acc),
    cashier_url: getCashierUrl(acc),
  })
  const [saving, setSaving] = useState(false)
  const overview = getAccountOverview(acc)
  const verificationMailbox = getVerificationMailbox(acc)
  const providerAccounts = getProviderAccounts(acc)
  const credentials = getCredentials(acc)
  const primaryMetrics = getPrimaryMetrics(acc)
  const secondaryMetrics = getSecondaryMetrics(acc)
  const warnings = getDisplayWarnings(acc)
  const displayBadges = getDisplayBadges(acc)
  const displaySections = getDisplaySections(acc)
  const copyText = (text: string) => navigator.clipboard.writeText(text)
  const platformCredentials = credentials.filter((item: any) => item.scope === 'platform')

  const save = async () => {
    setSaving(true)
    try {
      await apiFetch(`/accounts/${acc.id}`, { method: 'PATCH', body: JSON.stringify(form) })
      onSave()
    } finally { setSaving(false) }
  }

  return (
    <div className="dialog-backdrop" onClick={onClose}>
      <div className="dialog-panel dialog-panel-sm flex flex-col" style={{maxHeight:'90vh'}} onClick={e => e.stopPropagation()}>
        {/* ── Sticky Header ── */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-[var(--border)] shrink-0">
          <div>
            <h2 className="text-base font-semibold text-[var(--text-primary)]">账号详情</h2>
            <p className="text-xs text-[var(--text-muted)] mt-0.5">{acc.email}</p>
          </div>
          <button onClick={onClose} className="text-[var(--text-muted)] hover:text-[var(--text-primary)]"><X className="h-4 w-4" /></button>
        </div>
        {/* ── Scrollable Content ── */}
        <div className="px-6 py-4 space-y-3 flex-1 overflow-y-auto min-h-0">
          <div className="relative overflow-hidden rounded-lg border border-[var(--border-soft)] bg-[var(--accent-soft)] p-4 shadow-[var(--shadow-soft)]">
            <div className="pointer-events-none absolute -right-16 -top-20 h-44 w-44 rounded-full bg-[var(--accent-soft)] blur-3xl" />
            <div className="relative flex flex-wrap items-start justify-between gap-3">
              <div>
                <div className="text-xs uppercase tracking-[0.18em] text-[var(--text-muted)]">核心状态</div>
                <div className="mt-2 flex flex-wrap items-center gap-2">
                  <Badge variant={STATUS_VARIANT[getDisplayStatus(acc)] || 'secondary'}>{getDisplayStatus(acc)}</Badge>
                  <span className="text-lg font-semibold tracking-[-0.03em] text-[var(--text-primary)]">{acc.plan_name || overview.plan_name || overview.plan || getPlanState(acc)}</span>
                </div>
              </div>
              <div className="grid grid-cols-2 gap-2 text-right text-[11px] text-[var(--text-muted)] sm:grid-cols-3">
                <div className="rounded-xl border border-[var(--border-soft)] bg-black/10 px-2.5 py-2">
                  <div className="uppercase tracking-[0.12em]">生命周期</div>
                  <div className="mt-1 text-[var(--text-primary)]">{getLifecycleStatus(acc)}</div>
                </div>
                <div className="rounded-xl border border-[var(--border-soft)] bg-black/10 px-2.5 py-2">
                  <div className="uppercase tracking-[0.12em]">有效性</div>
                  <div className="mt-1 text-[var(--text-primary)]">{getValidityStatus(acc)}</div>
                </div>
                <div className="rounded-xl border border-[var(--border-soft)] bg-black/10 px-2.5 py-2">
                  <div className="uppercase tracking-[0.12em]">套餐状态</div>
                  <div className="mt-1 text-[var(--text-primary)]">{getPlanState(acc)}</div>
                </div>
              </div>
            </div>
            {secondaryMetrics.length > 0 && (
              <div className="relative mt-4 grid gap-2 sm:grid-cols-2">
                {secondaryMetrics.slice(0, 4).map((metric: any) => (
                  <DisplayMetricCard key={metric.key || metric.label} metric={metric} compact />
                ))}
              </div>
            )}
          </div>

          {primaryMetrics.length > 0 && (
            <div className="grid gap-3 sm:grid-cols-2">
              {primaryMetrics.map((metric: any) => (
                <DisplayMetricCard key={metric.key || metric.label} metric={metric} />
              ))}
            </div>
          )}

          <DisplayWarnings warnings={warnings} />
          <DisplaySections sections={displaySections} />

          {(displayBadges.length > 0 || verificationMailbox?.email) && (
            <div className="space-y-2">
              {displayBadges.length > 0 && (
                <div className="flex flex-wrap gap-1">
                  {displayBadges.map((badge: any, index: number) => (
                    <span key={`${badge?.label || 'badge'}-${index}`} className={getAccountBadgeClassName(badge, 'detail')}>
                      {badge?.label}
                    </span>
                  ))}
                </div>
              )}
              {verificationMailbox?.email && (
                <div className="rounded-xl border border-[var(--border-soft)] bg-[var(--bg-hover)] px-3 py-2 text-xs text-[var(--text-secondary)]">
                  验证码邮箱: {verificationMailbox.email} · {verificationMailbox.provider || '-'} · ID {verificationMailbox.account_id || '-'}
                </div>
              )}
            </div>
          )}
          {providerAccounts.length > 0 && (
            <div className="space-y-2">
              <label className="text-xs text-[var(--text-muted)] block">Provider Accounts</label>
              {providerAccounts.map((item: any, index: number) => (
                <div key={`${item.provider_name || 'provider'}-${item.login_identifier || index}`} className="rounded-xl border border-[var(--border-soft)] bg-[var(--bg-hover)] p-3">
                  <div className="text-xs font-semibold text-[var(--text-primary)]">
                    {item.provider_name || item.provider_type || 'provider'}
                  </div>
                  <div className="mt-1 text-xs text-[var(--text-secondary)] break-all">
                    登录标识: {item.login_identifier || '-'}
                  </div>
                  {item.credentials && Object.keys(item.credentials).length > 0 && (
                    <div className="mt-2 grid gap-2">
                      {Object.entries(item.credentials).map(([key, value]: [string, any]) => (
                        <div key={key}>
                          <div className="text-[11px] text-[var(--text-muted)]">{key}</div>
                          <div className="flex items-start gap-1">
                            <div className="flex-1 rounded-md border border-[var(--border-soft)] bg-black/20 px-2 py-1.5 text-xs font-mono text-[var(--text-secondary)] break-all max-h-40 overflow-y-auto">
                              {String(value || '-')}
                            </div>
                            {value ? (
                              <button onClick={() => copyText(String(value))} className="mt-1 shrink-0 text-[var(--text-muted)] hover:text-[var(--text-secondary)]">
                                <Copy className="h-3 w-3" />
                              </button>
                            ) : null}
                          </div>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              ))}
            </div>
          )}
          {platformCredentials.length > 0 && (
            <div className="space-y-2">
              <label className="text-xs text-[var(--text-muted)] block">Platform Credentials</label>
              {platformCredentials.map((item: any) => (
                <div key={`${item.scope}-${item.key}`} className="rounded-xl border border-[var(--border-soft)] bg-[var(--bg-hover)] p-3">
                  <div className="text-[11px] text-[var(--text-muted)]">{item.key}</div>
                  <div className="mt-1 flex items-start gap-1">
                    <div className="flex-1 rounded-md border border-[var(--border-soft)] bg-black/20 px-2 py-1.5 text-xs font-mono text-[var(--text-secondary)] break-all max-h-40 overflow-y-auto">
                      {item.value}
                    </div>
                    <button onClick={() => copyText(String(item.value || ''))} className="mt-1 shrink-0 text-[var(--text-muted)] hover:text-[var(--text-secondary)]">
                      <Copy className="h-3 w-3" />
                    </button>
                  </div>
                </div>
              ))}
            </div>
          )}
          <div>
            <label className="text-xs text-[var(--text-muted)] block mb-1">生命周期状态</label>
            <select value={form.lifecycle_status} onChange={e => setForm(f => ({ ...f, lifecycle_status: e.target.value }))}
              className="control-surface appearance-none">
              {['registered','trial','subscribed','expired','invalid','banned'].map(s => <option key={s} value={s}>{s}</option>)}
            </select>
          </div>
          <div>
            <label className="text-xs text-[var(--text-muted)] block mb-1">主凭证</label>
            <textarea value={form.primary_token} onChange={e => setForm(f => ({ ...f, primary_token: e.target.value }))}
              rows={2} className="control-surface control-surface-mono resize-none" />
          </div>
          <div>
            <label className="text-xs text-[var(--text-muted)] block mb-1">试用链接</label>
            <textarea value={form.cashier_url} onChange={e => setForm(f => ({ ...f, cashier_url: e.target.value }))}
              rows={2} className="control-surface control-surface-mono resize-none" />
          </div>
        </div>
        {/* ── Sticky Footer ── */}
        <div className="flex gap-3 px-6 py-4 border-t border-[var(--border)] shrink-0">
          <Button onClick={save} disabled={saving} className="flex-1">{saving ? '保存中...' : '保存'}</Button>
          <Button variant="outline" onClick={onClose} className="flex-1">取消</Button>
        </div>
      </div>
    </div>
  )
}

// ── 导入弹框 ────────────────────────────────────────────────
function ImportModal({ platform, onClose, onDone }: { platform: string; onClose: () => void; onDone: () => void }) {
  const [text, setText] = useState('')
  const [loading, setLoading] = useState(false)
  const [result, setResult] = useState<string | null>(null)
  const submit = async () => {
    setLoading(true)
    try {
      const lines = text.trim().split('\n').filter(Boolean)
      const res = await apiFetch('/accounts/import', { method: 'POST', body: JSON.stringify({ platform, lines }) })
      setResult(`导入成功 ${res.created} 个`); onDone()
    } catch (e: any) { setResult(`失败: ${e.message}`) } finally { setLoading(false) }
  }
  return (
    <div className="dialog-backdrop" onClick={onClose}>
      <div className="dialog-panel dialog-panel-sm p-6" onClick={e => e.stopPropagation()}>
        <h2 className="text-base font-semibold text-[var(--text-primary)] mb-2">批量导入</h2>
        <p className="text-xs text-[var(--text-muted)] mb-3">每行格式: <code className="bg-[var(--bg-hover)] px-1 rounded">email password [cashier_url]</code></p>
        <textarea value={text} onChange={e => setText(e.target.value)} rows={8}
          className="control-surface control-surface-mono resize-none mb-3" />
        {result && <p className="text-sm text-emerald-400 mb-3">{result}</p>}
        <div className="flex gap-2">
          <Button onClick={submit} disabled={loading} className="flex-1">{loading ? '导入中...' : '导入'}</Button>
          <Button variant="outline" onClick={onClose} className="flex-1">取消</Button>
        </div>
      </div>
    </div>
  )
}

function ExportMenu({
  platform,
  total,
  statusFilter,
  searchFilter,
  selectedIds,
  triggerClassName,
  showIcon = true,
}: {
  platform: string
  total: number
  statusFilter: string
  searchFilter: string
  selectedIds: number[]
  triggerClassName?: string
  showIcon?: boolean
}) {
  const { t } = useI18n()
  const [open, setOpen] = useState(false)
  const [loading, setLoading] = useState<string | null>(null)
  const menuRef = useRef<HTMLDivElement>(null)
  const hasSelection = selectedIds.length > 0

  useEffect(() => {
    if (!open) return
    const handler = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [open])

  const doExport = async (format: string) => {
    setLoading(format)
    try {
      const { blob, filename } = await apiDownload(`/accounts/export/${format}`, {
        method: 'POST',
        body: JSON.stringify({
          platform,
          ids: hasSelection ? selectedIds : [],
          select_all: !hasSelection,
          status_filter: !hasSelection ? statusFilter || null : null,
          search_filter: !hasSelection ? searchFilter || null : null,
        }),
      })
      triggerBrowserDownload(blob, filename)
      setOpen(false)
    } catch (e: any) {
      window.alert(e?.message || t('accounts.exportFailed'))
    } finally {
      setLoading(null)
    }
  }

  const options = [
    { key: 'json', label: t('accounts.exportJson') },
    { key: 'csv', label: t('accounts.exportCsv') },
    { key: 'any2api', label: t('accounts.exportAny2Api') },
    { key: 'sub2api', label: t('accounts.exportSub2Api') },
    { key: 'cpa', label: t('accounts.exportCpa') },
    ...(platform === 'kiro' ? [{ key: 'kiro-go', label: t('accounts.exportKiroGo') }] : []),
  ]

  return (
    <div className="relative" ref={menuRef}>
      <Button
        variant="outline"
        size="sm"
        onClick={() => setOpen(v => !v)}
        disabled={total === 0 || !!loading}
        className={cn(ACCOUNT_TOOL_BUTTON_CLASS, triggerClassName)}
      >
        {showIcon && <Download className="h-4 w-4 mr-1 shrink-0" />}
        {loading ? t('accounts.exporting') : hasSelection ? t('accounts.exportSelected', { count: selectedIds.length }) : t('accounts.export')}
      </Button>
      {open && (
        <div className="absolute right-0 top-10 z-20 min-w-[148px] rounded-lg border border-[var(--border-soft)] bg-[var(--bg-card)] py-1 shadow-lg">
          <div className="px-3 py-1 text-[11px] text-[var(--text-muted)]">
            {hasSelection ? t('accounts.exportSelected', { count: selectedIds.length }) : t('accounts.exportCurrentResults')}
          </div>
          {options.map(option => (
            <button
              key={option.key}
              onClick={() => doExport(option.key)}
              className="w-full px-3 py-1.5 text-left text-xs text-[var(--text-secondary)] hover:bg-[var(--bg-hover)] hover:text-[var(--text-primary)]"
            >
              {option.label}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}

// ── Main ────────────────────────────────────────────────────
export default function Accounts() {
  const { t, language } = useI18n()
  const { platform } = useParams<{ platform: string }>()
  const [tab, setTab] = useState(platform || 'chatgpt')
  useEffect(() => { if (platform) { setTab(platform) } }, [platform])

  const [accounts, setAccounts] = useState<any[]>([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [search, setSearch] = useState('')
  const [debouncedSearch, setDebouncedSearch] = useState('')
  const [filterStatus, setFilterStatus] = useState('')
  const [page, setPage] = useState(1)
  const [detail, setDetail] = useState<any | null>(null)
  const [showImport, setShowImport] = useState(false)
  const [showAdd, setShowAdd] = useState(false)
  const [showRegister, setShowRegister] = useState(false)
  const [platformsMap, setPlatformsMap] = useState<Record<string, any>>({})
  const [selectedIds, setSelectedIds] = useState<Set<number>>(new Set())
  const [actionResult, setActionResult] = useState<{ title: string; payload: any } | null>(null)
  const [bulkDeleting, setBulkDeleting] = useState(false)
  const [batchRefreshing, setBatchRefreshing] = useState(false)
  const [batchHealthChecking, setBatchHealthChecking] = useState(false)
  const [batchStatusOpen, setBatchStatusOpen] = useState(false)
  const [batchStatusUpdating, setBatchStatusUpdating] = useState(false)
  const [batchStatusValue, setBatchStatusValue] = useState('registered')
  const [batchTask, setBatchTask] = useState<{ taskId: string; title: string } | null>(null)
  const [batchTaskStatus, setBatchTaskStatus] = useState<string | null>(null)
  const [browserMode, setBrowserMode] = useState('camoufox_headed')
  const [actionConcurrency, setActionConcurrency] = useState(1)
  const [oauthTaskId, setOauthTaskId] = useState('')
  const [oauthBusy, setOauthBusy] = useState(false)
  const [oauthConfirmOpen, setOauthConfirmOpen] = useState(false)
  const [getRtTaskId, setGetRtTaskId] = useState('')
  const [getRtBusy, setGetRtBusy] = useState(false)
  const [getRtConfirmOpen, setGetRtConfirmOpen] = useState(false)
  const [getRtBypassTaskId, setGetRtBypassTaskId] = useState('')
  const [getRtBypassBusy, setGetRtBypassBusy] = useState(false)
  const [getRtBypassConfirmOpen, setGetRtBypassConfirmOpen] = useState(false)
  const [getRtSmsProvider, setGetRtSmsProvider] = useState('')
  const [getRtSmsapiPhone, setGetRtSmsapiPhone] = useState('')
  const [getRtSmsapiUrl, setGetRtSmsapiUrl] = useState('')
  const [getRtRecordHar, setGetRtRecordHar] = useState(false)
  const [getRtPhoneReuseCount, setGetRtPhoneReuseCount] = useState(3)
  const [getRtPhoneChangeLimit, setGetRtPhoneChangeLimit] = useState(10)
  const [getRtTaskMode, setGetRtTaskMode] = useState<'single' | 'target'>('single')
  const [getRtSmsBalanceAction, setGetRtSmsBalanceAction] = useState<GetRtSmsBalanceAction>('auto_switch')
  const [getRtExecutorType, setGetRtExecutorType] = useState('browser')
  const [configOptions, setConfigOptions] = useState<ConfigOptionsResponse>({
    mailbox_providers: [],
    captcha_providers: [],
    sms_providers: [],
    mailbox_settings: [],
    captcha_settings: [],
    sms_settings: [],
    captcha_policy: {},
    executor_options: [],
    identity_mode_options: [],
    oauth_provider_options: [],
  })

  useEffect(() => {
    getPlatforms().then((list: any[]) => {
      const map: Record<string, any> = {}
      list.forEach(p => { map[p.name] = p })
      setPlatformsMap(map)
      if (!platform && !tab && list[0]?.name) {
        setTab(list[0].name)
      }
    }).catch(() => {})
  }, [platform, tab])

  useEffect(() => {
    const timer = setTimeout(() => setDebouncedSearch(search), 400)
    return () => clearTimeout(timer)
  }, [search])

  useEffect(() => {
    let active = true
    getConfigOptions()
      .then(options => {
        if (active && options) setConfigOptions(options)
      })
      .catch(() => {})
    return () => { active = false }
  }, [])

  const pageSize = 10

  useEffect(() => {
    setSelectedIds(new Set())
    setPage(1)
  }, [tab, filterStatus, debouncedSearch])

  const load = useCallback(async (p = tab, s = debouncedSearch, fs = filterStatus, pg = page) => {
    setLoading(true)
    try {
      const params = new URLSearchParams({ platform: p, page: String(pg), page_size: String(pageSize) })
      if (s) params.set('email', s)
      if (fs) params.set('status', fs)
      const data = await apiFetch(`/accounts?${params}`)
      setAccounts(data.items); setTotal(data.total)
    } finally { setLoading(false) }
  }, [tab, debouncedSearch, filterStatus, page])

  useEffect(() => { load(tab, debouncedSearch, filterStatus, page) }, [tab, debouncedSearch, filterStatus, page, load])

  useEffect(() => {
    setSelectedIds(prev => {
      const visible = new Set(accounts.map(acc => acc.id))
      return new Set([...prev].filter(id => visible.has(id)))
    })
  }, [accounts])

  
  const exportCsv = () => {
    const header = 'email,password,display_status,lifecycle_status,plan_state,validity_status,cashier_url,created_at'
    const rowsSource = selectedIds.size > 0 ? accounts.filter(a => selectedIds.has(a.id)) : accounts
    const rows = rowsSource.map(a => [
      a.email,
      a.password,
      getDisplayStatus(a),
      getLifecycleStatus(a),
      getPlanState(a),
      getValidityStatus(a),
      getCashierUrl(a),
      a.created_at,
    ].map(escapeCsvField).join(','))
    const blob = new Blob([[header, ...rows].join('\n')], { type: 'text/csv' })
    triggerBrowserDownload(blob, `${tab}_accounts.csv`)
  }

  const pageIds = accounts.map(acc => acc.id)
  const allSelectedOnPage = pageIds.length > 0 && pageIds.every(id => selectedIds.has(id))
  const selectedCount = selectedIds.size
  const selectedAccounts = accounts.filter(acc => selectedIds.has(acc.id))
  const getRtEligibleIds = selectedAccounts
    .filter(acc => getRtTaskMode === 'target' ? isGetRtTargetModeAccount(acc) : isRegisteredOnlyAccount(acc))
    .map(acc => Number(acc.id))
    .filter(id => Number.isFinite(id) && id > 0)
  const getRtAnyModeEligibleIds = selectedAccounts
    .filter(isGetRtTargetModeAccount)
    .map(acc => Number(acc.id))
    .filter(id => Number.isFinite(id) && id > 0)
  const enabledSmsSettings = (configOptions.sms_settings || []).filter((item: any) => item?.enabled)
  const defaultSmsSetting = enabledSmsSettings.find((item: any) => item?.is_default) || enabledSmsSettings[0] || null
  const getRtSmsProviderOptions = (() => {
    const options: { value: string; label: string }[] = []
    const seen = new Set<string>()
    appendUniqueProviderOption(options, seen, { value: 'none', label: '(不启用)' })
    if (defaultSmsSetting) {
      appendUniqueProviderOption(options, seen, {
        value: 'default',
        label: `默认：${defaultSmsSetting.display_name || defaultSmsSetting.catalog_label || defaultSmsSetting.provider_key}`,
      })
      const defaultValue = normalizeGetRtSmsProviderKey(defaultSmsSetting.provider_key)
      if (defaultValue) seen.add(defaultValue)
    }
    enabledSmsSettings
      .filter((item: any) => item?.provider_key && item?.provider_key !== defaultSmsSetting?.provider_key)
      .forEach((item: any) => {
        const value = normalizeGetRtSmsProviderKey(item.provider_key)
        appendUniqueProviderOption(options, seen, {
          value,
          label: item.display_name || item.catalog_label || item.provider_key,
        })
      })
    appendUniqueProviderOption(options, seen, { value: 'smspool', label: 'SMSPool' })
    appendUniqueProviderOption(options, seen, { value: 'smsapi', label: 'SmsApi（自有固定号）' })
    return options
  })()

  useEffect(() => {
    if (!getRtSmsProvider) {
      setGetRtSmsProvider(defaultSmsSetting?.provider_key ? 'default' : 'none')
    }
  }, [getRtSmsProvider, defaultSmsSetting?.provider_key])

  const toggleOne = (id: number) => {
    setSelectedIds(prev => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  const togglePage = () => {
    setSelectedIds(prev => {
      const next = new Set(prev)
      if (allSelectedOnPage) pageIds.forEach(id => next.delete(id))
      else pageIds.forEach(id => next.add(id))
      return next
    })
  }

  const copy = (text: string) => {
    if (navigator.clipboard) { navigator.clipboard.writeText(text) }
    else { const el = document.createElement('textarea'); el.value = text; document.body.appendChild(el); el.select(); document.execCommand('copy'); document.body.removeChild(el) }
  }
  const emailApiLine = (email: string) =>
    `${email} https://hsxhome.com/api/find/openai?email=${email}&t=fzKIywnF4KEGGB_i`

  const startHealthCheck = async () => {
    setBatchHealthChecking(true)
    try {
      const res = await apiFetch(`/accounts/health-check?platform=${tab}`, {
        method: 'POST',
        body: JSON.stringify({ ids: selectedCount > 0 ? [...selectedIds] : [] }),
      })
      if (res?.task_id) {
        setBatchTask({ taskId: res.task_id, title: t('accounts.healthCheckTask', { platform: platformLabel }) })
        setBatchTaskStatus(null)
      }
    } catch (e) {
      console.error(e)
      setBatchHealthChecking(false)
    }
  }

  const startCodexOAuth = async () => {
    setError('')
    const ids = [...selectedIds].map(Number)
    if (ids.length === 0) {
      setError('请选择至少 1 个账户进行 Codex OAuth')
      return
    }
    setOauthBusy(true)
    try {
      const data = await apiFetch('/tasks/codex-oauth', {
        method: 'POST',
        body: JSON.stringify({
          platform: 'chatgpt',
          ids,
          browser_mode: browserMode,
          concurrency: Math.max(Number(actionConcurrency || 1), 1),
        }),
      })
      setOauthTaskId(String(data?.task_id || data?.id || ''))
    } catch (exc: any) {
      setError(exc?.message || t('login.requestFailed'))
    } finally {
      setOauthBusy(false)
    }
  }

  const handleOAuthTaskDone = useCallback(async () => {
    setOauthBusy(false)
    setSelectedIds(new Set())
    await load()
  }, [load])

  // ── 获取rt（refresh_token）──
  const startGetRt = async () => {
    setError('')
    const ids = getRtEligibleIds
    if (ids.length === 0) {
      setError(
        getRtTaskMode === 'target'
          ? '\u76ee\u6807\u6a21\u5f0f\u53ea\u80fd\u5bf9\u201c\u4ec5\u6ce8\u518c\u201d\u6216\u201c\u5df2\u83b7\u53d6rt\uff0c\u672a\u4e0a\u4f20\u201d\u72b6\u6001\u7684\u8d26\u53f7\u8d77\u4efb\u52a1\u3002'
          : '\u83b7\u53d6rt \u53ea\u80fd\u5bf9\u201c\u4ec5\u6ce8\u518c\u201d\u72b6\u6001\u7684\u8d26\u53f7\u8d77\u4efb\u52a1\uff0c\u5df2\u6388\u6743\u8d26\u53f7\u4f1a\u88ab\u8fc7\u6ee4\u3002',
      )
      return
    }
    setGetRtBusy(true)
    try {
      const data = await apiFetch('/tasks/get-rt', {
        method: 'POST',
        body: JSON.stringify({
          platform: 'chatgpt',
          ids,
          task_mode: getRtTaskMode,
          executor_type: getRtExecutorType,
          browser_mode: getRtExecutorType === 'protocol' ? '' : browserMode,
          concurrency: Math.max(Number(actionConcurrency || 1), 1),
          record_har: getRtExecutorType === 'protocol' ? '' : (getRtRecordHar ? 'true' : ''),
          sms_provider: normalizeGetRtSmsProviderKey(getRtSmsProvider),
          smsapi_phone: getRtSmsapiPhone.trim(),
          smsapi_url: getRtSmsapiUrl.trim(),
          phone_reuse_count: Math.max(Number(getRtPhoneReuseCount || 3), 3),
          phone_change_limit: Math.max(Number(getRtPhoneChangeLimit || 10), 1),
          sms_balance_action: getRtSmsBalanceAction,
        }),
      })
      setGetRtTaskId(String(data?.task_id || data?.id || ''))
    } catch (exc: any) {
      setError(exc?.message || t('login.requestFailed'))
    } finally {
      setGetRtBusy(false)
    }
  }

  const handleGetRtTaskDone = useCallback(async () => {
    setGetRtBusy(false)
    setSelectedIds(new Set())
    await load()
  }, [load])

  // 复用顶部弹窗：在“更多”菜单点击 获取rt / 获取rt(绕过) 时，先选中当前账号并弹出顶部弹窗
  const triggerGetRtForAccount = useCallback(
    (acc: any, kind: 'get_rt' | 'get_rt_bypass') => {
      const accountId = Number(acc?.id)
      if (!Number.isFinite(accountId)) return
      setError('')
      setSelectedIds(new Set([accountId]))
      if (kind === 'get_rt_bypass') {
        setGetRtBypassConfirmOpen(true)
      } else {
        setGetRtConfirmOpen(true)
      }
    },
    [],
  )

  // ── 获取rt(绕过手机号) ──
  const openGetRtConfirm = () => {
    setError('')
    if (selectedCount === 0) {
      setError(t('accounts.selectAtLeastOne'))
      return
    }
    if (getRtAnyModeEligibleIds.length === 0) {
      setError('\u83b7\u53d6rt \u53ea\u80fd\u5bf9\u201c\u4ec5\u6ce8\u518c\u201d\u6216\u201c\u5df2\u83b7\u53d6rt\uff0c\u672a\u4e0a\u4f20\u201d\u72b6\u6001\u7684\u8d26\u53f7\u8d77\u4efb\u52a1\uff0c\u5176\u4ed6\u8d26\u53f7\u4f1a\u88ab\u8fc7\u6ee4\u3002')
      return
    }
    setGetRtConfirmOpen(true)
  }

  const startGetRtBypass = async () => {
    setError('')
    const ids = [...selectedIds].map(Number)
    if (ids.length === 0) {
      setError('请选择至少 1 个账户')
      return
    }
    setGetRtBypassBusy(true)
    try {
      const data = await apiFetch('/tasks/get-rt-bypass', {
        method: 'POST',
        body: JSON.stringify({
          platform: 'chatgpt',
          ids,
          browser_mode: browserMode,
          concurrency: Math.max(Number(actionConcurrency || 1), 1),
        }),
      })
      setGetRtBypassTaskId(String(data?.task_id || data?.id || ''))
    } catch (exc: any) {
      setError(exc?.message || t('login.requestFailed'))
    } finally {
      setGetRtBypassBusy(false)
    }
  }

  const handleGetRtBypassTaskDone = useCallback(async () => {
    setGetRtBypassBusy(false)
    setSelectedIds(new Set())
    await load()
  }, [load])

  const openBatchStatusModal = () => {
    setError('')
    if (selectedCount === 0) {
      setError(t('accounts.selectAtLeastOne'))
      return
    }
    setBatchStatusOpen(true)
  }

  const submitBatchStatus = async () => {
    setError('')
    const ids = [...selectedIds].map(Number)
    if (ids.length === 0) {
      setError(t('accounts.selectAtLeastOne'))
      return
    }
    setBatchStatusUpdating(true)
    try {
      await apiFetch('/accounts/batch-status', {
        method: 'POST',
        body: JSON.stringify({
          platform: 'chatgpt',
          ids,
          lifecycle_status: batchStatusValue,
        }),
      })
      setBatchStatusOpen(false)
      setSelectedIds(new Set())
      await load()
    } catch (exc: any) {
      setError(exc?.message || t('accounts.operationFailed'))
    } finally {
      setBatchStatusUpdating(false)
    }
  }

  const currentPlatformMeta = platformsMap[tab]
  const platformLabel = currentPlatformMeta?.display_name || (tab === 'chatgpt' ? 'ChatGPT' : tab)
  const visibleTrial = accounts.filter(acc => getPlanState(acc) === 'trial').length
  const visibleFree = accounts.filter(acc => getPlanState(acc) === 'free').length
  const visibleSubscribed = accounts.filter(acc => getPlanState(acc) === 'subscribed').length
  const visibleInvalid = accounts.filter(acc => getValidityStatus(acc) === 'invalid' || getLifecycleStatus(acc) === 'invalid' || getLifecycleStatus(acc) === 'banned').length
  const linkedCashier = accounts.filter(acc => Boolean(getCashierUrl(acc))).length
  const pageCount = Math.max(1, Math.ceil(total / pageSize))
  const entryStart = total === 0 ? 0 : (page - 1) * pageSize + 1
  const entryEnd = total === 0 ? 0 : Math.min((page - 1) * pageSize + accounts.length, total)

  return (
    <div className="min-h-screen bg-[var(--bg-base)]">
      {detail && <DetailModal acc={detail} onClose={() => setDetail(null)} onSave={() => { setDetail(null); load() }} />}
      {showImport && <ImportModal platform={tab} onClose={() => setShowImport(false)} onDone={() => { setShowImport(false); load() }} />}
      {showAdd && <AddModal platform={tab} onClose={() => setShowAdd(false)} onDone={() => { setShowAdd(false); load() }} />}
      {showRegister && <RegisterModal platform={tab} platformMeta={platformsMap[tab]} onClose={() => setShowRegister(false)} onDone={() => load()} />}
      {actionResult && <ActionResultModal title={actionResult.title} payload={actionResult.payload} onClose={() => setActionResult(null)} />}
      {batchStatusOpen && (
        <BatchStatusModal
          count={selectedCount}
          value={batchStatusValue}
          submitting={batchStatusUpdating}
          language={language}
          onChange={setBatchStatusValue}
          onClose={() => setBatchStatusOpen(false)}
          onSubmit={submitBatchStatus}
        />
      )}
      {batchTask && (
        <ActionTaskModal
          title={batchTask.title}
          taskId={batchTask.taskId}
          taskStatus={batchTaskStatus}
          onClose={() => {
            setBatchTask(null)
            setBatchTaskStatus(null)
            setBatchRefreshing(false)
            setBatchHealthChecking(false)
            load()
          }}
          onDone={(status) => {
            setBatchTaskStatus(status)
            setBatchRefreshing(false)
            setBatchHealthChecking(false)
            load()
          }}
        />
      )}
      {oauthTaskId && (
        <TaskLogDialog
          title="Codex OAuth"
          taskId={oauthTaskId}
          taskStatus={null}
          onClose={() => setOauthTaskId('')}
          onDone={handleOAuthTaskDone}
        />
      )}
      {oauthConfirmOpen && (
        createPortal(
          <div
            className="dialog-backdrop"
            onClick={() => !oauthBusy && setOauthConfirmOpen(false)}
          >
            <div
              className="dialog-panel dialog-panel-md flex flex-col overflow-hidden rounded-xl border-[var(--border-soft)] bg-[var(--bg-card)] shadow-[var(--shadow-hard)]"
              onClick={event => event.stopPropagation()}
              style={{ width: 'min(560px, calc(100vw - 32px))', maxHeight: 'min(620px, calc(100dvh - 48px))' }}
            >
              <div className="shrink-0 flex items-center justify-between border-b border-[var(--border-soft)] bg-[var(--bg-elevated)] px-6 py-4">
                <div>
                  <h2 className="text-base font-semibold text-[var(--text-primary)]">
                    Codex OAuth
                  </h2>
                  <div className="mt-1 text-xs help-text">
                    {t('accounts.selected', { count: selectedIds.size })}
                  </div>
                </div>
                <button
                  onClick={() => !oauthBusy && setOauthConfirmOpen(false)}
                  className="text-[var(--text-muted)] hover:text-[var(--text-primary)]"
                >
                  <X className="h-4 w-4" />
                </button>
              </div>
              <div className="min-h-0 flex-1 space-y-4 overflow-y-auto bg-[var(--bg-base)] px-6 py-5">
                {getRtExecutorType !== 'protocol' && (
                  <div>
                    <label className="mb-1 block text-xs font-medium text-[var(--text-secondary)]">
                      {t('accounts.browserMode')}
                    </label>
                    <select
                      value={browserMode}
                      onChange={event => setBrowserMode(event.target.value)}
                      className="control-surface control-surface-compact w-full"
                    >
                      {BROWSER_MODE_OPTIONS.map(option => (
                        <option key={option.value} value={option.value}>
                          {option.label}
                        </option>
                      ))}
                    </select>
                  </div>
                )}
                <div>
                  <label className="mb-1 block text-xs font-medium text-[var(--text-secondary)]">
                    {t('accounts.concurrency')}
                  </label>
                  <input
                    type="number"
                    min={1}
                    value={actionConcurrency}
                    onChange={event =>
                      setActionConcurrency(Math.max(Number(event.target.value || 1), 1))
                    }
                    className="control-surface control-surface-compact w-full text-center"
                  />
                </div>
              </div>
              <div className="shrink-0 flex justify-end gap-2 border-t border-[var(--border-soft)] bg-[var(--bg-elevated)] px-6 py-3">
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => setOauthConfirmOpen(false)}
                  disabled={oauthBusy}
                >
                  {t('common.close')}
                </Button>
                <Button
                  size="sm"
                  onClick={async () => {
                    setOauthConfirmOpen(false)
                    await startCodexOAuth()
                  }}
                  disabled={oauthBusy || selectedIds.size === 0}
                >
                  {oauthBusy ? (
                    <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                  ) : (
                    <ShieldCheck className="mr-2 h-4 w-4" />
                  )}
                  {t('accounts.startGetRt')}
                </Button>
              </div>
            </div>
          </div>,
          document.body,
        )
      )}
      {getRtConfirmOpen && (
        createPortal(
          <div
            className="dialog-backdrop"
            onClick={() => !getRtBusy && setGetRtConfirmOpen(false)}
          >
            <div
              className="dialog-panel dialog-panel-md flex flex-col overflow-hidden rounded-xl border-[var(--border-soft)] bg-[var(--bg-card)] shadow-[var(--shadow-hard)]"
              onClick={event => event.stopPropagation()}
              style={{ width: 'min(720px, calc(100vw - 32px))', maxHeight: 'min(760px, calc(100dvh - 48px))' }}
            >
              <div className="shrink-0 flex items-center justify-between border-b border-[var(--border-soft)] bg-[var(--bg-elevated)] px-6 py-4">
                <div>
                  <h2 className="text-base font-semibold text-[var(--text-primary)]">
                    获取rt（refresh_token）
                  </h2>
                  <div className="mt-1 text-xs help-text">
                    已选 {selectedIds.size} 个账户。使用浏览器 OAuth + 手机验证跳过获取 refresh_token。
                  </div>
                </div>
                <button
                  onClick={() => !getRtBusy && setGetRtConfirmOpen(false)}
                  className="text-[var(--text-muted)] hover:text-[var(--text-primary)]"
                >
                  <X className="h-4 w-4" />
                </button>
              </div>
              <div className="min-h-0 flex-1 space-y-4 overflow-y-auto bg-[var(--bg-base)] px-6 py-5">
                <div>
                  <label className="mb-2 block text-xs font-medium text-[var(--text-secondary)]">
                    {'\u4efb\u52a1\u6a21\u5f0f'}
                  </label>
                  <div className="grid gap-3 sm:grid-cols-2">
                    {[
                      {
                        value: 'single',
                        title: '\u5355\u8f6e\u6a21\u5f0f',
                        desc: '\u6bcf\u4e2a\u8d26\u53f7\u53ea\u6267\u884c\u4e00\u6b21\uff0c\u4e0d\u7ba1\u6210\u529f\u6216\u5931\u8d25\uff0c\u672c\u8f6e\u5b8c\u6210\u540e\u7ed3\u675f\u4efb\u52a1\u3002',
                      },
                      {
                        value: 'target',
                        title: '\u76ee\u6807\u6a21\u5f0f',
                        desc: '\u4ee5\u201c\u5df2\u83b7\u53d6rt\uff0c\u5df2\u4e0a\u4f20\u201d\u4e3a\u7ec8\u6781\u76ee\u6807\uff0c\u5931\u8d25\u540e 10s \u81ea\u52a8\u91cd\u8bd5\uff0c\u4f59\u989d\u4e0d\u8db3\u65f6\u5207\u6362\u4e0b\u4e00\u4e2a\u5df2\u542f\u7528\u63a5\u7801\u5e73\u53f0\u3002',
                      },
                    ].map(option => {
                      const active = getRtTaskMode === option.value
                      return (
                        <button
                          key={option.value}
                          type="button"
                          onClick={() => setGetRtTaskMode(option.value as 'single' | 'target')}
                          className={cn(
                            'rounded-xl border px-4 py-3 text-left transition hover:border-[var(--accent)]/70',
                            active
                              ? 'border-[var(--accent)] bg-[var(--bg-elevated)] shadow-[0_0_0_1px_var(--accent)]'
                              : 'border-[var(--border)] bg-[var(--bg-hover)]',
                          )}
                        >
                          <div className="flex items-center justify-between gap-3">
                            <span className="text-sm font-semibold text-[var(--text-primary)]">{option.title}</span>
                            <span
                              className={cn(
                                'h-2.5 w-2.5 rounded-full border',
                                active ? 'border-[var(--accent-edge)] bg-[var(--gradient-accent)]' : 'border-[var(--text-muted)]',
                              )}
                            />
                          </div>
                          <div className="mt-1.5 help-text-xs">
                            {option.desc}
                          </div>
                        </button>
                      )
                    })}
                  </div>
                  {getRtTaskMode === 'target' ? (
                    <div className="notice-panel notice-panel-warning mt-2 px-3 py-2 text-[11px]">
                      {'\u76ee\u6807\u6a21\u5f0f\u4f1a\u6301\u7eed\u6d88\u8017\u53ef\u7528\u63a5\u7801\u8d44\u6e90\uff0c\u76f4\u5230\u5168\u90e8\u8d26\u53f7\u8fbe\u5230\u201c\u5df2\u83b7\u53d6rt\uff0c\u5df2\u4e0a\u4f20\u201d\uff0c\u6216\u6240\u6709\u5df2\u914d\u7f6e\u63a5\u7801\u5e73\u53f0\u5747\u4f59\u989d\u4e0d\u8db3 / \u9047\u5230\u786c\u6027\u5931\u8d25\u3002'}
                    </div>
                  ) : null}
                </div>
                {getRtTaskMode === 'target' && (
                  <div>
                    <label className="mb-2 block text-xs font-medium text-[var(--text-secondary)]">
                      {'\u63a5\u7801\u5e73\u53f0\u4f59\u989d\u4e0d\u8db3\u65f6'}
                    </label>
                    <div className="grid gap-2 md:grid-cols-3">
                      {GET_RT_SMS_BALANCE_ACTION_OPTIONS.map(option => {
                        const active = getRtSmsBalanceAction === option.value
                        return (
                          <button
                            key={option.value}
                            type="button"
                            onClick={() => setGetRtSmsBalanceAction(option.value)}
                            className={cn(
                              'rounded-xl border px-3 py-3 text-left transition hover:border-[var(--accent)]/70',
                              active
                                ? 'border-[var(--accent)] bg-[var(--bg-elevated)] shadow-[0_0_0_1px_var(--accent)]'
                                : 'border-[var(--border)] bg-[var(--bg-hover)]',
                            )}
                          >
                            <div className="flex items-center justify-between gap-3">
                              <span className="text-xs font-semibold text-[var(--text-primary)]">{option.title}</span>
                              <span
                                className={cn(
                                  'h-2.5 w-2.5 shrink-0 rounded-full border',
                                  active ? 'border-[var(--accent-edge)] bg-[var(--gradient-accent)]' : 'border-[var(--text-muted)]',
                                )}
                              />
                            </div>
                            <div className="mt-1.5 help-text-xs">
                              {option.desc}
                            </div>
                          </button>
                        )
                      })}
                    </div>
                  </div>
                )}
                <div>
                  <label className="mb-1 block text-xs font-medium text-[var(--text-secondary)]">
                    {'\u6267\u884c\u65b9\u5f0f'}
                  </label>
                  <select
                    value={getRtExecutorType}
                    onChange={event => setGetRtExecutorType(event.target.value)}
                    className="control-surface control-surface-compact w-full"
                  >
                    <option value="browser">{'\u6d4f\u89c8\u5668\u6a21\u5f0f'}</option>
                    <option value="protocol">{'\u534f\u8bae\u6a21\u5f0f'}</option>
                  </select>
                  <div className="mt-1 help-text-xs">
                    {'\u534f\u8bae\u6a21\u5f0f\u6309 HAR \u590d\u523b OAuth \u94fe\u8def\uff1b\u6d4f\u89c8\u5668\u6a21\u5f0f\u4fdd\u7559\u73b0\u6709\u9875\u9762\u81ea\u52a8\u5316\u6d41\u7a0b\u3002'}
                  </div>
                </div>
                {getRtExecutorType !== 'protocol' && (
                  <div>
                    <label className="mb-1 block text-xs font-medium text-[var(--text-secondary)]">
                      {t('accounts.browserMode')}
                    </label>
                    <select
                      value={browserMode}
                      onChange={event => setBrowserMode(event.target.value)}
                      className="control-surface control-surface-compact w-full"
                    >
                      {BROWSER_MODE_OPTIONS.map(option => (
                        <option key={option.value} value={option.value}>
                          {option.label}
                        </option>
                      ))}
                    </select>
                  </div>
                )}
                <div>
                  <label className="mb-1 block text-xs font-medium text-[var(--text-secondary)]">
                    {t('accounts.concurrency')}
                  </label>
                  <input
                    type="number"
                    min={1}
                    value={actionConcurrency}
                    onChange={event =>
                      setActionConcurrency(Math.max(Number(event.target.value || 1), 1))
                    }
                    className="control-surface control-surface-compact w-full text-center"
                  />
                </div>
                <div>
                  <label className="mb-1 block text-xs font-medium text-[var(--text-secondary)]">
                    {t('accounts.phoneReuseCount')}
                  </label>
                  <input
                    type="number"
                    min={3}
                    value={getRtPhoneReuseCount}
                    onChange={event =>
                      setGetRtPhoneReuseCount(Math.max(Number(event.target.value || 3), 3))
                    }
                    className="control-surface control-surface-compact w-full text-center"
                  />
                  <div className="mt-1 help-text-xs">
                    {t('accounts.phoneReuseHint')}
                  </div>
                </div>
                <div>
                  <label className="mb-1 block text-xs font-medium text-[var(--text-secondary)]">
                    {t('accounts.phoneChangeLimit')}
                  </label>
                  <input
                    type="number"
                    min={1}
                    value={getRtPhoneChangeLimit}
                    onChange={event =>
                      setGetRtPhoneChangeLimit(Math.max(Number(event.target.value || 10), 1))
                    }
                    className="control-surface control-surface-compact w-full text-center"
                  />
                  <div className="mt-1 help-text-xs">
                    {t('accounts.phoneChangeLimitHint')}
                  </div>
                </div>
                {getRtExecutorType !== 'protocol' && (
                  <label className="flex items-start gap-2 rounded-xl border border-[var(--border-soft)] bg-[var(--bg-hover)] px-4 py-3 cursor-pointer hover:border-[var(--accent)]/60">
                    <input
                      type="checkbox"
                      checked={getRtRecordHar}
                      onChange={event => setGetRtRecordHar(event.target.checked)}
                      className="mt-0.5 h-4 w-4 cursor-pointer accent-[var(--accent)]"
                    />
                    <div className="flex-1 text-xs text-[var(--text-secondary)]">
                      <div className="text-sm font-medium text-[var(--text-primary)]">
                        {t('accounts.captureCamoufoxHar')}
                      </div>
                      <div className="mt-0.5">
                        {t('accounts.captureCamoufoxHarDesc')}
                      </div>
                      {getRtRecordHar && !browserMode.startsWith('camoufox_') ? (
                        <div className="notice-panel notice-panel-warning mt-2 px-3 py-1.5 text-[11px]">
                          {t('accounts.captureHarUnsupported')}
                        </div>
                      ) : null}
                    </div>
                  </label>
                )}
                {/* ── 手机号接码（可选）── */}
                <div className="rounded-xl border border-[var(--border-soft)] bg-[var(--bg-hover)] px-4 py-3 space-y-3">
                  <div className="text-sm font-medium text-[var(--text-primary)]">手机号接码（可选，跳过则遇到 add_phone 会失败）</div>
                  <div>
                    <label className="mb-1 block text-xs font-medium text-[var(--text-secondary)]">{t('accounts.smsChannel')}</label>
                    <select
                      value={getRtSmsProvider}
                      onChange={e => setGetRtSmsProvider(e.target.value)}
                      className="control-surface control-surface-compact w-full"
                    >
                      {getRtSmsProviderOptions.map(option => (
                        <option key={option.value || 'disabled'} value={option.value}>
                          {option.label}
                        </option>
                      ))}
                    </select>
                  </div>
                  {getRtSmsProvider && getRtSmsProvider !== 'none' && !['smspool', 'smsapi'].includes(getRtSmsProvider) && (
                    <div className="notice-panel notice-panel-info px-3 py-2 text-[11px]">
                      {t('accounts.smsSavedConfigHint')}
                    </div>
                  )}
                  {getRtSmsProvider === 'smspool' && (
                    <div className="notice-panel notice-panel-info px-3 py-2 text-[11px]">
                      {'SMSPool API Key\u3001\u56fd\u5bb6\u3001\u670d\u52a1\u3001\u4ef7\u683c\u4e0a\u9650\u7b49\u53c2\u6570\u5c06\u76f4\u63a5\u4f7f\u7528\u201c\u8bbe\u7f6e -> \u63a5\u7801\u670d\u52a1\u201d\u91cc\u7684 SMSPool \u914d\u7f6e\u3002'}
                    </div>
                  )}
                  {getRtSmsProvider === 'smsapi' && (
                    <>
                      <div>
                        <label className="mb-1 block text-xs font-medium text-[var(--text-secondary)]">
                          手机号 + 查询 URL（支持 +1XXXXXXXX----URL 格式）
                        </label>
                        <textarea
                          value={getRtSmsapiPhone}
                          onChange={e => setGetRtSmsapiPhone(e.target.value)}
                          rows={3}
                          placeholder={"+17857019646----https://xxx/api/sms/recordText?key=xxx\n+17857019647----https://xxx/api/sms/recordText?key=yyy"}
                          className="control-surface control-surface-compact w-full resize-none font-mono text-xs"
                        />
                      </div>
                      <div>
                        <label className="mb-1 block text-xs font-medium text-[var(--text-secondary)]">
                          查询 URL（与手机号分开填写时用；上面的手机号字段已含 ----URL 则无需填写）
                        </label>
                        <input
                          type="text"
                          value={getRtSmsapiUrl}
                          onChange={e => setGetRtSmsapiUrl(e.target.value)}
                          placeholder="https://mail-api.yuecheng.shop/api/sms/recordText?key=xxx"
                          className="control-surface control-surface-compact w-full"
                        />
                      </div>
                    </>
                  )}
                  <div className="help-text-xs">
                    浏览器填表时自动去除 +1 区号，使用本地号码格式。
                  </div>
                </div>
              </div>
              <div className="shrink-0 flex justify-end gap-2 border-t border-[var(--border-soft)] bg-[var(--bg-elevated)] px-6 py-3">
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => setGetRtConfirmOpen(false)}
                  disabled={getRtBusy}
                >
                  {t('common.close')}
                </Button>
                <Button
                  size="sm"
                  onClick={async () => {
                    setGetRtConfirmOpen(false)
                    await startGetRt()
                  }}
                  disabled={getRtBusy || selectedIds.size === 0}
                >
                  {getRtBusy ? (
                    <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                  ) : (
                    <Zap className="mr-2 h-4 w-4" />
                  )}
                  {t('accounts.startGetRt')}
                </Button>
              </div>
            </div>
          </div>,
          document.body,
        )
      )}
      {getRtTaskId && (
        <TaskLogDialog
          title={t('accounts.getRt')}
          taskId={getRtTaskId}
          taskStatus={null}
          onClose={() => setGetRtTaskId('')}
          onDone={handleGetRtTaskDone}
        />
      )}
      {/* ── 获取rt(绕过) 确认弹窗 ── */}
      {getRtBypassConfirmOpen && (
        createPortal(
          <div className="dialog-backdrop" onClick={() => !getRtBypassBusy && setGetRtBypassConfirmOpen(false)}>
            <div
              className="dialog-panel dialog-panel-md flex flex-col overflow-hidden rounded-xl border-[var(--border-soft)] bg-[var(--bg-card)] shadow-[var(--shadow-hard)]"
              onClick={event => event.stopPropagation()}
              style={{ width: 'min(640px, calc(100vw - 32px))', maxHeight: 'min(620px, calc(100dvh - 48px))' }}
            >
              <div className="shrink-0 flex items-center justify-between border-b border-[var(--border-soft)] bg-[var(--bg-elevated)] px-6 py-4">
                <div>
                  <h2 className="text-base font-semibold text-[var(--text-primary)]">获取rt（绕过手机号）</h2>
                  <div className="mt-1 text-xs text-[var(--text-muted)]">
                    已选 {selectedIds.size} 个账户。拦截 session/select 跳过手机验证。
                  </div>
                </div>
                <button onClick={() => !getRtBypassBusy && setGetRtBypassConfirmOpen(false)} className="text-[var(--text-muted)] hover:text-[var(--text-primary)]"><X className="h-4 w-4" /></button>
              </div>
              <div className="min-h-0 flex-1 space-y-4 overflow-y-auto bg-[var(--bg-base)] px-6 py-5">
                <div>
                  <label className="mb-1 block text-xs text-[var(--text-muted)]">{t('accounts.browserMode')}</label>
                  <select value={browserMode} onChange={event => setBrowserMode(event.target.value)} className="control-surface control-surface-compact w-full">
                    {BROWSER_MODE_OPTIONS.map(option => (<option key={option.value} value={option.value}>{option.label}</option>))}
                  </select>
                </div>
                <div>
                  <label className="mb-1 block text-xs text-[var(--text-muted)]">{t('accounts.concurrency')}</label>
                  <input type="number" min={1} value={actionConcurrency}
                    onChange={event => setActionConcurrency(Math.max(Number(event.target.value || 1), 1))}
                    className="control-surface control-surface-compact w-full text-center" />
                </div>
                <div className="rounded-xl border border-[var(--border-soft)] bg-[var(--bg-hover)] px-4 py-3 text-xs text-[var(--text-secondary)]">
                  拦截 POST session/select 响应，将 phone_otp_* 替换为 consent 类型，浏览器直接跳授权同意页。
                </div>
              </div>
              <div className="shrink-0 flex justify-end gap-2 border-t border-[var(--border-soft)] bg-[var(--bg-elevated)] px-6 py-3">
                <Button variant="outline" size="sm" onClick={() => setGetRtBypassConfirmOpen(false)} disabled={getRtBypassBusy}>{t('common.close')}</Button>
                <Button size="sm" onClick={async () => { setGetRtBypassConfirmOpen(false); await startGetRtBypass() }} disabled={getRtBypassBusy || selectedIds.size === 0}>
                  {getRtBypassBusy ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <ShieldCheck className="mr-2 h-4 w-4" />}
                  {t('accounts.startGetRt')}
                </Button>
              </div>
            </div>
          </div>,
          document.body,
        )
      )}
      {/* ── 获取rt(绕过) 任务日志 ── */}
      {getRtBypassTaskId && (
        <TaskLogDialog
          title={t('accounts.getRtBypass')}
          taskId={getRtBypassTaskId}
          taskStatus={null}
          onClose={() => setGetRtBypassTaskId('')}
          onDone={handleGetRtBypassTaskDone}
        />
      )}
      {error && (
        <div className="rounded-lg border border-red-500/20 bg-red-500/10 px-3 py-2 text-sm text-red-300">
          {error}
        </div>
      )}

      <div className="min-h-screen bg-[var(--bg-base)] text-[var(--text-primary)]">
        <header className="sticky top-0 z-40 flex h-16 items-center justify-between gap-4 border-b border-transparent bg-[var(--bg-base)]/80 px-8 backdrop-blur-md">
          <div className="flex min-w-0 flex-1 items-center gap-4">
            <h1 className="shrink-0 whitespace-nowrap text-[16px] font-semibold tracking-normal text-[var(--text-primary)]">
              {t('accounts.managementTitle', { platform: platformLabel })}
            </h1>
            <div className={cn(
              "relative hidden",
              language === 'en-US' ? "w-[160px] shrink-0 xl:block" : "min-w-[112px] max-w-[180px] flex-1 md:block",
            )}>
              <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-[var(--text-muted)]" />
              <input
                type="text"
                placeholder={t('accounts.searchPlaceholder')}
                value={search}
                onChange={e => setSearch(e.target.value)}
                className="h-9 w-full rounded-lg border-0 bg-[#ededed] pl-10 pr-4 text-[12px] text-[var(--text-primary)] outline-none transition-all placeholder:text-[var(--text-muted)] focus:ring-2 focus:ring-[rgba(var(--accent-rgb),0.2)] dark:bg-[var(--bg-input)]"
              />
            </div>
          </div>
          <div className="flex shrink-0 items-center gap-4">
            <nav className="hidden items-center gap-4 lg:flex">
              <button onClick={() => setShowRegister(true)} className="border-b-2 border-[var(--accent)] pb-1 text-[11px] font-bold text-[var(--accent)]">
                {t('accounts.autoRegister')}
              </button>
              <button onClick={() => setShowImport(true)} className="pb-1 text-[11px] font-medium text-[var(--text-secondary)] transition-colors hover:text-[var(--accent)]">
                {t('accounts.import')}
              </button>
              {tab === 'chatgpt' ? (
                <ExportMenu
                  platform={tab}
                  total={total}
                  statusFilter={filterStatus}
                  searchFilter={debouncedSearch}
                  selectedIds={[...selectedIds]}
                  showIcon={false}
                  triggerClassName="h-auto border-0 bg-transparent px-0 py-0 pb-1 text-[11px] font-medium text-[var(--text-secondary)] shadow-none hover:bg-transparent hover:text-[var(--accent)]"
                />
              ) : (
                <button onClick={exportCsv} disabled={accounts.length === 0} className="pb-1 text-[11px] font-medium text-[var(--text-secondary)] transition-colors hover:text-[var(--accent)] disabled:opacity-40">
                  {t('accounts.export')}
                </button>
              )}
              <button onClick={() => setShowAdd(true)} className="pb-1 text-[11px] font-medium text-[var(--text-secondary)] transition-colors hover:text-[var(--accent)]">
                {t('accounts.manualAdd')}
              </button>
            </nav>
            <div className="flex items-center gap-3">
              <button
                onClick={() => load()}
                disabled={loading}
                className="flex h-9 w-9 items-center justify-center rounded-full text-[var(--text-secondary)] transition-colors hover:bg-[var(--bg-pane)] disabled:opacity-50"
              >
                <RefreshCw className={cn("h-4 w-4", loading && "animate-spin")} />
              </button>
              <div className="relative flex h-8 w-8 items-center justify-center rounded-full border border-[var(--accent-edge)] bg-[var(--gradient-accent)] text-[11px] font-bold text-white shadow-sm">
                A
              </div>
            </div>
          </div>
        </header>

        <section className="mx-auto max-w-[1440px] p-8">
          <div className="mb-8 flex flex-wrap items-center gap-4">
            <Button
              size="sm"
              onClick={() => setShowRegister(true)}
              className="h-10 rounded-lg px-5 text-[13px] font-semibold shadow-[var(--shadow-soft)] transition-all hover:-translate-y-0.5"
            >
              <Plus className="mr-2 h-4 w-4" />
              {t('accounts.autoRegister')}
            </Button>
            <Button
              size="sm"
              variant="outline"
              onClick={() => setShowImport(true)}
              className="h-10 rounded-lg border-[var(--border-soft)] bg-[var(--bg-card)] px-5 text-[13px] font-medium shadow-[var(--shadow-soft)] hover:bg-[var(--bg-pane)]"
            >
              <Upload className="mr-2 h-4 w-4" />
              {t('accounts.import')}
            </Button>
            {tab === 'chatgpt' ? (
              <Button
                size="sm"
                variant="outline"
                disabled={oauthBusy}
                onClick={() => {
                  setError('')
                  if (selectedCount === 0) {
                    setError(t('accounts.selectAtLeastOne'))
                    return
                  }
                  setOauthConfirmOpen(true)
                }}
                className="h-10 rounded-lg border-[var(--border-soft)] bg-[var(--bg-card)] px-5 text-[13px] font-medium shadow-[var(--shadow-soft)] hover:bg-[var(--bg-pane)]"
              >
                {oauthBusy ? (
                  <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                ) : (
                  <ShieldCheck className="mr-2 h-4 w-4" />
                )}
                {t('accounts.codexOAuth')}
              </Button>
            ) : null}
            {tab === 'chatgpt' ? (
              <Button
                size="sm"
                variant="outline"
                disabled={getRtBusy}
                onClick={openGetRtConfirm}
                className="h-10 rounded-lg border-[var(--border-soft)] bg-[var(--bg-card)] px-5 text-[13px] font-medium shadow-[var(--shadow-soft)] hover:bg-[var(--bg-pane)]"
              >
                {getRtBusy ? (
                  <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                ) : (
                  <Zap className="mr-2 h-4 w-4" />
                )}
                {t('accounts.getRt')}
              </Button>
            ) : null}
            {tab === 'chatgpt' ? (
              <Button
                size="sm"
                variant="outline"
                disabled={batchStatusUpdating || selectedCount === 0}
                onClick={openBatchStatusModal}
                className="h-10 rounded-lg border-[var(--border-soft)] bg-[var(--bg-card)] px-5 text-[13px] font-medium shadow-[var(--shadow-soft)] hover:bg-[var(--bg-pane)]"
              >
                {batchStatusUpdating ? (
                  <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                ) : (
                  <ListChecks className="mr-2 h-4 w-4" />
                )}
                {'\u4fee\u6539\u72b6\u6001'}
              </Button>
            ) : null}
            <div className="ml-auto flex flex-wrap items-center gap-4">
              <div className="rounded-lg border border-[var(--border-soft)] bg-[var(--bg-pane)] px-4 py-2">
                <span className="mr-2 text-[10px] font-semibold uppercase tracking-normal text-[var(--text-muted)]">
                  {t('accounts.totalAccounts')}
                </span>
                <span className="text-[16px] font-bold text-[var(--text-primary)]">
                  {total.toLocaleString(language === 'zh-CN' ? 'zh-CN' : 'en-US')}
                </span>
              </div>
            </div>
          </div>

          <div className="overflow-hidden rounded-xl border border-[var(--border-soft)] bg-[var(--bg-card)] shadow-[var(--shadow-soft)]">
            <div className="flex flex-col gap-4 border-b border-[var(--border-soft)] bg-[var(--bg-elevated)] p-6 lg:flex-row lg:items-center lg:justify-between">
              <div className="flex min-w-0 flex-wrap items-center gap-4">
                <h2 className="text-[16px] font-bold tracking-normal text-[var(--text-primary)]">
                  {t('accounts.registryTitle')}
                </h2>
                <div className="flex flex-wrap gap-1">
                  <span className="rounded-full bg-emerald-500/10 px-3 py-0.5 text-[11px] font-medium text-emerald-600 dark:text-emerald-400">
                    {t('accounts.freeTier')}
                    {visibleFree > 0 ? ` ${visibleFree}` : ''}
                  </span>
                  <span className="rounded-full bg-[rgba(var(--accent-rgb),0.1)] px-3 py-0.5 text-[11px] font-medium text-[var(--accent)]">
                    {t('accounts.usRegion')}
                  </span>
                  {selectedCount > 0 && (
                    <span className="rounded-full bg-[var(--bg-pane)] px-3 py-0.5 text-[11px] font-medium text-[var(--text-secondary)]">
                      {t('accounts.selected', { count: selectedCount })}
                    </span>
                  )}
                </div>
              </div>
              <div className="flex flex-wrap items-center gap-2">
                <select
                  value={filterStatus}
                  onChange={e => setFilterStatus(e.target.value)}
                  className="h-8 rounded-lg border-0 bg-[var(--bg-pane)] pl-3 pr-8 text-[12px] text-[var(--text-secondary)] outline-none focus:ring-1 focus:ring-[var(--accent)]"
                >
                  <option value="">{t('accounts.allStatuses')}</option>
                  {ACCOUNT_STATUS_FILTER_OPTIONS.map(status => (
                    <option key={status} value={status}>
                      {status === 'eligible' ? t('accounts.eligible') : translateAccountStatus(status, language)}
                    </option>
                  ))}
                </select>
                {tab === 'chatgpt' && (
                  <Button
                    variant="outline"
                    size="sm"
                    disabled={batchHealthChecking || batchRefreshing || loading}
                    className="h-8 rounded-lg border-0 bg-[var(--bg-pane)] px-3 text-[12px] font-medium"
                    title={t('accounts.healthCheckTitle')}
                    onClick={startHealthCheck}
                  >
                    <ShieldCheck className={cn("mr-1.5 h-3.5 w-3.5", batchHealthChecking && "animate-pulse")} />
                    {batchHealthChecking ? t('accounts.healthChecking') : t('accounts.healthCheck')}
                  </Button>
                )}
                <Button
                  variant="outline"
                  size="sm"
                  disabled={batchRefreshing || batchHealthChecking || loading}
                  className="h-8 rounded-lg border-0 bg-[var(--bg-pane)] px-3 text-[12px] font-medium"
                  title={t('accounts.refreshCreditsTitle')}
                  onClick={async () => {
                    setBatchRefreshing(true)
                    try {
                      const res = await apiFetch(`/accounts/check-all?platform=${tab}`, { method: 'POST' })
                      if (res?.task_id) {
                        setBatchTask({ taskId: res.task_id, title: t('accounts.refreshAllCreditsTask', { platform: platformLabel }) })
                        setBatchTaskStatus(null)
                      }
                    } catch (e) {
                      console.error(e)
                      setBatchRefreshing(false)
                    }
                  }}
                >
                  <Zap className={cn("mr-1.5 h-3.5 w-3.5", batchRefreshing && "animate-pulse")} />
                  {batchRefreshing ? t('accounts.refreshingCredits') : t('accounts.refreshCredits')}
                </Button>
                {tab === 'chatgpt' && selectedCount > 0 && (
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={batchStatusUpdating}
                    className="h-8 rounded-lg border-0 bg-[var(--bg-pane)] px-3 text-[12px] font-medium"
                    onClick={openBatchStatusModal}
                  >
                    {batchStatusUpdating ? (
                      <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />
                    ) : (
                      <ListChecks className="mr-1.5 h-3.5 w-3.5" />
                    )}
                    {'\u4fee\u6539\u72b6\u6001'}
                  </Button>
                )}
                {selectedCount > 0 && (
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={bulkDeleting}
                    className="h-8 rounded-lg border-0 bg-red-500/10 px-3 text-[12px] font-medium text-red-600 hover:bg-red-500/15"
                    onClick={async () => {
                      if (!confirm(t('accounts.deleteSelectedConfirm', { count: selectedCount }))) return
                      setBulkDeleting(true)
                      try {
                        await Promise.allSettled(
                          [...selectedIds].map(id => apiFetch(`/accounts/${id}`, { method: 'DELETE' }))
                        )
                        setSelectedIds(new Set())
                        load()
                      } finally {
                        setBulkDeleting(false)
                      }
                    }}
                  >
                    <Trash2 className="mr-1.5 h-3.5 w-3.5" />
                    {bulkDeleting ? t('common.deleting') : t('common.delete')}
                  </Button>
                )}
              </div>
            </div>

            <div className="overflow-x-auto">
              <table className="w-full table-fixed border-collapse text-left">
                <colgroup>
                  <col className="w-[7%]" />
                  <col className="w-[28%]" />
                  <col className="w-[15%]" />
                  <col className="w-[20%]" />
                  <col className="w-[10%]" />
                  <col className="w-[20%]" />
                </colgroup>
                <thead>
                  <tr className="bg-[var(--bg-pane)]/30 text-[10px] uppercase tracking-wider text-[var(--text-muted)]">
                    <th className="px-4 py-4 text-center font-semibold">
                      <input
                        type="checkbox"
                        checked={allSelectedOnPage}
                        onChange={togglePage}
                        className="checkbox-accent rounded border-[var(--border)] text-[var(--accent)] focus:ring-[var(--accent)]"
                      />
                    </th>
                    <th className="px-4 py-4 font-semibold">{t('accounts.accountInfo')}</th>
                    <th className="px-4 py-4 font-semibold">{t('common.password')}</th>
                    <th className="whitespace-nowrap px-4 py-4 font-semibold">{t('common.status')}</th>
                    <th className="whitespace-nowrap px-4 py-4 font-semibold">{t('accounts.registeredAt')}</th>
                    <th className="whitespace-nowrap px-2 py-4 text-right font-semibold">{t('common.actions')}</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-[var(--border-soft)]">
                  {accounts.length === 0 && (
                    <tr>
                      <td colSpan={6} className="px-6 py-20 text-center">
                        <div className="mx-auto flex max-w-sm flex-col items-center gap-3">
                          <div className="flex h-12 w-12 items-center justify-center rounded-full border border-[var(--border-soft)] bg-[var(--bg-pane)]">
                            <Mail className="h-5 w-5 text-[var(--text-muted)]" />
                          </div>
                          <div className="text-[13px] font-semibold text-[var(--text-primary)]">
                            {t('accounts.emptyTitle')}
                          </div>
                          <div className="text-[12px] text-[var(--text-muted)]">
                            {t('accounts.emptyDesc')}
                          </div>
                        </div>
                      </td>
                    </tr>
                  )}
                  {accounts.map(acc => (
                    (() => {
                      const overview = getAccountOverview(acc)
                      const verificationMailbox = getVerificationMailbox(acc)
                      const primaryMetrics = getPrimaryMetrics(acc)
                      const displayBadges = getDisplayBadges(acc)
                      const status = getDisplayStatus(acc)
                      const variant = String(STATUS_VARIANT[status] || 'secondary')
                      const statusStyles = (({
                        success: "bg-emerald-500/10 text-emerald-600 dark:text-emerald-400",
                        warning: "bg-amber-500/10 text-amber-600 dark:text-amber-400",
                        danger: "bg-red-500/10 text-red-600 dark:text-red-400",
                        secondary: "bg-[var(--bg-pane)] text-[var(--text-secondary)]",
                        default: "bg-[rgba(var(--accent-rgb),0.1)] text-[var(--accent)]",
                      } as Record<string, string>)[variant]) || "bg-[var(--bg-pane)] text-[var(--text-secondary)]"
                      const statusDot = variant === 'success'
                        ? 'bg-emerald-500'
                        : variant === 'warning'
                          ? 'bg-amber-500'
                          : variant === 'danger'
                            ? 'bg-red-500'
                            : variant === 'default'
                              ? 'bg-[var(--gradient-accent)]'
                              : 'bg-[var(--text-muted)]'

                      return (
                        <tr
                          key={acc.id}
                          className="group cursor-pointer transition-colors hover:bg-[var(--bg-pane)]/35"
                          onClick={() => setDetail(acc)}
                        >
                          <td className="px-4 py-4 text-center align-top" onClick={e => e.stopPropagation()}>
                            <input
                              type="checkbox"
                              checked={selectedIds.has(acc.id)}
                              onChange={() => toggleOne(acc.id)}
                              className="checkbox-accent rounded border-[var(--border)] text-[var(--accent)] opacity-60 transition-opacity group-hover:opacity-100 focus:ring-[var(--accent)]"
                            />
                          </td>
                          <td className="overflow-hidden px-4 py-4 align-top">
                            <div className="flex min-w-0 gap-2.5">
                              <div className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-[rgba(var(--accent-rgb),0.08)] text-[var(--accent)]">
                                <Mail className="h-4 w-4" />
                              </div>
                              <div className="min-w-0">
                                <div className="flex min-w-0 items-center gap-2">
                                  <span className="truncate font-mono text-[13px] font-medium text-[var(--text-primary)]" title={acc.email}>
                                    {acc.email}
                                  </span>
                                  <button
                                    onClick={e => { e.stopPropagation(); copy(emailApiLine(acc.email)) }}
                                    className="text-[var(--text-muted)] opacity-0 transition-opacity hover:text-[var(--accent)] group-hover:opacity-100"
                                    aria-label="copy"
                                  >
                                    <Copy className="h-3.5 w-3.5" />
                                  </button>
                                </div>
                                <div className="mt-1 truncate text-[11px] text-[var(--text-muted)]">
                                  {verificationMailbox?.email || overview?.remote_email || acc.username || acc.email}
                                </div>
                                {displayBadges.length > 0 && (
                                  <div className="mt-1.5 flex flex-wrap gap-1">
                                    {displayBadges.slice(0, 2).map((badge: any, index: number) => (
                                      <span key={`${badge?.label || 'badge'}-${index}`} className={getAccountBadgeClassName(badge, 'modern')}>
                                        {badge?.label}
                                      </span>
                                    ))}
                                  </div>
                                )}
                              </div>
                            </div>
                          </td>
                          <td className="overflow-hidden px-4 py-4 align-top">
                            <div className="flex min-w-0 items-center gap-2">
                              <span className="truncate font-mono text-[12px] text-[var(--text-muted)] blur-[3px] transition-all hover:blur-none" title={acc.password}>
                                {acc.password || '-'}
                              </span>
                              {acc.password && (
                                <button
                                  onClick={e => { e.stopPropagation(); copy(acc.password) }}
                                  className="text-[var(--text-muted)] opacity-0 transition-opacity hover:text-[var(--accent)] group-hover:opacity-100"
                                  aria-label="copy"
                                >
                                  <Copy className="h-3.5 w-3.5" />
                                </button>
                              )}
                            </div>
                          </td>
                          <td className="overflow-hidden px-4 py-4 align-top">
                            <div className="flex min-w-0 flex-col items-start gap-1.5">
                              <span className={cn("inline-flex items-center rounded-full px-2 py-0.5 text-[11px] font-semibold", statusStyles)}>
                                <span className={cn("mr-1.5 h-1.5 w-1.5 rounded-full", statusDot)} />
                                {translateAccountStatus(status, language)}
                              </span>
                              {primaryMetrics.length > 0 ? (
                                <div className="min-w-0 max-w-full space-y-0.5 text-[11px] leading-4 text-[var(--text-muted)]">
                                  {primaryMetrics.slice(0, 1).map((metric: any) => (
                                    <div key={metric.key || metric.label} className="truncate">
                                      <span className="font-medium text-[var(--text-secondary)]">{metric.label}: </span>
                                      {metric.value}
                                    </div>
                                  ))}
                                </div>
                              ) : (
                                <div className="truncate text-[11px] text-[var(--text-muted)]">
                                  {getCompactStatusMeta(acc)}
                                </div>
                              )}
                            </div>
                          </td>
                          <td className="overflow-hidden px-4 py-4 align-top font-mono text-[11px] leading-5 text-[var(--text-muted)]">
                            {acc.created_at ? formatDateTime(acc.created_at, language, {
                              month: '2-digit',
                              day: '2-digit',
                              hour: '2-digit',
                              minute: '2-digit',
                              hour12: false,
                            }) : '-'}
                          </td>
                          <td className="px-2 py-4 align-top" onClick={e => e.stopPropagation()}>
                            <div className="flex items-center justify-end gap-1.5">
                              {getCashierUrl(acc) && (
                                <>
                                  <button
                                    onClick={e => { e.stopPropagation(); copy(getCashierUrl(acc)) }}
                                    className="flex h-8 w-8 items-center justify-center rounded-lg bg-[var(--bg-pane)] text-[var(--text-secondary)] transition-colors hover:text-[var(--accent)]"
                                    aria-label="copy"
                                  >
                                    <Copy className="h-3.5 w-3.5" />
                                  </button>
                                  <a
                                    href={getCashierUrl(acc)}
                                    target="_blank"
                                    rel="noreferrer"
                                    onClick={e => e.stopPropagation()}
                                    className="flex h-8 w-8 items-center justify-center rounded-lg bg-[var(--bg-pane)] text-[var(--text-secondary)] transition-colors hover:text-[var(--accent)]"
                                    aria-label="open"
                                  >
                                    <ExternalLink className="h-3.5 w-3.5" />
                                  </a>
                                </>
                              )}
                              <ActionMenu
                                acc={acc}
                                onDetail={() => setDetail(acc)}
                                onDelete={() => load()}
                                onResult={(title, payload) => setActionResult({ title, payload })}
                                onChanged={() => load()}
                                onTriggerGetRt={triggerGetRtForAccount}
                              />
                            </div>
                          </td>
                        </tr>
                      )
                    })()
                  ))}
                </tbody>
              </table>
            </div>

            <div className="flex flex-col gap-3 border-t border-[var(--border-soft)] px-6 py-4 text-[11px] text-[var(--text-muted)] sm:flex-row sm:items-center sm:justify-between">
              <span>
                {t('accounts.showingEntries', { from: entryStart, to: entryEnd, total })}
              </span>
              <div className="flex items-center justify-end gap-2">
                <button
                  onClick={() => setPage(current => Math.max(1, current - 1))}
                  disabled={page <= 1}
                  className="flex h-7 w-7 items-center justify-center rounded-md text-[var(--text-secondary)] transition-colors hover:bg-[var(--bg-pane)] disabled:opacity-40"
                >
                  {'<'}
                </button>
                <span className="flex h-7 min-w-7 items-center justify-center rounded-md bg-[var(--gradient-accent)] px-2 text-[11px] font-bold text-white">
                  {page}
                </span>
                <span className="px-1 text-[var(--text-muted)]">/</span>
                <span className="min-w-5 text-center text-[11px] text-[var(--text-secondary)]">
                  {pageCount}
                </span>
                <button
                  onClick={() => setPage(current => Math.min(pageCount, current + 1))}
                  disabled={page >= pageCount}
                  className="flex h-7 w-7 items-center justify-center rounded-md text-[var(--text-secondary)] transition-colors hover:bg-[var(--bg-pane)] disabled:opacity-40"
                >
                  {'>'}
                </button>
              </div>
            </div>
          </div>
        </section>
      </div>

      <Card className="hidden shrink-0 bg-[var(--bg-pane)]/40 border border-[var(--border)] shadow-sm">
        <div className="flex flex-col gap-3 px-5 py-4 border-b border-[var(--border)]/50 lg:flex-row lg:items-start lg:justify-between">
          <div className="flex min-w-0 shrink-0 flex-wrap items-center gap-3">
            <h1 className="shrink-0 text-lg font-semibold tracking-tight text-[var(--text-primary)]">
              {platformLabel}
            </h1>
            <div className="hidden h-4 w-[1px] bg-[var(--border)] sm:block"></div>
            <div className="flex min-w-0 flex-wrap items-center gap-1.5 text-xs">
              <span className="shrink-0 text-[var(--text-muted)]">{t('accounts.count', { count: total })}</span>
              {visibleTrial > 0 && <span className="flex items-center rounded-full bg-emerald-500/10 px-2 py-0.5 font-medium text-emerald-500 ring-1 ring-inset ring-emerald-500/20">{t('accounts.trial', { count: visibleTrial })}</span>}
              {visibleSubscribed > 0 && <span className="flex items-center rounded-full bg-blue-500/10 px-2 py-0.5 font-medium text-blue-500 ring-1 ring-inset ring-blue-500/20">{t('accounts.subscribed', { count: visibleSubscribed })}</span>}
              {linkedCashier > 0 && <span className="flex items-center rounded-full bg-amber-500/10 px-2 py-0.5 font-medium text-amber-500 ring-1 ring-inset ring-amber-500/20">{t('accounts.linked', { count: linkedCashier })}</span>}
              {visibleInvalid > 0 && <span className="flex items-center rounded-full bg-red-500/10 px-2 py-0.5 font-medium text-red-500 ring-1 ring-inset ring-red-500/20">{t('accounts.invalid', { count: visibleInvalid })}</span>}
              {selectedCount > 0 && <span className="flex items-center rounded-full bg-[var(--text-primary)]/10 px-2 py-0.5 font-medium text-[var(--text-primary)] ring-1 ring-inset ring-[var(--text-primary)]/20">{t('accounts.selected', { count: selectedCount })}</span>}
            </div>
          </div>
          <div className="flex w-full flex-wrap items-center gap-2 lg:w-auto lg:justify-end">
            <Button size="sm" onClick={() => setShowRegister(true)} className="h-8 shrink-0 whitespace-nowrap shadow-sm">
              <Plus className="mr-1.5 h-3.5 w-3.5 shrink-0" />
              {t('accounts.autoRegister')}
            </Button>
            <div className="hidden h-4 w-[1px] shrink-0 bg-[var(--border)] sm:block"></div>
            <Button size="sm" variant="outline" onClick={() => setShowImport(true)} className={ACCOUNT_TOOL_BUTTON_CLASS}>
              <Upload className="mr-1.5 h-3.5 w-3.5 shrink-0" />
              {t('accounts.import')}
            </Button>
            {tab === 'chatgpt' ? (
              <ExportMenu
                platform={tab}
                total={total}
                statusFilter={filterStatus}
                searchFilter={debouncedSearch}
                selectedIds={[...selectedIds]}
              />
            ) : (
              <Button size="sm" variant="outline" onClick={exportCsv} disabled={accounts.length === 0} className={ACCOUNT_TOOL_BUTTON_CLASS}>
                <Download className="mr-1.5 h-3.5 w-3.5 shrink-0" />
                {t('accounts.export')}
              </Button>
            )}
            {tab === 'chatgpt' && (
              <Button
                size="sm"
                variant="outline"
                onClick={() => {
                  setError('')
                  if (selectedIds.size === 0) {
                    setError('请选择至少 1 个账户进行 Codex OAuth')
                    return
                  }
                  setOauthConfirmOpen(true)
                }}
                disabled={oauthBusy || selectedCount === 0}
                className={ACCOUNT_TOOL_BUTTON_CLASS}
              >
                {oauthBusy ? (
                  <Loader2 className="mr-1.5 h-3.5 w-3.5 shrink-0 animate-spin" />
                ) : (
                  <ShieldCheck className="mr-1.5 h-3.5 w-3.5 shrink-0" />
                )}
                Codex OAuth
              </Button>
            )}
            {tab === 'chatgpt' && (
              <Button
                size="sm"
                variant="outline"
                onClick={openGetRtConfirm}
                disabled={getRtBusy || selectedCount === 0}
                className={ACCOUNT_TOOL_BUTTON_CLASS}
              >
                {getRtBusy ? (
                  <Loader2 className="mr-1.5 h-3.5 w-3.5 shrink-0 animate-spin" />
                ) : (
                  <Zap className="mr-1.5 h-3.5 w-3.5 shrink-0" />
                )}
                获取rt
              </Button>
            )}
            {tab === 'chatgpt' && (
              <Button
                size="sm"
                variant="outline"
                onClick={() => {
                  setError('')
                  if (selectedIds.size === 0) {
                    setError('请选择至少 1 个账户')
                    return
                  }
                  setGetRtBypassConfirmOpen(true)
                }}
                disabled={getRtBypassBusy || selectedCount === 0}
                className={ACCOUNT_TOOL_BUTTON_CLASS}
              >
                {getRtBypassBusy ? (
                  <Loader2 className="mr-1.5 h-3.5 w-3.5 shrink-0 animate-spin" />
                ) : (
                  <ShieldCheck className="mr-1.5 h-3.5 w-3.5 shrink-0" />
                )}
                获取rt(绕过)
              </Button>
            )}
            {tab === 'chatgpt' && (
              <Button
                size="sm"
                variant="outline"
                onClick={openBatchStatusModal}
                disabled={batchStatusUpdating || selectedCount === 0}
                className={ACCOUNT_TOOL_BUTTON_CLASS}
              >
                {batchStatusUpdating ? (
                  <Loader2 className="mr-1.5 h-3.5 w-3.5 shrink-0 animate-spin" />
                ) : (
                  <ListChecks className="mr-1.5 h-3.5 w-3.5 shrink-0" />
                )}
                {'\u4fee\u6539\u72b6\u6001'}
              </Button>
            )}
            <Button size="sm" variant="outline" onClick={() => setShowAdd(true)} className={ACCOUNT_TOOL_BUTTON_CLASS}>
              <Plus className="mr-1.5 h-3.5 w-3.5 shrink-0" />
              {t('accounts.manualAdd')}
            </Button>
          </div>
        </div>
        
        {/* Search & Filter Toolbar */}
        <div className="flex items-center justify-between gap-4 px-5 py-2.5 bg-[var(--bg-pane)]/20">
          <div className="flex flex-1 items-center gap-3">
            <div className="relative flex-1 max-w-sm">
              <div className="pointer-events-none absolute inset-y-0 left-0 flex items-center pl-2.5 text-[var(--text-muted)]">
                <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><circle cx="11" cy="11" r="8"></circle><path d="m21 21-4.3-4.3"></path></svg>
              </div>
              <input
                type="text"
                placeholder={t('accounts.searchPlaceholder')}
                value={search}
                onChange={e => setSearch(e.target.value)}
                className="w-full rounded-md border border-[var(--border-soft)] bg-transparent py-1.5 pl-8 pr-3 text-sm text-[var(--text-primary)] transition-colors placeholder:text-[var(--text-muted)] focus:border-[var(--text-primary)] focus:outline-none focus:ring-1 focus:ring-[var(--text-primary)]"
              />
            </div>
            <select
              value={filterStatus}
              onChange={e => setFilterStatus(e.target.value)}
              className="rounded-md border border-[var(--border-soft)] bg-transparent py-1.5 pl-3 pr-8 text-sm text-[var(--text-primary)] transition-colors focus:border-[var(--text-primary)] focus:outline-none focus:ring-1 focus:ring-[var(--text-primary)] appearance-none"
              style={{ backgroundImage: `url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='14' height='14' viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='m6 9 6 6 6-6'/%3E%3C/svg%3E")`, backgroundPosition: 'right 8px center', backgroundRepeat: 'no-repeat' }}
            >
              <option value="">{t('accounts.allStatuses')}</option>
              {ACCOUNT_STATUS_FILTER_OPTIONS.map(status => (
                <option key={status} value={status}>
                  {status === 'eligible' ? t('accounts.eligible') : translateAccountStatus(status, language)}
                </option>
              ))}
            </select>
          </div>
          
          <div className="flex items-center gap-2">
            {tab === 'chatgpt' && (
              <Button
                variant="ghost"
                size="sm"
                disabled={batchHealthChecking || batchRefreshing || loading}
                className="h-7 px-2.5 text-[var(--text-muted)] hover:bg-[var(--bg-hover)] hover:text-[var(--text-primary)]"
                title={t('accounts.healthCheckTitle')}
                onClick={startHealthCheck}
              >
                <ShieldCheck className={`mr-1 h-3.5 w-3.5 ${batchHealthChecking ? 'animate-pulse' : ''}`} />
                {batchHealthChecking ? t('accounts.healthChecking') : t('accounts.healthCheck')}
              </Button>
            )}
            <Button
              variant="ghost"
              size="sm"
              disabled={batchRefreshing || batchHealthChecking || loading}
              className="h-7 px-2.5 text-[var(--text-muted)] hover:text-amber-500 hover:bg-amber-500/10"
              title={t('accounts.refreshCreditsTitle')}
              onClick={async () => {
                setBatchRefreshing(true)
                try {
                  const res = await apiFetch(`/accounts/check-all?platform=${tab}`, { method: 'POST' })
                  if (res?.task_id) {
                    setBatchTask({ taskId: res.task_id, title: t('accounts.refreshAllCreditsTask', { platform: platformLabel }) })
                    setBatchTaskStatus(null)
                  }
                } catch (e) {
                  console.error(e)
                  setBatchRefreshing(false)
                }
              }}
            >
              <Zap className={`mr-1 h-3.5 w-3.5 ${batchRefreshing ? 'animate-pulse' : ''}`} />
              {batchRefreshing ? t('accounts.refreshingCredits') : t('accounts.refreshCredits')}
            </Button>
            <Button variant="ghost" size="sm" onClick={() => load()} disabled={loading} className="h-7 w-7 p-0 text-[var(--text-muted)] hover:text-[var(--text-primary)]">
              <RefreshCw className={`h-3.5 w-3.5 ${loading ? 'animate-spin' : ''}`} />
            </Button>
            {tab === 'chatgpt' && selectedCount > 0 && (
              <Button
                size="sm"
                variant="ghost"
                disabled={batchStatusUpdating}
                className="h-7 px-2.5 text-[var(--text-muted)] hover:bg-[var(--bg-hover)] hover:text-[var(--text-primary)]"
                onClick={openBatchStatusModal}
              >
                {batchStatusUpdating ? (
                  <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />
                ) : (
                  <ListChecks className="mr-1.5 h-3.5 w-3.5" />
                )}
                {'\u4fee\u6539\u72b6\u6001'}
              </Button>
            )}
            {selectedCount > 0 && (
              <Button
                size="sm"
                variant="ghost"
                disabled={bulkDeleting}
                className="h-7 px-2.5 text-red-500 hover:bg-red-500/10 hover:text-red-600"
                onClick={async () => {
                  if (!confirm(t('accounts.deleteSelectedConfirm', { count: selectedCount }))) return
                  setBulkDeleting(true)
                  try {
                    await Promise.allSettled(
                      [...selectedIds].map(id => apiFetch(`/accounts/${id}`, { method: 'DELETE' }))
                    )
                    setSelectedIds(new Set())
                    load()
                  } finally {
                    setBulkDeleting(false)
                  }
                }}
              >
                <Trash2 className="mr-1.5 h-3.5 w-3.5" />
                {bulkDeleting ? t('common.deleting') : t('common.delete')}
              </Button>
            )}
          </div>
        </div>
      </Card>

      <Card className="hidden min-h-0 flex-1 overflow-hidden p-0 border border-[var(--border)] shadow-sm">
        <div className="flex h-full min-h-0 flex-col">
          <div className="glass-table-wrap min-h-0 flex-1 overflow-auto">
        <table className="table-fixed w-full min-w-[900px] text-sm">
          <colgroup>
            <col className="w-10" />
            <col className="w-[30%]" />
            <col className="w-[12%]" />
            <col className="w-[26%]" />
            <col className="w-[8%]" />
            <col className="w-[12%]" />
            <col className="w-[12%]" />
          </colgroup>
          <thead className="sticky top-0 z-10  bg-[var(--bg-pane)]/80">
            <tr className="border-b border-[var(--border)] text-xs uppercase tracking-wider font-medium text-[var(--text-muted)]">
              <th className="w-10 px-3 py-2 text-left">
                <input
                  type="checkbox"
                  checked={allSelectedOnPage}
                  onChange={togglePage}
                  className="checkbox-accent rounded-[3px] border-[var(--border)] focus:ring-[var(--text-primary)] focus:ring-offset-0 bg-transparent text-[var(--text-primary)]"
                />
              </th>
              <th className="px-3 py-2 text-left">{t('common.email')}</th>
              <th className="px-3 py-2 text-left">{t('common.password')}</th>
              <th className="px-3 py-2 text-left">{t('common.status')}</th>
              <th className="px-3 py-2 text-left">{t('accounts.link')}</th>
              <th className="px-3 py-2 text-left">{t('accounts.registeredAt')}</th>
              <th className="px-3 py-2 text-right">{t('common.actions')}</th>
            </tr>
          </thead>
          <tbody>
            {accounts.length === 0 && (
              <tr>
                <td colSpan={7} className="px-4 py-24 text-center">
                  <div className="flex flex-col items-center justify-center space-y-3">
                    <div className="flex h-12 w-12 items-center justify-center rounded-full bg-[var(--bg-pane)] border border-[var(--border)] shadow-sm">
                      <svg className="h-6 w-6 text-[var(--text-muted)]" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M20 13V6a2 2 0 00-2-2H6a2 2 0 00-2 2v7m16 0v5a2 2 0 01-2 2H6a2 2 0 01-2-2v-5m16 0h-2.586a1 1 0 00-.707.293l-2.414 2.414a1 1 0 01-.707.293h-3.172a1 1 0 01-.707-.293l-2.414-2.414A1 1 0 006.586 13H4" /></svg>
                    </div>
                    <h3 className="text-sm font-medium text-[var(--text-primary)]">{t('accounts.emptyTitle')}</h3>
                    <p className="text-xs text-[var(--text-muted)] max-w-sm">{t('accounts.emptyDesc')}</p>
                  </div>
                </td>
              </tr>
            )}
            {accounts.map(acc => (
              (() => {
                const overview = getAccountOverview(acc)
                const verificationMailbox = getVerificationMailbox(acc)
                const primaryMetrics = getPrimaryMetrics(acc)
                const displayBadges = getDisplayBadges(acc)
                return (
              <tr key={acc.id} className="group border-b border-[var(--border)]/30 hover:bg-[var(--text-primary)]/[0.02] transition-colors cursor-pointer"
                  onClick={() => setDetail(acc)}>
                <td className="px-3 py-2.5" onClick={e => e.stopPropagation()}>
                  <input
                    type="checkbox"
                    checked={selectedIds.has(acc.id)}
                    onChange={() => toggleOne(acc.id)}
                    className="checkbox-accent rounded-[3px] border-[var(--border)] focus:ring-[var(--text-primary)] focus:ring-offset-0 bg-transparent text-[var(--text-primary)] transition-all opacity-40 group-hover:opacity-100 data-[state=checked]:opacity-100"
                  />
                </td>
                <td className="px-3 py-2.5 font-mono text-sm text-[var(--text-primary)] align-top">
                  <div className="flex min-w-0 items-center gap-1.5">
                    <span className="truncate tracking-tight" title={acc.email}>{acc.email}</span>
                    <button onClick={e => { e.stopPropagation(); copy(emailApiLine(acc.email)) }} title="复制 Email+邮件API" className="text-[var(--text-muted)] hover:text-[var(--text-primary)] opacity-0 group-hover:opacity-100 transition-opacity"><Copy className="h-3 w-3" /></button>
                  </div>
                  {verificationMailbox && (verificationMailbox.email || verificationMailbox.account_id || verificationMailbox.provider) && (
                    <div
                      className="mt-1 truncate text-xs text-[var(--text-muted)] flex items-center gap-1"
                      title={`验证邮箱: ${verificationMailbox.email || '-'} · ${verificationMailbox.provider || '-'}`}
                    >
                      <svg className="w-3 h-3 opacity-60 flex-shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M4 4h16c1.1 0 2 .9 2 2v12c0 1.1-.9 2-2 2H4c-1.1 0-2-.9-2-2V6c0-1.1.9-2 2-2z" /><polyline points="22,6 12,13 2,6" /></svg>
                      <span className="truncate">{verificationMailbox.email || '-'}</span>
                    </div>
                  )}
                  {overview?.remote_email && overview.remote_email !== acc.email && (
                    <div className="mt-1 truncate text-xs text-[var(--text-muted)]" title={`远端邮箱: ${overview.remote_email}`}>
                      远端邮箱: {overview.remote_email}
                    </div>
                  )}
                  {displayBadges.length > 0 && (
                    <div className="mt-1.5 flex flex-wrap gap-1">
                      {displayBadges.slice(0, 3).map((badge: any, index: number) => (
                        <span key={`${badge?.label || 'badge'}-${index}`} className={getAccountBadgeClassName(badge, 'legacy')}>
                          {badge?.label}
                        </span>
                      ))}
                    </div>
                  )}
                </td>
                <td className="px-3 py-2.5 font-mono text-[13px] text-[var(--text-muted)] align-top">
                  <div className="flex min-w-0 items-center gap-1.5">
                    <span className="truncate blur-[3px] transition-all cursor-default hover:blur-none select-none hover:select-auto hover:text-[var(--text-primary)]" title={acc.password}>{acc.password}</span>
                    <button onClick={e => { e.stopPropagation(); copy(acc.password) }} className="text-[var(--text-muted)] hover:text-[var(--text-primary)] opacity-0 group-hover:opacity-100 transition-opacity"><Copy className="h-3 w-3" /></button>
                  </div>
                </td>
                <td className="px-3 py-2.5 align-top">
                  <div className="min-w-0 flex flex-col items-start gap-1.5">
                    {(() => {
                      const status = getDisplayStatus(acc);
                      const variant = String(STATUS_VARIANT[status] || 'secondary');
                      const styles = (({
                        success: "bg-emerald-500/10 text-emerald-500 ring-emerald-500/20",
                        warning: "bg-amber-500/10 text-amber-500 ring-amber-500/20",
                        danger: "bg-red-500/10 text-red-500 ring-red-500/20",
                        secondary: "bg-[var(--text-primary)]/5 text-[var(--text-secondary)] ring-[var(--border)]",
                        default: "bg-blue-500/10 text-blue-500 ring-blue-500/20"
                      } as Record<string, string>)[variant]) || "bg-[var(--text-primary)]/5 text-[var(--text-secondary)] ring-[var(--border)]";
                      
                      return (
                        <span className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ring-1 ring-inset ${styles}`}>
                          <span className={`mr-1 h-1 w-1 rounded-full ${variant === 'success' ? 'bg-emerald-500 shadow-[0_0_4px_rgba(16,185,129,0.6)]' : variant === 'warning' ? 'bg-amber-500 shadow-[0_0_4px_rgba(245,158,11,0.6)]' : variant === 'danger' ? 'bg-red-500 shadow-[0_0_4px_rgba(239,68,68,0.6)]' : variant === 'default' ? 'bg-blue-500' : 'bg-gray-400'}`}></span>
                          {translateAccountStatus(status, language)}
                        </span>
                      );
                    })()}
                    {primaryMetrics.length > 0 ? (
                      <div className="flex max-w-full flex-col gap-1">
                        {primaryMetrics.slice(0, 2).map((metric: any) => (
                          <div key={metric.key || metric.label} className="flex items-center gap-1.5">
                            <span className="h-1 w-1 rounded-full bg-[var(--text-muted)] opacity-50"></span>
                            <span className="text-xs tracking-tight text-[var(--text-muted)] whitespace-nowrap">
                              <span className="font-medium text-[var(--text-secondary)] mr-0.5">{metric.label}:</span>
                              {metric.value}
                            </span>
                          </div>
                        ))}
                      </div>
                    ) : (
                      <div
                        className="truncate text-xs text-[var(--text-muted)]"
                        title={getCompactStatusMeta(acc)}
                      >
                        {getCompactStatusMeta(acc)}
                      </div>
                    )}
                  </div>
                </td>
                <td className="px-3 py-2.5 align-top">
                  {getCashierUrl(acc) ? (
                    <div className="flex items-center gap-1.5 whitespace-nowrap opacity-70 group-hover:opacity-100 transition-opacity">
                      <button onClick={e => { e.stopPropagation(); copy(getCashierUrl(acc)) }} className="text-[var(--text-muted)] hover:text-[var(--text-primary)] transition-colors p-0.5 rounded hover:bg-[var(--bg-pane)]" title="复制链接"><Copy className="h-3 w-3" /></button>
                      <a href={getCashierUrl(acc)} target="_blank" rel="noreferrer" onClick={e => e.stopPropagation()} className="text-[var(--text-muted)] hover:text-[var(--text-primary)] transition-colors p-0.5 rounded hover:bg-[var(--bg-pane)]" title="打开收银台"><ExternalLink className="h-3 w-3" /></a>
                    </div>
                  ) : <span className="text-[var(--text-muted)]/50 text-xs">-</span>}
                </td>
                <td className="px-3 py-2.5 font-mono text-xs text-[var(--text-muted)] whitespace-nowrap align-top">
                  {acc.created_at ? formatDateTime(acc.created_at, language, { 
                    month: '2-digit', day: '2-digit',
                    hour: '2-digit', minute: '2-digit',
                    hour12: false 
                  }) : '-'}
                </td>
                <td className="px-3 py-2.5 align-top" onClick={e => e.stopPropagation()}>
                  <div className="flex items-center justify-end opacity-60 group-hover:opacity-100 transition-opacity">
                    <ActionMenu
                      acc={acc}
                      onDetail={() => setDetail(acc)}
                      onDelete={() => load()}
                      onResult={(title, payload) => setActionResult({ title, payload })}
                      onChanged={() => load()}
                      onTriggerGetRt={triggerGetRtForAccount}
                    />
                  </div>
                </td>
              </tr>
                )
              })()
            ))}
          </tbody>
        </table>
          </div>
        </div>
      </Card>
    </div>
  )
}
