import { forwardRef, type ButtonHTMLAttributes } from "react";
import { Icon, type IconName } from "./Icon";

type Props = ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: "primary" | "secondary" | "ghost" | "danger";
  icon?: IconName;
};

export const Button = forwardRef<HTMLButtonElement, Props>(function Button(
  {
    variant = "secondary",
    icon,
    className = "",
    type = "button",
    children,
    ...props
  },
  ref,
) {
  return (
    <button
      ref={ref}
      type={type}
      className={`ui-button ui-button--${variant} ${className}`}
      {...props}
    >
      {icon && <Icon name={icon} />}
      {children}
    </button>
  );
});
