import { Shape } from "./shapes";

export const View = (props: { shape: Shape }) => <div id="v">{props.shape.size}</div>;

export function identity<T>(value: T): T { return value; }
