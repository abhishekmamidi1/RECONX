import { forwardRef, type ButtonHTMLAttributes } from "react";

import { cn } from "@/lib/utils";

type Variant = "primary" | "secondary" | "ghost" | "danger";
type Size = "sm" | "md";

const variantClasses: Record<Variant, string> = {
  primary:
    "bg-green-600 text-white hover:bg-green-500 shadow-[inset_0_1px_0_rgba(255,255,255,0.16)] hover:shadow-[inset_0_1px_0_rgba(255,255,255,0.2),0_4px_14px_-6px_rgba(16,185,129,0.5)] disabled:bg-green-950 disabled:text-green-300/60 disabled:shadow-none",
  secondary:
    "bg-slate-800 text-slate-100 hover:bg-slate-700 border border-slate-700 hover:shadow-[0_4px_14px_-8px_rgba(0,0,0,0.6)]",
  ghost: "bg-transparent text-slate-300 hover:bg-slate-800",
  danger: "bg-rose-600 text-white hover:bg-rose-500 shadow-[inset_0_1px_0_rgba(255,255,255,0.14)] hover:shadow-[inset_0_1px_0_rgba(255,255,255,0.18),0_4px_14px_-6px_rgba(244,63,94,0.5)]",
};

const sizeClasses: Record<Size, string> = {
  sm: "h-9 px-3.5 text-[13px]",
  md: "h-11 px-5 text-sm",
};

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant;
  size?: Size;
}

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant = "primary", size = "md", ...props }, ref) => (
    <button
      ref={ref}
      className={cn(
        "inline-flex select-none items-center justify-center gap-2 rounded-md font-medium transition-[color,background-color,border-color,box-shadow,transform] hover:-translate-y-px focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-green-500 active:translate-y-0 active:scale-[0.98] disabled:pointer-events-none disabled:opacity-70",
        variantClasses[variant],
        sizeClasses[size],
        className,
      )}
      {...props}
    />
  ),
);

Button.displayName = "Button";
