import { useTranslation } from 'react-i18next';
import { AlertCircle, X } from 'lucide-react';
import { useState, useEffect, useRef } from 'react';
import { Alert, AlertDescription } from '@/components/ui/alert';
import { backendClient } from '@/app/infra/http';

const RETRY_DELAYS_MS = [1000, 2000, 4000, 8000];
const CLOUD_BETA_RETRY_LIMIT = RETRY_DELAYS_MS.length;

export function BetaBanner() {
  const { t } = useTranslation();
  const bannerRef = useRef<HTMLDivElement>(null);
  const [show, setShow] = useState(false);
  const [dismissed, setDismissed] = useState(false);
  const visible = show && !dismissed;

  // Publish the banner height so the fixed app shell (sidebar + inset) starts
  // below the notice instead of painting over it.
  useEffect(() => {
    const root = document.documentElement;
    if (!visible) {
      root.style.removeProperty('--beta-banner-height');
      return;
    }
    const element = bannerRef.current;
    if (!element) {
      return;
    }
    const sync = () => {
      root.style.setProperty(
        '--beta-banner-height',
        `${element.getBoundingClientRect().height}px`,
      );
    };
    sync();
    const observer = new ResizeObserver(sync);
    observer.observe(element);
    return () => {
      observer.disconnect();
      root.style.removeProperty('--beta-banner-height');
    };
  }, [visible]);

  useEffect(() => {
    let cancelled = false;
    let retryTimer: number | undefined;

    const checkBetaStatus = async (attempt = 0) => {
      try {
        const info = await backendClient.getSystemInfo();
        if (cancelled) {
          return;
        }
        setShow(info.deployment_mode === 'cloud' && info.beta === true);
      } catch {
        if (cancelled) {
          return;
        }
        // A single failed probe (cold start, restarting backend, flaky
        // network) must not hide the banner for the rest of the session, so
        // keep the last known state and retry with backoff.
        if (attempt < CLOUD_BETA_RETRY_LIMIT) {
          retryTimer = window.setTimeout(
            () => void checkBetaStatus(attempt + 1),
            RETRY_DELAYS_MS[attempt],
          );
        }
      }
    };

    void checkBetaStatus();

    const recheck = () => {
      if (!cancelled) {
        void checkBetaStatus();
      }
    };
    window.addEventListener('focus', recheck);

    return () => {
      cancelled = true;
      clearTimeout(retryTimer);
      window.removeEventListener('focus', recheck);
    };
  }, []);

  if (!visible) {
    return null;
  }

  // The cloud product page opens the dedicated (paid v1) environment tab;
  // space.langbot.app resolves the locale itself, so no path prefix applies.
  const cloudUrl = 'https://space.langbot.app/cloud?environment=dedicated';
  const ossUrl = 'https://github.com/langbot-app/LangBot';

  return (
    <Alert
      ref={bannerRef}
      className="relative z-30 rounded-none border-x-0 border-t-0 bg-amber-50 dark:bg-amber-950/20 border-amber-200 dark:border-amber-900/50 py-2"
    >
      <AlertCircle className="text-amber-600 dark:text-amber-500" />
      <AlertDescription className="text-sm text-amber-900 dark:text-amber-100">
        <div className="flex w-full items-start gap-3">
          <span className="flex-1">
            {t('beta_banner.message')}{' '}
            <a
              href={cloudUrl}
              target="_blank"
              rel="noopener noreferrer"
              className="underline hover:text-amber-700 dark:hover:text-amber-300 font-medium"
            >
              {t('beta_banner.cloud_link')}
            </a>{' '}
            {t('beta_banner.or')}{' '}
            <a
              href={ossUrl}
              target="_blank"
              rel="noopener noreferrer"
              className="underline hover:text-amber-700 dark:hover:text-amber-300 font-medium"
            >
              {t('beta_banner.oss_link')}
            </a>
            {t('beta_banner.period')}
          </span>
          <button
            onClick={() => setDismissed(true)}
            className="shrink-0 text-amber-600 dark:text-amber-500 hover:text-amber-800 dark:hover:text-amber-300 p-1 -mt-1 -mr-2"
            aria-label={t('beta_banner.dismiss')}
          >
            <X className="h-4 w-4" />
          </button>
        </div>
      </AlertDescription>
    </Alert>
  );
}
