import { build, Shape } from './shapes';

class Panel extends Shape {}

function local() {
  return 1;
}

export function run() {
  build();
  local();
  return new Panel();
}
