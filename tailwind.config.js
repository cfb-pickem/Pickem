/** @type {import('tailwindcss').Config} */
module.exports = {
  // Classes are written in the HTML *and* built inside JS template literals
  // (js/nav.js, the inline <script type="module"> blocks), so both are scanned.
  content: ['./*.html', './js/**/*.js'],
  theme: { extend: {} },
  plugins: [],
};
