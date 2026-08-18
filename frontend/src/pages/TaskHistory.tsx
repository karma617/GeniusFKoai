import { useEffect, useState } from 'react'
import { createPortal } from 'react-dom'
import { getPlatforms } from '@/lib/app-data'
import { apiFetch } from '@/lib/utils'
import { formatDateTime } from '@/lib/i18n'
import { useI18n } from '@/lib/i18n-context'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { TaskLogPanel } from '@/components/tasks/TaskLogPanel'
import { getTaskStatusText, isCancellableTaskStatus, isTerminalTaskStatus, TASK_STATUS_VARIANTS } from '@/lib/tasks'
import { RefreshCw, Activity, CheckCircle2, AlertTriangle, Clock3, ChevronDown, CircleStop, FileText, X } from 'lucide-react'

function shortId(id: string) {
  if (!id) return '-'
  return id.length > 12 ? '...' + id.slice(-8) : id
}

function formatError(error: string | null | undefined): string {
  if (!error) return ''
  // Try to extract a readable message from JSON-like strings
  try {
    if (error.startsWith('{') || error.startsWith('[')) {
      const parsed = JSON.parse(error)
      if (parsed.message) return parsed.message
      if (parsed.error) return parsed.error
      if (Array.isArray(parsed.errors) && parsed.errors.length > 0) {
        const first = parsed.errors[0]
        return first.message || first.kind || JSON.stringify(first).slice(0, 80)
      }
    }
  } catch {
    // not JSON
  }
  // Truncate long strings
  return error.length > 100 ? error.slice(0, 100) + '...' : error
}

const TASK_TYPE_OPTIONS = [
  { value: 'register', zh: '批量注册', en: 'Batch Register' },
  { value: 'account_check', zh: '批量测活', en: 'Batch Check' },
  { value: 'account_check_all', zh: '全部测活', en: 'Check All Accounts' },
  { value: 'account_health_check', zh: '账号健康检测', en: 'Account Health Check' },
  { value: 'platform_action', zh: '批量操作', en: 'Batch Action' },
  { value: 'phone_bind', zh: '绑定手机号', en: 'Bind Phone' },
  { value: 'codex_oauth', zh: 'Codex OAuth', en: 'Codex OAuth' },
  { value: 'momo_trial_probe', zh: 'MOMO试用检测', en: 'MoMo Trial Probe' },
  { value: 'get_rt', zh: '批量获取RT', en: 'Batch Get RT' },
  { value: 'get_rt_bypass', zh: '批量获取RT(绕过)', en: 'Batch Get RT (Bypass)' },
  { value: 'refresh_session', zh: '重登验证', en: 'Refresh Session' },
  { value: 'batch_security_setup', zh: '批量设置密码/2FA', en: 'Batch Password/2FA' },
  { value: 'agents_upload_sub2api', zh: '上传SUB2API', en: 'Upload SUB2API' },
  { value: 'gopay_pay_chatgpt', zh: '开通PLUS', en: 'Open PLUS' },
  { value: 'gopay_register_account', zh: '注册GoPay', en: 'Register GoPay' },
]

function getTaskTypeLabel(taskType: string | null | undefined, language: string): string {
  const value = String(taskType || '').trim()
  if (!value) return '-'
  const option = TASK_TYPE_OPTIONS.find((item) => item.value === value)
  if (!option) return value
  return language === 'en-US' ? option.en : option.zh
}

function TaskLogDialog({
  task,
  onClose,
  onTaskDone,
}: {
  task: any
  onClose: () => void
  onTaskDone: (taskId: string, status: string) => void
}) {
  const { t, language } = useI18n()
  const taskId = String(task?.id || task?.task_id || '')
  const taskType = String(task?.type || '')

  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', handleKeyDown)
    return () => window.removeEventListener('keydown', handleKeyDown)
  }, [onClose])

  if (!taskId) return null

  const dialog = (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/45 px-4 py-6 backdrop-blur-sm">
      <div className="flex h-[min(86vh,760px)] w-[60vw] flex-col overflow-hidden rounded-2xl border border-[var(--border)] bg-[var(--bg-card)] shadow-2xl">
        <div className="flex shrink-0 items-start justify-between gap-4 border-b border-[var(--border)] px-6 py-5">
          <div>
            <Badge variant="secondary" className="mb-3">
              {task.platform || t('taskLog.platformAction')}
            </Badge>
            <div className="flex flex-wrap items-center gap-3">
              <h2 className="text-lg font-semibold text-[var(--text-primary)]">
                {taskType || t('taskHistory.taskLogs')}
              </h2>
              <Badge variant={TASK_STATUS_VARIANTS[task.status] || 'secondary'}>
                {getTaskStatusText(task.status, language)}
              </Badge>
            </div>
            <p className="mt-1 text-sm text-[var(--text-secondary)]">
              {t('taskLog.dialogSubtitle')}
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full border border-[var(--border-soft)] bg-[var(--bg-pane)] text-[var(--text-secondary)] transition hover:text-[var(--text-primary)]"
            aria-label={t('common.close')}
          >
            <X className="h-4 w-4" />
          </button>
        </div>
        <div className="min-h-0 flex-1 bg-[var(--bg-base)] px-6 py-5">
          <TaskLogPanel
            taskId={taskId}
            onDone={(nextStatus) => onTaskDone(taskId, nextStatus)}
          />
        </div>
        <div className="flex shrink-0 items-center justify-between gap-3 border-t border-[var(--border)] px-6 py-3">
          <div className="truncate font-mono text-xs text-[var(--text-muted)]">
            {t('taskHistory.taskId')}: {taskId}
          </div>
          <Button variant="outline" size="sm" onClick={onClose}>
            {t('common.close')}
          </Button>
        </div>
      </div>
    </div>
  )

  return typeof document !== 'undefined' ? createPortal(dialog, document.body) : dialog
}

export default function TaskHistory() {
  const { t, language } = useI18n()
  const [tasks, setTasks] = useState<any[]>([])
  const [platform, setPlatform] = useState('')
  const [taskType, setTaskType] = useState('')
  const [status, setStatus] = useState('')
  const [platforms, setPlatforms] = useState<any[]>([])
  const [loading, setLoading] = useState(false)
  const [terminatingTaskIds, setTerminatingTaskIds] = useState<Set<string>>(() => new Set())
  const [logTask, setLogTask] = useState<any | null>(null)

  const load = async () => {
    setLoading(true)
    try {
      const params = new URLSearchParams({ page: '1', page_size: '50' })
      if (platform) params.set('platform', platform)
      if (taskType) params.set('type', taskType)
      if (status) params.set('status', status)
      const data = await apiFetch(`/tasks?${params}`)
      setTasks(data.items || [])
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    getPlatforms()
      .then((data) => setPlatforms(data || []))
      .catch(() => setPlatforms([]))
  }, [])

  useEffect(() => {
    load()
  }, [platform, taskType, status])

  const handleTerminate = async (task: any) => {
    const taskId = String(task.id || task.task_id || '')
    if (!taskId || terminatingTaskIds.has(taskId)) return
    setTerminatingTaskIds((current) => new Set(current).add(taskId))
    try {
      const updated = await apiFetch(`/tasks/${encodeURIComponent(taskId)}/cancel`, { method: 'POST' })
      setTasks((items) =>
        items.map((item) =>
          String(item.id || item.task_id || '') === taskId
            ? { ...item, ...updated }
            : item
        )
      )
    } finally {
      setTerminatingTaskIds((current) => {
        const next = new Set(current)
        next.delete(taskId)
        return next
      })
    }
  }

  const handleLogTaskDone = (taskId: string, nextStatus: string) => {
    setTasks((items) =>
      items.map((item) =>
        String(item.id || item.task_id || '') === taskId
          ? { ...item, status: nextStatus }
          : item
      )
    )
    setLogTask((current: any | null) =>
      current && String(current.id || current.task_id || '') === taskId
        ? { ...current, status: nextStatus }
        : current
    )
  }

  const succeeded = tasks.filter((t) => t.status === 'succeeded').length
  const failed = tasks.filter((t) => t.status === 'failed').length
  const running = tasks.filter((t) => ['running', 'claimed', 'pending'].includes(t.status)).length

  const metricCards = [
    { label: t('taskHistory.metric.total'), value: tasks.length, icon: Activity, tone: 'text-[var(--accent)]' },
    { label: t('common.success'), value: succeeded, icon: CheckCircle2, tone: 'text-emerald-500' },
    { label: t('common.failure'), value: failed, icon: AlertTriangle, tone: 'text-red-500' },
    { label: t('taskHistory.metric.running'), value: running, icon: Clock3, tone: 'text-amber-500' },
  ]

  return (
    <div className="space-y-5">
      {/* Header */}
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-bold text-[var(--text-primary)]">{t('taskHistory.title')}</h1>
        <Button variant="outline" size="sm" onClick={load} disabled={loading}>
          <RefreshCw className={`h-3.5 w-3.5 mr-1.5 ${loading ? 'animate-spin' : ''}`} />
          {t('common.refresh')}
        </Button>
      </div>

      {/* Metrics */}
      <div className="grid gap-3 grid-cols-2 lg:grid-cols-4">
        {metricCards.map(({ label, value, icon: Icon, tone }) => (
          <div
            key={label}
            className="flex items-center gap-3 rounded-xl border border-[var(--border-soft)] bg-[var(--bg-card)] px-4 py-3.5 shadow-[var(--shadow-soft)] transition-all duration-200 hover:shadow-[var(--shadow-hard)] hover:border-[var(--accent-edge)]"
          >
            <div className={`flex h-10 w-10 shrink-0 items-center justify-center rounded-lg bg-[var(--gradient-accent-soft)] ${tone}`}>
              <Icon className="h-4 w-4" />
            </div>
            <div>
              <div className="text-[11px] text-[var(--text-muted)] uppercase tracking-wider">{label}</div>
              <div className="text-lg font-semibold text-[var(--text-primary)]">{value}</div>
            </div>
          </div>
        ))}
      </div>

      {/* Filters — inline with table header */}
      <div className="rounded-xl border border-[var(--border-soft)] bg-[var(--bg-card)] overflow-hidden shadow-[var(--shadow-soft)]">
        <div className="flex flex-col gap-3 border-b border-[var(--border-soft)] bg-[var(--bg-pane)]/40 px-4 py-3 sm:flex-row sm:items-center sm:py-2.5">
          <span className="whitespace-nowrap text-sm font-medium text-[var(--text-primary)]">{t('taskHistory.recent')}</span>
          <div className="hidden flex-1 sm:block" />
          <div className="grid w-full grid-cols-1 gap-2 sm:flex sm:w-auto sm:items-center">
            <div className="relative min-w-0">
              <select
                value={taskType}
                onChange={(e) => setTaskType(e.target.value)}
                className="h-8 w-full appearance-none rounded-md border border-[var(--border)] bg-[var(--bg-input)] pl-3 pr-7 text-xs text-[var(--text-secondary)] transition-colors hover:border-[var(--accent)] focus:border-[var(--accent)] sm:w-auto"
              >
                <option value="">{t('taskHistory.allTaskTypes')}</option>
                {TASK_TYPE_OPTIONS.map((item) => (
                  <option key={item.value} value={item.value}>
                    {language === 'en-US' ? item.en : item.zh}
                  </option>
                ))}
              </select>
              <ChevronDown className="pointer-events-none absolute right-2 top-1/2 h-3 w-3 -translate-y-1/2 text-[var(--text-muted)]" />
            </div>
            <div className="relative min-w-0">
              <select
                value={platform}
                onChange={(e) => setPlatform(e.target.value)}
                className="h-8 w-full appearance-none rounded-md border border-[var(--border)] bg-[var(--bg-input)] pl-3 pr-7 text-xs text-[var(--text-secondary)] transition-colors hover:border-[var(--accent)] focus:border-[var(--accent)] sm:w-auto"
              >
                <option value="">{t('taskHistory.allPlatforms')}</option>
                {platforms.map((item: any) => (
                  <option key={item.name} value={item.name}>{item.display_name}</option>
                ))}
              </select>
              <ChevronDown className="pointer-events-none absolute right-2 top-1/2 h-3 w-3 -translate-y-1/2 text-[var(--text-muted)]" />
            </div>
            <div className="relative min-w-0">
              <select
                value={status}
                onChange={(e) => setStatus(e.target.value)}
                className="h-8 w-full appearance-none rounded-md border border-[var(--border)] bg-[var(--bg-input)] pl-3 pr-7 text-xs text-[var(--text-secondary)] transition-colors hover:border-[var(--accent)] focus:border-[var(--accent)] sm:w-auto"
              >
                <option value="">{t('taskHistory.allStatuses')}</option>
                <option value="running">{t('taskHistory.running')}</option>
                <option value="succeeded">{t('common.success')}</option>
                <option value="failed">{t('common.failure')}</option>
                <option value="cancelled">{getTaskStatusText('cancelled', language)}</option>
                <option value="interrupted">{getTaskStatusText('interrupted', language)}</option>
              </select>
              <ChevronDown className="pointer-events-none absolute right-2 top-1/2 h-3 w-3 -translate-y-1/2 text-[var(--text-muted)]" />
            </div>
            {(platform || taskType || status) && (
              <button
                onClick={() => { setPlatform(''); setTaskType(''); setStatus('') }}
                className="text-xs text-[var(--text-muted)] hover:text-[var(--accent)]"
              >
                {t('common.clear')}
              </button>
            )}
          </div>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-[var(--border-soft)] bg-[var(--bg-pane)]/60">
                <th className="px-4 py-2.5 text-left text-xs font-medium text-[var(--text-muted)]">{t('common.date')}</th>
                <th className="px-4 py-2.5 text-left text-xs font-medium text-[var(--text-muted)]">{t('taskHistory.taskId')}</th>
                <th className="px-4 py-2.5 text-left text-xs font-medium text-[var(--text-muted)]">{t('taskHistory.taskType')}</th>
                <th className="px-4 py-2.5 text-left text-xs font-medium text-[var(--text-muted)]">{t('common.platform')}</th>
                <th className="px-4 py-2.5 text-left text-xs font-medium text-[var(--text-muted)]">{t('common.status')}</th>
                <th className="px-4 py-2.5 text-left text-xs font-medium text-[var(--text-muted)]">{t('common.progress')}</th>
                <th className="px-4 py-2.5 text-left text-xs font-medium text-[var(--text-muted)]">{t('taskHistory.successFailure')}</th>
                <th className="px-4 py-2.5 text-left text-xs font-medium text-[var(--text-muted)]">{t('common.error')}</th>
                <th className="px-4 py-2.5 text-right text-xs font-medium text-[var(--text-muted)]">{t('common.actions')}</th>
              </tr>
            </thead>
            <tbody>
              {tasks.length === 0 && (
                <tr>
                  <td colSpan={9} className="px-4 py-12 text-center text-sm text-[var(--text-muted)]">
                    {t('taskHistory.empty')}
                  </td>
                </tr>
              )}
              {tasks.map((task) => {
                const success = task.success || 0
                const errorCount = task.error_count || 0
                const total = success + errorCount
                const errorText = formatError(task.error)
                const taskId = String(task.id || task.task_id || '')
                const terminating = taskId ? terminatingTaskIds.has(taskId) : false
                const statusText = String(task.status || '')
                const canTerminate = Boolean(
                  taskId &&
                  statusText !== 'cancel_requested' &&
                  !isTerminalTaskStatus(statusText) &&
                  (task.cancellable === true || isCancellableTaskStatus(statusText))
                )
                return (
                  <tr
                    key={task.id}
                    className="border-b border-[var(--border-soft)] transition-colors hover:bg-[var(--bg-hover)]"
                  >
                    <td className="whitespace-nowrap px-4 py-3 text-xs text-[var(--text-muted)]">
                      {task.created_at
                        ? formatDateTime(task.created_at, language, {
                            month: '2-digit',
                            day: '2-digit',
                            hour: '2-digit',
                            minute: '2-digit',
                            hour12: false,
                          })
                        : '-'}
                    </td>
                    <td className="px-4 py-3">
                      <span
                        className="cursor-default font-mono text-xs text-[var(--text-muted)]"
                        title={task.id}
                      >
                        {shortId(task.id)}
                      </span>
                    </td>
                    <td className="px-4 py-3">
                      <Badge variant="secondary" className="whitespace-nowrap">
                        {getTaskTypeLabel(task.type, language)}
                      </Badge>
                    </td>
                    <td className="px-4 py-3">
                      <Badge variant="secondary">{task.platform || '-'}</Badge>
                    </td>
                    <td className="px-4 py-3">
                      <Badge variant={TASK_STATUS_VARIANTS[task.status] || 'secondary'}>
                        {getTaskStatusText(task.status, language)}
                      </Badge>
                    </td>
                    <td className="px-4 py-3 text-xs text-[var(--text-secondary)]">
                      {task.progress || '-'}
                    </td>
                    <td className="px-4 py-3">
                      <div className="flex items-center gap-2">
                        {total > 0 ? (
                          <>
                            <div className="flex h-1.5 w-16 overflow-hidden rounded-full bg-[var(--chip-bg)]">
                              {success > 0 && (
                                <div
                                  className="h-full bg-emerald-500 rounded-full"
                                  style={{ width: `${(success / total) * 100}%` }}
                                />
                              )}
                              {errorCount > 0 && (
                                <div
                                  className="h-full bg-red-500 rounded-full"
                                  style={{ width: `${(errorCount / total) * 100}%` }}
                                />
                              )}
                            </div>
                            <span className="text-xs text-[var(--text-muted)] whitespace-nowrap">
                              <span className="text-emerald-500">{success}</span>
                              {' / '}
                              <span className="text-red-500">{errorCount}</span>
                            </span>
                          </>
                        ) : (
                          <span className="text-xs text-[var(--text-muted)]">-</span>
                        )}
                      </div>
                    </td>
                    <td className="max-w-[280px] px-4 py-3">
                      {errorText ? (
                        <span
                          className="block truncate text-xs text-red-500 cursor-default"
                          title={task.error || ''}
                        >
                          {errorText}
                        </span>
                      ) : (
                        <span className="text-xs text-[var(--text-muted)]">-</span>
                      )}
                    </td>
                    <td className="px-4 py-3 text-right">
                      <div className="flex justify-end gap-2">
                        <Button
                          variant="outline"
                          size="sm"
                          onClick={() => setLogTask(task)}
                          title={t('taskHistory.viewLogsTitle')}
                          className="gap-1.5 whitespace-nowrap"
                        >
                          <FileText className="h-3.5 w-3.5" />
                          {t('taskHistory.viewLogs')}
                        </Button>
                        {canTerminate || terminating ? (
                          <Button
                            variant="destructive"
                            size="sm"
                            onClick={() => handleTerminate(task)}
                            disabled={!canTerminate || terminating}
                            title={t('taskHistory.terminateTitle')}
                            className="gap-1.5 whitespace-nowrap"
                          >
                            <CircleStop className={`h-3.5 w-3.5 ${terminating ? 'animate-spin' : ''}`} />
                            {terminating ? t('taskHistory.terminating') : t('taskHistory.terminate')}
                          </Button>
                        ) : null}
                      </div>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      </div>
      {logTask ? (
        <TaskLogDialog
          task={logTask}
          onClose={() => setLogTask(null)}
          onTaskDone={handleLogTaskDone}
        />
      ) : null}
    </div>
  )
}
