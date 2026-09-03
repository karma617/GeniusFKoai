import { useEffect, useState } from 'react'
import { createPortal } from 'react-dom'
import { Check, Copy, X } from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { apiFetch } from '@/lib/utils'
import { TASK_STATUS_VARIANTS, getTaskStatusText } from '@/lib/tasks'
import { useI18n } from '@/lib/i18n-context'
import { TaskLogPanel } from '@/components/tasks/TaskLogPanel'

/**
 * 换绑任务日志弹窗：头部任务 ID + 状态，正文复用 TaskLogPanel
 * （内部 GET /tasks/{id} 轮询进度 + SSE /tasks/{id}/logs/stream 实时日志，
 * 并自带"复制日志"小按钮），底部提供显眼的"复制全部日志"入口——
 * 直接分页拉取任务全量事件并拼接完整文本，便于离线排查。
 */
export function RebindTaskLogDialog({
  taskId,
  taskStatus,
  onClose,
  onDone,
}: {
  taskId: string
  taskStatus?: string
  onClose: () => void
  onDone?: (status: string) => void
}) {
  const { t, language } = useI18n()
  const [copyState, setCopyState] = useState<'idle' | 'copying' | 'ok' | 'error'>('idle')

  useEffect(() => {
    if (copyState !== 'ok' && copyState !== 'error') return
    const timer = window.setTimeout(() => setCopyState('idle'), 2000)
    return () => window.clearTimeout(timer)
  }, [copyState])

  const copyAllLogs = async () => {
    if (copyState === 'copying') return
    setCopyState('copying')
    try {
      let since = 0
      const lines: string[] = []
      for (let guard = 0; guard < 40; guard += 1) {
        const data = await apiFetch(`/tasks/${taskId}/events?since=${since}&limit=500`)
        const items = Array.isArray(data?.items) ? data.items : []
        for (const item of items) {
          since = Math.max(since, Number(item?.id || 0))
          const detail = item?.detail && typeof item.detail === 'object' ? item.detail : {}
          const copyText =
            typeof detail.copy_text === 'string' && detail.copy_text.trim() ? detail.copy_text : ''
          const responseBody =
            typeof detail.response_body === 'string' && detail.response_body.trim()
              ? detail.response_body
              : ''
          lines.push(
            copyText ? copyText : responseBody ? `${item.line}\n${responseBody}` : String(item?.line || ''),
          )
        }
        if (items.length < 500) break
      }
      const text = lines.filter((line) => line !== '').join('\n')
      if (!navigator.clipboard?.writeText) throw new Error('clipboard unavailable')
      await navigator.clipboard.writeText(text)
      setCopyState('ok')
    } catch {
      setCopyState('error')
    }
  }

  return createPortal(
    <div className="dialog-backdrop" onClick={onClose}>
      <div
        className="dialog-panel flex flex-col overflow-hidden"
        style={{ width: 'min(860px, calc(100vw - 32px))', height: 'min(760px, calc(100dvh - 48px))' }}
        onClick={(event) => event.stopPropagation()}
      >
        <div className="flex shrink-0 items-start justify-between gap-3 border-b border-[var(--border)] px-6 py-4">
          <div className="min-w-0">
            <h2 className="text-base font-semibold text-[var(--text-primary)]">
              {t('chatgptRebind.taskLog.title')}
            </h2>
            <p className="mt-1 break-all font-mono text-xs text-[var(--text-muted)]">
              {taskId}
            </p>
          </div>
          <div className="flex shrink-0 items-center gap-2">
            {taskStatus ? (
              <Badge variant={TASK_STATUS_VARIANTS[taskStatus] || 'secondary'}>
                {getTaskStatusText(taskStatus, language)}
              </Badge>
            ) : null}
            <button
              onClick={onClose}
              className="shrink-0 text-[var(--text-muted)] hover:text-[var(--text-primary)]"
            >
              <X className="h-4 w-4" />
            </button>
          </div>
        </div>
        <div className="min-h-0 flex-1 overflow-hidden px-6 py-4">
          <TaskLogPanel taskId={taskId} onDone={(status) => onDone?.(status)} />
        </div>
        <div className="flex shrink-0 items-center justify-end gap-2 border-t border-[var(--border)] px-6 py-3">
          {copyState === 'ok' ? (
            <span className="flex items-center gap-1 text-xs text-emerald-700 dark:text-emerald-300">
              <Check className="h-3.5 w-3.5" />
              {t('chatgptRebind.taskLog.copyAllDone')}
            </span>
          ) : null}
          {copyState === 'error' ? (
            <span className="text-xs text-red-700 dark:text-red-300">
              {t('chatgptRebind.taskLog.copyAllFailed')}
            </span>
          ) : null}
          <Button
            size="sm"
            onClick={copyAllLogs}
            disabled={copyState === 'copying'}
          >
            {copyState === 'copying' ? (
              <Copy className="mr-1.5 h-3.5 w-3.5" />
            ) : copyState === 'ok' ? (
              <Check className="mr-1.5 h-3.5 w-3.5" />
            ) : (
              <Copy className="mr-1.5 h-3.5 w-3.5" />
            )}
            {t('chatgptRebind.taskLog.copyAll')}
          </Button>
          <Button variant="outline" size="sm" onClick={onClose}>
            {t('common.close')}
          </Button>
        </div>
      </div>
    </div>,
    document.body,
  )
}
