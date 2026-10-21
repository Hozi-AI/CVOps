import type { Config } from 'tailwindcss'

export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  // CSS-variable-driven theming. Components write `bg-surface-2 text-text-primary`;
  // the actual values come from `--surface-2` / `--text-primary` set in index.css,
  // which switch on the `data-theme` attribute on <html> (dark is the default).
  theme: {
    extend: {
      fontFamily: {
        sans: ['Geist', 'Inter', 'system-ui', '-apple-system', 'sans-serif'],
        mono: ['Geist Mono', 'JetBrains Mono', 'ui-monospace', 'monospace'],
      },
      colors: {
        // ── Brand primary: iris violet (the platform) ──────────────────────
        iris: {
          50:  '#F1EFFE',
          100: '#E4E0FD',
          200: '#CAC2FB',
          300: '#ABA0F9',
          400: '#8E80FF',
          500: '#7B6CF6',
          600: '#5D4FE0',
          700: '#4A3DC4',
          800: '#3B319C',
          900: '#2E2778',
          DEFAULT: '#7B6CF6',
          hover:   '#8E80FF',
        },

        // ── Signal: lime (your data, alive — live/active only) ─────────────
        signal: {
          400: '#D4F77A',
          500: '#C6F24E',
          600: '#B4E62B',
          DEFAULT: '#C6F24E',
        },

        // ── Neutral raw colors (still available for one-off needs) ─────────
        ink:   '#0A0B0D',
        mist:  '#A2A9B6',

        // ── Semantic, theme-aware tokens (resolve via CSS vars) ────────────
        // These are what app code should reach for by default.
        surface: {
          1: 'var(--surface-1)',  // page background
          2: 'var(--surface-2)',  // card / panel
          3: 'var(--surface-3)',  // modal / popover / elevated
          inverse: 'var(--surface-inverse)',
        },
        text: {
          primary:   'var(--text-primary)',
          secondary: 'var(--text-secondary)',
          muted:     'var(--text-muted)',
          inverse:   'var(--text-inverse)',
          onAccent:  'var(--text-on-accent)',
        },
        border: {
          DEFAULT: 'var(--border)',
          strong:  'var(--border-strong)',
        },

        // ── Semantic feedback / run status ─────────────────────────────────
        success: '#34D399',
        warning: '#FBBF24',
        error:   '#FB7185',
        info:    '#38BDF8',

        // ── Data-viz categorical (iris + signal lead, colorblind-safer) ────
        // Values live in index.css (--chart-N) so they switch with the theme.
        chart: {
          1: 'var(--chart-1)',  // iris
          2: 'var(--chart-2)',  // sky
          3: 'var(--chart-3)',  // emerald
          4: 'var(--chart-4)',  // amber
          5: 'var(--chart-5)',  // pink
          6: 'var(--chart-6)',  // signal
          7: 'var(--chart-7)',  // mist
          8: 'var(--chart-8)',  // teal
        },
      },

      borderColor: {
        DEFAULT: 'var(--border)',
      },

      ringColor: {
        focus: 'var(--focus)',
      },

      boxShadow: {
        sm: '0 1px 2px 0 var(--shadow-color)',
        DEFAULT: '0 1px 3px 0 var(--shadow-color), 0 1px 2px -1px var(--shadow-color)',
        md: '0 4px 6px -1px var(--shadow-color), 0 2px 4px -2px var(--shadow-color)',
        lg: '0 8px 30px var(--shadow-color)',
      },

      backgroundImage: {
        'cv-signature': 'linear-gradient(135deg, #7B6CF6 0%, #C6F24E 100%)',
      },
    },
  },
  plugins: [],
} satisfies Config
