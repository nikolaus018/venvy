import globals from "globals";

/**
 * ESLint flat config for the Venvy frontend.
 *
 * The frontend is native ES modules served directly, with no bundler, so this
 * config exists purely to catch mistakes — nothing here rewrites the shipped
 * code.
 */
export default [
    {
        files: ["src/venvy/web/js/**/*.js"],
        languageOptions: {
            ecmaVersion: 2022,
            sourceType: "module",
            globals: {
                ...globals.browser,
                // Vendor libraries loaded from CDNs in index.html.
                ace: "readonly",
                Terminal: "readonly",
                FitAddon: "readonly",
                JSZip: "readonly",
                feather: "readonly",
                QRCode: "readonly",
                tailwind: "readonly",
            },
        },
        rules: {
            // Correctness — these catch real bugs.
            "no-undef": "error",
            "no-unused-vars": ["warn", { argsIgnorePattern: "^_", varsIgnorePattern: "^_" }],
            "no-redeclare": "error",
            "no-dupe-keys": "error",
            "no-dupe-args": "error",
            "no-unreachable": "error",
            "no-fallthrough": "error",
            "no-self-assign": "error",
            "no-constant-condition": ["error", { checkLoops: false }],
            "no-async-promise-executor": "error",
            "require-atomic-updates": "off",
            "use-isnan": "error",
            "valid-typeof": "error",

            // Style choices the codebase already follows.
            eqeqeq: ["warn", "smart"],
            "prefer-const": "warn",
            "no-var": "error",
        },
    },
    {
        // The PWA bootstrap runs as a classic script before the module graph
        // loads, so it is not an ES module.
        files: ["src/venvy/web/js/pwa-bootstrap.js"],
        languageOptions: { sourceType: "script" },
    },
];
