/* The backdrop: aurora orbs drifting behind a faint grid. Pure CSS
   animation, so it costs nothing on the main thread. */
export default function Atmosphere() {
  return (
    <div className="atmosphere" aria-hidden>
      <div className="orb" style={{ width: 520, height: 520, left: "-8%", top: "-18%", background: "var(--color-cross)", animationDelay: "0s" }} />
      <div className="orb" style={{ width: 420, height: 420, right: "-6%", top: "-10%", background: "var(--color-square)", opacity: 0.28, animationDelay: "-9s" }} />
      <div className="orb" style={{ width: 460, height: 460, left: "45%", bottom: "-30%", background: "var(--color-cyan)", opacity: 0.22, animationDelay: "-17s" }} />
    </div>
  );
}
