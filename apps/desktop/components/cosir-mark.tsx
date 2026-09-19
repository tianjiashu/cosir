type CosirMarkProps = {
  className?: string;
  animated?: boolean;
};

/**
 * Renders the COSIR brand mark. When `animated` is enabled, SVG motion elements
 * move the cyan node along the mark's C-shaped stroke; this is presentation only
 * and does not read or change application state.
 */
export function CosirMark({ className, animated = false }: CosirMarkProps) {
  return (
    <svg
      className={[className, animated && "cosir-mark-animated"].filter(Boolean).join(" ")}
      viewBox="0 0 64 64"
      fill="none"
      aria-hidden="true"
      xmlns="http://www.w3.org/2000/svg"
    >
      <path
        className={animated ? "cosir-mark-animated__stroke" : undefined}
        d="M48 15C26 7 11 15 11 32C11 49 26 57 48 49"
        stroke="currentColor"
        strokeWidth="10"
        strokeLinecap="round"
      />
      <path
        className={animated ? "cosir-mark-animated__arrow" : undefined}
        d="M25 24L34 32L25 40"
        stroke="currentColor"
        strokeWidth="4"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      {animated ? (
        <>
          <circle className="cosir-mark-animated__static-dot" cx="45" cy="32" r="4.5" fill="#22D3EE" />
          <circle className="cosir-mark-animated__runner" cx="45" cy="32" r="4.5" fill="#22D3EE">
            <animateMotion
              dur="2.8s"
              repeatCount="indefinite"
              path="M0 0 C1 -8 9 -14 3 -17 C-19 -25 -34 -17 -34 0 C-34 17 -19 25 3 17 C4 14 -1 8 0 0"
            />
          </circle>
        </>
      ) : (
        <circle cx="45" cy="32" r="4.5" fill="#22D3EE" />
      )}
    </svg>
  );
}
