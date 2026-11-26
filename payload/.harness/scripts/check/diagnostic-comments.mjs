import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { join } from "node:path";
import { pathToFileURL } from "node:url";

const modules = new Map();
const load = (app, name) => {
  const url = pathToFileURL(createRequire(join(app, "package.json")).resolve(name)).href;
  if (!modules.has(url)) modules.set(url, import(url));
  return modules.get(url);
};

const groups = JSON.parse(readFileSync(0, "utf8"));
const directive = /^\s*(?:\*\s*)?(?:eslint-disable(?:-next-line|-line)?\b|eslint(?=\s|$)|@ts-(?:ignore|nocheck|expect-error)\b)/m;

const astroComments = (parseAstro, path, source) => {
  const result = parseAstro(source);
  const errors = result.diagnostics.filter((diagnostic) => diagnostic.severity === "error");
  if (errors.length) throw new Error(`${path}: ${JSON.stringify(errors)}`);
  const comments = [...result.ast.comments];
  const visit = (node) => {
    if (!node || typeof node !== "object") return;
    if (node.type === "AstroComment") comments.push(node);
    for (const [key, value] of Object.entries(node)) {
      if (key === "comments") continue;
      if (Array.isArray(value)) value.forEach(visit);
      else visit(value);
    }
  };
  visit(result.ast);
  return comments;
};

const findings = [];
const unresolved = [];
for (const [app, paths] of Object.entries(groups)) {
  let parseTypeScript, parseAstro;
  try {
    parseTypeScript = (await load(app, "@typescript-eslint/parser")).parse;
    parseAstro = paths.some((path) => path.endsWith(".astro")) ? (await load(app, "@astrojs/compiler-rs")).parse : null;
  } catch {
    unresolved.push(app);
    continue;
  }
  for (const path of paths) {
    const source = readFileSync(path, "utf8");
    const comments = path.endsWith(".astro")
      ? astroComments(parseAstro, path, source)
      : parseTypeScript(source, {
        filePath: path,
        comment: true,
        loc: true,
        range: true,
        sourceType: "module",
        ecmaFeatures: { jsx: true },
      }).comments;
    for (const comment of comments) {
      if (!directive.test(comment.value)) continue;
      const line = comment.loc?.start.line ?? source.slice(0, comment.start).split("\n").length;
      findings.push({ path, line });
    }
  }
}
process.stdout.write(JSON.stringify({ findings, unresolved }));
