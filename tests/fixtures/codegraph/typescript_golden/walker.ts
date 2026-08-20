import defaultThing, { named as renamed, other } from "./shapes";
import * as ns from "./shapes";
import "./shapes";

export const arrowConst = (a: number, b: string): string => b;
var functionExpression = function (x: number) { return x; };

export const literalApi = {
  shorthand(a: number) { return a; },
  pairValued: function (b: number) { return b; },
  arrowValued: (c: number) => c,
};

export function outerFunction(seed: number) {
  function innerFunction(step: number) { return seed + step; }
  return innerFunction;
}

export class Walker extends defaultThing implements other {
  private _hidden: number = 1;

  async method(a: number): Promise<number> {
    const insideMethod = { shorthand() { return 1; } };
    return a;
  }
}
