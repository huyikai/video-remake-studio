import { cva, type VariantProps } from "class-variance-authority";

export const buttonVariants = cva(
  "inline-flex items-center justify-center gap-1 whitespace-nowrap rounded-md text-sm font-medium transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-tungsten disabled:pointer-events-none disabled:opacity-50",
  {
    variants: {
      variant: {
        default: "bg-tungsten text-ink hover:opacity-90",
        destructive: "bg-bad text-ink hover:opacity-90",
        outline: "border border-line bg-surface text-text hover:border-tungsten/60 hover:bg-tungsten/10",
        ghost: "text-muted hover:text-text hover:bg-tungsten/10",
        secondary: "bg-panel text-text hover:bg-tungsten/15",
      },
      size: {
        default: "h-9 px-4 py-2",
        sm: "h-8 px-3 text-xs",
        lg: "h-10 px-6",
        icon: "h-9 w-9",
      },
    },
    defaultVariants: {
      variant: "default",
      size: "default",
    },
  },
);

export type ButtonVariants = VariantProps<typeof buttonVariants>;
