import { useEffect, useRef } from "react";

/** One focus boundary for modal panels; no task or application state. */
export function useDialogFocus<T extends HTMLElement = HTMLElement>(returnSelector?: string, enabled = true) {
  const ref = useRef<T>(null);
  useEffect(() => {
    const root = ref.current;
    if (!root || !enabled) return;
    const previous = document.activeElement as HTMLElement | null;
    const items = () => [...root.querySelectorAll<HTMLElement>(
      'button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), summary, a[href], [tabindex]'
    )].filter(el => el.tabIndex >= 0 && !!el.getClientRects().length);
    const top = () => [...document.querySelectorAll('[role="dialog"]')].filter(el => el.getClientRects().length).pop() === root;
    root.tabIndex = -1;
    (root.querySelector<HTMLElement>('[autofocus], [role="tab"]') || items()[0] || root).focus();
    const redirect = () => { if (top() && !root.contains(document.activeElement)) (items()[0] || root).focus(); };
    const trap = (event: KeyboardEvent) => {
      if (event.key !== 'Tab' || !top()) return;
      const focusable = items(), first = focusable[0], last = focusable[focusable.length - 1];
      if (!first || !root.contains(document.activeElement) || event.shiftKey && document.activeElement === first || !event.shiftKey && document.activeElement === last) {
        event.preventDefault(); (event.shiftKey ? last || root : first || root).focus();
      }
    };
    window.addEventListener('keydown', trap, true);
    document.addEventListener('focusin', redirect);
    return () => {
      window.removeEventListener('keydown', trap, true);
      document.removeEventListener('focusin', redirect);
      if (previous?.isConnected) previous.focus();
      else if (returnSelector) document.querySelector<HTMLElement>(returnSelector)?.focus();
    };
  }, [returnSelector, enabled]);
  return ref;
}
