import { createContext, useContext, useCallback, useState, useRef } from "react";
import { Icon } from "../components/ui";

/* ──────────────────────────────────────────────────────────────────────────
 * layout/ToastHost.jsx
 * Reusable notification (toast) system for the Enterprise UI Shell.
 * Any component calls `const toast = useToast()` then
 * `toast.success(msg)` / `.warning` / `.error` / `.info`.
 * Toasts auto-dismiss; errors persist a little longer. No business logic.
 * ────────────────────────────────────────────────────────────────────────── */

const ToastContext = createContext(null);

const VARIANT = {
  success: { color: "var(--ok)",   icon: "check",    ttl: 4000 },
  warning: { color: "var(--warn)", icon: "alert",    ttl: 5500 },
  error:   { color: "var(--err)",  icon: "x",        ttl: 7000 },
  info:    { color: "var(--info)", icon: "activity", ttl: 4500 },
};

export function ToastProvider({ children }) {
  const [toasts, setToasts] = useState([]);
  const idRef = useRef(0);

  const dismiss = useCallback((id) => {
    setToasts((list) => list.filter((t) => t.id !== id));
  }, []);

  const push = useCallback((variant, title, detail) => {
    const id = ++idRef.current;
    const meta = VARIANT[variant] || VARIANT.info;
    setToasts((list) => [...list, { id, variant, title, detail }]);
    if (meta.ttl) setTimeout(() => dismiss(id), meta.ttl);
    return id;
  }, [dismiss]);

  const api = {
    push,
    dismiss,
    success: (t, d) => push("success", t, d),
    warning: (t, d) => push("warning", t, d),
    error:   (t, d) => push("error", t, d),
    info:    (t, d) => push("info", t, d),
  };

  return (
    <ToastContext.Provider value={api}>
      {children}
      <div className="aeam-toast-host" role="region" aria-label="Notifications" aria-live="polite">
        {toasts.map((t) => {
          const meta = VARIANT[t.variant] || VARIANT.info;
          return (
            <div key={t.id} className="aeam-toast" style={{ "--tc": meta.color }}>
              <span className="aeam-toast-icon"><Icon name={meta.icon} size={15} color={meta.color} /></span>
              <div className="aeam-toast-body">
                <div className="aeam-toast-title">{t.title}</div>
                {t.detail && <div className="aeam-toast-detail">{t.detail}</div>}
              </div>
              <button className="aeam-toast-close" onClick={() => dismiss(t.id)} aria-label="Dismiss">
                <Icon name="x" size={13} />
              </button>
            </div>
          );
        })}
      </div>
    </ToastContext.Provider>
  );
}

export function useToast() {
  const ctx = useContext(ToastContext);
  // Safe no-op fallback so a component can call useToast() even if it somehow
  // renders outside the provider (never crashes the shell).
  return ctx || { push() {}, dismiss() {}, success() {}, warning() {}, error() {}, info() {} };
}
