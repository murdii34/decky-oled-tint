declare module "*.svg" {
  const content: string;
  export default content;
}

declare module "*.png" {
  const content: string;
  export default content;
}

declare module "*.jpg" {
  const content: string;
  export default content;
}

// package.json (rollup's json plugin): the version for the test pattern
declare module "*/package.json" {
  const pkg: { version: string };
  export default pkg;
}
