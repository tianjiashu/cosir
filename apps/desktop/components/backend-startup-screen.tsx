import { CosirMark } from "@/components/cosir-mark";
import "./backend-startup-screen.css";

type BackendStartupScreenProps = {
  exiting?: boolean;
};

/**
 * Presents the desktop boot gate while Rust starts and checks the local backend.
 * It owns no process or network work; `exiting` only fades the overlay after the
 * caller has observed backend readiness.
 */
export function BackendStartupScreen({ exiting = false }: BackendStartupScreenProps) {
  return (
    <div
      className="backend-startup-screen"
      data-state={exiting ? "exiting" : "starting"}
      aria-busy={!exiting}
      aria-hidden={exiting || undefined}
    >
      <div className="backend-startup-screen__grid" aria-hidden="true" />
      <div className="backend-startup-screen__scanline" aria-hidden="true" />
      <div className="backend-startup-screen__glow" aria-hidden="true" />
      <section
        className="backend-startup-screen__content"
        role={exiting ? undefined : "status"}
        aria-live={exiting ? undefined : "polite"}
      >
        <div className="backend-startup-screen__mark-wrap" aria-hidden="true">
          <div className="backend-startup-screen__orbit backend-startup-screen__orbit--outer">
            <div className="backend-startup-screen__orbit-spin">
              <span />
              <span />
            </div>
          </div>
          <div className="backend-startup-screen__orbit backend-startup-screen__orbit--inner">
            <div className="backend-startup-screen__orbit-spin">
              <span />
            </div>
          </div>
          <div className="backend-startup-screen__halo" />
          <CosirMark animated className="backend-startup-screen__mark" />
        </div>
        <h1 className="backend-startup-screen__wordmark">COSIR</h1>
        <div className="backend-startup-screen__status">
          <span className="backend-startup-screen__status-dot" aria-hidden="true" />
          <span>正在启动本机 Agent…</span>
          <span className="backend-startup-screen__status-pulse" aria-hidden="true">
            <i />
            <i />
            <i />
          </span>
        </div>
      </section>
    </div>
  );
}
