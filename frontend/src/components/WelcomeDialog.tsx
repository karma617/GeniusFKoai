import { useState } from 'react'
import { X } from 'lucide-react'
import { useI18n } from '@/lib/i18n-context'
import qrImage from '@/assets/qq-group.jpg'

const QQ_GROUP_URL = 'https://qm.qq.com/q/MfuBG14aI'

export default function WelcomeDialog() {
  const { t } = useI18n()
  const [open, setOpen] = useState(true)

  const close = () => {
    setOpen(false)
  }

  if (!open) return null

  return (
    <div
      className="dialog-backdrop fixed inset-0 z-[100] flex items-center justify-center p-4"
      onClick={close}
    >
      <div
        className="relative w-[60vw] rounded-2xl border border-[var(--border-soft)] bg-[var(--bg-card)] p-6 shadow-[var(--shadow-hard)]"
        onClick={(e) => e.stopPropagation()}
      >
        <button
          onClick={close}
          className="absolute right-3 top-3 rounded-md p-1.5 text-[var(--text-muted)] transition-colors hover:bg-[var(--bg-hover)] hover:text-[var(--text-primary)]"
          title={t('welcome.close')}
        >
          <X className="h-4 w-4" />
        </button>

        <h2 className="text-center text-lg font-semibold text-[var(--text-primary)]">
          {t('welcome.title')}
        </h2>
        <p className="mt-2 text-center text-sm text-[var(--text-muted)]">
          {t('welcome.desc')}
        </p>

        <div className="mt-4 flex justify-center">
          <img
            src={qrImage}
            alt={t('welcome.join')}
            className="h-56 w-56 rounded-lg border border-[var(--border)] object-cover"
          />
        </div>

        <a
          href={QQ_GROUP_URL}
          target="_blank"
          rel="noopener noreferrer"
          className="mt-4 block w-full rounded-lg bg-[var(--gradient-accent)] px-4 py-2.5 text-center text-sm font-medium text-white shadow-[var(--shadow-soft)] transition-all duration-200 hover:shadow-[var(--shadow-glow)] hover:brightness-105 active:scale-[0.98]"
        >
          {t('welcome.join')}
        </a>
        <button
          onClick={close}
          className="mt-2 block w-full rounded-lg px-4 py-2 text-center text-sm text-[var(--text-muted)] transition-colors hover:bg-[var(--bg-hover)] hover:text-[var(--text-primary)]"
        >
          {t('welcome.close')}
        </button>
      </div>
    </div>
  )
}
