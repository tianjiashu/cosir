type CosirMarkProps = {
  className?: string;
};

export function CosirMark({ className }: CosirMarkProps) {
  return (
    <svg
      className={className}
      viewBox="0 0 64 64"
      fill="none"
      aria-hidden="true"
      xmlns="http://www.w3.org/2000/svg"
    >
      <path
        d="M48 15C26 7 11 15 11 32C11 49 26 57 48 49"
        stroke="currentColor"
        strokeWidth="10"
        strokeLinecap="round"
      />
      <path
        d="M25 24L34 32L25 40"
        stroke="currentColor"
        strokeWidth="4"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <circle cx="45" cy="32" r="4.5" fill="#22D3EE" />
    </svg>
  );
}
