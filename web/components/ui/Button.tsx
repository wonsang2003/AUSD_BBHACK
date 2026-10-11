/**
 * Button: the one button style set for the app.
 *
 * Variants: primary (green, the main action on a screen), secondary (outlined), danger (red:
 * pause, revoke) and ghost (text-only). `busy` disables the button and shows a trailing
 * ellipsis while a signature or request is in flight. Defaults to type="button" so a button
 * inside a form never submits it by accident.
 */

import type { ButtonHTMLAttributes } from "react";

import { cx } from "@/lib/ui";

type ButtonVariant = "primary" | "secondary" | "danger" | "ghost";
type ButtonSize = "sm" | "md";

const VARIANT_CLASSES: Record<ButtonVariant, string> = {
  primary: "border-up bg-up text-black hover:bg-up/85",
  secondary: "border-line bg-surface-2 text-fg hover:border-fg-dim",
  danger: "border-down/60 bg-down/15 text-down hover:bg-down/25",
  ghost: "border-transparent bg-transparent text-fg-muted hover:text-fg",
};

const SIZE_CLASSES: Record<ButtonSize, string> = {
  sm: "h-6 px-2 text-[11px]",
  md: "h-8 px-3 text-xs",
};

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  size?: ButtonSize;
  busy?: boolean;
}

export function Button({
  variant = "secondary",
  size = "md",
  busy = false,
  disabled,
  type = "button",
  className,
  children,
  ...rest
}: ButtonProps) {
  return (
    <button
      type={type}
      disabled={disabled || busy}
      aria-busy={busy}
      className={cx(
        "inline-flex items-center justify-center gap-1.5 whitespace-nowrap rounded-sm border font-semibold transition-colors",
        "focus-visible:outline focus-visible:outline-1 focus-visible:outline-info",
        "disabled:cursor-not-allowed disabled:opacity-50",
        VARIANT_CLASSES[variant],
        SIZE_CLASSES[size],
        className,
      )}
      {...rest}
    >
      {children}
      {busy && <span aria-hidden>…</span>}
    </button>
  );
}
