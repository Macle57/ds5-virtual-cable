/* The page: warm white paper with a quiet dot grid (index.css) and a few
   pencil doodles in the margins -- a star, a spiral, a tiny pad. All static,
   all in the quiet-rule colour, so they read as marginalia, not content.
   They live in the gutters beside the 1320px page (index.css hides them when
   the viewport has no gutter to spare), measured from the page's centre. */
const gutter = (px: number) => `calc(50% - 660px - ${px}px)`;
export default function Atmosphere() {
  return (
    <div className="atmosphere" aria-hidden>
      {/* top-right: a five-point star drawn in one stroke */}
      <svg className="doodle sketch" style={{ right: gutter(90), top: 118, width: 54, height: 54 }} viewBox="0 0 54 54" fill="none"
           stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
        <path d="M27 5l6.5 14.5 15.5 1.5-11.5 10.5 3.5 15.5L27 39l-14 8 3.5-15.5L5 21l15.5-1.5z" />
      </svg>
      {/* left margin: a spiral */}
      <svg className="doodle sketch" style={{ left: gutter(84), top: "46%", width: 48, height: 48 }} viewBox="0 0 48 48" fill="none"
           stroke="currentColor" strokeWidth="2.5" strokeLinecap="round">
        <path d="M24 24c2-3 7-2 7 2 0 6-9 7-12 2-4-7 5-14 12-10 9 5 6 19-4 21-13 2-21-11-14-22 7-10 24-9 30 2" />
      </svg>
      {/* bottom-left: a tiny pad silhouette */}
      <svg className="doodle sketch" style={{ left: gutter(120), bottom: 48, width: 86, height: 56 }} viewBox="0 0 86 56" fill="none"
           stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
        <path d="M22 8h42c9 0 13 7 15 16l5 20c1 5-3 9-8 8-6-1-9-8-13-10H23c-4 2-7 9-13 10-5 1-9-3-8-8l5-20C9 15 13 8 22 8z" />
        <circle cx="30" cy="28" r="4" /><circle cx="56" cy="28" r="4" />
        <path d="M18 20v6M15 23h6M64 18l2 2M70 23l2 2" />
      </svg>
      {/* right margin, low: three motion dashes */}
      <svg className="doodle sketch" style={{ right: gutter(96), bottom: "24%", width: 60, height: 40 }} viewBox="0 0 60 40" fill="none"
           stroke="currentColor" strokeWidth="2.5" strokeLinecap="round">
        <path d="M4 10c14-4 28-4 42 0M8 22c12-3 26-3 40 0M14 33c10-2 20-2 30 0" />
      </svg>
    </div>
  );
}
