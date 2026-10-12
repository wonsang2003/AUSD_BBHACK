/**
 * TermButton: the terminal's command button — square, uppercase, bold.
 *
 * Props:
 *   variant  "default" (amber outline on black) | "primary" (solid amber, black text)
 *            | "go" (solid green, for <GO> / run) | "danger" (red outline: stop, revoke)
 *   size     "sm" | "md" (default) | "lg"
 *   busy     disables the button and shows "WORKING…" instead of the label
 *   ...      every native <button> prop (onClick, disabled, title, type — defaults to "button")
 */

import type { ButtonHTMLAttributes } from "react";

import { cls } from "./tones";

type TermButtonVariant = "default" | "primary" | "go" | "danger";
type TermButtonSize = "sm" | "md" | "lg";

interface TermButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: TermButtonVariant;
  size?: TermButtonSize;
  busy?: boolean;
}

const VARIANT_CLASS: Record<TermButtonVariant, string | undefined> = {
  default: undefined,
  primary: "t-btn--primary",
  go: "t-btn--go",
  danger: "t-btn--danger",
};

const SIZE_CLASS: Record<TermButtonSize, string | undefined> = {
  sm: "t-btn--sm",
  md: undefined,
  lg: "t-btn--lg",
};

export function TermButton({
  variant = "default",
  size = "md",
  busy = false,
  disabled,
  className,
  type = "button",
  children,
  ...rest
}: TermButtonProps) {
  return (
    <button
      type={type}
      disabled={disabled || busy}
      aria-busy={busy}
      className={cls("t-btn", VARIANT_CLASS[variant], SIZE_CLASS[size], className)}
      {...rest}
    >
      {busy ? <span className="t-blink">WORKING…</span> : children}
    </button>
  );
}
