export function greet(name: string): string {
  return name;
}

export class Animal {
  speak(sound: string): void {}
}

interface Named {
  name: string;
}

type Alias = string | number;

enum Color {
  Red,
  Green,
  Blue,
}

export const add = (a: number, b: number): number => a + b;
