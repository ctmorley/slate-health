/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,ts,jsx,tsx}"],
  theme: {
    extend: {
      colors: {
        slate: {
          brand: "#0f766e",
          "brand-light": "#14b8a6",
          "brand-dark": "#0d5c56",
        },
      },
    },
  },
  plugins: [],
};
