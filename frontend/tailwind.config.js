/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,ts,jsx,tsx}"],
  theme: {
    extend: {
      colors: {
        void: {
          0: '#000000',
          1: '#050508',
          2: '#0a0a10',
          3: '#0f0f18',
        },
        cyan: {
          glow: '#00f0ff',
          electric: '#00c8d6',
          dim: '#006870',
        },
      },
      fontFamily: {
        display: ['"Space Grotesk"', 'system-ui', 'sans-serif'],
        mono: ['"PT Mono"', '"Courier New"', 'monospace'],
      },
      animation: {
        'scan': 'scan 8s linear infinite',
        'blink': 'blink 1s step-end infinite',
        'slide-up': 'slide-up 0.3s cubic-bezier(0,0,0.2,1)',
        'flicker': 'flicker 0.15s ease-out',
        'pulse-cyan': 'pulse-cyan 2s ease-in-out infinite',
        'marquee': 'marquee 20s linear infinite',
      },
      keyframes: {
        'scan': {
          '0%': { transform: 'translateY(-100%)' },
          '100%': { transform: 'translateY(100vh)' },
        },
        'blink': {
          '0%, 100%': { opacity: '1' },
          '50%': { opacity: '0' },
        },
        'slide-up': {
          '0%': { transform: 'translateY(8px)', opacity: '0' },
          '100%': { transform: 'translateY(0)', opacity: '1' },
        },
        'flicker': {
          '0%': { opacity: '0' },
          '50%': { opacity: '0.8' },
          '51%': { opacity: '1' },
          '100%': { opacity: '1' },
        },
        'pulse-cyan': {
          '0%, 100%': { boxShadow: '0 0 2px #00f0ff40, inset 0 0 2px #00f0ff20' },
          '50%': { boxShadow: '0 0 8px #00f0ff60, inset 0 0 4px #00f0ff30' },
        },
        'marquee': {
          '0%': { transform: 'translateX(0)' },
          '100%': { transform: 'translateX(-50%)' },
        },
      },
    },
  },
  plugins: [],
}
