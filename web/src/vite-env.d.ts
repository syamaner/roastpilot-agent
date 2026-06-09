/// <reference types="vite/client" />

// CSS imports are handled by Vite (side-effect imports); declare them so tsc is
// happy. The `uplot/dist/*.css` import has no bundled types.
declare module "*.css";
declare module "uplot/dist/uPlot.min.css";
