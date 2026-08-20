export type Alias = string;
export enum Mode { On, Off }
export interface Base { id: string; }
export interface Shape extends Base { size: number; }

export namespace Outer {
  export class Base2 {}
  export class Inner extends Base2 {}
}
