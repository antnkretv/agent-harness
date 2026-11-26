import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import path from "node:path";
import { pathToFileURL } from "node:url";

const [root, app, config] = process.argv.slice(2);
const load = (file) => import(pathToFileURL(file).href);
const { LAYER_CONFIG_PREFIX, layerFolder, layerProbes, layerSource } = await load(
  path.join(root, ".config", "eslint-boundaries.js"),
);
const configs = [await (await load(path.join(app, config))).default].flat(Infinity);
const layered = configs.filter((entry) => entry?.name?.startsWith(LAYER_CONFIG_PREFIX));
const roots = [...new Set(layered.map((entry) => `${layerSource(entry)}/`))];
const { sources: given, modules } = JSON.parse(readFileSync(0, "utf8"));
const { ESLint } = createRequire(path.join(app, "package.json"))("eslint");
const eslint = new ESLint({ cwd: app });
const candidates = layered.length ? given.filter((file) => roots.some((dir) => file.startsWith(dir))) : given;
const sources = (await Promise.all(candidates.map(async (file) =>
  await eslint.isPathIgnored(path.join(app, file)) ? null : file,
))).filter(Boolean);
const folders = layered.map((entry) => `${layerFolder(entry)}/`);
const layerOf = (file) =>
  layered
    .filter((entry) => file.startsWith(`${layerFolder(entry)}/`))
    .sort((a, b) => layerFolder(b).length - layerFolder(a).length)[0];
const enforced = (value) => [2, "error"].includes(Array.isArray(value) ? value[0] : value);
const boundary = (entry, rules) =>
  Object.fromEntries(
    Object.entries(entry.rules)
      .filter(([, wanted]) => enforced(wanted))
      .map(([name, wanted]) => [name, { wanted, given: rules[name] ?? null }]),
  );
const rulesOf = async (file) => (await eslint.calculateConfigForFile(path.join(app, file)))?.rules ?? {};
const required = Object.assign(
  {},
  ...(await Promise.all(modules.map(async (file) => (await load(path.join(root, file))).required ?? {}))),
);
const [sourceRules, layers] = await Promise.all([
  Promise.all(sources.map(rulesOf)),
  Promise.all(
    layered.flatMap((entry) => layerProbes(entry).map(async (probe) => [probe, boundary(entry, await rulesOf(probe))])),
  ),
]);
const groups = {};
sources.forEach((file, index) => {
  const all = sourceRules[index];
  const rules = Object.fromEntries(Object.keys(required).map((name) => [name, all[name] ?? null]));
  const layer = layerOf(file);
  const imports = layer ? boundary(layer, all) : null;
  groups[JSON.stringify([rules, imports])] ??= { file, rules, imports };
});

console.log(JSON.stringify({
  required,
  empty: folders.filter((dir) => !sources.some((file) => file.startsWith(dir))),
  sources: Object.values(groups),
  layers: Object.fromEntries(layers),
}));
