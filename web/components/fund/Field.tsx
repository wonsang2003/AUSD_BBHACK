/**
 * Field / TextInput: a labelled compact input for the /fund and /agents forms.
 * `suffix` shows the unit (AUSD, USD, days) to the right; `hint` is a dim line under the label.
 */

import type { InputHTMLAttributes, ReactNode } from "react";

import { cx } from "@/lib/ui";

interface FieldProps {
  label: ReactNode;
  hint?: ReactNode;
  className?: string;
  children: ReactNode;
}

export function Field({ label, hint, className, children }: FieldProps) {
  return (
    <label className={cx("flex min-w-0 flex-col gap-0.5", className)}>
      <span className="text-[10px] font-semibold uppercase tracking-wider text-fg-dim">{label}</span>
      {children}
      {hint !== undefined && <span className="text-[10px] text-fg-dim">{hint}</span>}
    </label>
  );
}

interface TextInputProps extends InputHTMLAttributes<HTMLInputElement> {
  suffix?: string;
}

export function TextInput({ suffix, className, ...rest }: TextInputProps) {
  return (
    <span className="flex items-center rounded-sm border border-line bg-bg focus-within:border-info">
      <input {...rest} className={cx("num h-7 min-w-0 flex-1 bg-transparent px-2 text-xs outline-none", className)} />
      {suffix && <span className="px-1.5 text-[10px] text-fg-dim">{suffix}</span>}
    </span>
  );
}
