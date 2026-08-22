import { useEffect, useState, useRef, useCallback } from 'react'
import { createPortal } from 'react-dom'
import { useParams } from 'react-router-dom'
import { getConfig, getConfigOptions, getPlatforms } from '@/lib/app-data'
import type { ConfigOptionsResponse } from '@/lib/config-options'
import { getCaptchaStrategyLabel } from '@/lib/config-options'
import { apiDownload, apiFetch, triggerBrowserDownload, cn, API_BASE } from '@/lib/utils'
import { formatDateTime, translateAccountStatus } from '@/lib/i18n'
import { useI18n } from '@/lib/i18n-context'
import { buildExecutorOptions, buildRegistrationOptions, hasReusableOAuthBrowser, pickOAuthExecutor } from '@/lib/registration'
import { TaskLogPanel } from '@/components/tasks/TaskLogPanel'
import { PpBaTokenDialog, PpPlusFloatingWidget, PpPlusSettingsDialog, PpTaskCell, PpTaskLogDialog, fetchPpPlusStatus, getAccountBaToken, getAccountPpTask, type PpAccountTask, type PpPlusStatus } from '@/components/pp-plus/PpPlusPanels'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { getTaskStatusText, TASK_STATUS_VARIANTS, isTerminalTaskStatus } from '@/lib/tasks'
import { RefreshCw, Copy, ExternalLink, Download, Upload, Plus, X, Mail, Trash2, Zap, Loader2, ShieldCheck, Search, ListChecks, Eye, EyeOff, Globe2, Smartphone } from 'lucide-react'

const STATUS_VARIANT: Record<string, any> = {
  registered: 'default', authorized: 'success', rt_pending_upload: 'warning', rt_uploaded: 'success', agent_identity_uploaded: 'success', trial: 'success', subscribed: 'success',
  expired: 'warning', relogin_required: 'warning', invalid: 'danger', banned: 'danger',
  free: 'secondary', eligible: 'secondary', valid: 'success', unknown: 'secondary',
}

const platformActionsCache = new Map<string, any[]>()
const platformActionsPromiseCache = new Map<string, Promise<any[]>>()
const CHATGPT_K12_WORKSPACE_IDS_STORAGE_KEY = 'accounts.chatgpt.k12WorkspaceIds'
const GOPAY_REGISTER_FORM_STORAGE_KEY = 'accounts.gopay.registerForm'

const GOPAY_SMS_PROVIDER_OPTIONS = [
  { value: 'herosms', label: 'HeroSMS', country: '6', service: 'ni', apiKeyField: 'herosms_api_key' },
  { value: 'smsbower', label: 'SMSBower', country: '6', service: 'ni', apiKeyField: 'smsbower_api_key' },
  { value: 'smspool', label: 'SMSPool', country: '9', service: '392', apiKeyField: 'smspool_api_key' },
  { value: 'smsapi', label: 'SmsApi（自有固定号）', country: '', service: 'gopay', apiKeyField: '' },
] as const

type GopaySmsProvider = typeof GOPAY_SMS_PROVIDER_OPTIONS[number]['value']

const BROWSER_MODE_OPTIONS = [
  { value: 'camoufox_headed', label: 'Camoufox Headed' },
  { value: 'camoufox_headless', label: 'Camoufox Headless' },
  { value: 'bitbrowser_headed', label: 'BitBrowser Headed' },
  { value: 'bitbrowser_hidden', label: 'BitBrowser Hidden' },
  { value: 'bitbrowser_headless', label: 'BitBrowser Headless' },
]

type GetRtSmsBalanceAction = 'auto_switch' | 'wait_release' | 'terminate'
type RegisterCountMode = 'child' | 'parent'
type ChatGPTProtocolVariant = 'web' | 'android'
type TotpDialogState = {
  accountId: number
  email: string
  code: string
  remain: number
  copied: boolean
  period: number
  generatedAt: number
  serverOffsetMs: number
  windowIndex: number
}

type GmailApiCodeAliasUsage = {
  alias_limit?: number
  summary?: {
    configured_parent_count?: number
    usable_parent_count?: number
    confirmed_remaining?: number
    conservative_remaining?: number
  }
}

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
const EMAIL_ALIAS_HARD_LIMIT = 6
const ACCOUNT_STATUS_FILTER_OPTIONS = [
  'registered',
  'rt_pending_upload',
  'rt_uploaded',
  'agent_identity_uploaded',
  'subscribed',
  'eligible',
  'expired',
  'relogin_required',
  'invalid',
  'banned',
]
const ACCOUNT_TAG_FILTER_OPTIONS = ['试用', 'MOMO试用', '2FA已绑', 'WEB协议', '安卓协议', '无头浏览器', '有头浏览器', 'BUGFREE', 'FREE', 'K12', 'PLUS']
const CHATGPT_BATCH_STATUS_OPTIONS = [
  'registered',
  'rt_pending_upload',
  'rt_uploaded',
  'agent_identity_uploaded',
  'trial',
  'subscribed',
  'expired',
  'relogin_required',
  'invalid',
  'banned',
]

type PlanRefreshLogEntry = {
  id: string
  accountId?: number
  email: string
  status: 'pending' | 'running' | 'success' | 'error'
  message: string
  planName?: string
  planState?: string
  subscriptionStatus?: string
  usagePlanType?: string
  raw?: any
}

type PlanRefreshDialogState = {
  open: boolean
  running: boolean
  total: number
  success: number
  failed: number
  currentEmail: string
  logs: PlanRefreshLogEntry[]
}

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

function isRtPendingUploadAccount(acc: any) {
  const status = String(getDisplayStatus(acc) || getLifecycleStatus(acc) || '').trim().toLowerCase()
  return status === 'rt_pending_upload'
}

function isGetRtTargetModeAccount(acc: any) {
  return !hasChatgptRefreshToken(acc) || isRtPendingUploadAccount(acc)
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

function getValidityStatusLabel(acc: any) {
  const status = String(getValidityStatus(acc) || '').trim().toLowerCase()
  if (status === 'valid') return '有效'
  if (status === 'invalid') return '失效'
  if (status === 'relogin_required') return '需重登'
  if (status === 'unknown') return '未检测'
  return status || '未检测'
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

function getRegistrationModeBadge(acc: any) {
  const overview = getAccountOverview(acc)
  const legacyExtra = overview?.legacy_extra && typeof overview.legacy_extra === 'object' ? overview.legacy_extra : {}
  const variant = String(
    overview?.registration_protocol_variant || legacyExtra?.registration_protocol_variant || '',
  ).trim().toLowerCase()
  if (variant === 'android' || variant === 'android_app' || variant === 'android_protocol') {
    return { label: '安卓协议', tone: 'muted' }
  }
  if (variant === 'web' || variant === 'web_protocol') {
    return { label: 'WEB协议', tone: 'muted' }
  }
  const label = String(overview?.registration_mode_label || legacyExtra?.registration_mode_label || '').trim()
  if (label) return { label: label === '协议模式' ? 'WEB协议' : label, tone: 'muted' }
  const mode = String(overview?.registration_mode || legacyExtra?.registration_mode || '').trim().toLowerCase()
  if (mode === 'headless_browser') return { label: '无头浏览器', tone: 'muted' }
  if (mode === 'headed_browser') return { label: '有头浏览器', tone: 'muted' }
  if (mode === 'protocol') return { label: 'WEB协议', tone: 'muted' }
  const executor = String(overview?.registration_executor_type || legacyExtra?.registration_executor_type || '').trim().toLowerCase()
  if (executor === 'headless') return { label: '无头浏览器', tone: 'muted' }
  if (executor === 'headed') return { label: '有头浏览器', tone: 'muted' }
  if (executor === 'protocol') return { label: 'WEB协议', tone: 'muted' }
  return null
}

const COUNTRY_LABEL_FALLBACK: Record<string, string> = {
  JP: '日本',
  US: '美国',
  SG: '新加坡',
  HK: '香港',
  TW: '台湾',
  KR: '韩国',
  TH: '泰国',
  VN: '越南',
  PH: '菲律宾',
  ID: '印度尼西亚',
  MY: '马来西亚',
  GB: '英国',
  CA: '加拿大',
  AU: '澳大利亚',
  TR: '土耳其',
  BR: '巴西',
  MX: '墨西哥',
  IN: '印度',
}

function normalizeCountryCode(value: any) {
  const text = String(value || '').trim().toUpperCase()
  return /^[A-Z]{2}$/.test(text) ? text : ''
}

function formatCountryLabel(value: any) {
  const raw = String(value || '').trim()
  if (!raw) return ''
  const code = normalizeCountryCode(raw)
  if (!code) return raw
  try {
    const DisplayNamesCtor = (Intl as any)?.DisplayNames
    if (DisplayNamesCtor) {
      const name = new DisplayNamesCtor(['zh-CN'], { type: 'region' }).of(code)
      if (name && name !== code) return String(name)
    }
  } catch {
    // Fallback to local map below.
  }
  return COUNTRY_LABEL_FALLBACK[code] || code
}

function getRegistrationIpRegionBadge(acc: any) {
  if (String(acc?.platform || '').trim().toLowerCase() !== 'chatgpt') return null
  const overview = getAccountOverview(acc)
  const legacyExtra = overview?.legacy_extra && typeof overview.legacy_extra === 'object' ? overview.legacy_extra : {}
  const label = formatCountryLabel(
    overview?.registration_ip_country_label ||
    legacyExtra?.registration_ip_country_label ||
    overview?.registration_ip_country_code ||
    legacyExtra?.registration_ip_country_code ||
    overview?.registration_ip_region ||
    legacyExtra?.registration_ip_region ||
    '',
  )
  if (!label) return null
  return { label, tone: 'success' }
}

function isSameRegistrationIpRegionLabel(value: any, regionLabel: string) {
  const label = String(value || '').trim()
  return Boolean(label) && (label === regionLabel || formatCountryLabel(label) === regionLabel)
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
  const registrationModeBadge = getRegistrationModeBadge(acc)
  const registrationIpRegionBadge = getRegistrationIpRegionBadge(acc)
  const withRegistrationRegion = registrationIpRegionBadge
    ? [
      registrationIpRegionBadge,
      ...badges.filter((badge: any) => !isSameRegistrationIpRegionLabel(badge?.label, registrationIpRegionBadge.label)),
    ]
    : badges
  const withoutLegacyProtocolLabel = String(acc?.platform || '').trim().toLowerCase() === 'chatgpt'
    ? withRegistrationRegion.filter((badge: any) => !['协议模式', 'WEB协议', '安卓协议'].includes(String(badge?.label || '').trim()))
    : withRegistrationRegion
  const withRegistrationMode = registrationModeBadge && !withoutLegacyProtocolLabel.some((badge: any) => String(badge?.label || '').trim() === registrationModeBadge.label)
    ? [...withoutLegacyProtocolLabel, registrationModeBadge]
    : withoutLegacyProtocolLabel
  const normalizedBadges = String(acc?.platform || '').trim().toLowerCase() === 'chatgpt'
    && getChatgptTotpSecret(acc)
    && !withRegistrationMode.some((badge: any) => String(badge?.label || '').trim() === '2FA已绑')
    ? [...withRegistrationMode, { label: '2FA已绑', tone: 'success' }]
    : withRegistrationMode
  if (!isChatgptK12Account(acc)) return normalizedBadges
  let hasK12Badge = false
  let replacedFree = false
  const next = normalizedBadges.map((badge: any) => {
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
  const label = String(badge?.label || '').trim().toLowerCase()
  const isK12 = label === 'k12'
  const successClass = 'border-emerald-500/30 bg-emerald-500/15 text-emerald-700 dark:text-emerald-300'
  const bugfreeClass = 'border-red-600 bg-red-600 text-white shadow-[0_0_0_1px_rgba(220,38,38,0.35),0_8px_18px_rgba(220,38,38,0.18)] dark:border-red-400 dark:bg-red-500 dark:text-white'
  if (label === 'bugfree') {
    return cn(
      mode === 'detail'
        ? 'rounded-full border px-2 py-0.5 text-[11px] font-bold tracking-wide'
        : mode === 'modern'
          ? 'rounded border px-1.5 py-0.5 text-[10px] font-bold tracking-wide'
          : 'rounded border px-1 py-0.5 text-[11px] font-bold tracking-wide',
      bugfreeClass,
    )
  }
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

function getChatgptTotpSecret(acc: any) {
  const credential = getCredentials(acc).find((item: any) => item?.scope === 'platform' && item?.key === 'totp_secret' && item?.value)
  if (credential?.value) return String(credential.value)
  const overview = getAccountOverview(acc)
  const legacyExtra = overview?.legacy_extra && typeof overview.legacy_extra === 'object' ? overview.legacy_extra : {}
  return String(overview?.totp_secret || legacyExtra?.totp_secret || '').trim()
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

function getCredentialValue(acc: any, keys: string[]) {
  const wanted = new Set(keys.map(key => key.toLowerCase()))
  const credential = getCredentials(acc).find((item: any) =>
    item?.scope === 'platform' && wanted.has(String(item?.key || '').trim().toLowerCase()) && item?.value,
  )
  return credential?.value ? String(credential.value).trim() : ''
}

function hasChatgptRefreshToken(acc: any) {
  const credentialToken = getCredentialValue(acc, ['refresh_token', 'refreshToken'])
  if (credentialToken) return true
  const overview = getAccountOverview(acc)
  const legacyExtra = overview?.legacy_extra && typeof overview.legacy_extra === 'object' ? overview.legacy_extra : {}
  return Boolean(String(overview?.refresh_token || overview?.refreshToken || legacyExtra?.refresh_token || legacyExtra?.refreshToken || '').trim())
}

function normalizeCookieHeaderForCopy(value: any) {
  const text = String(value || '').trim().replace(/^cookie\s*:\s*/i, '')
  if (!text) return ''
  if (text.startsWith('{') && text.endsWith('}')) {
    try {
      const parsed = JSON.parse(text)
      if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
        return Object.entries(parsed)
          .map(([name, value]) => `${String(name).trim()}=${String(value ?? '')}`)
          .filter(part => part && !part.endsWith('='))
          .join(';')
      }
    } catch {
      // Fall back to raw cookie text below.
    }
  }
  return text.split(';').map(part => part.trim()).filter(Boolean).join(';')
}

function cookieHeaderScore(value: string) {
  const text = normalizeCookieHeaderForCopy(value)
  if (!text) return 0
  return text.split(';').filter(Boolean).length * 100000 + text.length
}

function getChatgptAccessToken(acc: any) {
  const credentialToken = getCredentialValue(acc, ['access_token', 'accessToken'])
  if (credentialToken) return credentialToken
  const session = getChatgptSessionPayload(acc)
  if (session && typeof session === 'object') {
    return String(session.accessToken || session.access_token || '').trim()
  }
  return String(acc?.primary_token || '').trim()
}

function getChatgptLoginStateCookie(acc: any) {
  const overview = getAccountOverview(acc)
  const legacyExtra = overview?.legacy_extra && typeof overview.legacy_extra === 'object' ? overview.legacy_extra : {}
  const session = getChatgptSessionPayload(acc)
  const wanted = new Set(['cookies', 'login_state_cookie', 'cookie_header', 'cookie', 'session_cookie'])
  const candidates = [
    ...getCredentials(acc)
      .filter((item: any) => item?.scope === 'platform' && wanted.has(String(item?.key || '').trim().toLowerCase()) && item?.value)
      .map((item: any) => String(item.value || '').trim()),
    String(overview?.cookies || '').trim(),
    String(overview?.login_state_cookie || '').trim(),
    String(overview?.cookie_header || '').trim(),
    String(overview?.cookie || '').trim(),
    String(legacyExtra?.cookies || '').trim(),
    String(legacyExtra?.login_state_cookie || '').trim(),
    String(legacyExtra?.cookie_header || '').trim(),
    String(legacyExtra?.cookie || '').trim(),
  ].filter(Boolean)
  const cookie = candidates.sort((left, right) => cookieHeaderScore(right) - cookieHeaderScore(left))[0] || ''
  if (cookie) return normalizeCookieHeaderForCopy(cookie)
  const sessionToken = (
    getCredentialValue(acc, ['session_token', 'sessionToken'])
    || (session && typeof session === 'object' ? String(session.sessionToken || session.session_token || '').trim() : '')
  )
  return sessionToken ? `__Secure-next-auth.session-token=${sessionToken}` : ''
}

function buildChatgptAtCookieCopyText(acc: any) {
  const accessToken = getChatgptAccessToken(acc)
  const cookie = getChatgptLoginStateCookie(acc)
  return accessToken && cookie ? `${accessToken} | ${cookie} \n` : ''
}

function getAccountPlanLabel(acc: any) {
  const overview = getAccountOverview(acc)
  const plan = String(acc?.plan_name || overview?.plan_name || overview?.plan || '').trim()
  if (plan) return plan
  const planState = String(getPlanState(acc) || '').trim().toLowerCase()
  if (planState === 'free' || planState === 'eligible' || planState === 'unknown') return 'Free'
  if (planState === 'trial') return 'Trial'
  if (planState === 'subscribed') return 'Plus'
  return planState || '-'
}

function compactBaExtractStepDesc(desc: string) {
  const text = String(desc || '').trim()
  if (!text) return '-'
  const withoutBody = text.replace(/\s+body=.*$/s, '').trim()
  return withoutBody.length > 90 ? `${withoutBody.slice(0, 90)}...` : withoutBody
}

function getAccountPlanPillClassName(acc: any, planLabel: string) {
  const normalizedLabel = String(planLabel || '').trim().toLowerCase()
  const normalizedState = String(getPlanState(acc) || '').trim().toLowerCase()
  if (normalizedLabel === 'plus' || normalizedState === 'subscribed') {
    return 'border-[#d6a83d]/70 bg-[#11100d] text-[#f8d675] shadow-[0_0_0_1px_rgba(214,168,61,0.32),0_8px_18px_rgba(17,16,13,0.22)] dark:border-[#f2c75b]/70 dark:bg-black dark:text-[#ffe08a]'
  }
  return 'border-emerald-500/20 bg-emerald-500/10 text-emerald-700 shadow-sm dark:text-emerald-300'
}

function getAccessTokenCopyCount(acc: any) {
  const raw = getAccountOverview(acc)?.access_token_copy_count
  const count = typeof raw === 'number' ? raw : Number(raw || 0)
  return Number.isFinite(count) && count > 0 ? Math.floor(count) : 0
}

function resolveTimeMs(value: any) {
  if (value === null || value === undefined || value === '') return 0
  if (typeof value === 'number') return value > 1000000000000 ? value : value * 1000
  const text = String(value || '').trim()
  if (!text) return 0
  if (/^\d+$/.test(text)) {
    const numberValue = Number(text)
    return numberValue > 1000000000000 ? numberValue : numberValue * 1000
  }
  const parsed = Date.parse(text)
  return Number.isFinite(parsed) ? parsed : 0
}

function getAccountExpiresAtMs(acc: any) {
  const overview = getAccountOverview(acc)
  const legacyExtra = overview?.legacy_extra && typeof overview.legacy_extra === 'object' ? overview.legacy_extra : {}
  const expiresCredential = getCredentials(acc).find((item: any) =>
    String(item?.key || '').trim().toLowerCase() === 'expires_at' && item?.value,
  )
  const candidates = [
    acc?.trial_end_time,
    overview?.trial_end_time,
    overview?.trial_ends_at,
    overview?.expires_at,
    overview?.expiresAt,
    acc?.expires_at,
    overview?.session?.expires,
    overview?.chatgpt_session?.expires,
    legacyExtra?.expires_at,
    legacyExtra?.expiresAt,
    legacyExtra?.session?.expires,
    legacyExtra?.chatgpt_session?.expires,
    expiresCredential?.value,
    overview?.subscription_expires_at,
    overview?.reset_at,
  ]
  for (const candidate of candidates) {
    const ms = resolveTimeMs(candidate)
    if (ms > 0) return ms
  }
  return 0
}

function getAccountValidityWindowLabel(acc: any) {
  const expiresAt = getAccountExpiresAtMs(acc)
  if (!expiresAt) return '-'
  const remainingMs = expiresAt - Date.now()
  if (remainingMs <= 0) return '已过期'
  const totalHours = Math.max(1, Math.floor(remainingMs / 3600000))
  const days = Math.floor(totalHours / 24)
  const hours = totalHours % 24
  if (days > 0 && hours > 0) return `${days}天${hours}小时`
  if (days > 0) return `${days}天`
  return `${hours}小时`
}

function getAccountCreatedAtLabel(acc: any, language: string) {
  if (!acc?.created_at) return '-'
  return formatDateTime(acc.created_at, language as any, {
    year: 'numeric',
    month: 'numeric',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  })
}

const BA_EXTRACT_SETTINGS_KEY = 'accounts.chatgpt.baExtractSettings'
const BA_EXTRACT_COUNTRY_CURRENCY: Record<string, string> = {
  US: 'USD',
  TR: 'TRY',
  VN: 'VND',
  BR: 'USD',
  JP: 'JPY',
  ID: 'IDR',
  SG: 'SGD',
  HK: 'HKD',
  GB: 'GBP',
  AU: 'AUD',
  CA: 'CAD',
  IN: 'INR',
  MX: 'MXN',
  DE: 'EUR',
  NL: 'EUR',
  IE: 'EUR',
  FR: 'EUR',
  BE: 'EUR',
}

function baExtractCurrencyForCountry(country: string): string {
  const code = String(country || 'US').trim().toUpperCase()
  return BA_EXTRACT_COUNTRY_CURRENCY[code] || 'USD'
}

function loadBaExtractSettings(): Record<string, string> {
  const defaults = {
    billing_proxy: '',
    promo_proxy: '',
    billing_country: 'US',
    promo_country: 'TR',
    billing_currency: 'USD',
    confirm_mode: 'pm',
    promo_create_mode: 'update_after_checkout',
    max_attempts: '20',
  }
  if (typeof window === 'undefined') return defaults
  try {
    const raw = window.localStorage.getItem(BA_EXTRACT_SETTINGS_KEY)
    if (!raw) return defaults
    const parsed = JSON.parse(raw)
    return { ...defaults, ...(parsed && typeof parsed === 'object' ? parsed : {}) }
  } catch {
    return defaults
  }
}

function saveBaExtractSettings(settings: Record<string, string>) {
  if (typeof window === 'undefined') return
  try {
    window.localStorage.setItem(BA_EXTRACT_SETTINGS_KEY, JSON.stringify(settings || {}))
  } catch {
    // ignore
  }
}

type BaExtractTaskState = {
  task_id?: string
  account_id?: number
  email?: string
  status?: string
  stage?: string
  step?: number
  total?: number
  attempt?: number
  max_attempts?: number
  ba_token?: string
  ba_url?: string
  region_combo?: string
  error?: string
  logs?: string[]
  updated_at?: number
}

function getAccountBaExtractTask(acc: any, live?: BaExtractTaskState | null): BaExtractTaskState {
  const overview = getAccountOverview(acc)
  return {
    task_id: String(live?.task_id || overview?.ba_extract_task_id || ''),
    account_id: Number(acc?.id || live?.account_id || 0),
    email: String(acc?.email || live?.email || ''),
    status: String(live?.status || overview?.ba_extract_status || 'idle'),
    stage: String(live?.stage || overview?.ba_extract_stage || ''),
    step: Number(live?.step ?? overview?.ba_extract_step ?? 0),
    total: Number(live?.total ?? overview?.ba_extract_total ?? 7),
    attempt: Number(live?.attempt ?? overview?.ba_extract_attempt ?? 0),
    max_attempts: Number(live?.max_attempts ?? overview?.ba_extract_max_attempts ?? 20),
    ba_token: String(live?.ba_token || overview?.pp_ba_token || overview?.ba_token || ''),
    ba_url: String(live?.ba_url || overview?.ba_extract_ba_url || ''),
    region_combo: String(live?.region_combo || overview?.ba_extract_region_combo || ''),
    error: String(live?.error || overview?.ba_extract_error || ''),
    logs: Array.isArray(live?.logs) ? live?.logs : Array.isArray(overview?.ba_extract_logs) ? overview.ba_extract_logs : [],
    updated_at: Number(live?.updated_at ?? overview?.ba_extract_updated_at ?? 0),
  }
}

function isBaExtractTaskRunning(task: BaExtractTaskState): boolean {
  // cancelling 也算占用中（用于进度展示）；真正禁点「提取」用 isBaExtractTaskActive
  return ['queued', 'started', 'running', 'cancelling'].includes(String(task?.status || '').toLowerCase())
}

function isBaExtractTaskActive(task: BaExtractTaskState): boolean {
  // 仅真正执行中禁止重复点；cancelling/终态允许再次 force 启动
  return ['queued', 'started', 'running'].includes(String(task?.status || '').toLowerCase())
}

function formatBaExtractLogTime(value?: number): string {
  const date = value ? new Date(Number(value) * 1000) : new Date()
  const pad = (num: number) => String(num).padStart(2, '0')
  return `[${date.getFullYear()}年${pad(date.getMonth() + 1)}月${pad(date.getDate())}日 ${pad(date.getHours())}时${pad(date.getMinutes())}分${pad(date.getSeconds())}秒]`
}

function withBaExtractLogTime(line: string, time?: number): string {
  const text = String(line || '').trim()
  if (!text) return ''
  if (/^\[\d{4}年\d{2}月\d{2}日 \d{2}时\d{2}分\d{2}秒\]/.test(text)) return text
  return `${formatBaExtractLogTime(time)} ${text}`
}

function formatBaExtractLogs(logs?: string[], fallbackTime?: number): string {
  return (Array.isArray(logs) ? logs : [])
    .map(line => withBaExtractLogTime(line, fallbackTime))
    .join('\n')
}

function BaExtractTaskLogDialog({
  open,
  task,
  onClose,
  onStop,
  stopping = false,
}: {
  open: boolean
  task: BaExtractTaskState | null
  onClose: () => void
  onStop?: () => void | Promise<void>
  stopping?: boolean
}) {
  if (!open || !task) return null
  const text = formatBaExtractLogs(task.logs, Number(task.updated_at || 0))
  const status = String(task.status || 'idle').toLowerCase()
  const running = isBaExtractTaskRunning(task)
  const cancelling = status === 'cancelling' || stopping
  const canStop = Boolean(onStop) && (running || cancelling)
  const progressLabel = task.step
    ? `步骤 ${Number(task.step || 0)}/${Number(task.total || 7)}`
    : ''
  return createPortal(
    <div className="dialog-backdrop" onClick={onClose}>
      <div
        className="w-[min(760px,94vw)] max-h-[88vh] overflow-auto rounded-2xl border border-[var(--border)] bg-[var(--bg-base)] p-5 shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="mb-3 flex items-center justify-between gap-3">
          <div className="min-w-0">
            <h2 className="text-base font-semibold text-[var(--text-primary)]">BA链提取日志</h2>
            <p className="mt-1 truncate text-xs text-[var(--text-muted)]">
              {task.email || '-'} · {task.status || 'idle'} · {task.stage || '-'}
              {progressLabel ? ` · ${progressLabel}` : ''}
              {cancelling ? ' · 终止中' : running ? ' · 实时更新中' : ''}
            </p>
          </div>
          <div className="flex shrink-0 items-center gap-2">
            {canStop && (
              <Button
                size="sm"
                variant="outline"
                disabled={cancelling}
                onClick={() => { void onStop?.() }}
                className="border-red-500/30 text-red-600 hover:border-red-500/50 hover:bg-red-500/10 dark:text-red-300"
                title="终止当前 BA 链提取任务"
              >
                {cancelling ? (
                  <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />
                ) : null}
                {cancelling ? '终止中' : '终止任务'}
              </Button>
            )}
            <Button
              size="sm"
              variant="outline"
              disabled={!text}
              onClick={() => {
                try { navigator.clipboard.writeText(text || '') } catch {}
              }}
            >
              <Copy className="mr-1.5 h-3.5 w-3.5" /> 复制全部
            </Button>
            <button
              type="button"
              onClick={onClose}
              className="rounded-lg p-1 text-[var(--text-muted)] hover:bg-[var(--bg-pane)]"
              aria-label="关闭"
            >
              <X className="h-4 w-4" />
            </button>
          </div>
        </div>
        {task.error && (
          <div className="mb-3 rounded-lg border border-red-500/20 bg-red-500/10 px-3 py-2 text-sm text-red-300">
            {task.error}
          </div>
        )}
        {task.ba_token && (
          <div className="mb-3 rounded-lg border border-emerald-500/20 bg-emerald-500/10 px-3 py-2 text-sm text-emerald-300">
            BA: {task.ba_token}{task.region_combo ? ` · IP地区 ${task.region_combo}` : ''}
          </div>
        )}
        <pre className="max-h-[60vh] overflow-auto rounded-xl border border-[var(--border-soft)] bg-[var(--bg-pane)]/40 p-3 text-xs leading-6 whitespace-pre-wrap">
          {text || '暂无日志'}
        </pre>
      </div>
    </div>,
    document.body,
  )
}

function SimpleTaskLogDialog({
  open,
  title,
  subtitle,
  taskId,
  onClose,
  onDone,
  showMomoTrialStats = false,
}: {
  open: boolean
  title: string
  subtitle?: string
  taskId: string
  onClose: () => void
  onDone?: (status: string) => void
  showMomoTrialStats?: boolean
}) {
  const [lines, setLines] = useState<string[]>([])
  const [status, setStatus] = useState<string>('running')
  const [error, setError] = useState('')
  const [task, setTask] = useState<any | null>(null)
  const seenRef = useRef<Set<number>>(new Set())
  const cursorRef = useRef(0)
  const doneRef = useRef(false)
  const onDoneRef = useRef(onDone)
  const eventSourceRef = useRef<EventSource | null>(null)

  useEffect(() => {
    onDoneRef.current = onDone
  }, [onDone])

  useEffect(() => {
    if (!open || !taskId) return
    seenRef.current = new Set()
    cursorRef.current = 0
    doneRef.current = false
    setLines([])
    setStatus('running')
    setError('')
    setTask(null)

    const pushLine = (line: string, eventId = 0) => {
      if (eventId && seenRef.current.has(eventId)) return
      if (eventId) {
        seenRef.current.add(eventId)
        cursorRef.current = Math.max(cursorRef.current, eventId)
      }
      if (!line) return
      setLines(prev => {
        const next = [...prev, line]
        if (next.length > 800) next.splice(0, next.length - 800)
        return next
      })
    }

    const markDone = (nextStatus: string) => {
      if (doneRef.current) return
      doneRef.current = true
      eventSourceRef.current?.close()
      eventSourceRef.current = null
      setStatus(String(nextStatus || 'succeeded'))
      try { onDoneRef.current?.(String(nextStatus || 'succeeded')) } catch {}
    }

    const fetchMissing = async () => {
      let guard = 0
      while (guard < 20) {
        guard += 1
        const data = await apiFetch(`/tasks/${taskId}/events?since=${cursorRef.current}&limit=500`)
        const items = data.items || []
        for (const item of items) {
          const eventId = Number(item?.id || 0)
          if (item?.line) pushLine(String(item.line), eventId)
          if (item?.done) markDone(item.status || 'succeeded')
        }
        if (items.length < 500) break
      }
    }

    const syncTask = async () => {
      const latest = await apiFetch(`/tasks/${taskId}`)
      setTask(latest)
      if (latest?.status) setStatus(String(latest.status))
      if (isTerminalTaskStatus(latest?.status) && !doneRef.current) {
        await fetchMissing()
        markDone(latest.status)
      }
    }

    const es = new EventSource(`${API_BASE}/tasks/${taskId}/logs/stream`)
    eventSourceRef.current = es
    es.onmessage = (e) => {
      try {
        const payload = JSON.parse(e.data)
        const eventId = Number(payload?.id || 0)
        if (payload?.line) pushLine(String(payload.line), eventId)
        if (payload?.done) markDone(payload.status || 'succeeded')
      } catch {}
    }
    es.onerror = () => {
      // fallback poll handles recovery
    }

    syncTask().catch((exc: any) => setError(String(exc?.message || '任务日志加载失败')))

    const progressPoll = window.setInterval(() => {
      if (doneRef.current) return
      syncTask().catch(() => {})
    }, 1500)
    const fallbackPoll = window.setInterval(() => {
      if (doneRef.current) return
      fetchMissing().catch(() => {})
    }, 1200)

    return () => {
      es.close()
      if (eventSourceRef.current === es) eventSourceRef.current = null
      window.clearInterval(progressPoll)
      window.clearInterval(fallbackPoll)
    }
  }, [open, taskId])

  if (!open || !taskId) return null
  const text = lines.join('\n')
  const running = !['succeeded', 'failed', 'cancelled', 'canceled', 'success', 'error'].includes(String(status || '').toLowerCase())
  const momoStats = showMomoTrialStats
    ? (task?.data && typeof task.data === 'object' ? task.data : task?.result?.data || {})
    : null
  return createPortal(
    <div className="dialog-backdrop" onClick={onClose}>
      <div
        className="w-[min(760px,94vw)] max-h-[88vh] overflow-auto rounded-2xl border border-[var(--border)] bg-[var(--bg-base)] p-5 shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="mb-3 flex items-center justify-between gap-3">
          <div className="min-w-0">
            <h2 className="text-base font-semibold text-[var(--text-primary)]">{title}</h2>
            <p className="mt-1 truncate text-xs text-[var(--text-muted)]">
              {subtitle || '-'} · {status || 'idle'}
              {running ? ' · 实时更新中' : ''}
            </p>
          </div>
          <div className="flex shrink-0 items-center gap-2">
            <Button
              size="sm"
              variant="outline"
              disabled={!text}
              onClick={() => {
                try { navigator.clipboard.writeText(text || '') } catch {}
              }}
            >
              <Copy className="mr-1.5 h-3.5 w-3.5" /> 复制全部
            </Button>
            <button
              type="button"
              onClick={onClose}
              className="rounded-lg p-1 text-[var(--text-muted)] hover:bg-[var(--bg-pane)]"
              aria-label="关闭"
            >
              <X className="h-4 w-4" />
            </button>
          </div>
        </div>
        {error && (
          <div className="mb-3 rounded-lg border border-red-500/20 bg-red-500/10 px-3 py-2 text-sm text-red-300">
            {error}
          </div>
        )}
        {momoStats && (
          <div className="mb-3 grid grid-cols-2 gap-2 sm:grid-cols-4">
            {[
              ['帐号总数', momoStats.total ?? 0],
              ['有资格', momoStats.ready ?? 0],
              ['无资格', momoStats.ineligible ?? 0],
              ['任务剩余数', momoStats.remaining ?? 0],
            ].map(([label, value]) => (
              <div key={String(label)} className="rounded-lg border border-[var(--border-soft)] bg-[var(--bg-pane)]/40 px-3 py-2">
                <div className="text-[11px] text-[var(--text-muted)]">{label}</div>
                <div className="mt-1 text-sm font-semibold text-[var(--text-primary)]">{value}</div>
              </div>
            ))}
          </div>
        )}
        <pre className="max-h-[60vh] overflow-auto rounded-xl border border-[var(--border-soft)] bg-[var(--bg-pane)]/40 p-3 text-xs leading-6 whitespace-pre-wrap">
          {text || '暂无日志'}
        </pre>
      </div>
    </div>,
    document.body,
  )
}

function BaExtractTaskCell({
  task,
  onViewLogs,
}: {
  task: BaExtractTaskState
  onViewLogs: () => void
}) {
  const status = String(task?.status || 'idle').toLowerCase()
  const running = isBaExtractTaskRunning(task)
  const success = status === 'success'
  const error = status === 'error'
  const logs = Array.isArray(task?.logs) ? task.logs : []
  const latest = logs.length > 0 ? logs[logs.length - 1] : (task?.stage || '')
  const percent = Math.max(0, Math.min(100, ((Number(task?.step || 0)) / (Number(task?.total || 7) || 7)) * 100))
  const cancelled = status === 'cancelled'
  const label = success ? '已提取' : cancelled ? '已终止' : error ? '失败' : running ? (status === 'cancelling' ? '终止中' : '进行中') : task?.ba_token ? '已填写' : '未开始'
  const canOpenLogs = logs.length > 0 || Boolean(task?.stage) || Boolean(task?.error) || Boolean(task?.ba_token) || running
  return (
    <div
      className={cn(
        'flex min-w-[150px] max-w-[190px] flex-col gap-1 rounded-md p-0.5',
        canOpenLogs ? 'cursor-pointer hover:bg-[var(--bg-pane)]/60' : '',
      )}
      onClick={canOpenLogs ? onViewLogs : undefined}
      title={canOpenLogs ? '查看BA链提取日志' : undefined}
      role={canOpenLogs ? 'button' : undefined}
      tabIndex={canOpenLogs ? 0 : undefined}
      onKeyDown={canOpenLogs ? (e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault()
          onViewLogs()
        }
      } : undefined}
    >
      <div className="flex items-center gap-1.5">
        <span
          className={cn(
            'inline-flex min-w-[54px] items-center justify-center rounded border px-1.5 py-0.5 text-[11px] font-bold',
            success || task?.ba_token
              ? 'border-emerald-500/20 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300'
              : error
                ? 'border-red-500/25 bg-red-500/10 text-red-600 dark:text-red-300'
                : running
                  ? 'border-sky-500/25 bg-sky-500/10 text-sky-600 dark:text-sky-300'
                  : 'border-[var(--border-soft)] bg-[var(--bg-pane)] text-[var(--text-muted)]',
          )}
        >
          {running && <Loader2 className="mr-1 h-3 w-3 animate-spin" />}
          {label}
        </span>
        {canOpenLogs && (
          <button
            type="button"
            onClick={(e) => {
              e.stopPropagation()
              onViewLogs()
            }}
            className="rounded border border-[var(--border-soft)] px-1.5 py-0.5 text-[10px] text-[var(--text-muted)] hover:border-[var(--accent-edge)] hover:text-[var(--accent)]"
            title="查看BA链提取日志"
          >
            日志
          </button>
        )}
      </div>
      {running && (
        <div className="h-1 overflow-hidden rounded-full bg-[var(--bg-pane)]">
          <div className="h-full rounded-full bg-sky-500 transition-all" style={{ width: `${percent}%` }} />
        </div>
      )}
      <div className="truncate text-[11px] text-[var(--text-muted)]" title={latest || task?.ba_token || ''}>
        {latest ? compactBaExtractStepDesc(latest) : task?.ba_token ? `${task.ba_token}${task.region_combo ? ` · ${task.region_combo}` : ''}` : '-'}
      </div>
    </div>
  )
}


function splitProxyPoolLines(raw: string): string[] {
  return String(raw || '')
    .replace(/,/g, '\n')
    .split(/\r?\n/)
    .map(line => line.trim())
    .filter(line => line && !line.startsWith('#'))
}

function inferRegionFromProxyText(raw: string, fallback = ''): string {
  const lines = splitProxyPoolLines(raw)
  const sample = lines[0] || String(raw || '').trim()
  if (!sample) return String(fallback || '').toUpperCase()
  if (/^[A-Za-z]{2}$/.test(sample)) return sample.toUpperCase()
  const patterns = [
    /(?:region[-_]?|[-_]g[-_]?|[-_]country[-_]?|[-_]cc[-_]?)([A-Za-z]{2})(?:[^A-Za-z]|$)/i,
    /(?:^|[-_])([A-Za-z]{2})[-_]\d{3,}/i,
    /[-_]([A-Za-z]{2})(?:[-_@]|$)/i,
  ]
  for (const re of patterns) {
    const m = sample.match(re)
    if (m?.[1]) return m[1].toUpperCase()
  }
  return String(fallback || '').toUpperCase()
}

function getAuthTokenForStream(): string {
  try {
    return localStorage.getItem('_auth_token') || ''
  } catch {
    return ''
  }
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

function ensureTrailingNewline(text: string) {
  return text.endsWith('\n') ? text : `${text}\n`
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

function readStoredGopayRegisterForm(): Record<string, any> {
  if (typeof window === 'undefined') return {}
  try {
    const raw = window.localStorage.getItem(GOPAY_REGISTER_FORM_STORAGE_KEY)
    const parsed = raw ? JSON.parse(raw) : {}
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : {}
  } catch {
    return {}
  }
}

function getProviderSettingValues(setting: any): Record<string, any> {
  return {
    ...(setting?.config || {}),
    ...(setting?.auth || {}),
  }
}

function normalizeGopaySmsProvider(value: any): GopaySmsProvider {
  const key = String(value || '').trim().toLowerCase()
  if (key.includes('smsbower')) return 'smsbower'
  if (key.includes('smspool') || key.includes('sms_pool')) return 'smspool'
  if (key === 'smsapi' || key === 'sms_api') return 'smsapi'
  return 'herosms'
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
  const [regCount, setRegCount] = useState(() => platform === 'gopay' ? Math.max(Number(readStoredGopayRegisterForm().count || 1), 1) : 1)
  const [registerCountMode, setRegisterCountMode] = useState<RegisterCountMode>('child')
  const [registerCountNotice, setRegisterCountNotice] = useState('')
  const [concurrency, setConcurrency] = useState(() => platform === 'gopay' ? Math.max(Number(readStoredGopayRegisterForm().concurrency || 1), 1) : 1)
  // chatgpt 平台特定：注册成功后自动获取支付链接能力保留；当前仅前端隐藏入口。
  const [autoPaymentLink] = useState(false)
  const [remoteUploadEnabled, setRemoteUploadEnabled] = useState(false)
  const [agentIdentityAuthJsonMode, setAgentIdentityAuthJsonMode] = useState(false)
  const [k12BatchUploadEnabled, setK12BatchUploadEnabled] = useState(false)
  const [bugfreeMode] = useState(false)
  const [k12Join, setK12Join] = useState(false)
  const [set2faAfterRegister, setSet2faAfterRegister] = useState(true)
  const [setPasswordAfterRegister, setSetPasswordAfterRegister] = useState(true)
  const [k12WorkspaceIds, setK12WorkspaceIds] = useState(() => platform === 'chatgpt' ? readStoredChatgptK12WorkspaceIds() : '')
  const [authflowExperimental] = useState(false)
  const [chatgptProtocolVariant, setChatgptProtocolVariant] = useState<ChatGPTProtocolVariant>('web')
  const [recordHar, setRecordHar] = useState(false)
  const [registerPhoneChangeLimit, setRegisterPhoneChangeLimit] = useState(10)
  const [enableEmailAlias, setEnableEmailAlias] = useState(false)
  const [emailAliasLimit, setEmailAliasLimit] = useState(6)
  const [gmailAliasUsage, setGmailAliasUsage] = useState<GmailApiCodeAliasUsage | null>(null)
  const [gmailAliasUsageLoading, setGmailAliasUsageLoading] = useState(false)
  const [gmailAliasUsageError, setGmailAliasUsageError] = useState('')
  // GoPay 注册草稿按浏览器保存，重新打开弹窗或刷新页面后恢复。
  const [gopaySmsProvider, setGopaySmsProvider] = useState<GopaySmsProvider>(() =>
    normalizeGopaySmsProvider(readStoredGopayRegisterForm().sms_provider || 'herosms'),
  )
  const [gopaySmsApiKeys, setGopaySmsApiKeys] = useState<Record<string, string>>(() => {
    const stored = readStoredGopayRegisterForm().sms_api_keys
    return stored && typeof stored === 'object' && !Array.isArray(stored) ? stored : {}
  })
  const [gopayPin, setGopayPin] = useState(() => String(readStoredGopayRegisterForm().pin || '147258'))
  const [gopayProxy, setGopayProxy] = useState(() => String(readStoredGopayRegisterForm().proxy || ''))
  const [gopayMaxPrice, setGopayMaxPrice] = useState(() => String(readStoredGopayRegisterForm().max_price || '0.011'))
  const [gopaySmsapiPhone, setGopaySmsapiPhone] = useState(() => String(readStoredGopayRegisterForm().smsapi_phone || ''))
  const [gopaySmsapiUrl, setGopaySmsapiUrl] = useState(() => String(readStoredGopayRegisterForm().smsapi_url || ''))
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
    .filter(option => !(option.identityProvider === 'oauth_browser' && ['google', 'microsoft'].includes(String(option.oauthProvider || '').toLowerCase())))
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
  const enabledGopaySmsSettings = (configOptions.sms_settings || []).filter((item: any) => item?.enabled)
  const configuredGopaySmsOptions = GOPAY_SMS_PROVIDER_OPTIONS.filter(option => {
    if (enabledGopaySmsSettings.length === 0) return true
    return enabledGopaySmsSettings.some((item: any) => normalizeGopaySmsProvider(item.provider_key) === option.value)
  })
  const gopaySmsProviderOptions = configuredGopaySmsOptions.length > 0
    ? configuredGopaySmsOptions
    : GOPAY_SMS_PROVIDER_OPTIONS
  const selectedGopaySms = GOPAY_SMS_PROVIDER_OPTIONS.find(option => option.value === gopaySmsProvider) || GOPAY_SMS_PROVIDER_OPTIONS[0]
  const selectedGopaySmsApiKey = String(gopaySmsApiKeys[gopaySmsProvider] || '')

  useEffect(() => {
    if (platform !== 'gopay') return
    if (!gopaySmsProviderOptions.some(option => option.value === gopaySmsProvider)) {
      setGopaySmsProvider(gopaySmsProviderOptions[0]?.value || 'herosms')
      return
    }
    const setting = enabledGopaySmsSettings.find((item: any) => normalizeGopaySmsProvider(item.provider_key) === gopaySmsProvider)
    if (!setting) return
    const values = getProviderSettingValues(setting)
    const keyField = selectedGopaySms.apiKeyField
    if (keyField && !gopaySmsApiKeys[gopaySmsProvider] && values[keyField]) {
      setGopaySmsApiKeys(current => ({ ...current, [gopaySmsProvider]: String(values[keyField]) }))
    }
    const configuredPrice = values[gopaySmsProvider + '_max_price'] || values[gopaySmsProvider + '_max_price_usd']
    if (configuredPrice && gopayMaxPrice === '0.011') {
      setGopayMaxPrice(String(configuredPrice))
    }
    if (gopaySmsProvider === 'smsapi') {
      if (!gopaySmsapiPhone && values.smsapi_phone) setGopaySmsapiPhone(String(values.smsapi_phone))
      if (!gopaySmsapiUrl && values.smsapi_url) setGopaySmsapiUrl(String(values.smsapi_url))
    }
  }, [platform, gopaySmsProvider, gopaySmsApiKeys, gopayMaxPrice, gopaySmsapiPhone, gopaySmsapiUrl, configOptions.sms_settings])

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
    if (platform !== 'gopay' || typeof window === 'undefined') return
    try {
      window.localStorage.setItem(GOPAY_REGISTER_FORM_STORAGE_KEY, JSON.stringify({
        sms_provider: gopaySmsProvider,
        sms_api_keys: gopaySmsApiKeys,
        pin: gopayPin,
        proxy: gopayProxy,
        max_price: gopayMaxPrice,
        smsapi_phone: gopaySmsapiPhone,
        smsapi_url: gopaySmsapiUrl,
        count: regCount,
        concurrency,
      }))
    } catch {
      // Ignore browsers that block localStorage.
    }
  }, [platform, gopaySmsProvider, gopaySmsApiKeys, gopayPin, gopayProxy, gopayMaxPrice, gopaySmsapiPhone, gopaySmsapiUrl, regCount, concurrency])

  useEffect(() => {
    if (platform !== 'chatgpt') return
    let active = true
    setGmailAliasUsageLoading(true)
    setGmailAliasUsageError('')
    apiFetch('/stats/gmail-api-code-alias-usage')
      .then((result) => {
        if (!active) return
        setGmailAliasUsage(result || null)
      })
      .catch((exc: any) => {
        if (!active) return
        setGmailAliasUsage(null)
        setGmailAliasUsageError(exc?.message || '邮箱池统计加载失败')
      })
      .finally(() => {
        if (active) setGmailAliasUsageLoading(false)
      })
    return () => { active = false }
  }, [platform])

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

  useEffect(() => {
    if (chatgptProtocolVariant === 'android' && selection.identityProvider !== 'mailbox') {
      setChatgptProtocolVariant('web')
    }
  }, [chatgptProtocolVariant, selection.identityProvider])

  const defaultMailboxProvider = (configOptions.mailbox_settings || []).find(item => item.is_default) || configOptions.mailbox_settings?.[0] || null
  const normalizedEmailAliasLimit = Math.min(Math.max(Number(emailAliasLimit || EMAIL_ALIAS_HARD_LIMIT), 1), EMAIL_ALIAS_HARD_LIMIT)
  const aliasCountLimitActive = platform === 'chatgpt'
    && selection.identityProvider === 'mailbox'
    && enableEmailAlias
    && defaultMailboxProvider?.provider_key === 'gmail_api_code'
  const emailAliasParentCount = Number(gmailAliasUsage?.summary?.usable_parent_count || 0)
  const apiCodeMailboxCapacity = Number(
    gmailAliasUsage?.summary?.conservative_remaining
      ?? gmailAliasUsage?.summary?.confirmed_remaining
      ?? 0,
  )
  const registerCountMax = aliasCountLimitActive && gmailAliasUsage
    ? (registerCountMode === 'parent' ? emailAliasParentCount : apiCodeMailboxCapacity)
    : 99
  const registerCountConsumedParents = aliasCountLimitActive && gmailAliasUsage
    ? Math.min(emailAliasParentCount, Math.max(regCount, 0))
    : 0

  useEffect(() => {
    if (!aliasCountLimitActive || !gmailAliasUsage) {
      setRegisterCountNotice('')
      return
    }
    if (registerCountMax <= 0) {
      if (regCount !== 0) setRegCount(0)
      setRegisterCountNotice(t('accounts.registrationCountNoMailboxCapacity'))
      return
    }
    if (regCount < 1) {
      setRegCount(1)
      return
    }
    if (regCount > registerCountMax) {
      setRegCount(registerCountMax)
      setRegisterCountNotice(
        registerCountMode === 'parent'
          ? t('accounts.registrationCountParentExceeded', { max: registerCountMax })
          : t('accounts.registrationCountChildExceeded', { max: registerCountMax }),
      )
      return
    }
    setRegisterCountNotice('')
  }, [aliasCountLimitActive, gmailAliasUsage, registerCountMax, registerCountMode, regCount, t])

  const updateRegisterCount = (rawValue: number) => {
    let next = Math.max(Number(rawValue || 0), 0)
    if (!aliasCountLimitActive || !gmailAliasUsage) {
      setRegisterCountNotice('')
      setRegCount(Math.max(next, 1))
      return
    }
    if (registerCountMax <= 0) {
      setRegCount(0)
      setRegisterCountNotice(t('accounts.registrationCountNoMailboxCapacity'))
      return
    }
    next = Math.max(next, 1)
    if (next > registerCountMax) {
      setRegCount(registerCountMax)
      setRegisterCountNotice(
        registerCountMode === 'parent'
          ? t('accounts.registrationCountParentExceeded', { max: registerCountMax })
          : t('accounts.registrationCountChildExceeded', { max: registerCountMax }),
      )
      return
    }
    setRegisterCountNotice('')
    setRegCount(next)
  }

  const start = async () => {
    setStarting(true)
    try {
      const finalRegCount = aliasCountLimitActive && gmailAliasUsage
        ? Math.min(Math.max(Number(regCount || 0), 0), registerCountMax)
        : Math.max(Number(regCount || 1), 1)
      if (finalRegCount < 1) {
        setRegisterCountNotice(t('accounts.registrationCountNoMailboxCapacity'))
        return
      }
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
      if (platform === 'chatgpt' && selection.executorType === 'protocol') {
        extra.chatgpt_protocol_variant = authflowExperimental
          ? 'authflow_experimental'
          : chatgptProtocolVariant
      }
      if (platform === 'chatgpt' && enableEmailAlias) {
        extra.enable_email_alias = true
        extra.email_alias_limit = normalizedEmailAliasLimit
      }
      if (selection.identityProvider === 'mailbox') {
        if (!defaultMailboxProvider?.provider_key) {
          throw new Error(t('accounts.missingDefaultMailbox'))
        }
        extra.mail_provider = defaultMailboxProvider.provider_key
      }
      // GoPay 专属：渠道决定对应的 GoPay country/service，避免服务码错配。
      if (platform === 'gopay') {
        if (!/^\d{6}$/.test(gopayPin.trim())) {
          throw new Error('GoPay PIN 必须是 6 位数字')
        }
        extra.sms_provider = gopaySmsProvider
        extra.gopay_pin = gopayPin.trim()
        extra.gopay_sms_country = selectedGopaySms.country
        extra.gopay_sms_service = selectedGopaySms.service
        if (gopayProxy.trim()) extra.gopay_proxy = gopayProxy.trim()
        if (gopaySmsProvider === 'smsapi') {
          if (!gopaySmsapiPhone.trim() || !gopaySmsapiUrl.trim()) {
            throw new Error('SmsApi 注册必须填写固定手机号和短信查询 URL')
          }
          extra.smsapi_phone = gopaySmsapiPhone.trim()
          extra.smsapi_url = gopaySmsapiUrl.trim()
        } else {
          if (!selectedGopaySmsApiKey.trim()) {
            throw new Error(selectedGopaySms.label + ' 注册必须填写 API key，或在设置页配置对应平台')
          }
          extra[selectedGopaySms.apiKeyField] = selectedGopaySmsApiKey.trim()
          const mp = parseFloat((gopayMaxPrice || '').trim())
          if (!isNaN(mp) && mp >= 0) {
            if (gopaySmsProvider === 'smspool') extra.smspool_max_price = mp
            else if (gopaySmsProvider === 'smsbower') extra.smsbower_max_price = mp
            else extra.herosms_max_price_usd = mp
          }
          if (gopaySmsProvider === 'smspool') {
            extra.smspool_country = selectedGopaySms.country
            extra.smspool_service = selectedGopaySms.service
          }
          if (gopaySmsProvider === 'smsbower') {
            extra.smsbower_country = selectedGopaySms.country
            extra.smsbower_service = selectedGopaySms.service
          }
        }
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
        extra.agent_identity_auth_json_mode = agentIdentityAuthJsonMode
        extra.enable_2fa_after_register = set2faAfterRegister
        extra.set_password_after_register = setPasswordAfterRegister
        extra.k12_batch_upload_enabled = remoteUploadEnabled && k12BatchUploadEnabled
        extra.bugfree_mode = bugfreeMode
      }
      const res = await apiFetch('/tasks/register', {
        method: 'POST',
        body: JSON.stringify({
          platform, count: finalRegCount, concurrency,
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
                  {platform === 'chatgpt' && selection.executorType === 'protocol' ? (
                    <div className="mt-3 grid gap-3 md:grid-cols-2">
                      <button
                        type="button"
                        aria-pressed={chatgptProtocolVariant === 'web'}
                        onClick={() => setChatgptProtocolVariant('web')}
                        className={`rounded-xl border px-4 py-3 text-left transition-colors ${
                          chatgptProtocolVariant === 'web'
                            ? 'border-[var(--accent)] bg-[var(--accent-soft)]'
                            : 'border-[var(--border)] bg-[var(--bg-pane)]/45 hover:border-[var(--accent)]/60'
                        }`}
                      >
                        <div className="flex items-center gap-2 text-sm font-medium text-[var(--text-primary)]">
                          <Globe2 className="h-4 w-4" />
                          WEB协议
                        </div>
                        <div className="mt-1 text-xs text-[var(--text-muted)]">使用当前 ChatGPT WEB 协议注册链路</div>
                      </button>
                      <button
                        type="button"
                        aria-pressed={chatgptProtocolVariant === 'android'}
                        disabled={selection.identityProvider !== 'mailbox'}
                        onClick={() => selection.identityProvider === 'mailbox' && setChatgptProtocolVariant('android')}
                        className={`rounded-xl border px-4 py-3 text-left transition-colors ${
                          selection.identityProvider !== 'mailbox'
                            ? 'cursor-not-allowed border-[var(--border)] bg-[var(--bg-hover)] opacity-50'
                            : chatgptProtocolVariant === 'android'
                              ? 'border-[var(--accent)] bg-[var(--accent-soft)]'
                              : 'border-[var(--border)] bg-[var(--bg-pane)]/45 hover:border-[var(--accent)]/60'
                        }`}
                      >
                        <div className="flex items-center gap-2 text-sm font-medium text-[var(--text-primary)]">
                          <Smartphone className="h-4 w-4" />
                          ANDROID协议
                        </div>
                        <div className="mt-1 text-xs text-[var(--text-muted)]">
                          {selection.identityProvider === 'mailbox' ? '使用 ChatGPT Android App 注册协议链路' : 'ANDROID协议仅支持邮箱身份'}
                        </div>
                      </button>
                    </div>
                  ) : null}
                </div>

                <div className={cn("grid gap-3", platform === 'chatgpt' ? "md:grid-cols-3" : "grid-cols-2")}>
                  {platform === 'chatgpt' && (
                    <div>
                      <label className="text-xs text-[var(--text-muted)] block mb-1">{t('accounts.registrationCountMode')}</label>
                      <select
                        value={registerCountMode}
                        onChange={e => setRegisterCountMode(e.target.value as RegisterCountMode)}
                        className="control-surface control-surface-compact w-full text-center"
                      >
                        <option value="child">{t('accounts.registrationCountModeChild')}</option>
                        <option value="parent">{t('accounts.registrationCountModeParent')}</option>
                      </select>
                    </div>
                  )}
                  <div>
                    <label className="text-xs text-[var(--text-muted)] block mb-1">{t('accounts.registrationCount')}</label>
                    <input type="number" min={aliasCountLimitActive && gmailAliasUsage && registerCountMax <= 0 ? 0 : 1} max={registerCountMax} value={regCount}
                      onChange={e => updateRegisterCount(Number(e.target.value))}
                      className="control-surface control-surface-compact text-center" />
                  </div>
                  <div>
                    <label className="text-xs text-[var(--text-muted)] block mb-1">{t('accounts.concurrency')}</label>
                    <input type="number" min={1} max={10} value={concurrency}
                      onChange={e => setConcurrency(Number(e.target.value))}
                      className="control-surface control-surface-compact text-center" />
                  </div>
                </div>

                {platform === 'chatgpt' && (
                  <div className="rounded-xl border border-[var(--border-soft)] bg-[var(--bg-hover)] px-4 py-3 text-xs text-[var(--text-secondary)]">
                    {aliasCountLimitActive ? (
                      <>
                        <div>
                          {gmailAliasUsageLoading
                            ? t('accounts.registrationCountAliasUsageLoading')
                            : gmailAliasUsage
                              ? t('accounts.registrationCountAliasCapacity', {
                                parents: emailAliasParentCount,
                                aliasLimit: apiCodeMailboxCapacity,
                                max: registerCountMax,
                                consumed: registerCountConsumedParents,
                              })
                              : (gmailAliasUsageError || t('accounts.registrationCountAliasUsageError'))}
                        </div>
                        {registerCountNotice && (
                          <div className="mt-2 rounded-lg border border-amber-500/20 bg-amber-500/10 px-3 py-2 text-amber-600 dark:text-amber-300">
                            {registerCountNotice}
                          </div>
                        )}
                      </>
                    ) : (
                      <div>
                        {t('accounts.registrationCountModeInactiveHint')}
                      </div>
                    )}
                  </div>
                )}

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
                        max={EMAIL_ALIAS_HARD_LIMIT}
                        disabled={!enableEmailAlias}
                        value={emailAliasLimit}
                        onChange={(e) => setEmailAliasLimit(Math.min(Math.max(Number(e.target.value || EMAIL_ALIAS_HARD_LIMIT), 1), EMAIL_ALIAS_HARD_LIMIT))}
                        className="control-surface control-surface-compact w-full text-center"
                      />
                      <div className="mt-1 help-text-xs">
                        {t('accounts.emailAliasLimitHint')}
                      </div>
                    </div>
                  </div>
                )}

                {platform === 'chatgpt' && (
                  <div className="grid gap-3 md:grid-cols-2">
                    <label className="flex items-start gap-2 rounded-xl border border-[var(--border-soft)] bg-[var(--bg-hover)] px-4 py-3 cursor-pointer hover:border-[var(--accent)]/60">
                      <input
                        type="checkbox"
                        checked={set2faAfterRegister}
                        onChange={(e) => setSet2faAfterRegister(e.target.checked)}
                        className="mt-0.5 h-4 w-4 cursor-pointer accent-[var(--accent)]"
                      />
                      <div className="flex-1 text-xs text-[var(--text-secondary)]">
                        <div className="text-sm font-medium text-[var(--text-primary)]">
                          设置2FA
                        </div>
                        <div className="mt-0.5">
                          注册成功并取得 session 后自动绑定 TOTP，保存密钥后可在账号列表查看实时 6 位验证码。
                        </div>
                      </div>
                    </label>
                    <label className="flex items-start gap-2 rounded-xl border border-[var(--border-soft)] bg-[var(--bg-hover)] px-4 py-3 cursor-pointer hover:border-[var(--accent)]/60">
                      <input
                        type="checkbox"
                        checked={setPasswordAfterRegister}
                        onChange={(e) => setSetPasswordAfterRegister(e.target.checked)}
                        className="mt-0.5 h-4 w-4 cursor-pointer accent-[var(--accent)]"
                      />
                      <div className="flex-1 text-xs text-[var(--text-secondary)]">
                        <div className="text-sm font-medium text-[var(--text-primary)]">
                          设置帐号密码
                        </div>
                        <div className="mt-0.5">
                          注册创建资料并取得 session 后写入当前生成密码；设置 2FA 时会先设置密码再绑定。
                        </div>
                      </div>
                    </label>
                  </div>
                )}

                {/* GoPay 专属：接码平台下拉 + 固定 GoPay 服务码 */}
                {platform === 'gopay' && (
                  <div className="rounded-xl border border-[var(--border-soft)] bg-[var(--bg-hover)] px-4 py-3 space-y-3">
                    <div className="text-sm font-medium text-[var(--text-primary)]">GoPay 注册参数</div>
                    <div className="grid gap-3 md:grid-cols-2">
                      <div>
                        <label className="text-xs text-[var(--text-muted)] block mb-1">接码平台</label>
                        <select
                          value={gopaySmsProvider}
                          onChange={(e) => setGopaySmsProvider(e.target.value as GopaySmsProvider)}
                          className="control-surface control-surface-compact w-full"
                        >
                          {gopaySmsProviderOptions.map(option => (
                            <option key={option.value} value={option.value}>{option.label}</option>
                          ))}
                        </select>
                      </div>
                      <div>
                        <label className="text-xs text-[var(--text-muted)] block mb-1">GoPay 固定服务</label>
                        <div className="control-surface control-surface-compact flex w-full items-center justify-between text-sm">
                          <span>{selectedGopaySms.service || '固定手机号查询'}</span>
                          <span className="text-xs text-[var(--text-muted)]">国家 {selectedGopaySms.country || '自有固定号'}</span>
                        </div>
                      </div>
                    </div>
                    {gopaySmsProvider === 'smsapi' ? (
                      <>
                        <div>
                          <label className="text-xs text-[var(--text-muted)] block mb-1">固定手机号（必填）</label>
                          <textarea
                            value={gopaySmsapiPhone}
                            onChange={(e) => setGopaySmsapiPhone(e.target.value)}
                            rows={2}
                            placeholder="+628xxxxxxxxx"
                            className="control-surface control-surface-compact w-full resize-y"
                          />
                        </div>
                        <div>
                          <label className="text-xs text-[var(--text-muted)] block mb-1">短信查询 URL（必填）</label>
                          <input
                            type="text"
                            value={gopaySmsapiUrl}
                            onChange={(e) => setGopaySmsapiUrl(e.target.value)}
                            placeholder="https://example.com/api/sms"
                            className="control-surface control-surface-compact w-full"
                          />
                        </div>
                      </>
                    ) : (
                      <div>
                        <label className="text-xs text-[var(--text-muted)] block mb-1">{selectedGopaySms.label} API key（必填）</label>
                        <input
                          type="text"
                          value={selectedGopaySmsApiKey}
                          onChange={(e) => setGopaySmsApiKeys(current => ({ ...current, [gopaySmsProvider]: e.target.value }))}
                          placeholder={selectedGopaySms.label + ' API key'}
                          className="control-surface control-surface-compact w-full"
                        />
                      </div>
                    )}
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
                      平台切换后自动使用对应的 GoPay 服务码；API key、PIN、代理和价格上限会自动保存，下次打开继续使用。
                    </div>
                  </div>
                )}

                {platform === 'chatgpt' && (
                  <label className="flex items-start gap-2 rounded-xl border border-[var(--border-soft)] bg-[var(--bg-hover)] px-4 py-3 cursor-pointer hover:border-[var(--accent)]/60">
                    <input
                      type="checkbox"
                      checked={agentIdentityAuthJsonMode}
                      onChange={(e) => setAgentIdentityAuthJsonMode(e.target.checked)}
                      className="mt-0.5 h-4 w-4 cursor-pointer accent-[var(--accent)]"
                    />
                    <div className="flex-1 text-xs text-[var(--text-secondary)]">
                      <div className="text-sm font-medium text-[var(--text-primary)]">
                        Agent Identity auth.json
                      </div>
                      <div className="mt-0.5">
                        勾选后账号注册成功会生成 Agent Identity auth.json 并上传到远端 Sub2Api；上传成功后当前账号才计入任务成功。
                      </div>
                    </div>
                  </label>
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
                  <div className="rounded-xl border border-[var(--border-soft)] bg-[var(--bg-hover)] px-4 py-3">
                    <label className="flex items-start gap-2 cursor-pointer">
                      <input
                        type="checkbox"
                        checked={remoteUploadEnabled}
                        onChange={(e) => {
                          const checked = e.target.checked
                          setRemoteUploadEnabled(checked)
                          if (!checked) setK12BatchUploadEnabled(false)
                        }}
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
                    {remoteUploadEnabled && (
                      <label className="mt-3 flex items-start gap-2 rounded-lg border border-[var(--border-soft)] bg-[var(--bg-card)]/60 px-3 py-2 cursor-pointer hover:border-[var(--accent)]/60">
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
                            勾选后按当前流程在任务结束后合并 SUB2API JSON 并统一上传；未勾选则恢复为每个账号成功后逐个上传。
                          </div>
                        </div>
                      </label>
                    )}
                  </div>
                )}

                <div className="rounded-xl border border-[var(--border-soft)] bg-[var(--bg-hover)] px-4 py-3 text-xs text-[var(--text-secondary)]">
                  <div>{t('accounts.identitySummary')}: <span className="text-[var(--text-primary)]">{selectedRegistration?.label || '-'}</span></div>
                  <div className="mt-1">{t('accounts.executorSummary')}: <span className="text-[var(--text-primary)]">{selectedExecutor?.label || '-'}</span></div>
                  <div className="mt-1">{t('accounts.verificationSummary')}: <span className="text-[var(--text-primary)]">{getCaptchaStrategyLabel(selection.executorType, configOptions.captcha_policy, configOptions.captcha_providers, language)}</span></div>
                  {selection.identityProvider === 'oauth_browser' && !reusableBrowser && (
                    <div className="mt-2 text-amber-400">后台浏览器自动依赖 Chrome Profile 或 Chrome CDP，未配置时只允许可视浏览器自动。</div>
                  )}
                </div>

              </>
            )
          ) : (
            <TaskLogPanel taskId={taskId} onDone={handleDone} />
          )}
        </div>
        <div className="shrink-0 px-6 py-3 border-t border-[var(--border)] bg-[var(--bg-base)]">
          {!taskId ? (
            <div className="flex gap-3">
              <Button
                onClick={start}
                disabled={starting || !selection.identityProvider || !selection.executorType}
                className="flex-1"
              >
                {starting ? t('accounts.starting') : t('accounts.startAutoRegister')}
              </Button>
              <Button variant="outline" size="sm" onClick={onClose} className="px-5">
                {t('common.cancel')}
              </Button>
            </div>
          ) : (
            <div className="flex justify-end">
              <Button variant="outline" size="sm" onClick={onClose}>
                {done ? t('common.close') : t('common.cancel')}
              </Button>
            </div>
          )}
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
  if (tone === 'good') return 'border-teal-500/35 bg-white text-slate-950 shadow-sm dark:bg-slate-950 dark:text-slate-100'
  if (tone === 'warning') return 'border-amber-500/45 bg-white text-slate-950 shadow-sm dark:bg-slate-950 dark:text-slate-100'
  if (tone === 'danger') return 'border-red-500/40 bg-white text-slate-950 shadow-sm dark:bg-slate-950 dark:text-slate-100'
  return 'border-[var(--border)] bg-white text-slate-950 shadow-sm dark:bg-slate-950 dark:text-slate-100'
}

function metricAccentClass(tone?: string) {
  if (tone === 'good') return 'from-teal-500 to-cyan-500'
  if (tone === 'warning') return 'from-amber-500 to-orange-500'
  if (tone === 'danger') return 'from-red-500 to-rose-500'
  return 'from-[var(--accent)] to-[var(--accent-strong)]'
}

function DisplayMetricCard({ metric, compact = false }: { metric: any; compact?: boolean }) {
  return (
    <div className={`group relative overflow-hidden rounded-lg border px-3.5 py-3 ${metricToneClass(metric?.tone)}`}>
      <div className={`pointer-events-none absolute inset-y-0 left-0 w-1 bg-gradient-to-b ${metricAccentClass(metric?.tone)}`} />
      <div className="relative flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="text-[10px] font-semibold uppercase tracking-[0.16em] text-slate-600 dark:text-slate-300">{metric?.label || '-'}</div>
          {metric?.sub ? <div className="mt-1 truncate text-[11px] font-medium text-slate-500 dark:text-slate-400">{metric.sub}</div> : null}
        </div>
        <div className={`${compact ? 'text-sm' : 'text-lg'} shrink-0 font-bold tracking-[-0.03em] text-slate-950 dark:text-slate-50`}>{formatResultValue(metric?.value)}</div>
      </div>
      {typeof metric?.percent === 'number' ? (
        <div className="relative mt-3 h-1.5 overflow-hidden rounded-full bg-slate-200 dark:bg-slate-800">
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
        <div key={section?.key || section?.title} className="rounded-xl border border-slate-200 bg-white p-3 shadow-sm dark:border-slate-800 dark:bg-slate-950">
          <div className="text-xs font-semibold text-[var(--text-primary)]">{section?.title || '明细'}</div>
          <div className="mt-3 grid gap-3 md:grid-cols-2">
            {(Array.isArray(section?.items) ? section.items : []).map((item: any, index: number) => (
              <div key={`${item?.title || 'item'}-${index}`} className="rounded-lg border border-slate-200 bg-slate-50 p-3 dark:border-slate-800 dark:bg-slate-900">
                <div className="text-xs font-semibold text-[var(--text-primary)]">{item?.title || '-'}</div>
                <div className="mt-2 grid grid-cols-2 gap-2 text-xs text-slate-700 dark:text-slate-300">
                  {(Array.isArray(item?.metrics) ? item.metrics : []).map((metric: any) => (
                    <div key={metric?.key || metric?.label}>
                      <span className="text-slate-500 dark:text-slate-400">{metric?.label || '-'}: </span>
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

function PlanRefreshLogDialog({
  state,
  onClose,
}: {
  state: PlanRefreshDialogState
  onClose: () => void
}) {
  const logEndRef = useRef<HTMLDivElement | null>(null)
  useEffect(() => {
    logEndRef.current?.scrollIntoView({ block: 'end' })
  }, [state.logs.length, state.currentEmail])
  const pending = Math.max(state.total - state.success - state.failed, 0)
  const rawText = JSON.stringify(state.logs.map(item => item.raw ? { email: item.email, raw: item.raw } : { email: item.email, status: item.status, message: item.message }), null, 2)

  return (
    <div className="dialog-backdrop" onClick={() => !state.running && onClose()}>
      <div
        className="dialog-panel dialog-panel-md flex flex-col overflow-hidden rounded-2xl border-[var(--border-soft)] bg-[var(--bg-card)] shadow-[var(--shadow-hard)]"
        onClick={event => event.stopPropagation()}
        style={{ width: 'min(920px, calc(100vw - 32px))', height: 'min(760px, calc(100dvh - 48px))' }}
      >
        <div className="relative shrink-0 overflow-hidden border-b border-emerald-500/15 bg-gradient-to-r from-emerald-500/12 via-[var(--bg-elevated)] to-[var(--bg-elevated)] px-6 py-4">
          <div className="flex items-start justify-between gap-4">
            <div className="min-w-0">
              <div className="mb-2 inline-flex rounded-full bg-emerald-500/10 px-3 py-0.5 text-[10px] font-semibold uppercase tracking-[0.14em] text-emerald-700 dark:text-emerald-300">
                Plan Refresh
              </div>
              <h2 className="truncate text-[16px] font-bold text-[var(--text-primary)]">一键刷新套餐日志</h2>
              <p className="mt-1 text-[12px] font-medium text-[var(--text-secondary)]">
                {state.running ? `正在处理：${state.currentEmail || '-'}` : '刷新已结束，可以关闭窗口。'}
              </p>
            </div>
            <div className="flex items-center gap-2">
              <Badge variant={state.running ? 'warning' : state.failed ? 'danger' : 'success'}>
                {state.running ? '运行中' : state.failed ? '部分失败' : '已完成'}
              </Badge>
              <button
                onClick={() => !state.running && onClose()}
                disabled={state.running}
                className="rounded-full border border-transparent bg-[var(--bg-pane)] p-2 text-[var(--text-secondary)] hover:text-[var(--text-primary)] disabled:cursor-not-allowed disabled:opacity-40"
              >
                <X className="h-4 w-4" />
              </button>
            </div>
          </div>
        </div>
        <div className="grid shrink-0 grid-cols-2 gap-3 border-b border-[var(--border-soft)] bg-[var(--bg-base)] px-6 py-4 md:grid-cols-4">
          {[
            ['总数', state.total],
            ['成功', state.success],
            ['失败', state.failed],
            ['待处理', pending],
          ].map(([label, value]) => (
            <div key={String(label)} className="rounded-xl border border-[var(--border-soft)] bg-[var(--bg-card)] px-4 py-3 shadow-sm">
              <div className="text-[11px] font-medium text-[var(--text-muted)]">{label}</div>
              <div className="mt-1 text-xl font-bold text-[var(--text-primary)]">{value}</div>
            </div>
          ))}
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto bg-[var(--bg-base)] px-6 py-5">
          <div className="space-y-2 font-mono text-[12px]">
            {state.logs.map(item => (
              <div key={item.id} className="rounded-xl border border-[var(--border-soft)] bg-[var(--bg-card)] p-3 shadow-sm">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="min-w-0 truncate font-semibold text-[var(--text-primary)]">{item.email}</div>
                  <Badge variant={item.status === 'success' ? 'success' : item.status === 'error' ? 'danger' : item.status === 'running' ? 'warning' : 'secondary'}>
                    {item.status === 'success' ? '成功' : item.status === 'error' ? '失败' : item.status === 'running' ? '处理中' : '等待'}
                  </Badge>
                </div>
                <div className="mt-2 grid gap-2 text-[11px] text-[var(--text-secondary)] md:grid-cols-4">
                  <span>套餐：{item.planName || '-'}</span>
                  <span>状态：{item.planState || '-'}</span>
                  <span>订阅：{item.subscriptionStatus || '-'}</span>
                  <span>usage：{item.usagePlanType || '-'}</span>
                </div>
                <div className="mt-2 break-all text-[11px] text-[var(--text-muted)]">{item.message}</div>
              </div>
            ))}
            <div ref={logEndRef} />
          </div>
        </div>
        <div className="flex shrink-0 items-center justify-between gap-3 border-t border-[var(--border-soft)] bg-[var(--bg-elevated)] px-6 py-3">
          <Button variant="outline" size="sm" onClick={() => navigator.clipboard?.writeText(rawText)}>
            <Copy className="mr-1.5 h-3.5 w-3.5" />
            复制接口返回
          </Button>
          <Button variant="outline" size="sm" onClick={onClose} disabled={state.running}>关闭</Button>
        </div>
      </div>
    </div>
  )
}

function TotpCodeDialog({
  account,
  code,
  remain,
  copied,
  onClose,
  onCopy,
}: {
  account: { email?: string }
  code: string
  remain: number
  copied: boolean
  onClose: () => void
  onCopy: () => void
}) {
  if (!account) return null
  return createPortal(
    <div className="dialog-backdrop" onClick={onClose}>
      <div
        className="dialog-panel dialog-panel-md flex flex-col overflow-hidden rounded-2xl border-[var(--border-soft)] bg-[var(--bg-card)] shadow-[var(--shadow-hard)]"
        onClick={event => event.stopPropagation()}
        style={{ width: 'min(560px, calc(100vw - 32px))', maxHeight: 'min(620px, calc(100dvh - 48px))' }}
      >
        <div className="relative shrink-0 overflow-hidden border-b border-[var(--border-soft)] bg-[var(--bg-elevated)] px-6 py-4">
          <div className="flex items-start justify-between gap-4">
            <div className="min-w-0">
              <div className="mb-2 inline-flex rounded-full bg-[rgba(var(--accent-rgb),0.1)] px-3 py-0.5 text-[10px] font-semibold uppercase tracking-[0.14em] text-[var(--accent)]">
                2FA
              </div>
              <h2 className="truncate text-[16px] font-bold text-[var(--text-primary)]">查看2FA验证码</h2>
              <p className="mt-1 truncate text-[12px] font-medium text-[var(--text-secondary)]">{account.email || '-'}</p>
            </div>
            <button
              onClick={onClose}
              className="rounded-full border border-transparent bg-[var(--bg-pane)] p-2 text-[var(--text-secondary)] hover:text-[var(--text-primary)]"
            >
              <X className="h-4 w-4" />
            </button>
          </div>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto bg-[var(--bg-base)] px-6 py-5">
          <div className="rounded-2xl border border-[var(--border-soft)] bg-[var(--bg-card)] p-4 shadow-sm">
            <div className="text-[11px] font-medium text-[var(--text-muted)]">当前验证码</div>
            <div className="mt-2 flex items-end justify-between gap-3">
              <div className="font-mono text-4xl font-bold tracking-[0.18em] text-[var(--text-primary)]">{code || '-'}</div>
              <Badge variant={remain > 0 ? 'success' : 'warning'}>
                {remain > 0 ? `剩余 ${remain} 秒` : '即将过期'}
              </Badge>
            </div>
            {copied ? (
              <div className="mt-3 text-sm font-medium text-emerald-600 dark:text-emerald-300">
                已复制到剪贴板
              </div>
            ) : (
              <div className="mt-3 text-sm text-[var(--text-secondary)]">
                点击复制后可直接粘贴使用
              </div>
            )}
          </div>
          <div className="mt-4 rounded-xl border border-[var(--border-soft)] bg-[var(--bg-pane)]/50 px-4 py-3 text-sm text-[var(--text-secondary)]">
            这个验证码会按当前时间窗口自动刷新，建议复制后尽快使用。
          </div>
        </div>

        <div className="flex shrink-0 items-center justify-end gap-2 border-t border-[var(--border-soft)] bg-[var(--bg-elevated)] px-6 py-3">
          <Button variant="outline" onClick={onCopy} disabled={!code}>
            <Copy className="mr-1.5 h-3.5 w-3.5" />
            复制验证码
          </Button>
          <Button onClick={onClose}>确定</Button>
        </div>
      </div>
    </div>,
    document.body,
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
            if (param.type === 'checkbox') {
              return (
                <label
                  key={param.key}
                  className="flex items-start gap-3 rounded-xl border border-[var(--border-soft)] bg-[var(--bg-hover)] px-3 py-2.5"
                >
                  <input
                    type="checkbox"
                    checked={value === 'true'}
                    onChange={e => setForm(current => ({ ...current, [param.key]: e.target.checked ? 'true' : '' }))}
                    className="mt-0.5 h-4 w-4 rounded border-[var(--border-soft)] accent-[var(--accent)]"
                  />
                  <span>
                    <span className="block text-sm font-medium text-[var(--text-primary)]">{param.label || param.key}</span>
                    {param.description && (
                      <span className="mt-0.5 block text-xs text-[var(--text-muted)]">{param.description}</span>
                    )}
                  </span>
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
  onViewTotp,
}: {
  acc: any
  onDetail: () => void
  onDelete: () => void
  onResult: (title: string, payload: any) => void
  onChanged: () => void
  onTriggerGetRt?: (acc: any, kind: 'get_rt' | 'get_rt_bypass') => void
  onViewTotp?: (acc: any) => void
}) {
  const { t, language } = useI18n()
  const [open, setOpen] = useState(false)
  const [actions, setActions] = useState<any[]>([])
  const [running, setRunning] = useState<string | null>(null)
  const [toast, setToast] = useState<{ type: 'success' | 'error'; text: string } | null>(null)
  const [actionTask, setActionTask] = useState<{ taskId: string; title: string } | null>(null)
  const [actionTaskStatus, setActionTaskStatus] = useState<string | null>(null)
  const [pendingAction, setPendingAction] = useState<{ action: any; params: Record<string, string> } | null>(null)
  const [actionLaunching, setActionLaunching] = useState<{ id: string; label: string } | null>(null)
  const [menuPosition, setMenuPosition] = useState({ top: 0, left: 0, maxHeight: 320 })
  const menuRef = useRef<HTMLDivElement>(null)
  const triggerRef = useRef<HTMLButtonElement>(null)
  const canCopySession = String(acc?.platform || '').trim().toLowerCase() === 'chatgpt'
  const canCopyK12Session = canCopySession && !isEmptyPayload(getChatgptK12SessionPayload(acc))
  const canViewTotp = canCopySession && Boolean(getChatgptTotpSecret(acc))
  const hasMenuItems = actions.length > 0 || canCopySession || canCopyK12Session || canViewTotp

  const runAction = (action: any, params: Record<string, any>) => {
    setRunning(action.id)
    setActionTaskStatus(null)
    setActionLaunching({ id: action.id, label: action.label || '操作' })
    apiFetch(`/actions/${acc.platform}/${acc.id}/${action.id}`, { method: 'POST', body: JSON.stringify({ params }) })
      .then(resp => {
        setActionLaunching(null)
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
        setActionLaunching(null)
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
    const copyActionCount = (canCopySession ? 1 : 0) + (canCopyK12Session ? 1 : 0) + (canViewTotp ? 1 : 0)
    const estimatedHeight = (actions.length + copyActionCount + 1) * 40 + 64
    const desiredHeight = Math.min(
      menuRef.current?.scrollHeight || estimatedHeight,
      window.innerHeight - viewportPadding * 2,
    )

    let left = rect.right - menuWidth
    if (left < viewportPadding) left = viewportPadding
    if (left + menuWidth > window.innerWidth - viewportPadding) {
      left = Math.max(viewportPadding, window.innerWidth - menuWidth - viewportPadding)
    }

    const gap = 8
    const spaceBelow = window.innerHeight - rect.bottom - viewportPadding - gap
    const spaceAbove = rect.top - viewportPadding - gap
    const openUp = spaceBelow < desiredHeight && spaceAbove > spaceBelow
    const availableHeight = Math.max(96, openUp ? spaceAbove : spaceBelow)
    const maxHeight = Math.min(desiredHeight, availableHeight)
    const top = openUp
      ? Math.max(viewportPadding, rect.top - maxHeight - gap)
      : Math.min(rect.bottom + gap, window.innerHeight - viewportPadding - maxHeight)

    setMenuPosition({
      top: Math.round(top),
      left: Math.round(left),
      maxHeight: Math.round(maxHeight),
    })
  }, [actions.length, canCopySession, canCopyK12Session, canViewTotp])

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
      {actionLaunching && !actionTask && !pendingAction && (
        <div className="fixed top-5 right-5 z-[9999] flex items-center gap-2.5 rounded-xl border border-[var(--border-soft)] bg-[var(--bg-card)] px-4 py-3 text-[13px] font-medium text-[var(--text-secondary)] shadow-lg">
          <Loader2 className="h-4 w-4 animate-spin text-[var(--accent)]" />
          <span>{actionLaunching.label}：正在启动任务...</span>
        </div>
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
                  {canViewTotp && (
                    <button
                      onClick={async () => {
                        setOpen(false)
                        onViewTotp?.(acc)
                      }}
                      className="w-full px-3 py-2 text-left text-xs text-[var(--text-secondary)] transition-colors hover:bg-[var(--bg-hover)] hover:text-[var(--text-primary)]"
                    >
                      查看2FA验证码
                    </button>
                  )}
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
          <div className="relative overflow-hidden rounded-lg border border-slate-200 bg-white p-4 shadow-sm dark:border-slate-800 dark:bg-slate-950">
            <div className="pointer-events-none absolute -right-16 -top-20 h-44 w-44 rounded-full bg-teal-100/50 blur-3xl dark:bg-teal-950/30" />
            <div className="relative flex flex-wrap items-start justify-between gap-3">
              <div>
                <div className="text-xs font-semibold uppercase tracking-[0.18em] text-slate-600 dark:text-slate-300">核心状态</div>
                <div className="mt-2 flex flex-wrap items-center gap-2">
                  <Badge variant={STATUS_VARIANT[getDisplayStatus(acc)] || 'secondary'}>{getDisplayStatus(acc)}</Badge>
                  <span className="text-lg font-semibold tracking-[-0.03em] text-[var(--text-primary)]">{acc.plan_name || overview.plan_name || overview.plan || getPlanState(acc)}</span>
                </div>
              </div>
              <div className="grid grid-cols-2 gap-2 text-right text-[11px] text-slate-600 dark:text-slate-300 sm:grid-cols-3">
                <div className="rounded-xl border border-slate-200 bg-slate-50 px-2.5 py-2 dark:border-slate-800 dark:bg-slate-900">
                  <div className="uppercase tracking-[0.12em]">生命周期</div>
                  <div className="mt-1 font-semibold text-slate-950 dark:text-slate-50">{getLifecycleStatus(acc)}</div>
                </div>
                <div className="rounded-xl border border-slate-200 bg-slate-50 px-2.5 py-2 dark:border-slate-800 dark:bg-slate-900">
                  <div className="uppercase tracking-[0.12em]">有效性</div>
                  <div className="mt-1 font-semibold text-slate-950 dark:text-slate-50">{getValidityStatusLabel(acc)}</div>
                </div>
                <div className="rounded-xl border border-slate-200 bg-slate-50 px-2.5 py-2 dark:border-slate-800 dark:bg-slate-900">
                  <div className="uppercase tracking-[0.12em]">套餐状态</div>
                  <div className="mt-1 font-semibold text-slate-950 dark:text-slate-50">{getPlanState(acc)}</div>
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
                <div className="rounded-xl border border-slate-200 bg-slate-50 px-3 py-2 text-xs font-medium text-slate-700 dark:border-slate-800 dark:bg-slate-900 dark:text-slate-300">
                  验证码邮箱: {verificationMailbox.email} · {verificationMailbox.provider || '-'} · ID {verificationMailbox.account_id || '-'}
                </div>
              )}
            </div>
          )}
          {providerAccounts.length > 0 && (
            <div className="space-y-2">
              <label className="text-xs text-[var(--text-muted)] block">Provider Accounts</label>
              {providerAccounts.map((item: any, index: number) => (
                <div key={`${item.provider_name || 'provider'}-${item.login_identifier || index}`} className="rounded-xl border border-slate-200 bg-white p-3 shadow-sm dark:border-slate-800 dark:bg-slate-950">
                  <div className="text-xs font-semibold text-[var(--text-primary)]">
                    {item.provider_name || item.provider_type || 'provider'}
                  </div>
                  <div className="mt-1 text-xs font-medium text-slate-700 break-all dark:text-slate-300">
                    登录标识: {item.login_identifier || '-'}
                  </div>
                  {item.credentials && Object.keys(item.credentials).length > 0 && (
                    <div className="mt-2 grid gap-2">
                      {Object.entries(item.credentials).map(([key, value]: [string, any]) => (
                        <div key={key}>
                          <div className="text-[11px] text-[var(--text-muted)]">{key}</div>
                          <div className="flex items-start gap-1">
                            <div className="flex-1 rounded-md border border-slate-200 bg-slate-50 px-2 py-1.5 text-xs font-mono text-slate-700 break-all max-h-40 overflow-y-auto dark:border-slate-800 dark:bg-slate-900 dark:text-slate-300">
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
                <div key={`${item.scope}-${item.key}`} className="rounded-xl border border-slate-200 bg-white p-3 shadow-sm dark:border-slate-800 dark:bg-slate-950">
                  <div className="text-[11px] text-[var(--text-muted)]">{item.key}</div>
                  <div className="mt-1 flex items-start gap-1">
                    <div className="flex-1 rounded-md border border-slate-200 bg-slate-50 px-2 py-1.5 text-xs font-mono text-slate-700 break-all max-h-40 overflow-y-auto dark:border-slate-800 dark:bg-slate-900 dark:text-slate-300">
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
  tagFilter,
  searchFilter,
  selectedIds,
  triggerClassName,
  showIcon = true,
}: {
  platform: string
  total: number
  statusFilter: string
  tagFilter: string
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
          tag_filter: !hasSelection ? tagFilter || null : null,
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
  const [filterTag, setFilterTag] = useState('')
  const [page, setPage] = useState(1)
  const [detail, setDetail] = useState<any | null>(null)
  const [showImport, setShowImport] = useState(false)
  const [showAdd, setShowAdd] = useState(false)
  const [showRegister, setShowRegister] = useState(false)
  const [showPpPlusSettings, setShowPpPlusSettings] = useState(false)
  const [ppPlusStatus, setPpPlusStatus] = useState<PpPlusStatus | null>(null)
  const [ppBaAccount, setPpBaAccount] = useState<any | null>(null)
  const [baExtractAccount, setBaExtractAccount] = useState<any | null>(null)
  const [baExtractForm, setBaExtractForm] = useState<Record<string, string>>(() => loadBaExtractSettings())
  const [baExtractRunning, setBaExtractRunning] = useState(false)
  const [baExtractTasks, setBaExtractTasks] = useState<Record<string, BaExtractTaskState>>({})
  const baExtractTaskStreamRefs = useRef<Record<string, AbortController>>({})
  const [ppLogTask, setPpLogTask] = useState<PpAccountTask | null>(null)
  const [baExtractLogTaskId, setBaExtractLogTaskId] = useState<number | null>(null)
  const [baExtractStopping, setBaExtractStopping] = useState(false)
  const [platformsMap, setPlatformsMap] = useState<Record<string, any>>({})
  const [selectedIds, setSelectedIds] = useState<Set<number>>(new Set())
  const [visiblePasswordIds, setVisiblePasswordIds] = useState<Set<number>>(new Set())
  const [actionResult, setActionResult] = useState<{ title: string; payload: any } | null>(null)
  const [totpDialog, setTotpDialog] = useState<TotpDialogState | null>(null)
  const totpRefreshInFlightRef = useRef(false)
  const [bulkDeleting, setBulkDeleting] = useState(false)
  const [invalidDeleting, setInvalidDeleting] = useState(false)
  const [batchRefreshing, setBatchRefreshing] = useState(false)
  const [batchHealthChecking, setBatchHealthChecking] = useState(false)
  const [batchPlanRefreshing, setBatchPlanRefreshing] = useState(false)
  const [agentsUploadBusy, setAgentsUploadBusy] = useState(false)
  const [rowActionBusy, setRowActionBusy] = useState('')
  const [copyingAccessTokenId, setCopyingAccessTokenId] = useState<number | null>(null)
  const [planRefreshDialog, setPlanRefreshDialog] = useState<PlanRefreshDialogState>({
    open: false,
    running: false,
    total: 0,
    success: 0,
    failed: 0,
    currentEmail: '',
    logs: [],
  })
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
  const [refreshSessionTaskId, setRefreshSessionTaskId] = useState('')
  const [refreshSessionBusy, setRefreshSessionBusy] = useState(false)
  const [batchSecurityBusy, setBatchSecurityBusy] = useState(false)
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

  const [pageSize, setPageSize] = useState(10)

  useEffect(() => {
    setSelectedIds(new Set())
    setPage(1)
  }, [tab, filterStatus, filterTag, debouncedSearch, pageSize])

  const load = useCallback(async (p = tab, s = debouncedSearch, fs = filterStatus, ft = filterTag, pg = page, ps = pageSize) => {
    setLoading(true)
    try {
      const params = new URLSearchParams({ platform: p, page: String(pg), page_size: String(ps) })
      if (s) params.set('email', s)
      if (fs) params.set('status', fs)
      if (ft) params.set('tag', ft)
      const data = await apiFetch(`/accounts?${params}`)
      setAccounts(data.items); setTotal(data.total)
    } finally { setLoading(false) }
  }, [tab, debouncedSearch, filterStatus, filterTag, page, pageSize])

  const fetchPlanRefreshTargets = useCallback(async () => {
    const targets: any[] = []
    let currentPage = 1
    const targetPageSize = 200
    while (true) {
      const params = new URLSearchParams({ platform: tab, page: String(currentPage), page_size: String(targetPageSize) })
      if (debouncedSearch) params.set('email', debouncedSearch)
      if (filterStatus) params.set('status', filterStatus)
      if (filterTag) params.set('tag', filterTag)
      const data = await apiFetch(`/accounts?${params}`)
      const items = Array.isArray(data?.items) ? data.items : []
      targets.push(...items)
      const totalItems = Number(data?.total || targets.length)
      if (targets.length >= totalItems || items.length === 0) break
      currentPage += 1
    }
    return targets
  }, [tab, debouncedSearch, filterStatus, filterTag])

  useEffect(() => { load(tab, debouncedSearch, filterStatus, filterTag, page, pageSize) }, [tab, debouncedSearch, filterStatus, filterTag, page, pageSize, load])

  useEffect(() => {
    setSelectedIds(prev => {
      const visible = new Set(accounts.map(acc => acc.id))
      return new Set([...prev].filter(id => visible.has(id)))
    })
  }, [accounts])

  useEffect(() => {
    setBaExtractTasks(prev => {
      const next = { ...prev }
      for (const acc of accounts) {
        const key = String(acc?.id || '')
        if (!key) continue
        const persisted = getAccountBaExtractTask(acc)
        const live = next[key]
        if (live && isBaExtractTaskRunning(live)) continue
        if (
          persisted.status !== 'idle'
          || persisted.ba_token
          || persisted.stage
          || (persisted.logs || []).length > 0
        ) {
          next[key] = persisted
        }
      }
      return next
    })
  }, [accounts])

  
  useEffect(() => {
    let active = true
    const tick = async () => {
      try {
        const status = await fetchPpPlusStatus()
        if (active) setPpPlusStatus(status)
      } catch {
        // ignore status poll errors
      }
    }
    tick()
    const timer = window.setInterval(tick, 2000)
    return () => {
      active = false
      window.clearInterval(timer)
    }
  }, [])

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
    .filter(acc => getRtTaskMode === 'target' ? isGetRtTargetModeAccount(acc) : !hasChatgptRefreshToken(acc))
    .map(acc => Number(acc.id))
    .filter(id => Number.isFinite(id) && id > 0)
  const ppLiveMap = (ppPlusStatus?.accounts && typeof ppPlusStatus.accounts === 'object') ? ppPlusStatus.accounts : {}
  const resolvePpTask = (acc: any): PpAccountTask => {
    const live = ppLiveMap[String(acc?.id)] || (ppPlusStatus?.current && Number(ppPlusStatus.current.account_id) === Number(acc?.id) ? ppPlusStatus.current : null)
    return getAccountPpTask(acc, live)
  }
  const resolveBaExtractTask = (acc: any): BaExtractTaskState => (
    getAccountBaExtractTask(acc, baExtractTasks[String(acc?.id)] || null)
  )
  const baExtractLogTask = (() => {
    if (!baExtractLogTaskId) return null
    const live = baExtractTasks[String(baExtractLogTaskId)]
    if (live) return live
    const acc = accounts.find(item => Number(item?.id) === Number(baExtractLogTaskId))
    return acc ? resolveBaExtractTask(acc) : null
  })()

  const applyBaExtractEvent = useCallback((accountId: number, evt: any) => {
    if (!Number.isFinite(accountId) || !evt || typeof evt !== 'object') return
    setBaExtractTasks(prev => {
      const key = String(accountId)
      const current = prev[key] || { account_id: accountId, status: 'idle', step: 0, total: 7, logs: [] }
      const logs = Array.isArray(current.logs) ? [...current.logs] : []
      const pushLog = (line: string) => {
        if (!line) return
        logs.push(withBaExtractLogTime(line, Number(evt.time || 0)))
        if (logs.length > 300) logs.splice(0, logs.length - 300)
      }
      let next: BaExtractTaskState = { ...current, account_id: accountId, logs }
      const type = String(evt.type || '')
      const currentStatus = String(current.status || '').toLowerCase()
      const incomingStatus = String(evt.status || '').toLowerCase()
      const sameTask = !evt.task_id || !current.task_id || String(evt.task_id) === String(current.task_id)
      if (
        currentStatus === 'cancelled'
        && sameTask
        && (
          ['progress', 'saved'].includes(type)
          || (type === 'snapshot' && ['queued', 'started', 'running', 'cancelling'].includes(incomingStatus))
        )
      ) {
        return prev
      }
      if (evt.task_id) next.task_id = String(evt.task_id)
      if (evt.time) next.updated_at = Number(evt.time)
      if (evt.region_combo) next.region_combo = String(evt.region_combo)
      if (type === 'snapshot') {
        next = { ...next, ...evt, account_id: accountId, logs: Array.isArray(evt.logs) ? evt.logs : logs }
      } else if (type === 'started') {
        // 新任务/重启：清空旧日志，避免「再次执行仍显示上次日志」
        const stage = String(evt.desc || '任务已开始')
        next = {
          ...next,
          status: String(evt.status || 'running'),
          stage,
          step: 0,
          attempt: 0,
          error: '',
          ba_url: '',
          max_attempts: Number(evt.max_attempts || next.max_attempts || 20),
          logs: stage ? [withBaExtractLogTime(stage, Number(evt.time || 0))] : [],
        }
        // 已直接写入 logs，跳过 pushLog 追加
        return { ...prev, [key]: next }
      } else if (type === 'progress') {
        next.step = Number(evt.step || next.step || 0)
        next.total = Number(evt.total || next.total || 7)
        next.attempt = Number(evt.attempt || next.attempt || 0)
        next.stage = String(evt.desc || next.stage || '')
        next.status = /终止/.test(next.stage) ? 'cancelling' : 'running'
        pushLog(`步骤 ${next.step}/${next.total}: ${next.stage}`)
      } else if (type === 'saved') {
        next.ba_token = String(evt.ba_token || next.ba_token || '')
        next.stage = '已写回 BA 链'
        pushLog(`已写回 BA: ${next.ba_token}`)
      } else if (type === 'done') {
        if (evt.ok) {
          next.status = 'success'
          next.ba_token = String(evt.ba_token || next.ba_token || '')
          next.ba_url = String(evt.ba_url || next.ba_url || '')
          const combo = String(evt.region_combo || next.region_combo || '')
          next.region_combo = combo
          next.stage = next.ba_token ? `成功 ${next.ba_token}${combo ? ` · ${combo}` : ''}` : '提取成功'
          next.error = ''
          pushLog(next.stage)
        } else if (evt.cancelled || /终止|取消/.test(String(evt.error || ''))) {
          next.status = 'cancelled'
          next.error = String(evt.error || '任务已终止')
          next.stage = next.error
          pushLog(`已终止: ${next.error}`)
        } else {
          next.status = 'error'
          next.error = String(evt.error || '提取失败')
          next.stage = next.error
          pushLog(`失败: ${next.error}`)
        }
      } else if (type === 'error') {
        next.status = 'error'
        next.error = String(evt.error || '错误')
        next.stage = next.error
        pushLog(`错误: ${next.error}`)
      }
      return { ...prev, [key]: next }
    })
  }, [])

  const subscribeBaExtractTask = useCallback((accountId: number) => {
    if (!Number.isFinite(accountId) || accountId <= 0) return
    const key = String(accountId)
    baExtractTaskStreamRefs.current[key]?.abort()
    const controller = new AbortController()
    baExtractTaskStreamRefs.current[key] = controller
    ;(async () => {
      try {
        const token = getAuthTokenForStream()
        const res = await fetch(`${API_BASE}/pp-plus/accounts/${accountId}/extract-ba-events`, {
          method: 'GET',
          headers: token ? { Authorization: `Bearer ${token}` } : {},
          signal: controller.signal,
        })
        if (!res.ok || !res.body) throw new Error(`BA链任务事件订阅失败 HTTP ${res.status}`)
        const reader = res.body.getReader()
        const decoder = new TextDecoder('utf-8')
        let buffer = ''
        while (true) {
          const { done, value } = await reader.read()
          if (done) break
          buffer += decoder.decode(value, { stream: true })
          const chunks = buffer.split(/\n\n/)
          buffer = chunks.pop() || ''
          for (const chunk of chunks) {
            for (const line of chunk.split(/\r?\n/)) {
              if (!line.startsWith('data:')) continue
              const raw = line.slice(5).trim()
              if (!raw) continue
              try {
                applyBaExtractEvent(accountId, JSON.parse(raw))
              } catch {
                // ignore malformed SSE line
              }
            }
          }
        }
      } catch (exc: any) {
        if (exc?.name !== 'AbortError') {
          applyBaExtractEvent(accountId, { type: 'error', error: exc?.message || 'BA链任务事件订阅失败' })
        }
      } finally {
        if (baExtractTaskStreamRefs.current[key] === controller) {
          delete baExtractTaskStreamRefs.current[key]
        }
      }
    })()
  }, [applyBaExtractEvent])

  useEffect(() => {
    for (const acc of accounts) {
      const accountId = Number(acc?.id)
      if (!Number.isFinite(accountId) || accountId <= 0) continue
      const key = String(accountId)
      const task = getAccountBaExtractTask(acc, baExtractTasks[key] || null)
      if (task.task_id && isBaExtractTaskRunning(task) && !baExtractTaskStreamRefs.current[key]) {
        subscribeBaExtractTask(accountId)
      }
    }
  }, [accounts, baExtractTasks, subscribeBaExtractTask])

  useEffect(() => {
    const timer = window.setInterval(() => {
      for (const acc of accounts) {
        const accountId = Number(acc?.id)
        if (!Number.isFinite(accountId) || accountId <= 0) continue
        const task = getAccountBaExtractTask(acc, baExtractTasks[String(accountId)] || null)
        if (!task.task_id || !isBaExtractTaskRunning(task)) continue
        apiFetch(`/pp-plus/accounts/${accountId}/extract-ba-task`)
          .then((data) => {
            if (data?.task) applyBaExtractEvent(accountId, { type: 'snapshot', ...data.task })
          })
          .catch(() => {})
      }
    }, 3000)
    return () => window.clearInterval(timer)
  }, [accounts, applyBaExtractEvent, baExtractTasks])

  const openBaExtractLogs = useCallback((accountId: number) => {
    const id = Number(accountId)
    if (!Number.isFinite(id) || id <= 0) return
    setBaExtractLogTaskId(id)
    const key = String(id)
    // 打开日志时若尚未订阅，立即接上 SSE（运行中可持续更新；结束任务也能拿到快照）
    if (!baExtractTaskStreamRefs.current[key]) {
      subscribeBaExtractTask(id)
    }
  }, [subscribeBaExtractTask])


  const stopBaExtractTask = useCallback(async (accountId?: number) => {
    const id = Number(accountId || baExtractLogTaskId || 0)
    if (!Number.isFinite(id) || id <= 0) return
    setBaExtractStopping(true)
    applyBaExtractEvent(id, {
      type: 'done',
      cancelled: true,
      error: '任务已终止',
      ok: false,
    })
    const controller = new AbortController()
    const timeout = window.setTimeout(() => controller.abort(), 5000)
    try {
      const data = await apiFetch(`/pp-plus/accounts/${id}/extract-ba-task/cancel`, {
        method: 'POST',
        body: '{}',
        signal: controller.signal,
      })
      const task = data?.task
      if (task && typeof task === 'object') {
        applyBaExtractEvent(id, {
          type: task.status === 'cancelled' ? 'done' : 'progress',
          cancelled: task.status === 'cancelled',
          status: task.status,
          desc: task.stage || (task.status === 'cancelled' ? '任务已终止' : '正在终止任务...'),
          step: task.step,
          total: task.total,
          attempt: task.attempt,
          error: task.error,
          ok: false,
        })
      } else {
        applyBaExtractEvent(id, { type: 'progress', desc: '正在终止任务...', step: 0, total: 7 })
      }
      // 确保继续吃 SSE，直到 worker 收口
      if (!baExtractTaskStreamRefs.current[String(id)]) {
        subscribeBaExtractTask(id)
      }
    } catch (exc: any) {
      if (exc?.name !== 'AbortError') {
        setError(exc?.message || '终止 BA 链任务失败')
      }
    } finally {
      window.clearTimeout(timeout)
      setBaExtractStopping(false)
    }
  }, [applyBaExtractEvent, baExtractLogTaskId, subscribeBaExtractTask])


  const startBaExtractForAccount = useCallback(async (acc: any, form: Record<string, string>) => {
    const accountId = Number(acc?.id)
    if (!Number.isFinite(accountId) || accountId <= 0) return
    const billingProxy = String(form.billing_proxy || '').trim()
    const promoProxy = String(form.promo_proxy || '').trim()
    if (!billingProxy || !promoProxy) {
      setError('请填写账单IP和优惠IP代理池')
      return
    }
    const billingCountry = inferRegionFromProxyText(billingProxy, form.billing_country || 'US') || 'US'
    const promoCountry = inferRegionFromProxyText(promoProxy, form.promo_country || 'TR') || 'TR'
    const billingCurrency = baExtractCurrencyForCountry(billingCountry)
    const conf = {
      billing_proxy: billingProxy,
      promo_proxy: promoProxy,
      billing_country: billingCountry,
      promo_country: promoCountry,
      billing_currency: String(form.billing_currency || billingCurrency || 'USD').toUpperCase(),
      confirm_mode: String(form.confirm_mode || 'pm'),
      promo_create_mode: String(form.promo_create_mode || 'update_after_checkout'),
      max_attempts: String(form.max_attempts || '20'),
    }
    saveBaExtractSettings(conf)
    setBaExtractForm(current => ({ ...current, ...conf }))
    setError('')
    setBaExtractRunning(true)

    // 立刻打开日志框 + 清空本地旧日志（后端 force 重启也会清）
    const key = String(accountId)
    baExtractTaskStreamRefs.current[key]?.abort()
    delete baExtractTaskStreamRefs.current[key]
    setBaExtractTasks(prev => ({
      ...prev,
      [key]: {
        account_id: accountId,
        email: String(acc?.email || ''),
        task_id: '',
        status: 'queued',
        stage: '任务已提交',
        step: 0,
        total: 7,
        attempt: 0,
        max_attempts: Number(conf.max_attempts || 20),
        ba_token: '',
        ba_url: '',
        region_combo: `${conf.billing_country}+${conf.promo_country}`,
        error: '',
        logs: [withBaExtractLogTime('任务已提交')],
        updated_at: Date.now() / 1000,
      },
    }))
    setBaExtractLogTaskId(accountId)
    setBaExtractAccount(null)

    try {
      const data = await apiFetch(`/pp-plus/accounts/${accountId}/extract-ba-task`, {
        method: 'POST',
        body: JSON.stringify({
          billing_proxy: conf.billing_proxy,
          promo_proxy: conf.promo_proxy,
          billing_country: conf.billing_country,
          promo_country: conf.promo_country,
          billing_currency: conf.billing_currency,
          confirm_mode: conf.confirm_mode,
          promo_create_mode: conf.promo_create_mode,
          max_attempts: Number(conf.max_attempts || 20),
          force: true,
        }),
      })
      if (data?.task) {
        setBaExtractTasks(prev => ({
          ...prev,
          [key]: {
            ...getAccountBaExtractTask(acc, prev[key] || null),
            ...(data.task || {}),
            // 以后端返回为准；若后端带回旧 logs 仍用返回值（force 后应为空/新日志）
            logs: Array.isArray(data.task?.logs) ? data.task.logs : (prev[key]?.logs || []),
          },
        }))
      }
      subscribeBaExtractTask(accountId)
      setBaExtractLogTaskId(accountId)
    } catch (exc: any) {
      const msg = exc?.message || 'BA链任务启动失败'
      setError(msg)
      applyBaExtractEvent(accountId, { type: 'error', error: msg })
      setBaExtractLogTaskId(accountId)
    } finally {
      setBaExtractRunning(false)
    }
  }, [applyBaExtractEvent, subscribeBaExtractTask])

  const clearPpBaToken = useCallback(async (acc: any) => {
    const accountId = Number(acc?.id)
    if (!Number.isFinite(accountId) || accountId <= 0) return
    if (!confirm(`确认清除该账号的 BA 链？\n${acc?.email || ''}`)) return
    setError('')
    try {
      await apiFetch(`/pp-plus/accounts/${accountId}/ba-token`, { method: 'DELETE' })
      setBaExtractTasks(prev => {
        const key = String(accountId)
        const current = prev[key]
        if (!current) return prev
        return {
          ...prev,
          [key]: {
            ...current,
            ba_token: '',
          },
        }
      })
      await load()
    } catch (exc: any) {
      setError(exc?.message || '清除 BA 链失败')
    }
  }, [load])


  useEffect(() => {
    return () => {
      Object.values(baExtractTaskStreamRefs.current).forEach(controller => controller.abort())
      baExtractTaskStreamRefs.current = {}
    }
  }, [])

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

  const togglePasswordVisible = useCallback((accountId: number) => {
    setVisiblePasswordIds(prev => {
      const next = new Set(prev)
      if (next.has(accountId)) next.delete(accountId)
      else next.add(accountId)
      return next
    })
  }, [])

  const showTotpCodeForAccount = async (acc: any) => {
    const accountId = Number(acc?.id)
    if (!Number.isFinite(accountId) || accountId <= 0) return
    setError('')
    try {
      const data = await apiFetch(`/accounts/${accountId}/totp-code`)
      const code = String(data?.code || '').trim()
      if (!code) {
        setError('未生成2FA验证码')
        return
      }
      let copied = false
      try {
        await writeClipboardText(code)
        copied = true
      } catch {
        // Clipboard is best-effort; the dialog still shows the code.
      }
      const period = Math.max(1, Number(data?.period || 30))
      const generatedAt = Math.max(0, Number(data?.generated_at || Math.floor(Date.now() / 1000)))
      const serverOffsetMs = generatedAt * 1000 - Date.now()
      const serverNow = Math.floor((Date.now() + serverOffsetMs) / 1000)
      const remain = Math.max(1, Number(data?.valid_for_seconds || (period - (serverNow % period))))
      setTotpDialog({
        accountId,
        email: String(acc?.email || ''),
        code,
        remain,
        copied,
        period,
        generatedAt,
        serverOffsetMs,
        windowIndex: Math.floor(serverNow / period),
      })
    } catch (exc: any) {
      setError(exc?.message || t('login.requestFailed'))
    }
  }

  useEffect(() => {
    if (!totpDialog) return
    const accountId = totpDialog.accountId
    const period = Math.max(1, Number(totpDialog.period || 30))
    const serverOffsetMs = Number(totpDialog.serverOffsetMs || 0)
    const refreshCode = async () => {
      if (totpRefreshInFlightRef.current) return
      totpRefreshInFlightRef.current = true
      try {
        const data = await apiFetch(`/accounts/${accountId}/totp-code`)
        const nextCode = String(data?.code || '').trim()
        if (!nextCode) return
        const nextPeriod = Math.max(1, Number(data?.period || period || 30))
        const generatedAt = Math.max(0, Number(data?.generated_at || Math.floor(Date.now() / 1000)))
        const nextOffsetMs = generatedAt * 1000 - Date.now()
        const serverNow = Math.floor((Date.now() + nextOffsetMs) / 1000)
        setTotpDialog(current => {
          if (!current || current.accountId !== accountId) return current
          return {
            ...current,
            code: nextCode,
            copied: nextCode === current.code ? current.copied : false,
            period: nextPeriod,
            generatedAt,
            serverOffsetMs: nextOffsetMs,
            remain: Math.max(1, Number(data?.valid_for_seconds || (nextPeriod - (serverNow % nextPeriod)))),
            windowIndex: Math.floor(serverNow / nextPeriod),
          }
        })
      } catch (exc: any) {
        setError(exc?.message || '刷新2FA验证码失败')
      } finally {
        totpRefreshInFlightRef.current = false
      }
    }

    const tick = () => {
      const serverNow = Math.floor((Date.now() + serverOffsetMs) / 1000)
      const nextRemain = Math.max(1, period - (serverNow % period))
      const nextWindowIndex = Math.floor(serverNow / period)
      setTotpDialog(current => {
        if (!current || current.accountId !== accountId) return current
        if (current.remain === nextRemain && current.windowIndex === nextWindowIndex) return current
        return { ...current, remain: nextRemain, windowIndex: nextWindowIndex }
      })
      if (nextWindowIndex > totpDialog.windowIndex) {
        refreshCode()
      }
    }

    tick()
    const timer = window.setInterval(tick, 1000)
    return () => window.clearInterval(timer)
  }, [totpDialog?.accountId, totpDialog?.period, totpDialog?.serverOffsetMs, totpDialog?.windowIndex])

  const copyTotpDialogCode = async () => {
    if (!totpDialog?.code) return
    try {
      await writeClipboardText(totpDialog.code)
      setTotpDialog(current => current ? { ...current, copied: true } : current)
    } catch {
      setError('复制2FA验证码失败')
    }
  }

  const copyAccessToken = async (acc: any, token: string) => {
    const accountId = Number(acc.id)
    if (!token || copyingAccessTokenId === accountId) return
    const overview = { ...getAccountOverview(acc) }
    const nextCount = getAccessTokenCopyCount(acc) + 1
    setCopyingAccessTokenId(accountId)
    setError('')
    try {
      copy(ensureTrailingNewline(token))
      overview.access_token_copy_count = nextCount
      const updated = await apiFetch(`/accounts/${accountId}`, {
        method: 'PATCH',
        body: JSON.stringify({ overview }),
      })
      setAccounts(items => items.map(item => Number(item.id) === accountId ? updated : item))
      setDetail((current: any | null) => Number(current?.id) === accountId ? updated : current)
    } catch (exc: any) {
      setError(`AT 已复制，但复制次数保存失败: ${exc?.message || exc}`)
    } finally {
      setCopyingAccessTokenId(null)
    }
  }

  const copyChatgptAtCookie = async (acc: any, text: string) => {
    const accountId = Number(acc.id)
    if (!text || copyingAccessTokenId === accountId) return
    const overview = { ...getAccountOverview(acc) }
    const nextCount = getAccessTokenCopyCount(acc) + 1
    setCopyingAccessTokenId(accountId)
    setError('')
    try {
      await writeClipboardText(ensureTrailingNewline(text))
    } catch {
      setError('复制AT|cookie失败')
      setCopyingAccessTokenId(null)
      return
    }
    try {
      overview.access_token_copy_count = nextCount
      const updated = await apiFetch(`/accounts/${accountId}`, {
        method: 'PATCH',
        body: JSON.stringify({ overview }),
      })
      setAccounts(items => items.map(item => Number(item.id) === accountId ? updated : item))
      setDetail((current: any | null) => Number(current?.id) === accountId ? updated : current)
    } catch (exc: any) {
      setError(`AT|cookie 已复制，但复制次数保存失败: ${exc?.message || exc}`)
    } finally {
      setCopyingAccessTokenId(null)
    }
  }
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

  const runInlineAccountAction = async (
    acc: any,
    actionId: string,
    title: string,
    params: Record<string, any> = {},
  ) => {
    const busyKey = `${acc.id}:${actionId}`
    setError('')
    setRowActionBusy(busyKey)
    try {
      const resp = await apiFetch(`/actions/${acc.platform}/${acc.id}/${actionId}`, {
        method: 'POST',
        body: JSON.stringify({ params }),
      })
      if (resp?.sync) {
        if (!resp.ok) {
          throw new Error(resp.error || t('accounts.operationFailed'))
        }
        const data = resp.data
        const actionUrl = data?.url || data?.checkout_url || data?.cashier_url
        if (actionUrl) {
          try {
            await writeClipboardText(actionUrl)
          } catch {
            // Clipboard is best-effort here; the result dialog still exposes the URL.
          }
        }
        if (data && typeof data === 'object') {
          setActionResult({ title, payload: data })
        }
        await load()
        return
      }
      const taskId = String(resp?.task_id || resp?.id || '')
      if (taskId) {
        setBatchTask({ taskId, title })
        setBatchTaskStatus(null)
        return
      }
      throw new Error(resp?.error || t('accounts.operationFailed'))
    } catch (exc: any) {
      setError(exc?.message || t('login.requestFailed'))
    } finally {
      setRowActionBusy(current => current === busyKey ? '' : current)
    }
  }

  const runInlineHealthCheck = async (acc: any) => {
    const busyKey = `${acc.id}:health_check`
    setError('')
    setRowActionBusy(busyKey)
    try {
      const res = await apiFetch(`/accounts/health-check?platform=${acc.platform || tab}`, {
        method: 'POST',
        body: JSON.stringify({ ids: [Number(acc.id)] }),
      })
      const taskId = String(res?.task_id || res?.id || '')
      if (taskId) {
        setBatchTask({ taskId, title: `检测存活 - ${acc.email || acc.id}` })
        setBatchTaskStatus(null)
        return
      }
      throw new Error(res?.error || t('accounts.operationFailed'))
    } catch (exc: any) {
      setError(exc?.message || t('login.requestFailed'))
    } finally {
      setRowActionBusy(current => current === busyKey ? '' : current)
    }
  }

  const refreshPlanForAccount = async (acc: any) => {
    const busyKey = `${acc.id}:refresh_plan`
    setError('')
    setRowActionBusy(busyKey)
    try {
      await apiFetch(`/accounts/refresh-plan?platform=${acc.platform || tab}`, {
        method: 'POST',
        body: JSON.stringify({ ids: [Number(acc.id)] }),
      })
      await load()
    } catch (exc: any) {
      setError(exc?.message || t('login.requestFailed'))
    } finally {
      setRowActionBusy(current => current === busyKey ? '' : current)
    }
  }

  const refreshPlanForAllAccounts = async () => {
    setError('')
    setBatchPlanRefreshing(true)
    try {
      const targets = await fetchPlanRefreshTargets()
      setPlanRefreshDialog({
        open: true,
        running: true,
        total: targets.length,
        success: 0,
        failed: 0,
        currentEmail: '',
        logs: targets.map((acc: any) => ({
          id: String(acc.id),
          accountId: Number(acc.id),
          email: String(acc.email || acc.id || ''),
          status: 'pending',
          message: '等待刷新套餐',
        })),
      })
      for (const acc of targets) {
        const accountId = Number(acc.id)
        const email = String(acc.email || accountId)
        setPlanRefreshDialog(current => ({
          ...current,
          currentEmail: email,
          logs: current.logs.map(item => item.accountId === accountId ? { ...item, status: 'running', message: '正在请求 /accounts/refresh-plan' } : item),
        }))
        try {
          const response = await apiFetch(`/accounts/refresh-plan?platform=${acc.platform || tab}`, {
            method: 'POST',
            body: JSON.stringify({ ids: [accountId] }),
          })
          const item = Array.isArray(response?.items) ? response.items.find((entry: any) => Number(entry?.account_id) === accountId) || response.items[0] : null
          const ok = Boolean(item?.ok)
          const planName = String(item?.plan_name || '')
          const planState = String(item?.plan_state || '')
          const subscriptionStatus = String(item?.subscription_status || '')
          const usagePlanType = String(item?.usage_plan_type || '')
          setPlanRefreshDialog(current => ({
            ...current,
            success: current.success + (ok ? 1 : 0),
            failed: current.failed + (ok ? 0 : 1),
            logs: current.logs.map(logItem => logItem.accountId === accountId ? {
              ...logItem,
              status: ok ? 'success' : 'error',
              message: ok
                ? `接口返回套餐 ${planName || subscriptionStatus || usagePlanType || planState || '-'}`
                : `接口返回失败：${item?.error || response?.error || 'unknown'}`,
              planName,
              planState,
              subscriptionStatus,
              usagePlanType,
              raw: response,
            } : logItem),
          }))
        } catch (exc: any) {
          setPlanRefreshDialog(current => ({
            ...current,
            failed: current.failed + 1,
            logs: current.logs.map(item => item.accountId === accountId ? {
              ...item,
              status: 'error',
              message: exc?.message || t('login.requestFailed'),
              raw: { error: exc?.message || String(exc || '') },
            } : item),
          }))
        }
      }
      setPlanRefreshDialog(current => ({ ...current, running: false, currentEmail: '' }))
      await load()
    } catch (exc: any) {
      setError(exc?.message || t('login.requestFailed'))
      setPlanRefreshDialog(current => ({ ...current, running: false, currentEmail: '' }))
    } finally {
      setBatchPlanRefreshing(false)
    }
  }

  const startAgentsUploadSub2Api = async () => {
    setError('')
    setAgentsUploadBusy(true)
    try {
      const ids = [...selectedIds].map(Number)
      const data = await apiFetch('/tasks/agents-upload-sub2api', {
        method: 'POST',
        body: JSON.stringify({
          platform: 'chatgpt',
          ids,
          batch_size: 10,
          verify_task: true,
          timeout: 30,
        }),
      })
      const taskId = String(data?.task_id || data?.id || '')
      if (taskId) {
        setBatchTask({ taskId, title: 'Agents上传到Sub2Api' })
        setBatchTaskStatus(null)
      }
    } catch (exc: any) {
      setError(exc?.message || t('login.requestFailed'))
    } finally {
      setAgentsUploadBusy(false)
    }
  }

  const startMomoTrialProbe = async () => {
    setOauthBusy(true)
    setError('')
    try {
      // 有勾选则检测勾选；无勾选则传空 ids，后端按 platform 检测全部
      const ids = selectedIds.size > 0 ? [...selectedIds].map(Number) : []
      const data = await apiFetch('/tasks/momo-trial-probe', {
        method: 'POST',
        body: JSON.stringify({
          ids,
          platform: tab || 'chatgpt',
          concurrency: Math.max(1, Math.min(Number(actionConcurrency) || 3, 10)),
        }),
      })
      if (data.task_id) {
        setOauthTaskId(data.task_id)
        setOauthConfirmOpen(false)
      }
    } catch (e: any) {
      setError(e.message)
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
          ? '目标模式会处理已选中且没有 RT 的账号；已获取 RT、未上传账号会重试上传。'
          : '获取rt 会处理已选中且没有 RT 的账号；已有 RT 的账号会跳过。',
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
      setError('获取rt 会处理已选中且没有 RT 的账号；已有 RT 的账号会跳过。')
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

  const startRefreshSession = async () => {
    setError('')
    const ids = [...selectedIds].map(Number)
    setRefreshSessionBusy(true)
    try {
      const data = await apiFetch('/tasks/refresh-session', {
        method: 'POST',
        body: JSON.stringify({
          platform: 'chatgpt',
          ids,
          concurrency: Math.max(Number(actionConcurrency || 1), 1),
          default_status: 'relogin_required',
        }),
      })
      setRefreshSessionTaskId(String(data?.task_id || data?.id || ''))
    } catch (exc: any) {
      setError(exc?.message || t('login.requestFailed'))
    } finally {
      setRefreshSessionBusy(false)
    }
  }

  const handleRefreshSessionTaskDone = useCallback(async () => {
    setRefreshSessionBusy(false)
    setSelectedIds(new Set())
    await load()
  }, [load])

  const startBatchSecuritySetup = async () => {
    setError('')
    setBatchSecurityBusy(true)
    let submitted = false
    try {
      const ids = selectedIds.size > 0 ? [...selectedIds].map(Number) : []
      const data = await apiFetch('/tasks/batch-security-setup', {
        method: 'POST',
        body: JSON.stringify({
          platform: 'chatgpt',
          ids,
          concurrency: Math.max(1, Math.min(Number(actionConcurrency) || 1, 5)),
        }),
      })
      const taskId = String(data?.task_id || data?.id || '')
      if (!taskId) {
        throw new Error('任务创建失败')
      }
      submitted = true
      setBatchTask({ taskId, title: '批量设置密码/2FA' })
      setBatchTaskStatus(null)
    } catch (exc: any) {
      setError(exc?.message || t('login.requestFailed'))
    } finally {
      if (!submitted) setBatchSecurityBusy(false)
    }
  }

  const deleteInvalidAndBanned = async () => {
    setError('')
    if (!confirm('确定要删除当前平台下所有失效和已封禁账号吗？此操作不可恢复。')) {
      return
    }
    setInvalidDeleting(true)
    try {
      await apiFetch(`/accounts/invalid-and-banned?platform=${encodeURIComponent(tab)}`, {
        method: 'DELETE',
      })
      setSelectedIds(new Set())
      setPage(1)
      await load(tab, debouncedSearch, filterStatus, filterTag, 1)
    } catch (exc: any) {
      setError(exc?.message || t('login.requestFailed'))
    } finally {
      setInvalidDeleting(false)
    }
  }

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
      {showPpPlusSettings && (
        <PpPlusSettingsDialog
          open={showPpPlusSettings}
          selectedAccountIds={[...selectedIds].map(Number)}
          onClose={() => setShowPpPlusSettings(false)}
          onStarted={(status) => {
            setPpPlusStatus(status)
            load()
          }}
        />
      )}
      {ppBaAccount && (
        <PpBaTokenDialog
          open={Boolean(ppBaAccount)}
          account={ppBaAccount}
          onClose={() => setPpBaAccount(null)}
          onSaved={() => load()}
        />
      )}
      {baExtractAccount && (
        <div className="dialog-backdrop" onClick={() => { if (!baExtractRunning) setBaExtractAccount(null) }}>
          <div
            className="w-[min(760px,96vw)] max-h-[92vh] overflow-auto rounded-2xl border border-[var(--border)] bg-[var(--bg-base)] p-5 shadow-2xl"
            onClick={e => e.stopPropagation()}
          >
            <div className="mb-4 flex items-start justify-between gap-3">
              <div className="min-w-0">
                <h2 className="text-base font-semibold text-[var(--text-primary)]">提取BA链</h2>
                <p className="mt-1 truncate text-xs text-[var(--text-muted)]">{baExtractAccount.email}</p>
                <p className="mt-1 text-xs leading-5 text-[var(--text-muted)]">
                  账单/优惠代理池每行一个代理，格式与代理池一致（支持 host:port:user:pass / user:pass@host:port / URL / host:port##user##pass）。国家根据代理自动识别。
                </p>
              </div>
              <button
                type="button"
                onClick={() => {
                  setBaExtractAccount(null)
                }}
                className="rounded-lg p-1 text-[var(--text-muted)] transition-colors hover:bg-[var(--bg-pane)] hover:text-[var(--text-primary)]"
                aria-label="关闭"
              >
                <X className="h-4 w-4" />
              </button>
            </div>

            <div className="space-y-4">
              <div className="grid gap-3 lg:grid-cols-2">
                <label className="space-y-1 text-xs">
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-[var(--text-secondary)]">代理池 - 账单IP</span>
                    <span className="text-[10px] text-[var(--text-muted)]">
                      {splitProxyPoolLines(baExtractForm.billing_proxy || '').length} 条 · 地区 {inferRegionFromProxyText(baExtractForm.billing_proxy || '', baExtractForm.billing_country || 'US') || '-'}
                    </span>
                  </div>
                  <textarea
                    rows={7}
                    className="w-full resize-y rounded-lg border border-[var(--border)] bg-[var(--bg-input)] px-3 py-2 font-mono text-xs leading-5 text-[var(--text-primary)] outline-none transition focus:border-[var(--accent)]"
                    value={baExtractForm.billing_proxy || ''}
                    disabled={baExtractRunning}
                    onChange={e => {
                      const value = e.target.value
                      const region = inferRegionFromProxyText(value, baExtractForm.billing_country || 'US')
                      setBaExtractForm(current => ({
                        ...current,
                        billing_proxy: value,
                        billing_country: region || current.billing_country || 'US',
                        billing_currency: baExtractCurrencyForCountry(region || current.billing_country || 'US'),
                      }))
                    }}
                    placeholder={"gate.kookeey.info:1000:user:pass-US-xxxx-5m\n每行一个代理，不带协议头默认 http://"}
                  />
                </label>
                <label className="space-y-1 text-xs">
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-[var(--text-secondary)]">代理池 - 优惠IP</span>
                    <span className="text-[10px] text-[var(--text-muted)]">
                      {splitProxyPoolLines(baExtractForm.promo_proxy || '').length} 条 · 地区 {inferRegionFromProxyText(baExtractForm.promo_proxy || '', baExtractForm.promo_country || 'TR') || '-'}
                    </span>
                  </div>
                  <textarea
                    rows={7}
                    className="w-full resize-y rounded-lg border border-[var(--border)] bg-[var(--bg-input)] px-3 py-2 font-mono text-xs leading-5 text-[var(--text-primary)] outline-none transition focus:border-[var(--accent)]"
                    value={baExtractForm.promo_proxy || ''}
                    disabled={baExtractRunning}
                    onChange={e => {
                      const value = e.target.value
                      const region = inferRegionFromProxyText(value, baExtractForm.promo_country || 'TR')
                      setBaExtractForm(current => ({
                        ...current,
                        promo_proxy: value,
                        promo_country: region || current.promo_country || 'TR',
                      }))
                    }}
                    placeholder={"gate.kookeey.info:1000:user:pass-TR-xxxx-5m\n每行一个代理，不带协议头默认 http://"}
                  />
                </label>
              </div>

              <div className="rounded-xl border border-[var(--border-soft)] bg-[var(--bg-pane)]/40 p-3">
                <div className="mb-2 text-[11px] font-medium tracking-wide text-[var(--text-secondary)]">识别结果 / 高级参数</div>
                <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
                  <label className="space-y-1 text-xs">
                    <span className="text-[var(--text-secondary)]">账单资料国家（自动）</span>
                    <input
                      className="h-9 w-full rounded-lg border border-[var(--border)] bg-[var(--bg-input)] px-3 text-sm"
                      value={baExtractForm.billing_country || ''}
                      disabled={baExtractRunning}
                      onChange={e => {
                        const country = e.target.value.toUpperCase()
                        setBaExtractForm(current => ({ ...current, billing_country: country, billing_currency: baExtractCurrencyForCountry(country) }))
                      }}
                    />
                  </label>
                  <label className="space-y-1 text-xs">
                    <span className="text-[var(--text-secondary)]">优惠国家（自动）</span>
                    <input
                      className="h-9 w-full rounded-lg border border-[var(--border)] bg-[var(--bg-input)] px-3 text-sm"
                      value={baExtractForm.promo_country || ''}
                      disabled={baExtractRunning}
                      onChange={e => setBaExtractForm(current => ({ ...current, promo_country: e.target.value.toUpperCase() }))}
                    />
                  </label>
                  <label className="space-y-1 text-xs">
                    <span className="text-[var(--text-secondary)]">账单币种</span>
                    <input
                      className="h-9 w-full rounded-lg border border-[var(--border)] bg-[var(--bg-input)] px-3 text-sm"
                      value={baExtractForm.billing_currency || ''}
                      disabled={baExtractRunning}
                      onChange={e => setBaExtractForm(current => ({ ...current, billing_currency: e.target.value.toUpperCase() }))}
                      placeholder="USD"
                    />
                  </label>
                  <label className="space-y-1 text-xs">
                    <span className="text-[var(--text-secondary)]">最大重试</span>
                    <input
                      type="number"
                      min={1}
                      max={50}
                      className="h-9 w-full rounded-lg border border-[var(--border)] bg-[var(--bg-input)] px-3 text-sm"
                      value={baExtractForm.max_attempts || '20'}
                      disabled={baExtractRunning}
                      onChange={e => setBaExtractForm(current => ({ ...current, max_attempts: e.target.value }))}
                    />
                  </label>
                </div>
                <div className="mt-3 grid gap-3 sm:grid-cols-2">
                  <label className="space-y-1 text-xs">
                    <span className="text-[var(--text-secondary)]">Confirm 模式</span>
                    <select
                      className="h-9 w-full rounded-lg border border-[var(--border)] bg-[var(--bg-input)] px-3 text-sm"
                      value={baExtractForm.confirm_mode || 'pm'}
                      disabled={baExtractRunning}
                      onChange={e => setBaExtractForm(current => ({ ...current, confirm_mode: e.target.value }))}
                    >
                      <option value="pm">pm（PaymentMethod）</option>
                      <option value="direct">direct（直连 confirm）</option>
                    </select>
                  </label>
                  <label className="space-y-1 text-xs">
                    <span className="text-[var(--text-secondary)]">优惠应用模式</span>
                    <select
                      className="h-9 w-full rounded-lg border border-[var(--border)] bg-[var(--bg-input)] px-3 text-sm"
                      value={baExtractForm.promo_create_mode || 'update_after_checkout'}
                      disabled={baExtractRunning}
                      onChange={e => setBaExtractForm(current => ({ ...current, promo_create_mode: e.target.value }))}
                    >
                      <option value="update_after_checkout">后置 update（默认）</option>
                      <option value="create_with_promo">创建时带 promo</option>
                    </select>
                  </label>
                  <div className="rounded-lg border border-dashed border-[var(--border-soft)] bg-[var(--bg-base)]/40 px-3 py-2 text-[11px] leading-5 text-[var(--text-muted)]">
                    代理池格式支持：host:port:user:pass、user:pass@host:port、http(s)/socks5 URL、host:port##user##pass。不带协议默认 http://。多行代理按重试次数顺序轮换。
                  </div>
                </div>
              </div>

              <div className="rounded-xl border border-[var(--border-soft)] bg-[var(--bg-pane)]/30 p-3 text-xs leading-5 text-[var(--text-muted)]">
                点击“开始提取”后会创建后台任务并自动打开日志查看框；列表“BA链任务”列也会同步显示当前步骤。每个账号使用独立任务，互不影响。点击任务格或“日志”可再次打开日志。
              </div>

            <div className="mt-5 flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
              <Button
                type="button"
                variant="outline"
                className="h-9 min-w-[104px]"
                disabled={baExtractRunning}
                onClick={() => setBaExtractAccount(null)}
              >
                取消
              </Button>
              <Button
                type="button"
                className="h-9 min-w-[120px]"
                disabled={baExtractRunning}
                onClick={() => startBaExtractForAccount(baExtractAccount, baExtractForm)}
              >
                {baExtractRunning ? '启动中...' : '开始提取'}
              </Button>
            </div>
            </div>
          </div>
        </div>
      )}
      {ppLogTask && (
        <PpTaskLogDialog
          open={Boolean(ppLogTask)}
          task={ppLogTask}
          onClose={() => setPpLogTask(null)}
        />
      )}
      {baExtractLogTask && (
        <BaExtractTaskLogDialog
          open={Boolean(baExtractLogTask)}
          task={baExtractLogTask}
          onClose={() => setBaExtractLogTaskId(null)}
          onStop={() => stopBaExtractTask(Number(baExtractLogTask.account_id || baExtractLogTaskId || 0))}
          stopping={baExtractStopping || String(baExtractLogTask.status || '').toLowerCase() === 'cancelling'}
        />
      )}
      <PpPlusFloatingWidget
        status={ppPlusStatus}
        onStop={async () => {
          const status = await apiFetch('/pp-plus/stop', { method: 'POST', body: '{}' })
          setPpPlusStatus(status)
        }}
        onOpenLogs={(task) => setPpLogTask(task)}
      />
      {showRegister && <RegisterModal platform={tab} platformMeta={platformsMap[tab]} onClose={() => setShowRegister(false)} onDone={() => load()} />}
      {actionResult && <ActionResultModal title={actionResult.title} payload={actionResult.payload} onClose={() => setActionResult(null)} />}
      {totpDialog && (
        <TotpCodeDialog
          account={{ email: totpDialog.email }}
          code={totpDialog.code}
          remain={totpDialog.remain}
          copied={totpDialog.copied}
          onClose={() => setTotpDialog(null)}
          onCopy={copyTotpDialogCode}
        />
      )}
      {planRefreshDialog.open && (
        <PlanRefreshLogDialog
          state={planRefreshDialog}
          onClose={() => setPlanRefreshDialog(current => ({ ...current, open: false }))}
        />
      )}
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
            setAgentsUploadBusy(false)
            setBatchSecurityBusy(false)
            load()
          }}
          onDone={(status) => {
            setBatchTaskStatus(status)
            setBatchRefreshing(false)
            setBatchHealthChecking(false)
            setAgentsUploadBusy(false)
            setBatchSecurityBusy(false)
            load()
          }}
        />
      )}
      {oauthTaskId && (
        <SimpleTaskLogDialog
          open={Boolean(oauthTaskId)}
          title="检测MOMO试用资格"
          subtitle="后台批量检测 · 同时具备试用和 MoMo 时打标签「MOMO试用」"
          taskId={oauthTaskId}
          showMomoTrialStats
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
                    检测MOMO试用资格
                  </h2>
                  <div className="mt-1 text-xs help-text">
                    {selectedIds.size > 0 ? t('accounts.selected', { count: selectedIds.size }) : '未勾选：将检测当前平台全部账号'}
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
                <p className="text-sm text-[var(--text-secondary)]">
                  {selectedIds.size > 0
                    ? `将检测已勾选的 ${selectedIds.size} 个账号。`
                    : '未勾选账号，将检测当前平台全部账号。'}
                  任务在后台运行；检测到同时具备试用资格和 MoMo 支付方式时自动打标签「MOMO试用」。
                </p>
                <div>
                  <label className="mb-1 block text-xs font-medium text-[var(--text-secondary)]">
                    并发线程数（1-10）
                  </label>
                  <input
                    type="number"
                    min={1}
                    max={10}
                    value={actionConcurrency}
                    onChange={event =>
                      setActionConcurrency(Math.max(1, Math.min(10, Number(event.target.value || 1))))
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
                    await startMomoTrialProbe()
                  }}
                  disabled={oauthBusy}
                >
                  {oauthBusy ? (
                    <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                  ) : (
                    <ShieldCheck className="mr-2 h-4 w-4" />
                  )}
                  开始检测
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
                    已选 {selectedIds.size} 个账户；将处理没有 RT 的账号，已有 RT 的账号会跳过。
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
      {refreshSessionTaskId && (
        <TaskLogDialog
          title="重新登录获取session/at"
          taskId={refreshSessionTaskId}
          taskStatus={null}
          onClose={() => setRefreshSessionTaskId('')}
          onDone={handleRefreshSessionTaskDone}
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
              {tab === 'chatgpt' && (
                <button onClick={() => setShowPpPlusSettings(true)} className="pb-1 text-[11px] font-medium text-[var(--text-secondary)] transition-colors hover:text-[var(--accent)]">
                  PLUS开通设置
                </button>
              )}
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
                  tagFilter={filterTag}
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

        <section className="w-full max-w-none p-8">
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
                disabled={batchSecurityBusy}
                onClick={startBatchSecuritySetup}
                className="h-10 rounded-lg border-[var(--border-soft)] bg-[var(--bg-card)] px-5 text-[13px] font-medium shadow-[var(--shadow-soft)] hover:bg-[var(--bg-pane)]"
                title={selectedCount > 0 ? `为已勾选的 ${selectedCount} 个账号设置密码和2FA` : '未勾选：处理全部未绑定2FA的账号'}
              >
                {batchSecurityBusy ? (
                  <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                ) : (
                  <ShieldCheck className="mr-2 h-4 w-4" />
                )}
                批量设置密码/2FA
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
                disabled={refreshSessionBusy}
                onClick={startRefreshSession}
                className="h-10 rounded-lg border-[var(--border-soft)] bg-[var(--bg-card)] px-5 text-[13px] font-medium shadow-[var(--shadow-soft)] hover:bg-[var(--bg-pane)]"
              >
                {refreshSessionBusy ? (
                  <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                ) : (
                  <RefreshCw className="mr-2 h-4 w-4" />
                )}
                重新登录获取session/at
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
            {tab === 'chatgpt' ? (
              <Button
                size="sm"
                variant="outline"
                disabled={invalidDeleting || loading}
                onClick={deleteInvalidAndBanned}
                className="h-10 rounded-lg border-red-500 bg-red-500/5 px-5 text-[13px] font-semibold text-red-600 shadow-[var(--shadow-soft)] hover:bg-red-500/10"
              >
                {invalidDeleting ? (
                  <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                ) : (
                  <Trash2 className="mr-2 h-4 w-4" />
                )}
                一键清除所有失效帐号
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
                <select
                  value={filterTag}
                  onChange={e => setFilterTag(e.target.value)}
                  className="h-8 rounded-lg border-0 bg-[var(--bg-pane)] pl-3 pr-8 text-[12px] text-[var(--text-secondary)] outline-none focus:ring-1 focus:ring-[var(--accent)]"
                >
                  <option value="">全部标签</option>
                  {ACCOUNT_TAG_FILTER_OPTIONS.map(tag => (
                    <option key={tag} value={tag}>{tag}</option>
                  ))}
                </select>
                {tab === 'chatgpt' && (
                  <Button
                    variant="outline"
                    size="sm"
                    disabled={agentsUploadBusy || batchPlanRefreshing || batchHealthChecking || batchRefreshing || loading}
                    className="h-8 rounded-lg border-0 bg-blue-500/10 px-3 text-[12px] font-medium text-blue-700 hover:bg-blue-500/15 dark:text-blue-300"
                    title={selectedCount > 0 ? `为已勾选的 ${selectedCount} 个 ChatGPT 账号生成 Agent Identity auth.json 并分批上传到 Sub2Api` : '为所有状态正常的 ChatGPT 账号生成 Agent Identity auth.json 并分批上传到 Sub2Api'}
                    onClick={startAgentsUploadSub2Api}
                  >
                    {agentsUploadBusy ? (
                      <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />
                    ) : (
                      <Upload className="mr-1.5 h-3.5 w-3.5" />
                    )}
                    {agentsUploadBusy ? 'Agents上传中' : 'Agents上传到Sub2Api'}
                  </Button>
                )}
                {tab === 'chatgpt' && (
                  <Button
                    variant="outline"
                    size="sm"
                    disabled={batchPlanRefreshing || agentsUploadBusy || batchHealthChecking || batchRefreshing || loading}
                    className="h-8 rounded-lg border-0 bg-emerald-500/10 px-3 text-[12px] font-medium text-emerald-700 hover:bg-emerald-500/15 dark:text-emerald-300"
                    title="全量刷新当前平台所有账号的订阅套餐类型"
                    onClick={refreshPlanForAllAccounts}
                  >
                    {batchPlanRefreshing ? (
                      <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />
                    ) : (
                      <RefreshCw className="mr-1.5 h-3.5 w-3.5" />
                    )}
                    {batchPlanRefreshing ? '刷新套餐中' : '一键刷新套餐'}
                  </Button>
                )}
                {tab === 'chatgpt' && (
                  <Button
                    variant="outline"
                    size="sm"
                    disabled={batchHealthChecking || batchPlanRefreshing || batchRefreshing || loading}
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
                  disabled={batchRefreshing || batchPlanRefreshing || batchHealthChecking || loading}
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
              <table className="w-full min-w-[1620px] border-collapse text-left text-[12px]">
                <colgroup>
                  <col className="w-[44px]" />
                  <col className="w-[210px]" />
                  <col className="w-[82px]" />
                  <col className="w-[112px]" />
                  <col className="w-[148px]" />
                  <col className="w-[110px]" />
                  <col className="w-[106px]" />
                  {tab === 'chatgpt' ? <col className="w-[88px]" /> : null}
                  <col className="w-[150px]" />
                  <col className="w-[320px]" />
                </colgroup>
                <thead>
                  <tr className="border-y border-[var(--border-soft)] bg-[var(--bg-pane)]/45 text-[12px] text-[var(--text-secondary)]">
                    <th className="sticky left-0 z-30 w-[44px] min-w-[44px] max-w-[44px] bg-[var(--bg-pane)] px-3 py-3 text-center font-semibold">
                      <input
                        type="checkbox"
                        checked={allSelectedOnPage}
                        onChange={togglePage}
                        className="checkbox-accent rounded border-[var(--border)] text-[var(--accent)] focus:ring-[var(--accent)]"
                      />
                    </th>
                    <th className="sticky left-[44px] z-30 w-[210px] min-w-[210px] max-w-[210px] border-r border-[var(--border-soft)] bg-[var(--bg-pane)] px-3 py-3 font-semibold shadow-[8px_0_12px_-10px_rgba(0,0,0,0.28)]">账号</th>
                    <th className="px-3 py-3 font-semibold">套餐</th>
                    <th className="px-3 py-3 font-semibold">账号状态</th>
                    <th className="px-3 py-3 font-semibold">标签</th>
                    <th className="px-3 py-3 font-semibold">有效期</th>
                    <th className="px-3 py-3 font-semibold">BA链任务</th>
                    {tab === 'chatgpt' && (
                      <th className="w-[88px] max-w-[88px] px-2 py-3 font-semibold">任务日志</th>
                    )}
                    <th className="px-3 py-3 font-semibold">创建时间</th>
                    <th className="sticky right-0 z-20 w-[320px] min-w-[320px] max-w-[320px] border-l border-[var(--border-soft)] bg-[var(--bg-pane)] px-2 py-3 text-center font-semibold shadow-[-8px_0_12px_-10px_rgba(0,0,0,0.28)]">
                      {t('common.actions')}
                    </th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-[var(--border-soft)]">
                  {accounts.length === 0 && (
                    <tr>
                      <td colSpan={tab === 'chatgpt' ? 10 : 9} className="px-6 py-20 text-center">
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
                      const primaryToken = getPrimaryToken(acc)
                      const accessTokenCopyCount = getAccessTokenCopyCount(acc)
                      const atCookieText = buildChatgptAtCookieCopyText(acc)
                      const hasAtCookie = Boolean(atCookieText)
                      const planLabel = getAccountPlanLabel(acc)
                      const sessionText = getChatgptSessionText(acc)
                      const hasSessionJson = Boolean(sessionText)
                      const createdLabel = getAccountCreatedAtLabel(acc, language)
                      const validityLabel = getAccountValidityWindowLabel(acc)
                      const status = getDisplayStatus(acc)
                      const statusVariant = String(STATUS_VARIANT[status] || 'secondary')
                      const statusPillClass = (({
                        success: 'border-emerald-500/20 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300',
                        warning: 'border-amber-500/25 bg-amber-500/10 text-amber-700 dark:text-amber-300',
                        danger: 'border-red-500/25 bg-red-500/10 text-red-600 dark:text-red-300',
                        secondary: 'border-[var(--border-soft)] bg-[var(--bg-pane)] text-[var(--text-secondary)]',
                        default: 'border-[var(--accent-edge)] bg-[var(--accent-soft)] text-[var(--accent)]',
                      } as Record<string, string>)[statusVariant]) || 'border-[var(--border-soft)] bg-[var(--bg-pane)] text-[var(--text-secondary)]'
                      const displayBadges = getDisplayBadges(acc)
                      const isBusy = (actionId: string) => rowActionBusy === `${acc.id}:${actionId}`
                      const baTask = resolveBaExtractTask(acc)
                      const baTaskActive = isBaExtractTaskActive(baTask)
                      const ppBaToken = getAccountBaToken(acc)
                      const accountPassword = String(acc?.password || '').trim()
                      const passwordVisible = visiblePasswordIds.has(Number(acc.id))

                      return (
                        <tr
                          key={acc.id}
                          className="group bg-[var(--bg-card)] transition-colors hover:bg-[var(--bg-pane)]/35"
                        >
                          <td
                            className="sticky left-0 z-20 w-[44px] min-w-[44px] max-w-[44px] bg-[var(--bg-card)] px-3 py-3 text-center align-middle group-hover:bg-[var(--bg-pane)]"
                            onClick={e => e.stopPropagation()}
                          >
                            <input
                              type="checkbox"
                              checked={selectedIds.has(acc.id)}
                              onChange={() => toggleOne(acc.id)}
                              className="checkbox-accent rounded border-[var(--border)] text-[var(--accent)] opacity-60 transition-opacity group-hover:opacity-100 focus:ring-[var(--accent)]"
                            />
                          </td>
                          <td className="sticky left-[44px] z-20 w-[210px] min-w-[210px] max-w-[210px] overflow-hidden border-r border-[var(--border-soft)] bg-[var(--bg-card)] px-3 py-3 align-middle shadow-[8px_0_12px_-10px_rgba(0,0,0,0.28)] group-hover:bg-[var(--bg-pane)]">
                            <div className="flex min-w-0 items-center gap-2">
                              <span
                                className="truncate font-mono text-[12px] font-bold tracking-tight text-[var(--text-primary)]"
                                title={acc.email}
                              >
                                {acc.email}
                              </span>
                              <button
                                onClick={e => { e.stopPropagation(); copy(acc.email) }}
                                className="shrink-0 text-[var(--text-muted)] opacity-0 transition-opacity hover:text-[var(--accent)] group-hover:opacity-100"
                                title="复制账号"
                                aria-label="复制账号"
                              >
                                <Copy className="h-3.5 w-3.5" />
                              </button>
                            </div>
                            {accountPassword && (
                              <div className="mt-1.5 flex min-w-0 items-center gap-1.5 font-mono text-[11px] text-[var(--text-muted)]">
                                <span className="shrink-0 text-[10px] font-semibold tracking-wide">密码</span>
                                <span
                                  className={cn(
                                    'min-w-0 flex-1 truncate transition-all duration-150',
                                    passwordVisible
                                      ? 'select-text text-[var(--text-secondary)] blur-0'
                                      : 'select-none text-[var(--text-muted)] blur-[4px]',
                                  )}
                                  title={passwordVisible ? accountPassword : '点击图标显示密码'}
                                >
                                  {accountPassword}
                                </span>
                                <button
                                  type="button"
                                  onClick={e => {
                                    e.stopPropagation()
                                    togglePasswordVisible(Number(acc.id))
                                  }}
                                  className="shrink-0 rounded p-0.5 text-[var(--text-muted)] transition-colors hover:bg-[var(--bg-hover)] hover:text-[var(--accent)]"
                                  title={passwordVisible ? '隐藏密码' : '显示密码'}
                                  aria-label={passwordVisible ? '隐藏密码' : '显示密码'}
                                >
                                  {passwordVisible ? <EyeOff className="h-3.5 w-3.5" /> : <Eye className="h-3.5 w-3.5" />}
                                </button>
                              </div>
                            )}
                          </td>
                          <td className="px-3 py-3 align-middle">
                            <span className={cn('inline-flex min-w-[44px] items-center justify-center rounded border px-2 py-1 text-[12px] font-bold', getAccountPlanPillClassName(acc, planLabel))}>
                              {planLabel}
                            </span>
                          </td>
                          <td className="px-3 py-3 align-middle">
                            <span className={cn('inline-flex min-w-[72px] items-center justify-center rounded border px-2 py-1 text-[12px] font-bold shadow-sm', statusPillClass)}>
                              {translateAccountStatus(status, language)}
                            </span>
                          </td>
                          <td className="px-3 py-3 align-middle">
                            {displayBadges.length > 0 ? (
                              <div className="flex max-w-[180px] flex-wrap gap-1">
                                {displayBadges.map((badge: any, index: number) => (
                                  <span key={`${badge?.label || 'badge'}-${index}`} className={getAccountBadgeClassName(badge, 'modern')}>
                                    {badge?.label}
                                  </span>
                                ))}
                              </div>
                            ) : (
                              <span className="inline-flex min-w-[42px] items-center justify-center rounded border border-[var(--border-soft)] bg-[var(--bg-pane)] px-2 py-1 text-[12px] text-[var(--text-muted)]">
                                -
                              </span>
                            )}
                          </td>
                          <td className="px-3 py-3 align-middle">
                            <span
                              className={cn(
                                'inline-flex min-w-[74px] items-center justify-center rounded border px-2 py-1 text-[12px] font-bold shadow-sm',
                                validityLabel === '-' || validityLabel === '已过期'
                                  ? 'border-[var(--border-soft)] bg-[var(--bg-pane)] text-[var(--text-muted)]'
                                  : 'border-emerald-500/20 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300',
                              )}
                            >
                              {validityLabel}
                            </span>
                          </td>
                          <td className="px-3 py-3 align-middle" onClick={e => e.stopPropagation()}>
                            <BaExtractTaskCell
                              task={baTask}
                              onViewLogs={() => openBaExtractLogs(Number(acc.id))}
                            />
                          </td>
                          {tab === 'chatgpt' && (
                            <td className="w-[88px] max-w-[88px] px-2 py-3 align-middle" onClick={e => e.stopPropagation()}>
                              <PpTaskCell
                                task={resolvePpTask(acc)}
                                onViewLogs={() => setPpLogTask(resolvePpTask(acc))}
                              />
                            </td>
                          )}
                          <td className="whitespace-nowrap px-3 py-3 align-middle font-mono text-[12px] text-[var(--text-secondary)]">
                            {createdLabel}
                          </td>
                          <td
                            className="sticky right-0 z-10 w-[320px] min-w-[320px] max-w-[320px] border-l border-[var(--border-soft)] bg-[var(--bg-card)] px-2 py-3 align-middle shadow-[-8px_0_12px_-10px_rgba(0,0,0,0.28)] group-hover:bg-[var(--bg-pane)]"
                            onClick={e => e.stopPropagation()}
                          >
                            <div className="flex w-[304px] flex-wrap content-start items-center gap-1.5">
                              {tab === 'chatgpt' && (
                                <button
                                  onClick={() => ppBaToken ? clearPpBaToken(acc) : setPpBaAccount(acc)}
                                  className={cn('table-action-btn', ppBaToken ? 'border-red-500/25 bg-red-500/5 text-red-600 hover:border-red-500/45 hover:bg-red-500/10 dark:text-red-300' : '')}
                                >
                                  {ppBaToken ? '清除BA链' : '填写BA链'}
                                </button>
                              )}
                              {tab === 'chatgpt' && (
                                <button
                                  onClick={() => {
                                    setBaExtractForm(loadBaExtractSettings())
                                    setBaExtractAccount(acc)
                                  }}
                                  disabled={baTaskActive}
                                  className="table-action-btn"
                                >
                                  {baTaskActive ? 'BA任务中' : '提取BA链'}
                                </button>
                              )}
                              <button onClick={() => setDetail(acc)} className="table-action-btn">查看</button>
                              {tab === 'chatgpt' && (
                                <button
                                  onClick={() => { if (hasSessionJson) copy(sessionText) }}
                                  disabled={!hasSessionJson}
                                  className="table-action-btn"
                                  title={hasSessionJson ? '复制 api/auth/session session 数据' : '当前账号未保存 api/auth/session'}
                                >
                                  复制SESSION
                                </button>
                              )}
                              {tab === 'chatgpt' && (
                                <button
                                  onClick={() => showTotpCodeForAccount(acc)}
                                  className="table-action-btn"
                                  title={getChatgptTotpSecret(acc) ? '查看并复制当前 6 位 2FA 验证码' : '当前账号未保存 2FA 密钥'}
                                >
                                  查看2FA验证码
                                </button>
                              )}
                              {tab === 'chatgpt' && (
                                <button
                                  onClick={() => runInlineAccountAction(acc, 'upload_sub2api', `上传 SUB2API - ${acc.email}`)}
                                  disabled={isBusy('upload_sub2api')}
                                  className="table-action-btn"
                                >
                                  {isBusy('upload_sub2api') ? '上传中' : '上传SUB2API'}
                                </button>
                              )}
                              <button
                                onClick={() => copyAccessToken(acc, primaryToken)}
                                disabled={!primaryToken || copyingAccessTokenId === Number(acc.id)}
                                className="table-action-btn border-emerald-500/25 bg-emerald-500/10 text-emerald-700 hover:border-emerald-500/45 hover:bg-emerald-500/15 hover:text-emerald-800 dark:text-emerald-300 dark:hover:text-emerald-200"
                                title={primaryToken ? `复制 access_token，已复制 ${accessTokenCopyCount} 次` : '当前账号没有 AT'}
                              >
                                {copyingAccessTokenId === Number(acc.id) ? '保存中' : `复制 AT${accessTokenCopyCount > 0 ? ` · ${accessTokenCopyCount}` : ''}`}
                              </button>
                              {tab === 'chatgpt' && (
                                <button
                                  onClick={() => copyChatgptAtCookie(acc, atCookieText)}
                                  disabled={!hasAtCookie || copyingAccessTokenId === Number(acc.id)}
                                  className="table-action-btn border-sky-500/25 bg-sky-500/10 text-sky-700 hover:border-sky-500/45 hover:bg-sky-500/15 hover:text-sky-800 dark:text-sky-300 dark:hover:text-sky-200"
                                  title={hasAtCookie ? '复制格式：Access Token | cookie' : '当前账号缺少 AT 或 cookie'}
                                >
                                  {copyingAccessTokenId === Number(acc.id) ? '保存中' : '复制AT|cookie'}
                                </button>
                              )}
                              {tab === 'chatgpt' && (
                                <button
                                  onClick={() => refreshPlanForAccount(acc)}
                                  disabled={isBusy('refresh_plan')}
                                  className="table-action-btn"
                                >
                                  {isBusy('refresh_plan') ? '刷新中' : '刷新套餐'}
                                </button>
                              )}
                              {tab === 'chatgpt' && (
                                <button
                                  onClick={() => runInlineAccountAction(acc, 'refresh_token', `刷新 token - ${acc.email}`)}
                                  disabled={isBusy('refresh_token')}
                                  className="table-action-btn"
                                >
                                  {isBusy('refresh_token') ? '刷新中' : '刷新 token'}
                                </button>
                              )}
                              <button
                                onClick={() => runInlineHealthCheck(acc)}
                                disabled={isBusy('health_check')}
                                className="table-action-btn"
                              >
                                {isBusy('health_check') ? '检测中' : '检测存活'}
                              </button>
                              <button
                                onClick={() => {
                                  if (confirm(t('accounts.deleteConfirm', { email: acc.email }))) {
                                    apiFetch(`/accounts/${acc.id}`, { method: 'DELETE' }).then(() => load())
                                  }
                                }}
                                className="table-action-btn table-action-btn-danger"
                              >
                                删除
                              </button>
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
              <div className="flex flex-wrap items-center gap-3">
                <span>
                  {t('accounts.showingEntries', { from: entryStart, to: entryEnd, total })}
                </span>
                <label className="flex items-center gap-1.5 text-[var(--text-secondary)]">
                  <span>每页</span>
                  <select
                    value={pageSize}
                    onChange={(e) => {
                      const next = Number(e.target.value) || 10
                      setPageSize(next)
                      setPage(1)
                    }}
                    className="h-7 rounded-md border border-[var(--border)] bg-[var(--bg-input)] px-2 text-[11px] text-[var(--text-primary)] outline-none focus:ring-2 focus:ring-[rgba(var(--accent-rgb),0.2)]"
                  >
                    {[5, 10, 20, 50, 100].map((value) => (
                      <option key={value} value={value}>{value}</option>
                    ))}
                  </select>
                  <span>条</span>
                </label>
              </div>
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
            {tab === 'chatgpt' && (
              <Button size="sm" variant="outline" onClick={() => setShowPpPlusSettings(true)} className={ACCOUNT_TOOL_BUTTON_CLASS}>
                PLUS开通设置
              </Button>
            )}
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
                tagFilter={filterTag}
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
                onClick={startBatchSecuritySetup}
                disabled={batchSecurityBusy}
                className={ACCOUNT_TOOL_BUTTON_CLASS}
                title={selectedCount > 0 ? `为已勾选的 ${selectedCount} 个账号设置密码和2FA` : '未勾选：处理全部未绑定2FA的账号'}
              >
                {batchSecurityBusy ? (
                  <Loader2 className="mr-1.5 h-3.5 w-3.5 shrink-0 animate-spin" />
                ) : (
                  <ShieldCheck className="mr-1.5 h-3.5 w-3.5 shrink-0" />
                )}
                批量设置密码/2FA
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
            <select
              value={filterTag}
              onChange={e => setFilterTag(e.target.value)}
              className="rounded-md border border-[var(--border-soft)] bg-transparent py-1.5 pl-3 pr-8 text-sm text-[var(--text-primary)] transition-colors focus:border-[var(--text-primary)] focus:outline-none focus:ring-1 focus:ring-[var(--text-primary)] appearance-none"
              style={{ backgroundImage: `url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='14' height='14' viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='m6 9 6 6 6-6'/%3E%3C/svg%3E")`, backgroundPosition: 'right 8px center', backgroundRepeat: 'no-repeat' }}
            >
              <option value="">全部标签</option>
              {ACCOUNT_TAG_FILTER_OPTIONS.map(tag => (
                <option key={tag} value={tag}>{tag}</option>
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
              <th className="px-3 py-2 text-left">{tab === 'chatgpt' ? 'BA链任务' : t('accounts.link')}</th>
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
                const baTask = resolveBaExtractTask(acc)
                const baTaskActive = isBaExtractTaskActive(baTask)
                const ppBaToken = getAccountBaToken(acc)
                const accountPassword = String(acc?.password || '').trim()
                const passwordVisible = visiblePasswordIds.has(Number(acc.id))
                return (
              <tr key={acc.id} className="group border-b border-[var(--border)]/30 hover:bg-[var(--text-primary)]/[0.02] transition-colors"
>
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
                    <span className="truncate tracking-tight cursor-copy" title={acc.email} onClick={e => { e.stopPropagation(); copy(acc.email) }}>{acc.email}</span>
                    <button onClick={e => { e.stopPropagation(); copy(acc.email) }} title="复制邮箱" className="text-[var(--text-muted)] hover:text-[var(--text-primary)] opacity-0 group-hover:opacity-100 transition-opacity"><Copy className="h-3 w-3" /></button>
                  </div>
                  {accountPassword && (
                    <div className="mt-1 flex min-w-0 items-center gap-1.5 text-xs text-[var(--text-muted)]">
                      <span className="shrink-0 text-[10px] font-semibold tracking-wide">密码</span>
                      <span
                        className={cn(
                          'min-w-0 flex-1 truncate transition-all duration-150',
                          passwordVisible ? 'select-text text-[var(--text-secondary)] blur-0' : 'select-none blur-[4px]',
                        )}
                        title={passwordVisible ? accountPassword : '点击图标显示密码'}
                      >
                        {accountPassword}
                      </span>
                      <button
                        type="button"
                        onClick={e => {
                          e.stopPropagation()
                          togglePasswordVisible(Number(acc.id))
                        }}
                        className="shrink-0 rounded p-0.5 text-[var(--text-muted)] transition-colors hover:bg-[var(--bg-hover)] hover:text-[var(--text-primary)]"
                        title={passwordVisible ? '隐藏密码' : '显示密码'}
                        aria-label={passwordVisible ? '隐藏密码' : '显示密码'}
                      >
                        {passwordVisible ? <EyeOff className="h-3.5 w-3.5" /> : <Eye className="h-3.5 w-3.5" />}
                      </button>
                    </div>
                  )}
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
                  {accountPassword ? (
                    <div className="flex min-w-0 items-center gap-1.5">
                      <span
                        className={cn(
                          'truncate transition-all duration-150',
                          passwordVisible ? 'select-text text-[var(--text-secondary)] blur-0' : 'select-none blur-[4px]',
                        )}
                        title={passwordVisible ? accountPassword : '点击图标显示密码'}
                      >
                        {accountPassword}
                      </span>
                      <button
                        type="button"
                        onClick={e => {
                          e.stopPropagation()
                          togglePasswordVisible(Number(acc.id))
                        }}
                        className="text-[var(--text-muted)] transition-colors hover:text-[var(--text-primary)]"
                        title={passwordVisible ? '隐藏密码' : '显示密码'}
                        aria-label={passwordVisible ? '隐藏密码' : '显示密码'}
                      >
                        {passwordVisible ? <EyeOff className="h-3 w-3" /> : <Eye className="h-3 w-3" />}
                      </button>
                      <button onClick={e => { e.stopPropagation(); copy(accountPassword) }} className="text-[var(--text-muted)] hover:text-[var(--text-primary)] opacity-0 group-hover:opacity-100 transition-opacity" title="复制密码"><Copy className="h-3 w-3" /></button>
                    </div>
                  ) : (
                    <span className="text-[var(--text-muted)]">-</span>
                  )}
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
                <td className="px-3 py-2.5 align-top" onClick={e => e.stopPropagation()}>
                  {tab === 'chatgpt' ? (
                    <BaExtractTaskCell
                      task={baTask}
                      onViewLogs={() => openBaExtractLogs(Number(acc.id))}
                    />
                  ) : getCashierUrl(acc) ? (
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
                {tab === 'chatgpt' && (
                  <td className="px-3 py-2.5 align-top" onClick={e => e.stopPropagation()}>
                    <PpTaskCell
                      task={resolvePpTask(acc)}
                      onViewLogs={() => setPpLogTask(resolvePpTask(acc))}
                    />
                  </td>
                )}
                <td className="px-3 py-2.5 align-top" onClick={e => e.stopPropagation()}>
                  <div className="flex items-center justify-end gap-1.5 opacity-60 group-hover:opacity-100 transition-opacity">
                    {tab === 'chatgpt' && (
                      <Button
                        size="sm"
                        variant="outline"
                        className={cn('h-7 px-2 text-[11px]', ppBaToken ? 'border-red-500/25 bg-red-500/5 text-red-600 hover:bg-red-500/10 dark:text-red-300' : '')}
                        onClick={() => ppBaToken ? clearPpBaToken(acc) : setPpBaAccount(acc)}
                      >
                        {ppBaToken ? '清除BA链' : '填写BA链'}
                      </Button>
                    )}
                    {tab === 'chatgpt' && (
                      <Button
                        size="sm"
                        variant="outline"
                        className="h-7 px-2 text-[11px]"
                        disabled={baTaskActive}
                        onClick={() => {
                          setBaExtractForm(loadBaExtractSettings())
                          setBaExtractAccount(acc)
                        }}
                      >
                        {baTaskActive ? 'BA任务中' : '提取BA链'}
                      </Button>
                    )}
                    <ActionMenu
                      acc={acc}
                      onDetail={() => setDetail(acc)}
                      onDelete={() => load()}
                      onResult={(title, payload) => setActionResult({ title, payload })}
                      onChanged={() => load()}
                      onTriggerGetRt={triggerGetRtForAccount}
                      onViewTotp={showTotpCodeForAccount}
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
