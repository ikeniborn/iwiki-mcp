import { foo } from "./foo";

export function use(): typeof foo {
  return foo;
}

class Base {}
class Derived extends Base {}

interface BaseInterface {}
interface DerivedInterface extends BaseInterface {}
class Impl implements DerivedInterface {}
