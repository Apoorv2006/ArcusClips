/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        ink: {
          950: "#0a0b0f",
          900: "#101218",
          850: "#151822",
          800: "#1b1f2a",
          700: "#262b38",
          600: "#39404f",
          500: "#5b6373",
          400: "#8a93a6",
          300: "#b9c0d0",
          200: "#d8dce6",
          100: "#eef0f5",
        },
        accent: {
          DEFAULT: "#6ee7c7",
          soft: "#34d399",
          deep: "#059669",
        },
        warn: "#fbbf24",
        danger: "#f87171",
      },
      fontFamily: {
        sans: ["Inter", "system-ui", "Segoe UI", "sans-serif"],
        mono: ["JetBrains Mono", "ui-monospace", "monospace"],
      },
    },
  },
  plugins: [],
};
