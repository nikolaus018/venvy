// Tailwind build configuration: scripts/build-css.sh turns the class names used
// in web/ into web/static/css/tailwind.css, which is committed and served as is.
// Class names are found by scanning these files, so write them out in full
// ('text-red-500', not 'text-' + color): a name that is assembled at runtime
// has no CSS.
module.exports = {
    content: ['./web/index.html', './web/partials/**/*.html', './web/static/js/**/*.js'],
    theme: {
        extend: {
            colors: {
                brand: { 50: '#eff6ff', 100: '#dbeafe', 500: 'var(--text)', 600: 'var(--text)', 700: '#1d4ed8' },
            },
        },
    },
};
