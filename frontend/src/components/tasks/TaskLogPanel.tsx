import { useEffect, useMemo, useRef, useState } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";

import { API_BASE, apiFetch } from "@/lib/utils";
import { getTaskStatusText, isTerminalTaskStatus } from "@/lib/tasks";
import { useI18n } from "@/lib/i18n-context";

/**
 * 单条日志事件。`subtaskId` 来自后端 ``serialize_event(...).detail.subtask_id``——
 * ``TaskLogger.log`` 在每个并发 worker 进入时通过 thread-local 自动注入。前端
 * 按这个字段分组折叠展示；空字符串表示主任务（任务级状态、汇总日志等）。
 */
type LogEvent = {
  id: number;
  line: string;
  subtaskId: string;
  subtaskLabel: string;
};

type LogGroup = {
  id: string;
  label: string;
  events: LogEvent[];
};

type TaskFailureDetail = {
  id: string;
  label: string;
  account: string;
  reason: string;
};

type HealthCheckItem = {
  account_id?: number | string;
  email?: string;
  valid?: boolean;
  transient?: boolean;
  status_code?: number | string;
  error?: string;
};

type GroupSummary = {
  id: string;
  label: string;
  account: string;
  status: "success" | "failed" | "running";
  failureReason: string;
};

const MAIN_GROUP_ID = "__main__";
const VISIBLE_LOG_LINE_LIMIT = 1200;
const VISIBLE_FAILURE_REASON_LIMIT = 520;

function classifyLine(line: string): string {
  if (line.includes("✓") || line.includes("成功")) return "text-emerald-700 dark:text-emerald-300";
  if (line.includes("✗") || line.includes("失败") || line.includes("错误"))
    return "text-red-700 dark:text-red-300";
  return "text-[var(--text-primary)]";
}

function getVisibleLine(line: string): string {
  if (line.length <= VISIBLE_LOG_LINE_LIMIT) return line;
  return line.slice(0, VISIBLE_LOG_LINE_LIMIT);
}

function stripLogPrefix(line: string): string {
  return line.replace(/^\[[^\]]+\]\s*/, "").trim();
}

function getVisibleFailureReason(reason: string): string {
  if (reason.length <= VISIBLE_FAILURE_REASON_LIMIT) return reason;
  return `${reason.slice(0, VISIBLE_FAILURE_REASON_LIMIT)}...`;
}

function extractEmail(line: string): string {
  return line.match(/[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}/i)?.[0] || "";
}

function extractFailureReason(line: string): string {
  const message = stripLogPrefix(line);
  const markers = ["✗ 注册失败:", "注册失败:", "测活失效", "测活异常", "失败:", "错误:"];
  for (const marker of markers) {
    const index = message.indexOf(marker);
    if (index >= 0) {
      return message.slice(index + marker.length).trim() || message;
    }
  }
  return message;
}

function summarizeLogGroup(group: LogGroup): GroupSummary {
  let account = "";
  let failureReason = "";
  let hasSuccess = false;
  let hasFailure = false;

  for (const ev of group.events) {
    const line = stripLogPrefix(ev.line);
    account = account || extractEmail(line);
    if (line.includes("✓") || line.includes("注册成功")) {
      hasSuccess = true;
      account = extractEmail(line) || account;
    }
    if (line.includes("✗") || line.includes("注册失败") || line.includes("错误")) {
      hasFailure = true;
      failureReason = extractFailureReason(line);
    }
  }

  return {
    id: group.id,
    label: group.label,
    account,
    status: hasFailure ? "failed" : hasSuccess ? "success" : "running",
    failureReason,
  };
}

export function TaskLogPanel({
  taskId,
  onDone,
}: {
  taskId: string;
  onDone: (status: string) => void;
}) {
  const { t, language } = useI18n();
  const [events, setEvents] = useState<LogEvent[]>([]);
  const [task, setTask] = useState<any | null>(null);
  const [doneStatus, setDoneStatus] = useState<string | null>(null);
  // 折叠状态：默认全展开（undefined / false 都视为展开）
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({});
  const seenEventIdsRef = useRef<Set<number>>(new Set());
  const cursorRef = useRef(0);
  const doneRef = useRef(false);
  const onDoneRef = useRef(onDone);
  const sseHealthyRef = useRef(false);
  const eventSourceRef = useRef<EventSource | null>(null);

  useEffect(() => {
    onDoneRef.current = onDone;
  }, [onDone]);

  useEffect(() => {
    if (!taskId) return;
    seenEventIdsRef.current = new Set();
    cursorRef.current = 0;
    doneRef.current = false;
    sseHealthyRef.current = false;
    setEvents([]);
    setTask(null);
    setDoneStatus(null);
    setCollapsed({});

    const pushEvent = (payload: any) => {
      const eventId = Number(payload?.id || 0);
      if (eventId && seenEventIdsRef.current.has(eventId)) return;
      if (eventId) {
        seenEventIdsRef.current.add(eventId);
        cursorRef.current = Math.max(cursorRef.current, eventId);
      }
      if (payload?.line) {
        const detail = payload?.detail || {};
        setEvents((prev) => [
          ...prev,
          {
            id: eventId || prev.length + 1,
            line: String(payload.line),
            subtaskId: String(detail?.subtask_id || ""),
            subtaskLabel: String(detail?.subtask_label || ""),
          },
        ]);
      }
      if (payload?.done && !doneRef.current) {
        doneRef.current = true;
        sseHealthyRef.current = false;
        eventSourceRef.current?.close();
        eventSourceRef.current = null;
        const nextStatus = payload.status || "succeeded";
        setDoneStatus(nextStatus);
        syncTask().catch(() => {});
        onDoneRef.current(nextStatus);
      }
    };

    const fetchMissingEvents = async () => {
      let guard = 0;
      while (guard < 20) {
        guard += 1;
        const data = await apiFetch(
          `/tasks/${taskId}/events?since=${cursorRef.current}&limit=500`,
        );
        const items = data.items || [];
        for (const item of items) {
          pushEvent(item);
        }
        if (items.length < 500) break;
      }
    };

    const syncTask = async () => {
      const latest = await apiFetch(`/tasks/${taskId}`);
      setTask(latest);
      if (isTerminalTaskStatus(latest.status) && !doneRef.current) {
        await fetchMissingEvents();
        pushEvent({ done: true, status: latest.status });
      }
    };

    const es = new EventSource(`${API_BASE}/tasks/${taskId}/logs/stream`);
    eventSourceRef.current = es;
    es.onopen = () => {
      sseHealthyRef.current = true;
    };
    es.onmessage = (e) => {
      sseHealthyRef.current = true;
      pushEvent(JSON.parse(e.data));
    };
    es.onerror = () => {
      if (doneRef.current) {
        es.close();
        if (eventSourceRef.current === es) {
          eventSourceRef.current = null;
        }
        return;
      }
      sseHealthyRef.current = false;
    };

    syncTask().catch(() => {});

    // 进度需要持续轮询：SSE 只发 events，progress 在 task model 上，
    // 必须主动 GET /tasks/{id} 拿。原实现里只在 SSE 不健康时轮询，导致
    // SSE 正常时进度从来不更新。
    const progressPoll = window.setInterval(() => {
      if (doneRef.current) return;
      syncTask().catch(() => {});
    }, 1500);

    const fallbackPoll = window.setInterval(async () => {
      if (doneRef.current || sseHealthyRef.current) return;
      try {
        await fetchMissingEvents();
      } catch {
        // passive
      }
    }, 1000);

    return () => {
      sseHealthyRef.current = false;
      eventSourceRef.current?.close();
      eventSourceRef.current = null;
      window.clearInterval(progressPoll);
      window.clearInterval(fallbackPoll);
    };
  }, [taskId]);

  // 按 subtaskId 把事件切成分组：主任务 + 每个 worker。
  // 顺序按"首次出现"排，保证 worker 折叠面板顺序稳定（worker_1 / worker_2…）。
  const groups: LogGroup[] = useMemo(() => {
    const map = new Map<string, LogGroup>();
    map.set(MAIN_GROUP_ID, {
      id: MAIN_GROUP_ID,
      label: t("taskLog.mainGroup"),
      events: [],
    });
    for (const ev of events) {
      const key = ev.subtaskId || MAIN_GROUP_ID;
      if (!map.has(key)) {
        map.set(key, {
          id: key,
          label: ev.subtaskLabel || key,
          events: [],
        });
      }
      const group = map.get(key)!;
      group.events.push(ev);
      if (key !== MAIN_GROUP_ID && ev.subtaskLabel) {
        group.label = ev.subtaskLabel;
      }
    }
    return Array.from(map.values());
  }, [events, t]);

  const toggleGroup = (id: string) => {
    setCollapsed((prev) => ({ ...prev, [id]: !prev[id] }));
  };

  const currentStatus = doneStatus || task?.status || "running";
  const progress = task?.progress_detail || {};
  const resultData =
    task?.data && typeof task.data === "object"
      ? task.data
      : task?.result?.data && typeof task.result.data === "object"
        ? task.result.data
        : {};
  const isAccountHealthCheck = task?.type === "account_health_check";
  const healthValidCount = Number(resultData?.valid || 0);
  const healthInvalidCount = Number(resultData?.invalid || 0);
  const healthErrorCount = Number(resultData?.error || 0);
  const healthTotal = Number(resultData?.total || 0);
  const progressTotal = Number(healthTotal || progress.total || 0);
  const progressCurrent = Number(progress.current || 0);
  const successCount = isAccountHealthCheck
    ? healthValidCount
    : Number(task?.success || 0);
  const failureCount = isAccountHealthCheck
    ? healthInvalidCount + healthErrorCount
    : Number(task?.error_count || 0);
  const handledCount =
    isAccountHealthCheck && progressTotal > 0
      ? Math.min(successCount + failureCount, progressTotal)
      : successCount + failureCount > 0
      ? successCount + failureCount
      : progressCurrent;
  const pendingCount =
    progressTotal > 0 ? Math.max(progressTotal - handledCount, 0) : 0;
  const progressPercent =
    progressTotal > 0
      ? Math.min(100, Math.round((handledCount / progressTotal) * 100))
      : 0;
  const errorText =
    task?.error || (Array.isArray(task?.errors) ? task.errors[0] : "");
  // SMS_POOL_EXHAUSTED 是后端约定的"号码不可用"标记前缀，渲染成更友好
  // 的中文（用户诉求："号池没号结束当前线程，并且前端弹窗此号码不可用"）
  const friendlyError = String(errorText || "").includes("SMS_POOL_EXHAUSTED")
    ? t("ctfGptPlus.smsPoolExhausted")
    : errorText;
  const statusTone =
    currentStatus === "succeeded"
      ? "border-emerald-500/25 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300"
      : currentStatus === "failed"
        ? "border-red-500/25 bg-red-500/10 text-red-700 dark:text-red-300"
        : currentStatus === "cancelled" || currentStatus === "interrupted"
          ? "border-amber-500/25 bg-amber-500/10 text-amber-700 dark:text-amber-300"
          : "border-[rgba(var(--accent-rgb),0.24)] bg-[rgba(var(--accent-rgb),0.08)] text-[var(--accent)]";
  const groupSummaries = useMemo(
    () =>
      groups
        .filter((group) => group.id !== MAIN_GROUP_ID)
        .map(summarizeLogGroup),
    [groups],
  );
  const healthCheckFailureDetails: TaskFailureDetail[] = useMemo(() => {
    if (!isAccountHealthCheck || !Array.isArray(resultData?.items)) return [];
    return (resultData.items as HealthCheckItem[])
      .filter((item) => item && !item.valid)
      .map((item, index) => {
        const accountId = String(item.account_id || "");
        const statusCode = String(item.status_code || "").trim();
        const defaultReason = item.transient
          ? "检测异常"
          : statusCode
            ? `账号状态/订阅 HTTP ${statusCode}`
            : "账号状态/订阅判定失效";
        return {
          id: accountId || `health-check-${index}`,
          label: item.transient ? "检测异常" : "账号失效",
          account: String(item.email || accountId || `#${index + 1}`),
          reason: String(item.error || defaultReason),
        };
      });
  }, [isAccountHealthCheck, resultData]);
  const taskFailureDetails: TaskFailureDetail[] = useMemo(() => {
    if (healthCheckFailureDetails.length > 0) return healthCheckFailureDetails;

    const details = groupSummaries
      .filter((item) => item.status === "failed" && item.failureReason)
      .map((item) => ({
        id: item.id,
        label: item.label,
        account: item.account,
        reason: item.failureReason,
      }));

    if (details.length > 0 || !friendlyError) return details;
    return [
      {
        id: "task-error",
        label: t("taskLog.mainGroup"),
        account: "",
        reason: String(friendlyError),
      },
    ];
  }, [friendlyError, groupSummaries, healthCheckFailureDetails, t]);

  const copyLogs = () => {
    navigator.clipboard
      ?.writeText(events.map((ev) => ev.line).join("\n"))
      .catch(() => {});
  };

  return (
    <div className="flex h-full min-h-0 flex-col gap-4 text-[var(--text-primary)]">
      <div className="grid shrink-0 gap-3 md:grid-cols-4">
        <div className={`rounded-lg border px-4 py-3 ${statusTone}`}>
          <div className="text-[11px] font-semibold uppercase tracking-[0.18em] opacity-80">
            {t("taskLog.status")}
          </div>
          <div className="mt-1 text-sm font-semibold">
            {getTaskStatusText(currentStatus, language)}
          </div>
        </div>
        <div className="rounded-lg border border-[var(--border-soft)] bg-[var(--bg-pane)] px-4 py-3">
          <div className="text-[11px] font-semibold uppercase tracking-[0.18em] text-[var(--text-secondary)]">
            {t("taskLog.taskTotal")}
          </div>
          <div className="mt-1 text-sm font-semibold text-[var(--text-primary)]">
            {t("taskLog.taskTotalValue", {
              handled: handledCount,
              total: progressTotal,
            })}
          </div>
        </div>
        <div className="rounded-lg border border-[var(--border-soft)] bg-[var(--bg-pane)] px-4 py-3">
          <div className="text-[11px] font-semibold uppercase tracking-[0.18em] text-[var(--text-secondary)]">
            {t("taskLog.successCount")}
          </div>
          <div className="mt-1 text-sm font-semibold text-emerald-700 dark:text-emerald-300">
            {successCount}
          </div>
        </div>
        <div className="rounded-lg border border-[var(--border-soft)] bg-[var(--bg-pane)] px-4 py-3">
          <div className="text-[11px] font-semibold uppercase tracking-[0.18em] text-[var(--text-secondary)]">
            {t("taskLog.failureCount")}
          </div>
          <div className="mt-1 flex items-baseline gap-2 text-sm font-semibold text-red-700 dark:text-red-300">
            <span>{failureCount}</span>
            {pendingCount > 0 ? (
              <span className="text-xs font-medium text-[var(--text-secondary)]">
                {t("taskLog.pendingCount", { count: pendingCount })}
              </span>
            ) : null}
          </div>
        </div>
      </div>

      <div className="h-2 shrink-0 overflow-hidden rounded-full bg-[var(--bg-pane)] ring-1 ring-[var(--border-soft)]">
        <div
          className={`h-full rounded-full transition-all duration-500 ${
            currentStatus === "failed"
              ? "bg-red-400"
              : currentStatus === "succeeded"
                ? "bg-emerald-400"
                : "bg-sky-400"
          }`}
          style={{
            width: `${progressTotal > 0 ? progressPercent : isTerminalTaskStatus(currentStatus) ? 100 : 18}%`,
          }}
        />
      </div>

      {taskFailureDetails.length > 0 ? (
        <div className="flex max-h-56 shrink-0 flex-col rounded-lg border border-red-500/25 bg-red-500/10 px-4 py-3 text-sm text-red-700 dark:text-red-300">
          <div className="mb-2 flex items-center justify-between gap-3 font-semibold">
            <span>{t("taskLog.failureDetails")}</span>
            <span className="text-xs font-medium text-red-700/70 dark:text-red-300/70">
              {t("taskLog.failureDetailCount", {
                count: taskFailureDetails.length,
              })}
            </span>
          </div>
          <div className="min-h-0 flex-1 space-y-2 overflow-y-auto pr-2">
            {taskFailureDetails.map((item) => (
              <div
                key={item.id}
                className="rounded-md border border-red-500/20 bg-[var(--bg-card)]/80 px-3 py-2"
              >
                <div className="flex flex-wrap items-center gap-2 text-xs font-semibold text-red-700 dark:text-red-300">
                  <span>{item.account || item.label}</span>
                  {item.account && item.label ? (
                    <span className="font-medium text-red-700/65 dark:text-red-300/65">
                      {item.label}
                    </span>
                  ) : null}
                </div>
                <div className="mt-1 whitespace-pre-wrap break-words text-xs leading-5 text-red-700/90 dark:text-red-300/90">
                  {getVisibleFailureReason(item.reason)}
                </div>
              </div>
            ))}
          </div>
        </div>
      ) : null}

      <div className="flex shrink-0 items-center justify-between gap-3">
        <div>
          <div className="text-[11px] font-semibold uppercase tracking-[0.18em] text-[var(--text-secondary)]">
            {t("taskLog.liveLog")}
          </div>
          <div className="mt-1 text-sm font-medium text-[var(--text-primary)]">
            {t("taskLog.liveTitle")} · {t("taskLog.logCount", { count: events.length })}
          </div>
        </div>
        <button
          type="button"
          onClick={copyLogs}
          className="rounded-full border border-[var(--border-soft)] bg-[var(--bg-pane)] px-3 py-1.5 text-xs font-medium text-[var(--text-secondary)] hover:text-[var(--text-primary)]"
        >
          {t("taskLog.copyLogs")}
        </button>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto rounded-xl border border-[var(--border-soft)] bg-[var(--bg-card)] p-3 font-mono text-xs shadow-sm">
        {events.length === 0 ? (
          <div className="flex h-full min-h-[180px] items-center justify-center rounded-lg border border-dashed border-[var(--border-soft)] text-[var(--text-secondary)]">
            {t("taskLog.waiting")}
          </div>
        ) : (
          <div className="flex flex-col gap-2">
            {groups.map((group) => {
              if (group.id === MAIN_GROUP_ID && group.events.length === 0) {
                return null;
              }
              return (
                <LogGroupView
                  key={group.id}
                  group={group}
                  collapsed={!!collapsed[group.id]}
                  isMain={group.id === MAIN_GROUP_ID}
                  onToggle={() => toggleGroup(group.id)}
                />
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}

/**
 * 单个分组（主任务或一个 worker）。
 *
 * 用 React 自身的虚拟 DOM diff 渲染日志列表，关键点：
 *   - 每条事件用稳定的 ``id`` 当 key（避免 React 整列重渲），
 *   - 折叠时 events 被卸载，DOM 不留滞；展开时完整展示后端已记录日志，
 *     便于出错后按原文排查请求与响应。
 */
function LogGroupView({
  group,
  collapsed,
  isMain,
  onToggle,
}: {
  group: LogGroup;
  collapsed: boolean;
  isMain: boolean;
  onToggle: () => void;
}) {
  const { t } = useI18n();
  const total = group.events.length;
  const visible = group.events;
  const bottomRef = useRef<HTMLDivElement>(null);

  // 展开时新事件到来自动滚到底部
  useEffect(() => {
    if (collapsed) return;
    bottomRef.current?.scrollIntoView({ behavior: "auto", block: "end" });
  }, [collapsed, total]);

  return (
    <div className="overflow-hidden rounded-lg border border-[var(--border-soft)] bg-[var(--bg-card)]">
      <button
        type="button"
        onClick={onToggle}
        className="flex w-full items-center gap-2 border-b border-[var(--border-soft)] bg-[var(--bg-pane)]/70 px-3 py-1.5 text-left text-[11px] font-medium uppercase tracking-[0.16em] text-[var(--text-secondary)] hover:text-[var(--text-primary)]"
      >
        {collapsed ? (
          <ChevronRight className="h-3.5 w-3.5" />
        ) : (
          <ChevronDown className="h-3.5 w-3.5" />
        )}
        <span className="truncate">
          {isMain ? t("taskLog.mainGroup") : group.label}
        </span>
        <span className="ml-auto text-[10px] text-[var(--text-secondary)]">
          {t("taskLog.logCount", { count: total })}
        </span>
      </button>
      {!collapsed && (
        <div className="px-2 py-2">
          <div className="space-y-1">
            {visible.map((ev) => (
              <LogLine key={ev.id} event={ev} />
            ))}
            <div ref={bottomRef} />
          </div>
        </div>
      )}
    </div>
  );
}

function LogLine({ event }: { event: LogEvent }) {
  const { t } = useI18n();
  const truncated = event.line.length > VISIBLE_LOG_LINE_LIMIT;
  const visibleLine = truncated
    ? `${getVisibleLine(event.line)}\n...`
    : event.line;

  return (
    <div
      className={`whitespace-pre-wrap break-words rounded-md border border-[var(--border-soft)] bg-[var(--bg-base)] px-3 py-1.5 leading-5 ${classifyLine(event.line)}`}
    >
      {visibleLine}
      {truncated ? (
        <div className="mt-1 border-t border-[var(--border-soft)] pt-1 text-[10px] font-sans text-[var(--text-secondary)]">
          {t("taskLog.lineTruncatedHint")}
        </div>
      ) : null}
    </div>
  );
}
