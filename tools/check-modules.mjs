/**
 * Verify the frontend module graph without running a browser.
 *
 * The frontend ships as native ES modules with no bundler, so nothing catches a
 * broken import at build time — the first sign would be a blank page. This
 * checks three things that together cover the ways the graph can break:
 *
 *   1. every file parses as an ES module,
 *   2. every import resolves to a file that really exports that name,
 *   3. every `onclick="foo()"` in index.html reaches `window` via main.js.
 *
 * Run it with `npm run check:modules`.
 */
import * as acorn from "acorn";
import * as walk from "acorn-walk";
import fs from "node:fs";
import path from "node:path";
import process from "node:process";

const JS_DIR = "src/venvy/web/js";
const HTML = "src/venvy/web/index.html";

/** Names the browser or a CDN library provides, so they need no import. */
const AMBIENT = new Set([
    // ECMAScript
    "undefined", "NaN", "Infinity", "Object", "Array", "String", "Number", "Boolean", "Symbol",
    "Math", "JSON", "Date", "RegExp", "Error", "TypeError", "RangeError", "Promise", "Map", "Set",
    "WeakMap", "WeakSet", "Proxy", "Reflect", "BigInt", "Intl", "globalThis", "parseInt",
    "parseFloat", "isNaN", "isFinite", "encodeURIComponent", "decodeURIComponent", "encodeURI",
    "decodeURI", "structuredClone", "queueMicrotask", "AggregateError",
    // DOM and BOM
    "window", "document", "console", "navigator", "location", "history", "screen", "localStorage",
    "sessionStorage", "fetch", "Headers", "Request", "Response", "FormData", "URL",
    "URLSearchParams", "Blob", "File", "FileReader", "XMLHttpRequest", "WebSocket", "EventSource",
    "AbortController", "AbortSignal", "setTimeout", "clearTimeout", "setInterval", "clearInterval",
    "requestAnimationFrame", "cancelAnimationFrame", "alert", "confirm", "prompt", "atob", "btoa",
    "getComputedStyle", "matchMedia", "ResizeObserver", "MutationObserver",
    "IntersectionObserver", "CustomEvent", "Event", "KeyboardEvent", "MouseEvent", "TouchEvent",
    "Element", "HTMLElement", "Node", "NodeList", "DOMParser", "TextDecoder", "TextEncoder",
    "Image", "Audio", "Worker", "crypto", "performance", "visualViewport", "open", "scrollTo",
    "CSS", "addEventListener", "removeEventListener", "DataTransfer", "ClipboardItem",
    // Vendor libraries loaded from CDNs in index.html
    "ace", "Terminal", "FitAddon", "JSZip", "feather", "QRCode", "tailwind",
]);

/** Identifiers that appear in inline handlers but are not app functions. */
const NOT_HANDLERS = new Set([
    "if", "for", "while", "return", "typeof", "function", "catch", "switch",
    "JSON", "Number", "String", "parseInt", "parseFloat", "Boolean", "Array", "Object", "Math",
    "event", "focus", "getElementById", "alert", "confirm", "setTimeout", "classList",
]);

const listJs = (dir) =>
    fs.readdirSync(dir, { withFileTypes: true }).flatMap((entry) =>
        entry.isDirectory()
            ? listJs(path.join(dir, entry.name))
            : entry.name.endsWith(".js")
                ? [path.join(dir, entry.name)]
                : []);

const problems = [];
const report = (message) => problems.push(message);

// ── Parse every module ──────────────────────────────────────────────────────
const modules = new Map(); // relative path -> { ast, exports, imports, source }

for (const file of listJs(JS_DIR)) {
    const rel = path.relative(JS_DIR, file).split(path.sep).join("/");
    const source = fs.readFileSync(file, "utf8");
    // pwa-bootstrap runs as a classic script, before the module graph loads.
    const sourceType = rel === "pwa-bootstrap.js" ? "script" : "module";
    let ast;
    try {
        ast = acorn.parse(source, { ecmaVersion: 2022, sourceType, locations: true });
    } catch (error) {
        report(`${rel}: parse error — ${error.message}`);
        continue;
    }

    const exports = new Set();
    const imports = [];
    for (const node of ast.body) {
        if (node.type === "ExportNamedDeclaration" && node.declaration) {
            const declaration = node.declaration;
            if (declaration.type === "FunctionDeclaration") exports.add(declaration.id.name);
            if (declaration.type === "VariableDeclaration") {
                for (const v of declaration.declarations) {
                    if (v.id.type === "Identifier") exports.add(v.id.name);
                }
            }
        }
        if (node.type === "ImportDeclaration") {
            imports.push({
                from: node.source.value,
                names: node.specifiers.map((s) => (s.imported ? s.imported.name : s.local.name)),
                line: node.loc.start.line,
            });
        }
    }
    modules.set(rel, { ast, exports, imports, sourceType });
}

// ── Every import must resolve ───────────────────────────────────────────────
for (const [rel, mod] of modules) {
    for (const { from, names, line } of mod.imports) {
        const target = path.posix.normalize(path.posix.join(path.posix.dirname(rel), from));
        const resolved = modules.get(target);
        if (!resolved) {
            report(`${rel}:${line}: imports from '${from}', which does not exist`);
            continue;
        }
        for (const name of names) {
            if (!resolved.exports.has(name)) {
                report(`${rel}:${line}: '${name}' is not exported by '${from}'`);
            }
        }
    }
}

// ── No identifier may be referenced without being declared or imported ──────
for (const [rel, mod] of modules) {
    const declared = new Set();
    for (const node of mod.ast.body) {
        if (node.type === "ImportDeclaration") {
            for (const s of node.specifiers) declared.add(s.local.name);
        }
    }
    const addPattern = (pattern) => {
        if (!pattern) return;
        if (pattern.type === "Identifier") declared.add(pattern.name);
        else if (pattern.type === "AssignmentPattern") addPattern(pattern.left);
        else if (pattern.type === "RestElement") addPattern(pattern.argument);
        else if (pattern.type === "ObjectPattern") pattern.properties.forEach((p) => addPattern(p.value ?? p.argument));
        else if (pattern.type === "ArrayPattern") pattern.elements.forEach(addPattern);
    };

    walk.full(mod.ast, (node) => {
        if (node.type === "VariableDeclarator") addPattern(node.id);
        if (node.type === "CatchClause") addPattern(node.param);
        if (["FunctionDeclaration", "FunctionExpression", "ClassDeclaration"].includes(node.type) && node.id) {
            declared.add(node.id.name);
        }
        if (["FunctionDeclaration", "FunctionExpression", "ArrowFunctionExpression"].includes(node.type)) {
            node.params.forEach(addPattern);
        }
    });

    const free = new Set();
    walk.ancestor(mod.ast, {
        Identifier(node, _state, ancestors) {
            const parent = ancestors[ancestors.length - 2];
            if (parent?.type === "MemberExpression" && parent.property === node && !parent.computed) return;
            if (parent?.type === "Property" && parent.key === node && !parent.computed) return;
            if (parent?.type === "ImportSpecifier") return;
            if (["LabeledStatement", "BreakStatement", "ContinueStatement"].includes(parent?.type)) return;
            if (declared.has(node.name) || AMBIENT.has(node.name)) return;
            free.add(node.name);
        },
    });
    for (const name of free) {
        report(`${rel}: '${name}' is used but never declared or imported`);
    }
}

// ── Inline handlers in index.html must reach window ─────────────────────────
const html = fs.readFileSync(HTML, "utf8");
const handlers = new Set();
for (const match of html.matchAll(/on[a-z]+="([^"]*)"/g)) {
    for (const call of match[1].matchAll(/([A-Za-z_$][A-Za-z0-9_$]*)\s*\(/g)) {
        if (!NOT_HANDLERS.has(call[1])) handlers.add(call[1]);
    }
}

const main = modules.get("main.js");
if (!main) {
    report("main.js is missing");
} else {
    const bridged = new Set();
    walk.simple(main.ast, {
        CallExpression(node) {
            const callee = node.callee;
            const isAssign =
                callee.type === "MemberExpression" &&
                callee.object.name === "Object" &&
                callee.property.name === "assign";
            if (!isAssign || node.arguments[0]?.name !== "window") return;
            for (const arg of node.arguments.slice(1)) {
                if (arg.type !== "ObjectExpression") continue;
                for (const property of arg.properties) {
                    const key = property.key ?? property.argument;
                    if (key?.name) bridged.add(key.name);
                }
            }
        },
    });
    for (const handler of handlers) {
        if (!bridged.has(handler)) {
            report(`index.html uses onclick="${handler}()" but main.js does not put it on window`);
        }
    }
}

// ── Report ──────────────────────────────────────────────────────────────────
if (problems.length) {
    console.error(`\n${problems.length} problem(s) found:\n`);
    for (const problem of problems) console.error("  " + problem);
    process.exit(1);
}
console.log(
    `Module graph OK — ${modules.size} modules, ` +
    `${handlers.size} inline handlers bridged, all imports resolve.`
);
