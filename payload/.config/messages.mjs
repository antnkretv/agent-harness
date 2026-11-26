export const layerBoundaryViolated = (layer, target) =>
  `Слой ${layer} не импортирует ${target}: направление зависимостей задает docs/platform/architecture/gates.md.`;
