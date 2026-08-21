/** @type {import('tailwindcss').Config} */
module.exports = {
  // Classes are written in the HTML *and* built inside JS template literals
  // (js/nav.js, the inline <script type="module"> blocks), so both are scanned.
  // js/vendor/ is excluded: it's a minified third-party bundle and scanning it
  // extracts thousands of bogus class-name candidates.
  content: ['./*.html', './js/**/*.js', '!./js/vendor/**'],
  theme: { extend: {} },
  plugins: [],
};
