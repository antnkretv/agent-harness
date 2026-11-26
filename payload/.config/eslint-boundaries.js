import { layerBoundaryViolated } from "./messages.mjs";

export const LAYER_CONFIG_PREFIX = "harness/layer-boundaries/";
const layerName = (entry) => entry.name.slice(LAYER_CONFIG_PREFIX.length).split("#");
export const layerSource = (entry) => layerName(entry)[0];
export const layerFolder = (entry) => layerName(entry).join("/");

const TS_EXTENSIONS = ["ts", "tsx", "mts", "cts"];
const SOURCE_EXTENSIONS = [...TS_EXTENSIONS, "js", "jsx", "mjs", "cjs"];

export const layerProbes = (entry) => TS_EXTENSIONS.map((ext) => `${layerFolder(entry)}/harness-probe.${ext}`);

export const layerBoundaries = ({ source = "src", layers }) =>
  layers.map(({ name, forbidden = [], patterns = [] }) => ({
    name: `${LAYER_CONFIG_PREFIX}${source}#${name}`,
    files: [`${source}/${name}/**/*.{${SOURCE_EXTENSIONS.join(",")}}`],
    rules: {
      "no-restricted-imports": [
        "error",
        {
          patterns: [
            ...forbidden.map((target) => ({
              regex: `(^|[/@])${target}(/|$)`,
              message: layerBoundaryViolated(name, target),
            })),
            ...patterns,
          ],
        },
      ],
    },
  }));
