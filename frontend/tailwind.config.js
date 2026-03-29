/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,ts,jsx,tsx}"],
  theme: {
    extend: {
      colors: {
        slate_d: {
          900: "#080C14",
          800: "#0C1322",
          700: "#111B30",
          600: "#182440",
          500: "#1E2D4A",
        },
        accent: {
          900: "#3730A3",
          800: "#4F46E5",
          700: "#6366F1",
          600: "#818CF8",
          500: "#A5B4FC",
          400: "#C7D2FE",
        },
        mint: {
          700: "#059669",
          600: "#10B981",
          500: "#34D399",
          400: "#6EE7B7",
        },
        coral: {
          600: "#F43F5E",
          500: "#FB7185",
        },
      },
      fontFamily: {
        display: ['"Plus Jakarta Sans"', "Inter", "system-ui", "sans-serif"],
        sans: ["Inter", "system-ui", "sans-serif"],
        mono: ['"JetBrains Mono"', "ui-monospace", "monospace"],
      },
      borderColor: {
        glass: "rgba(255,255,255,0.06)",
        "glass-hover": "rgba(99,102,241,0.2)",
      },
      backgroundColor: {
        glass: "rgba(255,255,255,0.02)",
        "glass-hover": "rgba(255,255,255,0.04)",
      },
      boxShadow: {
        dark: "0 4px 24px rgba(0,0,0,0.4)",
        "dark-sm": "0 2px 8px rgba(0,0,0,0.3)",
      },
    },
  },
  plugins: [],
};
