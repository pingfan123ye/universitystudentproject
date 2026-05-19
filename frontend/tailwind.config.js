/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,ts,jsx,tsx}"],
  theme: {
    extend: {
      colors: {
        surface: {
          0: '#0a0a0f',
          1: '#111118',
          2: '#16161f',
          3: '#1c1c28',
          4: '#222233',
        },
        accent: {
          amber: '#a78bfa',
          'amber-dim': '#7c3aed',
          blue: '#60a5fa',
          green: '#4ade80',
          rose: '#fb7185',
          purple: '#a78bfa',
          cyan: '#22d3ee',
        },
      },
      fontFamily: {
        display: ['"DM Serif Display"', 'Georgia', 'serif'],
        body: ['"DM Sans"', 'system-ui', 'sans-serif'],
        mono: ['"JetBrains Mono"', 'ui-monospace', 'monospace'],
      },
      animation: {
        'glow-pulse': 'glow-pulse 3s ease-in-out infinite',
        'slide-up': 'slide-up 0.4s cubic-bezier(0.16, 1, 0.3, 1)',
        'fade-in': 'fade-in 0.3s ease-out',
        'breathe': 'breathe 4s ease-in-out infinite',
        'wave': 'wave 2s ease-in-out infinite',
      },
      keyframes: {
        'glow-pulse': {
          '0%, 100%': { boxShadow: '0 0 20px rgba(240,168,64,0.15)' },
          '50%': { boxShadow: '0 0 40px rgba(240,168,64,0.35)' },
        },
        'slide-up': {
          '0%': { transform: 'translateY(12px)', opacity: '0' },
          '100%': { transform: 'translateY(0)', opacity: '1' },
        },
        'fade-in': {
          '0%': { opacity: '0' },
          '100%': { opacity: '1' },
        },
        'breathe': {
          '0%, 100%': { opacity: '0.4' },
          '50%': { opacity: '1' },
        },
        'wave': {
          '0%, 100%': { transform: 'scaleY(0.3)' },
          '50%': { transform: 'scaleY(1)' },
        },
      },
      backdropBlur: {
        xs: '2px',
      },
    },
  },
  plugins: [],
}
