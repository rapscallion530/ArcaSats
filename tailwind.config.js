/** Tailwind config for the PACKAGED build (compiled by the standalone CLI in the
 *  Dockerfile). Dev uses the Play CDN with an equivalent inline config in base.html.
 *  Colors map to the CSS variables in static/tokens.css so dark mode is a var swap. */
module.exports = {
  darkMode: 'class',
  content: ['./app/templates/**/*.html'],
  // Translucent utilities use fixed colors here (var() colors don't support /opacity).
  safelist: [
    'border-line/60', 'border-warn/50', 'bg-warn/10', 'text-darkcopy',
  ],
  theme: {
    extend: {
      colors: {
        page: 'var(--page)', surface: 'var(--surface)', surfaceblue: 'var(--surface-blue)',
        navbg: 'var(--nav-bg)', inputbg: 'var(--input-bg)',
        ink: 'var(--ink)', heading: 'var(--heading)', headingalt: 'var(--heading-alt)', muted: 'var(--muted)',
        line: 'var(--line)',
        accent: { DEFAULT: 'var(--accent)', deep: 'var(--accent-deep)' },
        darkbg: 'var(--dark-bg)', darkink: 'var(--dark-ink)', darkcopy: 'var(--dark-copy)',
        tagbg: 'var(--tag-bg)', tagink: 'var(--tag-ink)',
        gain: 'var(--gain)', loss: 'var(--loss)', warn: 'var(--warn)',
        btnbg: 'var(--btn-bg)', btnbghover: 'var(--btn-bg-hover)', btnink: 'var(--btn-ink)',
      },
      fontFamily: {
        display: ['Georgia', 'Trebuchet MS', 'serif'],
        body: ['Inter', 'Segoe UI', 'system-ui', 'sans-serif'],
      },
      borderRadius: { card: '14px' },
    },
  },
};
